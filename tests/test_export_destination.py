"""导出文件夹选择器：系统对话框全部替换，不操作真实的访达。"""

from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

from conftest import PORT
from gxr_studio.app import create_app
from test_api import find, import_all

PICKER = "/api/export/destination"


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        yield client


@pytest.fixture
def chooser(monkeypatch):
    calls = []
    result = {"path": None, "error": None}

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if result["error"] is not None:
            raise result["error"]
        return subprocess.CompletedProcess(args, 0, stdout=f"{result['path'] or ''}\n", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    return result, calls


def test_picker_returns_selected_folder_and_passes_default_as_data(client, chooser, tmp_path):
    result, calls = chooser
    current = tmp_path / '旧目录 "引号" $(不执行)'
    chosen = tmp_path / "导出到这里"
    chosen.mkdir()
    result["path"] = chosen
    response = client.post(PICKER, json={"dest_dir": str(current)})
    assert response.status_code == 200, response.text
    assert response.json() == {"cancelled": False, "path": str(chosen)}
    args, kwargs = calls[0]
    assert args[0] == "/usr/bin/osascript" and args[-1] == str(current)
    assert str(current) not in args[2], "路径不能拼进 AppleScript 源码"
    assert kwargs.get("shell", False) is False and kwargs["timeout"] <= 180


def test_picker_cancel_does_not_change_destination_or_create_it(client, settings, chooser):
    response = client.post(PICKER, json={})
    assert response.status_code == 200, response.text
    assert response.json() == {"cancelled": True, "path": None}
    assert chooser[1][0][0][-1] == str(settings.export_dir)
    assert not settings.export_dir.exists()


@pytest.mark.parametrize(
    "error", [OSError("osascript unavailable"), subprocess.TimeoutExpired("osascript", 125), subprocess.CalledProcessError(1, "osascript")]
)
def test_picker_failure_has_a_text_field_fallback(client, chooser, error):
    chooser[0]["error"] = error
    response = client.post(PICKER, json={})
    assert response.status_code == 503, response.text
    assert response.json()["error"] == "folder_picker_failed"
    assert "粘贴" in response.json()["message"]
    chooser[0]["error"] = None
    assert client.post(PICKER, json={}).json() == {"cancelled": True, "path": None}, "失败后必须释放选择器锁"


def test_picker_unavailable_keeps_the_text_field_option(client, chooser, monkeypatch):
    monkeypatch.setattr("gxr_studio.folder_picker._host_is_macos", lambda: False)
    response = client.post(PICKER, json={})
    assert response.status_code == 501 and response.json()["error"] == "folder_picker_unavailable"
    assert "粘贴" in response.json()["message"]
    assert chooser[1] == []


def test_picker_refuses_a_second_dialog(client, chooser):
    from gxr_studio.folder_picker import _CHOOSER_LOCK

    with _CHOOSER_LOCK:
        response = client.post(PICKER, json={})
    assert response.status_code == 409 and response.json()["error"] == "folder_picker_busy"
    assert chooser[1] == []


def test_picker_requires_token_and_same_origin_before_opening(client, chooser):
    client.headers.pop("X-GXR-Token")
    response = client.post(PICKER, json={}, headers={"Origin": str(client.base_url).rstrip("/")})
    assert response.status_code == 401 and response.json()["error"] == "token_required"
    client.headers["X-GXR-Token"] = "wrong"
    assert client.post(PICKER, json={}).status_code == 403
    client.headers["X-GXR-Token"] = "test-token-123"
    assert client.post(PICKER, json={}, headers={"Origin": "https://example.invalid"}).status_code == 403
    assert chooser[1] == []


@pytest.mark.parametrize("location", ["library", "card", "library-link"])
def test_picker_preserves_destination_boundaries(client, chooser, settings, card, tmp_path, location):
    paths = {"library": settings.library_dir, "card": card.root}
    link = tmp_path / "link-to-library"
    link.symlink_to(settings.library_dir, target_is_directory=True)
    paths["library-link"] = link
    chooser[0]["path"] = paths[location]
    response = client.post(PICKER, json={})
    assert response.status_code == 422, response.text
    assert response.json()["error"] == "bad_destination"


def test_picked_destination_still_uses_export_write_checks(client, chooser, card, tmp_path):
    import_all(client)
    photo = find(client, "R0000102")
    folder = tmp_path / "chosen"
    folder.mkdir()
    chooser[0]["path"] = folder
    picked = client.post(PICKER, json={})
    assert picked.status_code == 200, picked.text
    folder.rmdir()
    folder.write_bytes(b"keep")
    response = client.post(f"/api/photos/{photo['id']}/export", json={"dest_dir": picked.json()["path"]})
    assert response.status_code == 422 and response.json()["error"] == "dest_unwritable"
    assert folder.read_bytes() == b"keep"
