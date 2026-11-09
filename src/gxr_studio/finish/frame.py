"""五种边框加在照片外面。尺寸、位置和字号都在 `layout.py`，这里只按版面画。"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from .fonts import ASSETS, FontMetrics, PilFonts, draw_run
from .layout import FrameLayout, frame_layout
from .spec import FrameSpec


def apply_frame(rgb: np.ndarray, spec: FrameSpec, meta: dict[str, str]) -> np.ndarray:
    if spec.style == "none":
        return rgb
    fonts = PilFonts()
    height, width = rgb.shape[:2]
    layout = frame_layout(width, height, spec, meta, FontMetrics(fonts))
    if layout is None:
        return rgb
    return np.asarray(draw_frame(Image.fromarray(rgb, "RGB"), layout, fonts))


def draw_frame(image: Image.Image, layout: FrameLayout, fonts: PilFonts) -> Image.Image:
    canvas = Image.new("RGB", (layout.width, layout.height), layout.bg)
    canvas.paste(image, layout.photo)
    draw = ImageDraw.Draw(canvas)
    for item in layout.items:
        kind = item["k"]
        if kind == "text":
            draw_run(draw, item, fonts, tuple(item["fill"]))
        elif kind == "line":
            draw.line([(item["x1"], item["y1"]), (item["x2"], item["y2"])], fill=tuple(item["fill"]), width=item["w"])
        else:
            logo = Image.open(ASSETS / "logo" / "ricoh.png").convert("RGBA")
            logo = logo.resize((item["w"], item["h"]), Image.Resampling.LANCZOS)
            canvas.paste(logo, (item["x"], item["y"]), logo)
    return canvas
