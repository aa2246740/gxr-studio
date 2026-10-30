"""可选的 Sentry 上报。没设置 GXR_STUDIO_SENTRY_DSN 时不初始化，也不发任何请求。

本地告警（alerts.py）照常工作。这里只在任务失败时多送一份打过码的事件。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from . import __version__
from .jobs import Job
from .observe import log, redact, request_id

_DSN = "GXR_STUDIO_SENTRY_DSN"
_ENVIRONMENT = "GXR_STUDIO_SENTRY_ENVIRONMENT"
_RELEASE = "GXR_STUDIO_SENTRY_RELEASE"

# 卷名可以含空格（/Volumes/NO NAME），一直吃到 DCIM、MISC 或字符串结尾。
_VOLUME = re.compile(r"(?i)(?:/Volumes/.+?(?=/DCIM\b|/MISC\b|$)|/(?:run/)?media/[^/]+/[^/\s]+|/mnt/[^/\s]+)")
_CARD = re.compile(r"(?i)\b(?:DCIM|MISC)(?:/[^\s\"']+)*")
_FILENAME = re.compile(r"(?i)[^/\\\s\"']+\.(?:dng|dat|jpg|jpeg|tif|tiff|png|npz|cube|json|txt|bin)\b")
_USERINFO = re.compile(r"(?i)(https?://)[^/\s@]+@")
_LONG_BLOB = re.compile(r"(?:[A-Za-z0-9+/]{200,}={0,2}|[0-9a-fA-F]{128,})")


def scrub_text(text: str) -> str:
    """家目录、卷、卡路径、文件名、令牌和看起来像文件内容的长串换成占位符。"""
    text = redact(text)
    text = _USERINFO.sub(r"\1<redacted>@", text)
    home = str(Path.home())
    if home not in ("", "/"):
        text = text.replace(home, "<home>")
    text = text.replace("~/", "<home>/")
    text = _VOLUME.sub("<volume>", text)
    text = _CARD.sub("<card>", text)
    text = _FILENAME.sub("<file>", text)
    return _LONG_BLOB.sub("<redacted>", text)


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, bytes):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(key): _scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub(item) for item in value)
    return value


def before_send(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
    """不送请求体、用户信息和图像/DNG/DAT 内容。路径在剩下的字符串里打码。"""
    event.pop("request", None)
    event.pop("user", None)
    scrubbed = _scrub(event)
    return scrubbed if isinstance(scrubbed, dict) else event


def enabled() -> bool:
    try:
        import sentry_sdk
    except ImportError:
        return False
    return bool(sentry_sdk.get_client().is_active())


def init_error_tracking(*, transport: Any = None) -> bool:
    """DSN 为空就返回 False，不调用 sentry_sdk.init。transport 只给测试替换掉网络发送。"""
    dsn = os.environ.get(_DSN, "").strip()
    if not dsn:
        return False
    import sentry_sdk

    release = os.environ.get(_RELEASE, "").strip() or f"gxr-studio@{__version__}"
    environment = os.environ.get(_ENVIRONMENT, "").strip() or "local"
    send_hook: Any = before_send  # sentry 的 Event 是更窄的 TypedDict，打码函数收的是普通 dict
    try:
        sentry_sdk.init(
            dsn=dsn,
            release=release,
            environment=environment,
            send_default_pii=False,
            max_request_body_size="never",
            include_local_variables=False,
            auto_session_tracking=False,
            default_integrations=False,
            before_send=send_hook,
            transport=transport,
        )
    except Exception as exc:
        log.event(logging.ERROR, "sentry_init_failed", error=type(exc).__name__)
        return False
    sentry_sdk.set_tag("app_version", __version__)
    return True


def shutdown_error_tracking() -> None:
    if not enabled():
        return
    import sentry_sdk

    sentry_sdk.get_client().close()


def report_job_failure(job: Job) -> None:
    """失败任务多送一份事件。没初始化时什么都不做，本地告警不受影响。"""
    if not enabled():
        return
    import sentry_sdk

    rid = job.request_id or request_id.get()
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("request_id", rid)
        scope.set_tag("job_id", job.id)
        scope.set_tag("job_kind", job.kind)
        scope.set_tag("app_version", __version__)
        scope.set_context("job", {"id": job.id, "kind": job.kind, "request_id": rid})
        sentry_sdk.capture_message(scrub_text(job.error or "job failed"), level="error")
