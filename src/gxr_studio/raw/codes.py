"""白平衡后的传感器码值（渲染链的输入域）和半分辨率快速路径。

数值口径与研究代码一致（`model_adapter.Model.raw`、`lut_convert/camera.py scene()`）：

    归一化 = (线性化值 - 黑电平) / (白电平 - 黑电平)        ← Dng.normalised_at
    码值   = 归一化 / AsShotNeutral[颜色] × 4095             ← 不夹紧、不加曝光（BaselineExposure 不应用）

半分辨率 = 以裁切窗口（DefaultCrop）为基准的每个 2×2 小块，同色位置取平均（绿色两个位置取平均），
输出 (h/2, w/2, 3)，不做去马赛克。这与研究代码"稀疏 2×2 CFA 均值"逐点相同（见 tests/test_raw.py）。
"""

from __future__ import annotations

import numpy as np

from .dng import Dng, RawDecodeError

CODE_MAX = 4095.0
STRIP_ROWS = 256


def wb_gains(dng: Dng) -> np.ndarray:
    """每个颜色（R、G、B）的乘数 4095 / AsShotNeutral。"""
    n = dng.as_shot_neutral
    if n is None or n.shape != (3,) or not np.all(np.isfinite(n)) or np.any(n <= 0):
        raise RawDecodeError("这份 RAW 没有可用的白平衡信息（AsShotNeutral）")
    return CODE_MAX / n


def crop_shape(dng: Dng) -> tuple[int, int]:
    """DefaultCrop 的 (高, 宽)，未转正。"""
    return dng.crop_size[1], dng.crop_size[0]


def half_res_codes(dng: Dng, dtype=np.float32) -> np.ndarray:
    """半分辨率白平衡码值 (h//2, w//2, 3)，未转正（方向见 `orient_array`）。"""
    wb_gains(dng)  # 只为校验 AsShotNeutral；下面保持研究代码的算式顺序 均值 / neutral × 4095
    y0, x0, h, w = dng.crop_window()
    h2, w2 = h // 2, w // 2
    if h2 < 1 or w2 < 1:
        raise RawDecodeError("画面太小")
    phase = dng.crop_cfa_phase()
    out = np.empty((h2, w2, 3), dtype)
    # 逐条带做，避免整幅 float64（12 MP ≈ 98 MB）
    step = STRIP_ROWS // 2
    for r0 in range(0, h2, step):
        r1 = min(h2, r0 + step)
        norm = dng.normalised_block(y0 + 2 * r0, y0 + 2 * r1, x0, x0 + 2 * w2)
        quad = norm.reshape(r1 - r0, 2, w2, 2)
        for c in range(3):
            sites = [(i, j) for i in range(2) for j in range(2) if phase[i, j] == c]
            mean = sum(quad[:, i, :, j] for i, j in sites) / len(sites)
            out[r0:r1, :, c] = mean / dng.as_shot_neutral[c] * CODE_MAX
    return out


def wb_mosaic_block(dng: Dng, y0: int, y1: int, dtype=np.float32) -> np.ndarray:
    """裁切窗口里第 [y0, y1) 行（裁切坐标）的白平衡后马赛克：每个位置乘它自己颜色的 4095/AsShotNeutral。"""
    gains = wb_gains(dng)
    cy0, cx0, h, w = dng.crop_window()
    norm = dng.normalised_block(cy0 + y0, cy0 + y1, cx0, cx0 + w)
    yy = np.arange(cy0 + y0, cy0 + y1)[:, None]
    xx = np.arange(cx0, cx0 + w)[None, :]
    return (norm * gains[dng.cfa_at(yy, xx)]).astype(dtype, copy=False)


def orient_array(a: np.ndarray, orientation: int) -> np.ndarray:
    """按 TIFF Orientation 把存储方向的数组转正（与 `lut_convert/camera.py scene()` 里的 orient 相同）。"""
    o = int(orientation)
    if o == 1:
        return a
    if o == 2:
        return a[:, ::-1]
    if o == 3:
        return a[::-1, ::-1]
    if o == 4:
        return a[::-1]
    if o == 5:
        return a.swapaxes(0, 1)
    if o == 6:
        return np.rot90(a, 3)
    if o == 7:
        return a.swapaxes(0, 1)[::-1, ::-1]
    if o == 8:
        return np.rot90(a, 1)
    raise RawDecodeError(f"不支持的 Orientation：{orientation}")
