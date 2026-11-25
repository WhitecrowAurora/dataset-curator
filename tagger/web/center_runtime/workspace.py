"""本机身份代理工作区列表与指定领取。"""
import json
import httpx
from ...center_runtime.network import httpx_options
from fastapi import APIRouter, HTTPException


def request(center, action, body):
    try:
        identity=json.loads(center.worker_cfg.read_text(encoding="utf-8"))
        if (not isinstance(identity,dict)
                or any(not isinstance(identity.get(k),str) or not identity[k] for k in ("server","user_id","device_id","secret"))):
            raise ValueError("请先连接调度中心登记本机身份")
        headers={k:identity.get(k) for k in ("user_id","device_id","secret")}
        if action in ("workspace","reserve"):
            cfg=center.config()
            mode = cfg.get("mode", "both")
            body={**body,"parts":mode if mode != "both" else cfg.get("parts_pref") or "both"}
        response=httpx.post(identity["server"].rstrip("/")+"/api/v1/"+action,
                            json={**body,**headers},timeout=httpx.Timeout(20,connect=10),follow_redirects=False,
                            **httpx_options(identity['server'], center.config()))
        try:
            data=response.json()
        except ValueError as exc:
            if response.status_code >= 500:
                raise ValueError(f"调度中心暂时不可用（HTTP {response.status_code}），请稍后重试；本地分派仍保留") from exc
            raise ValueError(f"调度中心返回了非 JSON 内容（HTTP {response.status_code}），请检查中心地址或代理") from exc
        if not isinstance(data,dict):raise ValueError("调度中心返回格式不正确")
        if response.status_code!=200:
            raise ValueError(data.get("error") or "调度中心请求失败")
        return data
    except (OSError,KeyError,ValueError,httpx.HTTPError) as exc:
        raise HTTPException(400,center.credentials.redact(str(exc))) from exc


def router(center):
    routes=APIRouter(prefix="/api/center")
    from .offline import OfflineTasks
    offline=OfflineTasks(center)
    center.offline=offline
    from .leases import LeaseControl
    center.leases = LeaseControl(center, request)
    from .dispatch_plans import mount as mount_plans
    mount_plans(routes, center, request)
    @routes.post('/release', status_code=202)
    def release(body:dict):
        try:
            center.leases.start(body)
            return center.snapshot()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    @routes.get("/workspace")
    def packages(offset:int=0):
        data = request(center,"workspace",{"offset":offset})
        data['allow_manual_claim'] = False
        data['queue_only'] = False
        return data
    @routes.post("/reserve")
    def reserve(body:dict):
        if center.leases.running:
            raise HTTPException(400, '正在释放租约，请等待服务器确认')
        if center.runtime.active and center.config()['hosting_mode'] == 'passive':
            raise HTTPException(400,'托管中由中心分配任务')
        items=body.get("items")
        if not isinstance(items,list) or not items:
            raise HTTPException(400,"请选择分包")
        data=request(center,"reserve",{"items":items})
        center._board_cache=None
        return data
    @routes.post("/queue")
    def queue(body:dict):
        if center.leases.running:
            raise HTTPException(400, '正在释放租约，请等待服务器确认')
        if center.runtime.active and center.config()['hosting_mode'] == 'passive':
            raise HTTPException(400,'托管中由中心分配任务')
        return request(center,"queue",body)
    @routes.get("/offline")
    def offline_state():return offline.snapshot()
    @routes.put("/offline/config")
    def offline_config(body:dict):
        try:
            config=dict(body)
            token=config.pop("hf_token",None);remember=config.pop("remember_hf",None)
            if token is not None and not isinstance(token,str):raise ValueError("HF token 必须是字符串")
            if remember is not None and type(remember) is not bool:raise ValueError("是否记住 token 必须为布尔值")
            result=offline.configure(config)
            center.credentials.update(token,remember)
            return result
        except (ValueError,OSError,TypeError) as exc:raise HTTPException(400,str(exc)) from exc
    @routes.post("/offline/import")
    def offline_import(body:dict):
        try:return offline.load(body.get("path",""))
        except (ValueError,OSError,TypeError) as exc:raise HTTPException(400,str(exc)) from exc
    @routes.post("/offline/start")
    def offline_start(body:dict):
        from ...config import ConfigError
        try:return offline.start(body.get("shard_dir", ""),body.get("out_dir", ""),body.get("upload") is True)
        except (ValueError,OSError,TypeError,ConfigError) as exc:raise HTTPException(400,str(exc)) from exc
    @routes.post("/offline/stop")
    def offline_stop():return offline.stop()
    @routes.post('/offline/export')
    def offline_export(body:dict):
        from .offline_export import export
        try:return export(offline, body.get('shard_dir', ''), body.get('out_dir', ''), body.get('package', ''))
        except (ValueError, OSError, TypeError) as exc:raise HTTPException(400, str(exc)) from exc
    return routes
