"""五种边框加在照片外面。尺寸、位置和字号都在 `layout.py`，这里只按版面画。"""

from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw

from . import frame_paint as paint
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
    canvas = paint.background(layout.width, layout.height, layout.bg, layout.bgfx)
    base = canvas.copy()
    for item in layout.items:
        kind = item["k"]
        if kind == "photo":
            canvas.paste(image, (item["x"], item["y"]))
        elif kind == "text":
            if item.get("rot"):
                face = fonts.face(item["font"], item["px"], False)
                tmp = Image.new("RGBA", (item["tw"], face.getmetrics()[0] + math.ceil(item["px"] * 0.4)))
                run = {**item, "xs": item["xs0"], "y": 0}
                draw_run(ImageDraw.Draw(tmp), run, fonts, tuple(item["fill"]))
                tmp = tmp.rotate(90, expand=True)
                canvas.paste(tmp.convert("RGB"), (item["gx"], item["gy"]), tmp.getchannel("A"))
            else:
                draw_run(ImageDraw.Draw(canvas), item, fonts, tuple(item["fill"]))
        elif kind == "line":
            ImageDraw.Draw(canvas).line([(item["x1"], item["y1"]), (item["x2"], item["y2"])], fill=tuple(item["fill"]), width=item["w"])
        elif kind == "rect":
            paint.paint_rect(canvas, item)
        elif kind == "shadow":
            paint.paint_shadow(canvas, item)
        elif kind == "hole":
            paint.paint_hole(canvas, item, base)
        elif kind == "poly":
            paint.paint_poly(canvas, item)
        elif kind == "logo":
            with Image.open(ASSETS / "logo" / "ricoh.png") as source:
                logo = source.convert("RGBA").resize((item["w"], item["h"]), Image.Resampling.LANCZOS)
            canvas.paste(logo, (item["x"], item["y"]), logo)
    return canvas
