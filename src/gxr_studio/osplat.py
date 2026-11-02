"""平台层：POSIX（macOS、Linux）和 Windows 的差别都收在这里。

- 文件锁：POSIX 用 `fcntl.flock`，Windows 用 `msvcrt.locking` 锁文件第一个字节。两者都是进程级的独占锁，
  进程退出（正常、被杀、崩溃）系统会自己放掉；同一把锁第二次非阻塞获取会立刻失败（抛 OSError）。
- fd → 真实路径：macOS 用 `F_GETPATH`，Linux 读 `/proc/self/fd`，Windows 用 `GetFinalPathNameByHandleW`。
- 完整落盘：macOS 用 `F_FULLFSYNC`，其他用 `os.fsync`（Windows 上就是 FlushFileBuffers）。
- 父进程还在不在：POSIX 看 `os.getppid()` 有没有变（父进程没了会被过继）；Windows 不过继，`getppid` 一直是原来的号，
  改为启动时拿住父进程句柄、看它有没有结束（句柄拿住了，进程号被复用也不会认错）。
- 目录 fd：POSIX 按目录 fd 逐级打开、不跟随链接（`dir_fd`、`O_NOFOLLOW`）；Windows 没有这些，改用按路径的实现，
  逐级 lstat 拒绝链接和重解析点（`USE_PATH_IO`）。设环境变量 `GXR_FORCE_PATH_IO=1` 可以在 POSIX 上也走按路径的实现，
  测试用它在 macOS/Linux 上跑 Windows 那条路的逻辑。
"""

from __future__ import annotations

import os
import stat
import sys
from typing import Any, Callable

WINDOWS = sys.platform == "win32"
USE_PATH_IO = WINDOWS or os.environ.get("GXR_FORCE_PATH_IO") == "1"
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

if sys.platform == "win32":  # pragma: no cover - 只在 Windows 上执行（CI 的 windows-latest）；mypy 按平台跳过这一段
    import msvcrt

    HOST_IS_DARWIN = False

    def lock_exclusive_nb(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

    def fd_path(fd: int) -> str | None:
        import ctypes
        from ctypes import wintypes

        try:
            handle = msvcrt.get_osfhandle(fd)
        except OSError:
            return None
        fn = ctypes.windll.kernel32.GetFinalPathNameByHandleW
        fn.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        fn.restype = wintypes.DWORD
        buf = ctypes.create_unicode_buffer(1024)
        n = fn(handle, buf, 1024, 0)
        if n == 0 or n >= 1024:
            return None
        path = buf.value
        return path[4:] if path.startswith("\\\\?\\") else path

    def full_fsync(fd: int) -> None:
        os.fsync(fd)

    def parent_alive(parent_pid: int) -> Callable[[], bool]:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        handle = kernel32.OpenProcess(0x00100000, False, parent_pid)  # SYNCHRONIZE
        if not handle:
            return lambda: False  # 已经没了（或拿不到）：按父进程不在处理
        return lambda: bool(kernel32.WaitForSingleObject(handle, 0) == 0x102)  # WAIT_TIMEOUT = 还在跑

else:
    import fcntl

    # 真实宿主是不是 macOS：只在导入时定一次，按内核报告的系统名（Windows 没有 os.uname）
    HOST_IS_DARWIN = os.uname().sysname == "Darwin"
    _F_GETPATH = getattr(fcntl, "F_GETPATH", None)
    _F_FULLFSYNC = getattr(fcntl, "F_FULLFSYNC", None)

    def lock_exclusive_nb(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

    def fd_path(fd: int) -> str | None:
        """fd 现在的真实路径（macOS 用 F_GETPATH，Linux 读 /proc/self/fd）。拿不到返回 None。"""
        if _F_GETPATH is not None:
            try:
                raw = fcntl.fcntl(fd, _F_GETPATH, b"\0" * 1024)
            except OSError:
                return None
            return os.fsdecode(raw.split(b"\0", 1)[0])
        try:
            return os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            return None

    def full_fsync(fd: int) -> None:
        if _F_FULLFSYNC is not None and HOST_IS_DARWIN:
            try:
                fcntl.fcntl(fd, _F_FULLFSYNC)
                return
            except OSError:
                pass
        os.fsync(fd)

    def parent_alive(parent_pid: int) -> Callable[[], bool]:
        return lambda: os.getppid() == parent_pid


def is_link(st: os.stat_result) -> bool:
    """lstat 的结果是不是链接：符号链接，或 Windows 的重解析点（目录联接等）。"""
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & REPARSE_POINT)


def binary_flag() -> int:
    return int(getattr(os, "O_BINARY", 0))


def describe() -> dict[str, Any]:
    return {"windows": WINDOWS, "darwin": HOST_IS_DARWIN, "path_io": USE_PATH_IO}
