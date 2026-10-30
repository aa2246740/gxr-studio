"""进程内告警。任务失败时由 register_alert_service 登记的处理函数送出。

本地工具没有值班电话。告警写进内存列表，打一条打过码的 JSON 日志，
并从 GET /api/alerts 读出来。
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Callable

from .jobs import Job, JobRegistry
from .observe import log, redact


class StudioNotifier:
    def __init__(self, limit: int = 100) -> None:
        self._sent: deque[dict] = deque(maxlen=limit)

    def deliver(self, alert: dict) -> None:
        item = {key: redact(value) if isinstance(value, str) else value for key, value in alert.items()}
        self._sent.append(item)
        log.event(logging.ERROR, "alert", notifier="StudioNotifier", **item)

    def recent(self) -> list[dict]:
        return list(self._sent)


def on_job_failed(job: Job, notifier: StudioNotifier) -> None:
    """Alert rule: a failed background job is delivered by StudioNotifier."""
    notifier.deliver(
        {
            "handler": "on_job_failed",
            "job_id": job.id,
            "job_kind": job.kind,
            "error": job.error or "",
        }
    )


def register_alert_service(registry: JobRegistry, notifier: StudioNotifier) -> None:
    """Service registration: the running studio binds on_job_failed for the process lifetime."""
    registry.set_alert_handler(lambda job: on_job_failed(job, notifier))


Handler = Callable[[Job], None]
