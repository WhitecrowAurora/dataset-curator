"""每任务续做记录与兼容的最近一次记录；多实例互不覆盖唯一恢复副本。"""
from pathlib import Path

from .atomic import read_json, write_json
from .locking import FileLock


def _entry(path: Path, job_id: str) -> Path:
    return path.parent / f"{path.stem}-jobs" / f"{Path(job_id).name}.json"


def running_lock(path: Path, job_id: str) -> FileLock:
    return FileLock(_entry(path, job_id).with_suffix(".running.lock"))


def _available(path: Path, item) -> bool:
    if not isinstance(item, dict) or not isinstance(item.get("spec"), dict):
        return False
    out = item.get("out_dir")
    if out and not Path(out).exists():
        return False
    lock = running_lock(path, str(item.get("job_id") or "legacy"))
    if not lock.acquire(blocking=False):
        return False
    lock.close()
    return True


def save(path: Path, payload: dict) -> None:
    with FileLock(path.with_suffix(".lock")):
        write_json(_entry(path, payload["job_id"]), payload, backup=True)
        write_json(path, payload, backup=True)


def load(path: Path):
    with FileLock(path.with_suffix(".lock")):
        return _load(path)


def _load(path: Path):
    data = read_json(path, backup=True)
    if _available(path, data):
        entry = read_json(_entry(path, str(data.get("job_id") or "legacy")), backup=True)
        if not (isinstance(entry, dict) and entry.get("finished")):
            return entry if _available(path, entry) else data
    entries = path.parent / f"{path.stem}-jobs"
    latest = None
    for candidate in entries.glob("*.json"):
        item = read_json(candidate, backup=True)
        if _available(path, item):
            if latest is None or item.get("saved_at", 0) > latest.get("saved_at", 0):
                latest = item
    return latest


def clear(path: Path, job_id: str | None) -> None:
    with FileLock(path.with_suffix(".lock")):
        current = read_json(path, backup=True) or {}
        selected = job_id or current.get("job_id")
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
