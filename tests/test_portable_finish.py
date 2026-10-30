"""合成夹具覆盖成片流水线。不依赖固件、色卡或真实 RAW。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from conftest import PORT
from gxr_studio.app import create_app
from gxr_studio.finish import stamp
from gxr_studio.finish.approx import apply_lut, fit_lut
from gxr_studio.finish.auto_lines import estimate
from gxr_studio.finish.frame import apply_frame
from gxr_studio.finish.geometry import fitted_crop, output_size, warp
from gxr_studio.finish.spec import Geometry, StampSpec, parse_frame, parse_geometry, parse_stamp
from gxr_studio.finish.stamp import apply_stamp
from gxr_studio.render.params import EditError
from test_api import find, import_all
from test_raw_export import scene_dng


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        yield c


def _lines(width: int = 320, height: int = 240) -> np.ndarray:
    image = Image.new("RGB", (width, height), (24, 24, 24))
    draw = ImageDraw.Draw(image)
    for y in range(40, height - 20, 50):
        draw.line((20, y, width - 20, y), fill=(230, 230, 230), width=4)
    return np.asarray(image)


def test_auto_suggestion_is_an_integer_and_stable() -> None:
    level = estimate(_lines())
    assert level["confident"] is True
    assert isinstance(level["vertical"], float)
    tilted = np.asarray(Image.fromarray(_lines()).rotate(10, resample=Image.Resampling.BICUBIC, fillcolor=(24, 24, 24)))
    guess = estimate(tilted)
    assert guess["confident"] is True
    parsed = parse_geometry({"straighten": round(float(guess["straighten"]), 4), "vertical": int(round(float(guess["vertical"])))})
    assert isinstance(parsed.vertical, int)
    again = estimate(tilted)
    assert again["straighten"] == guess["straighten"] and int(round(float(again["vertical"]))) == int(round(float(guess["vertical"])))
    flat = estimate(np.full((80, 120, 3), 128, np.uint8))
    assert flat["confident"] is False


def test_frames_stamp_and_valid_crop_cover_the_synthetic_paths() -> None:
    fixture = Path(__file__).resolve().parent / "fixtures" / "NotoSansSC-Shanghai.otf"
    saved_fonts = stamp._CJK_FONTS
    stamp._CJK_FONTS = (fixture,)
    try:
        _frames_stamp_and_valid_crop()
    finally:
        stamp._CJK_FONTS = saved_fonts


def _frames_stamp_and_valid_crop() -> None:
    photo = np.full((80, 120, 3), 180, np.uint8)
    meta = {"lens_short": "A12 50mm", "aperture": "f/2.5", "shutter": "1/80s", "iso": "ISO 200", "date": "2026.10.31", "frame_no": "001"}
    for style in ("gallery", "leica", "hasselblad", "film", "instant", "canvas"):
        spec = parse_frame({"style": style, "text": "上海" if style == "instant" else "", "show_date": True})
        assert spec is not None
        out = apply_frame(photo, spec, meta)
        assert out.shape[0] >= photo.shape[0] and out.shape[1] >= photo.shape[1]
    beijing = apply_frame(photo, parse_frame({"style": "instant", "text": "北京"}), meta)  # type: ignore[arg-type]
    shanghai = apply_frame(photo, parse_frame({"style": "instant", "text": "上海"}), meta)  # type: ignore[arg-type]
    assert not np.array_equal(beijing, shanghai)
    stamp = StampSpec(enabled=True, content="text", text="W" * 24, font="sans", size="large", glow=False)
    base = np.full((640, 480, 3), 30, np.uint8)
    long = apply_stamp(base, stamp, None, datetime(2026, 10, 31))
    assert long.shape == base.shape
    ink = np.nonzero(np.any(long > 40, axis=2))
    assert ink[0].size and int(ink[1].min()) >= 0 and int(ink[1].max()) < base.shape[1]
    turned = output_size(640, 480, Geometry(rotate90=1))
    assert turned == (480, 640)
    fixed = fitted_crop(Geometry(straighten=12), (0.0, 0.0, 1.0, 1.0))
    assert fixed is not None and fixed != (0.0, 0.0, 1.0, 1.0)
    _rgb, valid = warp(np.full((40, 60, 3), 80, np.uint8), Geometry(straighten=12))
    assert valid.mean() < 1
    src = np.linspace(0, 255, 27, dtype=np.uint8).reshape(3, 3, 3)
    lut = fit_lut(src, 255 - src, n=4)
    assert apply_lut(src, lut).shape == src.shape


def test_bad_spec_values_are_rejected_before_they_can_be_saved() -> None:
    for raw in (
        {"style": "nope"},
        {"color": "red"},
        {"weight": "huge"},
        {"canvas_ratio": "2:1"},
        {"text": "字" * 30},
    ):
        try:
            parse_frame(raw)
        except EditError as exc:
            assert exc.message
        else:
            raise AssertionError(raw)
    for raw in ({"content": "nope"}, {"font": "comic"}, {"date_override": "2026-99-99"}, {"date_format": "nope"}):
        try:
            parse_stamp(raw)
        except EditError as exc:
            assert exc.message
        else:
            raise AssertionError(raw)


def test_pipeline_on_a_synthetic_library(client, card, settings):
    lines = Image.fromarray(_lines(640, 480))
    lines.save(card.root / "DCIM" / "100RICOH" / "LINES.JPG", quality=95)
    (card.root / "DCIM" / "100RICOH" / "R0000109.DNG").write_bytes(scene_dng())
    import_all(client)
    jpg = find(client, "R0000102")
    plain = client.post(f"/api/photos/{jpg['id']}/export", json={"quality": 90}).json()
    client.put(f"/api/photos/{jpg['id']}/edit", json={"adjust": {"exposure_ev": 1.5}})
    bright = client.post(f"/api/photos/{jpg['id']}/export", json={"quality": 90}).json()
    a = np.asarray(Image.open(plain["path"]).convert("RGB"))
    b = np.asarray(Image.open(bright["path"]).convert("RGB"))
    assert abs(float(b.mean()) - float(a.mean())) > 5
    original = client.get(f"/api/photos/{jpg['id']}/render", params={"stage": "original", "size": "preview"})
    assert original.status_code == 200 and original.content[:2] == b"\xff\xd8"
    client.put("/api/defaults/finish", json={"frame": {"style": "gallery"}, "stamp": {"enabled": False}})
    client.put(f"/api/photos/{jpg['id']}/edit", json={"frame": {"style": "none"}, "stamp": {"enabled": False}, "adjust": None})
    off = client.post(f"/api/photos/{jpg['id']}/export", json={"quality": 90}).json()
    assert (off["width"], off["height"]) == (jpg["width"], jpg["height"])
    client.put("/api/defaults/finish", json={"frame": {"style": "none"}, "stamp": {"enabled": False}})
    bad_date = client.put(f"/api/photos/{jpg['id']}/edit", json={"stamp": {"enabled": True, "date_override": "2026-99-99"}})
    assert bad_date.status_code == 422
    assert client.post(f"/api/photos/{jpg['id']}/export", json={"format": "tiff"}).status_code == 422
    raw = find(client, "R0000109")
    decoded = client.post(f"/api/photos/{raw['id']}/export", json={"quality": 80})
    assert decoded.status_code == 200, decoded.text
    assert decoded.json()["render"] == "neutral-preview"
    listed = client.get("/api/photos").json()
    assert all("thumb_url" in item for item in listed)
    shot = find(client, "LINES")
    suggested = client.post(f"/api/photos/{shot['id']}/geometry/auto").json()
    assert suggested["confident"] is True and isinstance(suggested["vertical"], int)
    applied = client.put(
        f"/api/photos/{shot['id']}/edit",
        json={"geometry": {"straighten": suggested["straighten"], "vertical": suggested["vertical"]}},
    )
    assert applied.status_code == 200, applied.text
    again = client.post(f"/api/photos/{shot['id']}/geometry/auto").json()
    assert again["vertical"] == suggested["vertical"]
    assert abs(again["straighten"] - suggested["straighten"]) < 1e-6
    body = client.post(f"/api/photos/{jpg['id']}/export", json={}).json()
    path = Path(body["path"])
    path.unlink()
    decoy = settings.export_dir / "missing-not-exported.jpg"
    decoy.write_bytes(b"nope")
    path.symlink_to(decoy)
    revealed = client.post(f"/api/exports/{body['export_id']}/reveal")
    assert revealed.status_code == 404 and revealed.json()["message"]
