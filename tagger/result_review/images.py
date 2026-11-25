import hashlib
import io
import json
import tarfile
import threading
import time
import zipfile
from pathlib import Path

from . import store


def index_archive(sid,repo,pkg,path):
    from ..naming import sanitize,IMAGE_SUFFIXES
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:entries={v.filename:(-1,v.file_size) for v in archive.infolist() if not v.is_dir()}
    else:
        with tarfile.open(path,'r:*') as archive:
            direct=isinstance(archive.fileobj,io.BufferedReader)
            entries={v.name:(v.offset_data if direct else -1,v.size) for v in archive if v.isfile()}
    aliases={}
    for name in entries:
        if not name.lower().endswith(IMAGE_SUFFIXES):continue
        stem=name.rsplit('.',1)[0]
        for key in {sanitize(stem.replace('/','-')),stem.rsplit('/',1)[-1]}:
            if key in aliases and aliases[key]!=name:aliases[key]=None
            else:aliases.setdefault(key,name)
    with store.connect(sid) as c:
        columns={v[1] for v in c.execute('PRAGMA table_info(members)')}
        for name in ('offset','length'):
            if name not in columns:c.execute('ALTER TABLE members ADD COLUMN '+name+' INTEGER NOT NULL DEFAULT -1')
        c.execute('DELETE FROM members WHERE repo=? AND pkg=?',(repo,pkg))
        found=0
        for iid, in c.execute('SELECT iid FROM records WHERE repo=? AND pkg=?',(repo,pkg)).fetchall():
            candidates=list(c.execute('SELECT DISTINCT member FROM mapping WHERE repo=? AND pkg=? AND iid=? AND member<>\'\'',(repo,pkg,iid)))
            member=candidates[0][0] if len(candidates)==1 and candidates[0][0] in entries else aliases.get(iid)
            if member:
                c.execute('INSERT INTO members(repo,pkg,iid,member,offset,length) VALUES(?,?,?,?,?,?)',
                    (repo,pkg,iid,member,*entries[member]));found+=1
        c.execute('UPDATE packages SET path=?,status=\'ready\',message=? WHERE repo=? AND pkg=?',
                  (str(path),f'可查看 {found} 张；未找到的图片保留待核对',repo,pkg))


def update(sid,repo,pkg,**fields):
    with store.connect(sid) as c:
        c.execute('UPDATE packages SET '+','.join(k+'=?' for k in fields)+' WHERE repo=? AND pkg=?',(*fields.values(),repo,pkg))


def download(sid,packages,token,proxy,conns,cancel):
    from .. import hf_source
    from huggingface_hub import HfApi
    for item in packages:
        if cancel.is_set():return
        repo,pkg=item['repo'],item['pkg']
        try:
            with store.connect(sid) as c:
                row=c.execute('SELECT * FROM packages WHERE repo=? AND pkg=?',(repo,pkg)).fetchone()
            if not row:raise ValueError('分包没有关联结果')
            meta=json.loads(row['meta'])
            update(sid,repo,pkg,status='downloading',message='查询原片版本与大小')
            api=HfApi(token=token or None)
            from .catalog import resolve
            meta=resolve(api,repo,pkg,meta)
            size,revision,sha=meta['size'],meta['revision'],meta.get('sha256','')
            with store.connect(sid) as c:c.execute('UPDATE packages SET meta=? WHERE repo=? AND pkg=?',(json.dumps(meta),repo,pkg))
            # 与现有下载页共享缓存；使用 SHA 子目录隔离历史同名版本。
            shared=hf_source.cache_dir_for(repo)
            source=Path(row['path']) if row['path'] else shared/pkg
            def valid(path):
                if not path.is_file() or path.stat().st_size!=size:return False
                if sha:
                    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()==sha
                return True
            if not valid(source):
                folder=shared/(meta.get('sha256') or revision)
                target=folder/pkg
                if valid(target):
                    index_archive(sid,repo,pkg,target)
                    update(sid,repo,pkg,done=size,total=size)
                    continue
                if target.is_file() and not valid(target):
                    # 保留损坏文件，避免下载器把非空坏缓存当成已完成。
                    backup=shared/'.delete'/'result-review'/str(time.time_ns())/(meta.get('sha256') or revision)/pkg
                    resolved=backup.resolve()
                    if not resolved.is_relative_to(shared.resolve()):raise ValueError('缓存归档路径不正确')
                    backup.parent.mkdir(parents=True,exist_ok=True);target.replace(backup)
                if hf_source.free_bytes(folder.parent)<max(0,size)+256*1024**2:
                    raise ValueError('磁盘空间不足，请换下载缓存位置或整理磁盘；现有文件保留')
                last=[0.0];progress_lock=threading.Lock()
                def progress(done,total):
                    with progress_lock:
                        now=time.monotonic()
                        if now-last[0]<.5 and done!=total:return
                        last[0]=now
                        update(sid,repo,pkg,done=done,total=total,message='下载原图包')
                shard=hf_source.download_shard(repo,pkg,folder,revision=revision,token=token or None,proxy=proxy,
                    conns=conns,verbose=False,on_progress=progress,should_stop=cancel.is_set)
                source=shard.path
                if not valid(source):raise ValueError('下载的原片 SHA256／大小与分享来源不一致，请核对仓库版本')
            index_archive(sid,repo,pkg,source)
            update(sid,repo,pkg,done=size,total=size)
        except Exception as exc:
            from ..web.center import CENTER
            message=CENTER.credentials.redact(str(exc))
            if token:message=message.replace(token,'***')
            update(sid,repo,pkg,status='canceled' if cancel.is_set() else 'failed',message=message)


def thumbnail(sid,repo,pkg,iid,width=512):
    with store.connect(sid) as c:
        row=c.execute('SELECT p.path,m.member,m.offset,m.length FROM packages p JOIN members m ON m.repo=p.repo AND m.pkg=p.pkg '
                      'WHERE p.repo=? AND p.pkg=? AND m.iid=?',(repo,pkg,iid)).fetchone()
    if not row:raise ValueError('该图片尚未下载或未找到唯一关联')
    path=Path(row['path']);member=row['member']
    if row['length']>64*1024**2:raise ValueError('图片文件过大')
    if row['offset']>=0:
        with path.open('rb') as stream:
            stream.seek(row['offset']);raw=stream.read(row['length'])
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            if archive.getinfo(member).file_size>64*1024**2:raise ValueError('图片文件过大')
            raw=archive.read(member)
    else:
        with tarfile.open(path,'r:*') as archive:
            entry=archive.getmember(member)
            if entry.size>64*1024**2:raise ValueError('图片文件过大')
            with archive.extractfile(entry) as image:raw=image.read()
    from PIL import Image,ImageOps
    with Image.open(io.BytesIO(raw)) as image:
        image=ImageOps.exif_transpose(image).convert('RGB');image.thumbnail((width,width))
        output=io.BytesIO();image.save(output,'JPEG',quality=88)
    return output.getvalue()
