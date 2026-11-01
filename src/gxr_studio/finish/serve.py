"""把渲染服务和成片管线接在一起，给预览、缩略图和导出共用。"""

from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageOps

from ..library.store import Library
from ..raw.dng import RawDecodeError
from ..render.params import EditError, EditState
from ..render.service import NoRaw, RenderService, RenderUnavailable
from .approx import ENABLED, apply_lut
from .compose import encode_jpeg, finish_array, has_visible_edit, load_jpeg, resolved_frame, resolved_stamp
from .hdr_export import HdrTrack
from .hdr_export import finish as finish_hdr
from .spec import factory_frame, factory_stamp

SIZE_EDGE = {"thumb": 240, "preview": 1600, "full": None}
NO_RAW = "配方需要 RAW 文件。拍摄时将画质设为 RAW+JPEG。"
APPROX_LABEL = "近似"


def defaults_path(home: Path) -> Path:
    return Path(home) / "finish-defaults.json"


def load_defaults(home: Path) -> tuple[Any, Any]:
    from .spec import factory_frame, factory_stamp, parse_frame, parse_stamp

    path = defaults_path(home)
    if not path.is_file():
        return factory_frame(), factory_stamp()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return factory_frame(), factory_stamp()
    return parse_frame(raw.get("frame")) or factory_frame(), parse_stamp(raw.get("stamp")) or factory_stamp()


def save_defaults(home: Path, frame: Any, stamp: Any) -> dict[str, Any]:
    path = defaults_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"frame": frame, "stamp": stamp, "rev": int(time_ns())}
    if path.is_file():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            payload["rev"] = int(old.get("rev") or 0) + 1
        except (OSError, ValueError, TypeError):
            payload["rev"] = 1
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def defaults_token(home: Path) -> str:
    """默认装饰的版本。列表链接和图像缓存都带上它，改默认值后旧图失效。"""
    path = defaults_path(home)
    if not path.is_file():
        return "0"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return str(int(raw.get("rev") or 0))
    except (OSError, ValueError, TypeError):
        return "0"


def time_ns() -> int:
    return int(datetime.now().timestamp() * 1_000_000_000)


def _decode(jpeg: bytes) -> np.ndarray:
    """按这份 JPEG 自己的 EXIF 方向转正，再交给缩放。转正后的像素不再带方向标记。"""
    with Image.open(io.BytesIO(jpeg)) as im:
        turned = ImageOps.exif_transpose(im)
        return np.asarray((turned if turned is not None else im).convert("RGB"))


def _meta(shot: dict[str, Any], lib: Library) -> tuple[dict[str, str], datetime | None, datetime | None]:
    from .meta_text import photo_meta

    return photo_meta(lib, shot)


def _fit(rgb: np.ndarray, max_edge: int | None) -> np.ndarray:
    if not max_edge:
        return rgb
    height, width = rgb.shape[:2]
    long_edge = max(height, width)
    if long_edge <= max_edge:
        return rgb
    scale = max_edge / long_edge
    resized = Image.fromarray(rgb, "RGB").resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.LANCZOS)
    return np.asarray(resized)


def _embedded_rgb(path: Path) -> np.ndarray | None:
    """没有全尺寸 CFA 时，用 DNG 里最大的一张 JPEG 预览。普通 TIFF 主图不算。"""
    from ..meta import dng_preview_image

    image = dng_preview_image(path, embedded_only=True)
    if image is None:
        return None
    return np.asarray(image.convert("RGB"))


def _raw_rgb(render: RenderService, lib: Library, shot: dict[str, Any], edit: EditState, *, full: bool) -> tuple[np.ndarray, dict[str, str]]:
    """full 为真时按传感器全尺寸解码。没有全尺寸 CFA 时退回内嵌的最大预览，并在 detail 里写明。"""
    from ..raw import NEUTRAL, open_dng, render_full

    if not full:
        out = render.render_preview(shot, edit, max_edge=None, quality=95, crop=None)
        kind = "prediction"
        return _decode(out.jpeg), {
            "kind": kind,
            "label": kind,
            "text": out.plan.label,
            "id": "recipe-prediction" if out.plan.calibrated else "recipe-prediction-uncalibrated",
            "tag": "",
            "description": "",
            "detail": "",
            "key": out.key,
            "cache": "hit" if out.cache_hit else "miss",
            "calibrated": "1" if out.plan.calibrated else "0",
        }
    path = lib.abspath(shot["files"]["dng"]["relpath"])
    try:
        dng = open_dng(path)
    except RawDecodeError:
        preview = _embedded_rgb(path)
        if preview is None:
            raise
        height, width = preview.shape[:2]
        return preview, {
            "kind": "original",
            "label": "original",
            "text": "原图",
            "id": "neutral-preview",
            "tag": "_neutral",
            "description": "",
            "detail": f"没有全尺寸传感器数据。已改用 {width}×{height} 的预览。",
            "key": "",
            "cache": "miss",
            "calibrated": "0",
            "limited": "1",
        }
    try:
        try:
            renderer = render.renderer(shot, edit, precise=False)
        except RenderUnavailable:
            if edit.recipe is not None:
                raise
            renderer = NEUTRAL
        rgb = render_full(dng, renderer)
    finally:
        dng.release()
    kind = "original" if renderer is NEUTRAL else "prediction"
    note = renderer.description or (
        f"GXR Studio {renderer.id}: white-balanced linear sensor data to sRGB, no recipe applied" if renderer is NEUTRAL else renderer.label
    )
    return rgb, {
        "kind": kind,
        "label": kind,
        "text": renderer.label,
        "id": renderer.id,
        "tag": renderer.filename_tag,
        "description": note,
        "detail": "",
        "key": "",
        "cache": "miss",
        "calibrated": "1" if renderer.calibrated else "0",
    }


def color_image(
    render: RenderService,
    shot: dict[str, Any],
    edit: EditState,
    *,
    max_edge: int | None,
    quality: int,
    lib: Library,
    base: str = "auto",
    full: bool = False,
) -> tuple[np.ndarray, bool, dict[str, str]]:
    """选底图。配方用 RAW；没配方有 JPG 就用相机 JPG；只有 RAW 用 04 标准。"""
    if edit.lut is not None:
        from ..lut.photo import color_image as lut_image

        return lut_image(render, lib, shot, edit, max_edge=max_edge, quality=quality, base=base, full=full)
    files = shot["files"]
    has_jpg = "jpg" in files
    has_dng = "dng" in files
    mode = base
    if mode == "auto":
        if edit.recipe is not None:
            mode = "recipe"
        elif has_jpg:
            mode = "camera-jpg"
        else:
            mode = "standard"
    if mode == "camera-jpg":
        if not has_jpg:
            raise NoRaw(NO_RAW)
        return (
            load_jpeg(lib.abspath(files["jpg"]["relpath"]), None if full else max_edge),
            False,
            {
                "kind": "original",
                "label": "original",
                "text": "原图",
                "id": "camera-jpg",
                "tag": "",
                "description": "原图",
                "detail": "",
                "calibrated": "0",
                "cache": "miss",
                "key": "",
            },
        )
    if mode == "neutral":
        if not has_dng:
            raise NoRaw(NO_RAW)
        rgb, labels = _raw_rgb(render, lib, shot, EditState(), full=full)
        labels["kind"] = "original"
        labels["label"] = "original"
        labels["text"] = "中性转换"
        labels["id"] = "neutral-preview"
        labels["tag"] = "_neutral"
        if not labels.get("description"):
            labels["description"] = "GXR Studio neutral-preview: white-balanced linear sensor data to sRGB, no recipe applied"
        return rgb, True, labels
    if not has_dng:
        if edit.recipe is None or not ENABLED or not render.available():
            raise NoRaw(NO_RAW)
        if edit.recipe.levels_dict():
            raise EditError("approx_knobs", "近似效果只支持默认参数")
        rgb = load_jpeg(lib.abspath(files["jpg"]["relpath"]), None if full else max_edge)
        rgb = apply_lut(rgb, render.approx_lut(edit.recipe.unit, int(edit.recipe.slot), int(shot.get("iso") or 200)))
        return (
            rgb,
            False,
            {
                "kind": "approx",
                "label": "approx",
                "text": APPROX_LABEL,
                "id": "approx",
                "tag": "",
                "description": APPROX_LABEL,
                "detail": "",
                "calibrated": "0",
                "cache": "miss",
                "key": "",
            },
        )
    raw_edit = edit if mode == "recipe" else EditState(adjust=edit.adjust, master=edit.master)
    try:
        rgb, labels = _raw_rgb(render, lib, shot, raw_edit, full=full)
    except (NoRaw, RenderUnavailable, RawDecodeError) as exc:
        if not has_jpg:
            raise
        return (
            load_jpeg(lib.abspath(files["jpg"]["relpath"]), None if full else max_edge),
            False,
            {
                "kind": "original",
                "label": "original",
                "text": "原图",
                "id": "camera-jpg",
                "tag": "",
                "description": "原图",
                "detail": f"没有全尺寸传感器数据。已改用相机 JPG。{exc}",
                "calibrated": "0",
                "cache": "miss",
                "key": "",
            },
        )
    if mode == "standard" and labels.get("id") != "neutral-preview":
        labels["kind"] = "prediction"
        labels["label"] = "prediction"
    return rgb, True, labels


def _source_path(lib: Library, shot: dict[str, Any]) -> Path:
    files = shot["files"]
    kind = "jpg" if "jpg" in files else "dng"
    return lib.abspath(files[kind]["relpath"])


def render_stage(
    render: RenderService,
    lib: Library,
    shot: dict[str, Any],
    edit: EditState,
    side: dict[str, Any],
    *,
    stage: str,
    max_edge: int | None,
    quality: int,
    base: str = "auto",
    track: HdrTrack | None = None,
) -> tuple[bytes, dict[str, str]]:
    """`track`：导出 HDR 时跟着成片走的 RAW 亮度层（`hdr_export`），底图一定下来就开始。"""
    frame_default, stamp_default = load_defaults(render.home)
    bare = stage == "original"
    frame = factory_frame() if bare else resolved_frame(edit.frame, frame_default)
    stamp = factory_stamp() if bare else resolved_stamp(edit.stamp, stamp_default)
    bare_edit = EditState() if bare else edit
    untouched = bare or not has_visible_edit(bare_edit, {} if bare else side, frame, stamp)
    if untouched and "jpg" in shot["files"] and base in ("auto", "camera-jpg"):
        raw = _source_path(lib, shot).read_bytes()
        labels = {
            "kind": "original",
            "label": "original",
            "text": "原图",
            "id": "camera-jpg",
            "tag": "",
            "description": "原图",
            "detail": "",
            "valid": "",
            "calibrated": "0",
            "cache": "miss",
            "key": "",
        }
        if track is not None:
            track.begin(load_jpeg(_source_path(lib, shot), None))
        if max_edge is None:
            return raw, labels
        return encode_jpeg(_fit(load_jpeg(_source_path(lib, shot), None), max_edge), quality), labels
    full = max_edge is None
    rgb, from_raw, labels = color_image(
        render, shot, bare_edit, max_edge=max_edge, quality=quality, lib=lib, base="standard" if bare else base, full=full
    )
    if bare:
        labels = {
            "kind": "original",
            "label": "original",
            "text": "原图",
            "id": labels.get("id") or "camera-jpg",
            "detail": labels.get("detail") or "",
        }
        return encode_jpeg(_fit(rgb, max_edge), quality), {**labels, "valid": ""}
    if track is not None:
        track.begin(rgb)
    meta, taken, file_time = _meta(shot, lib)
    crop_raw = side.get("crop") if isinstance(side.get("crop"), dict) else {}
    raw_rect = crop_raw.get("rect") if isinstance(crop_raw, dict) else None
    crop_rect = None
    if isinstance(raw_rect, list) and len(raw_rect) == 4:
        crop_rect = (float(raw_rect[0]), float(raw_rect[1]), float(raw_rect[2]), float(raw_rect[3]))
    preset = crop_raw.get("preset") if isinstance(crop_raw, dict) else None
    rgb, header = finish_array(
        rgb,
        bare_edit,
        stage=stage,
        frame=frame,
        stamp=stamp,
        crop_rect=crop_rect,
        meta=meta,
        taken=taken,
        file_time=file_time,
        from_raw=from_raw,
        crop_preset=preset if isinstance(preset, str) else None,
        track=track,
    )
    labels["valid"] = header or ""
    labels.setdefault("label", labels.get("kind") or "original")
    labels.setdefault("text", "原图")
    return encode_jpeg(_fit(rgb, max_edge), quality), labels


def export_finish(
    render: RenderService,
    lib: Library,
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
) -> Any:
    """成片导出。长边按加上边框之后的最终尺寸计算。机器值与显示文字分开。
    `hdr`：在导出的 JPEG 上按成片重新算增益图（`hdr_export`）；做不了就照常导出，结果里写原因。"""
    import os

    from ..export import FORMATS, ExportError, ExportOptions, ExportResult, check_destination, claim_path, write_with_exif

    ExportOptions(long_edge, quality, fmt).validate()
    files = shot["files"]
    track = (
        HdrTrack(
            lib.abspath(files["dng"]["relpath"]) if "dng" in files else None,
            jpeg=lib.abspath(files["jpg"]["relpath"]) if "jpg" in files else None,
        )
        if hdr
        else None
    )
    try:
        data, labels = render_stage(render, lib, shot, edit, side, stage="final", max_edge=None, quality=quality, base=base, track=track)
    except RawDecodeError as exc:
        detail = f"没有全尺寸传感器数据，也没有更小的预览。{exc}"
        if "jpg" in shot["files"]:
            raise ExportError("raw_unreadable", "无法读取 RAW 文件。文件可能已损坏。", detail) from exc
        raise ExportError("no_jpg_source", "无法读取 RAW 文件。这张照片没有相机 JPG。", detail) from exc
    if long_edge:
        fitted = _fit(_decode(data), long_edge)
        data = encode_jpeg(fitted, quality)
    note = labels.get("description") or labels.get("text") or "原图"
    kind = labels.get("label") or labels.get("kind") or "original"
    ident = labels.get("id") or ("camera-jpg" if kind == "original" else "recipe-prediction")
    text = labels.get("text") or "原图"
    tag = labels.get("tag") or ""
    calibrated = True if labels.get("calibrated") == "1" else False if labels.get("calibrated") == "0" else None
    dest = check_destination(dest_dir, [*forbidden, lib.root])
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExportError("dest_unwritable", "无法导出到这个位置。请换一个文件夹。", exc.strerror or "") from exc
    ext = FORMATS[fmt][1]
    try:
        path, fd = claim_path(dest, str(shot["name"]), ext, tag)
    except OSError as exc:
        raise ExportError("dest_unwritable", "无法导出到这个位置。请换一个文件夹。", exc.strerror or "") from exc
    owned = True
    hdr_info: dict[str, Any] = {}
    try:
        payload = write_with_exif(data, _source_path(lib, shot), note, quality=quality, fmt=fmt)
        if track is not None:
            payload, hdr_info = finish_hdr(payload, track, fmt)
        with os.fdopen(fd, "wb") as fh:
            owned = False
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        if owned:
            os.close(fd)
        path.unlink(missing_ok=True)
        raise
    with Image.open(path) as saved:
        width, height = saved.size
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
        hdr=hdr_info.get("hdr") if track is not None else None,
        hdr_reason=hdr_info.get("reason"),
    )


def edited_jpeg(render: RenderService, lib: Library, shot: dict[str, Any], max_edge: int) -> bytes | None:
    from ..render.params import parse_edit

    side, _problem = lib.read_sidecar_or_default(shot)
    edit = parse_edit(side)
    frame, stamp = load_defaults(render.home)
    frame = resolved_frame(edit.frame, frame)
    stamp = resolved_stamp(edit.stamp, stamp)
    if not has_visible_edit(edit, side, frame, stamp):
        return None
    data, _labels = render_stage(render, lib, shot, edit, side, stage="final", max_edge=max_edge, quality=86 if max_edge <= 256 else 88)
    return data
