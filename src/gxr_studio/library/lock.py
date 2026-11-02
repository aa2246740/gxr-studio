"""同一个图库只允许一个工作室在跑：启动时在 `.studio/studio.lock` 上拿一把进程级独占锁（POSIX flock，Windows msvcrt.locking，见 `osplat`）。

进程退出（正常、被杀、崩溃）系统会自己放掉这把锁，不会留下"锁死"的状态。
为什么要锁：`Library.open()` 会清空 `.studio/tmp`，导入开头会清空 `.studio/staging`，两个实例同时跑会互相删对方正在写的临时文件。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from .. import osplat
from .location import check_library_location

BUSY_EXIT_CODE = 75  # EX_TEMPFAIL：给 `GXR Studio.command` 看，不再多打一句"工作室已停止"


class LibraryBusy(Exception):
    """这个图库已经有一个工作室开着了。"""


class LibraryLock:
    def __init__(self, library_dir: Path, card_roots: Iterable[str | os.PathLike[str]] = ()) -> None:
        self.library_dir = Path(library_dir)
        self.card_roots = list(card_roots)
        self.path = self.library_dir / ".studio" / "studio.lock"
        self._fd: int | None = None

    def acquire(self) -> "LibraryLock":
        # 这是启动时第一次写图库：建 .studio、建锁文件之前先核对图库的真实位置（复审 A1a R3-P1-01）
        check_library_location(self.library_dir, self.card_roots)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | osplat.binary_flag(), 0o644)
        try:
            osplat.lock_exclusive_nb(fd)
        except OSError as exc:
            os.close(fd)
            raise LibraryBusy(str(self.path)) from exc
        # 进程号从第 0 字节写起：Windows 的字节锁只挡别的句柄对第 0 字节的读写，持锁的这个句柄自己可以写
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is not None:
            try:
                osplat.unlock(self._fd)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self) -> "LibraryLock":
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()
