"""裁切几何与 sidecar 往返（M1 验收 6）。"""

from __future__ import annotations

import io
import json
import random

import pytest
from PIL import Image

from gxr_studio import crop as cm
from gxr_studio.crop import CropError, crop_box, default_crop, validate_crop
from gxr_studio.export import ExportOptions, render
from gxr_studio.library import edits
from gxr_studio.library import sidecar as sc


def test_xpan_on_4288_wide_frame_is_4288x1583_and_centered():
    c = default_crop("xpan-65-24", 4288, 2848)
    x, y, w, h = crop_box(c.rect, 4288, 2848)
    assert (w, h) == (4288, 1583) and x == 0
    assert abs((y + h / 2) - 2848 / 2) <= 1, "默认居中"
    assert round(4288 * 24 / 65) == 1583


@pytest.mark.parametrize(
    "preset,expected",
    [
        ("original", (4288, 2848)),
        ("free", (4288, 2848)),
        ("3:2", (4272, 2848)),
        ("4:3", (3797, 2848)),
        ("1:1", (2848, 2848)),
        ("4:5", (3560, 2848)),
        ("16:9", (4288, 2412)),
        ("xpan-65-24", (4288, 1583)),
    ],
)
def test_preset_pixel_sizes_landscape(preset, expected):
    c = default_crop(preset, 4288, 2848)
    assert crop_box(c.rect, 4288, 2848)[2:] == expected


def test_presets_on_portrait_frame_turn_around():
    x = crop_box(default_crop("xpan-65-24", 2848, 4288).rect, 2848, 4288)
    assert x[2:] == (1583, 4287)  # 高度受 4288 限制，按 24:65 取整
    flipped = default_crop("3:2", 4288, 2848, flip=True)
    assert crop_box(flipped.rect, 4288, 2848)[2:] == (1898, 2847)


def test_straighten_shrinks_box_and_stays_inside():
    for angle in (-10, -3.3, 0.1, 5, 10):
        for preset in cm.PRESETS:
            c = default_crop(preset, 4288, 2848, angle)
            x0, y0, w, h = crop_box(c.rect, 4288, 2848)
            assert cm.box_inside((x0, y0, w, h), 4288, 2848, angle, 1.0), (preset, angle)
    assert crop_box(default_crop("xpan-65-24", 4288, 2848, 5).rect, 4288, 2848)[2] < 4288


def test_validation_rejects_bad_input():
    bad = [
        ("xpan-65-24", [0, 0, 1, 1], 0.0, "ratio_mismatch"),
        ("free", [0.5, 0.5, 0.6, 0.6], 0.0, "bad_rect"),
        ("free", [0, 0, 0, 0.5], 0.0, "bad_rect"),
        ("free", [0, 0, 0.001, 0.001], 0.0, "too_small"),
        ("free", [0, 0, 1, 1], 10.5, "crop_outside_image"),
        ("free", [0, 0, 1, 1], 50.0, "bad_angle"),
        ("free", [0, 0, 1, 1], 5.0, "crop_outside_image"),
        ("free", [0, 0, float("nan"), 1], 0.0, "bad_rect"),
        ("nope", [0, 0, 1, 1], 0.0, "unknown_preset"),
    ]
    for preset, rect, angle, code in bad:
        with pytest.raises(CropError) as e:
            validate_crop(preset, rect, angle, 4288, 2848)
        assert e.value.code == code, (preset, rect, angle)


def test_dragging_xpan_up_and_down_stays_valid():
    base = default_crop("xpan-65-24", 4288, 2848)
    x, y, w, h = base.rect
    for ny in (0.0, 0.1, 1 - h):
        c = validate_crop("xpan-65-24", [x, ny, w, h], 0.0, 4288, 2848)
        assert c.rect[1] == ny
    with pytest.raises(CropError):
        validate_crop("xpan-65-24", [x, 1 - h + 0.01, w, h], 0.0, 4288, 2848)


def test_rotation_direction_positive_is_clockwise(tmp_path):
    """一条竖线，正角度（顺时针）后，线的上端应向右偏。"""
    im = Image.new("RGB", (800, 600), "white")
    for y in range(600):
        for x in range(398, 402):
            im.putpixel((x, y), (0, 0, 0))
    src = tmp_path / "line.jpg"
    im.save(src, quality=95)

    def black_x(img: Image.Image, row: int) -> float:
        xs = [x for x in range(img.width) if img.getpixel((x, row))[0] < 100]
        return sum(xs) / len(xs)

    data, _ = render(src, default_crop("free", 800, 600, 5.0), ExportOptions(None, 95))
    out = Image.open(io.BytesIO(data))
    # 上端比下端更靠右
    assert black_x(out, 20) - black_x(out, out.height - 20) > 20
    data0, size0 = render(src, default_crop("free", 800, 600, 0.0), ExportOptions(None, 95))
    assert size0 == (800, 600)


def test_sidecar_roundtrip_is_exact_to_1e9(volumes, card, lib):
    from gxr_studio.cards.discovery import find_cards
    from gxr_studio.cards.importer import ImportOptions, run_import

    run_import(lib, find_cards([volumes])[0], ImportOptions(min_free_bytes=0))
    shot = next(s for s in lib.list_shots(limit=100) if s["name"] == "R0000102")
    rng = random.Random(7)
    for _ in range(25):
        angle = round(rng.uniform(-3, 3), 6)
        base = default_crop("free", 640, 480, angle)
        # 在合法范围内随机缩小、平移，并带上不"整"的浮点数
        w = base.rect[2] * rng.uniform(0.5, 1.0)
        h = base.rect[3] * rng.uniform(0.5, 1.0)
        x = base.rect[0] + (base.rect[2] - w) * rng.random()
        y = base.rect[1] + (base.rect[3] - h) * rng.random()
        out = edits.write_crop(lib, shot, preset="free", rect=[x, y, w, h], angle_deg=angle)
        # 重新开一遍：从磁盘上的 sidecar 读
        raw = json.loads(lib.sidecar_path_for(shot).read_text(encoding="utf-8"))
        for got, want in zip(raw["crop"]["rect"], [x, y, w, h]):
            assert abs(got - want) < 1e-9
        assert abs(raw["crop"]["angle_deg"] - angle) < 1e-9
        assert edits.read_crop(lib, shot)["rect"] == out["rect"]
    # 索引里的缓存也一致
    assert lib.get_shot(shot["shot_id"])["crop"] == raw["crop"]


def test_sidecar_keeps_unknown_keys_and_refuses_newer_schema(volumes, card, lib):
    from gxr_studio.cards.discovery import find_cards
    from gxr_studio.cards.importer import ImportOptions, run_import

    run_import(lib, find_cards([volumes])[0], ImportOptions(min_free_bytes=0))
    shot = next(s for s in lib.list_shots(limit=100) if s["name"] == "R0000102")
    path = lib.sidecar_path_for(shot)
    data = sc.read(path)
    data["recipe"] = {"kind": "builtin", "slot": 5}  # M2 以后才会写的块
    sc.write_atomic(path, data, bump=False)
    edits.write_crop(lib, shot, preset="1:1", rect=None, angle_deg=0.0)
    after = sc.read(path)
    assert after["recipe"] == {"kind": "builtin", "slot": 5} and after["crop"]["preset"] == "1:1" and after["rev"] == 1
    after["schema"] = "gxr-studio/edit/9"
    path.write_text(json.dumps(after), encoding="utf-8")
    with pytest.raises(sc.SidecarTooNew):
        edits.write_crop(lib, shot, preset="free", rect=[0, 0, 1, 1], angle_deg=0.0)


def test_rev_conflict(volumes, card, lib):
    from gxr_studio.cards.discovery import find_cards
    from gxr_studio.cards.importer import ImportOptions, run_import

    run_import(lib, find_cards([volumes])[0], ImportOptions(min_free_bytes=0))
    shot = next(s for s in lib.list_shots(limit=100) if s["name"] == "R0000102")
    edits.write_crop(lib, shot, preset="1:1", rect=None, angle_deg=0.0, expected_rev=0)
    with pytest.raises(edits.RevisionConflict):
        edits.write_crop(lib, shot, preset="4:3", rect=None, angle_deg=0.0, expected_rev=0)
    assert edits.write_crop(lib, shot, preset="4:3", rect=None, angle_deg=0.0, expected_rev=1)["rev"] == 2
    assert edits.write_crop(lib, shot, preset=None, rect=None, angle_deg=0.0, reset=True)["active"] is False
