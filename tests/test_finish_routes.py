"""浏览器预览画边框要用的接口：拍摄信息里的边框文字、字体和标志文件、响应里的放大倍数。"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import PORT
from gxr_studio.app import create_app
from gxr_studio.finish.fonts import FONT_FILES
from gxr_studio.finish.meta_text import photo_meta
from test_api import find, import_all


@pytest.fixture
def client(settings, card):
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        import_all(c)
        yield c


def test_meta_carries_the_exact_strings_the_frame_draws(client):
    photo = find(client, "R0000102")
    body = client.get(f"/api/photos/{photo['id']}/meta").json()
    frame = body["frame"]
    assert set(frame) == {"lens", "lens_short", "aperture", "shutter", "iso", "date", "frame_no"}
    c = client.app.state.ctx
    shot = c.lib.get_shot(photo["id"])
    info, taken, file_time = photo_meta(c.lib, shot)
    assert frame == info
    assert frame["frame_no"] == photo["name"][-4:]
    assert body["captured_at"] == (taken or file_time).isoformat(timespec="seconds")


@pytest.mark.parametrize("name", [*FONT_FILES.values(), "ricoh.png"])
def test_finish_assets_are_served(client, name):
    r = client.get(f"/api/finish/assets/{name}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("font/ttf" if name.endswith(".ttf") else "image/png")
    assert len(r.content) > 1000


@pytest.mark.parametrize("name", ["passwd", "..%2f..%2fetc%2fpasswd", "Inter-Regular.otf", "ricoh.svg"])
def test_finish_assets_only_serve_the_listed_files(client, name):
    assert client.get(f"/api/finish/assets/{name}").status_code in (404, 422)


def test_render_says_how_much_bigger_the_export_is(client):
    """X-GXR-Scale：导出（全尺寸）的长边是响应里这张图的几倍。裁切、缩小都算在里面。"""
    photo = find(client, "R0000102")
    pid = photo["id"]
    assert client.put(f"/api/photos/{pid}/crop", json={"preset": "free", "rect": [0.1, 0.2, 0.5, 0.4], "angle_deg": 0}).status_code == 200
    full = client.post(f"/api/photos/{pid}/render?stage=final&size=full", json={})
    assert full.status_code == 200 and float(full.headers["x-gxr-scale"]) == pytest.approx(1.0, abs=0.001)
    full_w = Image.open(io.BytesIO(full.content)).size[0]
    small = client.post(f"/api/photos/{pid}/render?stage=final&max_edge=96", json={"adjust": {"exposure_ev": 0.3}})
    assert small.status_code == 200
    width = Image.open(io.BytesIO(small.content)).size[0]
    assert width <= 96 and float(small.headers["x-gxr-scale"]) * width == pytest.approx(full_w, rel=0.03)


def test_scale_is_there_for_the_untouched_photo_too(client):
    photo = find(client, "R0000102")
    r = client.post(f"/api/photos/{photo['id']}/render?stage=final&max_edge=64", json={})
    assert r.status_code == 200
    width = Image.open(io.BytesIO(r.content)).size
    assert float(r.headers["x-gxr-scale"]) * max(width) == pytest.approx(max(photo["width"], photo["height"]), rel=0.02)


def test_the_card_size_is_gone(client):
    photo = find(client, "R0000102")
    assert client.post(f"/api/photos/{photo['id']}/render?stage=final&size=card", json={}).status_code == 422
