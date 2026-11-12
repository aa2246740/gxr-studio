"""可选的 Sentry：默认不初始化、不联网。有 DSN 时用假 transport 看标签和打码。"""

from __future__ import annotations

from pathlib import Path

import sentry_sdk

from gxr_studio.jobs import Job
from gxr_studio.observe import request_id
from gxr_studio.tracking import (
    before_send,
    enabled,
    init_error_tracking,
    report_job_failure,
    scrub_text,
    shutdown_error_tracking,
)


class _Capture(sentry_sdk.Transport):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, object]] = []

    def capture_envelope(self, envelope: object) -> None:
        event = envelope.get_event()  # type: ignore[attr-defined]
        if event is not None:
            self.events.append(event)


def _message(event: dict[str, object]) -> str:
    if isinstance(event.get("message"), str):
        return str(event["message"])
    logentry = event.get("logentry")
    if isinstance(logentry, dict):
        text = logentry.get("message") or logentry.get("formatted") or ""
        return str(text)
    return ""


def test_sentry_stays_off_without_a_dsn(monkeypatch):
    monkeypatch.delenv("GXR_STUDIO_SENTRY_DSN", raising=False)
    shutdown_error_tracking()
    assert init_error_tracking() is False
    assert enabled() is False
    report_job_failure(Job("import", {}))


def test_sentry_event_carries_request_and_job_tags(monkeypatch):
    shutdown_error_tracking()
    monkeypatch.setenv("GXR_STUDIO_SENTRY_DSN", "https://public@example.invalid/1")
    monkeypatch.setenv("GXR_STUDIO_SENTRY_ENVIRONMENT", "test")
    monkeypatch.setenv("GXR_STUDIO_SENTRY_RELEASE", "gxr-studio@test")
    transport = _Capture()
    assert init_error_tracking(transport=transport) is True
    token = request_id.set("req-0123456789abcd")
    try:
        job = Job("import", {})
        job.error = "boom"
        report_job_failure(job)
        sentry_sdk.flush(timeout=2)
        options = sentry_sdk.get_client().options
    finally:
        request_id.reset(token)
        shutdown_error_tracking()
    assert transport.events
    event = transport.events[0]
    tags = event["tags"]
    assert isinstance(tags, dict)
    assert tags["request_id"] == "req-0123456789abcd"
    assert tags["job_id"] == job.id
    assert tags["job_kind"] == "import"
    assert tags["app_version"] == "1.0.0"
    assert event["environment"] == "test"
    assert event["release"] == "gxr-studio@test"
    assert options["send_default_pii"] is False


def test_sentry_scrubs_home_volume_card_and_filename():
    home = Path.home()
    raw = f"read {home}/Pictures/GXR Studio/R1.DNG token=sekret /Volumes/NO NAME/DCIM/100RICOH/A.JPG https://secret-key@example.invalid/1 " + (
        "A" * 80 + "B" * 80 + "C" * 50
    )
    text = scrub_text(raw)
    assert str(home) not in text
    assert "R1.DNG" not in text and "A.JPG" not in text
    assert "sekret" not in text and "secret-key" not in text
    assert "/Volumes" not in text and "DCIM" not in text
    assert "<home>" in text and "<volume>" in text and "<card>" in text and "<file>" in text
    assert "<redacted>" in text
    event = before_send({"message": raw, "request": {"data": "pixels"}, "user": {"ip": "1"}}, {})
    assert "request" not in event and "user" not in event
    assert "R1.DNG" not in str(event["message"])
