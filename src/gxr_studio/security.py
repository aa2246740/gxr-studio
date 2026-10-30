"""本机服务的访问控制：令牌 + Host 校验 + Origin 校验（ARCHITECTURE §2.2）。

- 令牌：启动时生成；打开 `/?token=…` 会换成 HttpOnly + SameSite=Strict 的 cookie 并跳回 `/`；
  API 也接受 `Authorization: Bearer` 或 `X-GXR-Token`；
- Host：只认 127.0.0.1 / localhost 加本服务的端口（挡 DNS 重绑定）；
- Origin：带了就必须是本服务自己；没带 Origin 的写操作必须用头部令牌，不能只靠 cookie（挡跨站）。
"""

from __future__ import annotations

import hmac
import json
from typing import Any
from urllib.parse import parse_qs

from .config import Settings

COOKIE = "gxr_token"
# 页面只加载自己的脚本/样式；图片还要放行 blob:（预览图是 fetch 之后用 object URL 显示的）和 data:
CSP = "default-src 'self'; img-src 'self' blob: data:; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class SecurityMiddleware:
    def __init__(self, app: Any, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    def _origins(self) -> set[str]:
        return {f"http://{h}:{self.settings.port}" for h in self.settings.allowed_hostnames}

    def _host_ok(self, host: str) -> bool:
        if not host:
            return False
        name, sep, port = host.rpartition(":")
        if not sep or not port.isdigit():
            return False
        return name in self.settings.allowed_hostnames and int(port) == self.settings.port

    def _token_ok(self, candidate: str | None) -> bool:
        if not candidate:
            return False
        return hmac.compare_digest(candidate.encode(), self.settings.token.encode())

    async def __call__(self, scope: Any, receive: Any, send: Any) -> Any:
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        method = scope.get("method", "GET")

        async def deny(status: int, code: str, message: str) -> Any:
            if scope["type"] == "websocket":
                return await send({"type": "websocket.close", "code": 1008})
            body = json.dumps({"error": code, "message": message}, ensure_ascii=False).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        (b"content-type", b"application/json; charset=utf-8"),
                        (b"content-length", str(len(body)).encode()),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})

        if not self._host_ok(headers.get("host", "")):
            return await deny(403, "host_not_allowed", "无法打开页面。请用启动时显示的链接。")
        origin = headers.get("origin")
        if origin is not None and origin not in self._origins():
            return await deny(403, "origin_not_allowed", "无法完成操作。请用启动时显示的链接。")
        if headers.get("sec-fetch-site") == "cross-site":
            return await deny(403, "origin_not_allowed", "无法完成操作。请从本页面重试。")

        # 令牌
        cookies = {}
        for part in headers.get("cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k:
                cookies[k] = v
        auth = headers.get("authorization", "")
        header_token = headers.get("x-gxr-token") or (auth[7:] if auth.lower().startswith("bearer ") else None)

        if scope["type"] == "http" and scope["path"] == "/" and method == "GET":
            query = parse_qs(scope.get("query_string", b"").decode())
            if self._token_ok((query.get("token") or [None])[0]):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 303,
                        "headers": [
                            (b"location", b"/"),
                            (
                                b"set-cookie",
                                f"{COOKIE}={self.settings.token}; HttpOnly; SameSite=Strict; Path=/".encode(),
                            ),
                            (b"cache-control", b"no-store"),
                            (b"content-length", b"0"),
                        ],
                    }
                )
                return await send({"type": "http.response.body", "body": b""})

        if method not in SAFE_METHODS and origin is None:
            if not self._token_ok(header_token):
                return await deny(403, "origin_required", "无法完成操作。请用启动时显示的链接。")
        if not (self._token_ok(header_token) or self._token_ok(cookies.get(COOKIE))):
            return await deny(401, "token_required", "无法打开页面。请用启动时显示的链接。")

        async def send_wrapper(message: Any) -> None:
            if message["type"] == "http.response.start":
                extra = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"content-security-policy", CSP.encode()),
                ]
                has_cache = any(k.lower() == b"cache-control" for k, _ in message["headers"])
                if scope["path"].startswith("/api/") and not has_cache:
                    extra.append((b"cache-control", b"no-store"))
                message = {**message, "headers": [*message["headers"], *extra]}
            await send(message)

        return await self.app(scope, receive, send_wrapper)
