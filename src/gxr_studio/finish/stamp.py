"""时间戳画在照片里面。数码管组间用字体自带的等宽空白，撇号单独画成斜撇。字号和位置在 `layout.py`。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .fonts import FontMetrics, PilFonts, draw_run, open_face
from .layout import GLOW_GAIN, StampLayout, stamp_layout
from .spec import STAMP_TEXT_MAX, StampSpec


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
            return open_face(str(path), px)  # 和内置字体一样明确用 BASIC
    return None


def _paint(draw: ImageDraw.ImageDraw, layout: StampLayout, fonts: PilFonts, fill: tuple[int, int, int] | int) -> None:
    for op in layout.ops:
        if op["k"] == "text":
            draw_run(draw, op, fonts, fill)
        else:
            draw.polygon([tuple(p) for p in op["pts"]], fill=fill)


def apply_stamp(rgb: np.ndarray, spec: StampSpec, taken: datetime | None, file_time: datetime | None) -> np.ndarray:
    if not spec.enabled:
        return rgb
    text = format_stamp(spec, taken, file_time)
    if not text:
        return rgb
    image = Image.fromarray(rgb, "RGB")
    fonts = PilFonts()
    layout = stamp_layout(image.width, image.height, spec, text, FontMetrics(fonts))
    if layout is None:
        return rgb
    ink = layout.ink
    if layout.glow is not None:
        alpha = Image.new("L", image.size, 0)
        _paint(ImageDraw.Draw(alpha), layout, fonts, 255)
        blurred = alpha.filter(ImageFilter.GaussianBlur(radius=layout.glow))
        glow = Image.merge(
            "RGBA",
            (
                Image.new("L", image.size, ink[0]),
                Image.new("L", image.size, ink[1]),
                Image.new("L", image.size, ink[2]),
                blurred.point(lambda p: min(255, int(p * GLOW_GAIN))),
            ),
        )
        image = Image.alpha_composite(image.convert("RGBA"), glow).convert("RGB")
    _paint(ImageDraw.Draw(image), layout, fonts, ink)
    return np.asarray(image)
