"""边框和时间戳的版面：只有数字，不碰像素。

导出（`frame.py`、`stamp.py`）照这里算出的版面画；浏览器预览（`web/assets/finish_layout.js`）用同一套算式，
`tests/test_finish_layout_parity.py` 逐项对照。改这里的算式，两边要一起改。
字的宽度、每个字的位置、墨迹框都由字体文件里的整数（步进宽度、字形外框，见 `sfnt.py`）和整数算式得出，
不问 Pillow 的排版引擎，也不问浏览器的量字：RAQM 和 BASIC 量出来的宽度不一样，所以版面不能靠它们。
浏览器拿到的是同一份表，`web/assets/finish_layout.js` 逐行对应这里的算式，两边的数一模一样。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Protocol, cast

from ..crop import PRESETS, target_ratio
from .sfnt import Glyph
from .spec import OUTPUT_RATIOS, FrameSpec, StampSpec

Rgb = tuple[int, int, int]

STAMP_COLORS: dict[str, Rgb] = {"orange": (255, 106, 0), "red": (220, 32, 32), "yellow": (255, 196, 40), "white": (255, 255, 255)}
STAMP_SIZE = {"small": 0.038, "medium": 0.055, "large": 0.078}
STAMP_FONT = {"seg7": "dseg", "typewriter": "courier", "sans": "atkinson"}
LOGO_SIZE = (2400, 433)  # assets/logo/ricoh.png；tests/test_finish_layout.py 核对
GLOW_GAIN = 1.15  # 发光层的透明度乘数（stamp.py 画发光时用）

Item = dict[str, Any]


class Metrics(Protocol):
    """字体表。`font` 是字体代号（atkinson / inter / inter_sb / courier / patrick / dseg），`cjk` 为真时用系统中文字体。
    单位是字体单位（em 的 1/upm）；中文字体读不到表，按 1000 像素的字号量出来，当作 upm = 1000。"""

    def upm(self, font: str, cjk: bool) -> int: ...

    def glyph(self, font: str, ch: str, cjk: bool) -> Glyph: ...

    def ascent(self, font: str, px: int, cjk: bool) -> int: ...


def needs_cjk(text: str) -> bool:
    return any(ord(ch) > 127 for ch in text)


def ascent_px(ascender: int, upm: int, px: int) -> int:
    """Pillow 的 'la' 锚点：字体上沿（hhea ascender）按 FreeType 的办法取整：先四舍五入到 1/64 像素，再向上取整到像素。"""
    return -(-((2 * ascender * px * 64 + upm) // (2 * upm)) // 64)


def _pens(m: Metrics, font: str, text: str, cjk: bool) -> list[int]:
    """每个字符前面的步进之和（字体单位），最后一项是整行的步进。"""
    pens = [0]
    for ch in text:
        pens.append(pens[-1] + m.glyph(font, ch, cjk)[0])
    return pens


def advance(m: Metrics, font: str, px: int, text: str, cjk: bool = False) -> float:
    """整行的宽度（像素，小数）：步进之和 × 字号 / upm。没有字距，没有取整。"""
    return _pens(m, font, text, cjk)[-1] * px / m.upm(font, cjk)


def glyph_xs(m: Metrics, font: str, px: int, text: str, x: float, cjk: bool = False) -> list[int]:
    """每个字画在哪一列像素：起点 x 加上前面各字的步进，四舍五入到整像素。导出和预览都一个字一个字地画在这些位置上。"""
    upm = m.upm(font, cjk)
    return [math.floor(x + pen * px / upm + 0.5) for pen in _pens(m, font, text, cjk)[:-1]]


def ink_box(m: Metrics, font: str, px: int, text: str, cjk: bool = False) -> tuple[int, int, int, int]:
    """一行字的墨迹框（左、上、右、下，整像素，起点是 'la' 锚点：x = 第一个字的原点，y = 字体上沿）。
    横向和 Pillow 的 textbbox 一样把整行的步进算进去（左边不会大于 0，右边不会小于步进）。"""
    upm = m.upm(font, cjk)
    asc = m.ascent(font, px, cjk)
    pens = _pens(m, font, text, cjk)
    left = 0.0
    right = pens[-1] * px / upm
    up = down = 0.0
    for ch, pen in zip(text, pens, strict=False):
        box = m.glyph(font, ch, cjk)[1]
        if box is None:
            continue
        left = min(left, (pen + box[0]) * px / upm)
        right = max(right, (pen + box[2]) * px / upm)
        up = max(up, box[3] * px / upm)
        down = max(down, -box[1] * px / upm)
    return math.floor(left), math.floor(asc - up), math.ceil(right), math.ceil(asc + down)


@dataclass(frozen=True)
class FrameLayout:
    """成品画布：宽高、照片位置、底色，以及按顺序绘制的底层、照片和装饰。"""

    width: int
    height: int
    photo: tuple[int, int]
    bg: Rgb
    items: tuple[Item, ...]
    bgfx: Item | None = None
    unit_box: tuple[int, int, int, int] = (0, 0, 0, 0)
    fixed: bool = False
    grain: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            json.loads(
                json.dumps(
                    {
                        "width": self.width,
                        "height": self.height,
                        "photo": self.photo,
                        "bg": self.bg,
                        "items": self.items,
                        "bgfx": self.bgfx,
                        "unit_box": self.unit_box,
                        "fixed": self.fixed,
                        "grain": self.grain,
                    }
                )
            ),
        )


@dataclass(frozen=True)
class StampLayout:
    """日期戳：文字框（照片坐标）、字号、颜色、发光半径，以及要画的文字块和撇号。"""

    px: int
    font: str
    rect: tuple[int, int, int, int]
    ink: Rgb
    glow: float | None
    ops: tuple[Item, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "px": self.px,
            "font": self.font,
            "rect": list(self.rect),
            "ink": list(self.ink),
            "glow": self.glow,
            "ops": [dict(op) for op in self.ops],
        }


def text_item(m: Metrics, x: float, y: float, px: int, font: str, text: str, fill: Rgb, cjk: bool = False) -> Item:
    """一行字：x 是起点，xs 是每个字的整像素列（`glyph_xs`），y 是整像素行（字体上沿所在的那一行）。"""
    return {
        "k": "text",
        "x": x,
        "y": math.floor(y + 0.5),
        "px": px,
        "font": font,
        "text": text,
        "fill": list(fill),
        "cjk": cjk,
        "xs": glyph_xs(m, font, px, text, x, cjk),
    }


def crop_ratio(preset: str | None, w: int, h: int) -> tuple[int, int] | None:
    """无边框时显示的固定裁剪比例；照片横竖决定方向，原始和自由不标比例。"""
    if preset is None or PRESETS.get(preset) is None:
        return None
    ratio = target_ratio(preset, w, h)
    return (ratio.numerator, ratio.denominator) if ratio is not None else None


def ratio_size(w: int, h: int, rw: int, rh: int) -> tuple[int, int]:
    """容得下原版面的最小整数倍画布。3:4 的文件宽高严格满足 4w = 3h。"""
    multiple = max(-(-w // rw), -(-h // rh))
    return multiple * rw, multiple * rh


def fit_size(w: int, h: int, max_edge: int | None) -> tuple[int, int]:
    """导出尺寸档：加完边框以后缩小，不放大，舍入与实际像素缩放共用。"""
    if not max_edge or max(w, h) <= max_edge:
        return w, h
    scale = max_edge / max(w, h)
    return max(1, round(w * scale)), max(1, round(h * scale))


def frame_layout(w: int, h: int, spec: FrameSpec, meta: dict[str, str], m: Metrics) -> FrameLayout | None:
    """Photo pixels are never scaled or cropped. Port of approved DESIGN sections 3–6."""
    from . import frame_new as new
    from . import frame_objects as objects
    from . import frame_styles as plain

    ratio = OUTPUT_RATIOS.get(spec.output_ratio)
    if spec.style == "none":
        return None
    if spec.style == "gallery":
        raw = plain.gallery(w, h, meta, ratio, m, color=spec.color, weight=spec.weight)
    elif spec.style == "leica":
        raw = plain.leica(
            w,
            h,
            meta,
            ratio,
            m,
            color=spec.color,
            show_logo=spec.show_logo,
            show_params=spec.show_params,
            show_date=bool(spec.show_date),
            text=spec.text,
        )
    elif spec.style == "hasselblad":
        raw = plain.hasselblad(
            w, h, meta, ratio, m, color=spec.color, show_params=spec.show_params, show_date=bool(spec.show_date), text=spec.text
        )
    elif spec.style == "film":
        raw = objects._strip(w, h, meta, ratio, spec.fmt, spec.table, m)
    elif spec.style == "instant":
        raw = objects.instant(w, h, meta, ratio, m, text=spec.text, show_date=bool(spec.show_date), fmt=spec.fmt)
    elif spec.style == "thin_keyline":
        raw = new.thin_keyline(w, h, meta, ratio, m, color=spec.color)
    elif spec.style == "exhibition_label":
        raw = new.exhibition_label(w, h, meta, ratio, m, color=spec.color, text=spec.text)
    elif spec.style == "blur_fill":
        raw = new.blur_fill(w, h, meta, ratio, m, show_params=spec.show_params)
    elif spec.style == "cinema":
        raw = new.cinema(w, h, meta, ratio, m, text=spec.text)
    elif spec.style == "slide_mount":
        raw = new.slide_mount(w, h, meta, ratio, m, color=spec.color)
    elif spec.style == "darkroom":
        raw = new.darkroom(w, h, meta, ratio, m, text=spec.text)
    else:
        raw = new.postage_stamp(w, h, meta, ratio, m)
    return FrameLayout(
        raw["width"], raw["height"], raw["photo"], raw["bg"], tuple(raw["items"]), raw["bgfx"], raw["unit_box"], raw["fixed"], raw["grain"]
    )


# ---- 时间戳 -------------------------------------------------------------------


def seg7_text(text: str) -> str:
    """DSEG 的 `!` 是和数字等宽的空白。普通空格比数字窄。"""
    return text.replace(" ", "!")


def tick_item(x: int, y: int, px: int, fill: Rgb) -> Item:
    """斜撇。数码管没有这个字，缺字会画成空心方块。"""
    w = max(2, int(px * 0.07))
    h = max(6, int(px * 0.42))
    x0 = x + w
    y0 = y + max(0, int(px * 0.04))
    return {"k": "poly", "pts": [[x0 + 2 * w, y0], [x0 + 3 * w, y0], [x0 + w, y0 + h], [x0, y0 + h]], "fill": list(fill)}


def _stamp_ops(origin: tuple[int, int], text: str, px: int, font: str, cjk: bool, ink: Rgb, m: Metrics) -> list[Item]:
    x, y = origin
    if font != "dseg" or cjk:
        return [text_item(m, x, y, px, font, text, ink, cjk)]
    drawn = seg7_text(text)
    ops: list[Item] = []
    start = 0
    while start < len(drawn):
        tick = drawn.find("'", start)
        if tick < 0:
            ops.append(text_item(m, x, y, px, font, drawn[start:], ink))
            break
        chunk = drawn[start:tick]
        if chunk:
            ops.append(text_item(m, x, y, px, font, chunk, ink))
            x += int(round(advance(m, font, px, chunk)))
        ops.append(tick_item(x, y, px, ink))
        x += int(round(advance(m, font, px, "'")))
        start = tick + 1
    return ops


def stamp_layout(w: int, h: int, spec: StampSpec, text: str, m: Metrics, frame: FrameLayout | None = None) -> StampLayout | None:
    """text 是 `stamp.format_stamp` 的结果（空串表示没有可画的内容）。w、h 是照片像素宽高。"""
    if not text:
        return None
    if frame is not None:
        return _border_stamp_layout(w, h, spec, text, m, frame)
    font = STAMP_FONT[spec.font]
    cjk = needs_cjk(text)
    shown = text if cjk or spec.font != "seg7" else seg7_text(text)
    px = max(12, round(min(w, h) * STAMP_SIZE[spec.size]))
    box = ink_box(m, font, max(1, px), shown, cjk)
    margin = max(1, round(min(w, h) * 0.035))
    limit_w, limit_h = max(1, w - 2 * margin), max(1, h - 2 * margin)
    while (box[2] - box[0] > limit_w or box[3] - box[1] > limit_h) and px > 1:
        nxt = int(px * 0.85)
        px = nxt if nxt < px else px - 1
        box = ink_box(m, font, max(1, px), shown, cjk)
    tw, th = box[2] - box[0], box[3] - box[1]
    x = margin if spec.corner[1] == "l" else max(margin, w - margin - tw)
    y = margin if spec.corner[0] == "t" else max(margin, h - margin - th)
    ink = STAMP_COLORS[spec.color]
    face_px = max(1, px)
    ops = _stamp_ops((int(x - box[0]), int(y - box[1])), text, face_px, font, cjk, ink, m)
    glow = max(1.2, px * 0.16) if spec.glow and spec.font == "seg7" else None
    return StampLayout(face_px, font, (x, y, tw, th), ink, glow, tuple(ops))


def _border_stamp_layout(w: int, h: int, spec: StampSpec, text: str, m: Metrics, frame: FrameLayout) -> StampLayout | None:
    """拍立得底边：从照片和题字下方开始，发光也不能越过照片或画布边界。坐标属于整张成片。"""
    base = stamp_layout(w, h, spec, text, m)
    if base is None:
        return None
    top = frame.photo[1] + h
    for item in frame.items:
        if item["k"] == "text":
            box = ink_box(m, item["font"], item["px"], item["text"], item["cjk"])
            top = max(top, item["y"] + box[3] + 1)
    bottom = frame.unit_box[1] + frame.unit_box[3]
    margin = max(2, min(round(min(w, h) * 0.035), (bottom - top) // 8))
    cjk = needs_cjk(text)
    shown = seg7_text(text) if spec.font == "seg7" and not cjk else text
    px = base.px
    while True:
        box = ink_box(m, base.font, px, shown, cjk)
        glow = max(1.2, px * 0.16) if base.glow is not None else None
        pad = max(margin, math.ceil(3 * glow) + 2) if glow is not None else margin
        tw, th = box[2] - box[0], box[3] - box[1]
        if tw <= w - 2 * pad and th <= bottom - top - 2 * pad:
            break
        if px == 1:
            return None
        px = max(1, int(px * 0.85))
    x = frame.photo[0] + (pad if spec.corner[1] == "l" else w - pad - tw)
    y = top + pad if spec.corner[0] == "t" else bottom - pad - th
    ops = _stamp_ops((x - box[0], y - box[1]), text, px, base.font, cjk, base.ink, m)
    return StampLayout(px, base.font, (x, y, tw, th), base.ink, glow, tuple(ops))
