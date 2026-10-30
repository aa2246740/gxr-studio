"""全分辨率去马赛克：Malvar–He–Cutler 5×5（ARCHITECTURE §2.3 / §5.1 阶段 4）。

研究代码里没有全分辨率去马赛克（标定用的是稀疏 2×2 均值），所以这里选 MHC：
- 线性滤波，输入输出都在线性的"白平衡后传感器码值"域，不引入非线性或方向判断带来的色块；
- 5×5 梯度校正比双线性明显少拉链和假色，又比方向自适应算法简单可复现；
- 是 ARCHITECTURE 已经写定的选择，之后换更好的（例如 LibRaw 后端）要重跑 §11 的等价测试。

输出不夹紧：滤波的负瓣在高反差边缘会略微越过 [0, 4095]，留给后面的链去夹（链本来就要夹 4095）。
边界用镜像延拓（保持 Bayer 相位），条带版与整幅版逐位相同。
"""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np
from scipy.ndimage import correlate

from .codes import STRIP_ROWS, crop_shape, wb_mosaic_block
from .dng import Dng, RawDecodeError

PAD = 2

_K_G = np.array([[0, 0, -1, 0, 0], [0, 0, 2, 0, 0], [-1, 2, 4, 2, -1], [0, 0, 2, 0, 0], [0, 0, -1, 0, 0]], np.float32) / 8
_K_H = np.array([[0, 0, .5, 0, 0], [0, -1, 0, -1, 0], [-1, 4, 5, 4, -1], [0, -1, 0, -1, 0], [0, 0, .5, 0, 0]],
                np.float32) / 8   # 在 G 位置补"左右邻居那种颜色"
_K_V = _K_H.T.copy()              # 在 G 位置补"上下邻居那种颜色"
_K_X = np.array([[0, 0, -1.5, 0, 0], [0, 2, 0, 2, 0], [-1.5, 0, 6, 0, -1.5], [0, 2, 0, 2, 0], [0, 0, -1.5, 0, 0]],
                np.float32) / 8   # 在 R 位置补 B、在 B 位置补 R


def demosaic_block(padded: np.ndarray, phase: np.ndarray) -> np.ndarray:
    """`padded`：四周各多 PAD 个像素的白平衡马赛克；`phase`：它的第 PAD 行第 PAD 列起的 2×2 颜色码。

    返回去掉边距的 (h, w, 3) float32。PAD 是偶数，所以填充区的颜色相位与真实区一致。
    """
    ph, pw = padded.shape
    h, w = ph - 2 * PAD, pw - 2 * PAD
    tile = np.tile(np.asarray(phase), (h // 2 + 1, w // 2 + 1))[:h, :w]
    right = np.tile(np.asarray(phase)[:, ::-1], (h // 2 + 1, w // 2 + 1))[:h, :w]  # 每个位置右邻的颜色
    m = padded[PAD:PAD + h, PAD:PAD + w]
    g_rb = correlate(padded, _K_G, mode="constant")[PAD:PAD + h, PAD:PAD + w]
    k_h = correlate(padded, _K_H, mode="constant")[PAD:PAD + h, PAD:PAD + w]
    k_v = correlate(padded, _K_V, mode="constant")[PAD:PAD + h, PAD:PAD + w]
    k_x = correlate(padded, _K_X, mode="constant")[PAD:PAD + h, PAD:PAD + w]
    is_r, is_g, is_b = tile == 0, tile == 1, tile == 2
    rg = is_g & (right == 0)   # 左右邻居是 R 的绿点
    out = np.empty((h, w, 3), np.float32)
    out[..., 0] = np.where(is_r, m, np.where(is_b, k_x, np.where(rg, k_h, k_v)))
    out[..., 1] = np.where(is_g, m, g_rb)
    out[..., 2] = np.where(is_b, m, np.where(is_r, k_x, np.where(rg, k_v, k_h)))
    return out


def _reflect_pad(block: np.ndarray, top: int, bottom: int) -> np.ndarray:
    return np.pad(block, ((top, bottom), (PAD, PAD)), mode="reflect")


def demosaic_strips(dng: Dng, rows: int = STRIP_ROWS) -> Iterator[tuple[int, np.ndarray]]:
    """逐条带产出 (起始行, (n, w, 3) float32 白平衡码值)，裁切坐标、未转正。内存峰值只和条带有关。"""
    if rows < 2 or rows % 2:
        raise ValueError("条带行数要是正偶数")
    h, w = crop_shape(dng)
    if h < 4 or w < 4:
        raise RawDecodeError("画面太小")
    phase = dng.crop_cfa_phase()
    for y0 in range(0, h, rows):
        y1 = min(h, y0 + rows)
        lo, hi = max(0, y0 - PAD), min(h, y1 + PAD)
        block = wb_mosaic_block(dng, lo, hi)
        padded = _reflect_pad(block, PAD - (y0 - lo), PAD - (hi - y1))
        yield y0, demosaic_block(padded, phase)   # y0 是偶数，条带起点的相位等于整幅的相位


def demosaic_full(dng: Dng) -> np.ndarray:
    """整幅 (h, w, 3) float32（12 MP 约 147 MB）。导出请用 `demosaic_strips`。"""
    h, w = crop_shape(dng)
    out = np.empty((h, w, 3), np.float32)
    for y0, strip in demosaic_strips(dng):
        out[y0:y0 + strip.shape[0]] = strip
    return out
