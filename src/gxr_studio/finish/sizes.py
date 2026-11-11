"""导出前的像素尺寸：只读文件头和裁剪蒙版，不渲染照片。"""

from __future__ import annotations

import os
from typing import Any

from PIL import Image

from ..gpu.box import final_box
from ..library.media import open_media, read_all
from ..library.store import Library
from ..lut.spaces import DISPLAY
from ..raw import open_dng
from ..raw.dng import RawDecodeError
from ..render.params import EditState
from ..render.service import RenderService, RenderUnavailable
from .compose import resolved_frame
from .fonts import FontMetrics
from .layout import crop_ratio, fit_size, frame_layout
from .meta_text import photo_meta
from .serve import _embedded_rgb, load_defaults
from .spec import OUTPUT_RATIOS, Geometry

TIERS = {"original": None, "large": 3000, "medium": 2048, "small": 1600}


def _jpeg_size(lib: Library, shot: dict[str, Any]) -> tuple[int, int]:
    with os.fdopen(open_media(lib, shot["files"]["jpg"]["relpath"]), "rb") as source, Image.open(source) as image:
        w, h = image.size
        return (h, w) if image.getexif().get(274, 1) in (5, 6, 7, 8) else (w, h)


def source_size(render: RenderService, lib: Library, shot: dict[str, Any], edit: EditState) -> tuple[int, int]:
    """和 color_image(base=auto) 一样选相机 JPG、全尺寸 RAW 或内嵌预览。"""
    files = shot["files"]
    log_lut = edit.lut is not None and render.luts.target(edit.lut)[0].input_space not in DISPLAY
    raw = "dng" in files and ("jpg" not in files or edit.recipe is not None or log_lut)
    if not raw:
        return _jpeg_size(lib, shot)
    try:
        if edit.recipe is not None and not log_lut:
            render.plan(shot, edit)
        with os.fdopen(open_media(lib, files["dng"]["relpath"]), "rb") as source:
            try:
                dng = open_dng(read_all(source.fileno()))
            except RawDecodeError:
                image = _embedded_rgb(lib.abspath(files["dng"]["relpath"]))
                if image is None:
                    raise
                return image.shape[1], image.shape[0]
        try:
            _y, _x, h, w = dng.crop_window()
            return (int(h), int(w)) if dng.orientation in (5, 6, 7, 8) else (int(w), int(h))
        finally:
            dng.release()
    except (RawDecodeError, RenderUnavailable):
        if "jpg" not in files or log_lut or (edit.recipe is not None and edit.recipe.kind == "local"):
            raise
        return _jpeg_size(lib, shot)


def output_sizes(
    render: RenderService,
    lib: Library,
    shot: dict[str, Any],
    edit: EditState,
    rect: tuple[float, float, float, float] | None,
    preset: str | None,
) -> dict[str, Any]:
    width, height = source_size(render, lib, shot, edit)
    geom = edit.geometry if isinstance(edit.geometry, Geometry) else Geometry()
    (_x, _y, w, h), _valid = final_box(width, height, geom, rect, preset)
    frame = resolved_frame(edit.frame, load_defaults(render.home)[0])
    meta = photo_meta(lib, shot)[0]
    layout = frame_layout(w, h, frame, meta, FontMetrics())
    out_w, out_h = (layout.width, layout.height) if layout is not None else (w, h)
    ratio = crop_ratio(preset, w, h) if frame.style == "none" else OUTPUT_RATIOS.get(frame.output_ratio)
    return {
        "photo": [w, h],
        "frame": frame.to_json(),
        "meta": meta,
        "crop_preset": preset,
        "ratio": list(ratio) if ratio else None,
        "sizes": {tier: list(fit_size(out_w, out_h, edge)) for tier, edge in TIERS.items()},
    }
