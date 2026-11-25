import hashlib
import json
import secrets
import threading
import time
import uuid
import zipfile
from pathlib import Path


def ensure(c):
    c.execute('CREATE TABLE IF NOT EXISTS result_exports (id TEXT PRIMARY KEY,actor TEXT NOT NULL,'
              'created REAL NOT NULL,expires REAL NOT NULL,token_hash TEXT NOT NULL,revoked INTEGER NOT NULL DEFAULT 0,'
              'state TEXT NOT NULL,items_json TEXT NOT NULL,progress INTEGER NOT NULL DEFAULT 0,'
              'size INTEGER NOT NULL DEFAULT 0,sha256 TEXT NOT NULL DEFAULT \'\',error TEXT NOT NULL DEFAULT \'\')')


def recover(store):
    c=store.conn()
    try:
        ensure(c)
        c.execute("UPDATE result_exports SET state='failed',error='服务重启中断了生成，请重新创建导出' WHERE state IN ('queued','building')")
    finally:c.close()


def listing(store):
    c = store.conn()
    try:
        ensure(c)
        return dict(ok=True,items=[dict(id=r['id'],actor=r['actor'],created=r['created'],expires=r['expires'],
                    revoked=bool(r['revoked']),state=r['state'],packages=len(json.loads(r['items_json'])),
                    progress=r['progress'],size=r['size'],sha256=r['sha256'],error=r['error'])
                    for r in c.execute('SELECT * FROM result_exports ORDER BY created DESC LIMIT 100')])
    finally: c.close()


def create(store, body, actor):
    packages=body.get('packages',[]);repo=body.get('repo')
    days=body.get('days',7)
    if type(days) is not int or not 1<=days<=30:
        raise ValueError('分享有效期应为 1～30 天')
    if not isinstance(packages,list) or not 0<len(packages)<=500 or len(set(packages))!=len(packages):
        raise ValueError('请选择 1～500 个不重复的分片')
    items=[];c=store.conn()
    try:
        ensure(c)
        for pkg in packages:
            row=c.execute('SELECT p.sha256 FROM packages p JOIN package_artifacts a ON a.repo=p.repo AND a.pkg=p.id '
                          'AND a.source_sha256=p.sha256 WHERE p.repo=? AND p.id=?',(repo,pkg)).fetchone()
            if not row: raise ValueError('分片尚无可导出的中心结果：'+str(pkg))
            items.append(dict(repo=repo,package=pkg,sha256=row[0]))
        eid=uuid.uuid4().hex;token=secrets.token_urlsafe(32);now=time.time()
        c.execute('INSERT INTO result_exports(id,actor,created,expires,token_hash,state,items_json) VALUES(?,?,?,?,?,?,?)',
                  (eid,actor,now,now+days*86400,hashlib.sha256(token.encode()).hexdigest(),'queued',json.dumps(items)))
    finally:c.close()
    threading.Thread(target=build,args=(store,eid,items),name='result-export-'+eid[:8],daemon=True).start()
    return dict(ok=True,id=eid,token=token,expires=now+days*86400,state='queued')


def update(store,eid,**fields):
    c=store.conn()
    try:
        c.execute('UPDATE result_exports SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),eid))
    finally:c.close()


def build(store,eid,items):
    folder=Path(store.dir)/'result-exports';folder.mkdir(exist_ok=True)
    temporary=folder/(eid+'.partial');c=store.conn()
    try:
        update(store,eid,state='building')
        # 一次读事务固定正文、候选及来源；按分片写 ZIP，不把整仓库载入内存。
        c.execute('BEGIN')
        manifest=dict(format='lulynx-result-review',version=1,created=time.time(),packages=[])
        with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=3,allowZip64=True) as archive:
            with archive.open('artifacts.jsonl','w',force_zip64=True) as output:
                for index,item in enumerate(items,1):
                    repo,pkg,sha=item['repo'],item['package'],item['sha256']
                    source=c.execute('SELECT * FROM packages WHERE repo=? AND id=?',(repo,pkg)).fetchone()
                    if not source or source['sha256']!=sha:raise ValueError('源分片已变化，请重新创建导出')
                    artifact=c.execute('SELECT * FROM package_artifacts WHERE repo=? AND pkg=? AND source_sha256=?',(repo,pkg,sha)).fetchone()
                    if not artifact:raise ValueError('中心结果已变化，请重新创建导出')
                    cfg=c.execute('SELECT config_json FROM source_configs WHERE repo=?',(repo,)).fetchone()
                    cfg=json.loads(cfg[0]) if cfg else {}
                    verified=c.execute('SELECT revision FROM source_lists WHERE repo=? AND pkg=? AND source_sha256=?',(repo,pkg,sha)).fetchone()
                    annotation=c.execute('SELECT config_json FROM repo_artist WHERE repo=?',(repo,)).fetchone()
                    artist=json.loads(annotation[0]) if annotation else {}
                    revision=verified[0] if verified else artist.get('revision') or cfg.get('revision') or 'main'
                    provenance=dict(repo=repo,package=pkg,size=source['size'],sha256=sha,revision=revision,
                                    manifest={k:v for k,v in cfg.items() if k in ('path','id_field','package_field','member_field','rows_key')})
                    variants={}
                    for conflict in c.execute('SELECT * FROM artifact_conflicts WHERE repo=? AND pkg=? AND source_sha256=?',(repo,pkg,sha)):
                        variants.setdefault(conflict['image_id'],{})[conflict['part']]=dict(state=conflict['state'],
                            chosen=conflict['chosen'],values=json.loads(conflict['variants_json']))
                    rows=json.loads(artifact['rows_json']);provenance['count']=len(rows)
                    manifest['packages'].append(provenance)
                    for row in rows:
                        value={**row,'repo':repo,'package':pkg,'source_sha256':sha}
                        if row['image_id'] in variants:value['variants']=variants[row['image_id']]
                        output.write((json.dumps(value,ensure_ascii=False)+'\n').encode('utf-8'))
                    update(store,eid,progress=index)
            archive.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False))
        final=folder/(eid+'.zip');temporary.replace(final)
        with final.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        update(store,eid,state='ready',size=final.stat().st_size,sha256=digest)
    except Exception as exc:
        update(store,eid,state='failed',error=str(exc)[:500])
    finally:c.close()


def revoke(store,eid):
    c=store.conn()
    try:
        ensure(c)
        if not c.execute('UPDATE result_exports SET revoked=1 WHERE id=?',(eid,)).rowcount:
            raise ValueError('导出记录不存在')
    finally:c.close()
    return dict(ok=True)


def file(store,eid,token=None):
    if not isinstance(eid,str) or len(eid)!=32 or any(ch not in '0123456789abcdef' for ch in eid):
        raise ValueError('结果导出 ID 不正确')
    c=store.conn()
    try:
        ensure(c);row=c.execute('SELECT * FROM result_exports WHERE id=?',(eid,)).fetchone()
    finally:c.close()
    if not row:raise ValueError('导出不存在')
    if token is not None and (row['revoked'] or row['expires']<=time.time() or
            not secrets.compare_digest(row['token_hash'],hashlib.sha256(token.encode()).hexdigest())):
        raise ValueError('分享链接已失效或已撤销')
    if row['state']!='ready':raise ValueError('结果快照尚未准备完成：'+row['state'])
    path=Path(store.dir)/'result-exports'/(eid+'.zip')
    if not path.is_file():raise ValueError('结果文件不可用')
    return path,dict(row)
