"""开关、告警、指标、打码、索引查询次数、schema 文件。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from conftest import PORT
from gxr_studio.alerts import StudioNotifier, register_alert_service
from gxr_studio.app import create_app
from gxr_studio.flags import flag_enabled
from gxr_studio.jobs import JobRegistry
from gxr_studio.library.store import _SCHEMA
from gxr_studio.observe import redact


def test_redact_hides_bearer_query_and_github_tokens():
    text = redact("Authorization: Bearer super-secret token=abc ghp_" + "a" * 20)
    assert "super-secret" not in text
    assert "token=abc" not in text
    assert "ghp_" not in text
    assert "[redacted]" in text


def test_rebuild_flag_defaults_on_and_can_refuse(settings, tmp_path):
    assert flag_enabled(settings, "library_rebuild") is True
    (settings.home_dir).mkdir(parents=True, exist_ok=True)
    (settings.home_dir / "flags.json").write_text(json.dumps({"library_rebuild": False}), encoding="utf-8")
    assert flag_enabled(settings, "library_rebuild") is False
    app = create_app(settings)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        response = client.post("/api/library/rebuild")
    assert response.status_code == 403
    assert response.json()["error"] == "feature_disabled"


def test_failed_job_notifies_and_redacts():
    registry = JobRegistry()
    notifier = StudioNotifier()
    register_alert_service(registry, notifier)

    def boom(_job):
        raise RuntimeError("token=sekret")

    job = registry.start("import", {}, boom)
    job.thread.join(2)
    assert job.error is not None and "RuntimeError" in job.error
    assert notifier.recent()
    assert notifier.recent()[0]["handler"] == "on_job_failed"
    assert "sekret" not in json.dumps(notifier.recent())


def test_health_carries_request_id_and_counts_usage(settings):
    app = create_app(settings)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        health = client.get("/api/health")
        metrics = client.get("/api/metrics")
        usage = client.get("/api/usage")
    assert health.status_code == 200
    assert len(health.headers["x-request-id"]) == 16
    body = metrics.text
    assert "gxr_http_requests_total" in body
    assert "GET /api/health" in usage.json()["features"]


def test_schema_file_matches_the_ddl_the_library_executes():
    sql = Path("src/gxr_studio/library/schema.sql").read_text(encoding="utf-8")
    assert sql.strip() == _SCHEMA.strip()


def test_list_shots_uses_one_files_query(lib):
    for index in range(6):
        shot = f"shot{index:02d}"
        lib.db.execute(
            """INSERT INTO shots(shot_id, name, folder, date, date_source, captured_at, unit, lens,
                                  iso, orientation, width, height, has_raw, has_jpg, added_at)
               VALUES (?, ?, '2026/2026-10-30', '2026-10-30', 'exif', '2026-10-30T00:00:00',
                       '50mm', 'GR LENS', 200, 1, 4288, 2848, 1, 1, '2026-10-30T00:00:00')""",
            (shot, f"R{index:07d}"),
        )
        lib.db.execute(
            """INSERT INTO files(shot_id, kind, relpath, sha256, size, mtime_ns)
               VALUES (?, 'jpg', ?, ?, 10, 1)""",
            (shot, f"2026/2026-10-30/R{index:07d}.JPG", f"{index:064x}"),
        )
    log = lib.trace_queries()
    shots = lib.list_shots()
    assert len(shots) == 6
    file_queries = [sql for sql in log if "FROM files" in sql]
    edit_queries = [sql for sql in log if "FROM edits" in sql]
    assert len(file_queries) == 1
    assert len(edit_queries) == 1
    log.clear()
    dates = lib.dates()
    assert dates and dates[0]["count"] == 6
    assert len(log) == 1
