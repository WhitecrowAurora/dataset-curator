"""FTP 上传：把封口后的分包送到汇总机。

设计要点，都是踩过的坑：

1. 先传 .part 再改名。
   FTP 没有原子写。直接传到目标名，一旦中途断开，汇总机上就留下一个
   看起来完整的半截文件——下游会把它当成品读取，静默产出错误结果。
   所以先传 `xxx.zip.part`，成功后 RNFR/RNTO 改名。改名是原子的。

2. 断点续传靠 REST，但很多服务器不支持。
   探测失败就整体重传，不要假设。重传比卡死好。

3. 上传失败不丢数据。
   分片在本地是永久保存的，上传只是搬运。失败就进重试队列，
   本地文件原地不动，下次可以重来。

4. 用 SIZE 校验而不是下载回来校验。
   跨网下载一个 200MB 的分包来算 sha256 太贵，SIZE 足够发现截断。
"""

from __future__ import annotations

import ftplib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class FtpConfig:
    host: str
    user: str
    password: str
    remote_dir: str = "/"
    port: int = 21
    passive: bool = True
    timeout_s: int = 120
    # 上传限速（KB/s），0 = 不限。共享带宽时很有用
    rate_limit_kbps: int = 0
    use_tls: bool = False


@dataclass(slots=True)
class UploadResult:
    ok: bool
    remote_name: str = ""
    bytes_sent: int = 0
    attempts: int = 0
    error: str = ""
    resumed: bool = False


class FtpUploader:
    def __init__(self, cfg: FtpConfig, *, verbose: bool = True):
        self.cfg = cfg
        self.verbose = verbose
        self._ftp: ftplib.FTP | ftplib.FTP_TLS | None = None

    # ---------------------------------------------------------------- 连接

    def connect(self) -> None:
        cls = ftplib.FTP_TLS if self.cfg.use_tls else ftplib.FTP
        ftp = cls()
        ftp.connect(self.cfg.host, self.cfg.port, timeout=self.cfg.timeout_s)
        ftp.login(self.cfg.user, self.cfg.password)
        ftp.set_pasv(self.cfg.passive)
        if self.cfg.use_tls and isinstance(ftp, ftplib.FTP_TLS):
            ftp.prot_p()
        self._ftp = ftp
        self._ensure_remote_dir()

    def _ensure_remote_dir(self) -> None:
        """逐级创建远端目录，已存在就跳过。

        结束后连接**留在 remote_dir 里**。之前最后一句 cwd("/") 把这件事
        白做了：后续 storbinary 传的是裸文件名，全部落在 FTP 根目录，
        remote_dir 成了空壳，汇总机在约定目录下永远等不到文件。
        """
        assert self._ftp is not None
        target = self.cfg.remote_dir
        if target.startswith("/"):
            self._ftp.cwd("/")
        parts = [p for p in target.split("/") if p]
        for part in parts:
            try:
                self._ftp.cwd(part)
            except ftplib.error_perm:
                try:
                    self._ftp.mkd(part)
                    self._ftp.cwd(part)
                except ftplib.error_perm as exc:
                    raise RuntimeError(f"无法创建远端目录 {target}: {exc}") from exc

    def close(self) -> None:
        if self._ftp is None:
            return
        try:
            self._ftp.quit()
        except (ftplib.Error, OSError):
            try:
                self._ftp.close()
            except (ftplib.Error, OSError):
                pass
        self._ftp = None

    # ---------------------------------------------------------------- 上传

    def _remote_size(self, name: str) -> int | None:
        assert self._ftp is not None
        try:
            self._ftp.voidcmd("TYPE I")
            return self._ftp.size(name)
        except (ftplib.Error, OSError):
            return None

    def _remote_tail(self, name: str, *, rest: int) -> bytes | None:
        """取远端文件从 rest 偏移到末尾的字节。取不到返回 None（校验不可用）。"""
        assert self._ftp is not None
        buf = bytearray()
        try:
            self._ftp.voidcmd("TYPE I")
            self._ftp.retrbinary(f"RETR {name}", buf.extend, blocksize=8192,
                                 rest=max(0, rest))
        except (ftplib.Error, OSError):
            return None
        return bytes(buf)

    def _upload_once(self, local: Path, remote: str, *, allow_resume: bool) -> UploadResult:
        assert self._ftp is not None
        size = local.stat().st_size
        sent = 0
        resumed = False

        if allow_resume:
            existing = self._remote_size(remote)
            if existing is not None and 0 < existing < size:
                # SIZE 相同只能说明"长度一样"，不能说明"内容接得上"。
                # 远端那份 .part 可能来自另一个文件的残骸（同名不同批），
                # 盲目 APPE 会把两份字节缝在一起——缝出来的包 tar/zip 都
                # 打不开，还占着一个"看起来传完了"的名字。
                # 所以续传前把远端尾巴拉下来和本地同位置的字节对一遍。
                tail_len = min(existing, 1 << 16)
                tail = self._remote_tail(remote, rest=existing - tail_len)
                local_tail = b""
                if tail is not None:
                    with local.open("rb") as f:
                        f.seek(existing - tail_len)
                        local_tail = f.read(tail_len)
                if tail is None or tail != local_tail:
                    if self.verbose:
                        print(f"[ftp] {remote} 的已有字节对不上（"
                              f"{'读不到' if tail is None else '内容不符'}），整体重传")
                    try:
                        self._ftp.delete(remote)
                    except (ftplib.Error, OSError):
                        pass
                else:
                    try:
                        self._ftp.voidcmd("TYPE I")
                        self._ftp.sendcmd(f"REST {existing}")
                        with local.open("rb") as f:
                            f.seek(existing)
                            self._ftp.storbinary(f"APPE {remote}", f, blocksize=1 << 20)
                        sent = size - existing
                        resumed = True
                    except (ftplib.Error, OSError):
                        # 服务器不支持 REST/APPE，退回整体重传
                        resumed = False
                        try:
                            self._ftp.delete(remote)
                        except (ftplib.Error, OSError):
                            pass

        if not resumed:
            with local.open("rb") as f:
                self._ftp.storbinary(f"STOR {remote}", f, blocksize=1 << 20)
            sent = size

        final = self._remote_size(remote)
        if final is not None and final != size:
            raise RuntimeError(f"上传后大小不符：本地 {size}，远端 {final}")

        return UploadResult(ok=True, remote_name=remote, bytes_sent=sent, resumed=resumed)

    def upload(self, local_path: str | Path, *, remote_name: str | None = None,
               max_attempts: int = 3, allow_resume: bool = True) -> UploadResult:
        """上传一个文件。失败会重试，但不会删除本地文件。"""
        local = Path(local_path)
        if not local.is_file():
            return UploadResult(ok=False, error=f"文件不存在: {local}")

        name = remote_name or local.name
        part = f"{name}.part"
        last_err = ""

        for attempt in range(1, max_attempts + 1):
            try:
                if self._ftp is None:
                    self.connect()
                res = self._upload_once(local, part, allow_resume=allow_resume)
                # 传的是 .part，改名成正式名才算完成
                assert self._ftp is not None
                try:
                    self._ftp.rename(part, name)
                except ftplib.error_perm:
                    # 有些服务器不允许覆盖，先删再改名
                    try:
                        self._ftp.delete(name)
                        self._ftp.rename(part, name)
                    except (ftplib.Error, OSError) as exc:
                        raise RuntimeError(f"改名失败: {exc}") from exc

                if self.verbose:
                    note = "（续传）" if res.resumed else ""
                    print(f"[ftp] 上传完成 {name} {res.bytes_sent / 1048576:.1f} MB {note}")
                res.attempts = attempt
                return res

            except (ftplib.Error, OSError, RuntimeError) as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                if self.verbose:
                    print(f"[ftp] 第 {attempt} 次失败: {last_err}")
                self.close()
                if attempt < max_attempts:
                    time.sleep(min(2 ** attempt, 30))

        return UploadResult(ok=False, remote_name=name, attempts=max_attempts, error=last_err)

    def upload_many(self, paths: list[str | Path], **kwargs) -> list[UploadResult]:
        return [self.upload(p, **kwargs) for p in paths]


# 归档可能产出的分片后缀。webdataset 是 .tar，zip 是 .zip。
# 默认顺序把 .tar 放前面，因为 webdataset 是默认格式。
SHARD_SUFFIXES: tuple[str, ...] = (".tar", ".zip")


def list_local_shards(
    out_dir: str | Path,
    suffixes: tuple[str, ...] = SHARD_SUFFIXES,
) -> list[Path]:
    """待上传的分包列表。已经传过的由调用方用 .uploaded 标记文件排除。

    早期版本只认 .zip，把输出格式默认改成 webdataset 之后那些 .tar 会被
    静默漏掉——本地看着有产物，远端却一条没收到。所以后缀必须可枚举。
    """
    out_dir = Path(out_dir)
    marker = out_dir / ".uploaded"
    done: set[str] = set()
    if marker.is_file():
        done = {l.strip() for l in marker.read_text(encoding="utf-8").splitlines() if l.strip()}

    pending: list[Path] = []
    for suffix in suffixes:
        pending.extend(p for p in out_dir.glob(f"*{suffix}") if p.name not in done)
    # 按名字排序而不是按后缀分组，让同一批样本的 zip/tar 相邻，便于核对
    return sorted(pending, key=lambda p: (p.stem, p.suffix))


def mark_uploaded(out_dir: str | Path, name: str) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / ".uploaded").open("a", encoding="utf-8") as f:
        f.write(name + "\n")


def archive_dir_to_ftp(
    cfg: FtpConfig,
    out_dir: str | Path,
    *,
    enabled: bool = True,
    dry_run: bool = False,
    suffixes: tuple[str, ...] = SHARD_SUFFIXES,
    verbose: bool = True,
) -> tuple[int, int]:
    """把目录下所有未上传的分片传上去，返回 (成功数, 失败数)。

    enabled=False 时什么都不做，分片原样留在本地。这是默认状态：
    没有 FTP 服务器、或者落地目录还没谈好，流程也应该能完整跑完。

    与"传完了"的区别在于这里会明确打印被禁用，而不是假装成功。
    """
    if not enabled:
        if verbose:
            print("[ftp] 上传已禁用（upload.enabled: false），分片保留在本地：")
            print(f"      {Path(out_dir).resolve()}")
            print("      需要上传时把 upload.enabled 改成 true，或加 --force。")
        return 0, 0

    pending = list_local_shards(out_dir, suffixes)
    if not pending:
        if verbose:
            print(f"[ftp] {out_dir} 下没有待上传的分片（后缀 {suffixes}）")
        return 0, 0

    if dry_run:
        if verbose:
            print(f"[ftp] dry-run：以下 {len(pending)} 个分片会被上传，实际未连接")
            for path in pending:
                print(f"      {path.name}  {path.stat().st_size / 1048576:.1f} MB")
        return len(pending), 0

    ok = fail = 0
    uploader = FtpUploader(cfg, verbose=verbose)
    try:
        for path in pending:
            res = uploader.upload(path)
            if res.ok:
                mark_uploaded(out_dir, path.name)
                ok += 1
            else:
                fail += 1
    finally:
        uploader.close()
    return ok, fail
