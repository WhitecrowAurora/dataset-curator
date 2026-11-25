"""仅允许顺序指令确认过的并发调整，其他执行环境保持本机管理。"""
import json

from .contracts import ManagedError, config

FIELDS = {"tag_batch", "concurrency"}


def validate(action, changes, base, caps):
    if action not in caps.get("actions", []):
        raise ManagedError("此客户端尚不支持并发调整／探测，请更新客户端")
    if action == "probe":
        if changes:
            raise ManagedError("探测指令不能包含配置")
        return
    if not changes or set(changes) - FIELDS:
        raise ManagedError("只支持修改 NL 并发与 TAG 批大小")
    permissions=caps.get('remote_concurrency')
    if permissions is not None and any(not permissions.get(key,False) for key in changes):
        raise ManagedError('成员尚未允许服务器调整对应并发')
    config(changes, base, caps)


def confirmed(c, session, seq, old, new):
    if old == new:
        return
    expected = dict(old)
    for row in c.execute("SELECT config FROM managed_commands WHERE session_id=? AND action='configure'"
                         " AND seq<=? AND status='applied' ORDER BY seq", (session, seq)):
        expected.update(json.loads(row["config"]))
    if expected != new:
        raise ManagedError("执行配置变化没有对应的已确认调整指令，请先退出托管", 409)
