"""Windows 子进程树随宿主退出回收，单个 worker 崩溃也不留下孙进程。"""
from __future__ import annotations

import os
import subprocess
import threading


_lock = threading.Lock()
_host_job = None


if os.name == "nt":
    import ctypes
    from ctypes import wintypes as W

    class _Limits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                    ("flags", W.DWORD), ("min_ws", ctypes.c_size_t),
                    ("max_ws", ctypes.c_size_t), ("active", W.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", W.DWORD),
                    ("scheduling", W.DWORD)]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", _Limits), ("io", ctypes.c_ulonglong * 6),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

    class _ThreadEntry(ctypes.Structure):
        _fields_ = [("size", W.DWORD), ("usage", W.DWORD), ("thread_id", W.DWORD),
                    ("owner_pid", W.DWORD), ("base_priority", W.LONG),
                    ("delta_priority", W.LONG), ("flags", W.DWORD)]

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    for _name, _args, _result in (
        ("CreateJobObjectW", [W.LPVOID, W.LPCWSTR], W.HANDLE),
        ("SetInformationJobObject", [W.HANDLE, ctypes.c_int, W.LPVOID, W.DWORD], W.BOOL),
        ("AssignProcessToJobObject", [W.HANDLE, W.HANDLE], W.BOOL),
        ("GetCurrentProcess", [], W.HANDLE),
        ("CloseHandle", [W.HANDLE], W.BOOL),
        ("CreateToolhelp32Snapshot", [W.DWORD, W.DWORD], W.HANDLE),
        ("Thread32First", [W.HANDLE, ctypes.POINTER(_ThreadEntry)], W.BOOL),
        ("Thread32Next", [W.HANDLE, ctypes.POINTER(_ThreadEntry)], W.BOOL),
        ("OpenThread", [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
        ("ResumeThread", [W.HANDLE], W.DWORD),
    ):
        _fn = getattr(_kernel, _name)
        _fn.argtypes, _fn.restype = _args, _result


def _job():
    handle = _kernel.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = _ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not _kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        error = ctypes.get_last_error()
        _kernel.CloseHandle(handle)
        raise ctypes.WinError(error)
    return handle


def _own_host():
    global _host_job
    with _lock:
        if _host_job is None:
            job = _job()
            if not _kernel.AssignProcessToJobObject(job, _kernel.GetCurrentProcess()):
                error = ctypes.get_last_error()
                _kernel.CloseHandle(job)
                raise ctypes.WinError(error)
            # 不传给子进程，也不在 atexit 提前关闭；宿主退出时由 OS 关闭。
            # 先绑定宿主，新进程在出生时就继承所属 Job，覆盖 spawn/assign 窗口。
            _host_job = job


def _resume(pid):
    snapshot = _kernel.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = _ThreadEntry(size=ctypes.sizeof(_ThreadEntry))
        found = _kernel.Thread32First(snapshot, ctypes.byref(entry))
        while found:
            if entry.owner_pid == pid:
                handle = _kernel.OpenThread(2, False, entry.thread_id)  # THREAD_SUSPEND_RESUME
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    if _kernel.ResumeThread(handle) == 0xFFFFFFFF:
                        raise ctypes.WinError(ctypes.get_last_error())
                    return
                finally:
                    _kernel.CloseHandle(handle)
            found = _kernel.Thread32Next(snapshot, ctypes.byref(entry))
        raise OSError(f"无法恢复子进程 {pid} 的主线程")
    finally:
        _kernel.CloseHandle(snapshot)


def spawn_owned(*args, **kwargs):
    """保留 Popen 契约；Windows 在执行代码前建立进程树所有权。"""
    if os.name != "nt":
        return subprocess.Popen(*args, **kwargs)
    _own_host()
    job = _job()
    proc = None
    try:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | 4  # CREATE_SUSPENDED
        proc = subprocess.Popen(*args, **kwargs)
        if not _kernel.AssignProcessToJobObject(job, W.HANDLE(int(proc._handle))):
            raise ctypes.WinError(ctypes.get_last_error())
        _resume(proc.pid)
    except BaseException:
        if proc is not None:
            proc.kill()
            proc.wait()
        _kernel.CloseHandle(job)
        raise

    def reap():
        try:
            proc.wait()
        finally:
            # worker 本身退出时，关闭它的 Job 会收掉仍持有 stdout 的打标孙进程。
            _kernel.CloseHandle(job)

    threading.Thread(target=reap, name="process-owner", daemon=True).start()
    return proc
