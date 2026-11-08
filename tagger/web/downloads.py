"""模型权重下载：进度、网速、断点续传、取消、删除、打开目录。

为什么自己流式下，而不是直接调 `snapshot_download`：

1. 页面要进度条和网速，HF 的接口只吐 tqdm，抓不到；
2. 8B 一份 ~17G，网一抖就得从头来——按文件断点续传是必须的，不是优化；
3. 要能说清"下到哪个目录"（默认 `models/<key>`），并且**只允许删这个目录树
   里面的东西**，否则"删除模型"迟早变成删用户素材。

线程模型：一个下载一个后台线程，状态挂在内存里。进程重启后进度就没了，
但已落盘的文件还在——下次点下载会按文件大小续传，不会白下。
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
MODELS_ROOT = ROOT / "models"

_KINDS = {"vlm", "tag"}
_CHUNK = 512 * 1024


def default_dir(kind: str, key: str) -> Path:
    """默认落盘位置。tag 分类头跟现有习惯一致（models/<backend>），NL 放子目录。"""
    return MODELS_ROOT / (key if kind == "tag" else f"nl/{key}")


def resolve_dir(kind: str, key: str, raw: str | None) -> Path:
    """表单里的下载目录。相对路径按仓库根算，空则用默认值。"""
    text = (raw or "").strip()
    if not text:
        return default_dir(kind, key)
    p = Path(text)
    return p if p.is_absolute() else (ROOT / p)


_PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")


def socks_ready() -> bool:
    """httpx 认 socks5:// 的前提是装了 socksio；没装就别让用户点完才知道。"""
    import importlib.util

    return importlib.util.find_spec("socksio") is not None


def normalize_proxy(url: str) -> str:
    """校验并补全代理地址。空串 = 直连。

    允许只写 `127.0.0.1:7890`（按 http 处理）——这是从浏览器/系统设置里抄地址
    时最常见的形态，要求补全协议只会让人猜。
    """
    text = (url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"http://{text}"
    scheme = text.split("://", 1)[0].lower()
    if scheme not in _PROXY_SCHEMES:
        raise ValueError(
            f"不支持的代理协议：{scheme}（支持 http / https / socks5 / socks5h）")
    if scheme.startswith("socks") and not socks_ready():
        raise ValueError("socks5 代理需要 socksio，请先 `pip install socksio`")
    return text


def _client(proxy: str = "", *, read_timeout: float = 300.0):
    import httpx

    kwargs: dict[str, Any] = {
        "follow_redirects": True,
        "timeout": httpx.Timeout(30.0, read=read_timeout),
    }
    if proxy:
        kwargs["proxy"] = proxy
    try:
        return httpx.Client(**kwargs)
    except ImportError as exc:            # socks5 但没装 socksio
        raise RuntimeError("socks5 代理需要 socksio，请先 pip install socksio") from exc


def test_proxy(url: str) -> dict[str, Any]:
    """拿 HF 的模型列表接口探一次，用来看"代理通不通、延迟多少"。

    不抛异常：探代理失败是正常结果，页面要显示原因而不是报错弹窗。
    """
    try:
        proxy = normalize_proxy(url)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "via": "", "latency_ms": 0}
    base = _endpoint()
    started = time.time()
    try:
        with _client(proxy, read_timeout=10.0) as client:
            resp = client.get(f"{base}/api/models?limit=1", headers=_headers(),
                              timeout=10.0)
            resp.raise_for_status()
        return {
            "ok": True,
            "via": proxy or "直连",
            "endpoint": base,
            "latency_ms": int((time.time() - started) * 1000),
        }
    except Exception as exc:              # noqa: BLE001 - 失败原因原样给页面
        return {
            "ok": False,
            "via": proxy or "直连",
            "endpoint": base,
            "latency_ms": int((time.time() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
        }


@dataclass
class Task:
    kind: str
    key: str
    repo: str
    dest: Path
    proxy: str = ""                  # 空 = 直连；每任务带一份，改设置不影响在跑的
    # 只下这些文件（glob）。空 = 整仓库。
    # GGUF 仓库一个名字底下挂着 6 个量化档（20G+），全量下载纯属浪费；
    # 具体要哪几份由 tiers.yaml 的 download_files 声明，不在代码里写死。
    allow: tuple[str, ...] = ()
    status: str = "queued"           # queued / running / done / error / canceled
    message: str = ""
    current: str = ""                # 正在下的文件名
    files_total: int = 0
    files_done: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    speed: float = 0.0               # 字节/秒，每 0.5s 采一次
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    cancel: bool = False
    _sample_bytes: int = 0
    _sample_at: float = field(default_factory=time.time)

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.key}"

    def tick(self, done: int) -> None:
        """更新已下字节并按 0.5s 窗口估速。

        窗口估速而不是"总量/总时间"：全程均值在断点续传和限速抖动下会明显
        骗人，用户盯的是"现在多快"。
        """
        self.bytes_done = done
        now = time.time()
        dt = now - self._sample_at
        if dt >= 0.5:
            self.speed = (done - self._sample_bytes) / dt
            self._sample_bytes = done
            self._sample_at = now

    def state(self) -> dict[str, Any]:
        elapsed = (self.finished_at or time.time()) - self.started_at
        pct = (self.bytes_done / self.bytes_total * 100.0) if self.bytes_total else 0.0
        return {
            "kind": self.kind,
            "key": self.key,
            "repo": self.repo,
            "dest": str(self.dest),
            "proxy": self.proxy,
            "status": self.status,
            "message": self.message,
            "current": self.current,
            "files_total": self.files_total,
            "files_done": self.files_done,
            "bytes_total": self.bytes_total,
            "bytes_done": self.bytes_done,
            "percent": round(min(pct, 100.0), 1),
            "speed": round(self.speed, 1),
            "elapsed": round(elapsed, 1),
            "eta": (round((self.bytes_total - self.bytes_done) / self.speed, 1)
                    if self.speed > 0 and self.status == "running" else None),
        }


class DownloadManager:
    def __init__(self, models_root: Path | None = None) -> None:
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()
        self.models_root = (models_root or MODELS_ROOT)

    # ---- 查询

    def get(self, kind: str, key: str) -> Task | None:
        with self._lock:
            return self._tasks.get(f"{kind}:{key}")

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {tid: t.state() for tid, t in self._tasks.items()}

    def state_of(self, kind: str, key: str) -> dict[str, Any] | None:
        t = self.get(kind, key)
        return t.state() if t else None

    def active(self) -> bool:
        with self._lock:
            return any(t.status in ("queued", "running") for t in self._tasks.values())

    # ---- 动作

    def start(self, kind: str, key: str, repo: str, dest: Path,
              proxy: str = "", allow: list[str] | tuple[str, ...] | None = None,
              ) -> dict[str, Any]:
        if kind not in _KINDS:
            raise ValueError(f"未知的模型类型：{kind}")
        repo = (repo or "").strip()
        if not repo:
            raise ValueError(
                "这条没有可下载的仓库名：把「权重」填成 HF 仓库（org/name）再点下载")
        tid = f"{kind}:{key}"
        # 空字符串/空白项过滤掉：页面传来的空行不该变成"匹配所有文件"。
        globs = tuple(p.strip() for p in (allow or ()) if p and p.strip())
        with self._lock:
            exist = self._tasks.get(tid)
            if exist and exist.status in ("queued", "running"):
                return exist.state()
            task = Task(kind=kind, key=key, repo=repo, dest=Path(dest),
                        proxy=normalize_proxy(proxy), allow=globs)
            self._tasks[tid] = task
        threading.Thread(target=self._worker, args=(task,), daemon=True,
                         name=f"dl-{tid}").start()
        return task.state()

    def cancel(self, kind: str, key: str) -> dict[str, Any] | None:
        t = self.get(kind, key)
        if t is None:
            return None
        t.cancel = True
        if t.status in ("queued", "running"):
            t.status = "canceled"
            t.message = "已取消"
            t.finished_at = time.time()
        return t.state()

    def forget(self, kind: str, key: str) -> None:
        """删掉任务记录（模型文件删干净后再清，页面就不再显示进度）。"""
        with self._lock:
            self._tasks.pop(f"{kind}:{key}", None)

    # ---- 删文件 / 开目录

    def delete_dir(self, dest: Path) -> None:
        """删除下载目录。**只有 models/ 之内的路径才允许删**——这条不是洁癖，
        是防止有人把 local_dir 填成素材目录，一点删除就没了。"""
        target = self._guard(dest)
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()

    def reveal_dir(self, dest: Path) -> str:
        target = self._guard(dest)
        if not target.exists():
            raise ValueError("目录还不存在，先下载或手动建一个")
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", str(target)])       # noqa: S603,S607
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])            # noqa: S603,S607
        else:
            subprocess.Popen(["xdg-open", str(target)])        # noqa: S603,S607
        return str(target)

    def _guard(self, dest: Path) -> Path:
        root = self.models_root.resolve()
        p = Path(dest)
        if not p.is_absolute():
            p = ROOT / p
        p = p.resolve()
        if p == root or root not in p.parents:
            raise ValueError(f"拒绝操作：{p} 不在模型目录 {root} 之内")
        return p

    # ---- 干活

    def _worker(self, task: Task) -> None:
        try:
            task.status = "running"
            files = _list_files(task.repo, task.proxy)
            if task.allow:
                files = _filter_files(files, task.allow, task.repo)
            task.files_total = len(files)
            task.bytes_total = sum(size for _, size in files)
            done = 0
            task.dest.mkdir(parents=True, exist_ok=True)
            for name, size in files:
                if task.cancel:
                    break
                if not size:
                    size = 0
                task.current = name
                done = _fetch(task, name, size, done)
                task.files_done += 1
            if task.cancel:
                task.status = "canceled"
                task.message = "已取消（已下的文件留着，下次续传）"
            else:
                task.status = "done"
                task.message = f"已下到 {task.dest}"
                task.speed = 0.0
        except Exception as exc:                    # noqa: BLE001 - 尽力报告给页面
            task.status = "error"
            task.speed = 0.0
            task.message = _explain(exc, task.proxy)
        finally:
            task.finished_at = time.time()


def _explain(exc: Exception, proxy: str = "") -> str:
    """把异常翻成"下一步该干什么"。

    `ConnectError: [WinError 10061]` 这种原文用户看不懂，也搜不到解法；
    而这个页面上真正能做的动作只有两个（填代理 / 换镜像源），直接说出来。
    不认识的异常保持原样——瞎翻译会掩盖真正的原因。
    """
    name = type(exc).__name__
    note = (f"当前走代理 {proxy}" if proxy
            else "当前是直连：设置里的「下载代理」是空的")
    if any(k in name for k in ("Connect", "Timeout", "ReadTimeout", "Proxy")):
        return (f"连不上 {_endpoint()}（{name}）。{note}。\n"
                f"① 在「下载代理」里填本机代理，如 http://127.0.0.1:7890；\n"
                f"② 或设环境变量 HF_ENDPOINT=https://hf-mirror.com 走镜像后重启服务。")
    return f"{name}: {exc}"


def _endpoint() -> str:
    """HF 直链前缀。国内镜像走 HF_ENDPOINT，与 huggingface_hub 同一套环境变量。"""
    return (os.environ.get("HF_ENDPOINT") or "https://huggingface.co").rstrip("/")


def _token() -> str | None:
    for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACEHUB_API_TOKEN"):
        value = os.environ.get(name)
        if value:
            return value.strip()
    try:
        from huggingface_hub import get_token

        return get_token()
    except Exception:                              # noqa: BLE001
        return None


def _headers() -> dict[str, str]:
    token = _token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _next_link(header: str | None) -> str | None:
    """HF 的 tree 接口用 Link 头翻页；文件多的大仓库一页装不下。"""
    if not header:
        return None
    for part in header.split(","):
        seg = part.split(";")
        if len(seg) >= 2 and 'rel="next"' in seg[1]:
            return seg[0].strip().strip("<>")
    return None


def _filter_files(files: list[tuple[str, int]], patterns: tuple[str, ...],
                  repo: str) -> list[tuple[str, int]]:
    """按 glob 只留下要下的那几份。

    GGUF 仓库一个名字底下挂着 6 个量化档（合计 20G+），全量下载纯属浪费。
    匹配不到时必须报错并把仓库里实际有什么列出来——静默下 0 个文件更糟：
    页面会显示"已下到 ..."，人以为能跑了，起服务才发现权重不在。
    """
    picked = [(name, size) for name, size in files
              if any(fnmatch.fnmatch(name, p) for p in patterns)]
    if picked:
        return picked
    available = [name for name, _ in files if name.lower().endswith(".gguf")]
    shown = "、".join(available[:12]) or "（仓库里没有 gguf 文件）"
    raise ValueError(
        f"仓库 {repo} 里没有匹配 {' / '.join(patterns)} 的文件。实际有：{shown}")


def _list_files(repo: str, proxy: str = "") -> list[tuple[str, int]]:
    """仓库文件清单 + 字节数。

    走 HF 的 tree 接口而不是 `HfApi().model_info()`：后者没法单独指定代理
    （它会走自己的全局会话），而这个项目在中国大陆基本都得挂代理。顺带
    省掉一层 huggingface_hub 的元数据缓存，网络本来就通的情况下更直观。
    """
    url = f"{_endpoint()}/api/models/{repo}/tree/main?recursive=1"
    out: list[tuple[str, int]] = []
    with _client(proxy) as client:
        while url:
            resp = client.get(url, headers=_headers())
            if resp.status_code in (401, 403):
                raise RuntimeError(f"没有读权限：{repo}（私有/门禁仓库需要 HF_TOKEN）")
            if resp.status_code == 404:
                raise RuntimeError(f"仓库不存在：{repo}")
            resp.raise_for_status()
            for item in resp.json() or []:
                if not isinstance(item, dict) or item.get("type") != "file":
                    continue
                name = str(item.get("path") or "")
                if not name:
                    continue
                size = int(item.get("size")
                           or (item.get("lfs") or {}).get("size") or 0)
                out.append((name, size))
            url = _next_link(resp.headers.get("link"))
    if not out:
        raise ValueError(f"仓库 {repo} 里没有文件（或没有读权限，需要 HF_TOKEN）")
    return out


def _fetch(task: Task, name: str, size: int, done: int) -> int:
    """下单个文件，返回累计字节数。已有部分文件时按 Range 续传。"""
    dest = task.dest / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    have = dest.stat().st_size if dest.is_file() else 0
    if size and have >= size:
        task.tick(done + have)
        return done + have

    url = f"{_endpoint()}/{task.repo}/resolve/main/{quote(name)}"
    headers = _headers()
    if have and size:
        headers["Range"] = f"bytes={have}-"

    base = done + (have if size else 0)
    with _client(task.proxy, read_timeout=120.0) as client:
        with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code not in (200, 206):
                raise RuntimeError(f"{name} 下载失败：HTTP {resp.status_code}")
            append = have > 0 and resp.status_code == 206
            if not append:
                have = 0
                base = done
            task.tick(base)
            mode = "ab" if append else "wb"
            with dest.open(mode) as fh:
                for chunk in resp.iter_bytes(_CHUNK):
                    if task.cancel:
                        return base
                    fh.write(chunk)
                    base += len(chunk)
                    task.tick(base)
    return base


DOWNLOADS = DownloadManager()
