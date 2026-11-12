"""预览的版面（web/assets/finish_layout.js）和导出的版面（finish/layout.py）逐项相同。

两边用同样的输入：每种样式 × 每个选项 × 横、竖、方、条幅几种大小的照片 × 几份拍摄信息，再加每种日期戳字体、字号、位置。
字体表（步进宽度、字形外框、上沿高度）从字体文件里读，Python 直接用，node 拿到的是和浏览器一样的那份 JSON（`font_table_json`）；
中文要系统字体，node 里没有，Python 把量过的字记下来交给 node（只验证算式）。
矩形、线、标志、字号、每个字的列逐个数字相同（浮点误差以内）；画出来的字符串逐字相同。
真浏览器里的同一件事，在 tests/test_viewer_browser.py::test_browser_layout_matches_the_export_layout 里查。
"""

from __future__ import annotations

import itertools
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from gxr_studio.finish import stamp as stamp_mod
from gxr_studio.finish.fonts import FONT_FILES, FontMetrics, table_dict
from gxr_studio.finish.layout import frame_layout, stamp_layout
from gxr_studio.finish.logo_math import LogoInfo
from gxr_studio.finish.spec import LOGO_STYLES, OUTPUT_RATIOS, FrameSpec, StampSpec, parse_frame
from gxr_studio.finish.stamp import format_stamp

WEB = Path(__file__).resolve().parents[1] / "web" / "assets"
FIXTURE_CJK = Path(__file__).resolve().parent / "fixtures" / "NotoSansSC-Shanghai.otf"
SIZES = [
    (160, 120),
    (320, 240),
    (240, 320),
    (500, 500),
    (900, 600),
    (600, 900),
    (1600, 1066),
    (1066, 1600),
    (4288, 2848),
    (2848, 4288),
    (4288, 1583),
]
METAS = [
    {
        "lens": "GR LENS A12 50mm F2.5 MACRO",
        "lens_short": "A12 50mm",
        "aperture": "f/2.5",
        "shutter": "1/250s",
        "iso": "ISO 200",
        "date": "2026.10.23",
        "frame_no": "0102",
    },
    {"lens": "", "lens_short": "", "aperture": "", "shutter": "", "iso": "", "date": "", "frame_no": ""},
    {
        "lens": "GR LENS A12 28mm F2.5",
        "lens_short": "GR LENS A12 28mm F2.5",
        "aperture": "f/16",
        "shutter": "30s",
        "iso": "ISO 3200",
        "date": "2026.01.02",
        "frame_no": "9999",
    },
]
TEXTS = ["", "Hello", "A very long caption that goes on and on 1234", "上海"]
WHENS = [datetime(2026, 10, 23, 17, 41), datetime(1999, 1, 2, 3, 4), datetime(2026, 12, 31, 23, 59)]


class Recorder(FontMetrics):
    """FontMetrics，并把中文字体问过的东西记下来（node 里没有系统字体，只能照记下的答）。"""

    def __init__(self) -> None:
        super().__init__()
        self.cjk_glyphs: dict[str, Any] = {}
        self.cjk_ascents: dict[str, int] = {}

    def glyph(self, font: str, ch: str, cjk: bool) -> Any:
        value = super().glyph(font, ch, cjk)
        if cjk:
            self.cjk_glyphs[ch] = [value[0], list(value[1]) if value[1] else None]
        return value

    def ascent(self, font: str, px: int, cjk: bool) -> int:
        value = super().ascent(font, px, cjk)
        if cjk:
            self.cjk_ascents[str(px)] = value
        return value


def frame_specs(style: str, wide: bool) -> list[FrameSpec]:
    colors = ("white",) if wide else ("white", "black", "cream")
    weights = ("medium",) if wide else ("thin", "medium", "thick")
    out = []
    for color, weight, (logo, params, date), text in itertools.product(
        colors, weights, ((True, True, False), (False, False, True), (True, False, True)), TEXTS
    ):
        out.append(FrameSpec(style=style, color=color, weight=weight, show_logo=logo, show_params=params, show_date=date, text=text))
    out.append(FrameSpec(style=style, text="北京" * 12))
    return out


def stamp_specs(wide: bool) -> list[tuple[StampSpec, datetime | None]]:
    out: list[tuple[StampSpec, datetime | None]] = []
    sizes = ("medium",) if wide else ("small", "medium", "large")
    corners = ("br", "tl") if wide else ("tl", "tr", "bl", "br")
    for font, size, corner, glow in itertools.product(("seg7", "typewriter", "sans"), sizes, corners, (True, False)):
        for content, text, fmt, show_time in (
            ("date", "", "'yy mm dd", False),
            ("date", "", "yyyy.mm.dd", True),
            ("date", "", "yy mm dd", False),
            ("date", "", "mm dd 'yy", True),
            ("text", "Hello 42", "'yy mm dd", False),
            ("date+text", "Paris", "'yy mm dd", False),
            ("text", "上海" * 3, "'yy mm dd", False),
            ("text", "W" * 24, "'yy mm dd", False),
            ("text", "x" * 40, "'yy mm dd", False),
        ):
            spec = StampSpec(
                enabled=True,
                content=content,
                text=text,
                date_format=fmt,
                show_time=show_time,
                font=font,
                size=size,
                corner=corner,
                glow=glow,
                color="red",
            )
            out.append((spec, WHENS[len(out) % len(WHENS)]))
    out.append((StampSpec(enabled=True, content="date", date_override="2024-02-29", show_time=True), WHENS[0]))
    out.append((StampSpec(enabled=True, content="date"), None))
    out.append((StampSpec(enabled=True, content="text", text=""), WHENS[0]))
    return out


def when_json(moment: datetime | None) -> dict[str, int] | None:
    if moment is None:
        return None
    return {"year": moment.year, "month": moment.month, "day": moment.day, "hour": moment.hour, "minute": moment.minute}


JS = """
import * as L from './finish_layout.js';
import * as F from './finish_fonts.js';
import { useLogos } from './finish_frame_core.js';
import { readFileSync } from 'node:fs';
const data = JSON.parse(readFileSync(process.argv[2], 'utf8'));
for (const [id, table] of Object.entries(data.tables)) F.useTable(id, table);
useLogos(data.logos || []);
const need = (map, key) => {
  if (!(key in map)) throw new Error('Python 没有问过这个：' + key);
  return map[key];
};
// 内置字体用 finish_fonts.js 的 metrics（和浏览器同一份代码）；中文字体用 Python 记下的答案
const m = {
  upm: (f, c) => (c ? 1000 : F.metrics.upm(f, false)),
  glyph: (f, ch, c) => (c ? need(data.cjk.glyphs, ch) : F.metrics.glyph(f, ch, false)),
  ascent: (f, px, c) => (c ? need(data.cjk.ascents, String(px)) : F.metrics.ascent(f, px, false)),
};
const frames = data.frames.map((c) => L.frameLayout(c.w, c.h, c.spec, c.meta, m));
const stamps = data.stamps.map((c) => {
  const text = L.stampText(c.spec, c.when);
  const frame = c.frameSpec ? L.frameLayout(c.w, c.h, c.frameSpec, {}, m) : null;
  return { text, layout: L.stampLayout(c.w, c.h, c.spec, text, m, frame) };
});
console.log(JSON.stringify({ frames, stamps }));
"""


def same(a: Any, b: Any, path: str = "") -> None:
    """数字在 1e-9 以内相同，其余逐项相等。"""
    if a == b:
        return
    if isinstance(a, dict):
        assert isinstance(b, dict) and a.keys() == b.keys(), (path, sorted(a), sorted(b))
        for k in a:
            same(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        assert isinstance(b, list) and len(a) == len(b), (path, a, b)
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            same(x, y, f"{path}[{i}]")
    elif isinstance(a, (int, float)) and not isinstance(a, bool):
        assert isinstance(b, (int, float)) and abs(a - b) < 1e-9, (path, a, b)
    else:
        assert a == b, (path, a, b)


def test_preview_layout_equals_export_layout(tmp_path: Path, approved_frame_layouts) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("本机没有 node，跳过前端纯函数测试")
    saved = stamp_mod._CJK_FONTS
    stamp_mod._CJK_FONTS = (FIXTURE_CJK,)
    try:
        rec = Recorder()
        frame_cases: list[dict[str, Any]] = []
        frame_want: list[Any] = []
        stamp_cases: list[dict[str, Any]] = []
        stamp_want: list[Any] = []
        for w, h in SIZES:
            wide = w * h > 2_000_000
            for style in ("gallery", "leica", "hasselblad", "film", "instant"):
                for spec in frame_specs(style, wide):
                    for meta in METAS[:1] if wide else METAS:
                        frame_cases.append({"w": w, "h": h, "spec": spec.to_json(), "meta": meta})
                        got = frame_layout(w, h, spec, meta, rec)
                        frame_want.append(None if got is None else got.to_json())
            frame_cases.append({"w": w, "h": h, "spec": FrameSpec(style="none").to_json(), "meta": METAS[0]})
            frame_want.append(None)
            for spec, moment in stamp_specs(wide):
                text = format_stamp(spec, moment, None)
                stamp_cases.append({"w": w, "h": h, "spec": spec.to_json(), "when": when_json(moment)})
                got_stamp = stamp_layout(w, h, spec, text, rec)
                stamp_want.append({"text": text, "layout": None if got_stamp is None else got_stamp.to_json()})
            for caption, font, size, corner, glow in itertools.product(
                ("", "Paris"), ("seg7", "typewriter", "sans"), ("small", "large"), ("tl", "tr", "bl", "br"), (False, True)
            ):
                border_spec = FrameSpec(style="instant", text=caption)
                spec = StampSpec(enabled=True, placement="border", font=font, size=size, corner=corner, glow=glow)
                text = format_stamp(spec, WHENS[0], None)
                frame = frame_layout(w, h, border_spec, {}, rec)
                got_stamp = stamp_layout(w, h, spec, text, rec, frame)
                stamp_cases.append(
                    {"w": w, "h": h, "spec": spec.to_json(), "when": when_json(WHENS[0]), "frameSpec": border_spec.to_json(), "preset": "4:3"}
                )
                stamp_want.append({"text": text, "layout": None if got_stamp is None else got_stamp.to_json()})
        for preset, w, h in (
            ("4:3", 240, 320),
            ("4:3", 2400, 3200),
            ("4:5", 2400, 3000),
            ("1:1", 500, 500),
            ("16:9", 180, 320),
            ("16:9", 1800, 3200),
            ("xpan-65-24", 325, 120),
            ("xpan-65-24", 3250, 1200),
            ("xpan-65-24", 120, 325),
            ("original", 900, 600),
            ("original", 600, 900),
            ("free", 900, 600),
            ("16:10", 800, 500),
            ("1.91:1", 1910, 1000),
            ("2.35:1", 2350, 1000),
        ):
            for style, ratio in itertools.product(("none", "gallery", "leica", "hasselblad", "film", "instant"), (*OUTPUT_RATIOS, "original")):
                spec = FrameSpec(style=style, output_ratio=ratio, text="Hello", show_date=True)
                frame_cases.append({"w": w, "h": h, "spec": spec.to_json(), "meta": METAS[0], "preset": preset})
                got = frame_layout(w, h, spec, METAS[0], rec)
                frame_want.append(None if got is None else got.to_json())
        # Every approved option, every finished ratio, all six requested photo shapes.
        for case, layout in approved_frame_layouts:
            spec = parse_frame(case["spec"])
            assert spec is not None
            w, h = case["size"]
            frame_cases.append({"w": w, "h": h, "spec": spec.to_json(), "meta": case["meta"]})
            frame_want.append(layout)
        for style, size, text in itertools.product(
            ("thin_keyline", "exhibition_label", "blur_fill", "cinema", "slide_mount", "darkroom", "postage_stamp"),
            ((320, 240), (800, 1200), (1300, 480)),
            ("上海", "北京" * 12),
        ):
            spec = FrameSpec(style=style, output_ratio="4:5", text=text)
            layout = frame_layout(*size, spec, METAS[0], rec)
            assert layout is not None
            frame_cases.append({"w": size[0], "h": size[1], "spec": spec.to_json(), "meta": METAS[0]})
            frame_want.append(layout.to_json())
        sample_logos = [
            LogoInfo("wordmark", "Wordmark", 734, 120, 0.34, False, 24.0),
            LogoInfo("round_badge", "Round", 360, 360, 0.58, False, 232.0),
            LogoInfo("tall_mark", "Tall", 180, 420, 0.28, False, 30.0),
            LogoInfo("script_sig", "Script", 620, 230, 0.09, False, 18.0),
            LogoInfo("opaque_card", "Opaque", 300, 180, 1.0, True, 200.0),
        ]
        logo_map = {lg.id: lg for lg in sample_logos}
        logo_json = [
            {
                "id": lg.id,
                "name": lg.name,
                "w": lg.w,
                "h": lg.h,
                "coverage": lg.coverage,
                "opaque": lg.opaque,
                "lum": lg.lum,
            }
            for lg in sample_logos
        ]
        for style in sorted(LOGO_STYLES):
            colors = ("cream", "black") if style == "slide_mount" else ("white", "black", "cream")
            for (w, h), logo_id, mode, lsize, color, meta in itertools.product(
                ((900, 600), (600, 900)),
                ("wordmark", "round_badge", "tall_mark", "script_sig", "opaque_card", "none", "missing_logo", "constructor", "__proto__"),
                ("auto", "orig", "mono"),
                ("small", "medium", "large"),
                colors[:2],
                (METAS[0], METAS[1]),
            ):
                if logo_id in ("none", "missing_logo", "constructor", "__proto__") and (mode != "auto" or lsize != "medium"):
                    continue
                for model_on in (True, False) if style == "leica" else (True,):
                    spec = FrameSpec(
                        style=style,
                        color=color,
                        output_ratio="4:5",
                        logo=logo_id,
                        logo_mode=mode,
                        logo_size=lsize,
                        model=model_on,
                        text="上海" if logo_id == "wordmark" else "",
                        show_params=logo_id != "tall_mark",
                    )
                    layout = frame_layout(w, h, spec, meta, rec, logo_map)
                    assert layout is not None
                    frame_cases.append({"w": w, "h": h, "spec": spec.to_json(), "meta": meta})
                    frame_want.append(layout.to_json())
        for name in (
            "finish_layout.js",
            "finish_fonts.js",
            "finish_frame_core.js",
            "logo_catalog.js",
            "finish_frame_styles.js",
            "finish_frame_objects.js",
            "finish_frame_new.js",
        ):
            (tmp_path / name).write_text((WEB / name).read_text(encoding="utf-8"), encoding="utf-8")
        (tmp_path / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
        (tmp_path / "check.mjs").write_text(JS, encoding="utf-8")
        data = tmp_path / "cases.json"
        tables = {font: table_dict(font) for font in FONT_FILES}
        cjk = {"glyphs": rec.cjk_glyphs, "ascents": rec.cjk_ascents}
        data.write_text(
            json.dumps(
                {"tables": tables, "cjk": cjk, "logos": logo_json, "frames": frame_cases, "stamps": stamp_cases},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        res = subprocess.run([node, "check.mjs", str(data)], capture_output=True, text=True, cwd=tmp_path, encoding="utf-8")
        assert res.returncode == 0, res.stderr[-3000:]
        got_js = json.loads(res.stdout)
    finally:
        stamp_mod._CJK_FONTS = saved
    assert len(got_js["frames"]) == len(frame_want) > 2000
    for i, (case, want) in enumerate(zip(frame_cases, frame_want, strict=True)):
        same(want, got_js["frames"][i], f"frame {case['w']}x{case['h']} {case['spec']['style']} #{i}")
    assert len(got_js["stamps"]) == len(stamp_want) > 1000
    for i, (case, want) in enumerate(zip(stamp_cases, stamp_want, strict=True)):
        same(want, got_js["stamps"][i], f"stamp {case['w']}x{case['h']} #{i}")
