"""显卡对拍的颜色门槛（复审 r1 颜色章节第 1–4 条，编排者定稿）。CI 的合成用例和本机真实照片用例共用这一份。

1. 每个用例：≤1 码值的像素 ≥ 99.99%，平均 ΔE2000 ≤ 0.01。两项都用未舍入的计数判（`超过 1 码值的像素数 × 10⁴ ≤ N`）。
2. 有效区边缘带：在 CPU 有效区蒙版（`warp_valid`，裁到成片框）上取边界内外各一像素，随旋转、翻转、裁切一起变换。
   没有拉直时蒙版全是有效，边缘带为空：整块无效区、裁切边缘都不豁免。
3. 带外：最大差 ≤ 4；超过 2 码值的像素必须逐个由前向量化复算解释（`explain`），而且只能出现在这个用例冻结的位置上，
   差值不超过冻结值。新位置、更多像素、更大的差都失败。
4. 带内：超过 2 码值的像素数 ≤ ceil(N × 10⁻⁶)。

显示类 LUT 不豁免：带 `display_lut=True` 的用例冻结列表必须为空。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

import numpy as np
from scipy import ndimage

from gxr_studio.engine.colour import delta_e00, lab_from_codes
from gxr_studio.finish.geometry import warp_valid
from gxr_studio.finish.spec import Geometry

Frozen = Mapping[tuple[int, int], int]  # (y, x) → 冻结时的差值上限
Explain = Callable[[list[tuple[int, int]]], list[bool]]


def valid_mask(layer_w: int, layer_h: int, geom: Geometry, box: tuple[int, int, int, int] | None) -> np.ndarray:
    """成片坐标里的 CPU 有效区蒙版：颜色层按几何变换后，裁到成片框（没有框 = 整张）。"""
    mask = warp_valid(layer_w, layer_h, geom)
    if box is not None:
        x, y, w, h = box
        mask = mask[y : y + h, x : x + w]
    return mask


def edge_band(mask: np.ndarray) -> np.ndarray:
    """有效区边界内外各一像素（8 邻接）。画面四周不算边界。"""
    inner = mask & ~ndimage.binary_erosion(mask, structure=np.ones((3, 3), bool), border_value=1)
    outer = ~mask & ndimage.binary_dilation(mask, structure=np.ones((3, 3), bool))
    return inner | outer


def evaluate(
    gpu: np.ndarray,
    ref: np.ndarray,
    *,
    mask: np.ndarray | None = None,
    frozen: Frozen | None = None,
    explain: Explain | None = None,
    display_lut: bool = False,
) -> dict[str, Any]:
    """返回统计和判定（`pass`、失败原因 `why`）。所有计数都是整数，不舍入。"""
    assert gpu.shape == ref.shape, (gpu.shape, ref.shape)
    h, w = gpu.shape[:2]
    n = h * w
    diff = np.abs(gpu.astype(np.int16) - ref.astype(np.int16)).max(axis=2)
    de = delta_e00(lab_from_codes(gpu.reshape(-1, 3)), lab_from_codes(ref.reshape(-1, 3)))
    band = edge_band(mask) if mask is not None else np.zeros((h, w), bool)
    over1 = int((diff > 1).sum())
    out_band = diff * ~band
    in_band = diff * band
    over2_out = [(int(y), int(x), int(out_band[y, x])) for y, x in zip(*np.nonzero(out_band > 2))]
    over2_in = int((in_band > 2).sum())
    frozen = dict(frozen or {})
    why: list[str] = []
    if over1 * 10_000 > n:
        why.append(f"超过 1 码值的像素 {over1}/{n} > 0.01%")
    if float(de.mean()) > 0.01:
        why.append(f"平均 ΔE00 {float(de.mean()):.8f} > 0.01")
    if int(out_band.max(initial=0)) > 4:
        why.append(f"带外最大差 {int(out_band.max())} > 4")
    if display_lut and frozen:
        why.append("显示类 LUT 不允许冻结豁免")
    unknown = [(y, x, d) for y, x, d in over2_out if (y, x) not in frozen or d > frozen[(y, x)]]
    if unknown:
        why.append(f"带外 >2 码值出现在未冻结的位置或差值变大：{unknown[:8]}")
    explained = None
    if over2_out and not unknown:
        if explain is None:
            why.append("带外有 >2 码值的像素，但没有前向量化复算")
        else:
            explained = explain([(y, x) for y, x, _d in over2_out])
            if not all(explained):
                why.append(f"前向量化复算解释不了：{[p for p, ok in zip(over2_out, explained) if not ok]}")
    if over2_in > math.ceil(n * 1e-6):
        why.append(f"边缘带内 >2 码值 {over2_in} > ceil(N×10⁻⁶) = {math.ceil(n * 1e-6)}")
    return {
        "pixels": n,
        "mean_de00": float(de.mean()),
        "over_1": over1,
        "within_1": (n - over1) / n,
        "identical": int((diff == 0).sum()),
        "max_code": int(diff.max(initial=0)),
        "band_pixels": int(band.sum()),
        "max_out_band": int(out_band.max(initial=0)),
        "over_2_out_band": [[y, x, d] for y, x, d in over2_out],
        "over_2_in_band": over2_in,
        "explained": explained,
        "pass": not why,
        "why": why,
    }
