"""分派清单导入、持久待应用通知与联网／离线确认。"""
import json
from pathlib import Path
import threading
import zipfile

from ...storage.atomic import read_json, write_bytes
from .config import execution_config

LIMIT = 32 * 1024 * 1024


def document(data):
    if not isinstance(data,dict) or data.get('format') != 'lulynx-dispatch-plan' or data.get('version') != 1:
        raise ValueError('不是有效的 lulynx 分派清单，请选择 JSON 或用户 ZIP')
    if not isinstance(data.get('id'),str) or len(data['id'])!=25 or not data['id'].startswith('P') or any(x not in '0123456789abcdef' for x in data['id'][1:]):
        raise ValueError('分派编号不正确')
    from urllib.parse import urlsplit
    url = urlsplit(data.get('server',''))
    if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('分派的调度中心地址不正确')
    tasks = data.get('tasks')
    if not isinstance(tasks,list) or not 0<len(tasks)<=10000:
        raise ValueError('分派清单须包含 1～10000 个分项')
    seen = set()
    from huggingface_hub.utils import validate_repo_id
    from ...center_runtime.annotation import options
    for task in tasks:
        if not isinstance(task,dict) or any(not isinstance(task.get(k),str) for k in ('repo','package','part','user_id','device_id')):
            raise ValueError('分派任务字段不正确')
        validate_repo_id(task['repo'])
        pkg = task['package']
        if not pkg or '\\' in pkg or any(p in ('','.','..') for p in pkg.split('/')) or any(ord(ch)<32 or ch in ':*?"<>|' for ch in pkg):
            raise ValueError('分包文件名不正确')
        key = task['repo'],pkg,task['part']
        if key in seen or task['part'] not in ('tag','nl') or not task['user_id']:
            raise ValueError('分派任务重复或分项不正确')
        seen.add(key)
        execution_config(task.get('config',{}));options(task.get('annotation',{}))
    return data


class DispatchPlans:
    def __init__(self, center, request):
        self.center, self.request = center, request
        self.path = center.dir/'dispatch-plans.json'
        self.lock = threading.RLock()
        self.data = {'pending':None,'applied':None,'dismissed':[]}
        if self.path.is_file():
            saved = read_json(self.path)
            if isinstance(saved,dict):self.data.update(saved)
        if self.data['applied'] and self.data['applied'].get('offline_path'):
            try:self.center.offline.load(self.data['applied']['offline_path'])
            except (OSError,ValueError,TypeError):pass

    def save(self):
        write_bytes(self.path,json.dumps(self.data,ensure_ascii=False).encode('utf-8'))

    def snapshot(self):
        with self.lock:
            return {'ok':True,'pending':self.data['pending'],'applied':self.data['applied']}

    def prepare(self, plan):
        plan = document(plan)
        user = self.center.identity().get('user_id') or self.center.config()['user_id']
        recipients = sorted({r['user_id'] for r in plan['tasks']})
        return {**plan,'recipients':recipients,'selected_user':user if user in recipients else recipients[0] if len(recipients)==1 else ''}

    def import_file(self, path):
        source = Path(path).expanduser()
        if not source.is_file() or source.stat().st_size>LIMIT:
            raise ValueError('请选择不超过 32 MiB 的分派 JSON／ZIP')
        if zipfile.is_zipfile(source):
            with zipfile.ZipFile(source) as archive:
                entries=[i for i in archive.infolist() if i.filename.endswith('/分派任务.json')]
                if not entries or sum(i.file_size for i in entries)>LIMIT:
                    raise ValueError('ZIP 中没有用户分派任务清单，或解压内容超过 32 MiB')
                plans=[document(json.loads(archive.read(i).decode('utf-8-sig'))) for i in entries]
                first=plans[0]
                if any(p['id']!=first['id'] or p.get('fingerprint')!=first.get('fingerprint') for p in plans):
                    raise ValueError('ZIP 混入不同分派清单')
                plan={**first,'tasks':[r for p in plans for r in p['tasks']]}
                plan.pop('recipient',None)
        else:
            plan=json.loads(source.read_text(encoding='utf-8-sig'))
        with self.lock:
            self.data['pending']=self.prepare(plan);self.save();return self.snapshot()

    def poll(self):
        with self.lock:
            response=self.request(self.center,'dispatch-plan',{'action':'inbox'})
            ignored=set(self.data['dismissed'])
            if self.data['applied']:ignored.add(self.data['applied']['id'])
            if self.data['pending']:ignored.add(self.data['pending']['id'])
            candidate=next((r for r in response['items'] if r['id'] not in ignored),None)
            if candidate and not self.data['pending']:
                result=self.request(self.center,'dispatch-plan',{'action':'get','id':candidate['id']})
                plan=self.prepare(result['manifest'])
                plan['server']=self.center.identity()['server']
                self.data['pending']=plan;self.save()
            return self.snapshot()

    def dismiss(self):
        with self.lock:
            if self.data['pending']:
                self.data['dismissed']=(self.data['dismissed']+[self.data['pending']['id']])[-100:]
                self.data['pending']=None;self.save()
            return self.snapshot()

    def apply(self, mode, user):
        with self.center.runtime.lock, self.lock:
            if mode not in ('online','offline'):raise ValueError('请选择联网或离线执行')
            if self.center.runtime.active or self.center.runtime.preparing or self.center.runtime.stopping or self.center.running or self.center.history.running or self.center.offline.running or self.center.leases.running:
                raise ValueError('请先停止当前执行并等待停止完成，再应用分派')
            pending=self.data['pending']
            if not pending:raise ValueError('没有待应用分派')
            identity=self.center.identity()
            if not identity.get('device_id') or identity.get('user_id')!=user:
                raise ValueError('请先用所选用户连接调度中心登记本机')
            if identity['server'].rstrip('/')!=pending['server'].rstrip('/'):
                raise ValueError('分派来自另一调度中心，请先连接对应中心')
            remote=self.request(self.center,'dispatch-plan',{'action':'get','id':pending['id']})['manifest']
            if remote['fingerprint']!=pending.get('fingerprint'):raise ValueError('分派已变化，请重新导入')
            local_tasks=[r for r in pending['tasks'] if r['user_id']==user]
            remote_tasks=[r for r in remote['tasks'] if r['user_id']==user]
            if local_tasks!=remote_tasks or pending['repositories']!=remote['repositories']:
                raise ValueError('导入文件与中心分派内容不同，请重新下载')
            mine=[r for r in remote['tasks'] if r['user_id']==user and r['device_id'] in ('',identity['device_id'])]
            if not mine:raise ValueError('没有分给本机设备的任务，请联系管理员调整设备指定')
            config=execution_config(mine[0]['config'])
            if mode=='online' and any(execution_config(r['config'])!=config for r in mine):
                raise ValueError('此用户的任务使用不同执行配置，请按设备分别应用或使用离线执行')
            parts={r['part'] for r in mine}
            config.update(mode='both' if len(parts)==2 else next(iter(parts)),hosting_mode='passive',tag_device='auto',nl_device='auto')
            result=self.request(self.center,'dispatch-plan',{'action':'apply','id':pending['id'],'fingerprint':remote['fingerprint'],'mode':mode})
            if mode=='offline':
                directory=self.center.dir/'dispatch-imports';directory.mkdir(exist_ok=True)
                receipt=directory/(result['manifest']['id']+'.json')
                manifest=result['manifest']
                manifest['config'].update(tag_device='auto',nl_device='auto')
                for task in manifest['tasks']:
                    if 'config' in task:task['config'].update(tag_device='auto',nl_device='auto')
                write_bytes(receipt,json.dumps(manifest,ensure_ascii=False).encode('utf-8'))
                self.center.offline.load(str(receipt))
                receipt=self.center.offline.path
            else:
                for key in ('allow_remote_concurrency','allow_remote_tag_batch'):
                    config[key]=self.center.config().get(key,False)
                self.center.configure(**config,wait=False,parts_pref='',max_packages=0)
            self.data['applied']={'id':pending['id'],'mode':mode,'user_id':user,'tasks':mine,'config':config,
                                  'offline_path':str(receipt) if mode=='offline' else ''}
            self.data['pending']=None;self.save();self.center._board_cache=None
            return self.snapshot()

    def clear(self):
        with self.center.runtime.lock, self.lock:
            if self.center.runtime.active or self.center.runtime.preparing or self.center.runtime.stopping or self.center.running or self.center.history.running or self.center.offline.running or self.center.leases.running:
                raise ValueError('请先停止当前执行并等待停止完成')
            applied=self.data['applied']
            if applied:
                self.request(self.center,'dispatch-plan',{'action':'clear','id':applied['id']})
                if applied['mode']=='offline':
                    self.center.offline.manifest=None
                    self.center.offline.state={'status':'idle','message':'','error':'','tasks':[],'id':''}
            self.data['applied']=None;self.save();return self.snapshot()


def mount(routes, center, request):
    from fastapi import HTTPException
    plans=DispatchPlans(center,request);center.dispatch_plans=plans
    def run(fn,*args):
        try:return fn(*args)
        except (OSError,ValueError,TypeError,KeyError,zipfile.BadZipFile) as exc:
            raise HTTPException(400,center.credentials.redact(str(exc))) from exc
    @routes.get('/dispatch-plans')
    def state():return plans.snapshot()
    @routes.post('/dispatch-plans/check')
    def check():return run(plans.poll)
    @routes.post('/dispatch-plans/import')
    def import_plan(body:dict):return run(plans.import_file,body.get('path',''))
    @routes.post('/dispatch-plans/apply')
    def apply_plan(body:dict):return run(plans.apply,body.get('mode'),body.get('user_id'))
    @routes.post('/dispatch-plans/dismiss')
    def dismiss():return plans.dismiss()
    @routes.post('/dispatch-plans/clear')
    def clear():return run(plans.clear)
