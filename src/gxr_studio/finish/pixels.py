"""JPG 上的调整，以及 RAW 成片上多出来的四项。正值：曝光变亮，黑点压黑，饱和度和自然饱和度更艳，暗角提亮四角。"""

from __future__ import annotations

from typing import cast

import numpy as np

from ..render.params import Adjust
from ..render.tones import apply_table as _apply_table
from ..render.tones import channel_gains, display_table


def _srgb_to_linear(rgb: np.ndarray) -> np.ndarray:
    x = rgb.astype(np.float32) / np.float32(255.0)
    return np.where(x <= 0.04045, x / np.float32(12.92), ((x + 0.055) / 1.055) ** 2.4).astype(np.float32)


def _linear_to_srgb(lin: np.ndarray) -> np.ndarray:
    x = np.clip(lin, 0.0, None)
    encoded = np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1.0 / 2.4) - 0.055)
    return cast(np.ndarray, np.clip(encoded * 255.0 + 0.5, 0, 255).astype(np.uint8))


def apply_base(rgb: np.ndarray, adjust: Adjust, curve: tuple[tuple[float, float], ...] | None) -> np.ndarray:
    """曝光、色温、色调、对比度、高光、阴影、曲线。给没有 RAW 的 JPEG 用。"""
    if adjust.exposure_ev == 0 and adjust.warm == 0 and adjust.tint == 0 and adjust.tone_is_identity and curve is None:
        return rgb
    lin = _srgb_to_linear(rgb)
    gains = channel_gains(adjust)
    if gains is not None:
        lin = lin * gains.astype(np.float32)
    out = _linear_to_srgb(lin)
    table = display_table(adjust, curve)
    if table is not None:
        flat = _apply_table(out.astype(np.float32), table)
        out = np.clip(flat + 0.5, 0, 255).astype(np.uint8).reshape(out.shape)
    return out


def _black(rgb: np.ndarray, amount: int) -> np.ndarray:
    if amount == 0:
        return rgb
    y = rgb.astype(np.float32) / 255.0
    k = abs(amount) / 100.0 * 0.35
    if amount > 0:
        y = np.clip((y - k) / max(1e-4, 1.0 - k), 0.0, 1.0)
    else:
        y = y * (1.0 - k) + k
    return cast(np.ndarray, np.clip(y * 255.0 + 0.5, 0, 255).astype(np.uint8))


def _chroma(rgb: np.ndarray, amount: int, vibrance: bool) -> np.ndarray:
    if amount == 0:
        return rgb
    x = rgb.astype(np.float32)
    gray = x[..., 0] * 0.2126 + x[..., 1] * 0.7152 + x[..., 2] * 0.0722
    if vibrance:
        peak = x.max(axis=-1)
        floor = x.min(axis=-1)
        dull = 1.0 - np.clip((peak - floor) / 255.0, 0.0, 1.0)
        factor = 1.0 + (amount / 100.0) * dull
    else:
        factor = np.float32(1.0 + amount / 100.0)
    scale = factor[..., None] if vibrance else factor
    mixed = gray[..., None] + (x - gray[..., None]) * scale
    return cast(np.ndarray, np.clip(mixed + 0.5, 0, 255).astype(np.uint8))


def _vignette(rgb: np.ndarray, amount: int) -> np.ndarray:
    if amount == 0:
        return rgb
    h, w = rgb.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    nx = (xx - (w - 1) / 2.0) / max((w - 1) / 2.0, 1.0)
    ny = (yy - (h - 1) / 2.0) / max((h - 1) / 2.0, 1.0)
    radius = np.clip(np.hypot(nx, ny), 0.0, 1.0).astype(np.float32)
    mask = radius * radius * (3.0 - 2.0 * radius)
    gain = 1.0 + (amount / 100.0) * 0.65 * mask
    return cast(np.ndarray, np.clip(rgb.astype(np.float32) * gain[..., None] + 0.5, 0, 255).astype(np.uint8))


def apply_extra(rgb: np.ndarray, adjust: Adjust) -> np.ndarray:
    """黑点、饱和度、自然饱和度、暗角。RAW 和 JPEG 成片都走这里。"""
    out = _black(rgb, adjust.black_point)
    out = _chroma(out, adjust.saturation, False)
    out = _chroma(out, adjust.vibrance, True)
    return _vignette(out, adjust.vignette)
