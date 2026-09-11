"""Small process helpers shared by the server and the pet (Windows-first)."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys

STATE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "TokenMonitor")
PET_PID_FILE = os.path.join(STATE_DIR, "pet.pid")
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_DETACHED = 0x00000008 | 0x00000200        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def pythonw():
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        cand = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(cand):
            return cand
    return exe


def spawn_detached(module):
    """Start `python -m <module>` with no console, outliving the caller."""
    return subprocess.Popen([pythonw(), "-m", module], cwd=PROJECT_DIR,
                            creationflags=_DETACHED if sys.platform == "win32" else 0,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid


if sys.platform == "win32":
    # Declare signatures: without them ctypes truncates 64-bit HANDLEs to int,
    # GetExitCodeProcess then fails and every process looks dead.
    _k32 = ctypes.windll.kernel32
    _k32.OpenProcess.restype = ctypes.c_void_p
    _k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    _k32.GetExitCodeProcess.restype = ctypes.c_int
    _k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    _k32.TerminateProcess.restype = ctypes.c_int
    _k32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _k32.CloseHandle.restype = ctypes.c_int
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]


def is_alive(pid):
    if not pid:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid))
    if not h:
        return False
    try:
        code = ctypes.c_uint32()
        ok = _k32.GetExitCodeProcess(h, ctypes.byref(code))
        return bool(ok) and code.value == STILL_ACTIVE
    finally:
        _k32.CloseHandle(h)


def terminate(pid):
    if sys.platform == "win32":
        PROCESS_TERMINATE = 0x0001
        h = _k32.OpenProcess(PROCESS_TERMINATE, 0, int(pid))
        if not h:
            return False
        try:
            return bool(_k32.TerminateProcess(h, 0))
        finally:
            _k32.CloseHandle(h)
    try:
        os.kill(pid, 15)
        return True
    except OSError:
        return False


def read_pid(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def write_pid(path, pid):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(str(pid))


def clear_pid(path, only_if=None):
    """Remove the pid file, optionally only when it still names `only_if`."""
    try:
        if only_if is None or read_pid(path) == only_if:
            os.remove(path)
    except OSError:
        pass


def pet_status():
    pid = read_pid(PET_PID_FILE)
    alive = is_alive(pid)
    if pid and not alive:
        clear_pid(PET_PID_FILE)
    return {"running": alive, "pid": pid if alive else None}


def pet_start():
    st = pet_status()
    if st["running"]:
        return st
    pid = spawn_detached("tokmon.pet")
    return {"running": True, "pid": pid, "started": True}


def pet_stop():
    st = pet_status()
    if not st["running"]:
        return {"running": False, "pid": None}
    terminate(st["pid"])
    clear_pid(PET_PID_FILE)
    return {"running": False, "pid": None, "stopped": st["pid"]}
