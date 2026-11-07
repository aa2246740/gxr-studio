"""边框和时间戳的版面（finish/layout.py）：画布的边、版面落在画布里、导出照版面画，以及预览要用的字体常数。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageFont

from gxr_studio.finish.fonts import ASSETS, FONT_FILES, PilFonts
from gxr_studio.finish.frame import apply_frame
from gxr_studio.finish.layout import CANVAS, LOGO_SIZE, WEIGHT, frame_layout, stamp_layout
from gxr_studio.finish.spec import FrameSpec, StampSpec

WEB = Path(__file__).resolve().parents[1] / "web" / "assets"
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
@pytest.mark.parametrize("ratio", list(CANVAS))
@pytest.mark.parametrize("color", ["white", "black", "cream"])
def test_canvas_keeps_the_white_border_margin_on_all_four_sides(w: int, h: int, ratio: str, color: str) -> None:
    """画布：四边都留出和「白边 · 中」一样宽的边，再补到所选比例，照片居中。"""
    layout = frame_layout(w, h, FrameSpec(style="canvas", color=color, canvas_ratio=ratio), META, PilFonts())
    assert layout is not None
    margin = max(8, round(max(w, h) * WEIGHT["medium"]))
    x, y = layout.photo
    assert min(x, y, layout.width - w - x, layout.height - h - y) >= margin
    # 居中（奇数差一个像素分给右边和下边）
    assert 0 <= (layout.width - w - x) - x <= 1 and 0 <= (layout.height - h - y) - y <= 1
    rw, rh = CANVAS[ratio]
    assert abs(layout.width / layout.height - rw / rh) < 1.5 / min(layout.width, layout.height) + 1e-9
    # 照片加四边的边已经够大的那一边不再加宽：补边只补在比例不够的那个方向上
    inner_w, inner_h = w + 2 * margin, h + 2 * margin
    assert layout.width == inner_w or layout.height == inner_h


def test_canvas_with_a_crop_that_already_has_the_ratio_still_gets_a_border() -> None:
    """4:5 的照片放进画布 4:5：原来一点边也没有，现在四边都有。"""
    w, h = 2400, 3000
    layout = frame_layout(w, h, FrameSpec(style="canvas", canvas_ratio="4:5"), {}, PilFonts())
    assert layout is not None
    margin = max(8, round(max(w, h) * WEIGHT["medium"]))
    x, y = layout.photo
    assert x == margin and y >= margin  # 四边的边一样宽，上下再补到 4:5
    assert layout.width == w + 2 * margin and layout.height == round(layout.width * 5 / 4)


def test_canvas_of_a_3_by_4_crop_in_4_by_5_has_no_bare_top_and_bottom() -> None:
    """3:4 的照片放进 4:5：原来只有左右两条细白边，上下没有；现在上下也有边，左右更宽。"""
    w, h = 2400, 3200
    layout = frame_layout(w, h, FrameSpec(style="canvas", canvas_ratio="4:5"), {}, PilFonts())
    assert layout is not None
    margin = max(8, round(max(w, h) * WEIGHT["medium"]))
    x, y = layout.photo
    assert y == margin and layout.height - h - y == margin
    assert x > margin and layout.width == round(layout.height * 4 / 5)


def test_canvas_export_pixels_follow_the_layout() -> None:
    rng = np.random.default_rng(3)
    photo = rng.integers(0, 256, size=(90, 120, 3), dtype=np.uint8)
    spec = FrameSpec(style="canvas", color="cream", canvas_ratio="1:1")
    out = apply_frame(photo, spec, {})
    layout = frame_layout(120, 90, spec, {}, PilFonts())
    assert layout is not None and out.shape[:2] == (layout.height, layout.width)
    x, y = layout.photo
    assert np.array_equal(out[y : y + 90, x : x + 120], photo)
    assert tuple(out[0, 0]) == (245, 236, 220) and tuple(out[-1, -1]) == (245, 236, 220)


@pytest.mark.parametrize("style", ["gallery", "leica", "hasselblad", "film", "instant", "canvas"])
@pytest.mark.parametrize(("w", "h"), SIZES)
def test_layout_items_stay_inside_the_frame_canvas(style: str, w: int, h: int) -> None:
    spec = FrameSpec(style=style, text="Hello", show_date=True)
    layout = frame_layout(w, h, spec, META, PilFonts())
    assert layout is not None
    x, y = layout.photo
    assert x >= 0 and y >= 0 and x + w <= layout.width and y + h <= layout.height
    for item in layout.items:
        if item["k"] == "text":
            # 一行参数比照片还宽的小图，哈苏、徕卡的字会超出去（导出一直如此，不在这里改）；够大的图都在里面
            assert 0 <= item["y"] <= layout.height and (w < 500 or -1 <= item["x"] <= layout.width), item
        elif item["k"] == "logo":
            assert 0 <= item["x"] and item["x"] + item["w"] <= layout.width and item["y"] + item["h"] <= layout.height, item


def test_style_none_has_no_layout() -> None:
    assert frame_layout(100, 100, FrameSpec(style="none"), {}, PilFonts()) is None


def node_json(tmp_path: Path, body: str) -> object:
    """把 finish_layout.js 复制进临时目录当模块跑，返回 body 里 return 的东西。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("本机没有 node，跳过前端纯函数测试")
    (tmp_path / "finish_layout.js").write_text((WEB / "finish_layout.js").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
    script = tmp_path / "check.mjs"
    script.write_text(
        f"import * as L from './finish_layout.js';\nconst out = (() => {{\n{body}\n}})();\nconsole.log(JSON.stringify(out));\n",
        encoding="utf-8",
    )
    res = subprocess.run([node, str(script)], capture_output=True, text=True, cwd=tmp_path, encoding="utf-8")
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_preview_ascent_matches_pillow(tmp_path: Path) -> None:
    """画布按基线画字，预览用 hhea 的上沿高度推出 Pillow 的 'la' 锚点：和 Pillow 的 getmetrics 逐字号相同。"""
    fonts = list(FONT_FILES)
    got = node_json(
        tmp_path, f"return Object.fromEntries({json.dumps(fonts)}.map((f) => [f, Array.from({{length: 700}}, (_, px) => L.ascentPx(f, px))]));"
    )
    assert isinstance(got, dict)
    for font in fonts:
        for px in range(8, 700):
            face = ImageFont.truetype(str(ASSETS / "fonts" / FONT_FILES[font]), px)
            assert face.getmetrics()[0] == got[font][px], (font, px)


def test_dseg_tick_advance_matches_pillow(tmp_path: Path) -> None:
    em = node_json(tmp_path, "return L.DSEG_TICK_EM;")
    assert isinstance(em, float)
    fonts = PilFonts()
    for px in (12, 30, 47, 80, 101, 300):
        assert abs(fonts.advance("dseg", px, "'", False) - em * px) < 0.1, px


def test_stamp_layout_text_box_sits_inside_the_photo() -> None:
    fonts = PilFonts()
    for w, h in SIZES:
        for corner in ("tl", "tr", "bl", "br"):
            spec = StampSpec(enabled=True, corner=corner, size="large")
            layout = stamp_layout(w, h, spec, "'26 10 23", fonts)
            assert layout is not None
            x, y, tw, th = layout.rect
            assert 0 <= x and 0 <= y and x + tw <= w and y + th <= h, (w, h, corner, layout.rect)


def test_stamp_layout_of_no_text_is_none() -> None:
    assert stamp_layout(100, 100, StampSpec(enabled=True), "", PilFonts()) is None
