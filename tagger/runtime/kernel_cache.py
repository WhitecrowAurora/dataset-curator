"""为 native 子进程选择可写、可复用的 Triton 编译缓存。"""
from __future__ import annotations

import errno
import hashlib
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _writable(path):
    path.mkdir(parents=True, exist_ok=True)
    # Triton 会创建哈希子目录、临时目录，再原子替换编译产物。
    # 只看目录存在或 os.access 不能确认 Windows 上这些操作是否可用。
    with tempfile.TemporaryDirectory(prefix='.write-', dir=path) as probe:
        nested = Path(probe) / 'kernel' / 'temporary'
        nested.mkdir(parents=True)
        source = nested / 'compiled.tmp'
        source.write_bytes(b'cache')
        source.replace(nested.parent / 'compiled.bin')


def environment(root=None, environ=None):
    """保留可写的自定义缓存；默认写程序 run/cache，失败时换用户私有目录。"""
    env = dict(os.environ if environ is None else environ)
    root = Path(root or ROOT).resolve()
    candidates = []
    if configured := env.get('TRITON_CACHE_DIR'):
        candidates.append(Path(configured).expanduser())
    candidates.append(root / 'run' / 'cache' / 'triton')
    identity = hashlib.sha256(str(root).encode('utf8')).hexdigest()[:16]
    if local := env.get('LOCALAPPDATA'):
        candidates.append(Path(local) / 'lulynx' / identity / 'cache' / 'triton')
    candidates.append(Path(tempfile.gettempdir()) / 'lulynx' / identity / 'cache' / 'triton')
    seen, failures = set(), []
    for candidate in candidates:
        try:
            path = candidate.resolve()
            if path in seen:
                continue
            seen.add(path)
            _writable(path)
        except OSError as exc:
            if exc.errno == errno.ENOSPC or getattr(exc, 'winerror', None) == 112:
                raise RuntimeError(f'Triton 缓存所在磁盘空间不足：{candidate}。请腾出空间后重新启动。') from exc
            failures.append(f'{candidate}：{exc}')
            continue
        env['TRITON_CACHE_DIR'] = str(path)
        return env
    raise RuntimeError('native 无法写入 Triton 编译缓存，请检查目录权限或安全软件：\n' + '\n'.join(failures))


def configure():
    """在加载 torch/Triton 前初始化当前 native worker 的缓存。"""
    env = environment()
    os.environ['TRITON_CACHE_DIR'] = env['TRITON_CACHE_DIR']
    print(f"[native] Triton 缓存：{env['TRITON_CACHE_DIR']}", flush=True)
