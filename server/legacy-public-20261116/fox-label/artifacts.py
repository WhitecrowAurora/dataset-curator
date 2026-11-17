"""Bounded, opaque archive receipts for fox-label (no annotation parsing).

This index and its blobs are deliberately separate from the dispatch database.
All mutation paths use a process + OS lock.  A caller-provided short binding
transaction protects publication; the network receive never holds that lock.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import hmac
import math
import os
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
from pathlib import Path
from typing import BinaryIO, Callable

DEFAULT_MAX_BYTES = 128 * 1024 * 1024
DEFAULT_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
DEFAULT_MIN_FREE_BYTES = 64 * 1024 * 1024
DEFAULT_TOKEN_TTL = 3600
DEFAULT_UPLOAD_TIMEOUT = 600
CHUNK_BYTES = 1024 * 1024
UPLOAD_ID_RE = re.compile(r"A[0-9a-f]{24}\Z")
FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
META_FIELDS = ("repo", "pkg", "lease_id", "user_id", "device_id",
               "filename", "size", "sha256")

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
 upload_id TEXT PRIMARY KEY,
 repo TEXT NOT NULL, pkg TEXT NOT NULL, lease_id TEXT NOT NULL,
 user_id TEXT NOT NULL, device_id TEXT NOT NULL,
 filename TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
 token_hash TEXT NOT NULL UNIQUE, expires_at REAL NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', bytes_received INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, authorized_at REAL NOT NULL,
 received_at REAL, error TEXT NOT NULL DEFAULT '', blob_name TEXT,
 UNIQUE(repo,pkg)
);
CREATE TABLE IF NOT EXISTS upload_events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, upload_id TEXT NOT NULL,
 ts REAL NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL,
 detail TEXT NOT NULL DEFAULT ''
);
"""


class ArtifactError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def body(self) -> dict:
        return {"ok": False, "code": self.code, "error": self.message}


def _text(value, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactError(400, "bad_metadata", f"缺少或非法的 {label}")
    value = value.strip()
    if len(value) > limit or any(ord(x) < 32 or ord(x) == 127 for x in value):
        raise ArtifactError(400, "bad_metadata", f"非法的 {label}")
    return value


def normalize_metadata(target: dict, params: dict,
                       max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    if not isinstance(target, dict) or not isinstance(params, dict):
        raise ArtifactError(400, "bad_metadata", "目标与参数必须是对象")
    repo = _text(target.get("repo"), "repo", 512)
    pkg = _text(target.get("id"), "id", 512)
    if "/" in pkg or "\\" in pkg or pkg in (".", ".."):
        raise ArtifactError(400, "bad_metadata", "id 必须是裸分片名")
    filename = _text(params.get("filename"), "filename", 128)
    if (not FILENAME_RE.fullmatch(filename) or ".." in filename
            or not filename.lower().endswith((".tar", ".tar.gz", ".tgz", ".zip"))):
        raise ArtifactError(400, "bad_filename", "归档名须为安全的 ASCII 裸文件名（tar/tar.gz/tgz/zip）")
    size = params.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ArtifactError(400, "bad_size", "size 必须是正整数")
    if size > max_bytes:
        raise ArtifactError(413, "artifact_too_large", "归档超过单文件接收上限")
    sha = _text(params.get("sha256"), "sha256", 64).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ArtifactError(400, "bad_sha256", "sha256 必须是 64 位十六进制")
    return {
        "repo": repo, "pkg": pkg,
        "lease_id": _text(target.get("lease_id"), "lease_id", 64),
        "user_id": _text(params.get("user_id"), "user_id", 64),
        "device_id": _text(params.get("device_id"), "device_id", 64),
        "filename": filename, "size": size, "sha256": sha,
    }


def metadata_of(row: dict) -> dict:
    return {k: row[k] for k in META_FIELDS}


class _DownloadFile:
    """Hold a bounded reader slot until the streamed file closes."""
    def __init__(self, fh: BinaryIO, slots: threading.BoundedSemaphore):
        self.fh, self.slots = fh, slots

    def read(self, size: int = -1):
        return self.fh.read(size)

    def close(self):
        fh, self.fh = self.fh, None
        if fh is not None:
            try:
                fh.close()
            finally:
                self.slots.release()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        self.close()


class ArtifactStore:
    def __init__(self, root: Path | str, *, max_bytes: int = DEFAULT_MAX_BYTES,
                 max_total_bytes: int = DEFAULT_TOTAL_BYTES,
                 min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
                 token_ttl: int = DEFAULT_TOKEN_TTL,
                 upload_timeout: float = DEFAULT_UPLOAD_TIMEOUT):
        values = (max_bytes, max_total_bytes, min_free_bytes, token_ttl)
        if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in values):
            raise ValueError("artifact limits must be non-negative integers")
        if (max_bytes <= 0 or max_bytes > 1024 * 1024 * 1024
                or max_total_bytes < max_bytes or max_total_bytes > 1024 ** 4
                or token_ttl <= 0 or token_ttl > 86400
                or isinstance(upload_timeout, bool) or not isinstance(upload_timeout, (int, float))
                or not math.isfinite(upload_timeout) or not 0 < upload_timeout <= 3600):
            raise ValueError("invalid artifact limits")
        supplied = Path(root).expanduser().absolute()
        if supplied.is_symlink():
            raise ValueError("artifact root must not be a symlink")
        self.root = supplied.resolve()
        self.max_bytes, self.max_total_bytes = max_bytes, max_total_bytes
        self.min_free_bytes, self.token_ttl = min_free_bytes, token_ttl
        self.upload_timeout = float(upload_timeout)
        self._thread_lock = threading.Lock()
        self._read_slots = threading.BoundedSemaphore(2)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.incoming, self.objects = self.root / ".incoming", self.root / "objects"
        for directory in (self.incoming, self.objects):
            if directory.is_symlink():
                raise ValueError("artifact subdirectory must not be a symlink")
            directory.mkdir(mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
        self.index_path = self.root / "index.sqlite3"
        fd = os.open(self.index_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("artifact index must be a regular file")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        c = self._connect()
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=FULL")
            c.executescript(SCHEMA)
        finally:
            c.close()

    def _connect(self, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            c = sqlite3.connect(self.index_path.as_uri() + "?mode=ro", uri=True,
                                timeout=1, isolation_level=None)
            c.execute("PRAGMA query_only=ON")
        else:
            c = sqlite3.connect(self.index_path, timeout=1, isolation_level=None)
            c.execute("PRAGMA synchronous=FULL")
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=1000")
        return c

    @contextlib.contextmanager
    def _mutation(self):
        if not self._thread_lock.acquire(blocking=False):
            raise ArtifactError(503, "upload_busy", "产物写通道忙，请稍后重试")
        fd = None
        try:
            fd = os.open(self.root / "upload.lock",
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ArtifactError(503, "storage_unavailable", "产物锁不可用")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                raise ArtifactError(503, "upload_busy", "产物写通道忙，请稍后重试") from e
            yield
        finally:
            if fd is not None:
                os.close(fd)
            self._thread_lock.release()

    def _row(self, upload_id: str) -> dict | None:
        c = self._connect(readonly=True)
        try:
            row = c.execute("SELECT * FROM uploads WHERE upload_id=?", (upload_id,)).fetchone()
            return dict(row) if row else None
        finally:
            c.close()

    @staticmethod
    def _event(c, upload_id: str, kind: str, actor: str, detail: str = "") -> None:
        c.execute("INSERT INTO upload_events(upload_id,ts,kind,actor,detail) VALUES(?,?,?,?,?)",
                  (upload_id, time.time(), kind, actor, detail[:200]))

    def _object_path(self, upload_id: str) -> Path:
        if not isinstance(upload_id, str) or not UPLOAD_ID_RE.fullmatch(upload_id):
            raise ArtifactError(404, "artifact_not_found", "产物不存在")
        return self.objects / (upload_id + ".blob")

    def view(self, row: dict) -> dict:
        out = {k: row[k] for k in (
            "upload_id", "repo", "lease_id", "user_id", "device_id", "filename",
            "size", "sha256", "status", "bytes_received", "created_at",
            "authorized_at", "expires_at", "received_at", "error")}
        out["package"] = row["pkg"]
        out["checksum_verified"] = row["status"] == "received"
        out["annotation_validation"] = "not_performed"
        available = False
        if row["status"] == "received":
            path = self._object_path(row["upload_id"])
            try:
                s = path.lstat()
                available = stat.S_ISREG(s.st_mode) and s.st_size == row["size"]
            except OSError:
                pass
        out["storage_available"] = available
        out["download_path"] = (f"/admin/v1/artifacts/{row['upload_id']}/download"
                                if available else None)
        return out

    def list_for_package(self, repo: str, pkg: str) -> dict:
        repo, pkg = _text(repo, "repo", 512), _text(pkg, "package", 512)
        c = self._connect(readonly=True)
        try:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM uploads WHERE repo=? AND pkg=? ORDER BY created_at", (repo, pkg))]
        finally:
            c.close()
        return {"ok": True, "enabled": True, "repo": repo, "package": pkg,
                "max_upload_bytes": self.max_bytes,
                "items": [self.view(r) for r in rows]}

    def authorize(self, metadata: dict, actor: str, guard: Callable) -> dict:
        """Only called by the confirmed admin action, never by worker identity."""
        with self._mutation(), guard(metadata):
            c = self._connect()
            try:
                c.execute("BEGIN IMMEDIATE")
                existing = c.execute("SELECT * FROM uploads WHERE repo=? AND pkg=?",
                                     (metadata["repo"], metadata["pkg"])).fetchone()
                old = dict(existing) if existing else None
                if old and metadata_of(old) != metadata:
                    raise ArtifactError(409, "artifact_conflict", "本包已有不同归档或不同租约的记录，禁止覆盖")
                if old and old["status"] == "received":
                    c.execute("ROLLBACK")
                    with self.open_download(old["upload_id"])[1]:
                        pass
                    return {"upload_id": old["upload_id"], "already_received": True,
                            "artifact": self.view(old)}
                upload_id = old["upload_id"] if old else "A" + secrets.token_hex(12)
                token = "fau_" + secrets.token_urlsafe(32)
                digest = hashlib.sha256(token.encode()).hexdigest()
                now, expires = time.time(), time.time() + self.token_ttl
                if old:
                    c.execute("UPDATE uploads SET token_hash=?,expires_at=?,authorized_at=?,"
                              "status='pending',bytes_received=0,error='' WHERE upload_id=?",
                              (digest, expires, now, upload_id))
                else:
                    c.execute("INSERT INTO uploads(upload_id,repo,pkg,lease_id,user_id,device_id,"
                              "filename,size,sha256,token_hash,expires_at,created_at,authorized_at)"
                              " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (upload_id, *(metadata[k] for k in META_FIELDS), digest,
                               expires, now, now))
                self._event(c, upload_id, "authorized", actor)
                c.execute("COMMIT")
                row = self._row(upload_id)
                return {"upload_id": upload_id, "upload_token": token,
                        "expires_at": expires,
                        "upload_path": f"/api/v1/artifacts/{upload_id}",
                        "artifact": self.view(row)}
            except BaseException:
                if c.in_transaction:
                    c.execute("ROLLBACK")
                raise
            finally:
                c.close()

    def _authenticate(self, upload_id: str, token: str) -> dict:
        row = self._row(upload_id) if UPLOAD_ID_RE.fullmatch(str(upload_id)) else None
        if (not row or not isinstance(token, str) or not token.startswith("fau_")
                or len(token) > 128 or not hmac.compare_digest(
                    hashlib.sha256(token.encode()).hexdigest(), row["token_hash"])):
            raise ArtifactError(401, "upload_unauthorized", "上传凭证无效")
        if time.time() >= row["expires_at"]:
            raise ArtifactError(410, "upload_authorization_expired", "上传授权已过期，请重新授权或查阅回收记录")
        return row

    def _set_receiving(self, row: dict) -> None:
        c = self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE uploads SET status='receiving',bytes_received=0,error='' WHERE upload_id=?",
                      (row["upload_id"],))
            self._event(c, row["upload_id"], "receiving", row["user_id"])
            c.execute("COMMIT")
        finally:
            if c.in_transaction:
                c.execute("ROLLBACK")
            c.close()

    def _mark_failed(self, upload_id: str, code: str) -> None:
        c = self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE uploads SET status='failed',error=? WHERE upload_id=? AND status<>'received'",
                      (code[:80], upload_id))
            row = c.execute("SELECT user_id,status FROM uploads WHERE upload_id=?", (upload_id,)).fetchone()
            if row and row["status"] != "received":
                self._event(c, upload_id, "failed", row["user_id"], code[:80])
            c.execute("COMMIT")
        finally:
            if c.in_transaction:
                c.execute("ROLLBACK")
            c.close()

    def _space_check(self, size: int) -> None:
        # Count physical objects, including a publish-before-index crash orphan.
        used = 0
        for directory in (self.objects, self.incoming):
            for path in directory.iterdir():
                s = path.lstat()
                if not stat.S_ISREG(s.st_mode):
                    raise ArtifactError(503, "storage_unavailable", "产物存储目录包含异常项")
                used += s.st_size
        if used + size > self.max_total_bytes:
            raise ArtifactError(507, "storage_budget_exceeded", "产物存储预算不足")
        if shutil.disk_usage(self.root).free < size + self.min_free_bytes:
            raise ArtifactError(507, "storage_full", "产物存储空间不足")

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _cleanup_temp(self, path: Path | None, upload_id: str) -> None:
        if path is None:
            return
        # Never delete a user-supplied name or a path outside this private temp root.
        if path.parent.resolve() != self.incoming.resolve() or not path.name.startswith(upload_id + "-"):
            raise RuntimeError("unsafe temporary artifact path")
        path.unlink(missing_ok=True)

    def _cleanup_abandoned(self, upload_id: str) -> None:
        # Called only with the OS mutation lock, so no receiver still owns these.
        # A retry cleans its own temporary files, never another package's files.
        for path in self.incoming.iterdir():
            if path.name.startswith(upload_id + "-") and path.name.endswith(".part"):
                self._cleanup_temp(path, upload_id)

    def receive(self, upload_id: str, token: str, stream: BinaryIO, length: int,
                guard: Callable) -> dict:
        if isinstance(length, bool) or not isinstance(length, int) or length <= 0:
            raise ArtifactError(400, "bad_content_length", "需要正整数 Content-Length")
        if length > self.max_bytes:
            raise ArtifactError(413, "artifact_too_large", "归档超过单文件接收上限")
        temp_path = None
        started = False
        committed = False
        with self._mutation():
            row = self._authenticate(upload_id, token)
            metadata = metadata_of(row)
            if length != row["size"]:
                raise ArtifactError(400, "content_length_mismatch", "Content-Length 与授权大小不一致")
            with guard(metadata):
                pass
            replay = row["status"] == "received"
            try:
                self._cleanup_abandoned(upload_id)
                object_path = self._object_path(upload_id)
                reuse_object = object_path.exists() or object_path.is_symlink()
                if reuse_object:
                    with self._open_verified(row):
                        pass
                if not replay and not reuse_object:
                    self._space_check(length)
                    fd, name = tempfile.mkstemp(prefix=upload_id + "-", suffix=".part", dir=self.incoming)
                    temp_path = Path(name)
                    dest = os.fdopen(fd, "wb")
                else:
                    if not replay:
                        self._space_check(0)
                    dest = contextlib.nullcontext(None)
                deadline = time.monotonic() + self.upload_timeout
                digest = hashlib.sha256()
                left, progress_at = length, 0
                read_chunk = getattr(stream, "read1", stream.read)
                with dest as fh:
                    if not replay:
                        started = True
                        self._set_receiving(row)
                    while left:
                        if time.monotonic() >= deadline:
                            raise ArtifactError(408, "upload_timeout", "上传超过接收时限")
                        chunk = read_chunk(min(left, CHUNK_BYTES))
                        if not chunk:
                            raise ArtifactError(400, "truncated_upload", "上传正文不完整")
                        if len(chunk) > left:
                            raise ArtifactError(400, "bad_upload_stream", "上传正文越界")
                        digest.update(chunk)
                        if fh is not None:
                            fh.write(chunk)
                        left -= len(chunk)
                        received = length - left
                        if not replay and received - progress_at >= 8 * CHUNK_BYTES:
                            c = self._connect()
                            try:
                                c.execute("UPDATE uploads SET bytes_received=? WHERE upload_id=?",
                                          (received, upload_id))
                            finally:
                                c.close()
                            progress_at = received
                    if fh is not None:
                        fh.flush()
                        os.fsync(fh.fileno())
                if digest.hexdigest() != row["sha256"]:
                    raise ArtifactError(422, "checksum_mismatch", "上传 SHA-256 与授权不一致，未发布")
                # Expiry and dispatch ownership are checked again after the slow receive.
                self._authenticate(upload_id, token)
                with guard(metadata):
                    if replay:
                        current, verified = self.open_download(upload_id)
                        verified.close()
                        return {"ok": True, "already_received": True, "artifact": current}
                    object_path = self._object_path(upload_id)
                    if object_path.exists() or object_path.is_symlink():
                        # A process may have died after link+fsync and before index commit.
                        # Adopt only the exact already-verified bytes, never overwrite them.
                        with self._open_verified(row):
                            pass
                    else:
                        if temp_path is None:
                            raise ArtifactError(503, "artifact_unavailable", "已验证的归档对象在发布前消失")
                        os.link(temp_path, object_path, follow_symlinks=False)
                        self._fsync_directory(self.objects)
                    c = self._connect()
                    try:
                        c.execute("BEGIN IMMEDIATE")
                        c.execute("UPDATE uploads SET status='received',bytes_received=?,received_at=?,"
                                  "error='',blob_name=? WHERE upload_id=?",
                                  (length, time.time(), object_path.name, upload_id))
                        self._event(c, upload_id, "received", row["user_id"], row["sha256"])
                        c.execute("COMMIT")
                        committed = True
                    finally:
                        if c.in_transaction:
                            c.execute("ROLLBACK")
                        c.close()
                return {"ok": True, "already_received": False,
                        "artifact": self.view(self._row(upload_id))}
            except BaseException as e:
                if started and not committed:
                    code = e.code if isinstance(e, ArtifactError) else (
                        "storage_full" if isinstance(e, OSError) and e.errno == errno.ENOSPC
                        else "receive_failed")
                    try:
                        self._mark_failed(upload_id, code)
                    except sqlite3.Error:
                        pass  # The original failure is authoritative; never claim success.
                raise
            finally:
                self._cleanup_temp(temp_path, upload_id)

    def _open_verified(self, row: dict) -> BinaryIO:
        path = self._object_path(row["upload_id"])
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            fh = os.fdopen(fd, "rb")
            s = os.fstat(fh.fileno())
            if not stat.S_ISREG(s.st_mode) or s.st_size != row["size"]:
                raise ArtifactError(503, "artifact_unavailable", "归档对象缺失或大小不符")
            digest = hashlib.sha256()
            for chunk in iter(lambda: fh.read(CHUNK_BYTES), b""):
                digest.update(chunk)
            if digest.hexdigest() != row["sha256"]:
                raise ArtifactError(503, "artifact_corrupt", "归档校验不符，拒绝下载")
            fh.seek(0)
            return fh
        except BaseException as e:
            if "fh" in locals():
                fh.close()
            if isinstance(e, OSError):
                raise ArtifactError(503, "artifact_unavailable", "归档对象不可用") from e
            raise

    def open_download(self, upload_id: str) -> tuple[dict, BinaryIO]:
        self._object_path(upload_id)  # Validate before looking up or composing paths.
        row = self._row(upload_id)
        if not row or row["status"] != "received":
            raise ArtifactError(404, "artifact_not_received", "产物尚未回收")
        if not self._read_slots.acquire(blocking=False):
            raise ArtifactError(503, "download_busy", "产物下载通道忙，请稍后重试")
        try:
            return self.view(row), _DownloadFile(self._open_verified(row), self._read_slots)
        except BaseException:
            self._read_slots.release()
            raise
