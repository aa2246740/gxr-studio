"""边框标签的样式卡片（size=card）：「边框之前」的底图只做一次，每种样式只加边框；成片和导出的字节不变。

卡片原先每种样式都走一遍完整成片管线（出色、几何、日期戳、边框、缩小）。这里的测试数的是管线真的跑了几次，
以及拆出来的「边框之前」那一段拼上边框以后和原来的 `finish_array` 逐字节一样。
"""

from __future__ import annotations

import io
import threading
import time
from datetime import datetime

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import PORT
from gxr_studio.app import create_app
from gxr_studio.finish import cards, serve
from gxr_studio.finish.compose import finish_array, finish_unframed
from gxr_studio.finish.frame import apply_frame
from gxr_studio.finish.geometry import (
    clamp_norm_rect,
    crop_valid,
    fit_preset_in_valid,
    header_value,
    needs_valid_crop,
    normalized_valid_rect,
    warp,
)
from gxr_studio.finish.pixels import apply_base, apply_extra
from gxr_studio.finish.spec import FrameSpec, Geometry, StampSpec
from gxr_studio.finish.stamp import apply_stamp
from gxr_studio.render.params import EditState
from test_api import find, import_all

STYLES = ["none", "gallery", "leica", "hasselblad", "film", "instant", "canvas"]


@pytest.fixture
def client(settings, card):
    cards.clear()
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        import_all(c)
        yield c
    cards.clear()


@pytest.fixture
def builds(monkeypatch):
    """数一数「选底图」（成片管线里最重的一步）被调了几次。"""
    calls: list[int] = []
    real = serve.color_image

    def spy(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(serve, "color_image", spy)
    return calls


def _card(client, photo_id, style, size="card", **frame):
    body = {"frame": {"style": style, **frame}}
    return client.post(f"/api/photos/{photo_id}/render?stage=final&size={size}", json=body)


def test_seven_style_cards_build_the_base_once(client, builds):
    photo = find(client, "R0000102")
    for style in STYLES:
        r = _card(client, photo["id"], style)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "image/jpeg"
        assert max(Image.open(io.BytesIO(r.content)).size) <= 240
    assert len(builds) == 1


def test_card_layout_matches_the_real_frame_proportions(client):
    """版式和真正的成片一样：卡片图的边框比例、画面内容与 size=preview 的成片缩小以后几乎一样。
    （原来的 size=thumb 对只有 JPG 的照片会先缩到 240 再加边框，边框的最小尺寸一起算进去，比例是歪的。）"""
    photo = find(client, "R0000102")
    for style in STYLES:
        new = Image.open(io.BytesIO(_card(client, photo["id"], style, text="Hi").content)).convert("RGB")
        full = Image.open(io.BytesIO(_card(client, photo["id"], style, size="preview", text="Hi").content)).convert("RGB")
        scale = 240 / max(full.size)
        want = full.resize((max(1, round(full.width * scale)), max(1, round(full.height * scale))), Image.Resampling.LANCZOS)
        assert abs(new.width - want.width) <= 1 and abs(new.height - want.height) <= 1, style
        diff = np.abs(np.asarray(new, int) - np.asarray(want.resize(new.size), int)).mean()
        assert diff < 6, (style, diff)


def test_only_the_frame_style_is_not_in_the_cache_key(client, builds):
    photo = find(client, "R0000102")
    pid = photo["id"]
    _card(client, pid, "leica")
    _card(client, pid, "gallery", color="black")
    assert len(builds) == 1
    rev = client.get(f"/api/photos/{pid}/edit").json()["rev"]
    # 调整、裁切、日期戳变了，底图就要重做
    assert client.put(f"/api/photos/{pid}/edit", json={"adjust": {"exposure_ev": 0.5}, "expected_rev": rev}).status_code == 200
    _card(client, pid, "leica")
    assert len(builds) == 2
    assert client.put(f"/api/photos/{pid}/crop", json={"preset": "1:1"}).status_code == 200
    _card(client, pid, "leica")
    assert len(builds) == 3
    rev = client.get(f"/api/photos/{pid}/edit").json()["rev"]
    assert client.put(f"/api/photos/{pid}/edit", json={"stamp": {"enabled": True}, "expected_rev": rev}).status_code == 200
    _card(client, pid, "leica")
    assert len(builds) == 4
    # 草稿里的颜色编辑（请求体里的 adjust）也算
    r = client.post(f"/api/photos/{pid}/render?stage=final&size=card", json={"frame": {"style": "leica"}, "adjust": {"exposure_ev": -1.0}})
    assert r.status_code == 200
    assert len(builds) == 5
    # 换一张照片是另一份底图
    other = find(client, "R0000103")
    _card(client, other["id"], "leica")
    assert len(builds) == 6


def test_concurrent_cards_of_one_photo_share_the_build(client, monkeypatch):
    photo = find(client, "R0000102")
    real = serve.color_image
    started: list[int] = []

    def slow(*args, **kwargs):
        started.append(1)
        time.sleep(0.4)
        return real(*args, **kwargs)

    monkeypatch.setattr(serve, "color_image", slow)
    results: list[int] = []

    def work(style):
        results.append(_card(client, photo["id"], style).status_code)

    threads = [threading.Thread(target=work, args=(s,)) for s in STYLES]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [200] * len(STYLES)
    assert len(started) == 1


def test_card_size_on_a_stage_other_than_final_stays_small(client):
    """size=card 只对 final 有特殊做法；别的 stage 照旧按 240 的长边出图，不会变成全尺寸。"""
    photo = find(client, "R0000102")
    r = client.post(f"/api/photos/{photo['id']}/render?stage=uncropped&size=card", json={})
    assert r.status_code == 200
    assert max(Image.open(io.BytesIO(r.content)).size) <= 240


def test_unknown_size_is_still_rejected(client):
    photo = find(client, "R0000102")
    assert client.post(f"/api/photos/{photo['id']}/render?stage=final&size=huge", json={}).status_code == 422


# ---- 成片和导出的字节不变 -----------------------------------------------------------


def _old_finish_array(rgb, edit, *, stage, frame, stamp, crop_rect, meta, taken, file_time, from_raw, crop_preset=None):
    """拆分之前的 `finish_array`（原样抄下来，没有 HDR 跟踪）。"""
    if from_raw:
        rgb = apply_extra(rgb, edit.adjust)
    else:
        rgb = apply_base(rgb, edit.adjust, edit.master)
        rgb = apply_extra(rgb, edit.adjust)
    geom = edit.geometry if isinstance(edit.geometry, Geometry) else Geometry()
    if geom.is_identity():
        valid = np.ones(rgb.shape[:2], dtype=bool)
    else:
        rgb, valid = warp(rgb, geom)
    valid_header = header_value(normalized_valid_rect(valid))
    if stage == "uncropped" or stage == "original":
        return rgb, valid_header
    full = (0.0, 0.0, 1.0, 1.0)
    fitted = fit_preset_in_valid(valid, crop_preset, crop_rect or full) if needs_valid_crop(geom) else None
    if fitted is not None:
        x, y, bw, bh = fitted
        rgb = rgb[y : y + bh, x : x + bw]
    elif needs_valid_crop(geom):
        if crop_rect is None or crop_rect == full:
            crop_rect = normalized_valid_rect(valid)
        else:
            crop_rect = clamp_norm_rect(valid, crop_rect)
        rgb = crop_valid(rgb, crop_rect)
    elif crop_rect is not None and crop_rect != full:
        rgb = crop_valid(rgb, crop_rect)
    rgb = apply_stamp(rgb, stamp, taken, file_time)
    rgb = apply_frame(rgb, frame, meta)
    return rgb, None


def _scene():
    rng = np.random.default_rng(7)
    rgb = rng.integers(0, 256, size=(240, 360, 3), dtype=np.uint8)
    meta = {
        "lens": "GR LENS A12 50mm F2.5 MACRO",
        "aperture": "F2.5",
        "shutter": "1/125s",
        "iso": "ISO 400",
        "date": "2026.10.19",
        "frame_no": "12A",
    }
    return rgb, meta, datetime(2026, 10, 19, 10, 30), datetime(2026, 10, 19, 10, 31)


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize(
    ("geometry", "crop"),
    [(None, None), (Geometry(straighten=4.0), (0.1, 0.1, 0.7, 0.6)), (Geometry(rotate90=1, flip=True), (0.0, 0.0, 0.5, 1.0))],
)
def test_finish_array_bytes_are_unchanged_by_the_split(style, geometry, crop):
    rgb, meta, taken, file_time = _scene()
    frame = FrameSpec(style=style, text="Hello", show_date=True)
    stamp = StampSpec(enabled=True)
    edit = EditState(geometry=geometry)
    args = {"frame": frame, "stamp": stamp, "crop_rect": crop, "meta": meta, "taken": taken, "file_time": file_time, "from_raw": False}
    for stage in ("final", "uncropped"):
        got, header = finish_array(rgb.copy(), edit, stage=stage, **args)
        want, want_header = _old_finish_array(rgb.copy(), edit, stage=stage, **args)
        assert got.shape == want.shape and np.array_equal(got, want)
        assert header == want_header


@pytest.mark.parametrize("style", STYLES)
def test_unframed_part_plus_frame_is_the_whole_pipeline(style):
    rgb, meta, taken, file_time = _scene()
    frame = FrameSpec(style=style)
    stamp = StampSpec(enabled=True)
    edit = EditState(geometry=Geometry(straighten=2.0))
    kw = {"stamp": stamp, "crop_rect": (0.05, 0.05, 0.8, 0.8), "taken": taken, "file_time": file_time, "from_raw": False}
    base, _ = finish_unframed(rgb.copy(), edit, stage="final", **kw)
    whole, _ = finish_array(rgb.copy(), edit, stage="final", frame=frame, meta=meta, **kw)
    assert np.array_equal(apply_frame(base, frame, meta), whole)
