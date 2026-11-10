"""跑 llama.cpp 的 `convert_hf_to_gguf.py`，顺手把 sys.path 补好。

为什么不直接 `python convert_hf_to_gguf.py`
-------------------------------------------
随程序发的那份解释器是 embeddable 版，目录里放着 `python312._pth`。带 `._pth`
的 Python 处于 isolated 模式（`sys.flags.isolated == 1`）、`ignore_environment == 1`、
`safe_path == 1`，两个后果，任何一个都足以让转换一跑就死：

1. **PYTHONPATH 被整个忽略**——环境变量不进 sys.path，父进程里拼得再对也没用；
2. **脚本自己所在的目录也不进 sys.path**——`sys.path[0]` 被 safe_path 拿掉。

于是 `convert_hf_to_gguf.py` 第 18 行的 `from conversion import ...` 找不到同目录
下的 `conversion/` 包，报 `No module named 'conversion'`：看着像少装了 pip 包，
实际是 path 根本没进去。官方脚本只给自己补了 `gguf-py`（第 15 行），没管
`conversion`。

所以 path 只能在子进程里补：先 `sys.path.insert`，再 `runpy` 跑官方脚本。
父进程用参数列表直传（不过 shell），路径带空格/中文都不用转义：

    python -u convert_runner.py <工具目录> <convert_hf_to_gguf.py 的其余参数...>
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


def main(argv: list[str]) -> None:
    if not argv:
        raise SystemExit(
            "用法：convert_runner.py <工具目录> [convert_hf_to_gguf.py 的参数...]")
    tools = Path(argv[0]).resolve()
    script = tools / "convert_hf_to_gguf.py"
    if not script.is_file():
        raise SystemExit(f"找不到转换脚本：{script}")
    # gguf-py 在前：脚本第 15 行只给自己补这一段，conversion 得我们来。
    sys.path[:0] = [str(tools / "gguf-py"), str(tools)]
    # 官方脚本靠 sys.argv[0] 判断自己在哪、argparse 读后面的参数
    sys.argv = [str(script), *argv[1:]]
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main(sys.argv[1:])
