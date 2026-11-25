"""分配队列后，为在线托管设备提交原有开始指令。"""
import secrets
from .store import enqueue
from managed import store as managed
from managed.contracts import ManagedError


def dispatch(store, dispatcher, body, actor):
    if "start" in body and type(body["start"]) is not bool:
        raise ValueError("是否同时启动设备必须为布尔值")
    with store._write_lock:
        c=store.conn()
        try:
            if body.get('action') == 'cancel_preassignment':
                from .preassignment import cancel
                result = cancel(c, dispatcher, body)
            elif 'users' in body:
                from .preassignment import assign
                result = assign(c, dispatcher, body, actor)
            else:
                result=enqueue(c,dispatcher,body,actor,
                           on_event=lambda conn,repo,pkg,user,did: store.event(
                               conn,"workspace_dispatch",repo,pkg,actor,f"user={user};device={did}"))
            if 'users' in body or body.get('action') == 'cancel_preassignment':
                store.event(c, 'user_preassignment', None, None, actor, 'assigned=' + str(len(result.get('assigned', []))))
        finally:c.close()
    if 'users' in body or body.get('action') == 'cancel_preassignment':
        return result
    result["starts"]=[]
    if not body.get("start"):return result
    for did in sorted({item["device_id"] for item in result["assigned"]}):
        try:
            rows=managed.view(store,did)["devices"]
            if not rows:raise ManagedError("设备尚未进入被动托管，请在客户端点击开始",409)
            device=rows[0]
            state=device.get("state",{})
            if state.get("running") or state.get("status") in ("preparing","downloading","quantizing","starting","starting_service","loading_classifier"):
                result["starts"].append({"device_id":did,"status":"running"});continue
            managed.enqueue(store,{"device_id":did,"session_id":device["session_id"],
                                   "id":"workspace-"+secrets.token_hex(12),"action":"start","config":{}},actor)
            result["starts"].append({"device_id":did,"status":"queued"})
        except ManagedError as exc:
            result["starts"].append({"device_id":did,"status":"waiting","message":str(exc)})
    return result
