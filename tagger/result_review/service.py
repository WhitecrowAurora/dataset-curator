import json
import threading
import time
from . import store,importing,images,catalog

LOCK=threading.RLock()
RUNNING={}
CANCEL={}


def background(sid,operation):
    with LOCK:
        if sid in RUNNING and RUNNING[sid].is_alive():raise ValueError('此核验任务正在处理，请等待完成')
        def run():
            try:operation()
            except Exception as exc:
                from ..web.center import CENTER
                store.state(sid,status='failed',message=CENTER.credentials.redact(str(exc)))
        thread=threading.Thread(target=run,name='result-review-'+sid[:8],daemon=True)
        RUNNING[sid]=thread;thread.start()


def import_result(source,repo):
    sid=store.create();background(sid,lambda:importing.ingest(sid,source,repo));return store.view(sid)


def import_index(sid,body):
    def run():
        store.state(sid,status='indexing',message='正在准备总表与分包索引')
        importing.index(sid,body.get('path',''),body.get('repo',''),body.get('id_field','image_id'),
            body.get('package_field','shard'),body.get('member_field','file'))
    background(sid,run)
    return store.view(sid)


def prepare_packages(sid):
    background(sid,lambda:catalog.prepare(sid))
    return store.view(sid)


def download(sid,body):
    from ..web.center import CENTER
    from ..web.hf_downloads import global_proxy
    cfg=CENTER.config();cancel=threading.Event()
    packages=body.get('packages',[])
    if not isinstance(packages,list) or not packages:raise ValueError('请选择要下载的分包')
    for item in packages:
        if not isinstance(item,dict) or not item.get('repo') or not item.get('pkg'):raise ValueError('分包格式不正确')
    def run():
        for item in packages:images.update(sid,item['repo'],item['pkg'],status='queued',message='等待下载')
        images.download(sid,packages,CENTER.credentials.effective_token(),global_proxy(),cfg.get('download_conns',4),cancel)
    with LOCK:
        background(sid,run)
        CANCEL[sid]=cancel
    return store.view(sid)


def records(sid,repo,pkg,offset=0,limit=20):
    with store.connect(sid) as c:
        total=c.execute('SELECT COUNT(*) FROM records WHERE repo=? AND pkg=?',(repo,pkg)).fetchone()[0]
        rows=c.execute('SELECT r.*,m.member FROM records r LEFT JOIN members m ON m.repo=r.repo AND m.pkg=r.pkg AND m.iid=r.iid '
                       'WHERE r.repo=? AND r.pkg=? ORDER BY r.iid LIMIT ? OFFSET ?',(repo,pkg,limit,offset))
        items=[dict(image_id=r['iid'],payload=json.loads(r['payload']),review=r['review'],
                    edited=json.loads(r['edited']) if r['edited'] else None,image_available=bool(r['member'])) for r in rows]
    return dict(items=items,total=total)


def decide(sid,body):
    status=body.get('review')
    if status not in ('','pass','modify','redo'):raise ValueError('核验状态不正确')
    edited=body.get('edited')
    if edited is not None and (not isinstance(edited,dict) or set(edited)-{'tag','nl'} or any(not isinstance(v,str) for v in edited.values())):
        raise ValueError('修改只接受 TAG / NL 文本')
    with store.connect(sid) as c:
        row=c.execute('SELECT payload FROM records WHERE repo=? AND pkg=? AND iid=?',
            (body['repo'],body['pkg'],body['image_id'])).fetchone()
        if not row:raise ValueError('结果记录不存在')
        if edited:
            from .data import text
            from ..text_encoding.tokenizer import TrainingText
            payload=json.loads(row['payload'])
            effective={part:edited.get(part,text(payload.get(part),part)) for part in ('tag','nl')}
            counter=TrainingText()
            if counter.count(effective['nl'],effective['tag'])>min(512,counter.spec.max_length):
                raise ValueError('修改后的训练文本超过 token 硬上限，请精简')
        if not c.execute('UPDATE records SET review=?,edited=?,updated=? WHERE repo=? AND pkg=? AND iid=?',
            (status,json.dumps(edited) if edited is not None else '',time.time(),body['repo'],body['pkg'],body['image_id'])).rowcount:
            raise ValueError('结果记录不存在')
        c.execute("UPDATE totals SET reviewed=(SELECT COUNT(*) FROM records WHERE repo=? AND pkg=? AND review<>'') WHERE repo=? AND pkg=?",
            (body['repo'],body['pkg'],body['repo'],body['pkg']))
    return dict(ok=True)


def export(sid):
    path=store.directory(sid)/'review-decisions.jsonl'
    with store.connect(sid) as c,path.open('w',encoding='utf-8') as output:
        for row in c.execute('SELECT * FROM records WHERE review<>\'\' ORDER BY repo,pkg,iid'):
            output.write(json.dumps(dict(repo=row['repo'],package=row['pkg'],image_id=row['iid'],review=row['review'],
                original=json.loads(row['payload']),edited=json.loads(row['edited']) if row['edited'] else None,updated=row['updated']),ensure_ascii=False)+'\n')
    return path
