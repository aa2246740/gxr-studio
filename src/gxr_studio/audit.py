"""审计钩子：记录每一次文件写入的路径，并能拦截对受保护目录（存储卡）的写入。

`sys.addaudithook` 装上就拆不掉，所以这里只装一次，真正的规则放在模块级的表里，
可以随时增减：

- `AuditRecorder`：记录写入（以及可选的读取）事件，测试用它证明"对卡的写入次数为 0"；
- `protect()`：登记一个受保护的目录，之后任何对它的写入都会被记录并抛 `PermissionError`；
- `forbid_reads()`：测试里用来保证不会去碰真实挂载的卡；
- `forbid_network()`：离线验收（M1 验收 8）。
"""

from __future__ import annotations

import errno
import os
import sys
import threading
from dataclasses import dataclass, field

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

    def __exit__(self, *exc) -> None:
        with _lock:
            if self in _recorders:
                _recorders.remove(self)

    def writes_under(self, root: str | os.PathLike) -> list[WriteEvent]:
        prefixes = _prefixes(root)
        return [w for w in self.writes if _under(w.path, prefixes)]

    def reads_under(self, root: str | os.PathLike) -> list[str]:
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


def _prefixes(root: str | os.PathLike) -> tuple[str, ...]:
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


def _norm(value) -> str | None:
    if isinstance(value, int) or value is None:
        return None
    try:
        return os.path.abspath(os.fsdecode(value))
    except (TypeError, ValueError):
        return None


def _is_write_open(mode, flags) -> bool:
    if isinstance(mode, str) and any(c in mode for c in "wax+"):
        return True
    return isinstance(flags, int) and bool(flags & _WRITE_FLAGS)


def _hook(event: str, args: tuple) -> None:
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
        paths = [p for p in (_norm(args[i]) for i in idx if i < len(args)) if p]
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
            if isinstance(addr, (str, bytes)):
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


def _on_write(op: str, paths: tuple[str, ...]) -> None:
    for path in paths:
        if _volume_guard is not None and _volume_denied(path):
            raise PermissionError(errno.EACCES, f"写入被拒绝（测试禁止访问真实卷）: {path}")
    deny = False
    for path in paths:
        for rec in tuple(_recorders):
            if rec._wants(path):
                blocked = bool(_protected) and _under(path, tuple(_protected))
                if len(rec.writes) >= rec.limit:
                    del rec.writes[: rec.limit // 10 + 1]
                rec.writes.append(WriteEvent(op, path, blocked))
        if _protected and _under(path, tuple(_protected)):
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


def protect(root: str | os.PathLike) -> None:
    """把目录登记为只读：之后任何写入都会被记录并拒绝。"""
    install()
    with _lock:
        for p in _prefixes(root):
            if p not in _protected:
                _protected.append(p)


def unprotect(root: str | os.PathLike) -> None:
    with _lock:
        for p in _prefixes(root):
            if p in _protected:
                _protected.remove(p)


def forbid_reads(*roots: str | os.PathLike) -> None:
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
    _volume_guard = (base, tuple(allow))


def forbid_network(on: bool = True) -> None:
    global _network_forbidden
    install()
    _network_forbidden = on


def network_attempts() -> list[str]:
    return list(_network_log)
