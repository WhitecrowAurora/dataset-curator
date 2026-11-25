import json
import re
import sqlite3
import time
import uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]/'work'/'result-review'


class ReviewConnection(sqlite3.Connection):
    def __exit__(self,*args):
        try:return super().__exit__(*args)
        finally:self.close()


def directory(sid):
    if not re.fullmatch('[0-9a-f]{32}',sid or ''):raise ValueError('核验任务 ID 不正确')
    path=ROOT/sid
    if not path.is_dir():raise ValueError('核验任务不存在')
    return path


def connect(sid):
    c=sqlite3.connect(directory(sid)/'review.sqlite3',timeout=30,factory=ReviewConnection)
    c.row_factory=sqlite3.Row
    from ..storage.atomic import read_json
    refs=read_json(directory(sid)/'indices.json') or {}
    sources=[]
    for i,path in enumerate(refs.values()):
        if not Path(path).is_file():continue
        name='index'+str(i)
        c.execute('ATTACH DATABASE ? AS '+name,(path,))
        sources.append('SELECT * FROM '+name+'.mapping')
    c.execute('CREATE TEMP VIEW mapping AS '+(' UNION ALL '.join(sources) if sources else
              'SELECT \'\' repo,\'\' iid,\'\' pkg,\'\' member WHERE 0'))
    return c


def create():
    ROOT.mkdir(parents=True,exist_ok=True);sid=uuid.uuid4().hex
    folder=ROOT/sid;folder.mkdir()
    with sqlite3.connect(folder/'review.sqlite3') as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE records(repo TEXT NOT NULL,pkg TEXT NOT NULL,iid TEXT NOT NULL,payload TEXT NOT NULL,
            review TEXT NOT NULL DEFAULT '',edited TEXT NOT NULL DEFAULT '',updated REAL,
            has_tag INTEGER NOT NULL DEFAULT 0,has_nl INTEGER NOT NULL DEFAULT 0,
            has_conflicts INTEGER NOT NULL DEFAULT 0,has_failed INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(repo,pkg,iid));
        CREATE TABLE packages(repo TEXT NOT NULL,pkg TEXT NOT NULL,meta TEXT NOT NULL,path TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',message TEXT NOT NULL DEFAULT '',done INTEGER NOT NULL DEFAULT 0,
            total INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(repo,pkg));
        CREATE TABLE members(repo TEXT NOT NULL,pkg TEXT NOT NULL,iid TEXT NOT NULL,member TEXT NOT NULL,
            offset INTEGER NOT NULL DEFAULT -1,length INTEGER NOT NULL DEFAULT -1,
            PRIMARY KEY(repo,pkg,iid));
        CREATE TABLE totals(repo TEXT NOT NULL,pkg TEXT NOT NULL,count INTEGER,reviewed INTEGER,
            tag INTEGER,nl INTEGER,conflicts INTEGER,failed INTEGER,PRIMARY KEY(repo,pkg));
        ''')
    state(sid,status='importing',message='正在导入',created=time.time())
    return sid


def state(sid,**updates):
    from ..storage.atomic import read_json,write_json
    path=directory(sid)/'state.json';value=read_json(path) or {}
    if updates:value.update(updates);write_json(path,value)
    return value


def sessions():
    if not ROOT.exists():return []
    return [dict(id=p.name,**state(p.name)) for p in sorted(ROOT.iterdir(),key=lambda p:p.stat().st_mtime,reverse=True)
            if p.is_dir() and re.fullmatch('[0-9a-f]{32}',p.name) and (p/'state.json').exists()][:30]


def summarize(sid):
    with connect(sid) as c:
        c.execute('DELETE FROM totals')
        c.execute("INSERT INTO totals SELECT repo,pkg,COUNT(*),SUM(review<>''),SUM(has_tag),SUM(has_nl),"
                  'SUM(has_conflicts),SUM(has_failed) FROM records GROUP BY repo,pkg')


def view(sid):
    with connect(sid) as c:
        groups=[]
        for row in c.execute('SELECT * FROM totals ORDER BY repo,pkg'):
            pkg=c.execute('SELECT * FROM packages WHERE repo=? AND pkg=?',(row['repo'],row['pkg'])).fetchone()
            meta=json.loads(pkg['meta']) if pkg else {}
            groups.append({**dict(row),'size':meta.get('size',0),'status':pkg['status'] if pkg else '',
                'message':pkg['message'] if pkg else '', 'done':pkg['done'] if pkg else 0,'total':pkg['total'] if pkg else 0,
                'cached':bool(pkg and pkg['path'] and Path(pkg['path']).is_file())})
    return dict(id=sid,**state(sid),packages=groups)
