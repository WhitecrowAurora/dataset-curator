"""托管运行意图与自动开始；停止和失败保持暂停。"""
import json
import secrets

from .contracts import ManagedError, config

BUSY = {"preparing", "starting", "downloading", "quantizing", "starting_service",
        "checking_service", "loading_classifier", "recovering", "stopping", "probing"}


def intent(c, device_id, action, reason="manual"):
    c.execute("INSERT INTO managed_policy(device_id,desired,pause_reason) VALUES(?,?,?)"
              " ON CONFLICT(device_id) DO UPDATE SET desired=excluded.desired,pause_reason=excluded.pause_reason",
              (device_id, "running" if action == "start" else "paused", "" if action == "start" else reason))
    from time import time
    c.execute('UPDATE managed_policy SET changed_at=?,changed_by=? WHERE device_id=?',
              (time(), reason, device_id))


def reason(c, session, device, state):
    from scheduling.control import running
    row = c.execute("SELECT * FROM managed_policy WHERE device_id=?", (device["device_id"],)).fetchone()
    if row and row["desired"] != "running":
        if row['pause_reason']=='limit':
            return '已达到本轮分片上限；点击开始开启下一轮'
        return "上次执行失败后暂停；修正配置后点击开始" if row['pause_reason'] == 'failure' else "自动接单已暂停，点击开始恢复"
    if not running(c):
        return '管理员已暂停自动派单；当前分片可继续，下一片等待管理员启动'
    if state.get('pending_action'):
        return "完成当前分片后执行" + ("并发调整" if state['pending_action'] == 'configure' else "并发探测")
    if state.get('running'):
        return "正在执行，完成当前分片后继续自动领取"
    if state.get('preparing') or state.get('history_running') or state.get('status') in BUSY:
        return "设备正在准备模型或检查／探测，完成后自动接单"
    if c.execute("SELECT 1 FROM managed_commands WHERE session_id=? AND status IN ('queued','accepted')", (session,)).fetchone():
        return "等待客户端确认已下发指令"
    return "当前没有符合本机能力、工作区或指定范围的空闲分片；有任务后自动领取"


def arrange(c, dispatch, session, device, state, cfg, caps, now):
    from scheduling.control import running
    if not running(c):
        return reason(c, session, device, state)
    row = c.execute("SELECT desired FROM managed_policy WHERE device_id=?", (device["device_id"],)).fetchone()
    if not row or row["desired"] != "running":
        return reason(c, session, device, state)
    from .limits import reached
    if reached(c,device['device_id']):
        intent(c,device['device_id'],'stop',reason='limit')
        return reason(c,session,device,state)
    if state.get("running") or state.get('preparing') or state.get("history_running") or state.get('pending_action') or state.get("status") in BUSY:
        return reason(c, session, device, state)
    if c.execute("SELECT 1 FROM managed_commands WHERE session_id=? AND status IN ('queued','accepted')", (session,)).fetchone():
        return reason(c, session, device, state)
    # 状态是在 HTTP 请求开始前采样的，applied 确认与快照可相差一个轮询。
    recent = c.execute("SELECT updated_at FROM managed_commands WHERE session_id=? AND action='start'"
                       " AND status='applied' ORDER BY seq DESC LIMIT 1", (session,)).fetchone()
    if recent and now - recent['updated_at'] < 15:
        return "开始指令已执行，等待最新运行状态"
    if state.get("status") == "failed":
        intent(c, device["device_id"], "stop", reason="failure")
        return reason(c, session, device, state)
    from .parallel import next_package, required, permissions
    package=next_package(c,dispatch,device)
    if c.execute("SELECT 1 FROM leases WHERE device_id=? AND kind='active' AND status='open'", (device["device_id"],)).fetchone() is None:
        if package is None:
            from assignments.preassignment import mismatch
            for target in c.execute("SELECT * FROM part_assignments WHERE target_type='user' AND target_id=?", (device['user_id'],)):
                different = mismatch(dict(target), cfg, target['part'])
                if different:
                    return '预派任务等待本机配置匹配：' + different + '；请退出托管后调整配置再开始'
            return reason(c, session, device, state)
    changes={key:value for key,value in required(c,device,package).items()
             if cfg.get(key)!=value and permissions(caps).get(key, False)}
    if 'configure' not in caps.get('actions', []):
        changes = {}
    if changes:
        from .tuning import validate
        try:
            validate('configure',changes,cfg,caps)
        except ManagedError as exc:
            return '预派并发等待本机授权或配置修正：'+str(exc)
        seq=c.execute('SELECT next_seq FROM managed_sessions WHERE session_id=?',(session,)).fetchone()['next_seq']
        c.execute("INSERT INTO managed_commands(id,session_id,seq,action,config,actor,created_at,updated_at)"
                  " VALUES(?,?,?,'configure',?,'preassignment',?,?)",
                  ('auto-'+secrets.token_hex(12),session,seq,json.dumps(changes),now,now))
        c.execute('UPDATE managed_sessions SET next_seq=next_seq+1 WHERE session_id=?',(session,))
        return '已按预派任务下发并发，确认后自动接单'
    try:
        config({}, cfg, caps)
    except ManagedError as exc:
        c.execute("UPDATE managed_sessions SET automation_error=? WHERE session_id=?", (str(exc), session))
        intent(c, device["device_id"], "stop", reason="failure")
        return str(exc)
    seq = c.execute("SELECT next_seq FROM managed_sessions WHERE session_id=?", (session,)).fetchone()["next_seq"]
    c.execute("INSERT INTO managed_commands(id,session_id,seq,action,config,actor,created_at,updated_at)"
              " VALUES(?,?,?,'start','{}','auto',?,?)", ("auto-" + secrets.token_hex(12), session, seq, now, now))
    c.execute("UPDATE managed_sessions SET next_seq=next_seq+1,automation_error='' WHERE session_id=?", (session,))
    return "已自动下发开始指令"
