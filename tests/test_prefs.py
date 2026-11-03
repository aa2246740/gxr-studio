"""界面偏好：默认值、部分更新、坏文件，以及不跟随链接、不写进 Git 工作区。"""

from __future__ import annotations

import json
import os
import re
import subprocess

import pytest
from fastapi.testclient import TestClient

from conftest import PORT
from gxr_studio.app import create_app
from gxr_studio.prefs import load_prefs, update_prefs


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as test_client:
        yield test_client


def test_missing_directory_stays_missing_until_a_choice_is_saved(settings):
    missing = settings.home_dir / "not-created"
    assert load_prefs(missing) == {"onboarding_dismissed": False, "export_hdr": False}
    assert update_prefs(missing, [], {}) == {"onboarding_dismissed": False, "export_hdr": False}
    assert not missing.exists()


def _assert_owner_permissions(path, tmp_path):
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        return
    identity = subprocess.run(["whoami.exe", "/user", "/fo", "csv", "/nh"], capture_output=True, check=True, timeout=10).stdout
    text = identity.decode("utf-16-le") if b"\x00" in identity else identity.decode("utf-8", errors="replace")
    match = re.search(r"S-1-5-\d+(?:-\d+)+", text)
    assert match is not None, text
    sid = match.group()
    acl_path = tmp_path / "prefs-acl.txt"
    subprocess.run(["icacls.exe", str(path), "/save", str(acl_path), "/q"], capture_output=True, check=True, timeout=10)
    sddl = acl_path.read_text(encoding="utf-16")
    aces = [item.split(";") for item in re.findall(r"\(([^()]+)\)", sddl)]
    assert aces and all(len(ace) == 6 and ace[0] == "A" and ace[-1] in {sid, "SY", "BA"} for ace in aces), sddl
    assert sid in {ace[-1] for ace in aces}, sddl


def test_prefs_default_off_then_remember_each_choice(client, settings, tmp_path):
    assert client.get("/api/prefs").json() == {"onboarding_dismissed": False, "export_hdr": False}
    assert not (settings.home_dir / "prefs.json").exists()

    skipped = client.put("/api/prefs", json={"onboarding_dismissed": True})
    assert skipped.status_code == 200
    assert skipped.json() == {"onboarding_dismissed": True, "export_hdr": False}
    saved = json.loads((settings.home_dir / "prefs.json").read_text(encoding="utf-8"))
    assert saved["schema"] == "gxr-studio/prefs/1"
    assert saved["onboarding_dismissed"] is True and saved["export_hdr"] is False
    _assert_owner_permissions(settings.home_dir / "prefs.json", tmp_path)
    assert not any(".tmp" in path.name for path in settings.home_dir.iterdir())

    turned_on = client.put("/api/prefs", json={"export_hdr": True})
    assert turned_on.json() == {"onboarding_dismissed": True, "export_hdr": True}
    turned_off = client.put("/api/prefs", json={"export_hdr": False})
    assert turned_off.json() == {"onboarding_dismissed": True, "export_hdr": False}
    assert client.get("/api/prefs").json()["export_hdr"] is False

    empty = client.put("/api/prefs", json={})
    assert empty.status_code == 200
    assert empty.json()["onboarding_dismissed"] is True
    bad = client.put("/api/prefs", json={"export_hdr": None})
    assert bad.status_code == 422
    assert bad.json()["message"] == "请求无效。请刷新页面后重试。"
    assert client.get("/api/prefs").json()["export_hdr"] is False


def test_unreadable_prefs_use_defaults_for_the_bad_fields(client, settings):
    settings.home_dir.mkdir(parents=True, exist_ok=True)
    path = settings.home_dir / "prefs.json"
    path.write_text("{", encoding="utf-8")
    assert client.get("/api/prefs").json() == {"onboarding_dismissed": False, "export_hdr": False}
    path.write_text(json.dumps({"onboarding_dismissed": 1, "export_hdr": True, "schema": "nope"}), encoding="utf-8")
    assert client.get("/api/prefs").json() == {"onboarding_dismissed": False, "export_hdr": True}
    path.write_text("[]", encoding="utf-8")
    assert client.get("/api/prefs").json() == {"onboarding_dismissed": False, "export_hdr": False}


def test_prefs_replace_a_symlink_instead_of_following_it(client, settings):
    settings.home_dir.mkdir(parents=True, exist_ok=True)
    outside = settings.home_dir.parent / "outside-prefs.json"
    original = json.dumps({"onboarding_dismissed": True, "export_hdr": True})
    outside.write_text(original, encoding="utf-8")
    link = settings.home_dir / "prefs.json"
    link.symlink_to(outside)
    assert client.get("/api/prefs").json() == {"onboarding_dismissed": False, "export_hdr": False}
    saved = client.put("/api/prefs", json={"export_hdr": True})
    assert saved.status_code == 200
    assert saved.json() == {"onboarding_dismissed": False, "export_hdr": True}
    assert outside.read_text(encoding="utf-8") == original
    assert link.is_file() and not link.is_symlink()
    assert json.loads(link.read_text(encoding="utf-8"))["export_hdr"] is True


def test_prefs_are_refused_inside_a_git_worktree(settings):
    repo = settings.home_dir.parent / "repo"
    home = repo / "app"
    home.mkdir(parents=True)
    (repo / ".git").mkdir()
    app = create_app(settings.with_(home_dir=home))
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        response = client.put("/api/prefs", json={"onboarding_dismissed": True})
    assert response.status_code == 422
    assert response.json()["error"] == "private_path_in_repository"
    assert not (home / "prefs.json").exists()
