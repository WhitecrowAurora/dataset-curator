"""把分块回传包装成"可轮询的任务"，给 Upload 页用。

与 `uploads.py`（FTP）分开的原因和它当初分开的理由一样：回传不占 GPU、
不进"同一时刻只允许一个数据任务"的互斥。这里额外做两件事：

1. **后台线程跑 BlobClient**。页面每秒轮询快照，不 SSE、不 WS——搬文件
   的进度用事件管道是浪费，与 FTP 那条保持一致。
2. **目标目录隔离**。commit 的 name 前缀是远端目录：`<remote>/<文件名>`。
   服务端 commit 允许子路径（见 blobs._safe_rel），所以不用为目录另开接口。

★与 FTP 的关键差别在幂等：这里重跑一次几乎不传字节（块已在服务端）。
  所以页面上"再传一次"是安全操作，不需要 .uploaded 标记文件。
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

from ..blob_client import BlobClient, BlobClientError

# 允许回传的产物后缀。.tar/.zip 是封包产物，FTP 那条路拒收它们（uploads.py
# 只放 parquet/jsonl），这里必须收——否则选 webdataset 时"传上去"是空的。
SUFFIXES: tuple[str, ...] = (".parquet", ".jsonl", ".tar", ".zip")
# 与 FTP 页一致：账本不是产物，传上去对账侧也读不出记录
NON_ARTIFACT = {"failures.jsonl", "excused.jsonl", "skipped.jsonl",
                "manifest.jsonl", "state.jsonl"}


def list_uploadable(d: str | Path) -> list[Path]:
    """目录下可回传的分片，按名字排序（同批的 parquet/tar 相邻，便于核对）。"""
    base = Path(d)
    if not base.is_dir():
        return []
    out = []
    for suffix in SUFFIXES:
        for p in base.glob(f"*{suffix}"):
            if p.is_file() and p.name not in NON_ARTIFACT:
                out.append(p)
    return sorted(out, key=lambda p: (p.stem, p.suffix))


class BlobJob:
    """一次分块回传。状态读写跨线程（工作线程 + HTTP 轮询），过锁。"""

    def __init__(self, job_id: str, dir_path: Path, files: list[str], *,
                 url: str, token: str, remote_dir: str) -> None:
        self.id = job_id
        self.dir = dir_path
        self.files = files
        # 令牌只留在内存，快照里只给最后一个字节位以外的提示
        self._secret = token
        self.url = url
        self.remote_dir = (remote_dir or "").strip("/")
        self.status = "pending"          # pending|running|done|stopped|failed
        self.error = ""
        self.created = time.time()
        self.started = 0.0
        self.ended = 0.0
        self.stop_flag = threading.Event()
        self._lock = threading.Lock()
        self._items: dict[str, dict] = {}
        for name in files:
            p = dir_path / name
            self._items[name] = {
                "name": name,
                "bytes": p.stat().st_size if p.is_file() else 0,
                "status": "pending",
                "sent": 0,
                "chunks": 0,
                "uploaded_chunks": 0,
                "skipped_chunks": 0,
                "resumed": False,
                "error": "",
                # 服务端 commit 时回的分片校验结论。只有标注分片会有，
                # 成品包（tar/zip）留空——那不是"每行一张图"的东西。
                "verify": None,
            }

    # ---------------------------------------------------------------- 快照

    def snapshot(self) -> dict:
        with self._lock:
            files = [dict(v) for v in self._items.values()]
        done = sum(1 for f in files if f["status"] == "done")
        failed = sum(1 for f in files if f["status"] == "failed")
        return {
            "id": self.id, "dir": str(self.dir), "status": self.status,
            "error": self.error, "created": self.created,
            "started": self.started, "ended": self.ended,
            # 令牌不出门：只给"是不是带了"
            "target": {"url": self.url, "remote_dir": self.remote_dir,
                       "auth": bool(self._secret)},
            "files": files, "total": len(files), "done": done, "failed": failed,
            "sent_bytes": sum(f["sent"] for f in files),
            "uploaded_chunks": sum(f["uploaded_chunks"] for f in files),
            "skipped_chunks": sum(f["skipped_chunks"] for f in files),
        }

    def _set(self, name: str, **fields) -> None:
        with self._lock:
            self._items[name].update(fields)

    # ---------------------------------------------------------------- 执行

    def run(self) -> None:
        self.status = "running"
        self.started = time.time()
        client = BlobClient(self.url, token=self._secret)
        try:
            for name in self.files:
                if self.stop_flag.is_set():
                    break
                self._set(name, status="uploading")
                remote = f"{self.remote_dir}/{name}" if self.remote_dir else name
                try:
                    rep = client.upload(self.dir / name, remote_name=remote,
                                        on_progress=lambda i, n, _n=name:
                                        self._set(_n, chunks=n))
                except BlobClientError as exc:
                    self._set(name, status="failed", error=str(exc))
                    continue
                if rep.ok:
                    self._set(name, status="done", sent=rep.bytes,
                              chunks=rep.chunks, uploaded_chunks=rep.uploaded_chunks,
                              skipped_chunks=rep.skipped_chunks, resumed=rep.resumed,
                              verify=rep.verify or None)
                else:
                    self._set(name, status="failed", error=rep.error)
            if self.stop_flag.is_set():
                self.status = "stopped"
            elif any(self._items[n]["status"] == "failed" for n in self.files):
                self.status = "failed"
                self.error = "部分文件回传失败（见逐文件错误）；重传只会补缺的块"
            else:
                self.status = "done"
        except Exception as exc:          # noqa: BLE001 - 任何意外都落到状态里
            self.status = "failed"
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.ended = time.time()
            if self.status in ("stopped", "failed"):
                with self._lock:
                    for item in self._items.values():
                        if item["status"] in ("pending", "uploading"):
                            # 失败时把"正在传的那份"也标成未传：它没 commit，
                            # 服务端只有零散块，下游看不到半份文件
                            item["status"] = ("failed" if self.status == "failed"
                                              else "stopped")


class BlobManager:
    """进程级单例。同一时刻只允许一个分块回传在跑（抢同一条带宽没意义）。"""

    def __init__(self) -> None:
        self._jobs: dict[str, BlobJob] = {}
        self._lock = threading.Lock()

    def create(self, dir_path: str, files: list[str], *, url: str, token: str,
               remote_dir: str) -> BlobJob:
        base = Path(dir_path)
        if not base.is_dir():
            raise ValueError(f"目录不存在：{dir_path}")
        if not files:
            raise ValueError("没有勾选任何文件")
        if not url.strip():
            raise ValueError("回传地址不能为空")
        clean: list[str] = []
        for name in files:
            p = Path(name)
            # 先判越界再判后缀：`../a.parquet` 后缀是对的，只用后缀判会放过
            # 一个目录外的文件（虽然 is_file 还会兜一道，但报错要说对原因）
            if p.name != name or ".." in p.parts:
                raise ValueError(f"只支持目录内的文件，收到 {name!r}")
            if p.suffix.lower() not in SUFFIXES:
                raise ValueError(f"只支持目录内的 {'/'.join(SUFFIXES)} 文件，收到 {name!r}")
            if not (base / name).is_file():
                raise ValueError(f"文件不存在：{name}")
            clean.append(name)
        with self._lock:
            running = [j for j in self._jobs.values()
                       if j.status in ("pending", "running")]
            if running:
                raise RuntimeError("已有回传在跑。等它结束或先停止。")
            job = BlobJob(uuid.uuid4().hex[:12], base, clean, url=url.strip(),
                          token=token, remote_dir=remote_dir)
            self._jobs[job.id] = job
        th = threading.Thread(target=job.run, name=f"blob-{job.id}", daemon=True)
        th.start()
        return job

    def get(self, job_id: str) -> BlobJob | None:
        return self._jobs.get(job_id)

    def stop(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.status not in ("pending", "running"):
            return False
        job.stop_flag.set()
        return True


# app.py 持有的进程级单例，与 UPLOADS / MANAGER 同一模式
BLOB_UPLOADS = BlobManager()
