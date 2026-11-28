"""成片出图的取消：发请求的页面已经走了（刷新、换照片、中止了 fetch），还在算的图在下一个安全点停下。

安全点是条带循环和各阶段之间的 `checkpoint()`。停下时抛 `RenderCancelled`，调用栈上的数组随之释放；
取消的图不进缓存，也不会当成一张图返回。没有登记取消条件的调用（导出、缩略图、直接调用）里 `checkpoint()` 什么都不做。

`Flights`：同一个成片键同时只算一次。键相同的请求在算的时候进来（例如算到一半刷新了页面），就等那一次算完拿它的字节，
不再从头算一遍；所有等它的请求都走了才在安全点停下。算失败时每个等的请求都按同样的错误失败。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

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

    def client_gone(self) -> bool:
        """不节流地问一次登记的客户端走了没有（`Flights` 的领头请求用，外面已经有节流）。"""
        return self.cancelled or bool(self._gone())


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


class _Flight:
    """一次在算的成片：`waiters` 是还在等它的请求数（含领头的那个，只要它的客户端还在）。"""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.waiters = 1
        self.closed = False  # 已经停下或算完：新请求不再加入，自己从头算
        self.result: Any = None
        self.error: BaseException | None = None


def _clone(exc: BaseException) -> BaseException:
    """每个等的请求拿一份自己的异常（类型、参数和属性一样，不带算图线程的调用栈，不拉住算到一半的数组）。"""
    try:
        new = type(exc).__new__(type(exc), *exc.args)
        new.args = exc.args
        new.__dict__.update(getattr(exc, "__dict__", {}))
    except Exception:  # 构造不出来就用原来那个
        return exc
    return new if str(new) == str(exc) else exc


class Flights:
    """按成片键登记在算的出图。`run(key, work)`：没有在算的就自己算（领头），有就等它，返回 (结果, 是否等了别人的)。

    领头的请求用一张合起来的取消条件算：它自己的客户端走了只算少一个等的人；等的人数降到 0 才在下一个安全点停下。
    等的请求每 `PROBE_EVERY` 秒问一次自己的客户端，走了就退出（409，和今天一样），不影响别人。
    领头的线程在 finally 里一定会把结果或错误交出去，等的请求不会一直等下去。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._flights: dict[str, _Flight] = {}

    def running(self) -> int:
        with self._lock:
            return len(self._flights)

    def run(self, key: str, work: Callable[[], Any]) -> tuple[Any, bool]:
        with self._lock:
            flight = self._flights.get(key)
            if flight is not None and not flight.closed:
                flight.waiters += 1
            else:
                flight = None
                lead = self._flights[key] = _Flight()
        if flight is not None:
            return self._wait(flight), True
        return self._lead(key, lead, work), False

    def _leave(self, flight: _Flight) -> None:
        with self._lock:
            flight.waiters -= 1

    def _wait(self, flight: _Flight) -> Any:
        ticket = _TICKET.get()
        try:
            while not flight.done.wait(PROBE_EVERY):
                if ticket is not None:
                    ticket.check()  # 自己的客户端走了：抛 RenderCancelled
        finally:
            self._leave(flight)
        if flight.error is not None:
            raise _clone(flight.error)
        return flight.result

    def _lead(self, key: str, flight: _Flight, work: Callable[[], Any]) -> Any:
        outer = _TICKET.get()
        left = False

        def nobody_waits() -> bool:
            nonlocal left
            if not left and outer is not None and outer.client_gone():
                left = True
                self._leave(flight)
            with self._lock:
                if flight.waiters <= 0:
                    flight.closed = True  # 在锁里定下来：之后进来的请求自己从头算
                return flight.closed

        token = _TICKET.set(Ticket(nobody_waits) if outer is not None else None)
        try:
            flight.result = work()
            return flight.result
        except BaseException as exc:
            flight.error = _clone(exc)
            raise
        finally:
            _TICKET.reset(token)
            with self._lock:
                flight.closed = True
                if self._flights.get(key) is flight:
                    del self._flights[key]
            flight.done.set()
