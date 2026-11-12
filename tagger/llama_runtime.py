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
3. **CUDA 包要配 cudart 包**。官方 CUDA 构建不带 CUDA 运行库，只解主包的话
   Windows 缺 dll、Linux 缺 libcudart.so；两个包要解到同一目录，Linux 那份
   还得靠 LD_LIBRARY_PATH 指过去（见 model_loader.start_server）。
   平台差异只有后缀：Windows 发 .zip，Linux 发 .tar.gz。
4. **只在真的点按钮时联网**。`status()` 不查 GitHub（页面每几秒轮询一次），
   否则每次刷新都要等一个可能被墙的请求。
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import tarfile
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
        # 最近一次安装里 --version 的结果 {ok, detail}。空 = 没验过。
        self.smoke: dict = {}

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
        # 装完验活的结果（最近一次 install 里跑的 --version）。没装过 / 没验过
        # 是 None：页面据此区分"文件在"和"真能起"。
        smoke = dict(installer.smoke) or None
    binary = find_binary()
    return {
        "installed": bool(binary),
        "binary": binary or "",
        "version": _read_version(),
        "install_root": str(INSTALL_ROOT),
        "smoke": smoke,
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
        for asset, archive in zip(assets, zips):
            if cancel_evt.is_set():
                raise _Cancelled
            _extract(archive, INSTALL_ROOT, cancel_evt)
        # 补执行位必须排在 _flatten_cudart 前面：那边靠 _local_binary 定位 exe，
        # 0644 的二进制会让它直接返回空，cudart 也就不会被搬。
        _ensure_executable()
        # cudart 包是 `cudart\*.dll` 的目录结构，原样展开会落在
        # runtime\llama.cpp\cudart\ 里，**而 Windows 只在 exe 所在目录（和
        # PATH / System32）找 dll**，结果等于没装：双击 exe 一闪而过，日志里
        # 什么都没有。解完统一挪到 exe 旁边。
        _flatten_cudart()

        # CUDA 主包在、运行库没跟上时（官方发布窗口期真有），退回 CPU 版也
        # 比"装完起不来"强：打标能跑，只是慢。这一步只在验活失败、且本次装
        # 的是 CUDA 包时触发，别的失败（架构不对、驱动太老）不猜。
        _set(STAGE_VERIFYING, message="校验")
        binary = _local_binary()
        if binary is None:
            raise RuntimeError(
                f"装完了，但 {INSTALL_ROOT} 里还是找不到 {BINARY_NAME}。"
                f"包里可能换了目录结构，去该目录看一眼，或改用 "
                f"TAGGER_LLAMA_SERVER 指向它。")
        if tag:
            VERSION_FILE.write_text(tag + "\n", encoding="utf-8")
        # 装完立刻静默跑一次 --version。不跑这一下，"装好了"只是个文件存在
        # 的判断：缺 dll、架构不对、驱动太老都要等到用户半小时后点启动才
        # 暴露，而那时的报错看起来像软件坏了。这里当场把它翻成一句人话。
        ok, detail = smoke_test(binary)
        if not ok and _installed_cuda(assets):
            # 只在"装的是 CUDA 包"时退这一档。别的失败（架构不对、驱动太老）
            # 换 CPU 版也未必救得了，猜错反而掩盖真因。
            _set(message="CUDA 版起不来，改装 CPU 版")
            retry = _fallback_cpu_assets(cancel_evt)
            if retry is not None:
                tag_cpu, cpu_assets = retry
                _set(STAGE_DOWNLOADING, message="下载 CPU 版", done=0,
                     total=sum(int(a.get("size") or 0) for a in cpu_assets))
                got: list[Path] = []
                for asset in cpu_assets:
                    if cancel_evt.is_set():
                        raise _Cancelled
                    dest = DOWNLOAD_DIR / asset["name"]
                    _download(asset["url"], dest, cancel_evt)
                    got.append(dest)
                _set(STAGE_EXTRACTING, message="解压中")
                _clean_install_dir()
                for archive in got:
                    _extract(archive, INSTALL_ROOT, cancel_evt)
                _ensure_executable()
                binary = _local_binary()
                if binary is not None:
                    ok, detail = smoke_test(binary)
                    assets = cpu_assets
                    zips = got
                    if ok:
                        tag = tag_cpu
        with installer._lock:
            installer.smoke = {"ok": ok, "detail": detail}
        if not ok:
            raise RuntimeError(
                f"解压完成，但 {binary} 起不来：{detail}\n"
                f"{diagnose_text()}")
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


def _installed_cuda(assets: list[dict]) -> bool:
    return any("cuda" in str(a.get("name", "")).lower() for a in assets)


def _fallback_cpu_assets(cancel_evt: threading.Event) -> tuple[str, list[dict]] | None:
    """CUDA 版装完起不来时，找同一份 release 里的 CPU 版。

    实测动机：b12292 那次官方没挂 ubuntu 的 cudart，装完 `llama-server`
    起不来（libcudart.so.12 找不到）。用户看到的应该是"已经换成 CPU 版，
    能跑但慢"，而不是一句 dll 报错。**只在装的是 CUDA 包、且验活失败时**才
    走到这里——架构不对、驱动太老这些换 CPU 版也未必好，不在这猜。
    """
    tag = _platform_tag()
    if tag is None:
        return None
    flavour = "ubuntu" if tag.startswith("ubuntu") else "win-cpu"
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "tagger-llama-installer"}
    try:
        with httpx.Client(trust_env=trust_env_for(RELEASES_API), timeout=_TIMEOUT,
                          follow_redirects=True, headers=headers) as client:
            releases = _list_releases(client, cancel_evt)
    except Exception:                      # noqa: BLE001 - 退路拿不到就照原样报错
        return None
    for release in releases:
        assets = [a for a in (release.get("assets") or []) if a.get("name")]
        main = _pick_main(assets, flavour, _max_cuda_major())
        if main is None:
            continue
        return str(release.get("tag_name") or ""), [{**main}]
    return None


def _human_error(exc: BaseException) -> str:
    """把网络类异常翻成"下一步该干什么"。原样抛 httpx 的报错，用户只能
    看到一串 ConnectError，不知道是被墙了还是代理没设。

    ★只有 httpx 的异常才走这段翻译：我们自己抛的 RuntimeError 里可能正好
    含 "connect"（比如 "…… cudart …" 不会，但 "连不上" 会），以前是按
    关键字判的，一条清楚的"缺 cudart"被翻成"连不上 GitHub"。
    """
    text = f"{type(exc).__name__}: {exc}"
    if not isinstance(exc, httpx.HTTPError):
        return text
    low = f"{type(exc).__name__} {exc}".lower()
    if any(k in low for k in ("connect", "timeout", "resolve", "network", "ssl")):
        return (f"{text}\n连不上 GitHub。可以：① 设 HTTPS_PROXY 后重试；"
                f"② 手动下压缩包后，用「从本地压缩包安装」或在命令行 "
                f"`tagger llama install --zip <路径>`；"
                f"③ 已经装过的，设 TAGGER_LLAMA_SERVER 指向 {BINARY_NAME}。")
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


def _is_archive(name: str) -> bool:
    """当前平台的官方包是什么后缀。

    **Windows 发 .zip，Linux 只发 .tar.gz**。2026-11 的 b12292 实测：38 个
    资产里 ubuntu 平台一个 .zip 都没有，主包和 cudart 包全是 .tar.gz
    （`llama-b12292-bin-ubuntu-cuda-13.5-x64.tar.gz`）。以前这里对两个平台
    都要求 .zip，Linux 上于是永远匹配不到 —— 报错是"翻了最近 N 个 release
    都没有匹配的安装包"，而 GitHub 上明明有，`install.sh` 也照样会调到这里。
    """
    low = name.lower()
    if os.name == "nt":
        return low.endswith(".zip")
    return low.endswith((".tar.gz", ".tgz"))


# 后端词。用来把"通用 CPU 包"和"带加速后端的同名包"分开：
# `ubuntu-x64` 是 CPU 构建，而 `ubuntu-cuda-13.5-x64` 的资产名里**包含**
# `ubuntu` 和 `x64` 两个词，按子集匹配两个都会命中。只靠 tokens 子集的话
# Linux 上会把 CUDA 包当成 CPU 包装给没卡的机器（Windows 那边因为 flavour
# 自带 win-cpu 这类后端词，不会撞上）。
_BACKEND_TOKENS = {"cuda", "vulkan", "rocm", "sycl", "openvino", "opencl",
                   "cpu", "metal", "hip", "snapdragon", "zdnn"}


def _name_tokens(name: str) -> set[str]:
    """按分隔符切词。**必须按词比较而不是子串**：`x64` 是子串关系的重灾区
    （`x86_64`、`arm64` 都含它），一放宽就会给 ARM 机器挑到 x64 的包。"""
    return {t for t in re.split(r"[-_.\s]+", name.lower()) if t}


def _matches_asset(name: str, flavour: str) -> bool:
    """资产名是否对应"某个平台的某个后端"。

    资产名长这样（2026 年的命名）：
        llama-b12188-bin-win-cuda-13.5-x64.zip        ← Windows
        cudart-llama-bin-win-cuda-13.5-x64.zip
        llama-b12188-bin-ubuntu-cuda-13.5-x64.tar.gz  ← Linux（只有 .tar.gz）
        cudart-llama-b12188-bin-ubuntu-cuda-13.5-x64.tar.gz

    旧实现要求名字里有 `-bin-win-cuda` 这一段连续子串，而官方把架构塞进了
    后端名中间（`-bin-win-cuda-13.5-x64`），于是永远匹配不上、报"没有适合
    当前平台的安装包"。这里改成按词集合判断，版本号怎么变都不影响。
    """
    low = name.lower()
    if not (low.startswith("llama-") and _is_archive(low)):
        return False
    toks = _name_tokens(name)
    need = set(flavour.split("-"))
    if not need <= toks:
        return False
    # 通用 flavour（`ubuntu-x64` 这种不带后端词的）只能配"也不带后端词"的
    # 资产，否则 cuda / rocm / sycl / openvino 变体都会被子集命中。
    if not (need & _BACKEND_TOKENS) and (toks & _BACKEND_TOKENS):
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
    if not (low.startswith("cudart") and _is_archive(low)):
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

    # Linux 和 Windows 同一套取舍：有 N 卡优先 CUDA，其次 Vulkan，最后 CPU。
    # 官方 ubuntu 资产里这三档都在（`-ubuntu-cuda-13.5-x64` / `-ubuntu-vulkan-x64`
    # / `-ubuntu-x64`），只是后缀全是 .tar.gz。以前这里硬写 ["ubuntu-x64"]，
    # 等于"Linux 只配 CPU 版"，与 Windows 的挑法完全不对称。
    if tag.startswith("ubuntu"):
        order = (["ubuntu-cuda"] if _has_nvidia() else []) + ["ubuntu-vulkan", "ubuntu"]
    else:
        order = (["win-cuda"] if _has_nvidia() else []) + ["win-vulkan", "win-cpu"]

    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "tagger-llama-installer"}
    with httpx.Client(trust_env=trust_env_for(RELEASES_API), timeout=_TIMEOUT,
                      follow_redirects=True, headers=headers) as client:
        releases = _list_releases(client, cancel_evt)

    max_major = _max_cuda_major()
    seen_tags: list[str] = []
    # 唯一没有退路的情况：有钱买不到。所以"缺 cudart"要攒起来，等所有档位
    # 都试完再决定是不是真失败——排在它后面的 Vulkan / CPU 还能用。
    cudart_missing: list[str] = []
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
            # CUDA 主包要配同版本的 cudart。官方把运行库单独发，缺了它
            # 二进制起不来（Windows 报缺 xxx.dll，Linux 报 libcudart.so 找不到），
            # 而这两句报错都跟 llama.cpp 一点关系都看不出来。
            if "cuda" in main["name"].lower():
                ver = _cuda_version(main["name"])
                cudarts = [a for a in assets
                           if _matches_cudart(str(a["name"]), flavour, ver)]
                if not cudarts:
                    # **不要因为缺 cudart 就整体报错**。2026-11-11 实测：
                    # b12292 挂了 ubuntu 的 cuda 主包，却没挂同 tag 的
                    # cudart-llama-b12292-bin-ubuntu-cuda-13.5-x64.tar.gz（窗口
                    # 期，70 分钟里连推两个构建）。原实现在这里直接 raise，
                    # 结果 `install.sh` 那段必然失败、连 Vulkan / CPU 的退路
                    # 都走不到——"缺一个可选组件"变成了"这个平台装不了"。
                    #
                    # 但也不能静默跳过：CUDA 主包缺运行库时一启动就退，日志里
                    # 只有一句看不出原因的报错。所以**记下来往下找下一个档位**：
                    # 能装 Vulkan / CPU 就先能用，实在没有才把这条原因抛出去。
                    cudart_missing.append(
                        f"{tag_name} 的 CUDA 主包 {main['name']} 没有配套的 "
                        f"cudart 运行库（cudart-…-{flavour}-{ver}-x64）")
                    continue
                wanted.append({
                    "name": str(cudarts[0]["name"]),
                    "url": str(cudarts[0].get("browser_download_url") or ""),
                    "size": int(cudarts[0].get("size") or 0),
                    "kind": "cudart",
                })
            return tag_name, wanted

    if cudart_missing:
        # 主包有、运行库没有。这是官方发布窗口期的常见状态，用户什么错都没犯，
        # 所以要说清"可以换哪个档"，而不是丢一句"没有安装包"。
        raise RuntimeError(
            "; ".join(cudart_missing[:3])
            + f"。\n这是官方这个 release 漏挂了运行库。可以：① 等几分钟重试"
              f"（滚动构建很快会补上）；② 改用 CPU / Vulkan 版——网页上"
              f"「下载并安装 llama.cpp」在没装到 CUDA 时会自动往下退一档；"
              f"③ 手动下主包和任一旧 release 的 cudart 包，用"
              f"「从本地压缩包安装」。")
    raise RuntimeError(
        f"翻了最近 {len(seen_tags)} 个 release（{', '.join(seen_tags[:5])}…），"
        f"都没有匹配 {platform.system()}/{platform.machine()} 的安装包。"
        f"可以手动下对应平台的压缩包（Windows 是 .zip，Linux 是 .tar.gz），"
        f"再命令行 `tagger llama install --zip <路径>` 装上。")


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


def _flatten_cudart() -> list[str]:
    """把 cudart 目录里的运行库挪到 llama-server 旁边。

    官方 cudart 包解出来是 `cudart/cudart64_XX.dll`（Windows）或
    `cudart/lib/libcudart.so.12`（Linux）这类子目录结构，主包则是平铺的。
    Windows 的 dll 搜索路径**只有** exe 目录、System32、PATH，不认"exe 的
    兄弟目录"，所以原样放着等于没装。

    Linux 的 .so 更麻烦：动态链接器不看 exe 所在目录（除非 RPATH 是 $ORIGIN），
    所以搬过去还不够，`start_server` 必须把 exe 目录塞进子进程的
    LD_LIBRARY_PATH。两处成对改，只做一边等于没装，而且报错是
    "error while loading shared libraries: libcudart.so.12"，看不出来是这一步。
    返回挪了哪些文件名，方便报错时说清。
    """
    binary = _local_binary()
    if binary is None:
        return []
    target_dir = binary.parent
    if os.name == "nt":
        subdirs = (INSTALL_ROOT / "cudart", INSTALL_ROOT / "cudart" / "bin")
        patterns = ("*.dll",)
    else:
        subdirs = (INSTALL_ROOT / "cudart", INSTALL_ROOT / "cudart" / "lib",
                   INSTALL_ROOT / "cudart" / "lib64")
        patterns = ("*.so", "*.so.*")
    moved: list[str] = []
    for src_dir in subdirs:
        if not src_dir.is_dir():
            continue
        for pattern in patterns:
            for lib in src_dir.glob(pattern):
                # is_file() 跟随软链：官方包里的 libcudart.so 常常是指向
                # libcudart.so.12 的链，断链就别搬（搬过去也是坏的）。
                if not lib.is_file():
                    continue
                try:
                    dst = target_dir / lib.name
                    if dst.resolve() == lib.resolve():
                        continue
                    shutil.copy2(lib, dst)     # copy2 跟随软链，落成实体文件
                    moved.append(lib.name)
                except OSError:
                    pass
    return moved


def server_env(binary: str | Path,
               base: dict[str, str] | None = None) -> dict[str, str]:
    """llama-server 子进程的环境副本（不动 os.environ）。

    Linux 动态链接器不看 exe 所在目录（除非 RPATH 是 $ORIGIN），而
    `_flatten_cudart()` 把 cudart 包里的 .so 平铺到了 exe 旁边。不注入
    LD_LIBRARY_PATH，装完那次 --version 就会先报
    "error while loading shared libraries: libcudart.so.13"。
    验活与起服务必须走同一份口径，只做一边等于没装。
    """
    env = dict(os.environ if base is None else base)
    if os.name == "nt":
        return env
    lib_dir = str(Path(binary).resolve().parent)
    prev = env.get("LD_LIBRARY_PATH", "").strip()
    env["LD_LIBRARY_PATH"] = f"{lib_dir}:{prev}" if prev else lib_dir
    return env


def smoke_test(binary: str | Path, *, timeout_s: float = 20.0) -> tuple[bool, str]:
    """静默跑一次 `--version`，确认这个 exe 真的能起来。

    起不来的三种典型，报错长得一模一样（0.2 秒退出、日志空），但修法完全不同：
    - 缺 dll（WinError 2 / 0xc0000135 / "找不到指定模块"）
    - 架构不对（0xc000007b）
    - 驱动太老（CUDA 版起不来，但 CPU 版能起）
    所以这里返回的是"能不能起 + 一句尽量具体的原文"，不做判断——判断交给
    `diagnose_text()` 与用户/AI。
    """
    exe = str(binary)
    try:
        proc = subprocess.run([exe, "--version"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout_s,
                              env=server_env(exe),
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        # --version 20 秒都不回：进程起来了但卡住（显卡初始化卡死也可能）。
        # 不算失败——真正要用的是服务，不是这一条命令。
        return True, "（--version 超时，跳过验活）"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode == 0:
        return True, ""
    text = ((proc.stderr or "") + (proc.stdout or "")).strip()
    code = proc.returncode
    # Windows 的 0xC0000135 = STATUS_DLL_NOT_FOUND，0xC000007B = 架构不符。
    # Python 的 returncode 是有符号的，得按无符号看。
    unsigned = code & 0xFFFFFFFF
    hints = {
        0xC0000135: "缺 dll（STATUS_DLL_NOT_FOUND）：CUDA 版要配同版本的 cudart",
        0xC000007B: "架构不对（STATUS_INVALID_IMAGE_FORMAT）：下成了别的 CPU 架构的包",
        0xC000001D: "指令集不支持（STATUS_ILLEGAL_INSTRUCTION）",
    }
    hint = hints.get(unsigned, "")
    one_line = " / ".join(ln.strip() for ln in text.splitlines()[:3] if ln.strip())
    detail = f"退出码 {code}" + (f"（{hint}）" if hint else "")
    if one_line:
        detail += f"：{one_line}"
    return False, detail


def diagnose_text() -> str:
    """一段可以直接贴进 AI 的排错信息。

    为什么值得专门写：上面所有修法都只是"尽量让它自己跑起来"。官方资产改名、
    驱动版本组合、杀软删 dll 这些没法穷举，一旦没跑起来，用户手里应当有足够
    的信息让 AI 一次给出可行步骤——否则就是一封"放 C 盘还是不行"的邮件。
    """
    binary = find_binary() or ""
    version = _read_version() or "未知"
    lines = [
        "【tagger llama.cpp 排错信息】",
        f"系统: {platform.system()} {platform.release()} ({platform.machine()})",
        f"Python: {platform.python_version()}",
        f"安装目录: {INSTALL_ROOT}",
        f"找到的 exe: {binary or '（没找到）'}",
        f"构建版本: {version}",
    ]
    if binary:
        ok, detail = smoke_test(binary)
        lines.append(f"--version: {'能起' if ok else '起不来'} {detail}".rstrip())
        exe_dir = Path(binary).parent
        try:
            # dll 与 .so 都列：Linux 的 cudart 是 libcudart.so.13 这类文件，
            # 只 glob *.dll 的话排错报告在 Linux 上会永远显示"无"。
            found = list(exe_dir.glob("*.dll")) + list(exe_dir.glob("*.so*"))
            libs = sorted({p.name for p in found})[:40]
            lines.append(f"exe 同目录运行库({len(libs)}): {', '.join(libs) or '无'}")
        except OSError:
            pass
    try:
        from .hardware import query_gpus

        gpus = query_gpus()
        lines.append(f"显卡: {gpus[0].name if gpus else '（没检测到 NVIDIA 显卡）'}")
        lines.append(f"驱动支持的 CUDA: {_driver_cuda_hint()}")
    except Exception:                          # noqa: BLE001 - 探测失败不该让报告生不出来
        lines.append("显卡: （探测失败）")
    lines.append("请据此判断：是缺运行库（dll/.so）、架构不符，还是该换 CPU/Vulkan 版。")
    return "\n".join(lines)


def _driver_cuda_hint() -> str:
    """驱动版本 / 计算能力，只给个粗数。

    口径与 `web/deps.py` 的 torch 分档一致（那里有 580→CUDA 13、535→12.x
    的分界），但这里不 import 它：deps 是 web 层，运行时不该反向依赖 UI。
    """
    try:
        from .hardware import query_gpus

        gpu = query_gpus()[0]
    except Exception:                          # noqa: BLE001
        return "未知"
    cc = getattr(gpu, "compute_cap", None)
    cc_text = f"{cc[0]}.{cc[1]}" if cc else "未知"
    drv = _nvidia_driver_version()
    if not drv:
        return f"计算能力 {cc_text}；驱动版本没读到"
    m = re.match(r"(\d+)\.", drv)
    major = int(m.group(1)) if m else 0
    if major >= 610:
        tier = "13.3"
    elif major >= 580:
        tier = "13.x"
    elif major >= 535:
        tier = "12.x"
    else:
        tier = "可能低于 12.x，建议用 CPU/Vulkan 版"
    return f"{tier}（驱动 {drv}，计算能力 {cc_text}）"


def _nvidia_driver_version() -> str:
    try:
        from .hardware import _find_nvidia_smi

        smi = _find_nvidia_smi()
        if not smi:
            return ""
        proc = subprocess.run(
            [smi, "--query-gpu=driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or "").strip().splitlines()[0].strip() if proc.stdout.strip() else ""


def _extract(archive: Path, dest: Path, cancel_evt: threading.Event) -> None:
    """安全解压。逐个成员校验落点，挡掉 ../ 这种路径穿越——这个包是从网上
    下的，哪怕是官方源，也不该给它写任意路径的权力。

    **Linux 官方包是 .tar.gz、Windows 是 .zip**，两种都要能解；权限位也必须
    一起恢复：tar 里 llama-server 是 0755，用 `open("wb")` 写出来的文件是
    0644，装完一执行就 Permission denied，看着像"包坏了"。
    """
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    if archive.name.lower().endswith((".tar.gz", ".tgz")):
        _extract_tar(archive, dest, root, cancel_evt)
    else:
        _extract_zip(archive, dest, root, cancel_evt)


def _ensure_executable() -> int:
    """解压后兜底补执行位。tar 里的 0755 由 `_apply_mode` 恢复，但 Windows
    工具打出来的 zip 的 external_attr 是 0，解出来是 0644 —— 文件明明在，
    `--version` 却 Permission denied。只补 INSTALL_ROOT 里的 BINARY_NAME，
    不碰其它文件，也不碰用户用 TAGGER_LLAMA_SERVER 指定的那份。
    """
    if os.name == "nt" or not INSTALL_ROOT.is_dir():
        return 0
    try:
        paths = [INSTALL_ROOT / BINARY_NAME,
                 *sorted(INSTALL_ROOT.rglob(BINARY_NAME))]
    except OSError:
        return 0
    fixed = 0
    for path in dict.fromkeys(paths):
        if not path.is_file():
            continue
        try:
            if not os.access(path, os.X_OK):
                os.chmod(path, 0o755)
                fixed += 1
        except OSError:
            pass
    return fixed


def _in_root(target: Path, root: Path) -> bool:
    return target == root or root in target.parents


def _apply_mode(path: Path, mode: int) -> None:
    """恢复权限位。Windows 上 chmod 基本是空操作，直接跳过。"""
    if os.name == "nt" or not mode:
        return
    try:
        os.chmod(path, (mode & 0o777) or 0o644)
    except OSError:
        pass


def _extract_zip(archive: Path, dest: Path, root: Path,
                 cancel_evt: threading.Event) -> None:
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            if cancel_evt.is_set():
                raise _Cancelled
            target = (dest / member.filename).resolve()
            if not _in_root(target, root):
                continue
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out, _CHUNK)
            # Unix 权限在 external_attr 的高 16 位；Windows 打的包这里是 0，
            # _apply_mode 会自己跳过。
            _apply_mode(target, member.external_attr >> 16)


def _extract_tar(archive: Path, dest: Path, root: Path,
                 cancel_evt: threading.Event) -> None:
    with tarfile.open(archive, "r:gz") as tf:
        for member in tf:
            if cancel_evt.is_set():
                raise _Cancelled
            target = (dest / member.name).resolve()
            if not _in_root(target, root):
                continue
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                _apply_mode(target, member.mode | 0o700)
                continue
            if member.issym() or member.islnk():
                _safe_link(member, dest, root, target)
                continue
            if not member.isfile():
                continue                   # 设备节点 / fifo：不需要也不该建
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(member)
            if src is None:
                continue
            with src, target.open("wb") as out:
                shutil.copyfileobj(src, out, _CHUNK)
            _apply_mode(target, member.mode)


def _safe_link(member: tarfile.TarInfo, dest: Path, root: Path,
               target: Path) -> None:
    """官方包里有 `libllama.so -> libllama.so.0` 这种链。只建指向解压目录
    内部的链接，指向外面（`../../../etc/passwd`）的一律跳过。

    失败就算了：Windows 上建软链要管理员权限。真有二进制依赖这条链，
    后面的 smoke_test 会当场把它翻成一句人话。
    """
    name = member.linkname
    if not name:
        return
    # hardlink 的 linkname 相对包根，symlink 相对链接所在目录。
    base = dest if member.islnk() else target.parent
    if not _in_root((base / name).resolve(), root):
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.unlink(missing_ok=True)
        os.symlink(name, target)
    except (OSError, NotImplementedError):
        pass
