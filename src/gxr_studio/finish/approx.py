"""只有 JPEG 时，用显示域 LUT 近似八个内置配方的默认档。旋钮不进这张表。"""

from __future__ import annotations

from typing import Any

import numpy as np

from ..render import chain

# 色卡 R8051148（ISO 500，和日常 ISO 800 不是同一张图）上，八个内置配方的平均 ΔE00 是 5.179，超过 3。
# 所以近似配方关闭，capabilities.recipe 退回 none。
ENABLED = False


def fit_lut(src: np.ndarray, dst: np.ndarray, n: int = 17) -> np.ndarray:
    idx = np.clip((src.reshape(-1, 3).astype(np.float64) / 256.0 * n).astype(np.int32), 0, n - 1)
    acc = np.zeros((n, n, n, 3), np.float64)
    cnt = np.zeros((n, n, n), np.int32)
    lin = idx[:, 0] * n * n + idx[:, 1] * n + idx[:, 2]
    np.add.at(acc.reshape(-1, 3), lin, dst.reshape(-1, 3))
    np.add.at(cnt.reshape(-1), lin, 1)
    filled = cnt > 0
    lut = np.zeros_like(acc)
    lut[filled] = acc[filled] / cnt[filled, None]
    centers = (np.arange(n) + 0.5) / n * 255.0
    grid = np.stack(np.meshgrid(centers, centers, centers, indexing="ij"), -1)
    lut[~filled] = grid[~filled]
    return lut


def apply_lut(image: np.ndarray, lut: np.ndarray) -> np.ndarray:
    n = lut.shape[0]
    p = np.clip(image.astype(np.float64), 0, 255) / 255.0 * (n - 1)
    i0 = np.floor(p).astype(np.int32)
    i1 = np.minimum(i0 + 1, n - 1)
    f = p - i0
    acc = np.zeros(image.shape, np.float64)
    for bit in range(8):
        ix = i1[..., 0] if bit & 1 else i0[..., 0]
        iy = i1[..., 1] if bit & 2 else i0[..., 1]
        iz = i1[..., 2] if bit & 4 else i0[..., 2]
        w = (f[..., 0] if bit & 1 else 1 - f[..., 0]) * (f[..., 1] if bit & 2 else 1 - f[..., 1]) * (f[..., 2] if bit & 4 else 1 - f[..., 2])
        acc += lut[ix, iy, iz] * w[..., None]
    return np.clip(acc + 0.5, 0, 255).astype(np.uint8)


def lut_for_entries(model: Any, stock: bytes, recipe: bytes) -> np.ndarray:
    axis = np.linspace(0, 4095, 12, dtype=np.float32)
    rr, gg, bb = np.meshgrid(axis, axis, axis, indexing="ij")
    codes = np.stack([rr, gg, bb], -1)
    stock_bake = chain.bake_chroma(model, stock, 0.75)
    recipe_bake = chain.bake_chroma(model, recipe, 0.75)
    stock_rgb = chain.chroma_u8(codes, stock_bake, None, None)
    recipe_rgb = chain.chroma_u8(codes, recipe_bake, None, None)
    return fit_lut(stock_rgb, recipe_rgb)
