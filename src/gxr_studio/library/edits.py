"""裁切的读写：sidecar 是真相，索引里只缓存一份给列表用。"""

from __future__ import annotations

from .. import crop as cropmod
from ..crop import Crop, CropError
from . import sidecar as sc
from .store import Library


class RevisionConflict(Exception):
    def __init__(self, current: int):
        super().__init__(f"sidecar 已经是第 {current} 版")
        self.current = current


def _frame(side: dict) -> tuple[int, int]:
    frame = side.get("frame") or {}
    w, h = frame.get("width"), frame.get("height")
    if not w or not h:
        raise CropError("no_frame_size", "这张照片的尺寸未知，暂时不能裁切")
    return int(w), int(h)


def describe(side: dict) -> dict:
    """sidecar -> API 里的裁切描述（没有裁切时给出"原样"）。"""
    w, h = _frame(side)
    raw = side.get("crop")
    crop = (
        Crop(raw["preset"], tuple(raw["rect"]), float(raw.get("angle_deg", 0.0)))
        if raw
        else Crop("original", (0.0, 0.0, 1.0, 1.0), 0.0)
    )
    x0, y0, bw, bh = cropmod.crop_box(crop.rect, w, h)
    return {
        **crop.to_json(),
        "active": raw is not None,
        "frame": {"width": w, "height": h, "orientation": (side.get("frame") or {}).get("orientation", 1)},
        "pixels": {"x": x0, "y": y0, "width": bw, "height": bh},
        "rev": int(side.get("rev", 0)),
    }


def read_crop(lib: Library, shot: dict) -> dict:
    with lib.lock:
        side, problem = lib.read_sidecar_or_default(shot)   # 编辑记录坏了就当没有裁切
        return {**describe(side), **({"warning": problem} if problem else {})}


def write_crop(
    lib: Library,
    shot: dict,
    *,
    preset: str | None,
    rect,
    angle_deg: float,
    flip: bool = False,
    reset: bool = False,
    expected_rev: int | None = None,
) -> dict:
    path = lib.sidecar_path_for(shot)
    with lib.lock:
        side = lib.read_sidecar_for_write(shot)
        sc.check_writable(side)
        if expected_rev is not None and int(side.get("rev", 0)) != expected_rev:
            raise RevisionConflict(int(side.get("rev", 0)))
        w, h = _frame(side)
        if reset:
            side["crop"] = None
        else:
            preset = preset or "free"
            crop = (
                cropmod.validate_crop(preset, rect, angle_deg, w, h)
                if rect is not None
                else cropmod.default_crop(preset, w, h, angle_deg, flip)
            )
            side["crop"] = crop.to_json()
        out = sc.write_atomic(path, side, tmp_dir=lib.tmp_dir)
        lib.set_edit_cache(shot["shot_id"], int(out["rev"]), out["crop"])
        return describe(out)
