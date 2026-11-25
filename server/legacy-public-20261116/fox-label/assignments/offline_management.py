"""撤销离线占用并删除列表记录，保留历史租约与已回传产物。"""
import re
import time


def remove(store, dispatch, job, actor):
    if not isinstance(job, str) or not re.fullmatch(r'O[0-9a-f]{16}', job):
        raise ValueError('离线任务 ID 不正确')
    with store._write_lock:
        c = store.conn()
        moved = None
        try:
            with dispatch._Tx(c):
                row = c.execute('SELECT * FROM offline_jobs WHERE id=?', (job,)).fetchone()
                if not row:
                    raise ValueError('离线任务不存在')
                if row['deleted_at'] is not None:
                    return dict(ok=True, replay=True, id=job, released=0)
                leases = [dict(r) for r in c.execute(
                    "SELECT l.* FROM leases l JOIN offline_leases o USING(lease_id)"
                    " WHERE o.job_id=? AND l.status='open'", (job,))]
                from scheduling.leases import refresh, release_targets
                for lease in leases:
                    c.execute("UPDATE leases SET status='released',closed_reason='offline_deleted' WHERE lease_id=?",
                              (lease['lease_id'],))
                    release_targets(c, lease)
                    refresh(c, lease['repo'], lease['pkg'])
                c.execute("UPDATE offline_jobs SET status='canceled',deleted_at=? WHERE id=?", (time.time(), job))
                # The export contains a credential. Retain it outside the active download
                # directory, under the state archive, without deleting user artifacts.
                source = store.dir / 'offline' / job
                if source.exists():
                    target = store.dir / '.delete' / 'offline' / job
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        raise ValueError('工单归档位置已存在，请联系管理员检查')
                    source.rename(target)
                    moved = (source, target)
                store.event(c, 'offline_delete', None, None, actor, f'job={job};released={len(leases)}')
                return dict(ok=True, id=job, released=len(leases), results_preserved=True)
        except BaseException:
            if moved:
                moved[1].rename(moved[0])
            raise
        finally:
            c.close()
