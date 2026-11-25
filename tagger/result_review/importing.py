import hashlib
import json
import re
import sqlite3
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from . import store
from .data import rows


def fetch(source,target):
    from ..center_runtime.network import opener
    from ..web.center import CENTER
    with opener(source,CENTER.config(),redirects=True).open(source,timeout=180) as response, target.open('wb') as output:
        length=0;digest=hashlib.sha256()
        while block:=response.read(1024*1024):
            length+=len(block)
            if length>4*1024**3:raise ValueError('结果或索引文件超过 4 GB，请拆成多个导出')
            digest.update(block);output.write(block)
        expected=response.headers.get('X-Artifact-SHA256')
        if expected and digest.hexdigest()!=expected:raise ValueError('结果下载 SHA256 不一致，请重新下载')
    return target


def safe_package(value):
    value=str(value or '').replace('\\','/')
    if not value or value.startswith('/') or any(p in ('','..','.') for p in value.split('/')) or ':' in value:
        raise ValueError('分包路径不正确')
    return value


def ingest(sid,source,repo=''):
    folder=store.directory(sid)
    parsed=urlsplit(source)
    if parsed.scheme in ('http','https'):
        suffix=Path(parsed.path).suffix.lower()
        path=fetch(source,folder/('download'+(suffix if suffix in ('.parquet','.json','.jsonl') else '.bin')))
    else:
        path=Path(source)
        if not path.is_file():raise ValueError('结果文件不存在')
    manifest={};archive=None
    try:
        if zipfile.is_zipfile(path):
            archive=zipfile.ZipFile(path)
            if archive.getinfo('manifest.json').file_size>16*1024**2:raise ValueError('结果来源清单过大')
            manifest=json.loads(archive.read('manifest.json'))
            if manifest.get('format')!='lulynx-result-review' or manifest.get('version')!=1:
                raise ValueError('请导入中心生成的 lulynx 结果 ZIP，或直接导入 JSONL / Parquet')
            def entries():
                with archive.open('artifacts.jsonl') as stream:
                    for line in stream:
                        if line.strip():yield json.loads(line)
            iterator=entries()
        else:iterator=rows(path)
        with store.connect(sid) as c:
            for meta in manifest.get('packages',[]):
                package=safe_package(meta['package'])
                c.execute('INSERT INTO packages(repo,pkg,meta) VALUES(?,?,?)',(meta['repo'],package,json.dumps(meta)))
            count=0
            for value in iterator:
                iid=str(value.get('image_id') or '')
                origin=value.get('repo') or repo
                if not iid or not re.fullmatch(r'[\w.-]+/[\w.-]+',origin or ''):
                    raise ValueError('结果缺少 image_id 或源仓库，请填写源仓库后重新导入')
                package=value.get('package') or value.get('hf_shard') or ''
                if package:package=safe_package(package)
                old=c.execute('SELECT payload FROM records WHERE repo=? AND pkg=? AND iid=?',(origin,package,iid)).fetchone()
                if old:
                    existing=json.loads(old[0]);variants=existing.setdefault('variants',{})
                    for part in ('tag','nl'):
                        if value.get(part) and existing.get(part) and value.get(part)!=existing.get(part):
                            item=variants.setdefault(part,dict(state='pending',chosen=None,values=[existing.get(part,'')]))
                            if value[part] not in item['values']:
                                item['values'].append(value[part]);item.update(state='pending',chosen=None)
                        incoming=(value.get('variants') or {}).get(part)
                        if incoming:
                            if part not in variants:variants[part]=incoming
                            else:
                                item=variants[part]
                                for candidate in incoming.get('values',[]):
                                    if candidate not in item['values']:
                                        item['values'].append(candidate);item.update(state='pending',chosen=None)
                    for part in ('tag','nl'):
                        if not existing.get(part) and value.get(part):existing[part]=value[part]
                    existing['outcomes']={**existing.get('outcomes',{}),**value.get('outcomes',{})}
                    value=existing
                    c.execute('UPDATE records SET payload=?,has_tag=?,has_nl=?,has_conflicts=?,has_failed=? WHERE repo=? AND pkg=? AND iid=?',
                        (json.dumps(value),*flags(value),origin,package,iid))
                else:c.execute('INSERT INTO records(repo,pkg,iid,payload,has_tag,has_nl,has_conflicts,has_failed) VALUES(?,?,?,?,?,?,?,?)',
                        (origin,package,iid,json.dumps(value),*flags(value)))
                if package:c.execute('INSERT OR IGNORE INTO packages(repo,pkg,meta) VALUES(?,?,?)',(origin,package,json.dumps(dict(repo=origin,package=package,revision='main'))))
                count+=1
                if count%10000==0:c.commit();store.state(sid,message='已导入 '+str(count)+' 条')
        if not count:raise ValueError('结果文件没有标注记录')
        store.summarize(sid)
        with store.connect(sid) as c:count=c.execute('SELECT SUM(count) FROM totals').fetchone()[0]
        store.state(sid,status='ready',message='导入完成；未定位结果可导入总表进行匹配',records=count)
    finally:
        if archive:archive.close()


def field(row,key):
    for name in key.split('.'):
        row=row.get(name) if isinstance(row,dict) else None
    return row


def flags(value):
    return (bool(value.get('tag')),bool(value.get('nl')),bool(value.get('variants')),
            any(v.get('status')=='failed' for v in (value.get('outcomes') or {}).values()))


def index(sid,path,repo,id_field='image_id',package_field='shard',member_field='file'):
    if not re.fullmatch(r'[\w.-]+/[\w.-]+',repo or ''):raise ValueError('请填写有效源仓库')
    from ..web.center import CENTER
    from huggingface_hub import HfApi,hf_hub_download
    token=CENTER.credentials.effective_token() or None
    revision='main'
    companions=[]
    if not path or path.startswith('hf://'):
        from urllib.parse import parse_qs
        if path:
            parsed=urlsplit(path);parts=parsed.path.strip('/').split('/')
            if parsed.netloc!='datasets' or '/'.join(parts[:2])!=repo:raise ValueError('总表链接与源仓库不一致')
            filename='/'.join(parts[2:]);revision=parse_qs(parsed.query).get('revision',['main'])[0]
        else:
            api=HfApi(token=token);files=api.list_repo_files(repo,repo_type='dataset')
            candidates=[v for v in files if v.rsplit('/',1)[-1].lower() in ('manifest.parquet','manifest.jsonl','metadata.parquet','index.parquet','manifest.json')]
            if len(candidates)!=1:raise ValueError('没有找到唯一总表，请选择文件或填写 hf://datasets/仓库/总表路径')
            filename=candidates[0]
            with store.connect(sid) as c:
                revisions={json.loads(r[0]).get('revision') for r in c.execute('SELECT meta FROM packages WHERE repo=?',(repo,))}
            pinned=[v for v in revisions if v and len(v)==40]
            if len(set(pinned))==1:revision=pinned[0]
        api=HfApi(token=token)
        revision=api.repo_info(repo,repo_type='dataset',revision=revision).sha
        if repo=='MatchaCuratio/Anime-2024Dru768-8M' and filename=='manifest.jsonl' and package_field=='shard':
            from .indices import companion_tables
            companions,revision=companion_tables(sid,repo,revision,token)
            path=companions[0]
        else:path=hf_hub_download(repo,filename,repo_type='dataset',revision=revision,token=token)
        sample=next(iter(rows(Path(path))),{}) if not companions else {}
        if not companions and package_field=='shard' and not field(sample,package_field) and sample.get('src_shard'):
            files=api.list_repo_files(repo,repo_type='dataset',revision=revision)
            if sample['src_shard'] not in files:
                # src_shard 可能是重打包前的来源；实际包对应的 sidecar ID 清单才可定位。
                from .indices import companion_tables
                companions,revision=companion_tables(sid,repo,revision,token)
    elif path.startswith(('http://','https://')):
        suffix=Path(urlsplit(path).path).suffix.lower()
        path=fetch(path,store.directory(sid)/('index'+suffix))
    path=Path(path)
    # 本地保存的同仓库总表也使用旧 src_shard，与 HF 导入保持相同口径。
    if not companions and repo=='MatchaCuratio/Anime-2024Dru768-8M' and package_field=='shard':
        sample=next(iter(rows(path)),{})
        if not field(sample,'shard') and sample.get('src_shard'):
            from .indices import companion_tables
            companions,revision=companion_tables(sid,repo,revision,token)
    signature=[repo,str(path.resolve()),path.stat().st_size,path.stat().st_mtime_ns,id_field,package_field,member_field,
        [str(v) for v in companions]]
    cache_root=store.ROOT/'indices';cache_root.mkdir(parents=True,exist_ok=True)
    cached=cache_root/(hashlib.sha256(json.dumps(signature).encode()).hexdigest()+'.sqlite3')
    from ..storage.locking import FileLock
    from ..storage.atomic import read_json,write_json
    lock=FileLock(cached.with_suffix('.lock'))
    lock.acquire(blocking=True)
    try:
        if not cached.is_file():
            temporary=cached.with_suffix('.partial')
            db=sqlite3.connect(temporary)
            try:
                db.execute('CREATE TABLE IF NOT EXISTS mapping(repo TEXT NOT NULL,iid TEXT NOT NULL,pkg TEXT NOT NULL,member TEXT NOT NULL,PRIMARY KEY(repo,iid,pkg,member)) WITHOUT ROWID')
                db.execute('DELETE FROM mapping')
                count=0
                def mappings():
                    if not companions:yield from rows(path);return
                    for table in companions:
                        pkg=table.name.replace('.artist.parquet','.tar')
                        for value in rows(table):
                            yield {'image_id':value.get('id'),'shard':pkg,'file':str(value.get('id'))+'.jpg'}
                for value in mappings():
                    iid=field(value,id_field)
                    pkg=field(value,package_field) or (value.get('src_shard') if package_field=='shard' else None)
                    member=field(value,member_field)
                    if iid is None or not pkg:continue
                    pkg=safe_package(pkg)
                    db.execute('INSERT OR IGNORE INTO mapping VALUES(?,?,?,?)',(repo,str(iid),pkg,str(member or '')))
                    count+=1
                    if count%20000==0:db.commit();store.state(sid,message='总表索引 '+str(count)+' 条')
                if not count:raise ValueError('总表没有匹配的 ID / 分包列，请检查字段名')
                db.commit()
            finally:db.close()
            temporary.replace(cached)
    finally:lock.close()
    refs=read_json(store.directory(sid)/'indices.json') or {};refs[repo]=str(cached)
    write_json(store.directory(sid)/'indices.json',refs)
    with store.connect(sid) as c:
        count=c.execute('SELECT COUNT(*) FROM mapping WHERE repo=?',(repo,)).fetchone()[0]
        located=0
        # 游标分批读取，几百万条结果也不会一次载入内存。
        last=''
        while True:
            unresolved=list(c.execute("SELECT iid FROM records WHERE repo=? AND pkg='' AND iid>? ORDER BY iid LIMIT 2000",(repo,last)))
            if not unresolved:break
            for iid, in unresolved:
                packages=list(c.execute('SELECT DISTINCT pkg FROM mapping WHERE repo=? AND iid=?',(repo,iid)))
                if len(packages)!=1:continue
                pkg=packages[0][0]
                if c.execute('SELECT 1 FROM records WHERE repo=? AND pkg=? AND iid=?',(repo,pkg,iid)).fetchone():continue
                c.execute('UPDATE records SET pkg=? WHERE repo=? AND pkg=\'\' AND iid=?',(pkg,repo,iid))
                c.execute('INSERT OR IGNORE INTO packages(repo,pkg,meta) VALUES(?,?,?)',(repo,pkg,json.dumps(dict(repo=repo,package=pkg,revision=revision))))
                located+=1
            last=unresolved[-1][0];c.commit()
        mismatch=c.execute("SELECT COUNT(*) FROM records r WHERE repo=? AND pkg<>'' AND EXISTS (SELECT 1 FROM mapping m WHERE m.repo=r.repo AND m.iid=r.iid) AND NOT EXISTS (SELECT 1 FROM mapping m WHERE m.repo=r.repo AND m.iid=r.iid AND m.pkg=r.pkg)",(repo,)).fetchone()[0]
    store.summarize(sid)
    store.state(sid,status='ready',message=f'总表索引 {count} 条，定位 {located} 条；已有分包与总表不一致 {mismatch} 条',index_mismatches=mismatch)
