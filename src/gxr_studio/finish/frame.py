"""六种边框加在照片外面。徕卡水印的两行参数靠右，竖线在文字块左边。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .spec import FrameSpec

ASSETS = Path(__file__).resolve().parents[1] / "assets"
SANS = ASSETS / "fonts" / "AtkinsonHyperlegible-Regular.ttf"
PARAM = ASSETS / "fonts" / "Inter-Regular.ttf"
TYPEWRITER = ASSETS / "fonts" / "CourierPrime-Regular.ttf"
HAND = ASSETS / "fonts" / "PatrickHand-Regular.ttf"
COLORS = {"white": (255, 255, 255), "black": (12, 12, 12), "cream": (245, 236, 220)}
WEIGHT = {"thin": 0.02, "medium": 0.04, "thick": 0.07}
CANVAS = {"1:1": (1, 1), "4:5": (4, 5), "3:2": (3, 2), "9:16": (9, 16)}


def _font(path: Path, px: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), max(8, px))


def _text_face(path: Path, px: int, text: str) -> ImageFont.FreeTypeFont:
    """自定义文字缺中文字形时，按系统字体顺序回退。仓库里不打包中文字体。"""
    from ..render.params import EditError
    from .stamp import cjk_font, text_needs_cjk

    if text_needs_cjk(text):
        face = cjk_font(px)
        if face is None:
            raise EditError("missing_cjk", "无法显示中文。请改用英文或数字。")
        return face
    return _font(path, px)


def _fit(draw: ImageDraw.ImageDraw, text: str, path: Path, px: int, max_w: int) -> ImageFont.FreeTypeFont:
    face = _font(path, px)
    while px > 10 and draw.textlength(text, font=face) > max_w:
        px -= 1
        face = _font(path, px)
    return face


def _margin(image: Image.Image, weight: str) -> int:
    return max(8, round(max(image.size) * WEIGHT[weight]))


def apply_frame(rgb: np.ndarray, spec: FrameSpec, meta: dict[str, str]) -> np.ndarray:
    if spec.style == "none":
        return rgb
    image = Image.fromarray(rgb, "RGB")
    if spec.style == "gallery":
        out = _gallery(image, spec)
    elif spec.style == "leica":
        out = _leica(image, spec, meta)
    elif spec.style == "hasselblad":
        out = _hasselblad(image, spec, meta)
    elif spec.style == "film":
        out = _film(image, meta)
    elif spec.style == "instant":
        out = _instant(image, spec)
    else:
        out = _canvas(image, spec)
    return np.asarray(out)


def _gallery(image: Image.Image, spec: FrameSpec) -> Image.Image:
    m = _margin(image, spec.weight)
    canvas = Image.new("RGB", (image.width + 2 * m, image.height + 2 * m), COLORS[spec.color])
    canvas.paste(image, (m, m))
    return canvas


def _leica(image: Image.Image, spec: FrameSpec, meta: dict[str, str]) -> Image.Image:
    bar = max(64, round(min(image.size) * 0.09))
    canvas = Image.new("RGB", (image.width, image.height + bar), COLORS[spec.color])
    canvas.paste(image, (0, 0))
    draw = ImageDraw.Draw(canvas)
    ink = (20, 20, 20) if spec.color != "black" else (245, 245, 245)
    muted = (90, 90, 90) if spec.color != "black" else (170, 170, 170)
    pad = max(16, bar // 5)
    if spec.show_logo:
        logo = Image.open(ASSETS / "logo" / "ricoh.png").convert("RGBA")
        lh = int(bar * 0.42)
        logo = logo.resize((max(1, round(logo.width * lh / logo.height)), lh), Image.Resampling.LANCZOS)
        canvas.paste(logo, (pad, image.height + (bar - lh) // 2), logo)
        gx = pad + logo.width + pad // 2
        draw.text((gx, image.height + (bar - int(bar * 0.34)) // 2 - 2), "GXR", font=_font(SANS, int(bar * 0.34)), fill=ink)
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
    if not lines:
        return canvas
    first, second = lines[0], "  ".join(lines[1:])
    f1 = _fit(draw, first, PARAM, int(bar * 0.26), image.width // 2)
    f2 = _fit(draw, second, PARAM, int(bar * 0.22), image.width // 2) if second else f1
    w1 = draw.textlength(first, font=f1)
    w2 = draw.textlength(second, font=f2) if second else 0
    block = max(w1, w2)
    right = image.width - pad
    gap = max(8, bar // 8)
    rule = right - block - gap
    draw.line([(rule, image.height + pad), (rule, image.height + bar - pad)], fill=muted, width=max(1, bar // 28))
    y1 = image.height + int(bar * 0.16)
    draw.text((right - w1, y1), first, font=f1, fill=ink)
    if second:
        draw.text((right - w2, image.height + int(bar * 0.52)), second, font=f2, fill=muted)
    return canvas


def _hasselblad(image: Image.Image, spec: FrameSpec, meta: dict[str, str]) -> Image.Image:
    thin = max(10, round(max(image.size) * 0.018))
    bottom = max(thin * 4, round(min(image.size) * 0.16))
    canvas = Image.new("RGB", (image.width + 2 * thin, image.height + thin + bottom), COLORS[spec.color])
    canvas.paste(image, (thin, thin))
    draw = ImageDraw.Draw(canvas)
    ink = (24, 24, 24) if spec.color != "black" else (245, 245, 245)
    muted = (120, 120, 120) if spec.color != "black" else (180, 180, 180)
    title = _font(SANS, max(16, bottom // 5))
    sub = _font(PARAM, max(13, bottom // 7))
    cx = canvas.width // 2
    y0 = thin + image.height + int(bottom * 0.28)
    name = "RICOH GXR"
    draw.text((cx - draw.textlength(name, font=title) / 2, y0), name, font=title, fill=ink)
    bits: list[str] = []
    if spec.show_params:
        bits.extend(bit for bit in (meta.get("lens"), meta.get("aperture"), meta.get("shutter"), meta.get("iso")) if bit)
    if spec.show_date and meta.get("date"):
        bits.append(meta["date"])
    if spec.text:
        bits.append(spec.text)
    line = "  ·  ".join(bits)
    if line:
        draw.text((cx - draw.textlength(line, font=sub) / 2, y0 + int(bottom * 0.28)), line, font=sub, fill=muted)
    return canvas


def _film(image: Image.Image, meta: dict[str, str]) -> Image.Image:
    m = max(28, round(max(image.size) * 0.045))
    canvas = Image.new("RGB", (image.width + 2 * m, image.height + 2 * m), (8, 8, 8))
    canvas.paste(image, (m, m))
    draw = ImageDraw.Draw(canvas)
    amber = (232, 164, 74)
    face = _font(TYPEWRITER, max(11, m // 3))
    lens = meta.get("lens_short") or "A12 50mm"
    draw.text((m, canvas.height - m + 6), f"RICOH GXR  ·  {lens}", font=face, fill=amber)
    number = meta.get("frame_no") or ""
    if number:
        draw.text((canvas.width - m - draw.textlength(number, font=face), 6), number, font=face, fill=amber)
    return canvas


def _instant(image: Image.Image, spec: FrameSpec) -> Image.Image:
    side = max(18, round(max(image.size) * 0.035))
    bottom = max(side * 4, round(min(image.size) * 0.20))
    canvas = Image.new("RGB", (image.width + 2 * side, image.height + side + bottom), (248, 246, 242))
    canvas.paste(image, (side, side))
    if spec.text:
        draw = ImageDraw.Draw(canvas)
        px = max(22, bottom // 4)
        face = _text_face(HAND, px, spec.text)
        max_w = max(8, canvas.width - 2 * side)
        while px > 10 and draw.textlength(spec.text, font=face) > max_w:
            px = max(10, int(px * 0.85))
            face = _text_face(HAND, px, spec.text)
        tw = draw.textlength(spec.text, font=face)
        draw.text(((canvas.width - tw) / 2, side + image.height + (bottom - face.size) / 2), spec.text, font=face, fill=(40, 40, 40))
    return canvas


def _canvas(image: Image.Image, spec: FrameSpec) -> Image.Image:
    rw, rh = CANVAS[spec.canvas_ratio]
    target = rw / rh
    if image.width / image.height > target:
        out_w, out_h = image.width, round(image.width / target)
    else:
        out_h, out_w = image.height, round(image.height * target)
    canvas = Image.new("RGB", (out_w, out_h), COLORS[spec.color])
    canvas.paste(image, ((out_w - image.width) // 2, (out_h - image.height) // 2))
    return canvas
