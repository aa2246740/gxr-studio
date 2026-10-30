"""请求号、结构化日志、进程内指标和本地功能使用计数。

不联网。计数只活在这个进程里，给 `/api/metrics` 和 `/api/usage` 看。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from contextvars import ContextVar
from typing import Any

request_id: ContextVar[str] = ContextVar("request_id", default="")

_BEARER = re.compile(r"(?i)(bearer\s+)\S+")
_TOKEN_QUERY = re.compile(r"(?i)(token=)[^&\s]+")
_GITHUB_TOKEN = re.compile(r"(?:ghp_|gho_|github_pat_)[A-Za-z0-9_]+")


def redact(text: str) -> str:
    """去掉日志里的令牌和常见密钥形状。"""
    text = _BEARER.sub(r"\1[redacted]", text)
    text = _TOKEN_QUERY.sub(r"\1[redacted]", text)
    return _GITHUB_TOKEN.sub("[redacted]", text)


def _route(path: str) -> str:
    parts: list[str] = []
    for part in path.split("/"):
        if re.fullmatch(r"[0-9a-fA-F]{8,}", part) or part.isdigit():
            parts.append(":id")
        else:
            parts.append(part)
    return "/".join(parts)


class StudioLog:
    """一行一个 JSON 对象，字符串字段先打码。"""

    def __init__(self, name: str = "gxr_studio") -> None:
        self._log = logging.getLogger(name)
        self._log.setLevel(logging.INFO)
        self._log.propagate = False
        if not self._log.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._log.addHandler(handler)

    def event(self, level: int, event: str, **fields: object) -> None:
        payload: dict[str, object] = {"event": event, "request_id": request_id.get()}
        for key, value in fields.items():
            payload[key] = redact(value) if isinstance(value, str) else value
        self._log.log(level, json.dumps(payload, ensure_ascii=False, default=str))


log = StudioLog()


class Metrics:
    def __init__(self) -> None:
        self.requests = 0
        self.by_status: dict[str, int] = {}
        self.features: dict[str, int] = {}

    def observe(self, method: str, path: str, status: int) -> None:
        self.requests += 1
        key = str(status)
        self.by_status[key] = self.by_status.get(key, 0) + 1
        if path.startswith("/api/"):
            feature = f"{method} {_route(path)}"
            self.features[feature] = self.features.get(feature, 0) + 1

    def usage(self) -> dict[str, object]:
        return {"requests": self.requests, "features": dict(self.features)}

    def render(self) -> str:
        lines = [
            "# HELP gxr_http_requests_total HTTP requests observed by this process.",
            "# TYPE gxr_http_requests_total counter",
            f"gxr_http_requests_total {self.requests}",
        ]
        lines.append("# TYPE gxr_http_responses_total counter")
        for status, count in sorted(self.by_status.items()):
            lines.append(f'gxr_http_responses_total{{status="{status}"}} {count}')
        lines.append("# TYPE gxr_feature_uses_total counter")
        for feature, count in sorted(self.features.items()):
            escaped = feature.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'gxr_feature_uses_total{{route="{escaped}"}} {count}')
        return "\n".join(lines) + "\n"


class ObserveMiddleware:
    """内层中间件：过了访问控制的请求才会计数，并带上 X-Request-ID。"""

    def __init__(self, app: Any, metrics: Metrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Any, receive: Any, send: Any) -> Any:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        rid = uuid.uuid4().hex[:16]
        token = request_id.set(rid)
        status = {"code": 500}

        async def send_wrapper(message: Any) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                headers = [*message["headers"], (b"x-request-id", rid.encode())]
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            self.metrics.observe(scope.get("method", "GET"), scope.get("path", "/"), status["code"])
            request_id.reset(token)
