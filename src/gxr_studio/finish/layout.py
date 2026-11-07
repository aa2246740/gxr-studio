"""边框和时间戳的版面：只有数字，不碰像素。

导出（`frame.py`、`stamp.py`）照这里算出的版面画；浏览器预览（`web/assets/finish_layout.js`）用同一套算式，
`tests/test_finish_layout_parity.py` 逐项对照。改这里的算式，两边要一起改。
量字要用的字体尺寸由 `Metrics` 给：导出用 Pillow（`fonts.PilMetrics`），预览用画布的 `measureText`。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .spec import FrameSpec, StampSpec

Rgb = tuple[int, int, int]

WEIGHT = {"thin": 0.02, "medium": 0.04, "thick": 0.07}
CANVAS = {"1:1": (1, 1), "4:5": (4, 5), "3:2": (3, 2), "9:16": (9, 16)}
COLORS: dict[str, Rgb] = {"white": (255, 255, 255), "black": (12, 12, 12), "cream": (245, 236, 220)}
STAMP_COLORS: dict[str, Rgb] = {"orange": (255, 106, 0), "red": (220, 32, 32), "yellow": (255, 196, 40), "white": (255, 255, 255)}
STAMP_SIZE = {"small": 0.038, "medium": 0.055, "large": 0.078}
STAMP_FONT = {"seg7": "dseg", "typewriter": "courier", "sans": "atkinson"}
LOGO_SIZE = (2400, 433)  # assets/logo/ricoh.png；tests/test_finish_layout.py 核对
GLOW_GAIN = 1.15  # 发光层的透明度乘数（stamp.py 画发光时用）

Item = dict[str, Any]


class Metrics(Protocol):
    """量字。`font` 是字体代号（atkinson / inter / courier / patrick / dseg），`cjk` 为真时用系统中文字体。"""

    def advance(self, font: str, px: int, text: str, cjk: bool) -> float: ...

    def bbox(self, font: str, px: int, text: str, cjk: bool) -> tuple[int, int, int, int]: ...


def needs_cjk(text: str) -> bool:
    return any(ord(ch) > 127 for ch in text)


@dataclass(frozen=True)
class FrameLayout:
    """边框外的整张画布：宽高、照片的左上角、底色，以及画在照片外面的文字、线和标志。"""

    width: int
    height: int
    photo: tuple[int, int]
    bg: Rgb
    items: tuple[Item, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "photo": list(self.photo),
            "bg": list(self.bg),
            "items": [dict(item) for item in self.items],
        }


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


def text_item(x: float, y: float, px: int, font: str, text: str, fill: Rgb, cjk: bool = False) -> Item:
    return {"k": "text", "x": x, "y": y, "px": px, "font": font, "text": text, "fill": list(fill), "cjk": cjk}


def _face_px(px: int) -> int:
    """边框用的字体不会小于 8 像素。"""
    return max(8, px)


def _fit(m: Metrics, text: str, font: str, px: int, max_w: int) -> int:
    while px > 10 and m.advance(font, _face_px(px), text, False) > max_w:
        px -= 1
    return _face_px(px)


def _margin(w: int, h: int, weight: str) -> int:
    return max(8, round(max(w, h) * WEIGHT[weight]))


def _gallery(w: int, h: int, spec: FrameSpec) -> FrameLayout:
    m = _margin(w, h, spec.weight)
    return FrameLayout(w + 2 * m, h + 2 * m, (m, m), COLORS[spec.color], ())


def _leica_lines(spec: FrameSpec, meta: dict[str, str]) -> list[str]:
    lines = []
    if spec.show_params and meta.get("lens"):
        lines.append(meta["lens"])
    second = "  ".join(bit for bit in (meta.get("aperture"), meta.get("shutter"), meta.get("iso")) if bit)
    if spec.show_params and second:
        lines.append(second)
    if spec.show_date and meta.get("date"):
        lines.append(meta["date"])
    if spec.text:
        lines.append(spec.text)
    return lines


def _leica(w: int, h: int, spec: FrameSpec, meta: dict[str, str], m: Metrics) -> FrameLayout:
    bar = max(64, round(min(w, h) * 0.09))
    dark = spec.color == "black"
    ink: Rgb = (245, 245, 245) if dark else (20, 20, 20)
    muted: Rgb = (170, 170, 170) if dark else (90, 90, 90)
    pad = max(16, bar // 5)
    items: list[Item] = []
    if spec.show_logo:
        lh = int(bar * 0.42)
        lw = max(1, round(LOGO_SIZE[0] * lh / LOGO_SIZE[1]))
        items.append({"k": "logo", "x": pad, "y": h + (bar - lh) // 2, "w": lw, "h": lh})
        gx = pad + lw + pad // 2
        items.append(text_item(gx, h + (bar - int(bar * 0.34)) // 2 - 2, _face_px(int(bar * 0.34)), "atkinson", "GXR", ink))
    lines = _leica_lines(spec, meta)
    if lines:
        first, second = lines[0], "  ".join(lines[1:])
        px1 = _fit(m, first, "inter", int(bar * 0.26), w // 2)
        px2 = _fit(m, second, "inter", int(bar * 0.22), w // 2) if second else px1
        w1 = m.advance("inter", px1, first, False)
        w2 = m.advance("inter", px2, second, False) if second else 0
        right = w - pad
        rule = right - max(w1, w2) - max(8, bar // 8)
        items.append({"k": "line", "x1": rule, "y1": h + pad, "x2": rule, "y2": h + bar - pad, "w": max(1, bar // 28), "fill": list(muted)})
        items.append(text_item(right - w1, h + int(bar * 0.16), px1, "inter", first, ink))
        if second:
            items.append(text_item(right - w2, h + int(bar * 0.52), px2, "inter", second, muted))
    return FrameLayout(w, h + bar, (0, 0), COLORS[spec.color], tuple(items))


def _hasselblad(w: int, h: int, spec: FrameSpec, meta: dict[str, str], m: Metrics) -> FrameLayout:
    thin = max(10, round(max(w, h) * 0.018))
    bottom = max(thin * 4, round(min(w, h) * 0.16))
    out_w, out_h = w + 2 * thin, h + thin + bottom
    dark = spec.color == "black"
    ink: Rgb = (245, 245, 245) if dark else (24, 24, 24)
    muted: Rgb = (180, 180, 180) if dark else (120, 120, 120)
    title_px, sub_px = _face_px(max(16, bottom // 5)), _face_px(max(13, bottom // 7))
    cx = out_w // 2
    y0 = thin + h + int(bottom * 0.28)
    name = "RICOH GXR"
    items = [text_item(cx - m.advance("atkinson", title_px, name, False) / 2, y0, title_px, "atkinson", name, ink)]
    bits: list[str] = []
    if spec.show_params:
        bits.extend(bit for bit in (meta.get("lens"), meta.get("aperture"), meta.get("shutter"), meta.get("iso")) if bit)
    if spec.show_date and meta.get("date"):
        bits.append(meta["date"])
    if spec.text:
        bits.append(spec.text)
    line = "  ·  ".join(bits)
    if line:
        items.append(text_item(cx - m.advance("inter", sub_px, line, False) / 2, y0 + int(bottom * 0.28), sub_px, "inter", line, muted))
    return FrameLayout(out_w, out_h, (thin, thin), COLORS[spec.color], tuple(items))


def _film(w: int, h: int, meta: dict[str, str], m: Metrics) -> FrameLayout:
    margin = max(28, round(max(w, h) * 0.045))
    out_w, out_h = w + 2 * margin, h + 2 * margin
    amber: Rgb = (232, 164, 74)
    px = _face_px(max(11, margin // 3))
    lens = meta.get("lens_short") or "A12 50mm"
    items = [text_item(margin, out_h - margin + 6, px, "courier", f"RICOH GXR  ·  {lens}", amber)]
    number = meta.get("frame_no") or ""
    if number:
        items.append(text_item(out_w - margin - m.advance("courier", px, number, False), 6, px, "courier", number, amber))
    return FrameLayout(out_w, out_h, (margin, margin), (8, 8, 8), tuple(items))


def _instant(w: int, h: int, spec: FrameSpec, m: Metrics) -> FrameLayout:
    side = max(18, round(max(w, h) * 0.035))
    bottom = max(side * 4, round(min(w, h) * 0.20))
    out_w, out_h = w + 2 * side, h + side + bottom
    items: list[Item] = []
    if spec.text:
        cjk = needs_cjk(spec.text)

        def eff(size: int) -> int:
            return max(1, size) if cjk else _face_px(size)

        px = max(22, bottom // 4)
        max_w = max(8, out_w - 2 * side)
        while px > 10 and m.advance("patrick", eff(px), spec.text, cjk) > max_w:
            px = max(10, int(px * 0.85))
        tw = m.advance("patrick", eff(px), spec.text, cjk)
        items.append(text_item((out_w - tw) / 2, side + h + (bottom - eff(px)) / 2, eff(px), "patrick", spec.text, (40, 40, 40), cjk))
    return FrameLayout(out_w, out_h, (side, side), (248, 246, 242), tuple(items))


def _canvas(w: int, h: int, spec: FrameSpec) -> FrameLayout:
    """四边留出和「白边 · 中」一样宽的边，再补到所选的画布比例，照片居中。"""
    margin = _margin(w, h, "medium")
    inner_w, inner_h = w + 2 * margin, h + 2 * margin
    rw, rh = CANVAS[spec.canvas_ratio]
    target = rw / rh
    if inner_w / inner_h > target:
        out_w, out_h = inner_w, round(inner_w / target)
    else:
        out_h, out_w = inner_h, round(inner_h * target)
    return FrameLayout(out_w, out_h, ((out_w - w) // 2, (out_h - h) // 2), COLORS[spec.color], ())


def frame_layout(w: int, h: int, spec: FrameSpec, meta: dict[str, str], m: Metrics) -> FrameLayout | None:
    """w、h 是要装进边框的照片（已裁切、已盖日期戳）的像素宽高。样式 none 返回 None。"""
    builders: dict[str, Callable[[], FrameLayout]] = {
        "gallery": lambda: _gallery(w, h, spec),
        "leica": lambda: _leica(w, h, spec, meta, m),
        "hasselblad": lambda: _hasselblad(w, h, spec, meta, m),
        "film": lambda: _film(w, h, meta, m),
        "instant": lambda: _instant(w, h, spec, m),
        "canvas": lambda: _canvas(w, h, spec),
    }
    build = builders.get(spec.style)
    return build() if build else None


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
        return [text_item(x, y, px, font, text, ink, cjk)]
    drawn = seg7_text(text)
    ops: list[Item] = []
    start = 0
    while start < len(drawn):
        tick = drawn.find("'", start)
        if tick < 0:
            ops.append(text_item(x, y, px, font, drawn[start:], ink))
            break
        chunk = drawn[start:tick]
        if chunk:
            ops.append(text_item(x, y, px, font, chunk, ink))
            x += int(round(m.advance(font, px, chunk, False)))
        ops.append(tick_item(x, y, px, ink))
        x += int(round(m.advance(font, px, "'", False)))
        start = tick + 1
    return ops


def stamp_layout(w: int, h: int, spec: StampSpec, text: str, m: Metrics) -> StampLayout | None:
    """text 是 `stamp.format_stamp` 的结果（空串表示没有可画的内容）。w、h 是照片像素宽高。"""
    if not text:
        return None
    font = STAMP_FONT[spec.font]
    cjk = needs_cjk(text)
    shown = text if cjk or spec.font != "seg7" else seg7_text(text)
    px = max(12, round(min(w, h) * STAMP_SIZE[spec.size]))
    box = m.bbox(font, max(1, px), shown, cjk)
    margin = max(1, round(min(w, h) * 0.035))
    limit_w, limit_h = max(1, w - 2 * margin), max(1, h - 2 * margin)
    while (box[2] - box[0] > limit_w or box[3] - box[1] > limit_h) and px > 1:
        nxt = int(px * 0.85)
        px = nxt if nxt < px else px - 1
        box = m.bbox(font, max(1, px), shown, cjk)
    tw, th = box[2] - box[0], box[3] - box[1]
    x = margin if spec.corner[1] == "l" else max(margin, w - margin - tw)
    y = margin if spec.corner[0] == "t" else max(margin, h - margin - th)
    ink = STAMP_COLORS[spec.color]
    face_px = max(1, px)
    ops = _stamp_ops((int(x - box[0]), int(y - box[1])), text, face_px, font, cjk, ink, m)
    glow = max(1.2, px * 0.16) if spec.glow and spec.font == "seg7" else None
    return StampLayout(face_px, font, (x, y, tw, th), ink, glow, tuple(ops))
