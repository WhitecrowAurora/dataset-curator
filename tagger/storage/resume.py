"""每任务续做记录与兼容的最近一次记录；多实例互不覆盖唯一恢复副本。

两个入口，边界必须分清
----------------------
`load()` 只读：给界面回答"上次还有没有没跑完的任务"。它探一次运行锁就立刻
放掉，所以**不能拿它的结果去开跑**——检查与真正占位之间有空隙，两个进程能
先后通过同一个检查，然后各起一个任务往同一个 out_dir 里写（同一批图打两遍、
分片重复追加）。

`claim()` 检查即占用：挑出记录的同时把它的运行锁拿在手上，调用方一直持有到
这次任务真正收尾再 `close()`。别的进程这时连检查都过不去。
"""
from pathlib import Path

from .atomic import read_json, write_json
from .locking import FileLock


def _entry(path: Path, job_id: str) -> Path:
    return path.parent / f"{path.stem}-jobs" / f"{Path(job_id).name}.json"


def running_lock(path: Path, job_id: str) -> FileLock:
    return FileLock(_entry(path, job_id).with_suffix(".running.lock"))


def _usable(item) -> bool:
    """内容层面能不能续做（不看锁）。判据只有两条：参数还在、产物目录还在。"""
    if not isinstance(item, dict) or not isinstance(item.get("spec"), dict):
        return False
    out = item.get("out_dir")
    if out and not Path(out).exists():
        return False
    return True


def _candidates(path: Path):
    """按"最像能续做"的顺序给出候选：主文件指向的那份 → 主文件本身 → 各任务副本。"""
    data = read_json(path, backup=True)
    if isinstance(data, dict):
        entry = read_json(_entry(path, str(data.get("job_id") or "legacy")), backup=True)
        # 完成墓碑（{"finished": true}）不是候选：它代表"这条已经做完了"。
        if isinstance(entry, dict) and not entry.get("finished"):
            yield entry
        yield data
    entries = path.parent / f"{path.stem}-jobs"
    if not entries.is_dir():
        return
    found = []
    for candidate in entries.glob("*.json"):
        item = read_json(candidate, backup=True)
        if isinstance(item, dict) and not item.get("finished") and item.get("spec"):
            found.append(item)
    for item in sorted(found, key=lambda r: r.get("saved_at") or 0, reverse=True):
        yield item


def _pick_locked(path: Path):
    """挑一条可续做的记录并占住它的运行锁，返回 (payload, lock)。

    **调用方必须已持有** `path.lock`。分成这一层是因为 `clear()` 自己就攥着
    那把锁：Windows 的字节锁按文件句柄算，同一进程再开一个句柄去抢就是自己
    等自己。
    """
    for item in _candidates(path):
        if not _usable(item):
            continue
        lock = running_lock(path, str(item.get("job_id") or "legacy"))
        if not lock.acquire(blocking=False):
            continue
        # 拿到锁之后内容可能刚被别的进程改过（退役/重写），再判一次。
        if _usable(item):
            return item, lock
        lock.close()
    return None, None


def _pick(path: Path):
    with FileLock(path.with_suffix(".lock")):
        return _pick_locked(path)


def claim(path: Path):
    """挑出能续做的那条记录并**当场占住**。返回 (payload, lock)。

    lock 已持有；调用方必须一直拿到这次任务结束再 `close()`。没有可续做的
    记录时返回 (None, None)。
    """
    return _pick(path)


def save(path: Path, payload: dict) -> None:
    with FileLock(path.with_suffix(".lock")):
        write_json(_entry(path, payload["job_id"]), payload, backup=True)
        write_json(path, payload, backup=True)


def load(path: Path):
    """只读：界面问"还有没有能继续的任务"用这个，别用它开跑（用 `claim`）。"""
    data, lock = _pick(path)
    if lock is not None:
        lock.close()
    return data


def clear(path: Path, job_id: str | None) -> None:
    with FileLock(path.with_suffix(".lock")):
        current = read_json(path, backup=True) or {}
        selected = job_id
        if selected is None:
            available, lock = _pick_locked(path)
            if lock is not None:
                lock.close()
            selected = (available or {}).get("job_id") or current.get("job_id")
        if selected:
            entry = _entry(path, selected)
            # 先写完成记录，清理备份途中退出也不会复活任务。
            write_json(entry, {"finished": True})
            entry.with_suffix(".json.bak").unlink(missing_ok=True)
            # 保留小型完成记录，避免旧的最近任务指针/备份在崩溃后复活它。
        if job_id is None or current.get("job_id") == job_id:
            write_json(path, {"finished": True})
            path.with_suffix(path.suffix + ".bak").unlink(missing_ok=True)
            path.unlink(missing_ok=True)
