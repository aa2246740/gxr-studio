"""从空图库拉起应用，确认健康检查和设置接口。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import PORT
from gxr_studio.app import create_app


def test_health_and_settings_round_trip(settings):
    app = create_app(settings)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        health = client.get("/api/health")
        info = client.get("/api/settings")
    assert health.status_code == 200
    assert health.json()["ok"] is True
    assert info.status_code == 200
    assert info.json()["library_dir"] == str(settings.library_dir)
    assert info.json()["photo_count"] == 0
