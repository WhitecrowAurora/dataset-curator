"""llama.cpp 运行时：就地找到 / 下载 / 解开 llama-server。

**为什么需要这一层**：llama-server 不是 pip 包，官方只发 GitHub release 的
zip。默认引擎改成 llama.cpp 之后，第一次点「启动服务」必然失败，报的是
"找不到 llama-server"，而错误信息里给的两条路（下 zip 加 PATH / 设环境变量）
都要人离开网页去命令行。这一步就变成了"这个软件装不上"的观感——其实只是
一个 200MB 的 zip。

几条口径：

1. **装在 runtime/llama.cpp/**，与 models/ 并列。它和模型权重是一类东西
   （跑起来需要的大文件、可重下、迁移时要一起搬），不是配置。
2. **不改进程环境变量**。解压完就能用，靠的是 `find_binary()` 主动去这个
   目录里找——改进程的 PATH 对已经跑着的解释器没用，写系统环境变量更过分。
   用户想钉死版本仍然可以用 `TAGGER_LLAMA_SERVER`。
3. **CUDA 包要配 cudart 包**。官方 Windows CUDA 构建不带 CUDA 运行库，
   只解主包的话 `llama-server.exe` 双击就缺 dll；两个 zip 解到同一目录才对。
4. **只在真的点按钮时联网**。`status()` 不查 GitHub（页面每几秒轮询一次），
   否则每次刷新都要等一个可能被墙的请求。
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import threading
import time
import zipfile
from pathlib import Path

import httpx

from .netutil import trust_env_for

ROOT = Path(__file__).resolve().parents[1]
INSTALL_ROOT = ROOT / "runtime" / "llama.cpp"
DOWNLOAD_DIR = INSTALL_ROOT / ".download"
VERSION_FILE = INSTALL_ROOT / "VERSION"

RELEASE_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
# 为什么要看列表而不是 latest：2026 年起 `latest` 指向的是版本号发布（v0.9.0），
# 那里可能一个二进制都没有，滚动构建（b12188 这种）才挂资产。只查 latest 的
# 结果就是"点安装必然报没有匹配的包"，而 GitHub 上明明有。
RELEASES_API = ("https://api.github.com/repos/ggml-org/llama.cpp/"
                "releases?per_page=30")
# Windows 上可执行文件带后缀；判断用 os.name 而不是 platform.system()，
# 后者在 Windows 上返回 "Windows"，与 exe 是否存在无关。
BINARY_NAME = "llama-server.exe" if os.name == "nt" else "llama-server"

_TIMEOUT = httpx.Timeout(connect=20.0, read=120.0, write=120.0, pool=20.0)
_CHUNK = 1 << 20

# 安装阶段。页面按这个值决定显示"下载中 / 解压中 / 装好了"。
STAGE_IDLE = ""
STAGE_PROBING = "probing"
STAGE_DOWNLOADING = "downloading"
STAGE_EXTRACTING = "extracting"
STAGE_VERIFYING = "verifying"
STAGE_DONE = "done"
STAGE_ERROR = "error"
STAGE_CANCELLED = "cancelled"


class _Cancelled(Exception):
    """用户点了取消。只在这个模块内部流转，不会漏到调用方。"""


# ------------------------------------------------------------------ 查找

def find_binary() -> str | None:
    """找一个能用的 llama-server。环境变量 > 本模块装的 > PATH。

    顺序是有讲究的：环境变量是显式指定（用户钉版本），优先级必须最高；
    本模块装的目录排在 PATH 前面，是因为"刚点按钮装完"的那一份最可能
    就是用户想用的，而 PATH 里那个可能是半年前手工放的旧构建。
    """
    override = os.environ.get("TAGGER_LLAMA_SERVER", "").strip()
    if override:
        p = Path(override)
        return str(p) if p.is_file() else None
    local = _local_binary()
    if local is not None:
        return str(local)
    return shutil.which("llama-server")


def _local_binary() -> Path | None:
    """在我们自己的安装目录里找。zip 解出来可能是 build/bin/ 也可能是平铺，
    所以递归找；目录里只有几百个文件，代价可以忽略。"""
    if not INSTALL_ROOT.is_dir():
        return None
    direct = INSTALL_ROOT / BINARY_NAME
    if direct.is_file():
        return direct
    try:
        for candidate in sorted(INSTALL_ROOT.rglob(BINARY_NAME)):
            if candidate.is_file():
                return candidate
    except OSError:
        return None
    return None


def _read_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


# ------------------------------------------------------------------ 状态

class _Installer:
    """安装是全局唯一的：两个线程同时往同一个目录解压只会互相覆盖。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self.stage = STAGE_IDLE
        self.message = ""
        self.done = 0
        self.total = 0
        self.error = ""
        self.source = ""
        self.started = 0.0

    def busy(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()


_INSTALLER = _Installer()


def status() -> dict:
    """给页面的快照。**不联网、不跑 nvidia-smi**：这个接口会被轮询，
    每次都去摸 GitHub 的话，没网的时候整个面板会跟着卡住。"""
    installer = _INSTALLER
    with installer._lock:
        busy = installer._thread is not None and installer._thread.is_alive()
        stage = installer.stage
        message = installer.message
        done, total = installer.done, installer.total
        error = installer.error
        source = installer.source
    binary = find_binary()
    return {
        "installed": bool(binary),
        "binary": binary or "",
        "version": _read_version(),
        "install_root": str(INSTALL_ROOT),
        "busy": busy,
        "stage": stage,
        "message": message,
        "done": int(done),
        "total": int(total),
        "error": error,
        "source": source,
        # 当前平台有没有现成的安装包可下。Mac 上官方不发二进制，
        # 页面就该显示"自己去编译"，而不是给一个必然失败的按钮。
        "supported": _platform_tag() is not None,
        "platform": platform.system().lower(),
    }


# ------------------------------------------------------------------ 安装

def install(*, url: str = "", force: bool = False) -> dict:
    """开始安装（后台线程）。立刻返回当前状态。

    url 非空 = 直接下这个 zip（离线包 / 镜像 / 公司内网）。留空才去查
    GitHub 的 latest——这样即使 API 被墙，用户仍有一条能走的路。
    """
    installer = _INSTALLER
    reason = ""
    with installer._lock:
        if installer._thread is not None and installer._thread.is_alive():
            reason = "已经在装了"
        elif not force and find_binary():
            reason = "已经装了"
        else:
            installer._cancel.clear()
            installer.stage = STAGE_PROBING
            installer.message = "准备安装包"
            installer.done = installer.total = 0
            installer.error = ""
            installer.source = url.strip()
            installer.started = time.time()
            thread = threading.Thread(target=_run, args=(url.strip(),),
                                      daemon=True, name="llama-install")
            installer._thread = thread
            thread.start()
    # status() 自己也要拿这把锁，所以**只能**在释放之后调。threading.Lock
    # 不可重入，在持锁时调它是把自己锁死——表现是接口一直不返回（点了
    # 按钮页面转圈），而不是抛一个能看见的错。
    return {"started": not reason, "reason": reason, **status()}


def cancel() -> dict:
    """取消安装。解压到一半停下会留下残目录，下次安装先清掉它。"""
    _INSTALLER._cancel.set()
    return status()


def _set(stage: str = "", *, message: str | None = None,
         done: int | None = None, total: int | None = None,
         error: str | None = None, source: str | None = None) -> None:
    installer = _INSTALLER
    with installer._lock:
        if stage:
            installer.stage = stage
        if message is not None:
            installer.message = message
        if done is not None:
            installer.done = done
        if total is not None:
            installer.total = total
        if error is not None:
            installer.error = error
        if source is not None:
            installer.source = source


def _run(url: str) -> None:
    installer = _INSTALLER
    cancel_evt = installer._cancel
    try:
        INSTALL_ROOT.mkdir(parents=True, exist_ok=True)
        if url:
            assets = [{"name": Path(url).name or "llama.cpp.zip",
                       "url": url, "size": 0, "kind": "main"}]
            tag = ""
        else:
            tag, assets = _resolve_release(cancel_evt)
        if not assets:
            raise RuntimeError("没有找到适合当前平台的安装包")

        total_bytes = sum(int(a.get("size") or 0) for a in assets)
        _set(STAGE_DOWNLOADING, message="下载中", done=0, total=total_bytes,
             source=", ".join(a["name"] for a in assets))

        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        zips: list[Path] = []
        # 多个包（CUDA 主包 + cudart）时进度要接着走，不能每换一个文件
        # 就从 0 重新爬——页面上的进度条会来回跳。
        consumed = 0
        for asset in assets:
            if cancel_evt.is_set():
                raise _Cancelled
            dest = DOWNLOAD_DIR / asset["name"]
            _download(asset["url"], dest, cancel_evt,
                      done_offset=consumed, total=total_bytes)
            consumed += int(asset.get("size") or 0)
            zips.append(dest)

        _set(STAGE_EXTRACTING, message="解压中")
        _clean_install_dir()
        for archive in zips:
            if cancel_evt.is_set():
                raise _Cancelled
            _extract(archive, INSTALL_ROOT, cancel_evt)

        _set(STAGE_VERIFYING, message="校验")
        binary = _local_binary()
        if binary is None:
            raise RuntimeError(
                f"装完了，但 {INSTALL_ROOT} 里还是找不到 {BINARY_NAME}。"
                f"包里可能换了目录结构，去该目录看一眼，或改用 "
                f"TAGGER_LLAMA_SERVER 指向它。")
        if tag:
            VERSION_FILE.write_text(tag + "\n", encoding="utf-8")
        for archive in zips:
            archive.unlink(missing_ok=True)
        try:
            DOWNLOAD_DIR.rmdir()
        except OSError:
            pass
        _set(STAGE_DONE, message=f"装好了：{binary}", done=total_bytes,
             total=total_bytes)
    except _Cancelled:
        _set(STAGE_CANCELLED, message="已取消")
    except Exception as exc:                  # noqa: BLE001 - 失败原因要原样上页面
        _set(STAGE_ERROR, error=_human_error(exc),
             message="安装失败")


def _human_error(exc: BaseException) -> str:
    """把网络类异常翻成"下一步该干什么"。原样抛 httpx 的报错，用户只能
    看到一串 ConnectError，不知道是被墙了还是代理没设。"""
    text = f"{type(exc).__name__}: {exc}"
    low = f"{type(exc).__name__} {exc}".lower()
    if any(k in low for k in ("connect", "timeout", "resolve", "network", "ssl")):
        return (f"{text}\n连不上 GitHub。可以：① 设 HTTPS_PROXY 后重试；"
                f"② 手动下 zip 后，用「从本地 zip 安装」或在命令行 "
                f"`tagger llama install --zip <路径>`；"
                f"③ 已经装过的，设 TAGGER_LLAMA_SERVER 指向 llama-server.exe。")
    return text


def _platform_tag() -> str | None:
    """官方 release 里对应本机的资产后缀。没对应值 = 官方不发这个平台的
    二进制（比如 macOS），页面不该给安装按钮。"""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        return "win-x64" if machine in ("amd64", "x86_64", "") else None
    if system == "linux":
        return "ubuntu-x64" if machine in ("x86_64", "amd64") else None
    return None


def _has_nvidia() -> bool:
    try:
        from .hardware import query_gpus

        return bool(query_gpus())
    except Exception:                         # noqa: BLE001 - 探测失败按无卡处理
        return False


def _asset_kind(name: str) -> str:
    low = name.lower()
    if low.startswith("cudart"):
        return "cudart"
    return "main"


def _name_tokens(name: str) -> set[str]:
    """按分隔符切词。**必须按词比较而不是子串**：`x64` 是子串关系的重灾区
    （`x86_64`、`arm64` 都含它），一放宽就会给 ARM 机器挑到 x64 的包。"""
    return {t for t in re.split(r"[-_.\s]+", name.lower()) if t}


def _matches_asset(name: str, flavour: str) -> bool:
    """资产名是否对应"某个平台的某个后端"。

    资产名长这样（2026 年的命名）：
        llama-b12188-bin-win-cuda-13.5-x64.zip
        llama-b12188-bin-win-cpu-x64.zip
        cudart-llama-bin-win-cuda-13.5-x64.zip

    旧实现要求名字里有 `-bin-win-cuda` 这一段连续子串，而官方把架构塞进了
    后端名中间（`-bin-win-cuda-13.5-x64`），于是永远匹配不上、报"没有适合
    当前平台的安装包"。这里改成按词集合判断，版本号怎么变都不影响。
    """
    low = name.lower()
    if not (low.startswith("llama-") and low.endswith(".zip")):
        return False
    toks = _name_tokens(name)
    need = set(flavour.split("-"))
    if not need <= toks:
        return False
    # 架构必须显式说清楚：宁可匹配不到，也不要把 arm64 包装到 x64 机器上。
    if "x64" not in toks or "arm64" in toks:
        return False
    return True


def _matches_cudart(name: str, flavour: str, version: float) -> bool:
    """cudart 包要和主包"同平台、同架构、同 CUDA 版本"。

    踩过：只判 `startswith("cudart")` 加版本号，会挑到
    `cudart-llama-b12189-bin-ubuntu-cuda-13.5-arm64.tar.gz`——名字对得上、
    平台完全不对，解出来是一堆对 Windows 没用的 .so。
    """
    low = name.lower()
    if not (low.startswith("cudart") and low.endswith(".zip")):
        return False
    toks = _name_tokens(name)
    if not set(flavour.split("-")) <= toks:
        return False
    if "x64" not in toks or "arm64" in toks:
        return False
    return _cuda_version(name) == version


def _cuda_version(name: str) -> float:
    m = re.search(r"cuda-(\d+)(?:\.(\d+))?", name.lower())
    if not m:
        return 0.0
    return float(f"{m.group(1)}.{m.group(2) or 0}")


def _max_cuda_major() -> int:
    """按驱动版本估"能到哪个 CUDA 大版本"。取不到就按 12 处理（保守）。

    为什么要估：资产里有 cuda-12.4 和 cuda-13.5 两种，随便挑一个的话老驱动
    会拿到 13.x（起不来），新卡拿到 12.4 又可能缺 kernel。驱动版本与 CUDA
    大版本不是一一对应，这里用公开的对应关系粗判（CUDA 13 起于 580 系驱动）。
    """
    raw = ""
    try:
        import subprocess

        from .hardware import _find_nvidia_smi

        smi = _find_nvidia_smi()
        if smi:
            out = subprocess.run(
                [smi, "--query-gpu=driver_version", "--format=csv,noheader"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=10,
                creationflags=0x08000000 if os.name == "nt" else 0,
            )
            if out.returncode == 0 and out.stdout.strip():
                raw = out.stdout.strip().splitlines()[0]
    except Exception:                         # noqa: BLE001 - 探测失败按保守值
        raw = ""
    m = re.search(r"(\d+)\.(\d+)", raw)
    if not m:
        return 12
    return 13 if int(m.group(1)) >= 580 else 12


def _pick_main(assets: list[dict], flavour: str, max_major: int) -> dict | None:
    """在同一个 release 的资产里挑主包。

    CUDA 多种版本时：挑不超过驱动能力的最高版本；没有兼容包就交给调用方
    尝试 Vulkan / CPU。
    """
    hits = [a for a in assets if _matches_asset(str(a["name"]), flavour)]
    if not hits:
        return None
    if "cuda" in flavour:
        allowed = [a for a in hits if _cuda_version(str(a["name"])) <= max_major + 0.9]
        if not allowed:
            return None
        hits = allowed
    best = sorted(hits, key=lambda a: _cuda_version(str(a["name"])), reverse=True)[0]
    return {"name": str(best["name"]),
            "url": str(best.get("browser_download_url") or ""),
            "size": int(best.get("size") or 0),
            "kind": _asset_kind(str(best["name"]))}


def _resolve_release(cancel_evt: threading.Event) -> tuple[str, list[dict]]:
    """挑出"主包 + 可能的 cudart 包"，返回 (tag, assets)。

    两个坑一起修了：
    1. **latest 可能没有二进制**：版本号发布（v0.9.0）和滚动构建（b12188）是
       两条线，latest 打到前者时资产为空。所以看 releases 列表，跳到第一个
       真有合适资产的发布。
    2. **资产名匹配不能靠连续子串**：`-bin-win-cuda-13.5-x64` 里后端和架构
       是分开的词。见 `_matches_asset`。

    选包顺序：有 N 卡优先 CUDA，其次 Vulkan（AMD / CUDA 装不上时还能加速），
    最后 CPU。宁可慢一点也不要"装完起不来"。
    """
    tag = _platform_tag()
    if tag is None:
        raise RuntimeError(
            f"{platform.system()} 上没有官方预编译包，需要自己编译 llama.cpp，"
            f"然后把 TAGGER_LLAMA_SERVER 指向 llama-server。")
    if cancel_evt.is_set():
        raise _Cancelled

    if tag.startswith("ubuntu"):
        order = ["ubuntu-x64"]
    else:
        order = (["win-cuda"] if _has_nvidia() else []) + ["win-vulkan", "win-cpu"]

    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "tagger-llama-installer"}
    with httpx.Client(trust_env=trust_env_for(RELEASES_API), timeout=_TIMEOUT,
                      follow_redirects=True, headers=headers) as client:
        releases = _list_releases(client, cancel_evt)

    max_major = _max_cuda_major()
    seen_tags: list[str] = []
    for release in releases:
        if cancel_evt.is_set():
            raise _Cancelled
        tag_name = str(release.get("tag_name") or "")
        if tag_name:
            seen_tags.append(tag_name)
        assets = [a for a in (release.get("assets") or []) if a.get("name")]
        if not assets:
            continue                       # v0.9.0 这种纯源码发布，直接跳过
        for flavour in order:
            main = _pick_main(assets, flavour, max_major)
            if main is None:
                continue
            wanted = [main]
            # CUDA 主包要配同版本的 cudart。官方把运行库单独发，缺了它 exe
            # 起不来，而报错是 Windows 的"缺少 xxx.dll"，跟 llama.cpp 一点
            # 关系都看不出来。
            if "cuda" in main["name"].lower():
                ver = _cuda_version(main["name"])
                cudarts = [a for a in assets
                           if _matches_cudart(str(a["name"]), flavour, ver)]
                if cudarts:
                    wanted.append({
                        "name": str(cudarts[0]["name"]),
                        "url": str(cudarts[0].get("browser_download_url") or ""),
                        "size": int(cudarts[0].get("size") or 0),
                        "kind": "cudart",
                    })
            return tag_name, wanted

    raise RuntimeError(
        f"翻了最近 {len(seen_tags)} 个 release（{', '.join(seen_tags[:5])}…），"
        f"都没有匹配 {platform.system()}/{platform.machine()} 的安装包。"
        f"可以手动下 zip 后用「用现成的 zip」安装。")


def _list_releases(client: httpx.Client,
                   cancel_evt: threading.Event) -> list[dict]:
    """releases 列表；接口不可用时退回 latest（单条），保证老路径仍能走。"""
    try:
        resp = client.get(RELEASES_API)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            return data
    except Exception:                         # noqa: BLE001 - 退回 latest
        pass
    if cancel_evt.is_set():
        raise _Cancelled
    resp = client.get(RELEASE_API)
    resp.raise_for_status()
    one = resp.json()
    return [one] if isinstance(one, dict) else []


def _download(url: str, dest: Path, cancel_evt: threading.Event, *,
              done_offset: int = 0, total: int = 0) -> Path:
    """流式下载到 .part 再改名：中途断掉不会留一个"看着像装好了"的半包。

    done_offset/total 是"多包合计"的口径：单个包的 content-length 只是
    它自己，页面上要的是整次安装的进度。
    """
    tmp = dest.with_name(dest.name + ".part")
    tmp.unlink(missing_ok=True)
    with httpx.Client(trust_env=trust_env_for(url), timeout=_TIMEOUT,
                      follow_redirects=True,
                      headers={"User-Agent": "tagger-llama-installer"}) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            size = int(resp.headers.get("content-length") or 0)
            overall = total or (done_offset + size)
            done = 0
            _set(total=overall, done=done_offset)
            with tmp.open("wb") as fh:
                for chunk in resp.iter_bytes(_CHUNK):
                    if cancel_evt.is_set():
                        tmp.unlink(missing_ok=True)
                        raise _Cancelled
                    fh.write(chunk)
                    done += len(chunk)
                    _set(done=done_offset + done,
                         total=overall if overall else done_offset + done)
    tmp.replace(dest)
    return dest


def _clean_install_dir() -> None:
    """重装前清掉旧目录：不同构建的 dll 混在一起，表现是"改了参数没生效"。"""
    if not INSTALL_ROOT.is_dir():
        return
    for child in INSTALL_ROOT.iterdir():
        if child.name == ".download":
            continue
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        except OSError:
            pass


def _extract(archive: Path, dest: Path, cancel_evt: threading.Event) -> None:
    """安全解压。逐个成员校验落点，挡掉 ../ 这种路径穿越——这个包是从网上
    下的，哪怕是官方源，也不该给它写任意路径的权力。"""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            if cancel_evt.is_set():
                raise _Cancelled
            target = (dest / member.filename).resolve()
            if target != root and root not in target.parents:
                continue
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out, _CHUNK)
