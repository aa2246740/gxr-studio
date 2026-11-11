"""用户自定义标志的尺寸规则、颜色选择和绘制（DESIGN.md §10.2、§10.3、§10.7）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image

SIZE_MULT: dict[str, float] = {"small": 0.80, "medium": 1.00, "large": 1.25}
_MULT: tuple[tuple[float, float], ...] = (
    (0.5, 2.6),
    (1.0, 2.5),
    (1.5, 2.5),
    (2.5, 2.3),
    (4.0, 1.5),
    (6.0, 1.04),
    (9.0, 0.88),
)


@dataclass(frozen=True)
class LogoInfo:
    """版面计算所需的标志描述（已裁到墨迹框的像素宽高、覆盖率、是否不透明、墨迹亮度）。"""

    id: str
    name: str
    w: int
    h: int
    coverage: float
    opaque: bool
    lum: float

    @property
    def aspect(self) -> float:
        return self.w / max(1, self.h)


def aspect_mult(aspect: float) -> float:
    """按墨迹框宽高比给出目标高度相对参考字大写高度 C 的倍数（分段线性）。"""
    if aspect <= _MULT[0][0]:
        return _MULT[0][1]
    for (a0, m0), (a1, m1) in zip(_MULT, _MULT[1:], strict=False):
        if aspect <= a1:
            return m0 + (m1 - m0) * (aspect - a0) / (a1 - a0)
    return _MULT[-1][1]


def coverage_comp(coverage: float) -> float:
    """细笔迹（签名）适当放大，实心块适当缩小。"""
    return float(min(1.25, max(0.90, (0.35 / max(coverage, 0.02)) ** 0.25)))


def fit_logo(logo: LogoInfo, cap: int, max_h: int, max_w: int, size: str = "medium") -> tuple[int, int]:
    """按 DESIGN.md §10.2 计算标志绘制像素尺寸 (w, h)。"""
    aspect = logo.aspect
    mult = SIZE_MULT.get(size, 1.0)
    target_h = cap * aspect_mult(aspect) * coverage_comp(logo.coverage) * mult
    target_h = min(target_h, float(max_h), max_w / aspect)
    ih = max(8, round(target_h))
    return max(8, round(ih * aspect)), ih


def pick_logo_mode(logo: LogoInfo, mode: str, bg_lum: float) -> str:
    """按 DESIGN.md §10.3 选择绘制模式。不透明图片永远保持原色，不重着色。"""
    if logo.opaque:
        return "orig"
    if mode in ("mono", "orig"):
        return mode
    return "mono" if abs(logo.lum - bg_lum) < 70.0 else "orig"


def clogo_item(
    logo: LogoInfo,
    x: int,
    y: int,
    w: int,
    h: int,
    mode: str,
    ink: tuple[int, int, int],
    bg_lum: float,
) -> dict[str, Any]:
    """生成版面里的自定义标志项（DESIGN.md §10.7）。"""
    return {
        "k": "clogo",
        "id": logo.id,
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "mode": pick_logo_mode(logo, mode, bg_lum),
        "ink": list(ink),
        "auto": mode == "auto" and not logo.opaque,
    }


def _alpha_luma(src: Image.Image) -> tuple[bool, float]:
    alpha_u8 = np.asarray(src.getchannel("A"))
    opaque = int(alpha_u8.min()) >= 250
    rgb = np.asarray(src.convert("RGB"), dtype=np.float32)
    alpha = alpha_u8.astype(np.float32) / 255.0
    luma = rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    mean_lum = float((luma * alpha).sum() / max(1e-6, float(alpha.sum())))
    return opaque, mean_lum


def paint_clogo(canvas: Image.Image, item: dict[str, Any], logos: dict[str, Image.Image] | None) -> None:
    """在导出画布上按 Lanczos 缩放绘制自定义标志（含模糊背景下的背景亮度采样）。"""
    if not logos:
        return
    src = logos.get(str(item.get("id") or ""))
    if src is None:
        return
    big = src.resize((int(item["w"]), int(item["h"])), Image.Resampling.LANCZOS)
    opaque, lg = _alpha_luma(src)
    mode = "orig" if opaque else str(item.get("mode") or "orig")
    raw_ink = item.get("ink") or [22, 22, 26]
    ink = (int(raw_ink[0]), int(raw_ink[1]), int(raw_ink[2]))
    if item.get("auto_ink") and not opaque:
        x0, y0 = max(0, int(item["x"])), max(0, int(item["y"]))
        x1 = min(canvas.width, int(item["x"]) + int(item["w"]))
        y1 = min(canvas.height, int(item["y"]) + int(item["h"]))
        reg = canvas.crop((x0, y0, x1, y1)).convert("L")
        bg_l = float(np.asarray(reg, dtype=np.float32).mean()) if reg.width and reg.height else 128.0
        light, dark = item["auto_ink"]
        chosen = dark if bg_l > 150.0 else light
        ink = (int(chosen[0]), int(chosen[1]), int(chosen[2]))
        if item.get("auto"):
            mode = "mono" if abs(lg - bg_l) < 70.0 else "orig"
    if mode == "mono" and not opaque:
        solid = Image.new("RGBA", big.size, (*ink, 255))
        solid.putalpha(big.getchannel("A"))
        big = solid
    canvas.paste(big, (int(item["x"]), int(item["y"])), big)
