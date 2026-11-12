"""递归穿透嵌套压缩包，把里面的图片当成"虚拟目录里的文件"列出来。

素材组交上来的包经常是 zip 套 rar 套 tar 套 zip，层数不定，有的还带密码，
而且**同一个包里不同内层可能用了不同密码**。这个模块只做一件事：不管嵌套
多少层、密码有多少个，站在调用方视角都像在遍历一个普通目录。

三条设计：

**1. 格式按字节判断，不看扩展名。**
素材包里 `.zip` 实际是 rar、`.jpg` 实际是 zip 的情况很常见。

**2. 密码按容器缓存。**
同一个内层包里有几万张图，每张都从密码表第一个重试一遍的话，光试密码就能
跑到明天。每个容器只解一次密码，结果按容器路径记住。

**3. 按格式的随机访问能力分策略**，这决定了要不要落盘：

    zip   成员可随机读 → 懒加载，一个字节都不多解
    rar   成员可随机读 → 懒加载（需要 unrar）
    tar   顺序流        → 整包解到临时目录，避免每读一个成员就重解一遍
    7z    solid 压缩    → 只能整包解压

**代价**：内层是压缩过的容器时，必须先解出来才能读到它的目录（zip 的中央
目录在文件末尾，压缩流没法随机跳）。解在 `work_dir` 里，该层遍历完立刻删，
所以峰值只占"单个内层包"的大小，不是整棵树。
"""

from __future__ import annotations

import io
import shutil
import tarfile
import tempfile
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import tmpdir

# ---------------------------------------------------------------- 格式识别

MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip"),          # 空 zip
    (b"PK\x07\x08", "zip"),          # 分卷标记
    (b"Rar!\x1a\x07", "rar"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"\x1f\x8b", "gz"),
    (b"BZh", "bz2"),
    (b"\xfd7zXZ\x00", "xz"),
)

NESTED = {"zip", "rar", "7z", "tar", "gz", "bz2", "xz"}

_SUFFIXES = (".zip", ".rar", ".7z", ".tar", ".tgz", ".tar.gz",
             ".tar.bz2", ".tar.xz", ".gz", ".bz2", ".xz")


def sniff_file(path: str | Path) -> str | None:
    """读头几个字节判断真实格式。tar 没有魔术数，靠偏移 257 的 `ustar`。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(512)
    except OSError:
        return None
    for magic, kind in MAGIC:
        if head.startswith(magic):
            return kind
    if len(head) >= 262 and head[257:262] == b"ustar":
        return "tar"
    return None


def describe_backends() -> dict[str, dict[str, object]]:
    """各格式现在能不能读——依赖中心照着这个显示。"""
    def has(mod: str) -> bool:
        try:
            __import__(mod)
            return True
        except Exception:                      # noqa: BLE001
            return False

    rar_ok, rar_hint = False, "需要 unrar（WinRAR 自带）或 7-Zip 的命令行"
    if has("rarfile"):
        try:
            import rarfile
            rarfile.tool_setup()
            rar_ok, rar_hint = True, ""
        except Exception:                      # noqa: BLE001
            pass

    return {
        "zip":     {"ok": True, "note": "内置；含传统 ZipCrypto 加密", "hint": ""},
        "zip_aes": {"ok": has("pyzipper"), "note": "AES-256 加密的 zip（7-Zip/WinRAR 默认）",
                    "hint": "pip install pyzipper"},
        "tar":     {"ok": True, "note": "含 tar.gz / bz2 / xz，顺序解压",
                    "hint": ""},
        "7z":      {"ok": has("py7zr"), "note": "纯 Python；solid 压缩需整包解压",
                    "hint": "pip install py7zr"},
        "rar":     {"ok": rar_ok, "note": "需外部 unrar 可执行文件", "hint": rar_hint},
    }


# ---------------------------------------------------------------- 密码表

@dataclass
class PasswordTable:
    """一串候选密码，按顺序试；**空密码永远排第一个**。

    素材包常常有的加密、有的没有。空密码先试，否则一个没密码的包会被
    当成"密码不对"卡住，白跑一遍密码表。
    """

    items: list[str] = field(default_factory=list)
    _cache: dict[str, str] = field(default_factory=dict, repr=False)
    _failed: dict[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, path: str | Path | None) -> "PasswordTable":
        if not path:
            return cls()
        p = Path(path)
        if not p.is_file():
            raise ValueError(f"密码表不存在：{p}")
        raw = p.read_text(encoding="utf-8", errors="replace").splitlines()
        items = [ln.strip() for ln in raw
                 if ln.strip() and not ln.lstrip().startswith("#")]
        return cls(items=items)

    def candidates(self) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for pwd in ["", *self.items]:
            if pwd not in seen:
                seen.add(pwd)
                out.append(pwd)
        return out

    def remember(self, key: str, pwd: str) -> None:
        self._cache[key] = pwd

    def cached(self, key: str) -> str | None:
        return self._cache.get(key)

    def mark_failed(self, key: str, reason: str) -> None:
        self._failed[key] = reason

    @property
    def failures(self) -> dict[str, str]:
        return dict(self._failed)


# ---------------------------------------------------------------- 条目

@dataclass
class Entry:
    """虚拟路径下的一个文件。

    `open()` 每次调用都新建独立句柄，可重复读；但如果它是临时解出来的，
    文件会在本层遍历结束时删掉——要在推进到下一个条目之前读完。
    """

    vpath: str
    name: str
    size: int
    depth: int
    _open: Callable[[], io.IOBase] = field(repr=False)
    # 最外层真实文件（顶层包，或散图本身）。vpath 是**相对它**的虚拟路径。
    # 只有 vpath 定位不到数据：顶层包的成员在 vpath 里不带包名。
    source: str = ""

    def open(self) -> io.IOBase:
        return self._open()


def _file_opener(path: Path) -> Callable[[], io.IOBase]:
    return lambda: open(path, "rb")


# ---------------------------------------------------------------- 各格式

def _zip_names(path: Path, pwd: str | None) -> list[tuple[str, int, int]]:
    """(名字, 解压后大小, 压缩方式)。AES 加密的走 pyzipper。"""
    try:
        with zipfile.ZipFile(path) as zf:
            return [(i.filename, i.file_size, i.compress_type) for i in zf.infolist()]
    except RuntimeError:
        pass                               # 加密头会让 zipfile 直接抛 RuntimeError
    except (zipfile.BadZipFile, OSError):
        return []
    return _zip_names_aes(path, pwd)


def _zip_names_aes(path: Path, pwd: str | None) -> list[tuple[str, int, int]]:
    try:
        import pyzipper
    except ImportError:
        return []
    try:
        with pyzipper.AESZipFile(path) as zf:
            if pwd:
                zf.setpassword(pwd.encode())
            return [(i.filename, i.file_size, i.compress_type) for i in zf.infolist()]
    except Exception:                      # noqa: BLE001 - 密码错/格式怪都当读不出
        return []


def _open_zip(path: Path, name: str, pwd: str | None) -> io.IOBase:
    try:
        zf = zipfile.ZipFile(path)
        return zf.open(name)
    except RuntimeError:
        import pyzipper
        zf = pyzipper.AESZipFile(path)
        if pwd:
            zf.setpassword(pwd.encode())
        return zf.open(name)


def _open_rar(path: Path, name: str, pwd: str | None) -> io.IOBase:
    import rarfile
    rf = rarfile.RarFile(path)
    if pwd:
        rf.setpassword(pwd)
    return rf.open(name)


def _open_tar(path: Path, name: str) -> io.IOBase:
    tf = tarfile.open(path, "r:*")
    src = tf.extractfile(tf.getmember(name))
    if src is None:
        raise OSError(f"{name} 不是普通文件")
    return src


def _extract_one(path: Path, kind: str, name: str, pwd: str | None, dest: Path) -> bool:
    """把内层容器成员解到 dest。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        if kind == "zip":
            with open(dest, "wb") as out:
                shutil.copyfileobj(_open_zip(path, name, pwd), out, 1 << 20)
            return True
        if kind == "tar":
            with open(dest, "wb") as out:
                shutil.copyfileobj(_open_tar(path, name), out, 1 << 20)
            return True
        if kind == "rar":
            with open(dest, "wb") as out:
                shutil.copyfileobj(_open_rar(path, name, pwd), out, 1 << 20)
            return True
    except Exception:                      # noqa: BLE001
        return False
    return False


def _extract_all_7z(path: Path, pwd: str | None, dest: Path) -> bool:
    """7z 是 solid 压缩，只能整包解——这是格式决定的，不是实现偷懒。"""
    try:
        import py7zr
    except ImportError:
        return False
    try:
        with py7zr.SevenZipFile(path, mode="r", password=pwd or None) as zf:
            zf.extractall(path=dest)
        return True
    except Exception:                      # noqa: BLE001
        return False


def _extract_all_tar(path: Path, dest: Path) -> bool:
    try:
        with tarfile.open(path, "r:*") as tf:
            try:
                tf.extractall(path=dest, filter="data")   # 3.12+；挡掉 ../ 穿越
            except TypeError:                             # 老版本没有 filter
                tf.extractall(path=dest)
        return True
    except Exception:                      # noqa: BLE001
        return False


def _can_read(path: Path, kind: str, pwd: str | None) -> bool:
    """真的读一小段成员数据来判断密码对不对。

    不能只看能不能列出成员：AES 加密的 zip 只加密数据、不加密文件名，
    `infolist()` 空密码也能列出来，看起来像"打开成功"，实际读的时候就炸。
    """
    try:
        if kind == "zip":
            try:
                zf = zipfile.ZipFile(path)
                names = zf.namelist()
                if not names:
                    return True
                with zf.open(names[0]) as fh:
                    fh.read(16)
                return True
            except RuntimeError:
                import pyzipper
                zf = pyzipper.AESZipFile(path)
                if pwd:
                    zf.setpassword(pwd.encode())
                names = zf.namelist()
                if not names:
                    return True
                with zf.open(names[0]) as fh:
                    fh.read(16)
                return True
        if kind == "7z":
            import py7zr
            with py7zr.SevenZipFile(path, mode="r", password=pwd or None) as zf:
                return bool(zf.list()) or True
        if kind == "rar":
            import rarfile
            rarfile.tool_setup()
            with rarfile.RarFile(path) as rf:
                if pwd:
                    rf.setpassword(pwd)
                infos = [i for i in rf.infolist() if not i.isdir()]
                if not infos:
                    return True
                with rf.open(infos[0]) as fh:
                    fh.read(16)
                return True
    except Exception:                      # noqa: BLE001
        return False
    return False


def _probe(path: Path, kind: str, table: PasswordTable, key: str) -> str | None:
    """按密码表逐个试，第一个真能读出数据的算命中；结果按容器缓存。"""
    if kind not in ("zip", "7z", "rar"):
        return ""
    hit = table.cached(key)
    if hit is not None:
        return hit

    candidates = table.candidates()
    for pwd in candidates:
        if _can_read(path, kind, pwd or None):
            table.remember(key, pwd)
            return pwd

    table.mark_failed(key, f"密码表里 {len(candidates)} 个密码都没打开")
    return None


def _names_7z(path: Path, pwd: str | None) -> list[tuple[str, int]]:
    try:
        import py7zr
    except ImportError:
        return []
    try:
        with py7zr.SevenZipFile(path, mode="r", password=pwd or None) as zf:
            return [(i.filename, int(i.uncompressed or 0))
                    for i in zf.list() if not i.is_directory]
    except Exception:                      # noqa: BLE001
        return []


def _names_rar(path: Path, pwd: str | None) -> list[tuple[str, int]]:
    try:
        import rarfile
        rarfile.tool_setup()
    except Exception:                      # noqa: BLE001
        return []
    try:
        with rarfile.RarFile(path) as rf:
            if pwd:
                rf.setpassword(pwd)
            return [(i.filename, i.file_size) for i in rf.infolist() if not i.isdir()]
    except Exception:                      # noqa: BLE001
        return []


def _names_tar(path: Path) -> list[tuple[str, int]]:
    try:
        with tarfile.open(path, "r:*") as tf:
            return [(m.name, m.size) for m in tf.getmembers()
                    if m.isfile() and not m.isdir()]
    except Exception:                      # noqa: BLE001
        return []


def _is_nested_name(name: str) -> bool:
    low = name.lower()
    return low.endswith(_SUFFIXES)


# ---------------------------------------------------------------- 遍历

def iter_files(root: str | Path, *,
               work_dir: str | Path | None = None,
               passwords: PasswordTable | None = None,
               max_depth: int = 16,
               should_stop: Callable[[], bool] | None = None,
               on_log: Callable[[str], None] | None = None,
               ) -> Iterator[Entry]:
    """递归列出 root 下的文件（含嵌套容器内的）。

    root 可以是目录、单个压缩包，或单张图。容器内成员以
    `外层.zip::内层.rar::图.jpg` 这样的虚拟路径呈现。
    """
    table = passwords or PasswordTable()
    root_path = Path(root)
    # 默认落 ROOT/tmp/tagger-extract，不走 %TEMP%：解压是这里最占盘的一步，
    # 一个 30M 的 tar 能解出几百 MB（详见 tmpdir）。
    base_work = Path(work_dir) if work_dir else tmpdir.path("tagger-extract")
    base_work.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    def stopped() -> bool:
        return bool(should_stop and should_stop())

    def walk(real: Path, prefix: str, depth: int, origin: Path) -> Iterator[Entry]:
        if stopped():
            return
        if depth > max_depth:
            log(f"超过最大嵌套层数（{max_depth}），跳过 {prefix or real.name}")
            return

        kind = sniff_file(real)
        if kind not in NESTED:
            if real.is_file():
                yield Entry(vpath=f"{prefix}{real.name}", name=real.name,
                            size=real.stat().st_size, depth=depth,
                            _open=_file_opener(real), source=str(origin))
            return

        pwd = _probe(real, kind, table, prefix.rstrip("::") or str(real))
        if pwd is None:
            log(f"打不开（密码不对或文件损坏）：{prefix.rstrip('::') or real.name}")
            return

        # tar / 7z 只能顺序解：整包解到临时目录，再当普通目录走
        if kind in ("tar", "7z", "gz", "bz2", "xz"):
            tmp = Path(tempfile.mkdtemp(dir=base_work))
            try:
                ok = (_extract_all_7z(real, pwd or None, tmp) if kind == "7z"
                      else _extract_all_tar(real, tmp))
                if not ok:
                    log(f"解不开：{prefix}{real.name}")
                    return
                # prefix 已经带上了容器名（调用方传进来的），这里不能再拼
                # real.name——那是临时文件名 inner.bin，拼上去路径就变成了
                # `inner.bin::t/4.jpg`，跟原始包里的名字对不上。
                # 但目录层级要保留，否则 `t/4.jpg` 会塌成 `4.jpg`。
                for inner in sorted(tmp.rglob("*")):
                    if inner.is_file() and not stopped():
                        yield from walk(inner, _dir_prefix(tmp, inner, prefix),
                                        depth + 1, origin)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            return

        # zip / rar 成员可随机读：懒加载
        if kind == "zip":
            members = [(n, s) for n, s, _ in _zip_names(real, pwd or None) if not n.endswith("/")]
        else:
            members = _names_rar(real, pwd or None)

        for name, size in members:
            if stopped():
                return
            if _is_nested_name(name):
                tmp = Path(tempfile.mkdtemp(dir=base_work))
                try:
                    dest = tmp / "inner.bin"
                    if not _extract_one(real, kind, name, pwd, dest):
                        log(f"解不出来：{prefix}{name}")
                        continue
                    yield from walk(dest, f"{prefix}{name}::", depth + 1, origin)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
                continue

            opener = ((lambda n=name, p=pwd: _open_zip(real, n, p)) if kind == "zip"
                      else (lambda n=name, p=pwd: _open_rar(real, n, p)))
            yield Entry(vpath=f"{prefix}{name}", name=Path(name).name,
                        size=size, depth=depth, _open=opener,
                        source=str(origin))

    if root_path.is_file():
        yield from walk(root_path, "", 0, root_path)
    else:
        for path in sorted(root_path.rglob("*")):
            if stopped():
                return
            if path.is_file():
                yield from walk(path, _dir_prefix(root_path, path, ""), 0, path)


def _dir_prefix(base: Path, file: Path, prefix: str) -> str:
    """`file` 相对 `base` 的目录部分，接在 `prefix` 后面。

    walk() 产出的是 `prefix + file.name`，所以目录层级必须由前缀带进去。
    """
    parent = file.relative_to(base).parent.as_posix()
    return prefix if parent == "." else f"{prefix}{parent}/"
