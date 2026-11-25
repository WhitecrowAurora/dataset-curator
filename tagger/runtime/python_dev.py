"""补齐便携 Python 的编译头文件与导入库，使用随包的官方文件。"""
from __future__ import annotations

import hashlib
import os
import struct
import sys
import zipfile
from pathlib import Path

from ..storage.atomic import write_bytes

ROOT = Path(__file__).resolve().parents[2]
BUNDLES = {(3, 12): ('python-3.12.9-dev-amd64.zip', '44012b94a23186c7637f5fb4267195e95baff9d640f0fd93a9ba92bc08b32d3b')}


def missing(prefix=None):
    base = Path(prefix or sys.base_prefix)
    if os.name != 'nt':
        return []
    library = f'python{sys.version_info.major}{sys.version_info.minor}.lib'
    return [str(base / name) for name in ('Include/Python.h', 'Include/pyconfig.h', 'libs/' + library)
            if not (base / name).is_file()]


def ensure(prefix=None):
    """已完整的安装不改动；缺失时只补文件，不覆盖解释器或依赖。"""
    base = Path(prefix or sys.base_prefix).resolve()
    needed = missing(base)
    if not needed:
        return []
    bundle = BUNDLES.get(tuple(sys.version_info[:2]))
    if not bundle or struct.calcsize('P') != 8:
        raise RuntimeError('native 缺少 Python 开发文件：' + ', '.join(needed) + '。请安装与此 Python 版本和架构一致的 Include 与 libs。')
    archive = ROOT / 'env' / 'initialization' / bundle[0]
    if not archive.is_file():
        raise RuntimeError('native 缺少 Python.h 或 Python 开发库。请完整解压新版程序（保留 env/initialization），再启动或重跑 install.bat。')
    if hashlib.sha256(archive.read_bytes()).hexdigest() != bundle[1]:
        raise RuntimeError('Python 开发文件包不完整，请重新解压新版 env/initialization/' + bundle[0])
    installed = []
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            if member.is_dir():
                continue
            target = (base / member.filename).resolve()
            if target.parts[:len(base.parts)] != base.parts or member.filename.split('/')[0] not in ('Include', 'libs'):
                raise RuntimeError('Python 开发文件包路径不正确')
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            write_bytes(target, source.read(member))
            installed.append(member.filename)
    remaining = missing(base)
    if remaining:
        raise RuntimeError('Python 开发文件补齐失败：' + ', '.join(remaining))
    return installed
