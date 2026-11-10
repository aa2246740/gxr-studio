"""sidecar 里配方、旋钮、调整和曲线的读写（裁切的读写在 `library/edits.py`）。

- 相机侧（会跟着配方进相机）：`recipe`（底子 + 旋钮档位）；
- 电脑侧（只在电脑里生效）：`adjust`（曝光、色温色调、画面对比/高光/阴影）和 `curve`；
- 其他键（`crop`、`import`…）原样保留；写入走 sidecar 的原子写，`rev` 加一，带 `expected_rev` 时做乐观并发检查。
"""

from __future__ import annotations

import copy
from typing import Any

from ..finish.geometry import fitted_crop, output_size
from ..finish.spec import Geometry, parse_frame, parse_geometry, parse_stamp
from ..library import sidecar as sc
from ..library.edits import RevisionConflict, clean_border, heal_frame
from ..library.store import Library
from .camera_recipe import photo_edit
from .params import EditError, EditState, parse_adjust, parse_curve, parse_edit, parse_lut, parse_recipe
from .recipes import Menu, unit_of_lens

_UNSET = object()
SCOPE = {"camera": ["recipe"], "computer_only": ["adjust", "curve", "crop", "lut"]}


def read(lib: Library, shot: dict[Any, Any], menu: Menu | None = None) -> tuple[EditState, dict[Any, Any]]:
    with lib.lock:
        side, _ = lib.read_sidecar_or_default(shot)  # 编辑记录坏了/不见了就当没有编辑
        side = heal_frame(lib, shot, side)
    return (photo_edit(side, shot, menu) if menu is not None else parse_edit(side)), side


def describe(side: dict[Any, Any], edit: EditState) -> dict[Any, Any]:
    recipe = side.get("recipe")
    return {
        "rev": int(side.get("rev", 0)),
        "recipe": edit.recipe.to_json() if edit.recipe else None,
        "recipe_from_camera": "recipe" not in side and edit.recipe is not None,
        "lut": edit.lut,
        "memory": (recipe or {}).get("memory") or {},
        "adjust": edit.adjust.to_json(),
        "curve": edit.curve.to_json() if edit.curve else None,
        "geometry": edit.geometry.to_json() if getattr(edit.geometry, "to_json", None) else None,
        "frame": edit.frame.to_json() if getattr(edit.frame, "to_json", None) else None,
        "stamp": edit.stamp.to_json() if getattr(edit.stamp, "to_json", None) else None,
        "scope": SCOPE,
    }


def _memory(raw: Any) -> dict[Any, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise EditError("bad_recipe", "请求无效。请刷新页面后重试。", "memory 要是对象")
    out = {}
    for key, levels in raw.items():
        if (
            not isinstance(key, str)
            or "/" not in key
            or not isinstance(levels, dict)
            or any(not isinstance(k, str) or isinstance(v, bool) or not isinstance(v, int) for k, v in levels.items())
        ):
            raise EditError("bad_recipe", "请求无效。请刷新页面后重试。", "memory 格式不对")
        out[key] = dict(levels)
    return out


def write(
    lib: Library, shot: dict[Any, Any], changes: dict[Any, Any], menu: Menu, *, expected_rev: int | None = None
) -> tuple[EditState, dict[Any, Any]]:
    """只改请求中出现的键。None 清空对应块；recipe:null 显式关闭，不再沿用相机配方。"""
    path = lib.sidecar_path_for(shot)
    with lib.lock:
        side = lib.read_sidecar_for_write(shot)
        sc.check_writable(side)
        if expected_rev is not None and int(side.get("rev", 0)) != expected_rev:
            raise RevisionConflict(int(side.get("rev", 0)))
        side = heal_frame(lib, shot, side)
        base_rev = int(side.get("rev", 0))
        new = copy.deepcopy(side)
    if "recipe" in changes:
        raw = changes["recipe"]
        sel = parse_recipe(raw)
        if sel is None:
            new["recipe"] = None  # 显式关闭；缺键才表示沿用拍摄配方。
        else:
            photo_unit = unit_of_lens(shot.get("lens"))
            if photo_unit is not None and sel.unit != photo_unit:
                raise EditError("unit_mismatch", "无法使用这个配方。请选择同一镜头单元的配方。", photo_unit)
            menu.request_for(sel)  # 档位不合法（含到顶标记）就拒绝，不钳位
            memory = _memory(raw.get("memory")) if "memory" in raw else (side.get("recipe") or {}).get("memory") or {}
            new["recipe"] = {**sel.to_json(), **({"memory": memory} if memory else {})}
    if "adjust" in changes:
        adjust = parse_adjust(changes["adjust"])
        if adjust.is_identity:
            new.pop("adjust", None)
        else:
            new["adjust"] = adjust.to_json()
    if "curve" in changes:
        curve = parse_curve(changes["curve"])
        if curve is None:
            new.pop("curve", None)
        else:
            new["curve"] = curve.to_json()
    if "lut" in changes:
        lut = parse_lut(changes["lut"])
        if lut is None:
            new.pop("lut", None)
        else:
            new["lut"] = lut
    if "geometry" in changes:
        raw_geo = changes["geometry"]
        old_raw = side.get("geometry")
        old_geom = parse_geometry(old_raw) if isinstance(old_raw, dict) else Geometry()
        geom = parse_geometry(raw_geo) if raw_geo is not None else Geometry()
        if raw_geo is None:
            new.pop("geometry", None)
        else:
            new["geometry"] = geom.to_json()
        crop = new.get("crop") if isinstance(new.get("crop"), dict) else None
        stored = None
        if crop is not None and isinstance(crop.get("rect"), list) and len(crop["rect"]) == 4:
            stored = (float(crop["rect"][0]), float(crop["rect"][1]), float(crop["rect"][2]), float(crop["rect"][3]))
        preset_name = crop.get("preset") if isinstance(crop, dict) else None
        raw_frame = new.get("frame")
        frame = raw_frame if isinstance(raw_frame, dict) else {}
        fw, fh = frame.get("width"), frame.get("height")
        src_w = fw if isinstance(fw, int) and not isinstance(fw, bool) and fw > 0 else None
        src_h = fh if isinstance(fh, int) and not isinstance(fh, bool) and fh > 0 else None
        # 画布尺寸变了（透视、畸变拉开或收回；不算转 90°，转的框由界面自己转）：框是按旧画布的比例存的，
        # 不重收的话，退回没有透视时框的比例就不对了（存下来的框通不过 validate_crop）
        resized = bool(
            src_w
            and src_h
            and old_geom.rotate90 % 2 == geom.rotate90 % 2
            and output_size(src_w, src_h, old_geom) != output_size(src_w, src_h, geom)
        )
        fixed = fitted_crop(
            geom,
            stored,
            preset=preset_name if isinstance(preset_name, str) else None,
            width=src_w,
            height=src_h,
            force=resized and stored is not None,
        )
        if fixed is not None:
            new["crop"] = {
                "preset": (crop or {}).get("preset") or "free",
                "rect": [round(float(v), 6) for v in fixed],
                "angle_deg": geom.straighten,
            }
        elif isinstance(crop, dict):
            crop["angle_deg"] = geom.straighten
        elif abs(geom.straighten) > 1e-9:
            new["crop"] = {"preset": "free", "rect": [0.0, 0.0, 1.0, 1.0], "angle_deg": geom.straighten}
    if "frame" in changes:
        # 请求体的键仍叫 frame。sidecar 里尺寸占用了 frame，样式写到 border，不能整块盖掉。
        if changes["frame"] is None:
            new.pop("border", None)
        else:
            parsed_frame = parse_frame(changes["frame"])
            new["border"] = parsed_frame.to_json() if parsed_frame is not None else {"style": "none"}
    if "stamp" in changes:
        if changes["stamp"] is None:
            new.pop("stamp", None)
        else:
            parsed_stamp = parse_stamp(changes["stamp"])
            new["stamp"] = parsed_stamp.to_json() if parsed_stamp is not None else {"enabled": False}
    with lib.lock:
        current = lib.read_sidecar_for_write(shot)
        if int(current.get("rev", 0)) != base_rev:
            raise RevisionConflict(int(current.get("rev", 0)))
        out = sc.write_atomic(path, clean_border(new), tmp_dir=lib.tmp_dir)
        lib.set_edit_cache(shot["shot_id"], int(out["rev"]), out.get("crop"))
        return photo_edit(out, shot, menu), out
