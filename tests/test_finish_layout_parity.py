"""预览的版面（web/assets/finish_layout.js）和导出的版面（finish/layout.py）逐项相同。

两边用同样的输入：每种样式 × 每个选项 × 横、竖、方、条幅几种大小的照片 × 几份拍摄信息。字的宽度由 Pillow 量好，
记在表里交给 node（node 里没有字体）：JS 版问到表里没有的字就失败，说明两边的算式不一样了。
矩形、线、标志的位置和字号逐个数字相同（浮点误差以内）；画出来的字符串逐字相同。
真浏览器里用画布量字时和 Pillow 的差，在 tests/test_viewer_browser.py::test_browser_layout_matches_the_export_layout 里查。
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
from gxr_studio.finish.fonts import PilFonts
from gxr_studio.finish.layout import frame_layout, stamp_layout
from gxr_studio.finish.spec import FrameSpec, StampSpec
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


class Recorder:
    """Pillow 量字，并把每一次问答记下来。"""

    def __init__(self) -> None:
        self.fonts = PilFonts()
        self.table: dict[str, Any] = {}

    @staticmethod
    def key(kind: str, font: str, px: int, cjk: bool, text: str) -> str:
        return json.dumps([kind, font, px, cjk, text], ensure_ascii=False, separators=(",", ":"))

    def advance(self, font: str, px: int, text: str, cjk: bool) -> float:
        value = self.fonts.advance(font, px, text, cjk)
        self.table[self.key("a", font, px, cjk, text)] = value
        return value

    def bbox(self, font: str, px: int, text: str, cjk: bool) -> tuple[int, int, int, int]:
        value = self.fonts.bbox(font, px, text, cjk)
        self.table[self.key("b", font, px, cjk, text)] = list(value)
        return value


def frame_specs(style: str, wide: bool) -> list[FrameSpec]:
    if style == "canvas":
        return [
            FrameSpec(style=style, color=c, canvas_ratio=r)
            for c, r in itertools.product(("white", "black", "cream"), ("1:1", "4:5", "3:2", "9:16"))
        ]
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
import { readFileSync } from 'node:fs';
const data = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const key = (kind, font, px, cjk, text) => JSON.stringify([kind, font, px, cjk, text]);
const ask = (kind, font, px, text, cjk) => {
  const k = key(kind, font, px, cjk, text);
  if (!(k in data.table)) throw new Error('Python 没有问过这个：' + k);
  return data.table[k];
};
const m = { advance: (f, p, t, c) => ask('a', f, p, t, c), bbox: (f, p, t, c) => ask('b', f, p, t, c) };
const frames = data.frames.map((c) => L.frameLayout(c.w, c.h, c.spec, c.meta, m));
const stamps = data.stamps.map((c) => {
  const text = L.stampText(c.spec, c.when);
  return { text, layout: L.stampLayout(c.w, c.h, c.spec, text, m) };
});
console.log(JSON.stringify({ frames, stamps }));
"""


def same(a: Any, b: Any, path: str = "") -> None:
    """数字在 1e-9 以内相同，其余逐项相等。"""
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


def test_preview_layout_equals_export_layout(tmp_path: Path) -> None:
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
            for style in ("gallery", "leica", "hasselblad", "film", "instant", "canvas"):
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
        (tmp_path / "finish_layout.js").write_text((WEB / "finish_layout.js").read_text(encoding="utf-8"), encoding="utf-8")
        (tmp_path / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
        (tmp_path / "check.mjs").write_text(JS, encoding="utf-8")
        data = tmp_path / "cases.json"
        data.write_text(json.dumps({"table": rec.table, "frames": frame_cases, "stamps": stamp_cases}, ensure_ascii=False), encoding="utf-8")
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
