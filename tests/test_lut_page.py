"""LUT 页默认选项与 50mm 流程。LUT 全部现场生成，配方条目只在内存比较。"""

from __future__ import annotations

import hashlib

import pytest

from gxr_studio.render.params import parse_recipe
from gxr_studio.render.recipes import CameraData
from test_lut import camhome as camhome
from test_lut import client as client
from test_lut import data_client as data_client
from test_lut import home_with_data as home_with_data
from test_lut import wait
from test_lut_library import cube_bytes, upload


@pytest.mark.parametrize(
    "comments,title,filename,space,source",
    [
        ("# Input: V-Log\n", "sRGB style", "L-Log.cube", "vlog", "declared"),
        ("", "film", "warm_sRGB.cube", "srgb", "guessed"),
        ("# Creative look for V-Log\n", "film", "film.cube", "vlog", "guessed"),
        ("", "warm film", "warm_film.cube", "srgb", "fallback"),
        ("", "Analog Street", "Analog_Street.cube", "srgb", "fallback"),
        ("", "Catalog look", "catalog.cube", "srgb", "fallback"),
        ("# Output: Rec.709\n", "warm film", "warm_film.cube", "srgb", "fallback"),
        ("# Input: unsupported\n", "film", "film.cube", None, "required"),
        ("", "FLog2C style", "FLog2C.cube", None, "required"),
        ("# sRGB look\n", "FLog2C style", "FLog2C.cube", None, "required"),
    ],
)
def test_space_default_is_reported_on_upload_listing_and_patch(client, comments, title, filename, space, source):
    raw = cube_bytes(lambda x: 1 - x, title=title, size=5, comments=comments)
    response = upload(client, raw, filename)
    assert response.status_code == 200, response.text
    item = response.json()
    for record in (
        item,
        client.get("/api/luts").json()["luts"][0],
        client.patch(f"/api/luts/{item['cube_id']}", json={"starred": True}).json(),
    ):
        assert record["default_input_space"] == space
        assert record["default_input_source"] == source
        assert record["default_input_note"]
    # 页面建议不改已存的输入空间，也不改原始 LUT。
    if source == "fallback":
        assert item["input_space"] is None
        assert "sRGB" in item["default_input_note"]


def test_conflicting_space_is_never_replaced_with_a_display_default(client):
    item = upload(client, cube_bytes(), "vlog.cube").json()
    assert item["input_space_state"] == "conflict"
    assert item["default_input_space"] is None
    assert item["default_input_source"] == "required"


def test_manual_space_wins_on_reimport_and_is_not_labelled_a_guess(client):
    raw = cube_bytes(comments="# Input: V-Log\n")
    item = upload(client, raw, "vlog.cube").json()
    response = client.patch(f"/api/luts/{item['cube_id']}", json={"input_space": "rec709"})
    assert response.status_code == 200
    repeated = upload(client, raw, "another_vlog.cube").json()
    assert repeated["default_input_space"] == "rec709"
    assert repeated["default_input_source"] == "manual"
    assert repeated["input_space_manual"]


def test_spaces_and_unit_capabilities_are_explicit(client):
    response = client.get("/api/lut/spaces").json()
    assert response["default_input"] == "srgb"
    assert {u["id"]: u["fit_supported"] for u in response["units"]} == {"jlaunch": True, "qlaunch": False, "l08firm": False}
    assert {s["id"] for s in response["spaces"] if s["kind"] == "display"} == {"srgb", "rec709", "gamma24"}


@pytest.mark.parametrize("unit", ["qlaunch", "l08firm"])
def test_unsupported_units_explain_the_scope_before_camera_data_is_needed(client, unit):
    item = upload(client).json()
    response = client.post("/api/lut/fit", json={"cube_id": item["cube_id"], "unit": unit})
    assert response.status_code == 422
    assert response.json()["error"] == "unit_not_calibrated"
    assert "现在只能为 A12 50mm 生成" in response.json()["message"]
    assert "只用于 50mm 拍的照片" in response.json()["message"]


def test_explicit_50mm_selection_preserves_existing_recipe_bytes_and_reports_unit(data_client, settings):
    """旧的默认请求与页面显式传入相同颜色和组件的请求，得到同一组 552 B 条目。"""
    raw = cube_bytes(lambda x: x * [0.93, 0.9, 0.86], title="synthetic warm", size=5, comments="# Input: sRGB\n")
    cube_id = upload(data_client, raw).json()["cube_id"]
    bodies = [
        {"cube_id": cube_id, "space": "srgb", "output": "srgb", "profile": "quick"},
        {"cube_id": cube_id, "space": "srgb", "output": "srgb", "profile": "quick", "unit": "jlaunch"},
    ]
    entries = []
    cam = CameraData(settings.home_dir)
    for body in bodies:
        started = data_client.post("/api/lut/fit", json=body)
        assert started.status_code == 200, started.text
        job = started.json()
        assert job["unit"] == "jlaunch"
        assert data_client.get(f"/api/lut/fit/{job['job_id']}").json()["unit"] == "jlaunch"
        status = wait(data_client, job["job_id"])
        assert status["state"] == "done", status
        assert status["unit"] == "jlaunch"
        resolved = []
        for candidate in status["result"]["candidates"]:
            selection = {"kind": "builtin", "unit": candidate["unit"], "slot": candidate["slot"], "levels": candidate["levels"]}
            entry = cam.resolve(parse_recipe(selection)).entry
            assert len(entry) == 552
            assert hashlib.sha256(entry).hexdigest() == candidate["entry_sha256"]
            resolved.append(entry)
        entries.append(resolved)
        saved = data_client.post("/api/my-recipes", json={"job_id": job["job_id"], "candidate": 0, "name": "synthetic warm"})
        assert saved.status_code == 200, saved.text
        doc = saved.json()
        assert doc["recipe"]["unit"] == "jlaunch"
        assert not doc["source"]["input_space_guessed"], "文件声明不是推测"
        assert data_client.get(f"/api/my-recipes/{doc['id']}").json()["recipe"]["unit"] == "jlaunch"
        assert data_client.post(f"/api/lut/fit/{job['job_id']}/cancel").json()["unit"] == "jlaunch"
    assert entries[0] == entries[1]
    assert len(entries[0]) == 2
    assert all(r["recipe"]["unit"] == "jlaunch" for r in data_client.get("/api/my-recipes").json()["recipes"])
