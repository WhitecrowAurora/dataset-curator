"""分块回传（HTTP）服务端：内容寻址的块仓库 + 提交成文件。

为什么不用 FTP：`upload.py` 里那些 REST/APPE/SIZE 的兼容分支、续传前还要
把远端末尾 64KB 拉回来比对，全是在替服务器的不一致兜底。块寻址天然解决：

- **续传判据是哈希**。每块按 sha256 命名，传一半断了，重连时问一次
  `/api/blob/have`，只补缺的块。不存在"长度对得上但内容接不上"这种情况。
- **重复传同一批是秒传**。/api/blob/have 返回已有的块，一个字节都不用传。
  再跑一次续做、重算过的批次，共有的块直接复用。
- **失败恢复粒度是块**，不是整个几百 MB 的分片。

落盘布局（都在 root 下）：

    objects/ab/ab12...          块内容，文件名就是它的 sha256
    tmp/upload-xxxxxx           接收中的临时文件（写完 rename 进 objects）
    files/<按 commit 的相对路径> 拼好的成品

★块仓库只按内容存，**不区分来自哪次提交**。所以清理只能按"是否被任何
  manifest 引用"，见 gc()；不能按 mtime 乱删，删掉可能正被别的 manifest
 引用的块，下游就读到一个缺洞的文件。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

# 块名只允许 64 位小写 hex（sha256）。这条校验是**路径穿越的第一道门**：
# 不校验的话 `../` 或绝对路径能直接写到仓库外面去。
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
# 相对路径里不允许 ..、绝对路径、盘符。第二道门，commit 时用。
_SAFE_NAME_RE = re.compile(r"^[^\\/:*?\"<>|]+$")

# 单块上限。8MB 是 `blobs.py`(客户端) 的默认块长，留一倍余量给调用方调整。
MAX_CHUNK = 64 << 20


class BlobError(Exception):
    """调用方能自己修的错误（块名非法、manifest 不完整）。路由翻成 400。"""


class BlobStore:
    """进程级单例。所有方法都可在多线程下调用（工作线程 + HTTP 请求）。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()
        # gc 专用锁，和 _lock 分开：_lock 护的是"块/成品的写"，gc 要与
        # commit() 互斥（否则可能读到写了一半的 .chunks 清单，把正在被
        # 引用的块当孤儿删掉），但不需要挡住 put_stream 的整块写入。
        self._gc_lock = threading.Lock()

    # ---------------------------------------------------------------- 路径

    @property
    def objects(self) -> Path:
        return self.root / "objects"

    @property
    def tmp(self) -> Path:
        return self.root / "tmp"

    @property
    def files(self) -> Path:
        return self.root / "files"

    def _object_path(self, sha: str) -> Path:
        if not SHA_RE.match(sha or ""):
            raise BlobError(f"块名不是合法的 sha256：{sha!r}")
        return self.objects / sha[:2] / sha

    def has(self, sha: str) -> bool:
        try:
            return self._object_path(sha).is_file()
        except BlobError:
            return False

    def have(self, shas: list[str]) -> tuple[list[str], list[str]]:
        """返回 (已有的, 非法的)。非法的单列出来，别让一个坏块名拖垮整次询问。"""
        ok: list[str] = []
        bad: list[str] = []
        for sha in shas:
            if not isinstance(sha, str) or not SHA_RE.match(sha):
                bad.append(str(sha))
            elif self.has(sha):
                ok.append(sha)
        return ok, bad

    # ---------------------------------------------------------------- 写入

    def put_chunk(self, sha: str, data: bytes) -> bool:
        """落一块，返回 True=新落下，False=已经有了（秒传）。

        先写 tmp 再 rename：进程被杀时只会在 tmp 里留垃圾，不会在 objects
        里留一个"长度对、内容半截"的块——后者才是真正会污染下游的东西。
        """
        target = self._object_path(sha)
        if len(data) > MAX_CHUNK:
            raise BlobError(f"单块超过上限 {MAX_CHUNK} 字节：{len(data)}")
        actual = hashlib.sha256(data).hexdigest()
        if actual != sha:
            raise BlobError(f"块内容与名字不符：名字 {sha[:12]}，实际 {actual[:12]}")
        if target.is_file():
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.tmp
        tmp.mkdir(parents=True, exist_ok=True)
        scratch = tmp / f"upload-{uuid.uuid4().hex}"
        try:
            scratch.write_bytes(data)
            # 目标可能被并发请求抢先落好：os.replace 会覆盖，内容一样，无害
            os.replace(scratch, target)
        finally:
            if scratch.exists():
                try:
                    scratch.unlink()
                except OSError:
                    pass
        return True

    async def put_stream(self, sha: str, read) -> tuple[bool, int]:
        """流式落一块。`read` 是 async 的可调用，返回字节，空字节 = 结束。

        不把 8MB 全读进内存：写 tmp 的同时算 sha256，bytes / 计数都过一遍手。
        **已经有这块时也要把请求体读完**，否则 HTTP keep-alive 连接会错位
        （下一个请求的头部会被当成上一块的尾巴）。
        """
        if not SHA_RE.match(sha or ""):
            raise BlobError(f"块名不是合法的 sha256：{sha!r}")
        target = self._object_path(sha)
        exists = target.is_file()
        scratch: Path | None = None
        digest = hashlib.sha256()
        total = 0
        fh = None
        try:
            if not exists:
                target.parent.mkdir(parents=True, exist_ok=True)
                self.tmp.mkdir(parents=True, exist_ok=True)
                scratch = self.tmp / f"upload-{uuid.uuid4().hex}"
                fh = scratch.open("wb")
            while True:
                part = await read()
                if not part:
                    break
                total += len(part)
                if total > MAX_CHUNK:
                    raise BlobError(f"单块超过上限 {MAX_CHUNK} 字节")
                if exists:
                    continue                      # 只要把流读干净，不落盘
                digest.update(part)
                fh.write(part)
            if exists:
                return False, total
            assert scratch is not None and fh is not None
            fh.close()
            fh = None
            if digest.hexdigest() != sha:
                raise BlobError(
                    f"块内容与名字不符：名字 {sha[:12]}，实际 {digest.hexdigest()[:12]}")
            os.replace(scratch, target)
            scratch = None
            return True, total
        finally:
            if fh is not None:
                fh.close()
            if scratch is not None and scratch.exists():
                try:
                    scratch.unlink()
                except OSError:
                    pass

    # ---------------------------------------------------------------- 提交

    def _safe_rel(self, name: str) -> Path:
        """把 commit 里的名字翻成仓库内的相对路径，拒绝一切越界写法。"""
        raw = (name or "").replace("\\", "/").strip()
        if not raw:
            raise BlobError("文件名不能为空")
        parts = [p for p in raw.split("/") if p not in ("", ".")]
        if not parts:
            raise BlobError(f"文件名非法：{name!r}")
        for p in parts:
            if p == ".." or not _SAFE_NAME_RE.match(p):
                raise BlobError(f"文件名含非法片段 {p!r}：{name!r}")
        return Path(*parts)

    def commit(self, name: str, shas: list[str], *, total_bytes: int = 0) -> dict:
        """按块顺序拼成 files/<name>。缺块直接报错，不留半截成品。

        返回 {name, bytes, sha256, chunks}；sha256 是拼好整文件的哈希，
        调用方拿它和自己本地算的对一遍就完成了端到端校验。
        """
        rel = self._safe_rel(name)
        if not shas:
            raise BlobError("manifest 是空的")
        missing = [s for s in shas if not self.has(s)]
        if missing:
            raise BlobError(f"还缺 {len(missing)} 块，例如 {missing[0][:12]}")
        out = self.files / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        # 先拼到同名 .part，成功后 rename：轮询/下游不会读到半份文件
        scratch = out.with_name(out.name + ".part")
        digest = hashlib.sha256()
        written = 0
        # ★整段与 gc 互斥：gc 的"读引用表 → 删块"必须看到一致的快照。若不锁，
        # 这里正在依次打开 read 一块的对象文件，gc 恰好判定它没被引用（清单
        # 还没写）→ unlink，Windows 上 open 会直接失败、POSIX 上更隐蔽——
        # 已打开的 fd 还能读，但拼出来的成品对应的块已经没了，下次 gc 无关，
        # 这份成品从此无法再被复用（秒传失效）。锁住即可，代价是 gc 等一次拼装。
        with self._gc_lock:
            try:
                with scratch.open("wb") as w:
                    for sha in shas:
                        with self._object_path(sha).open("rb") as r:
                            while True:
                                buf = r.read(1 << 20)
                                if not buf:
                                    break
                                written += len(buf)
                                digest.update(buf)
                                w.write(buf)
                if total_bytes and written != total_bytes:
                    raise BlobError(f"拼出来 {written} 字节，与清单声明的 {total_bytes} 不符")
                os.replace(scratch, out)
            finally:
                if scratch.exists():
                    try:
                        scratch.unlink()
                    except OSError:
                        pass
            # 引用表：gc() 靠它判断哪些块还在用。内容一样的两份文件共用一套块，
            # 所以这张表是按 sha 列举的，不是"按文件独占"。与 rename 放在同一
            # 个临界区里：中间态被 gc 看到就等于漏算引用。
            self._write_refs(out, shas)
        return {"name": str(rel).replace("\\", "/"), "bytes": written,
                "sha256": digest.hexdigest(), "chunks": len(shas)}

    def _refs_path(self, out: Path) -> Path:
        return out.with_name(out.name + ".chunks")

    def _write_refs(self, out: Path, shas: list[str]) -> None:
        tmp = self._refs_path(out).with_name(out.name + ".chunks.part")
        tmp.write_text("\n".join(shas) + "\n", encoding="utf-8")
        os.replace(tmp, self._refs_path(out))

    def refs(self, name: str) -> list[str]:
        out = self.files / self._safe_rel(name)
        p = self._refs_path(out)
        if not p.is_file():
            return []
        return [l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]

    # ---------------------------------------------------------------- 清理

    def referenced(self) -> set[str]:
        """当前所有成品的 manifest 引用到的块。"""
        used: set[str] = set()
        if not self.files.is_dir():
            return used
        for p in self.files.rglob("*.chunks"):
            try:
                used.update(l.strip() for l in
                            p.read_text(encoding="utf-8").splitlines() if l.strip())
            except OSError:
                continue
        return used

    def gc(self, *, dry_run: bool = True, linger_hours: float = 24.0) -> dict:
        """删掉没被任何成品引用的块。**默认 dry_run**：这是不可逆操作。

        linger_hours：未引用的块先留一段时间。上传是"先落块、后 commit"，
        传完还没提交的那批块必然暂时没人引用，立刻清会把在途的上传打断。
        """
        # 与 commit 互斥：清单是"先写 .chunks 再 rename"，不加锁可能读到
        # 半份清单 → 少算引用 → 删掉正被引用的块。锁只在 gc 这一侧取，
        # put_stream 不受影响（它写的块本就还没人引用，被 linger 豁免）。
        with self._gc_lock:
            return self._gc_locked(dry_run=dry_run, linger_hours=linger_hours)

    def _gc_locked(self, *, dry_run: bool, linger_hours: float) -> dict:
        used = self.referenced()
        cutoff = time.time() - linger_hours * 3600
        victims: list[Path] = []
        freed = 0
        kept = 0
        if self.objects.is_dir():
            for p in self.objects.rglob("*"):
                if not p.is_file():
                    continue
                if p.name in used:
                    kept += 1
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                if st.st_mtime > cutoff:
                    kept += 1
                    continue
                victims.append(p)
                freed += st.st_size
        # 在途的临时文件同样按时间清：进程被杀留下的那些永远不会有人来收拾
        dropped_tmp = 0
        if self.tmp.is_dir():
            for p in self.tmp.iterdir():
                try:
                    if p.stat().st_mtime <= cutoff:
                        dropped_tmp += 1
                        if not dry_run:
                            p.unlink()
                except OSError:
                    continue
        if not dry_run:
            for p in victims:
                try:
                    p.unlink()
                except OSError:
                    continue
            # 顺手删空的二级目录（ab/ 这种）
            if self.objects.is_dir():
                for d in self.objects.iterdir():
                    try:
                        if d.is_dir() and not any(d.iterdir()):
                            d.rmdir()
                    except OSError:
                        continue
        return {"dry_run": dry_run, "orphans": len(victims),
                "bytes": freed, "kept": kept, "tmp_dropped": dropped_tmp,
                "referenced": len(used)}

    def stats(self) -> dict:
        files = 0
        total = 0
        if self.objects.is_dir():
            for p in self.objects.rglob("*"):
                if p.is_file():
                    files += 1
                    try:
                        total += p.stat().st_size
                    except OSError:
                        continue
        finished = 0
        if self.files.is_dir():
            # 引用表 *.chunks 是内部记账，*.verify.json 是校验报告——
            # 都不是交付物；算进"文件数"会让人以为多传了一份
            finished = sum(1 for p in self.files.rglob("*")
                           if p.is_file()
                           and not p.name.endswith((".chunks", ".part", ".verify.json")))
        return {"chunks": files, "bytes": total, "files": finished,
                "root": str(self.root), "referenced": len(self.referenced())}

    def reset(self) -> None:
        """清空仓库（测试/换目录用）。"""
        with self._lock:
            for sub in (self.objects, self.tmp, self.files):
                if sub.is_dir():
                    shutil.rmtree(sub, ignore_errors=True)


# 进程级单例：仓库根可用 TAGGER_BLOB_ROOT 覆盖（容器里指到数据盘）。
# app.py 的 create_app() 不重建它——同一进程里换根会让在途上传写到两个地方。
BLOB_ROOT_DEFAULT = Path(os.environ.get("TAGGER_BLOB_ROOT")
                         or (Path.home() / ".tagger" / "blobstore"))
BLOBS = BlobStore(BLOB_ROOT_DEFAULT)
