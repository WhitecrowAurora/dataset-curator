"""当前派工的产物回传，沿用 worker 身份。"""
from pathlib import Path
import time
from urllib.parse import quote

from .artifacts import shard_stem
from .blob_client import BlobClient, BlobClientError


def deliver(cfg: dict, lease: dict, out_dir: Path, *, budget=None, source=None):
    from .center_runtime.delivery_state import save
    save(out_dir, lease, 'uploading')
    headers = {"X-Fox-User": quote(str(cfg["user_id"]), safe=""),
               "X-Fox-Device": str(cfg["device_id"]),
               "X-Fox-Secret": str(cfg.get("secret") or ""),
               "X-Fox-Lease": str(lease["lease_id"])}
    deadline = time.monotonic() + budget if budget is not None else None
    client = BlobClient(cfg["server"], headers=headers, timeout=300, deadline=deadline, network=cfg)
    if lease.get("resume_supported"):
        from .center_runtime.artifacts import prepare, request
        for path in prepare(lease, out_dir, source):
            report = client.upload(path)
            if not report.ok:
                raise BlobClientError(report.error, code=report.error_code, report=report.error_report)
            print(report.line(), flush=True)
        remaining = deadline - time.monotonic() if deadline else 120
        if remaining <= 0:
            raise BlobClientError('回传等待时间已到，本机产物保留')
        save(out_dir, lease, 'verifying')
        print('[回传] 文件已上传，正在等待中心逐图验收', flush=True)
        report = request(cfg, lease, "reconcile", {}, timeout=remaining)["report"]
        save(out_dir, lease, 'checked', report=report)
        print('[验收] 中心已接收检查结果，等待登记本轮完成分项', flush=True)
        return report
    stem = shard_stem(lease["package"])
    paths = sorted(p for p in out_dir.glob("artifacts-*.jsonl")
                   if p.name == f"artifacts-{stem}.jsonl"
                   or p.name.startswith(f"artifacts-{stem}-"))
    if not paths:
        raise BlobClientError(f"没有找到 {lease['package']} 的 JSONL 产物")
    for path in paths:
        report = client.upload(path)
        if not report.ok:
            raise BlobClientError(report.error, code=report.error_code, report=report.error_report)
        print(report.line(), flush=True)
