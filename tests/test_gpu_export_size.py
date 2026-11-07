"""透视 / 畸变的成片在导出时的像素：浏览器画的边框和时间戳按它排版，必须和导出的照片一样大。

浏览器取这个大小的办法：在全尺寸画布上问 `/gpu/box`（`final_box`），用它的宽高（见 web/assets/gpu_warp.js 的 `exportSize`）。
这里在全尺寸（2848×4288）上把 `final_box` 的宽高和 `finish_array` 真正导出的照片宽高逐个对：不旋转、旋转 90°、
带拉直的 180° 加翻转的极端值，各配「没有裁切」和固定比例。

旧办法（颜色层上的裁切框按「全尺寸画布 ÷ 颜色层画布」放大）在旋转的透视上会差 20–30 像素：这里也算出来打印，并要求至少一个旋转用例里确实不同，
证明这个测试分得出两种办法。
"""

from __future__ import annotations

import numpy as np
import pytest

from gxr_studio.finish.compose import finish_array
from gxr_studio.finish.geometry import output_size
from gxr_studio.finish.spec import factory_frame, factory_stamp, parse_geometry
from gxr_studio.gpu.box import final_box
from gxr_studio.render.params import EditState

FULL_W, FULL_H = 2848, 4288  # 全尺寸（已转正）
LAYER_W, LAYER_H = 1424, 2144  # 颜色层（半分辨率）

GEOMETRIES = {
    "plain": {"vertical": 30, "distortion": -20},
    "rotated_90": {"horizontal": -30, "distortion": 30, "rotate90": 1},
    "extreme": {"vertical": 100, "horizontal": -100, "distortion": 100, "straighten": -4.0, "rotate90": 2},
}
CROPS = {"none": None, "3:2": "3:2"}

_estimates: dict[str, tuple[int, int]] = {}


def _estimate(geometry: dict, preset: str | None) -> tuple[int, int]:
    """旧办法：颜色层上的框，乘全尺寸画布与颜色层画布之比。"""
    geom = parse_geometry(geometry)
    box = final_box(LAYER_W, LAYER_H, geom, (0.0, 0.0, 1.0, 1.0) if preset else None, preset)[0]
    fw, fh = output_size(FULL_W, FULL_H, geom)
    lw, lh = output_size(LAYER_W, LAYER_H, geom)
    return round(box[2] * fw / lw), round(box[3] * fh / lh)


@pytest.mark.parametrize("gid", list(GEOMETRIES))
@pytest.mark.parametrize("crop", list(CROPS))
def test_full_size_box_is_the_exported_photo_size(gid, crop):
    geometry, preset = GEOMETRIES[gid], CROPS[crop]
    geom = parse_geometry(geometry)
    rect = (0.0, 0.0, 1.0, 1.0) if preset else None
    rgb = np.zeros((FULL_H, FULL_W, 3), np.uint8)
    out, _valid = finish_array(
        rgb,
        EditState(geometry=geom),
        stage="final",
        frame=factory_frame(),
        stamp=factory_stamp(),
        crop_rect=rect,
        meta={},
        taken=None,
        file_time=None,
        from_raw=True,
        crop_preset=preset,
    )
    (_x, _y, w, h), _valid_rect = final_box(FULL_W, FULL_H, geom, rect, preset)
    assert (w, h) == (out.shape[1], out.shape[0])
    old = _estimate(geometry, preset)
    _estimates[f"{gid}/{crop}"] = (old[0] - w, old[1] - h)
    print(f"{gid}/{crop}: export {w}x{h}, old estimate off by {old[0] - w}, {old[1] - h}")


def test_the_old_estimate_missed_on_rotated_perspective():
    """上面的用例跑完以后：旧办法在旋转的透视上确实不对（20–30 像素），所以这个测试分得出两种办法。"""
    if len(_estimates) < len(GEOMETRIES) * len(CROPS):
        pytest.skip("要和上面的用例一起跑")
    assert max(abs(v) for k, d in _estimates.items() if k.startswith(("rotated_90", "extreme")) for v in d) >= 5, _estimates
