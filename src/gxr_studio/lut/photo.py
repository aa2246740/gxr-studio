"""原 LUT 进入既有成片管线，预览和导出共用颜色步骤。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import numpy as np

from ..raw import NEUTRAL, Renderer, open_dng, render_full, render_half
from ..render.params import Adjust, EditError, EditState
from ..render.service import RenderUnavailable
from .errors import LutError
from .scene import SceneError, scene_colour
from .spaces import DISPLAY


def color_image(
    render: Any, lib: Any, shot: dict[str, Any], edit: EditState, *, max_edge: int | None, quality: int, base: str, full: bool
) -> tuple[np.ndarray, bool, dict[str, str]]:
    from ..finish.serve import _fit
    from ..finish.serve import color_image as base_image

    try:
        target, item = render.luts.target(edit.lut)
        if target.input_space in DISPLAY:
            stripped = replace(edit, lut=None, adjust=Adjust(), master=None)
            try:
                rgb, _from_raw, _labels = base_image(render, shot, stripped, max_edge=max_edge, quality=quality, lib=lib, base=base, full=full)
            except RenderUnavailable:
                if edit.recipe is not None:
                    raise
                with_dng = open_dng(lib.abspath(shot["files"]["dng"]["relpath"]))
                try:
                    rgb = render_full(with_dng) if full else render_half(with_dng)
                finally:
                    with_dng.release()
            rgb = _apply(target, rgb)
        else:
            if "dng" not in shot["files"] or render.photo_unit(shot) != "jlaunch":
                raise EditError("lut_needs_raw", "这个 LUT 需要 A12 50mm 的 RAW 文件")
            dng = open_dng(lib.abspath(shot["files"]["dng"]["relpath"]))
            try:
                colour = scene_colour(dng)

                def display(codes: np.ndarray) -> np.ndarray:
                    return _apply(target, NEUTRAL.display(codes), colour.xyz(codes))

                renderer = Renderer("lut-prediction", "电脑预测", "", False, display)
                rgb = render_full(dng, renderer) if full else render_half(dng, renderer)
            finally:
                dng.release()
    except LutError as exc:
        raise EditError(exc.code, exc.message) from exc
    except SceneError as exc:
        raise EditError(exc.code, "无法换算输入色彩空间。请检查 RAW 文件。") from exc
    # 所有照片调整均在 LUT 后执行，交给 finish_array 完成。
    return (
        _fit(rgb, None if full else max_edge),
        False,
        {
            "kind": "prediction",
            "label": "prediction",
            "text": "电脑预测",
            "id": "lut-prediction",
            "tag": "",
            "description": f"GXR Studio computer prediction: LUT {edit.lut}, {item['input_space']} to {item['output_space']}",
            "detail": "",
            "key": "",
            "cache": "miss",
            "calibrated": "0",
        },
    )


def _apply(target: Any, rgb: np.ndarray, xyz: np.ndarray | None = None) -> np.ndarray:
    shape = rgb.shape
    flat = rgb.reshape(-1, 3)
    scene = None if xyz is None else xyz.reshape(-1, 3)
    result = np.empty(flat.shape, np.uint8)
    # 整张照片也限制中间数组大小，和全尺寸 RAW 的条带接口兼容。
    for start in range(0, len(flat), 65536):
        end = start + 65536
        out = target.render(flat[start:end], None if scene is None else scene[start:end])
        if not np.isfinite(out).all():
            raise EditError("lut_nonfinite", "无法套用 LUT。请检查输入色彩空间。")
        result[start:end] = np.floor(np.clip(out, 0, 255) + 0.5).astype(np.uint8)
    return cast(np.ndarray, result.reshape(shape))
