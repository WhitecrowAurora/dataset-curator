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


def reason(c, session, device, state):
    row = c.execute("SELECT * FROM managed_policy WHERE device_id=?", (device["device_id"],)).fetchone()
    if row and row["desired"] != "running":
        return "上次执行失败后暂停；修正配置后点击开始" if row['pause_reason'] == 'failure' else "自动接单已暂停，点击开始恢复"
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
    row = c.execute("SELECT desired FROM managed_policy WHERE device_id=?", (device["device_id"],)).fetchone()
    if not row or row["desired"] != "running":
        return reason(c, session, device, state)
    if state.get("status") == "failed":
        intent(c, device["device_id"], "stop", reason="failure")
        return reason(c, session, device, state)
    if state.get("running") or state.get('preparing') or state.get("history_running") or state.get('pending_action') or state.get("status") in BUSY:
        return reason(c, session, device, state)
    if c.execute("SELECT 1 FROM managed_commands WHERE session_id=? AND status IN ('queued','accepted')", (session,)).fetchone():
        return reason(c, session, device, state)
    # 状态是在 HTTP 请求开始前采样的，applied 确认与快照可相差一个轮询。
    recent = c.execute("SELECT updated_at FROM managed_commands WHERE session_id=? AND action='start'"
                       " AND status='applied' ORDER BY seq DESC LIMIT 1", (session,)).fetchone()
    if recent and now - recent['updated_at'] < 15:
        return "开始指令已执行，等待最新运行状态"
    if c.execute("SELECT 1 FROM leases WHERE device_id=? AND kind='active' AND status='open'", (device["device_id"],)).fetchone() is None:
        if dispatch._pick_package(c, device, dispatch.params()) is None:
            return reason(c, session, device, state)
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
