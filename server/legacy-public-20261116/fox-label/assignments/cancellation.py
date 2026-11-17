"""取消尚未开工的用户定向，保留分片和其它调度配置。"""
from scheduling.transitions import atomic


class CancellationError(ValueError):
    pass


def snapshot(c, repo, pkg):
    row = c.execute("SELECT user_id,assigned_at,actor FROM package_targets WHERE repo=? AND pkg=?",
                    (repo, pkg)).fetchone()
    if row is None:
        raise CancellationError("该分片已无待派记录，请刷新列表")
    package = c.execute("SELECT status FROM packages WHERE repo=? AND id=?", (repo, pkg)).fetchone()
    if not package or package['status'] != 'available':
        raise CancellationError("该分片已非未认领状态，请刷新列表")
    if c.execute("SELECT 1 FROM leases WHERE repo=? AND pkg=? AND status='open' LIMIT 1",
                 (repo, pkg)).fetchone():
        raise CancellationError("该分片已在执行或预锁，请先回收租约")
    return dict(reserved_for=row['user_id'], assigned_at=row['assigned_at'], reservation_actor=row['actor'])


@atomic
def cancel(c, store, repo, pkg, actor, expected, audit_extra):
    current = snapshot(c, repo, pkg)
    if current != {key: expected.get(key) for key in current}:
        raise CancellationError("待派记录已变化，请重新确认")
    c.execute("DELETE FROM package_targets WHERE repo=? AND pkg=?", (repo, pkg))
    seq = store.event(c, 'reservation_cancel', repo, pkg, actor,
                      f"previous_user={current['reserved_for']}; {audit_extra}")
    return dict(repo=repo, id=pkg, canceled=True, previous_user=current['reserved_for'], event_seq=seq)
