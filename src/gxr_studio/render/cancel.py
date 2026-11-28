"""成片出图的取消：发请求的页面已经走了（刷新、换照片、中止了 fetch），还在算的图在下一个安全点停下。

安全点是条带循环和各阶段之间的 `checkpoint()`。停下时抛 `RenderCancelled`，调用栈上的数组随之释放；
取消的图不进缓存，也不会当成一张图返回。没有登记取消条件的调用（导出、缩略图、直接调用）里 `checkpoint()` 什么都不做。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

PROBE_EVERY = 0.2  # 秒：两次询问"客户端还在吗"之间至少隔这么久，条带循环里调用也几乎不花时间


class RenderCancelled(Exception):
    """这张图已经没人等了：在安全点停下。"""


class Ticket:
    def __init__(self, gone: Callable[[], bool], every: float = PROBE_EVERY) -> None:
        self._gone = gone
        self._every = every
        self._next = 0.0
        self.cancelled = False

    def check(self) -> None:
        if not self.cancelled:
            now = time.monotonic()
            if now < self._next:
                return
            self._next = now + self._every
            self.cancelled = bool(self._gone())
        if self.cancelled:
            raise RenderCancelled()


_TICKET: ContextVar[Ticket | None] = ContextVar("gxr_render_ticket", default=None)


def checkpoint() -> None:
    """安全点：这次出图登记过的客户端已经走了就抛 `RenderCancelled`。一旦取消，之后每个安全点都抛。"""
    ticket = _TICKET.get()
    if ticket is not None:
        ticket.check()


@contextmanager
def cancel_when(gone: Callable[[], bool]) -> Iterator[Ticket]:
    """在这个 with 里（同一线程）出图时，`gone()` 返回真就在下一个安全点停下。"""
    ticket = Ticket(gone)
    token = _TICKET.set(ticket)
    try:
        yield ticket
    finally:
        _TICKET.reset(token)
