"""电脑专用调整：白平衡偏移 + 曝光（链的输入端）和显示域的一维调子表（链的输出端）。

输入端（线性的白平衡后码值，0..4095）：
    码值 × 2^EV × (R、G、B 增益)，之后由配方链第一步夹到 0..4095。
    暖/品偏移只动 R 和 B 的增益（ARCHITECTURE §5.1 阶段 2）：暖 = R 升 B 降，品红 = R、B 一起升（相对绿）。

输出端（显示域 0..255，配方链之后）：
    画面对比、高光、阴影与主曲线合成**一条**表，逐通道套用（§5.5）。名字和相机旋钮相同，但是另一套算法，
    不复用 `knobs.tone_curve` 的剂量，界面上标"仅电脑"。
"""

from __future__ import annotations

import numpy as np
from scipy.interpolate import PchipInterpolator

from .params import Adjust

WARM_EV = 0.35            # 色温 ±100 对应 R/B 增益 2^±0.35
TINT_EV = 0.25            # 色调 ±100 对应 R、B 同升/同降 2^±0.25
TABLE_STEP = 4            # 调子表每 1 个显示码值取 4 个点
TABLE_SIZE = 255 * TABLE_STEP + 1
SHADOW_HIGHLIGHT_AMOUNT = 0.13   # 高光/阴影 ±100 在峰值处最多挪 13% 满幅度（再大就会把曲线折回来）


def channel_gains(adjust: Adjust) -> np.ndarray | None:
    """(3,) float64 的 R、G、B 乘数；没有任何曝光/白平衡调整时返回 None（链的输入原样通过）。"""
    if adjust.exposure_ev == 0 and adjust.warm == 0 and adjust.tint == 0:
        return None
    warm, tint = adjust.warm / 100.0, adjust.tint / 100.0
    r = adjust.exposure_ev + WARM_EV * warm + TINT_EV * tint
    g = adjust.exposure_ev
    b = adjust.exposure_ev - WARM_EV * warm + TINT_EV * tint
    return np.exp2(np.array([r, g, b], np.float64))


def _smoothstep(t: np.ndarray) -> np.ndarray:
    return t * t * (3.0 - 2.0 * t)


def tone_function(t: np.ndarray, adjust: Adjust) -> np.ndarray:
    """0..1 → 0..1 的画面对比/高光/阴影。k = ±1 时仍然单调（测试里验证）。"""
    y = t
    if adjust.contrast:
        k = adjust.contrast / 100.0
        y = y + k * (_smoothstep(y) - y)
    if adjust.highlight:
        y = y + adjust.highlight / 100.0 * SHADOW_HIGHLIGHT_AMOUNT * 6.75 * y * y * (1.0 - y)
    if adjust.shadow:
        y = y + adjust.shadow / 100.0 * SHADOW_HIGHLIGHT_AMOUNT * 6.75 * y * (1.0 - y) ** 2
    return np.clip(y, 0.0, 1.0)


def display_table(adjust: Adjust, master: tuple[tuple[float, float], ...] | None) -> np.ndarray | None:
    """显示域一维表 (TABLE_SIZE,) float32，输入 0..255 每 1/TABLE_STEP 一个点；全是恒等就返回 None。"""
    if adjust.tone_is_identity and master is None:
        return None
    x = np.linspace(0.0, 255.0, TABLE_SIZE)
    y = tone_function(x / 255.0, adjust) * 255.0
    if master is not None:
        xs, ys = (np.array(v, np.float64) for v in zip(*master))
        curve = PchipInterpolator(xs, ys, extrapolate=False)
        inside = curve(np.clip(y, xs[0], xs[-1]))
        y = np.clip(inside, 0.0, 255.0)
    return y.astype(np.float32)


def apply_table(values: np.ndarray, table: np.ndarray) -> np.ndarray:
    """对 0..255 的浮点值逐元素查表（均匀网格上的线性插值）；返回 float32。"""
    pos = np.clip(np.asarray(values, np.float32), 0.0, 255.0) * np.float32(TABLE_STEP)
    i = np.minimum(pos.astype(np.int32), TABLE_SIZE - 2)
    f = pos - i
    lo = table[i]
    return lo + (table[i + 1] - lo) * f
