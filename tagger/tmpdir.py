"""临时产物的唯一落点：仓库根的 `tmp/`。

为什么不直接用 `tempfile`
-------------------------
`tempfile` 默认落在 `%LOCALAPPDATA%\\Temp`（Windows）/ `/tmp`（Linux），也就是
用户主目录或系统盘。这个项目被装进别人机器的场景里，有几个前提不成立：

- 有些部署（沙箱、只读 C 盘、受控工作站）**不允许往程序目录之外写**，于是
  `mkdtemp()` 直接抛 PermissionError，而报错看起来像"功能坏了"；
- 成员机的系统盘普遍比数据盘小，解一个 30M 的压缩包、转一份 GGUF、建一个
  sqlite 索引都可能把 C 盘塞到红；
- 出问题要人肉去翻 `%TEMP%` 里几个随机名字的目录，谁都找不到。

所以：**程序自己的临时产物一律写 `ROOT/tmp/`**，跟随程序目录走。子进程（torch /
HF / llama.cpp）看的是环境变量，由 `apply_env()` 把 TEMP/TMP/TMPDIR 指过来，
一起落地。

约定
----
- 只在**临时**产物上用它；产物要留在原地的是 `work/`、`run/`、`out/`。
- 目录随用随建（`ensure()`），不要求安装时先建好——打包分发时 `tmp/`
  不进包，运行时第一次用才创建。
- 清理不自动做：半路崩溃留下的目录是排查线索。`tagger clean` 负责删。
"""

from __future__ import annotations

import os
from pathlib import Path

# 仓库根：tagger/tmpdir.py → 上一级
ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = ROOT / "tmp"


def ensure() -> Path:
    """返回 `tmp/` 并保证它存在。建不出来时抛出 OSError（由调用方连带上下文报）。"""
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    return TMP_ROOT


def path(*parts: str) -> Path:
    """`tmp/` 下的一个具体位置（不创建）。"""
    return TMP_ROOT.joinpath(*parts)


def apply_env() -> Path:
    """把 TEMP/TMP/TMPDIR 指到 `tmp/`，让**子进程**也一起落地。

    为什么要改环境变量而不是只传 dir=：torch 的 kernel 缓存、HF 的下载缓存、
    llama.cpp 自己的临时文件都不是我们能传参的，它们只认这几个变量。改完
    之后整个进程树都写在程序目录里。

    只在本进程内生效（os.environ 是进程私有的），不会污染系统环境变量。
    """
    root = ensure()
    value = str(root)
    for name in ("TMPDIR", "TEMP", "TMP"):
        os.environ[name] = value
    return root
