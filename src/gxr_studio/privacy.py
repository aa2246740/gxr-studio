"""私有字节的目标目录守卫（设计 §5.6 第一道）。

理光字节（相机配方文件、标定包、将来的 blob / 快照 / .gxrkit）只能写到用户自己的应用数据或导出目录。
默认目录都在仓库外面，但 `GXR_STUDIO_HOME`、`GXR_STUDIO_EXPORT` 可以改到任何地方，所以每个会产出私有字节的
入口在写之前都要核对真实目标目录：

1. 取 realpath，逐级往上找 `.git`（目录或文件都算）。找到就拒绝：覆盖主仓库、studio 子仓库和任何 Git 工作区，
   经符号链接进入的也一样；
2. 不能在任何卷根（`GXR_STUDIO_VOLUMES`）、任何当前卡根（含 DCIM）或图库目录之下；登记了写保护的目录
   （发现到的卡根，`audit.protect`）不管调用方传没传，一律算在内；
3. 写的时候（`PrivateDir`）按 O_DIRECTORY|O_NOFOLLOW 逐级打开目标目录，之后的创建、读回、删除、改名、整体替换
   全部相对这个目录 fd 进行；每次改动之前再核对 fd 的真实路径：检查之后有人把目录换成链接，也写不进去；
4. 拒绝时不写半个文件，报"无法导出到这个文件夹。请选择其他文件夹。"，`detail` 写原因。

忽略规则和提交前检查（`scripts/private_bytes.py`）只是辅助，这里才是防线。
"""

from __future__ import annotations

import ctypes
import errno
import functools
import os
import secrets
import shutil
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, TypeVar, cast

from . import audit, osplat
from .observe import log

MESSAGE = "无法导出到这个文件夹。请选择其他文件夹。"
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_HOST_IS_DARWIN = osplat.HOST_IS_DARWIN  # 真实宿主；不看运行中可能被改动的 sys.platform


class PrivatePathError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code, self.message, self.detail = code, MESSAGE, detail


def _real(path: str | os.PathLike[Any]) -> Path:
    return Path(os.path.realpath(os.path.abspath(os.fspath(path))))


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def git_worktree_of(path: str | os.PathLike[Any]) -> Path | None:
    """`path`（取 realpath 以后）或它的任何一级上级里有 `.git`（目录或文件）就返回那一级。"""
    real = _real(path)
    for d in (real, *real.parents):
        if os.path.lexists(d / ".git"):
            return d
    return None


def forbidden_for(settings: Any, card_roots: Iterable[str | os.PathLike[Any]] = ()) -> list[Path]:
    """卷根、当前卡根和图库目录：私有字节不能落在这些目录下面。"""
    out = [Path(p) for p in settings.volumes_roots]
    out += [Path(p) for p in card_roots]
    out.append(Path(settings.library_dir))
    return out


def check_private_dir(path: str | os.PathLike[Any], purpose: str, forbidden: Iterable[str | os.PathLike[Any]] = ()) -> Path:
    """核对 `path` 能不能放私有字节；能就返回它的 realpath，不能抛 `PrivatePathError`。目录不存在也可以检查。"""
    real = _real(path)
    repo = git_worktree_of(real)
    if repo is not None:
        raise PrivatePathError("private_path_in_repository", f"{purpose}: {real} 在 Git 工作区 {repo} 里")
    for root in [*forbidden, *audit.protected_roots()]:
        froot = _real(root)
        if _within(real, froot):
            raise PrivatePathError("private_path_forbidden", f"{purpose}: {real} 在受保护的目录 {froot} 下")
    return real


def fd_path(fd: int) -> str | None:
    """目录 fd 现在的真实路径（见 `osplat.fd_path`）。拿不到返回 None。"""
    return osplat.fd_path(fd)


def _open_dir_nofollow(real: Path) -> int:
    """从根开始逐级 O_DIRECTORY|O_NOFOLLOW 打开（不存在就排他创建那一级）。任何一级是链接都失败。"""
    fd = os.open("/", _DIR_FLAGS)
    try:
        for part in real.parts[1:]:
            try:
                nfd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(part, 0o700, dir_fd=fd)
                nfd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


def _rmtree_at(dfd: int, name: str, check: Any = None) -> None:
    """相对目录 fd 删掉一项（是目录就整棵删）；不跟随任何链接，链接本身被删掉。
    `check(fd)` 在每次删除之前核对实际使用的父目录 fd 还在允许的位置（被移走就停下）。"""
    if check is not None:
        check(dfd)
    st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(name, dir_fd=dfd)
        return
    with audit.relative_to(dfd):
        fd = os.open(name, _DIR_FLAGS, dir_fd=dfd)
    try:
        for child in os.listdir(fd):
            _rmtree_at(fd, child, check)
    finally:
        os.close(fd)
    if check is not None:
        check(dfd)
    os.rmdir(name, dir_fd=dfd)


def _exchange_at(dfd: int, a: str, b: str) -> bool:
    """macOS：renameatx_np(RENAME_SWAP) 在同一个目录 fd 下一次对调两个名字。不支持返回 False。"""
    if not _HOST_IS_DARWIN:
        return False
    try:
        fn = ctypes.CDLL(None, use_errno=True).renameatx_np
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
    except (OSError, AttributeError):
        return False
    return bool(fn(dfd, os.fsencode(a), dfd, os.fsencode(b), 2) == 0)


def _parts(rel: str) -> list[str]:
    parts = rel.split("/")
    if not rel or rel.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise PrivatePathError("private_name_invalid", f"文件名不合法：{rel}")
    return parts


_F = TypeVar("_F", bound=Callable[..., Any])


def _routed(fn: _F) -> _F:
    """公开方法先看子类有没有 `_path_<名字>` 的按路径实现（Windows）。测试里给 `PrivateDir.<名字>` 打补丁，两种实现都生效。"""
    name = fn.__name__

    @functools.wraps(fn)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        alt = getattr(type(self), f"_path_{name}", None)
        if alt is not None:
            return alt(self, *args, **kwargs)
        return fn(self, *args, **kwargs)

    return cast(_F, wrapper)


class PrivateDir:
    """已经核对过的私有目录能力（设计 §5.6）。没有目录 fd 的平台（Windows）上构造出来的是 `_PathPrivateDir`，接口相同。

    打开时：`check_private_dir` 核对，再按 realpath 从根逐级 O_DIRECTORY|O_NOFOLLOW 打开（核对之后被换成链接就打不开）。
    之后的所有读写都相对持有的目录 fd；每次改动之前 `verify()`：fd 的真实路径必须还是核对过的那个，而且仍然合格。
    """

    def __new__(cls, *args: Any, **kwargs: Any) -> "PrivateDir":
        if cls is PrivateDir and osplat.USE_PATH_IO:
            return super().__new__(_PathPrivateDir)
        return super().__new__(cls)

    def __init__(self, path: str | os.PathLike[Any], purpose: str, forbidden: Iterable[str | os.PathLike[Any]] = ()) -> None:
        self.purpose = purpose
        self.forbidden = [Path(f) for f in forbidden]
        self.real = check_private_dir(path, purpose, self.forbidden)
        try:
            self.fd = _open_dir_nofollow(self.real)
        except OSError as exc:
            raise PrivatePathError("private_path_changed", f"{purpose}: 无法按不跟随链接的方式打开 {self.real}（{exc.strerror}）") from exc
        try:
            self.verify()
        except BaseException:
            os.close(self.fd)
            raise

    def __enter__(self) -> "PrivateDir":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    @_routed
    def verify(self) -> None:
        now = fd_path(self.fd)
        if now is None or _real(now) != self.real:
            raise PrivatePathError("private_path_changed", f"{self.purpose}: 目录在检查之后变了（{now}）")
        check_private_dir(now, self.purpose, self.forbidden)

    @_routed
    def check_fd(self, fd: int) -> None:
        """核对实际要用的那个目录 fd（不只是最外层）：它现在的真实位置必须还在核对过的目录里面，而且仍然合格
        （不在任何 Git 工作区、卡根或其他禁止目录下）。子目录在打开之后被整个移走，也在这里拦下。"""
        now = fd_path(fd)
        if now is None:
            raise PrivatePathError("private_path_changed", f"{self.purpose}: 拿不到目录的真实位置")
        real = _real(now)
        if not _within(real, self.real):
            raise PrivatePathError("private_path_changed", f"{self.purpose}: 目录在检查之后被移到了 {real}")
        check_private_dir(real, self.purpose, self.forbidden)

    @contextmanager
    def _at(self, rel: str, create: bool = False) -> Iterator[tuple[int, str]]:
        """相对路径 → (父目录 fd, 最后一级名字)。中间每一级都不跟随链接；`create` 时缺的目录排他创建。"""
        parts = _parts(rel)
        opened: list[int] = []
        fd = self.fd
        try:
            for part in parts[:-1]:
                with audit.relative_to(fd):
                    try:
                        nfd = os.open(part, _DIR_FLAGS, dir_fd=fd)
                    except FileNotFoundError:
                        if not create:
                            raise
                        self.check_fd(fd)  # 建目录之前：这一级父目录还在原处
                        os.mkdir(part, 0o700, dir_fd=fd)
                        nfd = os.open(part, _DIR_FLAGS, dir_fd=fd)
                opened.append(nfd)
                fd = nfd
            # 审计钩子的 open 事件不带 dir_fd：把 fd 本身交给它，事件发生时再解析真实目录（不用早先存下的路径字符串）
            with audit.relative_to(fd):
                yield fd, parts[-1]
        finally:
            for o in reversed(opened):
                os.close(o)

    @_routed
    def exists(self, rel: str) -> bool:
        try:
            with self._at(rel) as (dfd, name):
                os.stat(name, dir_fd=dfd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    @_routed
    def names(self, rel: str) -> list[str]:
        """按实际目录句柄列名字，不跟随中间目录的链接。"""
        with self._at(f"{rel}/.entries") as (dfd, _name):
            self.check_fd(dfd)
            return os.listdir(dfd)

    @_routed
    def read(self, rel: str) -> bytes:
        with self._at(rel) as (dfd, name):
            self.check_fd(dfd)
            fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dfd)
            with os.fdopen(fd, "rb") as fh:
                return fh.read()

    @_routed
    def write(self, rel: str, data: bytes) -> None:
        """排他创建一个新文件（已存在就失败，不覆盖），写完 fsync。"""
        self.verify()
        with self._at(rel, create=True) as (dfd, name):
            self.check_fd(dfd)  # 创建之前核对最终要用的父目录 fd
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=dfd)
            try:
                try:
                    view = memoryview(data)
                    while view:
                        self.check_fd(dfd)  # 每次写字节之前（复审 A1a R3-P2-01）
                        n = os.write(fd, view)
                        if n <= 0:
                            raise OSError("write returned 0")
                        view = view[n:]
                        self.check_fd(dfd)  # 每次写完：写的同时目录被移走也拦得住
                    _full_sync(fd)
                    self.check_fd(dfd)
                except PrivatePathError as exc:
                    raise self._moved_while_writing(fd, exc) from exc
            finally:
                os.close(fd)

    def _moved_while_writing(self, fd: int, exc: PrivatePathError) -> PrivatePathError:
        """文件已经创建、父目录在写的过程中被移到了别处：把这个文件里已经写进去的字节清空（只动自己打开的文件，
        不再对已经移走的那个目录做删除、改名等目录操作），在错误里写清楚留下了什么。"""
        where = fd_path(fd) or "（拿不到位置）"
        try:
            os.ftruncate(fd, 0)
            _full_sync(fd)
            left = f"已把写进去的字节清空，那里留下一个 0 字节的文件：{where}"
        except OSError as err:
            left = f"没能清空写进去的字节（{err.strerror}），文件：{where}"
        return PrivatePathError("private_path_changed", f"{exc.detail}；{left}（不再对已移走的目录做删除等操作）")

    @_routed
    def replace_file(self, rel: str, data: bytes) -> None:
        """原子替换一个文件：同目录写临时文件、fsync、改名、同步目录。失败不留临时文件。"""
        parts = _parts(rel)
        tmp = "/".join([*parts[:-1], f".{parts[-1]}.{secrets.token_hex(6)}.tmp"])
        try:
            self.write(tmp, data)
            self.verify()
            with self._at(rel) as (dfd, name):
                self.check_fd(dfd)
                os.rename(tmp.rsplit("/", 1)[-1], name, src_dir_fd=dfd, dst_dir_fd=dfd)
                _full_sync(dfd)
        except BaseException:
            self.remove(tmp)
            raise

    @_routed
    def remove(self, rel: str) -> None:
        """删掉一项（目录整棵删）；不存在就什么都不做。"""
        self.verify()
        try:
            with self._at(rel) as (dfd, name):
                _rmtree_at(dfd, name, self.check_fd)
        except FileNotFoundError:
            pass

    @_routed
    def rename(self, old: str, new: str) -> None:
        """本目录这一层里改名（两个名字都不带 /）。"""
        _parts(old), _parts(new)
        if "/" in old or "/" in new:
            raise PrivatePathError("private_name_invalid", f"只能在这一层改名：{old} → {new}")
        self.verify()
        self.check_fd(self.fd)
        os.rename(old, new, src_dir_fd=self.fd, dst_dir_fd=self.fd)

    @_routed
    def swap_in(self, new: str, root: str) -> None:
        """用 `new` 整体换掉 `root`（两者都在本目录这一层）。优先原子对调；不支持时退回 root→root.old、new→root，
        两步之间被打断由 `store.recover_interrupted_swaps` 恢复。"""
        _parts(new), _parts(root)
        self.verify()
        self.check_fd(self.fd)
        if not self.exists(root):
            os.rename(new, root, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        elif _exchange_at(self.fd, new, root):
            self.remove(new)  # 对调之后 new 这个名字下是旧的那份
        else:
            old = root + ".old"
            self.remove(old)
            os.rename(root, old, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.rename(new, root, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            self.remove(old)
        _full_sync(self.fd)


def _chain_has_link(path: Path) -> Path | None:
    """从盘符/根开始逐级 lstat：返回第一级是链接或重解析点的路径，没有就返回 None。"""
    cur = Path(path.anchor)
    for part in path.parts[1:]:
        cur = cur / part
        try:
            if osplat.is_link(os.lstat(cur)):
                return cur
        except FileNotFoundError:
            return None
    return None


class _PathPrivateDir(PrivateDir):
    """没有目录 fd 的平台（Windows，或测试里 GXR_FORCE_PATH_IO=1）：按路径做与 `PrivateDir` 相同的事。

    - 打开时与每次改动之前：realpath 必须还是核对过的那个，路径上任何一级都不能是链接或重解析点，目录仍然合格；
    - 读：先 lstat 拒绝链接，打开后用 fstat 的 (st_dev, st_ino) 核对没被换掉；
    - 写：排他创建（O_EXCL），写完落盘；整体替换用同目录临时文件 + `os.replace`；
    - Windows 上目录不能单独落盘，也没有原子对调，`swap_in` 走 root→root.old、new→root 两步（中断由
      `store.recover_interrupted_swaps` 恢复，与 POSIX 不支持对调时相同）。
    """

    def __init__(self, path: str | os.PathLike[Any], purpose: str, forbidden: Iterable[str | os.PathLike[Any]] = ()) -> None:
        self.purpose = purpose
        self.forbidden = [Path(f) for f in forbidden]
        self.real = check_private_dir(path, purpose, self.forbidden)
        self.fd = -1
        try:
            self._ensure(self.real)
        except OSError as exc:
            raise PrivatePathError("private_path_changed", f"{purpose}: 无法按不跟随链接的方式打开 {self.real}（{exc.strerror}）") from exc
        self.verify()

    def _ensure(self, real: Path) -> None:
        cur = Path(real.anchor)
        for part in real.parts[1:]:
            cur = cur / part
            try:
                st = os.lstat(cur)
            except FileNotFoundError:
                os.mkdir(cur, 0o700)
                continue
            if osplat.is_link(st) or not stat.S_ISDIR(st.st_mode):
                raise PrivatePathError("private_path_changed", f"{self.purpose}: {cur} 是链接或不是目录")

    def close(self) -> None:
        self.fd = -1

    def _path_verify(self) -> None:
        try:
            st = os.lstat(self.real)
        except OSError:
            st = None
        if st is None or not stat.S_ISDIR(st.st_mode) or _real(self.real) != self.real or _chain_has_link(self.real) is not None:
            raise PrivatePathError("private_path_changed", f"{self.purpose}: 目录在检查之后变了（{self.real}）")
        check_private_dir(self.real, self.purpose, self.forbidden)

    def _path_check_fd(self, fd: int) -> None:
        self.verify()

    def _path(self, rel: str, create: bool = False) -> Path:
        parts = _parts(rel)
        cur = self.real
        for part in parts[:-1]:
            cur = cur / part
            try:
                st = os.lstat(cur)
            except FileNotFoundError:
                if not create:
                    raise
                self.verify()
                try:
                    os.mkdir(cur, 0o700)
                except FileNotFoundError as exc:
                    raise self._parent_gone(cur, exc) from exc
                continue
            # 与 POSIX 的 O_DIRECTORY|O_NOFOLLOW 打开失败相同：链接是 ELOOP，不是目录是 ENOTDIR
            if osplat.is_link(st):
                raise OSError(errno.ELOOP, "路径里有链接", str(cur))
            if not stat.S_ISDIR(st.st_mode):
                raise NotADirectoryError(errno.ENOTDIR, "不是目录", str(cur))
        return cur / parts[-1]

    def _parent_gone(self, path: Path, exc: OSError) -> OSError | PrivatePathError:
        """按路径操作报"找不到"时：父目录在核对之后被移走了（不是文件本来就没有），就按"目录变了"报，
        与按 fd 的实现拿着已移走目录的 fd 时一样。"""
        try:
            st = os.lstat(path.parent)
            if stat.S_ISDIR(st.st_mode) and not osplat.is_link(st):
                return exc
        except OSError:
            pass
        return PrivatePathError("private_path_changed", f"{self.purpose}: {path.parent} 在检查之后被移走了")

    def _opened_here(self, path: Path, fd: int) -> None:
        """打开以后核对：系统报告的这个文件的真实位置就是我们核对过的路径。中间某一级在 lstat 和打开之间被换成
        链接或目录联接，打开会跟着走到别处；这里按 fd 的真实位置拦下（Windows 用 GetFinalPathNameByHandleW）。"""
        now = osplat.fd_path(fd)
        if now is None or os.path.normcase(str(_real(Path(now)))) != os.path.normcase(str(path)):
            raise PrivatePathError("private_path_changed", f"{self.purpose}: {path} 打开后实际在 {now}")

    def _path_exists(self, rel: str) -> bool:
        try:
            os.lstat(self._path(rel))
            return True
        except FileNotFoundError:
            return False

    def _path_names(self, rel: str) -> list[str]:
        folder = self._path(f"{rel}/.entries").parent
        self.verify()
        if osplat.is_link(os.lstat(folder)):
            raise OSError(errno.ELOOP, "路径里有链接", str(folder))
        return os.listdir(folder)

    def _path_read(self, rel: str) -> bytes:
        path = self._path(rel)
        self.verify()
        try:
            before = os.lstat(path)
            if osplat.is_link(before):
                raise OSError(errno.ELOOP, "是链接", str(path))
            fd = os.open(path, os.O_RDONLY | osplat.binary_flag())
        except FileNotFoundError as exc:
            raise self._parent_gone(path, exc) from exc
        with os.fdopen(fd, "rb") as fh:
            after = os.fstat(fh.fileno())
            if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
                raise PrivatePathError("private_path_changed", f"{self.purpose}: {path} 在读之前被换掉了")
            self._opened_here(path, fh.fileno())
            return fh.read()

    def _path_write(self, rel: str, data: bytes) -> None:
        self.verify()
        path = self._path(rel, create=True)
        self.verify()
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | osplat.binary_flag(), 0o600)
        except FileNotFoundError as exc:
            raise self._parent_gone(path, exc) from exc
        try:
            try:
                view = memoryview(data)
                while view:
                    self._still_here(path, fd)  # 每次写之前、写之后都核对：目录还在原处，这个路径还是我们打开的文件
                    n = os.write(fd, view)
                    if n <= 0:
                        raise OSError("write returned 0")
                    view = view[n:]
                    self._still_here(path, fd)
                _full_sync(fd)
                self._still_here(path, fd)
            except PrivatePathError as exc:
                raise self._moved_while_writing(fd, exc) from exc
        finally:
            os.close(fd)

    def _still_here(self, path: Path, fd: int) -> None:
        """按 fd 的实现核对父目录 fd；按路径时核对这个路径现在指向的仍是我们打开的文件（中间哪一级被移走都会变），
        而且这个文件的真实位置就是这个路径（中间一级被换成链接、目录联接也会变）。"""
        self.verify()
        self._opened_here(path, fd)
        try:
            st = os.lstat(path)
        except OSError:
            st = None
        now = os.fstat(fd)
        if st is None or (st.st_dev, st.st_ino) != (now.st_dev, now.st_ino):
            raise PrivatePathError("private_path_changed", f"{self.purpose}: {path} 所在的目录在写的过程中被移走了")

    def _path_replace_file(self, rel: str, data: bytes) -> None:
        parts = _parts(rel)
        tmp = "/".join([*parts[:-1], f".{parts[-1]}.{secrets.token_hex(6)}.tmp"])
        try:
            self.write(tmp, data)
            self.verify()
            target = self._path(rel)
            try:
                os.replace(self._path(tmp), target)
            except FileNotFoundError as exc:
                raise self._parent_gone(target, exc) from exc
        except BaseException:
            self.remove(tmp)
            raise

    def _path_remove(self, rel: str) -> None:
        self.verify()
        try:
            path = self._path(rel)
        except FileNotFoundError:
            return
        try:
            st = os.lstat(path)
        except FileNotFoundError as exc:
            gone = self._parent_gone(path, exc)
            if isinstance(gone, PrivatePathError):
                raise gone from exc
            return
        if osplat.is_link(st):
            # 链接本身删掉，不跟随（Windows 的目录联接要用 rmdir）
            if stat.S_ISDIR(st.st_mode):
                os.rmdir(path)
            else:
                os.unlink(path)
        elif stat.S_ISDIR(st.st_mode):
            shutil.rmtree(path)
        else:
            os.unlink(path)

    def _path_rename(self, old: str, new: str) -> None:
        if "/" in old or "/" in new:
            raise PrivatePathError("private_name_invalid", f"只能在这一层改名：{old} → {new}")
        self.verify()
        os.rename(self._path(old), self._path(new))

    def _path_swap_in(self, new: str, root: str) -> None:
        _parts(new), _parts(root)
        self.verify()
        if not self.exists(root):
            os.rename(self._path(new), self._path(root))
            return
        old = root + ".old"
        self.remove(old)
        os.rename(self._path(root), self._path(old))
        os.rename(self._path(new), self._path(root))
        self.remove(old)


def private_write(
    directory: str | os.PathLike[Any], name: str, data: bytes, purpose: str, forbidden: Iterable[str | os.PathLike[Any]] = ()
) -> Path:
    """把私有字节原子写进 `directory/name`（按已核对的目录 fd）。检查之后目录被换成链接或换了位置：拒绝，不留半个文件。"""
    if not name or "/" in name or name in (".", ".."):
        raise PrivatePathError("private_name_invalid", f"{purpose}: 文件名不合法")
    with PrivateDir(directory, purpose, forbidden) as d:
        d.replace_file(name, data)
        return d.real / name


def _full_sync(fd: int) -> None:
    osplat.full_fsync(fd)


def startup_check(settings: Any) -> dict[str, str | None]:
    """启动时核对应用数据和导出目录。不挡启动（JPEG 导出不是私有字节），只记日志；真正的拒绝在每次写私有字节之前。"""
    out: dict[str, str | None] = {}
    for purpose, path in (("home_dir", settings.home_dir), ("export_dir", settings.export_dir)):
        try:
            check_private_dir(path, purpose, forbidden_for(settings))
            out[purpose] = None
        except PrivatePathError as exc:
            out[purpose] = exc.code
            log.event(30, "private_dir_rejected", purpose=purpose, code=exc.code)
    return out
