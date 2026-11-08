"""Web 端 HF 分片下载管理器。

和打标任务**解耦**：这里只负责"把仓库里的分片下到本地"，不排队等 GPU。
下好的分片落在 `run/hf-cache/<org--name>/`，打标任务用同一个目录
（`hf_source.cache_dir_for`），所以两边天然接上：

    下载页提前下好 → 任务跑起来直接命中（不再下载）→ 处理完立刻删 → 空间释放

三条设计约束
------------
1. **token 绝不落盘**：只放内存，进程重启就得重填。写进 state.json 等于
   把私有仓库凭证留在磁盘上。
2. **单下载线程**：带宽是瓶颈，并发下载只是互相抢，还会让"剩余空间"算不准。
3. **空间不满足就排队**：不是"下到一半写失败"，而是在 `queued` 里等，
   并在 message 里写清楚缺多少——用户据此清理或换盘。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from .. import hf_source

_TG = 1073741824


def _gb(n: float) -> str:
    return f"{n / _TG:.1f} GB"


def global_proxy() -> str:
    """这台机器上网用的代理。空 = 直连。

    单独一个函数而不是在 __init__ 里读一次：用户可能不改配置就换了代理，
    页面上的"保存"必须立刻反映到下一次快照里。
    """
    try:
        from .models import proxy_url

        return str(proxy_url() or "")
    except Exception:                    # noqa: BLE001 - 读不到就按直连
        return ""


class HfDownloads:
    """分片下载队列。进程内单例（由 app.py 懒加载）。"""

    def __init__(self, cache_root: str | Path | None = None, *,
                 state_path: str | Path | None = None,
                 reserve_bytes: int = 2 << 30) -> None:
        self.cache_root = Path(cache_root or hf_source.HF_CACHE_ROOT)
        self._state = Path(state_path) if state_path else None
        self._reserve_bytes = max(0, int(reserve_bytes))
        self._lock = threading.RLock()
        self._items: list[dict] = []
        # token 只在内存：私有仓库凭证不落盘
        self._tokens: dict[str, str] = {}
        self._marks: dict[str, deque] = {}
        self._stop = threading.Event()
        self._load()
        # 上次进程退出时还在跑的那条：重新排队。.part 还在，会从断点续上。
        for item in self._items:
            if item.get("status") in ("running", "cancelling"):
                item["status"] = "queued"
                item["done"] = 0
                item["message"] = "上次没下完，已重新排队（会从断点续传）"
        self._thread = threading.Thread(target=self._loop, name="hf-download",
                                        daemon=True)
        self._thread.start()

    # ------------------------------------------------------------ 对外

    def submit(self, repo: str, files: list[str], *,
               sizes: dict[str, int] | None = None, token: str = "",
               revision: str = "main", conns: int = 4,
               proxy: str = "") -> list[dict]:
        repo = (repo or "").strip()
        if not repo:
            raise ValueError("缺少仓库名（org/name）")
        if not files:
            raise ValueError("没有选中任何分片")
        sizes = sizes or {}
        created: list[dict] = []
        with self._lock:
            busy = {(i["repo"], i["filename"]) for i in self._items
                    if i.get("status") in ("queued", "running", "cancelling")}
            for name in files:
                name = str(name or "").strip()
                if not name or (repo, name) in busy:
                    continue
                item = {
                    "id": uuid.uuid4().hex[:12],
                    "repo": repo,
                    "filename": name,
                    "revision": revision or "main",
                    "conns": max(1, min(16, int(conns or 1))),
                    # 每项带一份快照：改全局代理不该影响已在排队的任务
                    "proxy": str(proxy or ""),
                    "size": int(sizes.get(name) or 0),
                    "done": 0,
                    "status": "queued",
                    "message": "",
                    "created": time.time(),
                    "started": 0.0,
                    "finished": 0.0,
                    "speed": 0.0,
                }
                self._items.append(item)
                if token:
                    self._tokens[item["id"]] = token
                created.append(dict(item))
            self._save()
        return created

    def snapshot(self) -> dict:
        with self._lock:
            items = [dict(i) for i in self._items]
        return {
            "items": items,
            "cache_root": str(self.cache_root),
            "free_bytes": hf_source.free_bytes(self.cache_root),
            "reserve_bytes": self._reserve_bytes,
            "running": sum(1 for i in items if i["status"] == "running"),
            "queued": sum(1 for i in items if i["status"] == "queued"),
            # 全局代理：下载页要在输入框里回填它。与打标任务的 hf_proxy、
            # 模型下载共用同一份（models.proxy_url），三处不一致最容易让人
            # 以为"代理坏了"。
            "proxy": global_proxy(),
        }

    def cancel(self, item_id: str) -> bool:
        with self._lock:
            for item in self._items:
                if item["id"] != item_id:
                    continue
                if item["status"] == "queued":
                    item["status"] = "cancelled"
                    item["message"] = "已取消（没有开始下载）"
                elif item["status"] == "running":
                    # 置 cancelling，由 download_shard 的 should_stop 感知；
                    # 直接改 cancelled 的话 .part 会写脏。
                    item["status"] = "cancelling"
                self._save()
                return True
        return False

    def cancel_all(self) -> None:
        with self._lock:
            for item in self._items:
                if item["status"] == "queued":
                    item["status"] = "cancelled"
                    item["message"] = "已取消（没有开始下载）"
                elif item["status"] == "running":
                    item["status"] = "cancelling"
            self._save()

    def clear_finished(self) -> int:
        """清掉已结束的条目（不删磁盘上的文件）。"""
        with self._lock:
            keep = [i for i in self._items
                    if i.get("status") in ("queued", "running", "cancelling")]
            removed = len(self._items) - len(keep)
            self._items = keep
            self._save()
        return removed

    def set_reserve(self, reserve_bytes: int) -> None:
        """改"给产物留的余量"。下载页上直接可调，不必重启服务。"""
        with self._lock:
            self._reserve_bytes = max(0, int(reserve_bytes))
            self._save()

    def packs(self) -> list[dict]:
        """缓存根下已有的"离线包"（每个仓库一个目录）。

        给打标页面用：下载页把分片备好之后，用户不该再去填一遍 HF 仓库
        （那会重新走网络），而是直接从"已下载的离线包"里挑一个。

        目录名是 `org--name`（见 hf_source.cache_dir_for），这里反解回来；
        解不出来的目录（用户手工放的、早期版本留下的）也给出来，用目录名当
        repo 标识——**能选上比"名字好看"重要**。
        """
        root = self.cache_root
        out: list[dict] = []
        if not root.is_dir():
            return out
        for sub in sorted(root.iterdir()):
            if not sub.is_dir():
                continue
            files: list[dict] = []
            total = 0
            for path in sorted(sub.rglob("*")):
                if not path.is_file() or path.name.endswith(".part"):
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                files.append({"filename": path.relative_to(sub).as_posix(),
                              "bytes": size})
                total += size
            if not files:
                continue
            out.append({
                "repo": sub.name.replace("--", "/"),
                "dir": str(sub),
                "files": files,
                "count": len(files),
                "bytes": total,
            })
        return out

    def cached(self, repo: str) -> list[dict]:
        """这个仓库在本地已经下好的分片（不含 .part 半截文件）。"""
        base = hf_source.cache_dir_for(repo, self.cache_root)
        out: list[dict] = []
        if base.is_dir():
            for path in base.rglob("*"):
                if path.is_file() and not path.name.endswith(".part"):
                    try:
                        out.append({"filename": path.relative_to(base).as_posix(),
                                    "bytes": path.stat().st_size})
                    except OSError:
                        continue
        return sorted(out, key=lambda kv: kv["filename"])

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------ 调度

    def _loop(self) -> None:
        while not self._stop.is_set():
            item = self._pick()
            if item is None:
                self._stop.wait(0.8)
                continue
            self._run(item)

    def _pick(self) -> dict | None:
        with self._lock:
            for item in self._items:
                if item["status"] != "queued":
                    continue
                ok, why = self._fits(item)
                if ok:
                    return item
                if item.get("message") != why:
                    item["message"] = why
                    self._save()
            return None

    def _fits(self, item: dict) -> tuple[bool, str]:
        size = int(item.get("size") or 0)
        if size <= 0:
            # 大小未知（tree 没给 size）只能下完再看；这时不拦，避免永远排不上
            return True, ""
        base = hf_source.cache_dir_for(item["repo"], self.cache_root)
        free = hf_source.free_bytes(base)
        need = int(size * 1.05)
        if free < need + self._reserve_bytes:
            return False, (f"空间不够：剩 {_gb(free)}，这片要 {_gb(need)}"
                           f" + 余量 {_gb(self._reserve_bytes)}")
        return True, ""

    def _run(self, item: dict) -> None:
        item_id = item["id"]
        with self._lock:
            item["status"] = "running"
            item["started"] = time.time()
            item["message"] = ""
            item["speed"] = 0.0
            self._marks[item_id] = deque(maxlen=20)

        def on_progress(done: int, total: int) -> None:
            now = time.time()
            with self._lock:
                item["done"] = int(done)
                if total:
                    item["size"] = int(total)
                marks = self._marks.setdefault(item_id, deque(maxlen=20))
                marks.append((now, float(done)))
                if len(marks) >= 2:
                    t0, d0 = marks[0]
                    span = now - t0
                    if span > 0.4:
                        item["speed"] = max(0.0, (done - d0) / span)

        def should_stop() -> bool:
            with self._lock:
                return item.get("status") == "cancelling"

        token = self._tokens.get(item_id) or ""
        dest = hf_source.cache_dir_for(item["repo"], self.cache_root)
        try:
            shard = hf_source.download_shard(
                item["repo"], item["filename"], dest,
                revision=item.get("revision") or "main",
                token=token or None, verbose=False,
                on_progress=on_progress, should_stop=should_stop,
                conns=int(item.get("conns") or 1),
                proxy=str(item.get("proxy") or ""))
            with self._lock:
                item["status"] = "done"
                item["size"] = item["done"] = int(shard.bytes)
                item["speed"] = 0.0
                item["finished"] = time.time()
        except hf_source.HFCancelled:
            with self._lock:
                item["status"] = "cancelled"
                item["message"] = "已暂停；再点下载会从断点续传"
                item["speed"] = 0.0
        except Exception as exc:            # noqa: BLE001 - 单条失败不该拖垮队列
            with self._lock:
                item["status"] = "error"
                item["message"] = f"{type(exc).__name__}: {exc}"[:300]
                item["speed"] = 0.0
        finally:
            self._tokens.pop(item_id, None)
            with self._lock:
                self._marks.pop(item_id, None)
                self._save()

    # ------------------------------------------------------------ 持久化

    def _load(self) -> None:
        if self._state is None or not self._state.is_file():
            return
        try:
            data = json.loads(self._state.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        items = data.get("items") if isinstance(data, dict) else None
        if isinstance(items, list):
            self._items = [i for i in items if isinstance(i, dict) and i.get("id")]
        try:
            self._reserve_bytes = int(data.get("reserve_bytes") or self._reserve_bytes)
        except (TypeError, ValueError):
            pass

    def _save(self) -> None:
        if self._state is None:
            return
        try:
            self._state.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state.with_name(self._state.name + ".tmp")
            tmp.write_text(json.dumps({
                "reserve_bytes": self._reserve_bytes,
                "items": self._items,
            }, ensure_ascii=False, indent=0), encoding="utf-8")
            os.replace(tmp, self._state)
        except OSError:
            # 状态写不下去只是"重启后列表丢了"，不该影响正在下的任务
            pass


# app.py 持有的进程级单例。与 MANAGER / DEDUPS / UPLOADS 同一模式。
# 落一份 state 到 run/ 下：重启后队列里的条目还在（running 的会被重排成
# queued 并靠 .part 续传），否则用户看不到"上次下到哪了"。
DOWNLOADS = HfDownloads(state_path=Path("run") / "hf-downloads.json")
