"""Cross-process GPU reservation for development scripts, without CUDA imports."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
import runpy
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path


def _device_uuid(device):
    result = subprocess.run(
        ['nvidia-smi', '-i', str(device), '--query-gpu=uuid', '--format=csv,noheader'],
        check=True, capture_output=True, text=True, timeout=15)
    uuids = result.stdout.strip().splitlines()
    if len(uuids) != 1 or not uuids[0].startswith('GPU-'):
        raise RuntimeError(f'Cannot resolve one physical GPU from {device!r}')
    return uuids[0].strip()


@contextmanager
def gpu_lock(device=0, *, label='GPU development', poll_s=1.0):
    """Hold across the whole GPU case, including any child worker shutdown.

    All participating sessions use the same physical UUID. Windows releases
    the mutex if its owner exits; no stale file or manual unlock is required.
    """
    uuid = _device_uuid(device)
    key = hashlib.sha256(uuid.encode()).hexdigest()[:24]
    acquired = False
    started = time.monotonic()
    next_notice = started
    if os.name == 'nt':
        from ctypes import wintypes

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel.ReleaseMutex.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateMutexW(None, False, 'Global\\lulynx.gpu.' + key)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())

        def attempt():
            status = kernel.WaitForSingleObject(handle, max(1, int(poll_s * 1000)))
            if status in (0, 0x80):  # acquired, or an exited owner abandoned it
                return True
            if status != 0x102:
                raise ctypes.WinError(ctypes.get_last_error())
            return False

        def close():
            if acquired:
                kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)
    else:
        import fcntl

        folder = Path(os.environ.get('XDG_RUNTIME_DIR', '/tmp')) / 'lulynx-gpu'
        folder.mkdir(exist_ok=True)
        handle = (folder / (key + '.lock')).open('a+b')

        def attempt():
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                time.sleep(poll_s)
                return False

        def close():
            handle.close()

    try:
        while not acquired:
            acquired = attempt()
            now = time.monotonic()
            if not acquired and now >= next_notice:
                print(f'[gpu-queue] waiting pid={os.getpid()} gpu={uuid} case={label} '
                      f'elapsed={now - started:.0f}s', flush=True)
                next_notice = now + 30
        print(f'[gpu-queue] acquired pid={os.getpid()} gpu={uuid} case={label} '
              f'wait={time.monotonic() - started:.1f}s', flush=True)
        yield
    finally:
        try:
            torch = sys.modules.get('torch')
            if acquired and torch is not None and torch.cuda.is_initialized():
                import gc

                gc.collect()
                torch.cuda.synchronize()
                torch.cuda.empty_cache()
        finally:
            close()
            if acquired:
                print(f'[gpu-queue] released pid={os.getpid()} gpu={uuid} case={label}', flush=True)


def main():
    parser = argparse.ArgumentParser(description='Wait for a shared GPU reservation, then run a Python script.')
    parser.add_argument('--device', default='0', help='Physical nvidia-smi index or GPU UUID')
    parser.add_argument('script', type=Path)
    parser.add_argument('args', nargs=argparse.REMAINDER)
    options = parser.parse_args()
    script = options.script.resolve(strict=True)
    sys.argv = [str(script), *options.args]
    sys.path.insert(0, str(script.parent))
    with gpu_lock(options.device, label=script.name):
        runpy.run_path(str(script), run_name='__main__')


if __name__ == '__main__':
    main()
