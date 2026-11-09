"""边框和时间戳的版面（finish/layout.py）：画布的边、版面落在画布里、导出照版面画。"""

from __future__ import annotations

import math

import numpy as np
import pytest
from PIL import Image

from gxr_studio.finish.fonts import ASSETS, FontMetrics
from gxr_studio.finish.frame import apply_frame
from gxr_studio.finish.layout import LOGO_SIZE, advance, frame_layout, stamp_layout
from gxr_studio.finish.spec import OUTPUT_RATIOS, FrameSpec, StampSpec

META = {
    "lens": "GR LENS A12 50mm F2.5 MACRO",
    "lens_short": "A12 50mm",
    "aperture": "f/2.5",
    "shutter": "1/250s",
    "iso": "ISO 200",
    "date": "2026.10.23",
    "frame_no": "0102",
}
SIZES = [(160, 120), (320, 240), (240, 320), (500, 500), (900, 600), (600, 900), (1600, 1066), (4288, 2848), (4288, 1583)]


def test_logo_size_constant_matches_the_file() -> None:
    with Image.open(ASSETS / "logo" / "ricoh.png") as logo:
        assert logo.size == LOGO_SIZE


@pytest.mark.parametrize(("w", "h"), SIZES)
@pytest.mark.parametrize("ratio", list(OUTPUT_RATIOS))
@pytest.mark.parametrize("color", ["white", "black", "cream"])
def test_gallery_keeps_the_white_border_margin_on_all_four_sides(w: int, h: int, ratio: str, color: str) -> None:
    """Medium mat has 9% minimums, with 45% of spare height above the photo."""
    layout = frame_layout(w, h, FrameSpec(style="gallery", color=color, output_ratio=ratio), META, FontMetrics())
    assert layout is not None
    x, y = layout.photo
    width, height = layout.width, layout.height
    assert min(x, width - w - x) >= math.ceil(0.09 * width)
    assert min(y, height - h - y) >= math.ceil(0.09 * height)
    assert abs((width - w - x) - x) <= 1
    assert y == max(math.ceil(0.09 * height), min(height - h - math.ceil(0.09 * height), round(0.45 * (height - h))))
    rw, rh = OUTPUT_RATIOS[ratio]
    assert width * rh == height * rw
    # A single smaller integer ratio unit cannot contain the photo and all minimum margins.
    pw, ph = width - rw, height - rh
    assert pw - w < 2 * math.ceil(0.09 * pw) or ph - h < 2 * math.ceil(0.09 * ph)


def test_gallery_with_a_crop_that_already_has_the_ratio_still_gets_a_border() -> None:
    """4:5 的照片放进画布 4:5：原来一点边也没有，现在四边都有。"""
    w, h = 2400, 3000
    layout = frame_layout(w, h, FrameSpec(style="gallery", output_ratio="4:5"), {}, FontMetrics())
    assert layout is not None
    margin = math.ceil(0.09 * layout.width)
    x, y = layout.photo
    assert x >= margin and y >= math.ceil(0.09 * layout.height)
    assert (layout.width, layout.height) == (2928, 3660)


def test_gallery_of_a_3_by_4_crop_in_4_by_5_has_no_bare_top_and_bottom() -> None:
    """3:4 的照片放进 4:5：原来只有左右两条细白边，上下没有；现在上下也有边，左右更宽。"""
    w, h = 2400, 3200
    layout = frame_layout(w, h, FrameSpec(style="gallery", output_ratio="4:5"), {}, FontMetrics())
    assert layout is not None
    margin = math.ceil(0.09 * layout.height)
    x, y = layout.photo
    assert y >= margin and layout.height - h - y >= margin
    assert (layout.width, layout.height) == (3124, 3905)
    assert (x, y) == (362, 352)


def test_gallery_export_pixels_follow_the_layout() -> None:
    rng = np.random.default_rng(3)
    photo = rng.integers(0, 256, size=(90, 120, 3), dtype=np.uint8)
    spec = FrameSpec(style="gallery", color="cream", output_ratio="1:1")
    out = apply_frame(photo, spec, {})
    layout = frame_layout(120, 90, spec, {}, FontMetrics())
    assert layout is not None and out.shape[:2] == (layout.height, layout.width)
    x, y = layout.photo
    assert np.array_equal(out[y : y + 90, x : x + 120], photo)
    assert tuple(out[0, 0]) == (244, 239, 230) and tuple(out[-1, -1]) == (244, 239, 230)


@pytest.mark.parametrize("style", ["gallery", "leica", "hasselblad", "film", "instant"])
@pytest.mark.parametrize(("w", "h"), SIZES)
def test_layout_items_stay_inside_the_frame_canvas(style: str, w: int, h: int) -> None:
    """每个尺寸、每种样式：照片和每一行字（整行的宽度）、标志都在画布里面。
    一行参数比照片还宽时先缩字号，再放不下就去掉最后几项，不靠量字的结果碰运气。"""
    spec = FrameSpec(style=style, text="Hello", show_date=True)
    metrics = FontMetrics()
    layout = frame_layout(w, h, spec, META, metrics)
    assert layout is not None
    x, y = layout.photo
    assert x >= 0 and y >= 0 and x + w <= layout.width and y + h <= layout.height
    for item in layout.items:
        if item["k"] == "text" and not item.get("rot"):
            width = advance(metrics, item["font"], item["px"], item["text"], item["cjk"])
            assert item["x"] >= 0 and item["x"] + width <= layout.width + 0.5, (item["text"], item["x"], width, layout.width)
            assert 0 <= item["y"] and item["y"] + item["px"] <= layout.height + 1, item
        elif item["k"] == "logo":
            assert 0 <= item["x"] and item["x"] + item["w"] <= layout.width and item["y"] + item["h"] <= layout.height, item


def test_hasselblad_500_square_keeps_its_long_line_inside() -> None:
    """CI 上出过的情形：500 × 500 的哈苏，参数行比画布宽，字被画到左边外面（x = -4.5）。"""
    metrics = FontMetrics()
    layout = frame_layout(500, 500, FrameSpec(style="hasselblad", text="Hello", show_date=True), META, metrics)
    assert layout is not None
    lines = [item for item in layout.items if item["k"] == "text"]
    assert len(lines) == 2
    for item in lines:
        assert item["x"] >= 0 and item["x"] + advance(metrics, item["font"], item["px"], item["text"], item["cjk"]) <= layout.width


def test_style_none_has_no_layout() -> None:
    assert frame_layout(100, 100, FrameSpec(style="none"), {}, FontMetrics()) is None


def test_every_glyph_has_a_column() -> None:
    """版面给每个字一个整像素的列，和字符一一对应；导出和预览都画在这些列上。"""
    metrics = FontMetrics()
    layout = frame_layout(1600, 1066, FrameSpec(style="leica", text="Hello", show_date=True), META, metrics)
    assert layout is not None
    for item in layout.items:
        if item["k"] == "text":
            assert len(item["xs"]) == len(item["text"])
            assert item["xs"] == sorted(item["xs"]) and all(isinstance(v, int) for v in item["xs"])


def test_stamp_layout_text_box_sits_inside_the_photo() -> None:
    metrics = FontMetrics()
    for w, h in SIZES:
        for corner in ("tl", "tr", "bl", "br"):
            spec = StampSpec(enabled=True, corner=corner, size="large")
            layout = stamp_layout(w, h, spec, "'26 10 23", metrics)
            assert layout is not None
            x, y, tw, th = layout.rect
            assert 0 <= x and 0 <= y and x + tw <= w and y + th <= h, (w, h, corner, layout.rect)


def test_stamp_layout_of_no_text_is_none() -> None:
    assert stamp_layout(100, 100, StampSpec(enabled=True), "", FontMetrics()) is None
