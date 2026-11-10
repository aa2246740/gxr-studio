"""Seeded fibre paper, negative-carrier contours and photo-derived backdrops.

The export equations and NumPy RNG match the approved R3 renderer. Textures
are generated while painting, so layout/parity checks stay independent of pixels.
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageFilter

from .frame_core import Type
from .frame_paint import paint_poly
from .layout import Metrics


def blurred_background(photo: Image.Image, width: int, height: int, fx: dict[str, Any]) -> Image.Image:
    scale = max(width / photo.width, height / photo.height)
    bw, bh = max(width, round(photo.width * scale)), max(height, round(photo.height * scale))
    small = photo.resize((max(8, bw // 8), max(8, bh // 8)), Image.Resampling.BILINEAR)
    small = small.filter(ImageFilter.GaussianBlur(max(2.0, fx["sigma"] / 8.0)))
    arr = np.asarray(small.resize((bw, bh), Image.Resampling.BICUBIC), dtype=np.float32)
    left, top = (bw - width) // 2, (bh - height) // 2
    arr = arr[top : top + height, left : left + width]
    luma = (arr * np.array([0.299, 0.587, 0.114], dtype=np.float32)).sum(axis=2, keepdims=True)
    arr = (luma + (arr - luma) * fx.get("sat", 1.0)) * fx["veil"]
    return Image.fromarray(arr.clip(0, 255).astype(np.uint8), "RGB")


def paper_grain(canvas: Image.Image, amount: float) -> Image.Image:
    width, height = canvas.size
    rng = np.random.default_rng(seed=42)
    fine = rng.normal(0.0, 255.0 * amount, size=(height, width, 1)).astype(np.float32)
    low = Image.fromarray(rng.normal(128, 40, size=(max(4, height // 6), max(4, width // 6))).clip(0, 255).astype(np.uint8))
    low_arr = (np.asarray(low.resize((width, height), Image.Resampling.BICUBIC), dtype=np.float32)[..., None] - 128) * amount * 0.8
    return Image.fromarray((np.asarray(canvas, dtype=np.float32) + fine + low_arr).clip(0, 255).astype(np.uint8), "RGB")


def _wobble(n: int, rng: np.random.Generator, wavelength: float, notches: int, notch_w: float) -> NDArray[np.float64]:
    t = np.arange(n, dtype=np.float64)
    out = np.zeros(n)
    for k, amp in enumerate((1.0, 0.55, 0.3)):
        wl = wavelength / (1 + 1.7 * k)
        out += amp * np.sin(2 * math.pi * t / wl + rng.uniform(0, 6.28))
    out /= 1.85
    for _ in range(notches):
        pos, wid = rng.uniform(0, n), rng.uniform(0.5, 1.3) * notch_w
        out += rng.choice([-1.0, 1.0]) * rng.uniform(0.7, 1.4) * np.exp(-(((t - pos) / wid) ** 2))
    out += rng.normal(0, 0.05, n)
    return np.clip(out, -1.4, 1.4)


@lru_cache(maxsize=8)
def carrier_contours(w: int, h: int, c: int, jit: int, seed: int) -> tuple[list[list[float]], list[list[float]]]:
    """Local photo origin; the caller translates the immutable cached contours."""
    short = min(w, h)
    rng = np.random.default_rng(seed=seed)
    wob = {s: _wobble(w // 2 + 4 if s in "tb" else h // 2 + 4, rng, 0.35 * short, 4, 0.04 * short) for s in "trbl"}
    gap = {s: np.clip(_wobble(len(wob[s]), rng, 0.2 * short, 3, 0.03 * short) * 1.4 + 1.2, 0, 3.2) for s in "trbl"}
    outer, inner = [], []
    for side, length in (("t", w), ("r", h), ("b", w), ("l", h)):
        for i in range(length // 2 + 1):
            index = min(i, len(wob[side]) - 1)
            d = c + 0.55 * jit * float(wob[side][index]) + 0.45 * jit
            g = float(gap[side][index])
            along = i * 2
            if side == "t":
                outer.append([float(along), -d])
                inner.append([float(along), -g])
            elif side == "r":
                outer.append([w + d, float(along)])
                inner.append([w + g, float(along)])
            elif side == "b":
                outer.append([float(w - along), h + d])
                inner.append([float(w - along), h + g])
            else:
                outer.append([-d, float(h - along)])
                inner.append([-g, float(h - along)])
    return outer, inner


def paint_carrier(canvas: Image.Image, item: dict[str, Any]) -> None:
    contours = carrier_contours(item["w"], item["h"], item["c"], item["jit"], item["seed"])
    for points, fill in zip(contours, (item["fill"], item["paper"]), strict=True):
        paint_poly(canvas, {"pts": [[x + item["x"], y + item["y"]] for x, y in points], "fill": fill})


def text_ink(canvas: Image.Image, item: dict[str, Any], metrics: Metrics) -> tuple[int, int, int]:
    if not item.get("auto_ink"):
        r, g, b = item["fill"]
        return r, g, b
    width = math.ceil(Type(metrics).advance(item["font"], item["px"], item["text"], item.get("track", 0.0)))
    x0 = int(item["xs"][0]) if item["xs"] else 0
    region = canvas.crop((max(0, x0 - 6), max(0, item["y"]), min(canvas.width, x0 + width + 6), min(canvas.height, item["y"] + item["px"])))
    luma = float(np.asarray(region.convert("L"), dtype=np.float32).mean()) if region.width and region.height else 0.0
    light, dark = item["auto_ink"]
    r, g, b = dark if luma > 150 else light
    return r, g, b
