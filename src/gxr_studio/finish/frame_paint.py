"""Anti-aliased frame shapes, matching the approved Pillow renderer."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter


def _ss_for(area: int) -> int:
    return 4 if area < 300000 else 3 if area < 1500000 else 2


def aa_paint(
    canvas: Image.Image,
    bbox: tuple[int, int, int, int],
    draw_fn: Any,
    color: tuple[int, int, int],
    alpha: float = 1.0,
    src: Image.Image | None = None,
) -> None:
    """Paint anti-aliased shape(s) into canvas. draw_fn(draw, ss, ox, oy) draws 255-valued shapes."""
    x0, y0, x1, y1 = bbox
    bw, bh = (max(1, x1 - x0), max(1, y1 - y0))
    ss = _ss_for(bw * bh)
    big = Image.new("L", (bw * ss, bh * ss), 0)
    draw_fn(ImageDraw.Draw(big), ss, x0, y0)
    mask = big.resize((bw, bh), Image.Resampling.BOX)
    if alpha < 1.0:
        mask = mask.point(lambda v: int(v * alpha))
    if src is not None:
        canvas.paste(src.crop((x0, y0, x0 + bw, y0 + bh)), (x0, y0), mask)
    else:
        canvas.paste(Image.new("RGB", (bw, bh), tuple(color)), (x0, y0), mask)


def _rr(
    d: ImageDraw.ImageDraw, ss: int, ox: int, oy: int, x: float, y: float, w: float, h: float, r: float, value: int = 255, inset: float = 0.0
) -> None:
    box = [(x + inset - ox) * ss, (y + inset - oy) * ss, (x + w - inset - ox) * ss - 1, (y + h - inset - oy) * ss - 1]
    rr = max(0.0, r - inset) * ss
    if rr > 0:
        d.rounded_rectangle(box, radius=rr, fill=value)
    else:
        d.rectangle(box, fill=value)


def paint_rect(canvas: Image.Image, it: dict[str, Any]) -> None:
    x, y, w, h = (it["x"], it["y"], it["w"], it["h"])
    r = it.get("radius", 0)
    fill, outline = (it.get("fill"), it.get("outline"))
    sw = it.get("stroke_w", 1)
    alpha = it.get("alpha", 1.0)
    if r <= 0 and alpha >= 1.0:
        ImageDraw.Draw(canvas).rectangle(
            [x, y, x + w - 1, y + h - 1], fill=tuple(fill) if fill else None, outline=tuple(outline) if outline else None, width=sw
        )
        return
    bbox = (x - 1, y - 1, x + w + 1, y + h + 1)
    if fill:
        aa_paint(canvas, bbox, lambda d, ss, ox, oy: _rr(d, ss, ox, oy, x, y, w, h, r), tuple(fill), alpha)
    if outline:

        def ring(d: ImageDraw.ImageDraw, ss: int, ox: int, oy: int) -> None:
            _rr(d, ss, ox, oy, x, y, w, h, r, 255)
            _rr(d, ss, ox, oy, x, y, w, h, r, 0, inset=sw)

        aa_paint(canvas, bbox, ring, tuple(outline), alpha)


def paint_poly(canvas: Image.Image, it: dict[str, Any]) -> None:
    pts = it["pts"]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    bbox = (math.floor(min(xs)) - 2, math.floor(min(ys)) - 2, math.ceil(max(xs)) + 2, math.ceil(max(ys)) + 2)

    def fn(d: ImageDraw.ImageDraw, ss: int, ox: int, oy: int) -> None:
        d.polygon([((p[0] - ox) * ss, (p[1] - oy) * ss) for p in pts], fill=255)

    aa_paint(canvas, bbox, fn, tuple(it["fill"]), it.get("alpha", 1.0))


def paint_hole(canvas: Image.Image, it: dict[str, Any], base: Image.Image) -> None:
    x, y, w, h, r = (it["x"], it["y"], it["w"], it["h"], it.get("radius", 0))
    aa_paint(canvas, (x - 1, y - 1, x + w + 1, y + h + 1), lambda d, ss, ox, oy: _rr(d, ss, ox, oy, x, y, w, h, r), (0, 0, 0), 1.0, src=base)


def paint_shadow(canvas: Image.Image, it: dict[str, Any]) -> None:
    x, y, w, h = (it["x"], it["y"], it["w"], it["h"])
    blur, dy, dx = (it["blur"], it.get("dy", 0), it.get("dx", 0))
    r = it.get("radius", 0)
    pad = int(blur * 3) + 2
    x0, y0 = (max(0, x + dx - pad), max(0, y + dy - pad))
    x1, y1 = (min(canvas.width, x + dx + w + pad), min(canvas.height, y + dy + h + pad))
    if x1 <= x0 or y1 <= y0:
        return
    m = Image.new("L", (x1 - x0, y1 - y0), 0)
    d = ImageDraw.Draw(m)
    box = [x + dx - x0, y + dy - y0, x + dx + w - x0 - 1, y + dy + h - y0 - 1]
    if r > 0:
        d.rounded_rectangle(box, radius=r, fill=255)
    else:
        d.rectangle(box, fill=255)
    m = m.filter(ImageFilter.GaussianBlur(blur)).point(lambda v: int(v * it.get("alpha", 0.3)))
    canvas.paste(Image.new("RGB", m.size, tuple(it.get("color", (0, 0, 0)))), (x0, y0), m)


def background(width: int, height: int, bg: tuple[int, int, int], fx: dict[str, Any] | None) -> Image.Image:
    if not fx or fx["kind"] != "glow":
        return Image.new("RGB", (width, height), bg)
    # One row/column each avoids allocating two additional full float64 canvases.
    xx = np.arange(width, dtype=np.float32)[None, :]
    yy = np.arange(height, dtype=np.float32)[:, None]
    r2 = ((xx - width / 2) / (width / 2)) ** 2 + ((yy - height / 2) / (height / 2)) ** 2
    k = (1.0 - fx.get("falloff", 0.05) * r2 / 2.0)[..., None]
    arr = np.asarray(bg, dtype=np.float32)[None, None, :] * k
    return Image.fromarray(arr.clip(0, 255).astype(np.uint8), "RGB")
