"""回传：把本地处理好的 parquet / JSONL 分片上传到汇总机 FTP。

与任务簿（jobs.py）分开建模块，有两个原因：

1. 上传不占 GPU。打标跑着的时候照样可以回传上一批的产物，所以它**不进**
   "同一时刻只允许一个任务"的互斥，也不走 WS 事件流——前端每秒轮询一次
   状态就够了，为几 MB/s 的搬运活建一条事件管道是浪费。
2. 生命周期不同。任务簿的 Job 有 tag/pack 两阶段和模型权重；上传只有
   "逐个文件搬"这一件事，一条 FTP 连接传完就关。

停止是**文件间**生效的：storbinary 中途没法干净地掐断（掐断了远端只留
.part，本地文件又没动，得不偿失），所以置位 stop_flag 后当前文件传完
（或失败）就停，剩下的标记为 stopped。单个分片几百 MB，内网也就几十秒。

FTP 密码只存在 UploadJob 实例内存里，**不进快照、不进任何日志**；
快照里的 ftp 段只暴露 host/port/目录/模式这些无敏感字段。
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

from ..upload import FtpConfig, FtpUploader


def shard_rows(path: str | Path) -> int | None:
    """一片里有多少行 = 这一片处理好的图片数。

    两个 sink 都只把**成功**打标的行写进分片，所以行数就是成品数。

    parquet 读 footer 元数据，O(1)，不扫数据；jsonl 只能数行，但一片也就
    2000 行、几毫秒的事，换来的是"没装 pyarrow 也能看"。读不动返回 None，
    页面显示 "-"，不让一个坏文件挡住整个列表。
    """
    p = Path(path)
    if p.suffix == ".jsonl":
        try:
            with p.open("rb") as fh:
                return sum(1 for line in fh if line.strip())
        except OSError:
            return None
    try:
        import pyarrow.parquet as pq
    except Exception:                     # noqa: BLE001 - 可选依赖
        return None
    try:
        return int(pq.ParquetFile(p).metadata.num_rows)
    except Exception:                     # noqa: BLE001 - 半截文件/损坏文件
        return None


class UploadJob:
    """一次回传的全部状态。读写跨线程（工作线程 + HTTP 轮询），过锁。"""

    def __init__(self, job_id: str, dir_path: Path, files: list[str],
                 *, host: str, port: int, user: str, password: str,
                 remote_dir: str, passive: bool):
        self.id = job_id
        self.dir = dir_path
        self.files = files
        self._ftp = dict(host=host, port=port, user=user, password=password,
                         remote_dir=remote_dir, passive=passive)
        # pending | running | done | stopped | failed
        self.status = "pending"
        self.error = ""
        self.created = time.time()
        self.started = 0.0
        self.ended = 0.0
        self.stop_flag = threading.Event()
        self._items: dict[str, dict] = {}
        for name in files:
            p = dir_path / name
            self._items[name] = {
                "name": name,
                "bytes": p.stat().st_size if p.is_file() else 0,
                "rows": shard_rows(p),
                "status": "pending",
                "sent": 0,
                "resumed": False,
                "attempts": 0,
                "error": "",
            }
        self._lock = threading.Lock()

    def snapshot(self) -> dict:
        with self._lock:
            files = [dict(v) for v in self._items.values()]
        done = sum(1 for f in files if f["status"] == "done")
        failed = sum(1 for f in files if f["status"] == "failed")
        sent = sum(f["sent"] for f in files)
        return {
            "id": self.id,
            "dir": str(self.dir),
            "status": self.status,
            "error": self.error,
            "created": self.created,
            "started": self.started,
            "ended": self.ended,
            # 密码与用户名不出门：轮询方只需要知道连的是哪台机器
            "ftp": {"host": self._ftp["host"], "port": self._ftp["port"],
                    "remote_dir": self._ftp["remote_dir"],
                    "passive": self._ftp["passive"]},
            "files": files,
            "total": len(files),
            "done": done,
            "failed": failed,
            "sent_bytes": sent,
        }

    def _set(self, name: str, **fields) -> None:
        with self._lock:
            self._items[name].update(fields)

    # ---- 执行（跑在上传线程里）

    def run(self) -> None:
        from ..upload import UploadResult

        self.status = "running"
        self.started = time.time()
        cfg = FtpConfig(
            host=str(self._ftp["host"]),
            user=str(self._ftp["user"]),
            password=str(self._ftp["password"]),
            remote_dir=str(self._ftp["remote_dir"] or "/"),
            port=int(self._ftp["port"] or 21),
            passive=bool(self._ftp["passive"]),
        )
        uploader = FtpUploader(cfg, verbose=False)
        fatal = ""
        try:
            try:
                uploader.connect()
            except Exception as exc:      # noqa: BLE001 - 连不上就整体失败
                fatal = f"FTP 连接失败：{type(exc).__name__}: {exc}"
                return
            for name in self.files:
                if self.stop_flag.is_set():
                    break
                self._set(name, status="uploading")
                res: UploadResult = uploader.upload(self.dir / name)
                if res.ok:
                    self._set(name, status="done", sent=res.bytes_sent,
                              resumed=res.resumed, attempts=res.attempts)
                else:
                    self._set(name, status="failed", error=res.error,
                              attempts=res.attempts)
            if self.stop_flag.is_set():
                self.status = "stopped"
            elif any(self._items[n]["status"] == "failed" for n in self.files):
                self.status = "failed"
                self.error = "部分文件上传失败（见逐文件错误）；本地文件未动，可直接重传"
            else:
                self.status = "done"
        except Exception as exc:          # noqa: BLE001 - 任何意外都落到状态里
            self.status = "failed"
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            uploader.close()
            self.ended = time.time()
            if fatal:
                self.status = "failed"
                self.error = fatal
                with self._lock:
                    for item in self._items.values():
                        if item["status"] in ("pending", "uploading"):
                            item["status"] = "stopped"
            if self.status == "stopped":
                with self._lock:
                    for item in self._items.values():
                        if item["status"] in ("pending", "uploading"):
                            item["status"] = "stopped"


class UploadManager:
    """进程级单例（app.py 持有）。同一时刻只允许一个上传在跑：两个上传
    抢同一条带宽只会互相拖慢，还可能把同名 .part 搅在一起。"""

    def __init__(self) -> None:
        self._jobs: dict[str, UploadJob] = {}
        self._lock = threading.Lock()

    def create(self, dir_path: str, files: list[str], *, host: str, port: int,
               user: str, password: str, remote_dir: str,
               passive: bool) -> UploadJob:
        base = Path(dir_path)
        if not base.is_dir():
            raise ValueError(f"目录不存在：{dir_path}")
        if not files:
            raise ValueError("没有勾选任何文件")
        if not host.strip():
            raise ValueError("FTP 主机不能为空")
        # 与回传页的产物清单一致；文件仍必须位于所选目录内。
        clean: list[str] = []
        for name in files:
            p = Path(name)
            if p.name != name or p.suffix.lower() not in (".parquet", ".jsonl"):
                raise ValueError(f"只支持目录内的 .parquet / .jsonl 文件，收到 {name!r}")
            if not (base / name).is_file():
                raise ValueError(f"文件不存在：{name}")
            clean.append(name)
        with self._lock:
            running = [j for j in self._jobs.values() if j.status in ("pending", "running")]
            if running:
                raise RuntimeError("已有回传在跑。等它结束或先停止。")
            job = UploadJob(uuid.uuid4().hex[:12], base, clean,
                            host=host.strip(), port=int(port or 21),
                            user=user, password=password,
                            remote_dir=remote_dir or "/", passive=bool(passive))
            self._jobs[job.id] = job
        thread = threading.Thread(target=job.run, name=f"upload-{job.id}",
                                  daemon=True)
        thread.start()
        return job

    def get(self, job_id: str) -> UploadJob | None:
        return self._jobs.get(job_id)

    def stop(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.status not in ("pending", "running"):
            return False
        job.stop_flag.set()
        return True


# app.py 持有的进程级单例。与任务簿的 MANAGER 同一模式。
UPLOADS = UploadManager()
