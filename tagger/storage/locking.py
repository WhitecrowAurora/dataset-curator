"""本机跨进程锁；进程退出时由操作系统释放，不依赖超时猜测。"""
from __future__ import annotations

import os
import errno
import time
from pathlib import Path


class FileLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.file = None

    def acquire(self, *, blocking: bool = True) -> bool:
        if self.file is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = self.path.open("a+b")
        if f.seek(0, os.SEEK_END) == 0:
            f.write(b"\0")
            f.flush()
        while True:
            try:
                f.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.file = f
                return True
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    f.close()
                    raise
                if not blocking:
                    f.close()
                    return False
                time.sleep(0.05)

    def close(self) -> None:
        if self.file is not None:
            self.file.close()
            self.file = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.close()
