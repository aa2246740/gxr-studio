"""后台任务：一个任务一个线程，同一种任务同时只跑一个；前端轮询 `/api/jobs/{id}`。"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable


class Job:
    def __init__(self, kind: str, progress: Any):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.progress = progress  # 有 to_dict() 的对象，或普通 dict
        self.cancel = threading.Event()
        self.error: str | None = None
        self.started = time.time()
        self.thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def to_dict(self) -> dict:
        prog = self.progress.to_dict() if hasattr(self.progress, "to_dict") else dict(self.progress)
        return {"id": self.id, "kind": self.kind, "running": self.running, "error": self.error, "progress": prog}


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._alert: Callable[[Job], None] | None = None

    def set_alert_handler(self, handler: Callable[[Job], None] | None) -> None:
        """运行中的进程登记失败告警。传 None 表示卸掉。"""
        self._alert = handler

    def active(self, kind: str) -> Job | None:
        with self._lock:
            return next((j for j in self._jobs.values() if j.kind == kind and j.running), None)

    def start(self, kind: str, progress: Any, fn: Callable[[Job], None]) -> Job:
        job = Job(kind, progress)

        def runner() -> None:
            try:
                fn(job)
            except BaseException as exc:  # 记下来，别让线程静悄悄地死掉
                job.error = f"{type(exc).__name__}: {exc}"
                handler = self._alert
                if handler is not None:
                    handler(job)

        job.thread = threading.Thread(target=runner, name=f"job-{kind}-{job.id}", daemon=True)
        with self._lock:
            self._jobs[job.id] = job
        job.thread.start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.started, reverse=True)
