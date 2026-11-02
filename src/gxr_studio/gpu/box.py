"""成片的裁切框：与 `finish.compose.finish_array` 对同一尺寸的画面裁出来的位置逐像素相同，但不生成图像。

显卡只按这个框取样；有效区和比例收框的规则仍由 Python 决定（只算蒙版，不算 RGB）。
"""

from __future__ import annotations

import numpy as np

from ..finish.geometry import (
    clamp_norm_rect,
    crop_box,
    fit_preset_in_valid,
    needs_valid_crop,
    normalized_valid_rect,
    output_size,
    warp_valid,
)
from ..finish.spec import Geometry

FULL = (0.0, 0.0, 1.0, 1.0)


def final_box(
    width: int,
    height: int,
    geom: Geometry,
    crop_rect: tuple[float, float, float, float] | None,
    crop_preset: str | None,
) -> tuple[tuple[int, int, int, int], tuple[float, float, float, float]]:
    """(裁切框像素 x, y, w, h；有效区的归一化矩形)。width/height 是颜色层（未拉直）的尺寸。"""
    if geom.is_identity():
        valid = np.ones((height, width), dtype=bool)
    else:
        valid = warp_valid(width, height, geom)
    vh, vw = valid.shape
    valid_rect = normalized_valid_rect(valid)
    fitted = fit_preset_in_valid(valid, crop_preset, crop_rect or FULL) if needs_valid_crop(geom) else None
    if fitted is not None:
        x, y, bw, bh = fitted
        return (int(x), int(y), int(bw), int(bh)), valid_rect
    if needs_valid_crop(geom):
        rect = valid_rect if crop_rect is None or crop_rect == FULL else clamp_norm_rect(valid, crop_rect)
        return crop_box(vw, vh, rect), valid_rect
    if crop_rect is not None and crop_rect != FULL:
        return crop_box(vw, vh, crop_rect), valid_rect
    ow, oh = output_size(width, height, geom)
    return (0, 0, ow, oh), valid_rect
