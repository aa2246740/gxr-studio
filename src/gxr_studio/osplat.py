"""平台层：POSIX（macOS、Linux）和 Windows 的差别都收在这里。

- 文件锁：POSIX 用 `fcntl.flock`，Windows 用 `msvcrt.locking` 锁文件第一个字节。两者都是进程级的独占锁，
  进程退出（正常、被杀、崩溃）系统会自己放掉；同一把锁第二次非阻塞获取会立刻失败（抛 OSError）。
- fd → 真实路径：macOS 用 `F_GETPATH`，Linux 读 `/proc/self/fd`，Windows 用 `GetFinalPathNameByHandleW`。
- 完整落盘：macOS 用 `F_FULLFSYNC`，其他用 `os.fsync`（Windows 上就是 FlushFileBuffers）。
- 父进程还在不在：POSIX 看 `os.getppid()` 有没有变（父进程没了会被过继）；Windows 不过继，`getppid` 一直是原来的号，
  改为启动时拿住父进程句柄、看它有没有结束（句柄拿住了，进程号被复用也不会认错）。

**按目录句柄的文件操作**（`open_dir`、`open_dir_at`、`open_file_at`、`lstat_at`、`listdir_fd`、`mkdir_at`、`unlink_at`、
`rmdir_at`、`rename_at`）：私有目录（`privacy.PrivateDir`）、读卡（`cards.discovery.CardReader`）和照片文件（`library.media`）
只通过这些函数碰文件系统，两个平台走同一套上层代码。句柄一律是 CRT 的整数 fd（目录也是），用完 `os.close`。

- POSIX：`os.*(..., dir_fd=…)`，目录 `O_DIRECTORY|O_NOFOLLOW`，文件 `O_NOFOLLOW`；
- Windows：`NtCreateFile` 以父目录句柄为根（`RootDirectory`）相对打开，带 `FILE_OPEN_REPARSE_POINT`，打开后看属性：
  重解析点（符号链接、目录联接、挂载点，保守起见也包括云盘占位）一律当链接拒绝（与 `O_NOFOLLOW` 的 ELOOP 相同）；
  删除和改名都按已经打开的那个句柄做（`NtSetInformationFile` 的 Disposition / Rename，改名的目标以父目录句柄为根）；
  列目录按句柄（`GetFileInformationByHandleEx`）。这些调用不经过 `os.*`，所以这里自己发出同名的审计事件（`sys.audit`），
  `audit` 的保护规则照常生效。

两个平台都没有"核对完再按路径做一次"的空隙。
"""

from __future__ import annotations

import errno
import os
import stat
import sys
from pathlib import PurePath
from typing import Any, Callable

from . import audit

WINDOWS = sys.platform == "win32"
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _check_name(name: str) -> str:
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\0" in name:
        raise OSError(errno.EINVAL, "名字不合法", name)
    return name


if sys.platform == "win32":  # pragma: no cover - 只在 Windows 上执行（CI 的 windows-latest）
    import ctypes
    import msvcrt
    import struct
    from ctypes import wintypes

    HOST_IS_DARWIN = False

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")

    class _UnicodeString(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", wintypes.LPWSTR)]

    class _ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(_UnicodeString)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", wintypes.LPVOID),
            ("SecurityQualityOfService", wintypes.LPVOID),
        ]

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

    class _FileBasicInfo(ctypes.Structure):
        _fields_ = [
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
        ]

    _NtCreateFile = _ntdll.NtCreateFile
    _NtCreateFile.restype = ctypes.c_long
    _NtCreateFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
    ]
    _NtSetInformationFile = _ntdll.NtSetInformationFile
    _NtSetInformationFile.restype = ctypes.c_long
    _NtSetInformationFile.argtypes = [wintypes.HANDLE, ctypes.POINTER(_IoStatusBlock), ctypes.c_void_p, wintypes.ULONG, ctypes.c_int]
    _RtlNtStatusToDosError = _ntdll.RtlNtStatusToDosError
    _RtlNtStatusToDosError.restype = wintypes.ULONG
    _RtlNtStatusToDosError.argtypes = [ctypes.c_long]
    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.restype = wintypes.HANDLE
    _CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _CloseHandle.restype = wintypes.BOOL
    _GetInfoEx = _kernel32.GetFileInformationByHandleEx
    _GetInfoEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _GetInfoEx.restype = wintypes.BOOL
    _GetFinalPath = _kernel32.GetFinalPathNameByHandleW
    _GetFinalPath.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    _GetFinalPath.restype = wintypes.DWORD
    _INVALID = wintypes.HANDLE(-1).value

    # 访问权限、共享、打开方式（winnt.h / ntifs.h）
    _SYNCHRONIZE = 0x00100000
    _DELETE = 0x00010000
    _READ_CONTROL = 0x00020000
    _FILE_LIST_DIRECTORY = 0x0001
    _FILE_TRAVERSE = 0x0020
    _FILE_READ_ATTRIBUTES = 0x0080
    _FILE_GENERIC_READ = 0x00120089
    _FILE_GENERIC_WRITE = 0x00120116
    _SHARE_ALL = 0x7
    _FILE_OPEN, _FILE_CREATE = 1, 2
    _FILE_DIRECTORY_FILE = 0x00000001
    _FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
    _FILE_NON_DIRECTORY_FILE = 0x00000040
    _FILE_OPEN_FOR_BACKUP_INTENT = 0x00004000
    _FILE_OPEN_REPARSE_POINT = 0x00200000
    _OBJ_CASE_INSENSITIVE = 0x40
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _FILE_ATTRIBUTE_DIRECTORY = 0x10
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _OPEN_EXISTING = 3
    _DIR_ACCESS = _FILE_LIST_DIRECTORY | _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES | _READ_CONTROL | _SYNCHRONIZE
    # 信息类别：NtSetInformationFile（FILE_INFORMATION_CLASS）和 GetFileInformationByHandleEx（FILE_INFO_BY_HANDLE_CLASS）
    _FileRenameInformation, _FileDispositionInformation = 10, 13
    _FileDispositionInformationEx, _FileRenameInformationEx = 64, 65
    _FileAttributeTagInfo, _FileFullDirectoryInfo, _FileFullDirectoryRestartInfo = 9, 14, 15
    _ERROR_NO_MORE_FILES = 18

    def _nt_error(status: int, name: str) -> OSError:
        code = _RtlNtStatusToDosError(status)
        native = status & 0xFFFFFFFF
        message = f"{ctypes.FormatError(code).strip()} (NTSTATUS 0x{native:08x})"
        # Win32 把目录类型错误也转换成 ERROR_ACCESS_DENIED；保留 NT 的类型区别。
        if native == 0xC00000BA:  # STATUS_FILE_IS_A_DIRECTORY
            return IsADirectoryError(errno.EISDIR, message, name)
        if native == 0xC0000103:  # STATUS_NOT_A_DIRECTORY
            return NotADirectoryError(errno.ENOTDIR, message, name)
        return OSError(None, message, name, code)

    def _handle(fd: int) -> int:
        return int(msvcrt.get_osfhandle(fd))

    def fstat(fd: int) -> os.stat_result:
        """内容核对的 ctime 取 NT ChangeTime，避免把 Windows 创建时间当作修改标识。"""
        st = os.fstat(fd)
        info = _FileBasicInfo()
        if not _GetInfoEx(_handle(fd), 0, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        ns = (info.ChangeTime - 116444736000000000) * 100
        base = {"st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid", "st_size"}
        fields = {name: getattr(st, name) for name in dir(st) if name.startswith("st_") and name not in base}
        fields.update(st_ctime=ns / 1_000_000_000, st_ctime_ns=ns)
        return os.stat_result(tuple(st[:9]) + (ns // 1_000_000_000,), fields)

    def _units(text: str) -> int:
        return len(text.encode("utf-16-le")) // 2

    def _nt_open(dfd: int, name: str, access: int, disposition: int, options: int) -> int:
        """以目录 fd 为根相对打开 `name`，返回原始 HANDLE（不跟随最后一级的重解析点）。"""
        _check_name(name)
        buf = ctypes.create_unicode_buffer(name)
        size = _units(name) * 2  # UTF-16 字节数（BMP 以外的字符占两个单元）
        uname = _UnicodeString(size, size + 2, ctypes.cast(buf, wintypes.LPWSTR))
        attrs = _ObjectAttributes(ctypes.sizeof(_ObjectAttributes), _handle(dfd), ctypes.pointer(uname), _OBJ_CASE_INSENSITIVE, None, None)
        iosb = _IoStatusBlock()
        out = wintypes.HANDLE()
        options |= _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_OPEN_FOR_BACKUP_INTENT | _FILE_OPEN_REPARSE_POINT
        status = _NtCreateFile(
            ctypes.byref(out),
            access | _SYNCHRONIZE,
            ctypes.byref(attrs),
            ctypes.byref(iosb),
            None,
            _FILE_ATTRIBUTE_NORMAL,
            _SHARE_ALL,
            disposition,
            options,
            None,
            0,
        )
        if status < 0:
            raise _nt_error(status, name)
        return int(out.value or 0)

    def _close_handle(h: int) -> None:
        _CloseHandle(h)

    def _tag(h: int) -> tuple[int, int]:
        """(文件属性, 重解析标记)。"""
        buf = (ctypes.c_ubyte * 8)()
        if not _GetInfoEx(h, _FileAttributeTagInfo, buf, 8):
            raise ctypes.WinError(ctypes.get_last_error())
        attrs, tag = struct.unpack("<II", bytes(buf))
        return attrs, tag

    def _is_surrogate(h: int) -> tuple[bool, bool]:
        """(是不是重解析点, 是不是目录)。重解析点一律当链接（与 `is_link` 一致）：符号链接、目录联接、挂载点，
        也包括云盘占位这类非链接的重解析点——保守起见同样拒绝。"""
        attrs, _tag_value = _tag(h)
        return bool(attrs & REPARSE_POINT), bool(attrs & _FILE_ATTRIBUTE_DIRECTORY)

    def _to_fd(h: int, readonly: bool) -> int:
        try:
            return int(msvcrt.open_osfhandle(h, os.O_RDONLY if readonly else 0))
        except BaseException:
            _close_handle(h)
            raise

    def _where(dfd: int, name: str) -> str:
        base = fd_path(dfd)
        return os.path.join(base, name) if base else name

    def _open_checked(dfd: int, name: str, access: int, disposition: int, options: int, *, want_dir: bool | None) -> int:
        """相对打开并拒绝重解析点（不跟随，等同 O_NOFOLLOW 的 ELOOP）。返回原始 HANDLE。"""
        h = _nt_open(dfd, name, access, disposition, options)
        try:
            link, is_dir = _is_surrogate(h)
            if link:
                raise OSError(errno.ELOOP, "路径里有链接", _where(dfd, name))
            if want_dir is True and not is_dir:
                raise NotADirectoryError(errno.ENOTDIR, "不是目录", _where(dfd, name))
            if want_dir is False and is_dir:
                raise IsADirectoryError(errno.EISDIR, "是目录", _where(dfd, name))
        except BaseException:
            _close_handle(h)
            raise
        return h

    def open_dir(path: str | os.PathLike[str]) -> int:
        """绝对路径的目录：盘符/共享根用 CreateFileW 打开，之后逐级相对打开，任何一级是链接都失败。"""
        p = os.path.abspath(os.fspath(path))
        pure = PurePath(p)
        h = _CreateFileW(pure.anchor, _DIR_ACCESS, _SHARE_ALL, None, _OPEN_EXISTING, _FILE_FLAG_BACKUP_SEMANTICS, None)
        if h is None or h == _INVALID:
            raise ctypes.WinError(ctypes.get_last_error())
        fd = _to_fd(int(h), True)
        try:
            for part in pure.parts[1:]:
                nfd = open_dir_at(fd, part)
                os.close(fd)
                fd = nfd
        except BaseException:
            os.close(fd)
            raise
        return fd

    def open_dir_at(dfd: int, name: str) -> int:
        sys.audit("open", _where(dfd, name), None, os.O_RDONLY)
        return _to_fd(_open_checked(dfd, name, _DIR_ACCESS, _FILE_OPEN, _FILE_DIRECTORY_FILE, want_dir=True), True)

    def open_file_at(dfd: int, name: str, *, create: bool = False) -> int:
        """`create=False`：只读打开已有文件；`create=True`：排他新建、只写（已存在就 FileExistsError）。"""
        if create:
            sys.audit("open", _where(dfd, name), None, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            h = _open_checked(dfd, name, _FILE_GENERIC_WRITE | _FILE_GENERIC_READ, _FILE_CREATE, _FILE_NON_DIRECTORY_FILE, want_dir=False)
            return _to_fd(h, False)
        sys.audit("open", _where(dfd, name), None, os.O_RDONLY)
        return _to_fd(_open_checked(dfd, name, _FILE_GENERIC_READ, _FILE_OPEN, _FILE_NON_DIRECTORY_FILE, want_dir=False), True)

    def lstat_at(dfd: int, name: str) -> os.stat_result:
        """不跟随最后一级：链接类重解析点返回它自己的信息（`is_link` 认得出）。"""
        h = _nt_open(dfd, name, _FILE_READ_ATTRIBUTES, _FILE_OPEN, 0)
        fd = _to_fd(h, True)
        try:
            return fstat(fd)
        finally:
            os.close(fd)

    def listdir_fd(dfd: int) -> list[str]:
        """按目录句柄列名字（FILE_FULL_DIR_INFO），不经过路径。"""
        sys.audit("os.listdir", fd_path(dfd) or "")
        h = _handle(dfd)
        names: list[str] = []
        buf = (ctypes.c_ubyte * 65536)()
        cls = _FileFullDirectoryRestartInfo
        while True:
            if not _GetInfoEx(h, cls, buf, ctypes.sizeof(buf)):
                err = ctypes.get_last_error()
                if err == _ERROR_NO_MORE_FILES:
                    return names
                raise ctypes.WinError(err)
            cls = _FileFullDirectoryInfo
            raw = bytes(buf)
            off = 0
            while True:
                nxt = struct.unpack_from("<I", raw, off)[0]
                length = struct.unpack_from("<I", raw, off + 60)[0]
                name = raw[off + 68 : off + 68 + length].decode("utf-16-le")
                if name not in (".", ".."):
                    names.append(name)
                if nxt == 0:
                    break
                off += nxt

    def mkdir_at(dfd: int, name: str, mode: int = 0o700) -> None:
        sys.audit("os.mkdir", name, mode, dfd)
        _close_handle(_nt_open(dfd, name, _FILE_LIST_DIRECTORY, _FILE_CREATE, _FILE_DIRECTORY_FILE))

    def _set_info(h: int, cls: int, data: Any) -> int:
        iosb = _IoStatusBlock()
        return int(_NtSetInformationFile(h, ctypes.byref(iosb), ctypes.byref(data), ctypes.sizeof(data), cls))

    def _delete_open(dfd: int, name: str, *, want_dir: bool) -> None:
        """按句柄删除：打开的就是要删的那个对象（链接本身，不跟随），删它；不会按路径再解析一次。"""
        h = _nt_open(dfd, name, _DELETE | _FILE_READ_ATTRIBUTES, _FILE_OPEN, 0)
        try:
            link, is_dir = _is_surrogate(h)
            if not link and want_dir and not is_dir:
                raise NotADirectoryError(errno.ENOTDIR, "不是目录", _where(dfd, name))
            if not link and not want_dir and is_dir:
                raise IsADirectoryError(errno.EISDIR, "是目录", _where(dfd, name))
            if link and want_dir:
                raise NotADirectoryError(errno.ENOTDIR, "是链接", _where(dfd, name))
            # POSIX 语义的删除：名字立即消失；不支持时退回普通的关闭时删除
            ex = wintypes.DWORD(0x1 | 0x2 | 0x10)  # DELETE | POSIX_SEMANTICS | IGNORE_READONLY_ATTRIBUTE
            status = _set_info(h, _FileDispositionInformationEx, ex)
            if status < 0:
                plain = ctypes.c_ubyte(1)
                status = _set_info(h, _FileDispositionInformation, plain)
            if status < 0:
                raise _nt_error(status, _where(dfd, name))
        finally:
            _close_handle(h)

    def unlink_at(dfd: int, name: str) -> None:
        sys.audit("os.remove", name, dfd)
        _delete_open(dfd, name, want_dir=False)

    def rmdir_at(dfd: int, name: str) -> None:
        sys.audit("os.rmdir", name, dfd)
        _delete_open(dfd, name, want_dir=True)

    def rename_at(dfd: int, old: str, new: str) -> None:
        """同一个目录句柄下改名（目标已存在就替换）。源按句柄打开；目标以这个目录句柄为根，不按路径解析。"""
        _check_name(new)
        sys.audit("os.rename", old, new, dfd, dfd)
        h = _nt_open(dfd, old, _DELETE | _FILE_READ_ATTRIBUTES, _FILE_OPEN, 0)
        try:
            n = _units(new)

            class _Rename(ctypes.Structure):
                _fields_ = [
                    ("Flags", wintypes.DWORD),
                    ("RootDirectory", wintypes.HANDLE),
                    ("FileNameLength", wintypes.DWORD),
                    ("FileName", wintypes.WCHAR * (n + 1)),
                ]

            info = _Rename(0x1 | 0x2, _handle(dfd), n * 2, new)  # REPLACE_IF_EXISTS | POSIX_SEMANTICS
            status = _set_info(h, _FileRenameInformationEx, info)
            if status < 0:
                info.Flags = 1  # FileRenameInformation：ReplaceIfExists = TRUE
                status = _set_info(h, _FileRenameInformation, info)
            if status < 0:
                raise _nt_error(status, _where(dfd, old))
        finally:
            _close_handle(h)

    def sync_dir(dfd: int) -> None:
        """Windows 不能单独让目录落盘（FlushFileBuffers 要写权限）；文件本身已经落盘。"""

    def lock_exclusive_nb(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

    def fd_path(fd: int) -> str | None:
        try:
            handle = msvcrt.get_osfhandle(fd)
        except OSError:
            return None
        buf = ctypes.create_unicode_buffer(1024)
        n = _GetFinalPath(handle, buf, 1024, 0)
        if n == 0 or n >= 1024:
            return None
        path = buf.value
        if path.startswith("\\\\?\\UNC\\"):
            return "\\\\" + path[8:]
        return path[4:] if path.startswith("\\\\?\\") else path

    def full_fsync(fd: int) -> None:
        os.fsync(fd)

    def parent_alive(parent_pid: int) -> Callable[[], bool]:
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
    _DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
    _NONBLOCK = getattr(os, "O_NONBLOCK", 0)

    def fstat(fd: int) -> os.stat_result:
        return os.fstat(fd)

    def open_dir(path: str | os.PathLike[str]) -> int:
        """绝对路径的目录：从 `/` 开始逐级 O_DIRECTORY|O_NOFOLLOW 打开，任何一级是链接都失败（ELOOP/ENOTDIR）。"""
        pure = PurePath(os.path.abspath(os.fspath(path)))
        fd = os.open(pure.anchor, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            for part in pure.parts[1:]:
                nfd = open_dir_at(fd, part)
                os.close(fd)
                fd = nfd
        except BaseException:
            os.close(fd)
            raise
        return fd

    def open_dir_at(dfd: int, name: str) -> int:
        # 只是逐级进入目录（遍历），不读文件内容：不按句柄解析审计路径（否则从 / 走到 /Volumes 会被当成读卷根）
        return os.open(_check_name(name), _DIR_FLAGS, dir_fd=dfd)

    def open_file_at(dfd: int, name: str, *, create: bool = False) -> int:
        """`create=False`：只读打开已有的普通文件；`create=True`：排他新建、只写。最后一级不跟随链接。"""
        with audit.relative_to(dfd):  # `open` 审计事件不带 dir_fd：按这个句柄的真实位置解析相对名
            if create:
                return os.open(_check_name(name), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
            fd = os.open(_check_name(name), os.O_RDONLY | os.O_NOFOLLOW | _NONBLOCK, dir_fd=dfd)  # FIFO 之类不会卡住
        try:
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise IsADirectoryError(errno.EISDIR, "是目录", name)
            os.set_blocking(fd, True)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def lstat_at(dfd: int, name: str) -> os.stat_result:
        return os.stat(_check_name(name), dir_fd=dfd, follow_symlinks=False)

    def listdir_fd(dfd: int) -> list[str]:
        return os.listdir(dfd)

    def mkdir_at(dfd: int, name: str, mode: int = 0o700) -> None:
        os.mkdir(_check_name(name), mode, dir_fd=dfd)

    def unlink_at(dfd: int, name: str) -> None:
        os.unlink(_check_name(name), dir_fd=dfd)

    def rmdir_at(dfd: int, name: str) -> None:
        os.rmdir(_check_name(name), dir_fd=dfd)

    def rename_at(dfd: int, old: str, new: str) -> None:
        os.rename(_check_name(old), _check_name(new), src_dir_fd=dfd, dst_dir_fd=dfd)

    def sync_dir(dfd: int) -> None:
        full_fsync(dfd)

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


def lstat(path: str | os.PathLike[str]) -> os.stat_result:
    """内容核对用的状态：Windows 同样提供修改时间含义的 ctime。"""
    if not WINDOWS:
        return os.lstat(path)
    p = PurePath(os.path.abspath(os.fspath(path)))
    parent = open_dir(os.path.realpath(p.parent))
    try:
        return lstat_at(parent, p.name)
    finally:
        os.close(parent)


def is_link(st: os.stat_result) -> bool:
    """lstat 的结果是不是链接：符号链接，或 Windows 的重解析点（目录联接等）。"""
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & REPARSE_POINT)


def is_real_dir(st: os.stat_result) -> bool:
    """是目录、而且不是链接（Windows 的目录联接 st_mode 也是目录）。"""
    return stat.S_ISDIR(st.st_mode) and not is_link(st)


def binary_flag() -> int:
    return int(getattr(os, "O_BINARY", 0))


def describe() -> dict[str, Any]:
    return {"windows": WINDOWS, "darwin": HOST_IS_DARWIN}
