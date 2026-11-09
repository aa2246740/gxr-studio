"""成片管线拆成「边框之前」和「加边框」两段：拼起来和原来的 `finish_array` 逐字节一样（导出的字节不变）。"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pytest

from gxr_studio.finish.compose import finish_array, finish_unframed
from gxr_studio.finish.frame import apply_frame
from gxr_studio.finish.geometry import (
    clamp_norm_rect,
    crop_valid,
    fit_preset_in_valid,
    header_value,
    needs_valid_crop,
    normalized_valid_rect,
    warp,
)
from gxr_studio.finish.pixels import apply_base, apply_extra
from gxr_studio.finish.spec import FrameSpec, Geometry, StampSpec
from gxr_studio.finish.stamp import apply_stamp
from gxr_studio.render.params import EditState

STYLES = ["none", "gallery", "leica", "hasselblad", "film", "instant"]


def _old_finish_array(rgb, edit, *, stage, frame, stamp, crop_rect, meta, taken, file_time, from_raw, crop_preset=None):
    """拆分之前的 `finish_array`（原样抄下来，没有 HDR 跟踪）。"""
    if from_raw:
        rgb = apply_extra(rgb, edit.adjust)
    else:
        rgb = apply_base(rgb, edit.adjust, edit.curve)
        rgb = apply_extra(rgb, edit.adjust)
    geom = edit.geometry if isinstance(edit.geometry, Geometry) else Geometry()
    if geom.is_identity():
        valid = np.ones(rgb.shape[:2], dtype=bool)
    else:
        rgb, valid = warp(rgb, geom)
    valid_header = header_value(normalized_valid_rect(valid))
    if stage == "uncropped" or stage == "original":
        return rgb, valid_header
    full = (0.0, 0.0, 1.0, 1.0)
    fitted = fit_preset_in_valid(valid, crop_preset, crop_rect or full) if needs_valid_crop(geom) else None
    if fitted is not None:
        x, y, bw, bh = fitted
        rgb = rgb[y : y + bh, x : x + bw]
    elif needs_valid_crop(geom):
        if crop_rect is None or crop_rect == full:
            crop_rect = normalized_valid_rect(valid)
        else:
            crop_rect = clamp_norm_rect(valid, crop_rect)
        rgb = crop_valid(rgb, crop_rect)
    elif crop_rect is not None and crop_rect != full:
        rgb = crop_valid(rgb, crop_rect)
    rgb = apply_stamp(rgb, stamp, taken, file_time)
    rgb = apply_frame(rgb, frame, meta)
    return rgb, None


def _scene():
    rng = np.random.default_rng(7)
    rgb = rng.integers(0, 256, size=(240, 360, 3), dtype=np.uint8)
    meta = {
        "lens": "GR LENS A12 50mm F2.5 MACRO",
        "aperture": "F2.5",
        "shutter": "1/125s",
        "iso": "ISO 400",
        "date": "2026.10.19",
        "frame_no": "12A",
    }
    return rgb, meta, datetime(2026, 10, 19, 10, 30), datetime(2026, 10, 19, 10, 31)


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize(
    ("geometry", "crop"),
    [(None, None), (Geometry(straighten=4.0), (0.1, 0.1, 0.7, 0.6)), (Geometry(rotate90=1, flip=True), (0.0, 0.0, 0.5, 1.0))],
)
def test_finish_array_bytes_are_unchanged_by_the_split(style, geometry, crop):
    rgb, meta, taken, file_time = _scene()
    frame = FrameSpec(style=style, text="Hello", show_date=True)
    stamp = StampSpec(enabled=True)
    edit = EditState(geometry=geometry)
    args = {"frame": frame, "stamp": stamp, "crop_rect": crop, "meta": meta, "taken": taken, "file_time": file_time, "from_raw": False}
    for stage in ("final", "uncropped"):
        got, header = finish_array(rgb.copy(), edit, stage=stage, **args)
        want, want_header = _old_finish_array(rgb.copy(), edit, stage=stage, **args)
        assert got.shape == want.shape and np.array_equal(got, want)
        assert header == want_header


@pytest.mark.parametrize("style", STYLES)
def test_unframed_part_plus_frame_is_the_whole_pipeline(style):
    rgb, meta, taken, file_time = _scene()
    frame = FrameSpec(style=style)
    stamp = StampSpec(enabled=True)
    edit = EditState(geometry=Geometry(straighten=2.0))
    kw = {"stamp": stamp, "crop_rect": (0.05, 0.05, 0.8, 0.8), "taken": taken, "file_time": file_time, "from_raw": False}
    base, _ = finish_unframed(rgb.copy(), edit, stage="final", **kw)
    whole, _ = finish_array(rgb.copy(), edit, stage="final", frame=frame, meta=meta, **kw)
    assert np.array_equal(apply_frame(base, frame, meta), whole)
