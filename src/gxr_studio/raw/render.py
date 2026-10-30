"""RAW → 8 位 sRGB 显示图：渲染器接口，以及这一轮唯一的实现"中性转换"。

`Renderer.display` 把"白平衡后的传感器码值"（float32，形状 (h, w, 3)，0..4095，不夹紧）变成 uint8 sRGB。
它必须是逐像素的、与位置无关的函数，这样条带式全分辨率渲染和整幅渲染结果一致。

中性转换 = 码值 ÷ 4095 → 夹到 0..1 → 标准 sRGB 传递函数 → 8 位。没有配方、没有色彩矩阵、没有曝光和曲线，
所以颜色是相机原生的（偏平、偏淡）。它只是让只有 RAW 的照片能看、能导出，不是"相机的样子"，
界面和导出文件都要标成"中性转换"。

S6 接入配方渲染时，只需要提供另一个 `Renderer`（`calibrated=False/True`、自己的 label 和文件名标记），
`render_full` / `render_half` 和导出代码都不用改。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

import numpy as np

from .codes import CODE_MAX, crop_shape, half_res_codes, orient_array
from .demosaic import demosaic_strips
from .dng import Dng


def srgb_encode(linear: np.ndarray) -> np.ndarray:
    """IEC 61966-2-1 的 sRGB 传递函数，输入输出都是 0..1。"""
    a = np.clip(linear, 0.0, 1.0).astype(np.float32, copy=False)
    return cast(np.ndarray, np.where(a <= 0.0031308, a * 12.92, 1.055 * np.power(a, 1 / 2.4, dtype=np.float32) - 0.055))


def _neutral_display(codes: np.ndarray) -> np.ndarray:
    return np.rint(srgb_encode(codes / np.float32(CODE_MAX)) * 255.0).astype(np.uint8)


@dataclass(frozen=True)
class Renderer:
    id: str  # 写进 API 返回和 EXIF
    label: str  # 给人看的名字
    filename_tag: str  # 加在 `_gxr` 之后的文件名标记，"" = 不加
    calibrated: bool  # 是否经过相机配方/标定（中性转换 = False）
    display: Callable[[np.ndarray], np.ndarray]
    description: str = ""  # 写进导出文件 EXIF ImageDescription 的话（只能是 ASCII）；空 = 用默认说明


NEUTRAL = Renderer(
    id="neutral-preview",
    label="中性转换",
    filename_tag="_neutral",
    calibrated=False,
    display=_neutral_display,
)


def render_full(dng: Dng, renderer: Renderer = NEUTRAL) -> np.ndarray:
    """全分辨率：条带式白平衡 → MHC 去马赛克 → 渲染器 → uint8 (H, W, 3)，已按 Orientation 转正。"""
    h, w = crop_shape(dng)
    out = np.empty((h, w, 3), np.uint8)
    for y0, strip in demosaic_strips(dng):
        out[y0 : y0 + strip.shape[0]] = renderer.display(strip)
    return np.ascontiguousarray(orient_array(out, dng.orientation))


def render_half(dng: Dng, renderer: Renderer = NEUTRAL) -> np.ndarray:
    """半分辨率快速路径（2×2 合并，不去马赛克）：uint8 (H/2, W/2, 3)，已转正。用于预览。"""
    out = renderer.display(half_res_codes(dng))
    return np.ascontiguousarray(orient_array(out, dng.orientation))
