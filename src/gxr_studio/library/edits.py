"""裁切的读写：sidecar 是真相，索引里只缓存一份给列表用。

`frame` 只放照片尺寸（orientation / width / height）。边框样式在 `border`。
旧文件把样式写进了 `frame`、把尺寸盖掉时，读裁切之前从照片文件把尺寸补回来。
"""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any

from .. import crop as cropmod
from ..crop import Crop, CropError
from ..finish.geometry import fitted_crop, output_size
from ..finish.spec import Geometry, parse_geometry
from ..meta import read_dng_meta, read_jpeg_meta
from . import sidecar as sc
from .store import Library

_BORDER_KEYS = ("style", "color", "weight", "show_logo", "show_params", "show_date", "text", "canvas_ratio", "keep_ratio")


class RevisionConflict(Exception):
    def __init__(self, current: int) -> None:
        super().__init__(f"sidecar 已经是第 {current} 版")
        self.current = current


def _has_border(side: dict[Any, Any]) -> bool:
    border = side.get("border")
    return isinstance(border, dict) and "style" in border


def _photo_size(lib: Library, shot: dict[Any, Any]) -> tuple[int, int, int] | None:
    """JPG 优先，其次 DNG；文件头读不到再用索引里导入时记下的尺寸。"""
    files = shot.get("files") or {}
    for kind, reader in (("jpg", read_jpeg_meta), ("dng", read_dng_meta)):
        info = files.get(kind)
        if not isinstance(info, dict):
            continue
        rel = info.get("relpath")
        if not isinstance(rel, str):
            continue
        meta = reader(lib.abspath(rel))
        width, height = meta.width, meta.height
        if (
            meta.error
            or isinstance(width, bool)
            or isinstance(height, bool)
            or not isinstance(width, int)
            or not isinstance(height, int)
            or width <= 0
            or height <= 0
        ):
            continue
        orientation = meta.orientation if isinstance(meta.orientation, int) else 1
        return width, height, orientation
    sw, sh = shot.get("width"), shot.get("height")
    if isinstance(sw, bool) or isinstance(sh, bool) or not isinstance(sw, int) or not isinstance(sh, int) or sw <= 0 or sh <= 0:
        return None
    raw = shot.get("orientation")
    orientation = raw if isinstance(raw, int) and not isinstance(raw, bool) else 1
    return sw, sh, orientation


def heal_frame(lib: Library, shot: dict[Any, Any], side: dict[Any, Any]) -> dict[Any, Any]:
    """尺寸还在、也没有被样式盖住时原样返回。否则从照片把宽高写回 `frame`，样式迁到 `border`。"""
    frame = side.get("frame") if isinstance(side.get("frame"), dict) else None
    width_raw = frame.get("width") if frame is not None else None
    height_raw = frame.get("height") if frame is not None else None
    has_size = (
        isinstance(width_raw, int)
        and not isinstance(width_raw, bool)
        and isinstance(height_raw, int)
        and not isinstance(height_raw, bool)
        and width_raw > 0
        and height_raw > 0
    )
    has_style = frame is not None and "style" in frame
    if has_size and not has_style:
        return side
    recovered = _photo_size(lib, shot)
    if recovered is None and not has_size:
        return side
    updated = dict(side)
    if has_style and frame is not None and not _has_border(side):
        updated["border"] = {key: frame[key] for key in _BORDER_KEYS if key in frame}
    if recovered is not None:
        width, height, orientation = recovered
    elif isinstance(width_raw, int) and not isinstance(width_raw, bool) and isinstance(height_raw, int) and not isinstance(height_raw, bool):
        width, height = width_raw, height_raw
        raw_ori = frame.get("orientation") if frame is not None else None
        orientation = raw_ori if isinstance(raw_ori, int) and not isinstance(raw_ori, bool) else 1
    else:
        return side
    updated["frame"] = {"orientation": orientation, "width": width, "height": height}
    out = sc.write_atomic(lib.sidecar_path_for(shot), updated, tmp_dir=lib.tmp_dir)
    lib.set_edit_cache(shot["shot_id"], int(out["rev"]), out.get("crop"))
    return out


def _source_frame(side: dict[Any, Any]) -> tuple[int, int]:
    frame = side.get("frame") or {}
    w, h = frame.get("width"), frame.get("height")
    if isinstance(w, bool) or isinstance(h, bool) or not isinstance(w, int) or not isinstance(h, int) or w <= 0 or h <= 0:
        raise CropError("no_frame_size", "无法裁剪。这张照片的尺寸未知。")
    return w, h


def _geometry_of(side: dict[Any, Any]) -> Geometry:
    raw = side.get("geometry")
    return parse_geometry(raw) if isinstance(raw, dict) else Geometry()


def _frame(side: dict[Any, Any]) -> tuple[int, int]:
    """裁剪接口用的画布：几何变换之后的尺寸。sidecar 里的 frame 仍是原始尺寸。"""
    width, height = _source_frame(side)
    return output_size(width, height, _geometry_of(side))


def describe(side: dict[Any, Any]) -> dict[Any, Any]:
    """sidecar -> API 里的裁切描述（没有裁切时给出"原样"）。"""
    w, h = _frame(side)
    raw = side.get("crop")
    crop = Crop(raw["preset"], tuple(raw["rect"]), float(raw.get("angle_deg", 0.0))) if raw else Crop("original", (0.0, 0.0, 1.0, 1.0), 0.0)
    x0, y0, bw, bh = cropmod.crop_box(crop.rect, w, h)
    return {
        **crop.to_json(),
        "active": raw is not None,
        "frame": {"width": w, "height": h, "orientation": (side.get("frame") or {}).get("orientation", 1)},
        "pixels": {"x": x0, "y": y0, "width": bw, "height": bh},
        "orientation": "landscape" if bw >= bh else "portrait",
        "rev": int(side.get("rev", 0)),
    }


def read_crop(lib: Library, shot: dict[Any, Any]) -> dict[Any, Any]:
    with lib.lock:
        side, problem = lib.read_sidecar_or_default(shot)  # 编辑记录坏了就当没有裁切
        side = heal_frame(lib, shot, side)
        return {**describe(side), **({"warning": problem} if problem else {})}


def write_crop(
    lib: Library,
    shot: dict[Any, Any],
    *,
    preset: str | None,
    rect: Any,
    angle_deg: float | None = None,
    flip: bool = False,
    orientation: str | None = None,
    reset: bool = False,
    expected_rev: int | None = None,
    fit: bool = False,
) -> dict[Any, Any]:
    """写裁切。orientation（landscape / portrait）指几何变换之后的画布上的横竖，和界面上的按钮一样：
    转过 90° 的横图是竖的画布，在上面选「横向」就是横框。给了 orientation 就不看 flip（旧版界面才用 flip）。"""
    path = lib.sidecar_path_for(shot)
    with lib.lock:
        side = lib.read_sidecar_for_write(shot)
        sc.check_writable(side)
        if expected_rev is not None and int(side.get("rev", 0)) != expected_rev:
            raise RevisionConflict(int(side.get("rev", 0)))
        side = heal_frame(lib, shot, side)
        base_rev = int(side.get("rev", 0))
        side = copy.deepcopy(side)
    w, h = _frame(side)
    if orientation in ("landscape", "portrait"):
        flip = (orientation == "landscape") != (w >= h)
    if angle_deg is None:
        raw_crop = side.get("crop")
        stored = raw_crop.get("angle_deg", 0.0) if isinstance(raw_crop, dict) else 0.0
        angle_deg = float(stored) if isinstance(stored, (int, float)) and not isinstance(stored, bool) else 0.0
    if reset:
        side["crop"] = None
        if isinstance(side.get("geometry"), dict):
            side["geometry"]["straighten"] = 0.0
    else:
        preset = preset or "free"
        geom_now = _geometry_of(side)
        forced = False
        if rect is None:
            crop = cropmod.default_crop(preset, w, h, angle_deg, flip)
        else:
            try:
                # 透视、畸变拉开过的画布：画布不是一块拉直后的矩形，`box_inside` 的角点检查对不上真正的有效区。
                # 这时不在这里按角度拒，下面的收框（fitted_crop）按真正的有效蒙版把框收进去
                crop = cropmod.validate_crop(preset, rect, angle_deg, w, h, mirrored=geom_now.flip, check_region=not geom_now.expands())
            except CropError:
                if not fit:
                    raise
                # fit：普通写入会被拒的框，才当目标收成离它最近的合格框；普通写入收得下的，结果和普通写入一模一样
                crop = cropmod.fit_target_crop(preset, rect, angle_deg, w, h)
                forced = True
        geom = replace(geom_now, straighten=float(angle_deg))
        src_w, src_h = _source_frame(side)
        fixed = fitted_crop(geom, crop.rect, preset=crop.preset, width=src_w, height=src_h, force=forced)
        if fixed is not None:
            crop = Crop(crop.preset, fixed, crop.angle_deg)
        cropmod.check_final(crop.preset, crop.rect, w, h)  # 每条写入路径收完都查：太小、比例差太多的框不存（下一次写入就会被拒）
        side["crop"] = crop.to_json()
        geo = dict(side.get("geometry") or {})
        geo["straighten"] = crop.angle_deg
        side["geometry"] = geo
    with lib.lock:
        current = lib.read_sidecar_for_write(shot)
        if int(current.get("rev", 0)) != base_rev:
            raise RevisionConflict(int(current.get("rev", 0)))
        out = sc.write_atomic(path, side, tmp_dir=lib.tmp_dir)
        lib.set_edit_cache(shot["shot_id"], int(out["rev"]), out["crop"])
        return describe(out)
