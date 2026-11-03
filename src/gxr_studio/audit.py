"""审计钩子：记录每一次文件写入的路径，并能拦截对受保护目录（存储卡）的写入。

`sys.addaudithook` 装上就拆不掉，所以这里只装一次，真正的规则放在模块级的表里，
可以随时增减：

- `AuditRecorder`：记录写入（以及可选的读取）事件，测试用它证明"对卡的写入次数为 0"；
- `protect()`：登记一个受保护的目录，之后任何对它的写入都会被记录并抛 `PermissionError`；
- `forbid_reads()`：测试里用来保证不会去碰真实挂载的卡；
- `forbid_network()`：离线验收（M1 验收 8）。
"""

from __future__ import annotations

import contextvars
import errno
import os
import socket
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

# 事件名 -> 参数里哪几个位置是路径
_PATH_EVENTS: dict[str, tuple[int, ...]] = {
    "os.rename": (0, 1),
    "os.remove": (0,),
    "os.rmdir": (0,),
    "os.mkdir": (0,),
    "os.truncate": (0,),
    "os.chmod": (0,),
    "os.chown": (0,),
    "os.utime": (0,),
    "os.link": (0, 1),
    "os.symlink": (1,),
    "shutil.copyfile": (0, 1),
    "shutil.copymode": (1,),
    "shutil.copystat": (1,),
    "shutil.copytree": (0, 1),
    "shutil.move": (0, 1),
    "shutil.rmtree": (0,),
    "sqlite3.connect": (0,),
}
_READ_EVENTS = {"os.listdir", "os.scandir", "os.walk", "os.fwalk"}
# 带 dir_fd 的事件：路径参数位置 -> dir_fd 参数位置。相对路径按 dir_fd 的真实目录解析，不按 cwd
_DIR_FD_ARGS: dict[str, dict[int, int]] = {
    "os.rename": {0: 2, 1: 3},
    "os.remove": {0: 1},
    "os.rmdir": {0: 1},
    "os.mkdir": {0: 2},
    "os.chmod": {0: 2},
    "os.chown": {0: 3},
    "os.utime": {0: 3},
    "os.link": {0: 2, 1: 3},
    "os.symlink": {1: 2},
}
# `open` 事件不带 dir_fd：按目录 fd 读写的代码（privacy.PrivateDir）在这段时间里把那个目录的真实路径放在这里
# 放目录 fd（整数）时在事件发生的那一刻再解析真实位置：目录在这之间被移走，记录的也是它真正所在的地方
_relative_base: contextvars.ContextVar[str | int | None] = contextvars.ContextVar("gxr_audit_relative_base", default=None)


@dataclass(frozen=True)
class WriteEvent:
    op: str
    path: str
    blocked: bool = False


@dataclass
class AuditRecorder:
    """收集写入事件。`watch` 非空时只收集落在这些前缀下的事件。"""

    watch: tuple[str, ...] = ()
    record_reads: bool = False
    limit: int = 20000
    writes: list[WriteEvent] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)

    def __enter__(self) -> "AuditRecorder":
        install()
        with _lock:
            _recorders.append(self)
        return self

    def __exit__(self, *exc: object) -> None:
        with _lock:
            if self in _recorders:
                _recorders.remove(self)

    def writes_under(self, root: str | os.PathLike[Any]) -> list[WriteEvent]:
        prefixes = _prefixes(root)
        return [w for w in self.writes if _under(w.path, prefixes)]

    def reads_under(self, root: str | os.PathLike[Any]) -> list[str]:
        prefixes = _prefixes(root)
        return [p for p in self.reads if _under(p, prefixes)]

    def _wants(self, path: str) -> bool:
        return not self.watch or _under(path, self.watch)


_lock = threading.Lock()
_installed = False
_recorders: list[AuditRecorder] = []
_protected: list[str] = []
_forbidden_reads: list[str] = []
_volume_guard: tuple[str, tuple[str, ...]] | None = None  # (卷根, 允许的卷名)
_network_forbidden = False
_network_log: list[str] = []


def _prefixes(root: str | os.PathLike[Any]) -> tuple[str, ...]:
    p = os.path.abspath(os.fspath(root))
    out = {p}
    try:
        out.add(os.path.realpath(p))
    except OSError:
        pass
    return tuple(out)


def _under(path: str, prefixes: tuple[str, ...]) -> bool:
    for pre in prefixes:
        if path == pre or path.startswith(pre.rstrip(os.sep) + os.sep):
            return True
    return False


def _fd_dir(fd: int) -> str | None:
    from .osplat import fd_path  # 延迟导入：审计钩子要尽早装上，平台层不依赖它

    return fd_path(fd)


@contextmanager
def relative_to(directory: str | int | None) -> Iterator[None]:
    """这段时间里 `open` 事件的相对路径按 `directory` 解析：目录 fd（推荐，事件发生时再查真实位置）或路径。"""
    token = _relative_base.set(directory)
    try:
        yield
    finally:
        _relative_base.reset(token)


def _norm(value: Any, dir_fd: Any = None) -> str | None:
    if isinstance(value, int) or value is None:
        return None
    try:
        p = os.fsdecode(value)
    except (TypeError, ValueError):
        return None
    if not os.path.isabs(p):
        base = _fd_dir(dir_fd) if isinstance(dir_fd, int) and dir_fd >= 0 else _relative_base.get()
        if isinstance(base, int):
            base = _fd_dir(base)
        if base:
            return os.path.normpath(os.path.join(base, p))
    return os.path.abspath(p)


def _is_write_open(mode: Any, flags: Any) -> bool:
    if isinstance(mode, str) and any(c in mode for c in "wax+"):
        return True
    return isinstance(flags, int) and bool(flags & _WRITE_FLAGS)


def _event_loop_socketpair(args: tuple[Any, ...]) -> bool:
    """仅识别 CPython 事件循环自唤醒通道中的那一次 socketpair 连接。"""
    from asyncio import proactor_events, selector_events

    caller = sys._getframe(2)
    owner = caller.f_back
    fallback = getattr(socket, "_fallback_socketpair", socket.socketpair)
    if caller.f_code is not getattr(fallback, "__code__", None) or owner is None:
        return False
    if owner.f_code not in (
        getattr(selector_events.BaseSelectorEventLoop, "_make_self_pipe").__code__,
        getattr(proactor_events.BaseProactorEventLoop, "_make_self_pipe").__code__,
    ):
        return False
    listener = caller.f_locals.get("lsock")
    client = caller.f_locals.get("csock")
    if len(args) != 2 or args[0] is not client or not isinstance(listener, socket.socket):
        return False
    address = args[1]
    return isinstance(address, tuple) and len(address) == 2 and address[0] in ("127.0.0.1", "::1") and listener.getsockname()[:2] == address


def _hook(event: str, args: tuple[Any, ...]) -> None:
    if event == "open":
        if not (_recorders or _protected or _forbidden_reads or _volume_guard):
            return
        path = _norm(args[0])
        if path is None:
            return
        if _is_write_open(args[1] if len(args) > 1 else None, args[2] if len(args) > 2 else None):
            _on_write("open", (path,))
        else:
            _on_read(path)
        return
    idx = _PATH_EVENTS.get(event)
    if idx is not None:
        fds = _DIR_FD_ARGS.get(event, {})
        paths = [p for p in (_norm(args[i], args[fds[i]] if i in fds and fds[i] < len(args) else None) for i in idx if i < len(args)) if p]
        if paths:
            _on_write(event, tuple(paths))
        return
    if event in _READ_EVENTS:
        if args:
            path = _norm(args[0]) if args[0] is not None else os.getcwd()
            if path:
                _on_read(path)
        return
    if event in ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname") and _network_forbidden:
        target = repr(args[1:] if event == "socket.connect" else args)
        if event == "socket.connect":
            addr = args[1] if len(args) > 1 else None
            # 本机 unix socket 不算联网
            if isinstance(addr, (str, bytes)) or _event_loop_socketpair(args):
                return
        _network_log.append(f"{event} {target}")
        raise PermissionError(errno.EPERM, f"offline: {event} {target}")


def _volume_denied(path: str) -> bool:
    if _volume_guard is None:
        return False
    base, allowed = _volume_guard
    if path == base:
        return True
    if not path.startswith(base + os.sep):
        return False
    return path[len(base) + 1 :].split(os.sep, 1)[0] not in allowed


def _resolved(path: str) -> tuple[str, tuple[str, ...]]:
    """-> (记录用的真实路径, 用来判断保护的全部形式)。经过链接的别名要解析到真实位置（复审 A1a R2-P1-01）：
    上级目录解析后的位置（改名、删除、建目录作用在这里）和整条路径解析后的位置（打开写入会跟到这里）都要核对。"""
    try:
        parent = os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
        full = os.path.realpath(path)
    except (OSError, ValueError):
        return path, (path,)
    forms = tuple(dict.fromkeys((path, parent, full)))
    return full, forms


def _on_write(op: str, paths: tuple[str, ...]) -> None:
    resolved = [_resolved(p) for p in paths]
    for _, forms in resolved:
        for form in forms:
            if _volume_guard is not None and _volume_denied(form):
                raise PermissionError(errno.EACCES, f"写入被拒绝（测试禁止访问真实卷）: {form}")
    deny = False
    protected = tuple(_protected)
    for path, forms in resolved:
        blocked = bool(protected) and any(_under(f, protected) for f in forms)
        for rec in tuple(_recorders):
            if any(rec._wants(f) for f in forms):
                if len(rec.writes) >= rec.limit:
                    del rec.writes[: rec.limit // 10 + 1]
                rec.writes.append(WriteEvent(op, path, blocked))
        if blocked:
            deny = True
            deny_path = path
    if deny:
        raise PermissionError(errno.EROFS, f"写入被拒绝（受保护的存储卡目录）: {deny_path}")


def _on_read(path: str) -> None:
    if _volume_guard is not None and _volume_denied(path):
        raise PermissionError(errno.EACCES, f"读取被拒绝（测试禁止访问真实卷）: {path}")
    for rec in tuple(_recorders):
        if rec.record_reads and rec._wants(path):
            if len(rec.reads) >= rec.limit:
                del rec.reads[: rec.limit // 10 + 1]
            rec.reads.append(path)
    if _forbidden_reads and _under(path, tuple(_forbidden_reads)):
        raise PermissionError(errno.EACCES, f"读取被拒绝（测试禁止访问）: {path}")


def install() -> None:
    global _installed
    with _lock:
        if not _installed:
            sys.addaudithook(_hook)
            _installed = True


def protect(root: str | os.PathLike[Any]) -> None:
    """把目录登记为只读：之后任何写入都会被记录并拒绝。"""
    install()
    with _lock:
        for p in _prefixes(root):
            if p not in _protected:
                _protected.append(p)


def protected_roots() -> list[str]:
    """当前登记为只读的目录（发现到的卡根等）。私有字节的目标目录守卫把它们一律当作禁止的目的地。"""
    with _lock:
        return list(_protected)


def unprotect(root: str | os.PathLike[Any]) -> None:
    with _lock:
        for p in _prefixes(root):
            if p in _protected:
                _protected.remove(p)


def forbid_reads(*roots: str | os.PathLike[Any]) -> None:
    install()
    with _lock:
        for r in roots:
            for p in _prefixes(r):
                if p not in _forbidden_reads:
                    _forbidden_reads.append(p)


def forbid_volumes(base: str = "/Volumes", allow: tuple[str, ...] = ("Workspace", "Macintosh HD")) -> None:
    """测试用：禁止读写 `/Volumes` 下除 allow 以外的任何卷（用户的真实存储卡在这里），连列目录都不行。"""
    global _volume_guard
    install()
    _volume_guard = (os.path.abspath(base), tuple(allow))


def forbid_network(on: bool = True) -> None:
    global _network_forbidden
    install()
    _network_forbidden = on


def network_attempts(*, clear: bool = False) -> list[str]:
    with _lock:
        result = list(_network_log)
        if clear:
            _network_log.clear()
        return result
