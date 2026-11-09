"""Rendered results of the new frame item kinds, using synthetic photographs."""

from __future__ import annotations

import hashlib
import math

import numpy as np
import pytest

from gxr_studio.finish.fonts import ASSETS, FontMetrics
from gxr_studio.finish.frame import apply_frame
from gxr_studio.finish.layout import frame_layout, stamp_layout
from gxr_studio.finish.spec import FrameSpec, StampSpec


@pytest.mark.parametrize(("width", "height"), [(600, 400), (400, 600)])
@pytest.mark.parametrize("fmt", ["135", "120"])
@pytest.mark.parametrize("table", ["light", "black"])
def test_film_keeps_the_photo_and_perforations_expose_the_table(width, height, fmt, table):
    photo = np.full((height, width, 3), (31, 96, 151), np.uint8)
    spec = FrameSpec(style="film", fmt=fmt, table=table, output_ratio="4:5")
    layout = frame_layout(width, height, spec, {}, FontMetrics())
    assert layout is not None
    out = apply_frame(photo, spec, {})
    x, y = layout.photo
    assert np.array_equal(out[y : y + height, x : x + width], photo)
    holes = [i for i in layout.items if i["k"] == "hole" or i["k"] == "rect" and i.get("fill") == (232, 228, 218)]
    assert bool(holes) == (fmt == "135")
    for hole in holes:
        hx, hy = hole["x"] + hole["w"] // 2, hole["y"] + hole["h"] // 2
        pixel = out[hy, hx]
        assert min(pixel) > 210
    if height > width:
        texts = [i for i in layout.items if i["k"] == "text"]
        assert texts and all(i.get("rot") for i in texts)
        assert all(i["gx"] < x or i["gx"] >= x + width for i in texts)


def test_instant_fixed_ratio_has_a_rounded_card_shadow_and_an_empty_chin():
    spec = FrameSpec(style="instant", output_ratio="4:5")
    layout = frame_layout(600, 400, spec, {"lens_short": "A12 50mm"}, FontMetrics())
    assert layout is not None
    assert not [i for i in layout.items if i["k"] == "text"]
    out = apply_frame(np.full((400, 600, 3), 100, np.uint8), spec, {"lens_short": "A12 50mm"})
    x, y, w, h = layout.unit_box
    assert np.all(out[y, x] < np.array([232, 228, 220]))  # Rounded corner exposes the shadowed stone.
    assert tuple(out[y + h // 2, x + 2]) == (250, 248, 243)
    assert all(out[y + h + 4, x + w // 2] < np.array([232, 228, 220]))


def test_inter_semibold_is_the_approved_official_file():
    assert (
        hashlib.sha256((ASSETS / "fonts" / "Inter-SemiBold.ttf").read_bytes()).hexdigest()
        == "78a843fade9d4612a5567302fb595b56976eb5fcebf4fea5a5912d638bafcde3"
    )


@pytest.mark.parametrize(("width", "height"), [(1200, 800), (800, 1200)])
@pytest.mark.parametrize("fmt", ["polaroid", "instax_square", "instax_wide", "instax_mini"])
@pytest.mark.parametrize("ratio", ["original", "4:5", "16:9"])
@pytest.mark.parametrize("caption", ["", "Harbour"])
@pytest.mark.parametrize(("font", "glow"), [("sans", False), ("seg7", True)])
def test_border_stamp_stays_on_the_instant_paper(width, height, fmt, ratio, caption, font, glow):
    metrics = FontMetrics()
    frame = frame_layout(width, height, FrameSpec(style="instant", fmt=fmt, output_ratio=ratio, text=caption), {}, metrics)
    assert frame is not None
    spec = StampSpec(enabled=True, placement="border", font=font, glow=glow)
    stamp = stamp_layout(width, height, spec, "2026.11.10", metrics, frame)
    assert stamp is not None
    x, y, w, h = stamp.rect
    ux, uy, uw, uh = frame.unit_box
    bleed = math.ceil(3 * stamp.glow) + 2 if stamp.glow is not None else 0
    assert x - bleed >= ux and x + w + bleed <= ux + uw
    assert y - bleed >= frame.photo[1] + height and y + h + bleed <= uy + uh
