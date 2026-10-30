"""时间戳画在照片里面。数码管组间用字体自带的等宽空白，撇号单独画成斜撇。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .spec import STAMP_TEXT_MAX, StampSpec

ASSETS = Path(__file__).resolve().parents[1] / "assets" / "fonts"
FONT_FILE = {
    "seg7": "DSEG7Classic-Regular.ttf",
    "typewriter": "CourierPrime-Regular.ttf",
    "sans": "AtkinsonHyperlegible-Regular.ttf",
}
COLORS = {"orange": (255, 106, 0), "red": (220, 32, 32), "yellow": (255, 196, 40), "white": (255, 255, 255)}
SIZE = {"small": 0.038, "medium": 0.055, "large": 0.078}


def format_stamp(spec: StampSpec, taken: datetime | None, file_time: datetime | None) -> str:
    moment: datetime | None
    if spec.date_override:
        year, month, day = spec.date_override.split("-")
        moment = datetime(int(year), int(month), int(day))
    else:
        moment = taken or file_time
    parts: list[str] = []
    if spec.content in {"date", "date+text"} and moment is not None:
        yy, mm, dd = f"{moment.year % 100:02d}", f"{moment.month:02d}", f"{moment.day:02d}"
        if spec.date_format == "yy mm dd":
            text = f"{yy} {mm} {dd}"
        elif spec.date_format == "yyyy.mm.dd":
            text = f"{moment.year:04d}.{mm}.{dd}"
        elif spec.date_format == "mm dd 'yy":
            text = f"{mm} {dd} '{yy}"
        else:
            text = f"'{yy} {mm} {dd}"
        if spec.show_time:
            text = f"{text} {moment.hour:02d}:{moment.minute:02d}"
        parts.append(text)
    if spec.content in {"text", "date+text"} and spec.text:
        parts.append(spec.text[:STAMP_TEXT_MAX])
    return " ".join(parts)


_CJK_FONTS = (
    Path("/System/Library/Fonts/PingFang.ttc"),
    Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
    Path("/System/Library/Fonts/STHeiti Medium.ttc"),
    Path("/System/Library/Fonts/STHeiti Light.ttc"),
)


def cjk_font(px: int) -> ImageFont.FreeTypeFont | None:
    for path in _CJK_FONTS:
        if path.is_file():
            return ImageFont.truetype(str(path), max(1, px))
    return None


def text_needs_cjk(text: str) -> bool:
    return any(ord(ch) > 127 for ch in text)


def _font(kind: str, px: int, text: str = "") -> ImageFont.FreeTypeFont:
    face = ImageFont.truetype(str(ASSETS / FONT_FILE[kind]), max(1, px))
    if text_needs_cjk(text):
        fallback = cjk_font(px)
        if fallback is None:
            from ..render.params import EditError

            raise EditError("missing_cjk", "无法显示中文。请改用英文或数字。")
        return fallback
    return face


def _seg7_text(text: str) -> str:
    """DSEG 的 `!` 是和数字等宽的空白。普通空格比数字窄。"""
    return text.replace(" ", "!")


def _draw_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    face: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int] | int,
    kind: str,
) -> None:
    if kind != "seg7" or text_needs_cjk(text):
        draw.text(xy, text, font=face, fill=fill)
        return
    drawn = _seg7_text(text)
    x, y = xy
    px = int(face.size)
    start = 0
    while start < len(drawn):
        tick = drawn.find("'", start)
        if tick < 0:
            chunk = drawn[start:]
            if chunk:
                draw.text((x, y), chunk, font=face, fill=fill)
            return
        chunk = drawn[start:tick]
        if chunk:
            draw.text((x, y), chunk, font=face, fill=fill)
            x += int(round(draw.textlength(chunk, font=face)))
        _apostrophe(draw, x, y, px, fill)
        x += int(round(draw.textlength("'", font=face)))
        start = tick + 1


def _apostrophe(draw: ImageDraw.ImageDraw, x: int, y: int, px: int, fill: tuple[int, int, int] | int) -> None:
    """斜撇。数码管没有这个字，缺字会画成空心方块。"""
    w = max(2, int(px * 0.07))
    h = max(6, int(px * 0.42))
    x0 = x + w
    y0 = y + max(0, int(px * 0.04))
    draw.polygon(
        [(x0 + 2 * w, y0), (x0 + 3 * w, y0), (x0 + w, y0 + h), (x0, y0 + h)],
        fill=fill,
    )


def apply_stamp(rgb: np.ndarray, spec: StampSpec, taken: datetime | None, file_time: datetime | None) -> np.ndarray:
    if not spec.enabled:
        return rgb
    text = format_stamp(spec, taken, file_time)
    if not text:
        return rgb
    image = Image.fromarray(rgb, "RGB")
    px = max(12, round(min(image.size) * SIZE[spec.size]))
    face = _font(spec.font, px, text)
    ink = COLORS[spec.color]
    probe = ImageDraw.Draw(image)
    shown = text if text_needs_cjk(text) or spec.font != "seg7" else _seg7_text(text)
    box = probe.textbbox((0, 0), shown, font=face)
    tw, th = box[2] - box[0], box[3] - box[1]
    margin = max(1, round(min(image.size) * 0.035))
    limit_w = max(1, image.width - 2 * margin)
    limit_h = max(1, image.height - 2 * margin)
    while (tw > limit_w or th > limit_h) and px > 1:
        nxt = int(px * 0.85)
        px = nxt if nxt < px else px - 1
        face = _font(spec.font, px, text)
        box = probe.textbbox((0, 0), shown, font=face)
        tw, th = box[2] - box[0], box[3] - box[1]
    origin_x, origin_y = -box[0], -box[1]
    x = margin if spec.corner[1] == "l" else max(margin, image.width - margin - tw)
    y = margin if spec.corner[0] == "t" else max(margin, image.height - margin - th)
    origin = (int(x + origin_x), int(y + origin_y))
    if spec.glow and spec.font == "seg7":
        alpha = Image.new("L", image.size, 0)
        _draw_text(ImageDraw.Draw(alpha), origin, text, face, 255, spec.font)
        blurred = alpha.filter(ImageFilter.GaussianBlur(radius=max(1.2, px * 0.16)))
        glow = Image.merge(
            "RGBA",
            (
                Image.new("L", image.size, ink[0]),
                Image.new("L", image.size, ink[1]),
                Image.new("L", image.size, ink[2]),
                blurred.point(lambda p: min(255, int(p * 1.15))),
            ),
        )
        image = Image.alpha_composite(image.convert("RGBA"), glow).convert("RGB")
    _draw_text(ImageDraw.Draw(image), origin, text, face, ink, spec.font)
    return np.asarray(image)
