"""裁切的社交媒体比例与构图辅助线（冒烟）。

只走接口和静态检查，不开浏览器；点按、拖动、线条显示效果在浏览器里走查，见 research/studio_crop_guides/REPORT.md。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gxr_studio.app import create_app

from conftest import PORT

WEB = Path(__file__).resolve().parents[1] / "web"
CROP_JS = WEB / "assets" / "crop.js"

# 每个比例：（后端 id, 长边, 短边）。与 crop.py 的 PRESETS 对应
RATIOS = {
    "3:2": (3, 2),
    "4:3": (4, 3),
    "1:1": (1, 1),
    "4:5": (5, 4),
    "16:9": (16, 9),
    "16:10": (16, 10),
    "1.91:1": (191, 100),
    "2.35:1": (235, 100),
    "xpan-65-24": (65, 24),
}


@pytest.fixture
def web_client(settings):
    app = create_app(settings.with_(web_dir=WEB))
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        yield c


def _import_all(client):
    card_id = client.get("/api/cards").json()[0]["id"]
    job = client.post("/api/import", json={"card_id": card_id}).json()
    for _ in range(600):
        j = client.get(f"/api/jobs/{job['id']}").json()
        if not j["running"]:
            assert j["progress"]["state"] == "done", j["progress"]
            return
        time.sleep(0.05)
    raise AssertionError("导入超时")


def test_social_presets_are_served_by_the_backend(web_client, card):
    _import_all(web_client)
    ids = {p["id"]: p["ratio"] for p in web_client.get("/api/crop-presets").json()}
    for pid, ratio in RATIOS.items():
        assert pid in ids, pid
        assert ids[pid] == list(ratio), pid


def _one_pair_per_orientation(client):
    """假卡里的 DNG+JPG 照片各取一张：横拍一张、竖拍一张（假卡的 R0000105 是方向 6 的竖拍）。"""
    photos = client.get("/api/photos", params={"limit": 5000}).json()
    chosen = {}
    for p in photos:
        if not (p["has_raw"] and p["has_jpg"]):
            continue
        f = client.get(f"/api/photos/{p['id']}/crop").json()["frame"]
        chosen.setdefault(f["width"] >= f["height"], p["id"])
    assert set(chosen) == {True, False}, "假卡里应该同时有横拍和竖拍的 DNG+JPG"
    return list(chosen.values())


@pytest.mark.parametrize("pid", sorted(RATIOS))
def test_saved_crop_exports_the_chosen_ratio_in_both_orientations(web_client, card, pid):
    """保存裁切后，导出的尺寸要符合所选比例；横、竖两个方向都要对（横拍、竖拍照片都试）。"""
    _import_all(web_client)
    long_side, short_side = RATIOS[pid]
    ratio = long_side / short_side
    for photo_id in _one_pair_per_orientation(web_client):
        for flip in (False, True):
            put = web_client.put(f"/api/photos/{photo_id}/crop", json={"preset": pid, "rect": None, "angle_deg": 0, "flip": flip})
            assert put.status_code == 200, put.text
            crop = put.json()
            fw, fh = crop["frame"]["width"], crop["frame"]["height"]
            want_landscape = (fw >= fh) != flip
            exp = web_client.post(f"/api/photos/{photo_id}/export", json={"long_edge": 640, "quality": 90})
            assert exp.status_code == 200, exp.text
            out = exp.json()
            w, h = out["width"], out["height"]
            if long_side != short_side:  # 1:1 没有横竖
                assert (w >= h) == want_landscape, (pid, fw, fh, flip, w, h)
            got = max(w, h) / min(w, h)
            assert abs(got - ratio) / ratio < 0.02, (pid, fw, fh, flip, w, h, got, ratio)


def test_default_is_no_auto_xpan_suggestion():
    """不自动推荐 XPan：前端不再有 16:9 → XPan 的提示或按钮。"""
    src = CROP_JS.read_text(encoding="utf-8")
    assert "xpan-suggest" not in src
    assert "裁成 XPan" not in src
    assert "要裁成 XPan" not in src


def test_guide_and_preset_tables_are_wired():
    """前端的比例表和辅助线表存在，G / O 快捷键接到裁切标签上。"""
    src = CROP_JS.read_text(encoding="utf-8")
    for needle in ("PRESET_GROUPS", "GUIDES", "'thirds'", "'golden'", "'spiral'", "'diagonal'", "'cross'", "'grid'", "cycleSpiral", "cycleGuide"):
        assert needle in src, needle
    viewer = (WEB / "assets" / "viewer.js").read_text(encoding="utf-8")
    assert "cycleGuide(view)" in viewer and "cycleSpiral(view)" in viewer
