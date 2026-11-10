"""缺少拍摄目录的 RAW：粘贴、滑块、撤销/重做的版本与持久状态一致。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from conftest import PORT
from gxr_studio.app import create_app
from gxr_studio.library import sidecar as sc
from test_clone_coverage import _tiny_dng

TAG = "GXR1 6a3a5a3c02 28 06 9999C9999-"


@pytest.fixture
def tagged(settings, lib):
    path = lib.root / "2026/2026-11-10/R0000001.DNG"
    path.parent.mkdir(parents=True)
    path.write_bytes(_tiny_dng(root_extra=[(33432, "ASCII", TAG)]))
    lib.rebuild()
    shot = lib.list_shots()[0]
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        yield client, lib, shot


def saved(tagged, rev, contrast):
    client, lib, shot = tagged
    response = client.get(f"/api/photos/{shot['shot_id']}/edit")
    assert response.status_code == 200, response.text
    edit = response.json()
    side = json.loads(lib.sidecar_path_for(shot).read_text())
    assert edit["rev"] == side["rev"] == lib.get_shot(shot["shot_id"])["sidecar_rev"] == rev
    assert edit["adjust"]["contrast"] == side.get("adjust", {}).get("contrast", 0) == contrast
    assert "recipe" not in side and edit["recipe"] is None
    assert edit["render"]["available"] is False
    assert edit["render"]["reason"] == "camera_recipe_catalog_missing"
    return edit


def paste_adjust(tagged, rev):
    client, _lib, shot = tagged
    response = client.post(
        "/api/edits/paste",
        json={
            "groups": ["adjust"],
            "source": {"adjust": {"contrast": 2}},
            "targets": [{"id": shot["shot_id"], "expected_rev": rev}],
            "include_state": True,
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    item = result["results"][0]
    assert item["status"] == "applied", item
    assert item["applied"] == ["adjust"] and item["skipped"] == []
    assert item["rev"] == item["edit"]["rev"] == rev + 1
    assert item["edit"]["adjust"]["contrast"] == 2
    assert item["edit"]["render"]["reason"] == "camera_recipe_catalog_missing"
    assert "仍可保存" in item["edit"]["render"]["message"]
    return result


def test_missing_catalog_paste_then_slider_undo_and_redo_have_no_orphan_revisions(tagged):
    client, _lib, shot = tagged
    saved(tagged, 0, 0)
    paste_adjust(tagged, 0)
    saved(tagged, 1, 2)
    # 滑块保存，再撤销滑块、撤销粘贴、重做粘贴、重做滑块，沿用编辑器 PUT 的版本协议。
    for expected_rev, contrast in ((1, 3), (2, 2), (3, 0), (4, 2), (5, 3)):
        response = client.put(
            f"/api/photos/{shot['shot_id']}/edit",
            json={"expected_rev": expected_rev, "adjust": {"contrast": contrast}},
        )
        assert response.status_code == 200, response.text
        assert response.json() == saved(tagged, expected_rev + 1, contrast)


def test_missing_catalog_slider_save_succeeds_and_reports_prediction_unavailable(tagged):
    client, _lib, shot = tagged
    response = client.put(f"/api/photos/{shot['shot_id']}/edit", json={"expected_rev": 0, "adjust": {"contrast": 2}})
    assert response.status_code == 200, response.text
    assert response.json() == saved(tagged, 1, 2)
    assert "仍可保存" in response.json()["render"]["message"]


def test_missing_catalog_batch_undo_and_repeated_paste_keep_the_current_revision(tagged):
    client, _lib, _shot = tagged
    first = paste_adjust(tagged, 0)
    undo = client.post("/api/edits/paste/undo", json={"batch_id": first["batch_id"]})
    assert undo.status_code == 200, undo.text
    assert undo.json()["counts"] == {"restored": 1, "changed": 0, "error": 0}
    assert undo.json()["results"][0]["rev"] == 2
    saved(tagged, 2, 0)
    second = paste_adjust(tagged, 2)
    saved(tagged, 3, 2)
    undone = client.post("/api/edits/paste/undo", json={"batch_id": second["batch_id"]})
    assert undone.status_code == 200 and undone.json()["results"][0]["rev"] == 4
    saved(tagged, 4, 0)


@pytest.mark.parametrize("missing_frame", (False, True))
def test_rejected_recipe_does_not_commit_the_adjustment_or_consume_a_revision(tagged, missing_frame):
    client, lib, shot = tagged
    path = lib.sidecar_path_for(shot)
    if missing_frame:
        side = sc.read(path)
        side["frame"] = {"orientation": 1, "width": None, "height": None}
        sc.write_atomic(path, side, bump=False, tmp_dir=lib.tmp_dir)
    before = path.read_bytes()
    response = client.put(
        f"/api/photos/{shot['shot_id']}/edit",
        json={"expected_rev": 0, "recipe": {"unit": "jlaunch", "slot": 1, "levels": {"saturation": 7}}, "adjust": {"contrast": 2}},
    )
    assert response.status_code == 422 and response.json()["error"] == "level_not_allowed"
    assert path.read_bytes() == before
    assert lib.get_shot(shot["shot_id"])["sidecar_rev"] == 0
    if not missing_frame:
        saved(tagged, 0, 0)


def test_invalid_merged_edit_is_rejected_before_sidecar_or_cache_changes(tagged):
    client, lib, shot = tagged
    path = lib.sidecar_path_for(shot)
    side = sc.read(path)
    side["curve"] = {"master": [[0, 0], [255, 999]]}
    out = sc.write_atomic(path, side, tmp_dir=lib.tmp_dir)
    lib.set_edit_cache(shot["shot_id"], out["rev"], out.get("crop"))
    before = path.read_bytes()
    response = client.put(f"/api/photos/{shot['shot_id']}/edit", json={"expected_rev": 1, "adjust": {"contrast": 2}})
    assert response.status_code == 422 and response.json()["error"] == "bad_curve"
    assert path.read_bytes() == before
    assert lib.get_shot(shot["shot_id"])["sidecar_rev"] == 1


@pytest.mark.parametrize("groups", (["adjust"], ["recipe", "adjust"]))
def test_source_copy_only_requires_the_catalog_for_the_recipe_group(tagged, groups):
    client, lib, source = tagged
    side = sc.read(lib.sidecar_path_for(source))
    side["adjust"] = {"contrast": 2}
    out = sc.write_atomic(lib.sidecar_path_for(source), side, tmp_dir=lib.tmp_dir)
    lib.set_edit_cache(source["shot_id"], out["rev"], out.get("crop"))
    path = lib.root / "2026/2026-11-10/R0000002.DNG"
    path.write_bytes(_tiny_dng())
    lib.rebuild()
    target = next(shot for shot in lib.list_shots() if shot["name"] == "R0000002")
    response = client.post(
        "/api/edits/paste",
        json={
            "groups": groups,
            "source_id": source["shot_id"],
            "targets": [{"id": target["shot_id"], "expected_rev": 0}],
            "include_state": True,
        },
    )
    if "recipe" in groups:
        assert response.status_code == 422 and response.json()["error"] == "camera_recipe_catalog_missing"
        assert sc.read(lib.sidecar_path_for(target))["rev"] == 0
        assert lib.get_shot(target["shot_id"])["sidecar_rev"] == 0
    else:
        assert response.status_code == 200, response.text
        item = response.json()["results"][0]
        assert item["status"] == "applied" and item["rev"] == 1
        assert item["edit"]["adjust"]["contrast"] == 2 and item["edit"]["recipe"] is None
    saved(tagged, 1, 2)


def test_importing_the_catalog_resumes_recorded_knobs_without_changing_saved_edits(tagged, monkeypatch):
    from test_recipe_tag_render import SyntheticCamera

    client, lib, shot = tagged
    paste_adjust(tagged, 0)
    path = lib.sidecar_path_for(shot)
    before = path.read_bytes()
    camera = SyntheticCamera()
    service = client.app.state.ctx.render
    monkeypatch.setattr(service, "recipe_menu", lambda: camera)
    monkeypatch.setattr(service, "_require", lambda unit=None: camera)
    response = client.get(f"/api/photos/{shot['shot_id']}/edit")
    assert response.status_code == 200, response.text
    edit = response.json()
    assert edit["rev"] == 1 and edit["adjust"]["contrast"] == 2
    assert edit["recipe"]["levels"]["amount"] == 90 and edit["recipe"]["levels"]["fade"] == 3
    assert edit["render"]["available"] is True
    assert edit["render"]["effective_levels"]["fade"] == 3
    assert path.read_bytes() == before


def test_skipped_paste_does_not_commit_a_pending_frame_repair(tagged):
    client, lib, shot = tagged
    path = lib.sidecar_path_for(shot)
    side = sc.read(path)
    side["frame"] = {"orientation": 1, "width": None, "height": None}
    sc.write_atomic(path, side, bump=False, tmp_dir=lib.tmp_dir)
    before = path.read_bytes()
    response = client.post(
        "/api/edits/paste",
        json={
            "groups": ["recipe"],
            "source": {"recipe": {"unit": "jlaunch", "slot": 1, "levels": {"saturation": 7}}},
            "targets": [{"id": shot["shot_id"], "expected_rev": 0}],
            "include_state": True,
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["results"][0]["status"] == "skipped" and "rev" not in result["results"][0]
    assert path.read_bytes() == before and lib.get_shot(shot["shot_id"])["sidecar_rev"] == 0
    assert client.post("/api/edits/paste/undo", json={"batch_id": result["batch_id"]}).status_code == 404


def test_frame_repair_and_accepted_paste_commit_as_one_revision(tagged):
    client, lib, shot = tagged
    path = lib.sidecar_path_for(shot)
    side = sc.read(path)
    side["frame"] = {"orientation": 1, "width": None, "height": None}
    sc.write_atomic(path, side, bump=False, tmp_dir=lib.tmp_dir)
    result = paste_adjust(tagged, 0)
    saved(tagged, 1, 2)
    assert sc.read(path)["frame"]["width"] == 32
    undo = client.post("/api/edits/paste/undo", json={"batch_id": result["batch_id"]})
    assert undo.status_code == 200 and undo.json()["results"][0]["rev"] == 2
    saved(tagged, 2, 0)


def test_rejected_crop_does_not_commit_a_pending_frame_repair(tagged):
    client, lib, shot = tagged
    path = lib.sidecar_path_for(shot)
    side = sc.read(path)
    side["frame"] = {"orientation": 1, "width": None, "height": None}
    sc.write_atomic(path, side, bump=False, tmp_dir=lib.tmp_dir)
    before = path.read_bytes()
    response = client.put(f"/api/photos/{shot['shot_id']}/crop", json={"expected_rev": 0, "preset": "free", "rect": [2.0, 0, 0.5, 0.5]})
    assert response.status_code == 422 and response.json()["error"] == "bad_rect"
    assert path.read_bytes() == before and lib.get_shot(shot["shot_id"])["sidecar_rev"] == 0
