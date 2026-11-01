"""把颜色图做成未裁切画面或成片。"""

from __future__ import annotations

import io
from datetime import datetime
from typing import Any

import numpy as np
from PIL import Image, ImageOps

from ..export import srgb_icc
from ..render.params import EditState
from .frame import apply_frame
from .geometry import clamp_norm_rect, crop_valid, fit_preset_in_valid, header_value, needs_valid_crop, normalized_valid_rect, warp
from .hdr_export import HdrTrack
from .pixels import apply_base, apply_extra
from .spec import FrameSpec, Geometry, StampSpec, factory_frame, factory_stamp
from .stamp import apply_stamp


def resolved_frame(edit_frame: FrameSpec | None, defaults: FrameSpec | None) -> FrameSpec:
    return edit_frame if edit_frame is not None else (defaults or factory_frame())


def resolved_stamp(edit_stamp: StampSpec | None, defaults: StampSpec | None) -> StampSpec:
    return edit_stamp if edit_stamp is not None else (defaults or factory_stamp())


def has_visible_edit(edit: EditState, side: dict[str, Any], frame: FrameSpec, stamp: StampSpec) -> bool:
    if edit.recipe is not None or edit.master is not None or not edit.adjust.is_identity:
        return True
    geom = edit.geometry
    if geom is not None and not geom.is_identity():
        return True
    if side.get("crop"):
        return True
    if frame.style != "none" or stamp.enabled:
        return True
    return False


def encode_jpeg(rgb: np.ndarray, quality: int) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(rgb), "RGB").save(buf, format="JPEG", quality=quality, icc_profile=srgb_icc())
    return buf.getvalue()


def load_jpeg(path: Any, max_edge: int | None) -> np.ndarray:
    with Image.open(path) as im:
        rgb = ImageOps.exif_transpose(im).convert("RGB")
    if max_edge and max(rgb.size) > max_edge:
        rgb.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    return np.asarray(rgb)


def finish_array(
    rgb: np.ndarray,
    edit: EditState,
    *,
    stage: str,
    frame: FrameSpec,
    stamp: StampSpec,
    crop_rect: tuple[float, float, float, float] | None,
    meta: dict[str, str],
    taken: datetime | None,
    file_time: datetime | None,
    from_raw: bool,
    crop_preset: str | None = None,
    track: HdrTrack | None = None,
) -> tuple[np.ndarray, str | None]:
    """`track`（导出 HDR 时）：RAW 亮度层跟着做同样的几何、裁剪，日期戳和边框处记为不是照片（见 `hdr_export`）。"""
    track = track or HdrTrack(None)
    if from_raw:
        rgb = apply_extra(rgb, edit.adjust)
    else:
        rgb = apply_base(rgb, edit.adjust, edit.master)
        rgb = apply_extra(rgb, edit.adjust)
    geom = edit.geometry if isinstance(edit.geometry, Geometry) else Geometry()
    if geom.is_identity():
        valid = np.ones(rgb.shape[:2], dtype=bool)
    else:
        rgb, valid = warp(rgb, geom)
        track.warp(geom)
    valid_header = header_value(normalized_valid_rect(valid))
    if stage == "uncropped" or stage == "original":
        return rgb, valid_header
    full = (0.0, 0.0, 1.0, 1.0)
    fitted = fit_preset_in_valid(valid, crop_preset, crop_rect or full) if needs_valid_crop(geom) else None
    if fitted is not None:
        x, y, bw, bh = fitted
        rgb = rgb[y : y + bh, x : x + bw]
        track.box(x, y, bw, bh)
    elif needs_valid_crop(geom):
        if crop_rect is None or crop_rect == full:
            crop_rect = normalized_valid_rect(valid)
        else:
            crop_rect = clamp_norm_rect(valid, crop_rect)
        rgb = crop_valid(rgb, crop_rect)
        track.crop(crop_rect)
    elif crop_rect is not None and crop_rect != full:
        rgb = crop_valid(rgb, crop_rect)
        track.crop(crop_rect)
    rgb = apply_stamp(rgb, stamp, taken, file_time)
    track.stamp(stamp, taken, file_time)
    rgb = apply_frame(rgb, frame, meta)
    track.frame(frame, meta)
    return rgb, None
