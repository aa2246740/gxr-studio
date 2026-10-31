"""同一个图库只允许一个工作室在跑：启动时在 `.studio/studio.lock` 上拿一把 flock。

进程退出（正常、被杀、崩溃）系统会自己放掉这把锁，不会留下"锁死"的状态。
为什么要锁：`Library.open()` 会清空 `.studio/tmp`，导入开头会清空 `.studio/staging`，两个实例同时跑会互相删对方正在写的临时文件。
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Iterable

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
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise LibraryBusy(str(self.path)) from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self) -> "LibraryLock":
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()
