"""托管运行意图与自动开始；停止和失败保持暂停。"""
import json
import secrets

from .contracts import ManagedError, config

BUSY = {"preparing", "starting", "downloading", "quantizing", "starting_service",
        "checking_service", "loading_classifier", "recovering", "stopping"}


def intent(c, device_id, action):
    c.execute("INSERT INTO managed_policy VALUES(?,?) ON CONFLICT(device_id) DO UPDATE SET desired=excluded.desired",
              (device_id, "running" if action == "start" else "paused"))


def arrange(c, dispatch, session, device, state, cfg, caps, now):
    row = c.execute("SELECT desired FROM managed_policy WHERE device_id=?", (device["device_id"],)).fetchone()
    if not row or row["desired"] != "running":
        return
    if state.get("status") == "failed":
        intent(c, device["device_id"], "stop")
        return
    if state.get("running") or state.get("history_running") or state.get("status") in BUSY:
        return
    if c.execute("SELECT 1 FROM managed_commands WHERE session_id=? AND status IN ('queued','accepted')", (session,)).fetchone():
        return
    if c.execute("SELECT 1 FROM leases WHERE device_id=? AND kind='active' AND status='open'", (device["device_id"],)).fetchone() is None:
        if dispatch._pick_package(c, device, dispatch.params()) is None:
            return
    try:
        config({}, cfg, caps)
    except ManagedError as exc:
        c.execute("UPDATE managed_sessions SET automation_error=? WHERE session_id=?", (str(exc), session))
        intent(c, device["device_id"], "stop")
        return
    seq = c.execute("SELECT next_seq FROM managed_sessions WHERE session_id=?", (session,)).fetchone()["next_seq"]
    c.execute("INSERT INTO managed_commands(id,session_id,seq,action,config,actor,created_at,updated_at)"
              " VALUES(?,?,?,'start','{}','auto',?,?)", ("auto-" + secrets.token_hex(12), session, seq, now, now))
    c.execute("UPDATE managed_sessions SET next_seq=next_seq+1,automation_error='' WHERE session_id=?", (session,))
