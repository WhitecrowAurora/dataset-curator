"""按既有路径排序逐目录枚举，避免少量预览也展开整个数据集。"""
from __future__ import annotations

import os
from pathlib import Path


class InputScanStopped(Exception):
    """输入扫描或规划收到停止请求，不能返回半份计划。"""


def check_stopped(should_stop=None):
    if should_stop is not None and should_stop():
        raise InputScanStopped("输入扫描已停止")


def sorted_files(root: Path, *, recursive=True, should_stop=None):
    check_stopped(should_stop)
    # Path 的排序按路径分量比较；目录排到哪里，就在那里展开它的后代。
    # 保留 Windows 不区分大小写的既有排序，不跟随目录符号链接。
    with os.scandir(root) as scan:
        entries = []
        for entry in scan:
            check_stopped(should_stop)
            entries.append(entry)
    check_stopped(should_stop)
    entries.sort(key=lambda entry: os.path.normcase(entry.name))
    for entry in entries:
        check_stopped(should_stop)
        if entry.is_file():
            yield entry
        elif recursive and entry.is_dir(follow_symlinks=False):
            yield from sorted_files(Path(entry.path), recursive=True, should_stop=should_stop)
