"""在访达中显示：只打开这次导出记下的文件，测试里不真的启动访达。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import PORT
from gxr_studio.app import create_app
from gxr_studio.export import RevealError, reveal_export
from test_api import find, import_all


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        yield c


def _fake_open(monkeypatch):
    calls: list[list[str]] = []

    def run(args, **_kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr("gxr_studio.export.subprocess.run", run)
    return calls


def test_reveal_opens_only_the_file_this_export_wrote(client, card, monkeypatch):
    calls = _fake_open(monkeypatch)
    import_all(client)
    photo = find(client, "R0000102")
    exported = client.post(f"/api/photos/{photo['id']}/export", json={"long_edge": 320, "quality": 80})
    assert exported.status_code == 200, exported.text
    body = exported.json()
    opened = client.post(f"/api/exports/{body['export_id']}/reveal", json={"path": "/etc/passwd"})
    assert opened.status_code == 200, opened.text
    assert calls == [["open", "-R", str(Path(body["path"]).resolve())]]
    assert opened.json()["path"] == str(Path(body["path"]).resolve())
    assert client.post("/api/exports/deadbeefdead/reveal").status_code == 404


def test_reveal_of_a_batch_uses_the_first_exported_file(client, card, monkeypatch):
    calls = _fake_open(monkeypatch)
    import_all(client)
    first = find(client, "R0000102")
    second = find(client, "R0000101")
    job = client.post("/api/export", json={"ids": [first["id"], second["id"]], "long_edge": 200, "quality": 70})
    assert job.status_code == 202, job.text
    job_id = job.json()["id"]
    for _ in range(200):
        current = client.get(f"/api/jobs/{job_id}").json()
        if not current["running"]:
            break
    else:
        raise AssertionError("导出超时")
    paths = [item["path"] for item in current["progress"]["results"] if item.get("path")]
    assert len(paths) == 2
    opened = client.post(f"/api/exports/{job_id}/reveal")
    assert opened.status_code == 200, opened.text
    assert calls == [["open", "-R", str(Path(paths[0]).resolve())]]
    assert Path(opened.json()["path"]) == Path(paths[0]).resolve()
    assert opened.json()["path"] != str(Path(paths[1]).resolve())


def test_reveal_requires_the_file_and_macos(client, card, monkeypatch):
    calls = _fake_open(monkeypatch)
    import_all(client)
    photo = find(client, "R0000102")
    exported = client.post(f"/api/photos/{photo['id']}/export", json={})
    assert exported.status_code == 200, exported.text
    body = exported.json()
    assert Path(body["path"]).is_file(), body
    Path(body["path"]).unlink()
    missing = client.post(f"/api/exports/{body['export_id']}/reveal")
    assert missing.status_code == 404 and missing.json()["error"] == "export_file_missing"
    assert calls == []
    again = client.post(f"/api/photos/{photo['id']}/export", json={}).json()
    monkeypatch.setattr("gxr_studio.export.sys.platform", "linux")
    refused = client.post(f"/api/exports/{again['export_id']}/reveal")
    assert refused.status_code == 501 and refused.json()["error"] == "finder_unavailable"
    assert calls == []


def test_reveal_refuses_a_file_outside_the_export_directory(tmp_path):
    root = tmp_path / "exports"
    root.mkdir()
    outside = tmp_path / "other.jpg"
    outside.write_bytes(b"nope")
    with pytest.raises(RevealError) as caught:
        reveal_export(root, outside)
    assert caught.value.status == 404 and caught.value.code == "export_file_missing"
