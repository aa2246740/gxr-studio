"""边框和时间戳的字体：版面用的表是从字体文件读的，不依赖 Pillow 的排版引擎（RAQM / BASIC），也不依赖这台电脑有没有 libraqm。

CI 的 macOS 机器没有 fribidi（Pillow 退到 BASIC），开发机有（RAQM）：同一个字号量出来的宽度不一样，
版面（和导出的字节）因此随电脑变。这里证明现在不会：表和 Pillow 逐字相符，版面不碰 Pillow 的量字，
每个字体对象都明确用 BASIC，两种引擎下导出的字节一样。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont, features

from gxr_studio.finish.fonts import ASSETS, FONT_FILES, FontMetrics, PilFonts, font_table, font_table_json, table_dict
from gxr_studio.finish.frame import apply_frame
from gxr_studio.finish.layout import ascent_px, frame_layout, stamp_layout
from gxr_studio.finish.spec import FrameSpec, StampSpec
from gxr_studio.finish.stamp import apply_stamp, cjk_font

WEB = Path(__file__).resolve().parents[1] / "web" / "assets"
BASIC = ImageFont.Layout.BASIC
META = {
    "lens": "GR LENS A12 50mm F2.5 MACRO",
    "lens_short": "A12 50mm",
    "aperture": "f/2.5",
    "shutter": "1/250s",
    "iso": "ISO 200",
    "date": "2026.10.23",
    "frame_no": "0102",
}


def path_of(font: str) -> str:
    return str(ASSETS / "fonts" / FONT_FILES[font])


@pytest.mark.parametrize("font", list(FONT_FILES))
def test_table_advances_equal_pillow_for_every_glyph_the_font_has(font: str) -> None:
    """字号 = upm 时一个字体单位正好一个像素，Pillow（BASIC）量出的步进就是表里的整数：每个字都对；没有的字都是 .notdef 的宽度。"""
    table = font_table(font)
    face = ImageFont.truetype(path_of(font), table.upm, layout_engine=BASIC)
    wrong = [hex(code) for code, (adv, _box) in table.glyphs.items() if face.getlength(chr(code)) != adv]
    assert wrong == [], wrong[:10]
    missing = next(code for code in range(0xE000, 0xF900) if code not in table.glyphs)
    assert face.getlength(chr(missing)) == table.notdef[0]


@pytest.mark.parametrize("font", list(FONT_FILES))
def test_table_boxes_match_the_ink_pillow_draws(font: str) -> None:
    """字形外框（glyf 的头）和 Pillow 在 100 像素画出来的墨迹：差不到 1.6 像素。
    不到 3% 的字形被字体指令挪了位置（最多 9 像素），不影响版面用的整数。"""
    table = font_table(font)
    px = 100
    face = ImageFont.truetype(path_of(font), px, layout_engine=BASIC)
    k = px / table.upm
    worst = 0.0
    off = 0
    for code, (_adv, box) in table.glyphs.items():
        image = Image.new("L", (400, 400), 0)
        ImageDraw.Draw(image).text((150, 250), chr(code), font=face, fill=255, anchor="ls")
        ink = image.getbbox()
        if box is None:
            assert ink is None, hex(code)
            continue
        assert ink is not None, hex(code)
        gap = max(
            abs(ink[0] - 150 - box[0] * k), abs(250 - ink[1] - box[3] * k), abs(ink[2] - 150 - box[2] * k), abs(250 - ink[3] - box[1] * k)
        )
        worst = max(worst, gap)
        off += gap > 1.6
    assert worst <= 9 and off <= 0.03 * len(table.glyphs), (font, worst, off)


@pytest.mark.parametrize("font", list(FONT_FILES))
def test_ascent_formula_matches_pillow_for_every_size(font: str) -> None:
    """Pillow 画字用 'la' 锚点；预览按基线画字，要同样的上沿高度：公式和 getmetrics 逐字号相同。"""
    table = font_table(font)
    for px in range(8, 700):
        face = ImageFont.truetype(path_of(font), px, layout_engine=BASIC)
        assert ascent_px(table.ascender, table.upm, px) == face.getmetrics()[0], (font, px)


def test_js_ascent_equals_python(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("本机没有 node，跳过前端纯函数测试")
    (tmp_path / "finish_layout.js").write_text((WEB / "finish_layout.js").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
    cases = {font: [font_table(font).ascender, font_table(font).upm] for font in FONT_FILES}
    script = (
        "import { ascentPx } from './finish_layout.js';\n"
        f"const cases = {json.dumps(cases)};\n"
        "const out = {};\n"
        "for (const [font, [a, u]] of Object.entries(cases)) out[font] = Array.from({ length: 800 }, (_, px) => ascentPx(a, u, px));\n"
        "console.log(JSON.stringify(out));\n"
    )
    (tmp_path / "check.mjs").write_text(script, encoding="utf-8")
    res = subprocess.run([node, "check.mjs"], capture_output=True, text=True, cwd=tmp_path, encoding="utf-8")
    assert res.returncode == 0, res.stderr
    got = json.loads(res.stdout)
    for font, (ascender, upm) in cases.items():
        assert got[font] == [ascent_px(ascender, upm, px) for px in range(800)], font


def test_the_browser_gets_the_same_table() -> None:
    for font in FONT_FILES:
        sent = json.loads(font_table_json(font))
        assert sent == table_dict(font)
        assert sent["cps"] == sorted(sent["cps"]) and len(sent["adv"]) == len(sent["cps"]) and len(sent["box"]) == 4 * len(sent["cps"])


# ---- 排版引擎 -------------------------------------------------------------------------------------------------


def _cases() -> dict[str, np.ndarray]:
    """带字的边框和时间戳：每种样式和字体各几张，导出的像素。"""
    rng = np.random.default_rng(11)
    out: dict[str, np.ndarray] = {}
    for w, h in ((640, 480), (500, 500), (1200, 800)):
        photo = rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)
        for style in ("leica", "hasselblad", "film", "instant"):
            spec = FrameSpec(style=style, text="Hello 42", show_date=True)
            out[f"{style}-{w}x{h}"] = apply_frame(photo, spec, META)
        for font in ("seg7", "typewriter", "sans"):
            stamp = StampSpec(enabled=True, content="date+text", text="Paris", font=font, show_time=True, corner="br")
            out[f"stamp-{font}-{w}x{h}"] = apply_stamp(photo, stamp, datetime(2026, 10, 23, 17, 41), None)
    return out


def _digest(arrays: dict[str, np.ndarray]) -> dict[str, str]:
    return {key: hashlib.sha256(array.tobytes()).hexdigest() for key, array in arrays.items()}


@pytest.mark.parametrize("have_raqm", [False, True])
def test_every_face_uses_basic_whatever_pillow_would_pick(monkeypatch: pytest.MonkeyPatch, have_raqm: bool) -> None:
    """不指定引擎时 Pillow 看 core.HAVE_RAQM 选；我们明确指定 BASIC：这台电脑装没装 libraqm 都一样。"""
    monkeypatch.setattr(ImageFont.core, "HAVE_RAQM", have_raqm)
    fonts = PilFonts()
    for font in FONT_FILES:
        assert fonts.face(font, 20, False).layout_engine == BASIC
    cjk = cjk_font(20)
    if cjk is not None:
        assert cjk.layout_engine == BASIC


def test_export_bytes_do_not_depend_on_the_layout_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制两种引擎（有 / 没有 libraqm）各导出一遍：带字的边框和时间戳逐字节相同。"""
    digests = []
    for have_raqm in (False, True):
        monkeypatch.setattr(ImageFont.core, "HAVE_RAQM", have_raqm)
        digests.append(_digest(_cases()))
    assert digests[0] == digests[1]


def test_the_control_would_notice_an_engine_difference() -> None:
    """对照：不指定引擎的字体对象，在有 libraqm 的电脑上和 BASIC 量出来的宽度不一样（所以上面的测试不是摆设）。"""
    if not features.check("raqm"):
        pytest.skip("这台电脑没有 libraqm，没有 RAQM 可比")
    table = font_table("dseg")
    text = "'88 11 06"
    plain = ImageFont.truetype(path_of("dseg"), 12)
    basic = ImageFont.truetype(path_of("dseg"), 12, layout_engine=BASIC)
    assert plain.layout_engine == ImageFont.Layout.RAQM
    assert plain.getlength(text) != basic.getlength(text)
    assert table.upm == 1000  # 表里的量法和两个引擎都无关


def test_layout_never_asks_pillow_to_measure_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """版面全靠字体表和整数算式：把 Pillow 的量字换成会报错的，内置字体的版面照样算得出来。"""

    def refuse(*_args: object, **_kwargs: object) -> float:
        raise AssertionError("版面不该问 Pillow 量字")

    monkeypatch.setattr(ImageFont.FreeTypeFont, "getlength", refuse)
    monkeypatch.setattr(ImageFont.FreeTypeFont, "getbbox", refuse)
    monkeypatch.setattr(ImageDraw.ImageDraw, "textlength", refuse)
    monkeypatch.setattr(ImageDraw.ImageDraw, "textbbox", refuse)
    metrics = FontMetrics()
    for style in ("leica", "hasselblad", "film", "instant"):
        assert frame_layout(900, 600, FrameSpec(style=style, text="Hello", show_date=True), META, metrics) is not None
    for font in ("seg7", "typewriter", "sans"):
        assert stamp_layout(900, 600, StampSpec(enabled=True, font=font), "'26 10 23 17:41", metrics) is not None


def test_dseg_tick_has_the_notdef_advance_at_every_size() -> None:
    """数码管没有撇号：缺字用 .notdef 的宽度（版面里撇号单独画，只占这么宽）。表里没有它，查出来就是 .notdef，按字号线性放大。"""
    table = font_table("dseg")
    assert ord("'") not in table.glyphs
    metrics = FontMetrics()
    assert metrics.glyph("dseg", "'", False) == table.notdef
