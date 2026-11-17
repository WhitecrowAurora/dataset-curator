"""当前派工的产物回传，沿用 worker 身份。"""
from pathlib import Path
import time
from urllib.parse import quote

from .artifacts import shard_stem
from .blob_client import BlobClient, BlobClientError


def deliver(cfg: dict, lease: dict, out_dir: Path, *, budget=None):
    headers = {"X-Fox-User": quote(str(cfg["user_id"]), safe=""),
               "X-Fox-Device": str(cfg["device_id"]),
               "X-Fox-Secret": str(cfg.get("secret") or ""),
               "X-Fox-Lease": str(lease["lease_id"])}
    deadline = time.monotonic() + budget if budget is not None else None
    client = BlobClient(cfg["server"], headers=headers, timeout=300, deadline=deadline)
    if lease.get("resume_supported"):
        from .center_runtime.artifacts import prepare, request
        for path in prepare(lease, out_dir):
            report = client.upload(path)
            if not report.ok:
                raise BlobClientError(report.error)
            print(report.line(), flush=True)
        remaining = deadline - time.monotonic() if deadline else 120
        if remaining <= 0:
            raise BlobClientError('回传等待时间已到，本机产物保留')
        return request(cfg, lease, "reconcile", {}, timeout=remaining)["report"]
    stem = shard_stem(lease["package"])
    paths = sorted(p for p in out_dir.glob("artifacts-*.jsonl")
                   if p.name == f"artifacts-{stem}.jsonl"
                   or p.name.startswith(f"artifacts-{stem}-"))
    if not paths:
        raise BlobClientError(f"没有找到 {lease['package']} 的 JSONL 产物")
    for path in paths:
        report = client.upload(path)
        if not report.ok:
            raise BlobClientError(report.error)
        print(report.line(), flush=True)
