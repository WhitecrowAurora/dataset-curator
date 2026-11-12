"""在系统文件管理器里打开一个目录：一份实现，三处调用。

为什么单独拆出来
----------------
「打开文件夹」这个按钮原来在 `prompts.reveal()` 和
`downloads.reveal_dir()` 里各写了一遍 `explorer / open / xdg-open`。三行
代码看起来不值得抽，但它们的**失败方式**才是问题：

  * 服务器上没有桌面（纯 SSH、systemd 起的服务、容器）时，`xdg-open`
    能以 0 退出码启动并立刻失败，或者干脆 command not found；
  * `Popen` 本身成功也不代表窗口开了——用户看到的只是"点了没反应"。

两处各自"静默失败"就是两个查不出来的 bug。所以这里统一成：能开就开，
开不了就把**路径原样回给前端**，由页面显示成可复制的文本。打不开目录不是
错误（远程服务器本来就该用 SSH / 文件传输去看），不能抛异常拦住操作。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _opener_command(target: str) -> list[str] | None:
    """选一个"能把目录弹出来"的命令。找不到就返回 None。

    `shutil.which` 要查，不能直接 Popen：Linux 上没装 `xdg-utils` 极其常见，
    `Popen(["xdg-open", ...])` 会抛 FileNotFoundError；有些实现包装了
    `subprocess.Popen(...)` 后异常被上层当成"打开失败"报红，而这其实只是
    "这台机器没有文件管理器"。
    """
    if sys.platform.startswith("win"):
        # explorer 在 Windows 上一定存在；它对带空格的路径也认。
        return ["explorer", target]
    if sys.platform == "darwin":
        return ["open", target] if shutil.which("open") else None
    # 优先 xdg-open（发行版标准），再退几个常见的兜底。
    for name in ("xdg-open", "gio", "nautilus", "dolphin", "thunar"):
        exe = shutil.which(name)
        if not exe:
            continue
        if name == "gio":
            return [exe, "open", target]
        return [exe, target]
    return None


def open_dir(target: str | Path) -> dict:
    """尝试在文件管理器里打开 target。**绝不抛异常**。

    返回 {"path", "opened", "detail"}：
      * path   —— 规范化后的绝对路径，页面拿它做"复制路径"的兜底；
      * opened —— 真的拉起了一个打开器；
      * detail —— 没打开时给人看的一句话（有打开器就空着）。
    """
    p = Path(target).expanduser()
    try:
        p = p.resolve()
    except OSError:
        p = p.absolute()
    shown = str(p)

    if not p.exists():
        return {"path": shown, "opened": False,
                "detail": "这个目录还不存在"}

    cmd = _opener_command(shown)
    if cmd is None:
        return {"path": shown, "opened": False,
                "detail": "这台机器没有图形文件管理器（无桌面环境），"
                          "照下面这个路径去服务端看"}

    # 不能只信 Popen 不抛异常：服务器上 PATH 里有 xdg-open 但 DISPLAY 没有，
    # 它会自己以非 0 退出。这里等 2 秒看它的退出码——打开器通常几百毫秒就
    # 返回；超时说明它把自己变成了常驻进程（有的发行版是这样），算成功。
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=2.0, check=False,
            start_new_session=os.name != "nt",
        )
    except subprocess.TimeoutExpired:
        return {"path": shown, "opened": True, "detail": ""}
    except (OSError, ValueError):
        return {"path": shown, "opened": False,
                "detail": "拉不起文件管理器，照下面这个路径去看"}
    if proc.returncode != 0:
        return {"path": shown, "opened": False,
                "detail": f"文件管理器返回 {proc.returncode}"
                          f"（多半是没有桌面环境），照下面这个路径去看"}
    return {"path": shown, "opened": True, "detail": ""}
