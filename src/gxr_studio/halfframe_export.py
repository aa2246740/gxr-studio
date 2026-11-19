"""g250 半格一对的多文件导出（diptych / singles / both）与路由/成片公共助手。"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

import numpy as np
from PIL import Image
from pydantic import BaseModel

from . import halfframe_pairs as hfp
from .export import FORMATS, ExportError, ExportOptions, ExportResult, check_destination, claim_path, write_rgb_with_exif
from .halfframe_compose import (
    camera_jpg_singles,
    compose_strips,
    orient_half,
    pair_half_shots,
    pair_orientation_and_half,
    render_pair_lut_scene_strips,
    render_pair_strips,
)
from .raw import RawDecodeError
from .render.params import Adjust, EditError, EditState
from .render.service import NoRaw, RenderUnavailable

NO_RAW_MSG = "需要 RAW 文件。拍摄时将画质设为 RAW+JPEG。"


class ExportRecipeBinding(BaseModel):
    record_sha256: str
    entry_sha256: str
    binding_sha256: str
    selection_id: str
    quality_state: Literal["passed", "rejected", "needs_evidence", "historical_exception", "unattached"]
    assessed_entry: bool


class ExportFileItem(BaseModel):
    """半格一对多文件导出（singles / both）时的单个输出文件条目。"""

    id: str
    role: Literal["diptych", "first", "second"]
    path: str
    width: int
    height: int
    bytes: int
    format: str
    render: str
    render_label: Literal["prediction", "approx", "original"]
    render_text: str
    prediction: bool
    calibrated: bool | None = None
    detail: str | None = None
    hdr: bool | None = None
    hdr_note: str | None = None
    recipe_binding: ExportRecipeBinding | None = None


@dataclass(frozen=True)
class PairExportParts:
    diptych: np.ndarray
    single1: np.ndarray
    single2: np.ndarray
    from_raw: bool
    labels: dict[str, str]
    first_labels: dict[str, str]
    second_labels: dict[str, str]


def _camera_jpg_parts(lib: Any, shot: dict[Any, Any], detail: str = "") -> PairExportParts:
    from .finish.compose import load_jpeg

    ori, first_half = pair_orientation_and_half(shot)
    s1, s2 = pair_half_shots(shot)
    single1, single2 = camera_jpg_singles(
        lib.abspath(s1["files"]["jpg"]["relpath"]),
        lib.abspath(s2["files"]["jpg"]["relpath"]),
        first_half=first_half,
        orientation=ori,
    )
    diptych = load_jpeg(lib.abspath(shot["files"]["jpg"]["relpath"]), None)
    labels = {
        "kind": "original",
        "label": "original",
        "text": "原图",
        "id": "camera-jpg",
        "tag": "",
        "description": "原图",
        "detail": detail,
        "calibrated": "0",
        "cache": "miss",
        "key": "",
    }
    return PairExportParts(diptych, single1, single2, False, labels, dict(labels), dict(labels))


def render_pair_export_parts(
    render: Any,
    lib: Any,
    shot: dict[Any, Any],
    edit: EditState,
    *,
    base: str = "auto",
) -> PairExportParts:
    """一次去马赛克同时产出双联与两张单格底图（均已转正），以及双联/各半各自对应的标注。"""
    from .lut.errors import LutError
    from .lut.photo import _apply as lut_apply
    from .lut.scene import SceneError
    from .lut.spaces import DISPLAY

    pair_raw = hfp.has_pair_raw(shot)
    if edit.lut is not None:
        try:
            target, item = render.luts.target(edit.lut)
            if target.input_space in DISPLAY:
                stripped = replace(edit, lut=None, adjust=Adjust(), curve=None)
                try:
                    parts = render_pair_export_parts(render, lib, shot, stripped, base=base)
                except RenderUnavailable:
                    if edit.recipe is not None:
                        raise
                    if pair_raw:
                        parts = render_pair_export_parts(render, lib, shot, EditState(), base="neutral")
                    else:
                        parts = _camera_jpg_parts(lib, shot)
                dip = lut_apply(target, parts.diptych)
                s1_rgb = lut_apply(target, parts.single1)
                s2_rgb = lut_apply(target, parts.single2)
            else:
                if not pair_raw:
                    raise NoRaw("这个 LUT 需要 RAW 文件")
                strip1, strip2, seam_w, first_half, ori = render_pair_lut_scene_strips(lib, shot, target, lut_apply, full=True)
                dip = compose_strips(strip1, strip2, first_half=first_half, seam_w=seam_w, orientation=ori)
                s1_rgb = orient_half(strip1, ori)
                s2_rgb = orient_half(strip2, ori)
        except LutError as exc:
            raise EditError(exc.code, exc.message) from exc
        except SceneError as exc:
            raise EditError(exc.code, "无法换算输入色彩空间。请检查 RAW 文件。") from exc
        lut_labels = {
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
        }
        return PairExportParts(dip, s1_rgb, s2_rgb, False, lut_labels, dict(lut_labels), dict(lut_labels))
    if base == "camera-jpg":
        return _camera_jpg_parts(lib, shot)
    if not pair_raw:
        if base in ("neutral", "recipe") or edit.recipe is not None:
            raise NoRaw(NO_RAW_MSG)
        return _camera_jpg_parts(lib, shot)
    if base == "neutral":
        strip1, strip2, seam_w, first_half, ori, labels, first_labels, second_labels = render_pair_strips(
            render, lib, shot, EditState(), full=True, neutral=True
        )
        return PairExportParts(
            compose_strips(strip1, strip2, first_half=first_half, seam_w=seam_w, orientation=ori),
            orient_half(strip1, ori),
            orient_half(strip2, ori),
            False,
            labels,
            first_labels,
            second_labels,
        )
    try:
        strip1, strip2, seam_w, first_half, ori, labels, first_labels, second_labels = render_pair_strips(
            render, lib, shot, edit, full=True, neutral=False
        )
    except (RenderUnavailable, RawDecodeError) as exc:
        if edit.recipe is not None and edit.recipe.kind == "local":
            raise
        if "jpg" not in shot.get("files", {}):
            raise
        return _camera_jpg_parts(lib, shot, detail=f"没有全尺寸传感器数据。已改用相机 JPG。{exc}")
    return PairExportParts(
        compose_strips(strip1, strip2, first_half=first_half, seam_w=seam_w, orientation=ori),
        orient_half(strip1, ori),
        orient_half(strip2, ori),
        True,
        labels,
        first_labels,
        second_labels,
    )


def _write_export_file(
    rgb: np.ndarray,
    *,
    source_path: Path,
    dest: Path,
    stem: str,
    ext: str,
    tag: str,
    note: str,
    quality: int,
    long_edge: int | None,
    fmt: str,
    labels: dict[str, str],
    hdr_refused: bool,
) -> ExportResult:
    from .finish.serve import _fit

    fitted = _fit(rgb, long_edge)
    payload = write_rgb_with_exif(fitted, source_path, note, quality=quality, fmt=fmt)
    try:
        path, fd = claim_path(dest, stem, ext, tag)
    except OSError as exc:
        raise ExportError("dest_unwritable", "无法导出到这个位置。请换一个文件夹。", exc.strerror or "") from exc
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    with Image.open(path) as saved:
        width, height = saved.size
    kind = labels.get("label") or labels.get("kind") or "original"
    ident = labels.get("id") or ("camera-jpg" if kind == "original" else "recipe-prediction")
    text = labels.get("text") or "原图"
    calibrated = True if labels.get("calibrated") == "1" else False if labels.get("calibrated") == "0" else None
    return ExportResult(
        path=path,
        width=width,
        height=height,
        bytes=path.stat().st_size,
        render=ident,
        render_label=text,
        format=fmt,
        render_kind=kind,
        calibrated=calibrated,
        detail=labels.get("detail") or "",
        hdr=False if hdr_refused else None,
        hdr_reason="halfframe_pair" if hdr_refused else None,
        recipe_binding={
            k: labels[k] for k in ("record_sha256", "entry_sha256", "binding_sha256", "selection_id", "quality_state", "assessed_entry")
        }
        if labels.get("selection_id")
        else None,
    )


def export_pair_finish(
    render: Any,
    lib: Any,
    shot: dict[str, Any],
    edit: EditState,
    side: dict[str, Any],
    dest_dir: Path,
    forbidden: list[Path],
    *,
    quality: int,
    long_edge: int | None,
    fmt: str,
    base: str = "auto",
    hdr: bool = False,
    mode: str = "singles",
) -> list[tuple[str, str, ExportResult]]:
    """半格一对导出（singles = 2 张单格，both = 双联 + 2 张单格），返回 [(shot_id, role, ExportResult), ...]。"""
    from .finish.compose import finish_array, resolved_frame, resolved_stamp
    from .finish.serve import _meta, _source_path, load_defaults
    from .finish.spec import factory_frame, factory_stamp

    ExportOptions(long_edge, quality, fmt).validate()
    dest = check_destination(dest_dir, [*forbidden, lib.root])
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExportError("dest_unwritable", "无法导出到这个位置。请换一个文件夹。", exc.strerror or "") from exc
    try:
        parts = render_pair_export_parts(render, lib, shot, edit, base=base)
    except RawDecodeError as exc:
        detail = f"没有全尺寸传感器数据，也没有更小的预览。{exc}"
        raise ExportError("raw_unreadable", "无法读取 RAW 文件。文件可能已损坏。", detail) from exc
    s1, s2 = pair_half_shots(shot)
    ext = FORMATS[fmt][1]
    single_edit = EditState(adjust=edit.adjust, curve=edit.curve)
    single1, single2 = (
        finish_array(
            arr,
            single_edit,
            stage="final",
            frame=factory_frame(),
            stamp=factory_stamp(),
            crop_rect=None,
            meta={},
            taken=None,
            file_time=None,
            from_raw=parts.from_raw,
        )[0]
        for arr in (parts.single1, parts.single2)
    )
    outputs: list[tuple[str, str, ExportResult]] = []
    if mode == "both":
        frame_default, stamp_default = load_defaults(render.home)
        frame = resolved_frame(edit.frame, frame_default)
        stamp = resolved_stamp(edit.stamp, stamp_default)
        meta, taken, file_time = _meta(shot, lib)
        crop_raw = side.get("crop") if isinstance(side.get("crop"), dict) else {}
        raw_rect = crop_raw.get("rect") if isinstance(crop_raw, dict) else None
        crop_rect = (
            (float(raw_rect[0]), float(raw_rect[1]), float(raw_rect[2]), float(raw_rect[3]))
            if isinstance(raw_rect, list) and len(raw_rect) == 4
            else None
        )
        preset = crop_raw.get("preset") if isinstance(crop_raw, dict) else None
        use_custom = frame.style != "none" and frame.logo not in ("", "ricoh", "none")
        dip_rgb, _ = finish_array(
            parts.diptych,
            edit,
            stage="final",
            frame=frame,
            stamp=stamp,
            crop_rect=crop_rect,
            meta=meta,
            taken=taken,
            file_time=file_time,
            from_raw=parts.from_raw,
            crop_preset=preset if isinstance(preset, str) else None,
            logos=render.logos.info_map() if use_custom else None,
            logo_images=render.logos.image_map(frame.logo) if use_custom else None,
        )
        dip_note = parts.labels.get("description") or parts.labels.get("text") or "原图"
        dip_tag = parts.labels.get("tag") or ""
        outputs.append(
            (
                str(shot["shot_id"]),
                "diptych",
                _write_export_file(
                    dip_rgb,
                    source_path=_source_path(lib, shot),
                    dest=dest,
                    stem=str(shot["name"]),
                    ext=ext,
                    tag=dip_tag,
                    note=dip_note,
                    quality=quality,
                    long_edge=long_edge,
                    fmt=fmt,
                    labels=parts.labels,
                    hdr_refused=hdr,
                ),
            )
        )
    for role, half_shot, half_rgb, half_labels in (
        ("first", s1, single1, parts.first_labels),
        ("second", s2, single2, parts.second_labels),
    ):
        half_note = half_labels.get("description") or half_labels.get("text") or "原图"
        half_tag = half_labels.get("tag") or ""
        outputs.append(
            (
                str(half_shot["shot_id"]),
                role,
                _write_export_file(
                    half_rgb,
                    source_path=_source_path(lib, half_shot),
                    dest=dest,
                    stem=str(half_shot["name"]),
                    ext=ext,
                    tag=f"{half_tag}_half",
                    note=half_note,
                    quality=quality,
                    long_edge=long_edge,
                    fmt=fmt,
                    labels=half_labels,
                    hdr_refused=hdr,
                ),
            )
        )
    return outputs


def halfframe_mode(settings: Any, forbidden_roots: list[Path], req_halfframe: str | None) -> str:
    if req_halfframe is not None:
        return req_halfframe
    from . import privacy
    from .prefs import load_prefs

    prefs = load_prefs(settings.home_dir, privacy.forbidden_for(settings, forbidden_roots))
    return str(prefs.get("export_halfframe") or "diptych")


def export_result_dict(
    shot_id: str,
    res: Any,
    *,
    hdr_notes: dict[str, str],
    hdr_unsupported: str,
    warning: str | None = None,
    role: str | None = None,
) -> dict[Any, Any]:
    return {
        "id": shot_id,
        **({"role": role} if role is not None else {}),
        "path": str(res.path),
        "width": res.width,
        "height": res.height,
        "bytes": res.bytes,
        "format": res.format,
        "render": res.render,
        "render_label": res.render_kind,
        "render_text": res.render_label,
        "prediction": res.render_kind == "prediction",
        "calibrated": res.calibrated,
        **({"recipe_binding": res.recipe_binding} if res.recipe_binding is not None else {}),
        **({"detail": res.detail} if res.detail else {}),
        **({"warning": warning} if warning else {}),
        **({"hdr": res.hdr} if res.hdr is not None else {}),
        **({"hdr_note": hdr_notes.get(res.hdr_reason or "", hdr_unsupported)} if res.hdr is False else {}),
    }


def maybe_export_pair(
    c: Any,
    shot: dict[Any, Any],
    edit: EditState,
    side: dict[str, Any],
    dest: Path,
    forbidden: list[Path],
    opts: ExportOptions,
    *,
    want: str,
    hdr: bool,
    req_halfframe: str | None,
    warning: str | None,
    hdr_notes: dict[str, str],
    hdr_unsupported: str,
) -> dict[Any, Any] | None:
    if not hfp.is_pair_second(shot):
        return None
    hf_mode = halfframe_mode(c.settings, forbidden, req_halfframe)
    if hf_mode not in ("singles", "both"):
        return None
    entries = export_pair_finish(
        c.render,
        c.lib,
        shot,
        edit,
        side,
        dest,
        forbidden,
        quality=opts.quality,
        long_edge=opts.long_edge,
        fmt=opts.fmt,
        base=want,
        hdr=hdr,
        mode=hf_mode,
    )
    items = [export_result_dict(sid, r, hdr_notes=hdr_notes, hdr_unsupported=hdr_unsupported, role=role) for sid, role, r in entries]
    primary = export_result_dict(shot["shot_id"], entries[0][2], hdr_notes=hdr_notes, hdr_unsupported=hdr_unsupported, warning=warning)
    primary["files"] = items
    return primary


def remember_export(exports: Any, export_id: str, item: dict[Any, Any]) -> dict[Any, Any]:
    files = item.get("files")
    if isinstance(files, list) and files:
        for entry in files:
            p = Path(entry["path"])
            exports.note(export_id, p.parent, p)
    else:
        path = Path(item["path"])
        exports.note(export_id, path.parent, path)
    return {**item, "export_id": export_id}


def dedupe_batch_pairs(shots: list[dict[Any, Any]]) -> tuple[list[dict[Any, Any]], int]:
    paired_first = {
        str(p["partner_id"])
        for s in shots
        for p in (s.get("pair"),)
        if isinstance(p, dict) and p.get("role") == "second" and p.get("partner_id")
    }
    if not paired_first:
        return shots, 0
    return [s for s in shots if s["shot_id"] not in paired_first], len(paired_first)
