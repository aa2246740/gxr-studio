"""DNG → 显卡能直接读的 RAW 布局（`frontend/src/shaders/bin.wgsl` 的输入）。

显卡做的事和 `raw.codes.half_res_codes` + `orient_array` 相同：裁切窗口里每个 2×2 同色取平均，除以 AsShotNeutral 乘 4095，
再按 Orientation 转正。这里只描述字节在哪里、怎么解；显卡不认的写法一律返回原因，前端退回原来的服务器预览。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from ..lut.scene import ANCHOR, SceneColour, SceneError, scene_colour
from ..raw.codes import CODE_MAX, wb_gains
from ..raw.dng import Dng


@dataclass(frozen=True)
class RawSource:
    layout: dict[str, Any] | None
    reason: str | None
    scene: dict[str, Any] | None


def _black_table(dng: Dng) -> list[float] | None:
    """相对 ActiveArea 的 (y%2, x%2) 四个黑电平；更复杂的写法（大于 2×2、逐行逐列的偏移）返回 None。"""
    bp = np.asarray(dng.black_pattern, np.float64)
    if bp.ndim != 2 or bp.shape[0] > 2 or bp.shape[1] > 2 or dng.black_dh is not None or dng.black_dv is not None:
        return None
    return [float(bp[ry % bp.shape[0], rx % bp.shape[1]]) for ry in range(2) for rx in range(2)]


def raw_layout(dng: Dng) -> tuple[dict[str, Any] | None, str | None]:
    """返回 (布局, None)，或 (None, 不支持的原因)。"""
    if dng.bps not in (12, 16):
        return None, f"bits_per_sample_{dng.bps}"
    if dng.bps == 12 and dng.width % 2:
        return None, "odd_width_12bit"
    if dng.lin_table is not None:
        return None, "linearization_table"
    if dng.cfa.shape != (2, 2):
        return None, "cfa_not_2x2"
    black = _black_table(dng)
    if black is None:
        return None, "black_level_pattern"
    if not 1 <= dng.orientation <= 8:
        return None, "orientation"
    y0, x0, h, w = dng.crop_window()
    if h < 2 or w < 2:
        return None, "too_small"
    gains = wb_gains(dng)  # 没有可用的 AsShotNeutral 时抛 RawDecodeError，调用方处理
    offs, cnts = dng._strips  # noqa: SLF001  条带位置只在 Dng 里有，只读
    phase = [int(v) for v in np.asarray(dng.crop_cfa_phase()).reshape(-1)]
    w2, h2 = w // 2, h // 2
    swap = dng.orientation >= 5
    return {
        "bps": dng.bps,
        "big_endian": dng.endian == "big",
        "row_bytes": (dng.width * dng.bps + 7) // 8,
        "strips": [[int(o), int(n)] for o, n in zip(offs, cnts)],
        "crop": [int(y0), int(x0), int(h), int(w)],
        "active": [int(dng.active[0]), int(dng.active[1])],
        "phase": phase,
        "black": black,
        "white": float(dng.white),
        "wb": [float(v) for v in gains],
        "orientation": int(dng.orientation),
        "width": int(dng.width),
        "height": int(dng.height),
        # 颜色层（半分辨率、已转正）和全尺寸（已转正）的宽高
        "layer": [h2, w2] if swap else [w2, h2],
        "full": [h, w] if swap else [w, h],
    }, None


def affine_of(colour: SceneColour) -> dict[str, Any] | None:
    """`SceneColour.xyz` 展开成 xyz = A·codes + b（逐式相同）。"""
    u, f = ANCHOR["u_m"], ANCHOR["f"]
    white = colour.neutral @ colour.matrix.T
    scale = 0.18 / ((u - f) * white[1])
    weighted = colour.matrix * colour.neutral[None, :] * scale
    a = weighted / CODE_MAX
    b = -f * weighted.sum(axis=1)
    if not (np.all(np.isfinite(a)) and np.all(np.isfinite(b))):
        return None
    return {"A": a.tolist(), "b": b.tolist()}


def scene_affine(dng: Dng) -> dict[str, Any] | None:
    """Log 类 LUT 要的 码值 → 场景 XYZ。DNG 没有可用的色彩矩阵时返回 None（前端退回服务器出图）。"""
    try:
        return affine_of(scene_colour(dng))
    except SceneError:
        return None


def describe(dng: Dng, *, with_scene: bool) -> RawSource:
    layout, reason = raw_layout(dng)
    return RawSource(layout, reason, scene_affine(dng) if with_scene and layout is not None else None)
