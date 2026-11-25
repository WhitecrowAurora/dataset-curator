"""分块回传的客户端：把本地文件按内容寻址的块推到汇总机上。

设计只认三件事，其余的都不需要：

1. **块哈希即身份**。8MB 一块（可调），sha256 命名。断在哪都能续——重问一次
   `/api/blob/have`，只补缺的。
2. **秒传**。同一批文件重跑、续做时算出来的块大多已存在，一个字节都不用传。
   这解决 FTP 路线里"重按一次就重传几百 MB"的问题。
3. **端到端校验**。拼好后服务端回整文件 sha256，跟本地算的对一遍。不一致就是
   真出错了，不是"大小一样就当成功"。

依赖只用标准库（urllib）——这条通道要在数据机上跑，不想为它引入 requests。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

DEFAULT_CHUNK = 8 << 20


class BlobClientError(Exception):
    """带上下文的上传失败（哪一步、哪个文件）。"""

    def __init__(self, message: str, *, code: int | None = None, report: dict | None = None):
        super().__init__(message)
        self.code = code
        self.report = report or {}

    @property
    def permanent(self) -> bool:
        return self.code is not None and 400 <= self.code < 500 and self.code not in (408, 429)

    @classmethod
    def from_http(cls, exc: urllib.error.HTTPError, method: str, path: str):
        detail, report = "", {}
        try:
            raw = exc.read(128 * 1024).decode("utf-8", "replace")
            try:
                body = json.loads(raw)
            except ValueError:
                body = {}
            if isinstance(body, dict):
                detail = str(body.get("error") or "")
                report = body.get("report") if isinstance(body.get("report"), dict) else {}
            detail = detail or raw[:300]
        except OSError:
            pass
        return cls(f"{method} {path} → HTTP {exc.code} {detail}", code=exc.code, report=report)


@dataclass
class UploadReport:
    """一次上传的结果，给页面/日志直接用。"""
    name: str
    ok: bool
    bytes: int = 0
    chunks: int = 0
    uploaded_chunks: int = 0        # 实际传了几块（其余是秒传命中）
    skipped_chunks: int = 0
    resumed: bool = False
    sha256: str = ""
    seconds: float = 0.0
    error: str = ""
    error_code: int | None = None
    error_report: dict = field(default_factory=dict)
    # 服务端在 commit 时回的分片校验报告（{ok, rows, problems, ...}）。
    # 空 dict = 对方是老版本 / 跳过了校验。
    verify: dict = field(default_factory=dict)

    def line(self) -> str:
        if not self.ok:
            return f"[回传] {self.name} 失败：{self.error}"
        saved = f"，秒传 {self.skipped_chunks} 块" if self.skipped_chunks else ""
        return (f"[回传] {self.name} 文件上传完成 {self.bytes / 1048576:.1f} MB"
                f"（{self.uploaded_chunks} 块{saved}，{self.seconds:.1f}s）")


@dataclass
class BlobClient:
    """指向一台汇总机的客户端。一个实例可以连续传多个文件，连接由 urllib 复用。"""
    base_url: str
    token: str = ""
    timeout: float = 60.0
    chunk_size: int = DEFAULT_CHUNK
    retries: int = 3
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    _opener: urllib.request.OpenerDirector = field(default=None, repr=False)
    deadline: float | None = None
    network: dict | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        from .center_runtime.network import opener
        self._opener = opener(self.base_url, self.network, redirects=self.network is None)

    # ---------------------------------------------------------------- HTTP

    def _headers(self) -> dict[str, str]:
        # 认证走 Bearer：与页面同源时可以不填，跨机必须填
        return {**self.headers, **({"Authorization": f"Bearer {self.token}"} if self.token else {})}

    def _request(self, method: str, path: str, *, body: bytes | None = None,
                 headers: dict[str, str] | None = None) -> dict:
        url = f"{self.base_url}{path}"
        hdrs = {**self._headers(), **(headers or {})}
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        timeout = self.timeout
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - time.monotonic())
            if timeout <= 0:
                raise BlobClientError('回传等待时间已到，本机产物保留')
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise BlobClientError.from_http(exc, method, path) from exc
        except urllib.error.URLError as exc:
            raise BlobClientError(f"{method} {path} 连不上：{exc.reason}") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise BlobClientError(f"{method} {path} 返回的不是 JSON") from exc

    def probe_auth(self) -> dict:
        """问服务端要不要令牌。返回 {required, token_hint, loopback, ...}。

        连不上/没这个口子（老版本服务端）时返回 required=False，让上层按
        "不需要鉴权"继续——比直接报错更接近实际（老服务端确实不校验）。
        """
        try:
            return self._request("GET", "/api/blob/auth")
        except BlobClientError:
            return {"required": False, "unknown": True}

    def _retry(self, fn: Callable[[], dict], what: str) -> dict:
        last: Exception | None = None
        for attempt in range(self.retries):
            if self.deadline is not None and time.monotonic() >= self.deadline:
                raise BlobClientError('回传等待时间已到，本机产物保留')
            try:
                return fn()
            except BlobClientError as exc:
                last = exc
                # 400/401/403/413 这类是参数或权限问题，重试没意义，直接抛
                if exc.permanent:
                    raise
                time.sleep(min(2 ** attempt, 10))
            except OSError as exc:
                last = exc
                time.sleep(min(2 ** attempt, 10))
        raise BlobClientError(f"{what} 重试 {self.retries} 次仍失败：{last}",
                              code=getattr(last, 'code', None), report=getattr(last, 'report', None))

    # ---------------------------------------------------------------- 分块

    def _chunks(self, path: Path) -> Iterable[tuple[str, bytes]]:
        """逐块 (sha256, 字节)。一次只驻留一块，8MB 以内。"""
        with path.open("rb") as fh:
            while True:
                buf = fh.read(self.chunk_size)
                if not buf:
                    return
                yield hashlib.sha256(buf).hexdigest(), buf

    def plan(self, path: str | Path) -> list[str]:
        """只算块名不传：用于"先把清单问一遍"或者预检。"""
        p = Path(path)
        return [sha for sha, _ in self._chunks(p)]

    def upload(self, path: str | Path, *, remote_name: str | None = None,
               on_progress: Callable[[int, int], None] | None = None,
               need: str = "") -> UploadReport:
        """传一个文件：算块 → 问缺 → 补缺 → commit → 校验整文件哈希。

        on_progress(已传块数, 总块数) 在每块之后调一次，给页面画进度。

        `need`：告诉服务端"这一片本该有哪几侧"（both/tag/nl），它在 commit
        时就按这个口径校验并回一份报告。留空 = 只查空行/坏行。服务器自己
        猜不出这片是被派去补 tag 还是补 nl，只能由上传方说明。
        """
        started = time.time()
        p = Path(path)
        name = remote_name or p.name
        if not p.is_file():
            return UploadReport(name, False, error=f"文件不存在：{p}")

        shas = self.plan(p)
        if not shas:
            return UploadReport(name, False, error="空文件不传")

        try:
            have = set()
            for start in range(0, len(shas), 64):
                batch = shas[start:start + 64]
                got = self._retry(lambda: self._request(
                    "GET", f"/api/blob/have?sha={','.join(batch)}"), "询问已有块")
                have.update(got.get("have") or [])
        except BlobClientError as exc:
            return UploadReport(name, False, error=str(exc), chunks=len(shas),
                                error_code=exc.code, error_report=exc.report)
        uploaded = 0
        total = 0
        i = 0
        for i, (sha, buf) in enumerate(self._chunks(p), 1):
            if i > len(shas) or sha != shas[i - 1]:
                return UploadReport(name, False, error="上传期间文件发生变化")
            total += len(buf)
            if sha not in have:
                try:
                    self._retry(lambda: self._request(
                        "POST", f"/api/blob/chunk?sha={sha}", body=buf,
                        headers={"Content-Type": "application/octet-stream"}), f"上传块 {sha[:12]}")
                except BlobClientError as exc:
                    return UploadReport(name, False, error=str(exc), chunks=len(shas),
                                        error_code=exc.code, error_report=exc.report,
                                        uploaded_chunks=uploaded,
                                        skipped_chunks=len(shas) - uploaded)
                uploaded += 1
            if on_progress is not None:
                on_progress(i, len(shas))

        if i != len(shas):
            return UploadReport(name, False, error="上传期间文件发生变化")
        try:
            res = self._retry(lambda: self._request(
                "POST", "/api/blob/commit",
                body=json.dumps({"name": name, "chunks": shas, "bytes": total,
                                 "need": (need or "").strip().lower()},
                                ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"}), "提交清单")
        except BlobClientError as exc:
            return UploadReport(name, False, error=str(exc), chunks=len(shas),
                                error_code=exc.code, error_report=exc.report,
                                uploaded_chunks=uploaded)

        remote_sha = str(res.get("sha256") or "")
        local_sha = hashlib.sha256()
        with p.open("rb") as fh:
            while True:
                buf = fh.read(1 << 20)
                if not buf:
                    break
                local_sha.update(buf)
        if remote_sha and remote_sha != local_sha.hexdigest():
            return UploadReport(name, False, error="服务端拼出的文件与本机不一致",
                                chunks=len(shas), uploaded_chunks=uploaded)
        verify = res.get("verify")
        return UploadReport(
            name=str(res.get("name") or name), ok=True, bytes=int(res.get("bytes") or total),
            chunks=len(shas), uploaded_chunks=uploaded,
            skipped_chunks=len(shas) - uploaded,
            resumed=uploaded < len(shas), sha256=remote_sha,
            seconds=time.time() - started,
            verify=verify if isinstance(verify, dict) else {})

    def upload_many(self, paths: Iterable[str | Path], *,
                    on_file: Callable[[int, int, UploadReport], None] | None = None
                    ) -> list[UploadReport]:
        out: list[UploadReport] = []
        paths = list(paths)
        for i, path in enumerate(paths, 1):
            rep = self.upload(path)
            out.append(rep)
            if on_file is not None:
                on_file(i, len(paths), rep)
        return out


def upload_dir(url: str, out_dir: str | Path, *, token: str = "",
               suffixes: tuple[str, ...] = (".parquet", ".jsonl", ".tar", ".zip"),
               verbose: bool = True) -> list[UploadReport]:
    """把一个目录里的产物按后缀传上去。CLI / 自动回传都走这里。"""
    d = Path(out_dir)
    pending = sorted(p for s in suffixes for p in d.glob(f"*{s}"))
    client = BlobClient(url, token=token)
    reports: list[UploadReport] = []
    for p in pending:
        rep = client.upload(p)
        reports.append(rep)
        if verbose:
            print(rep.line(), flush=True)
    return reports


def default_root() -> Path:
    return Path(os.environ.get("TAGGER_BLOB_ROOT") or (Path.home() / ".tagger" / "blobstore"))
