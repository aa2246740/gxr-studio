"""等后台任务的共用工具：按墙上时钟的期限轮询，不数循环次数。

GitHub 的 Windows 运行器很慢，"轮询 N 次、每次睡 50 毫秒"在那里会提前判超时，任务其实还在跑。
所以这里只看 `time.monotonic()`：期限给得宽（默认 120 秒），轮询间隔仍然很短，快的机器不会被拖慢。
真的卡死时仍然会失败，只是晚一点，并且报错里写明在等什么。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

# 慢速 CI（Windows 运行器跑完整套件要二十多分钟）也够用；真卡死最多多等这么久才失败。
WAIT_TIMEOUT = 120.0
POLL_INTERVAL = 0.05


def wait_until(
    probe: Callable[[], Any],
    what: str,
    *,
    timeout: float = WAIT_TIMEOUT,
    interval: float = POLL_INTERVAL,
) -> Any:
    """反复调用 `probe`，它返回真值就把这个值交回；超过 `timeout` 秒（墙上时钟）仍是假值就抛 AssertionError。

    至少会调用一次 `probe`；`what` 写进失败信息，说明是什么没等到。
    """
    deadline = time.monotonic() + timeout
    while True:
        value = probe()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"{what}超时：等了 {timeout:g} 秒仍未完成")
        time.sleep(interval)


def wait_job(
    client: Any,
    job_id: str,
    what: str = "任务",
    *,
    timeout: float = WAIT_TIMEOUT,
    interval: float = POLL_INTERVAL,
) -> dict[str, Any]:
    """等 `/api/jobs/<id>` 不再 running，返回最后一次的任务字典（不替调用方断言成败）。"""

    def finished() -> dict[str, Any] | None:
        job = client.get(f"/api/jobs/{job_id}").json()
        return None if job["running"] else job

    return wait_until(
        finished,
        f"{what}（任务 {job_id}）",
        timeout=timeout,
        interval=interval,
    )
