"""导出只编码一次之前（origin/main e1e67f9）的出图顺序，原样冻结，只给 `test_export_once.py` 对照用。

- `legacy_render_stage`：旧的 `finish.serve.render_stage`，逐行照抄。预览字节必须和它一样。
- `legacy_export_payload`：旧的 `export_finish` 里写盘之前的部分：`render_stage` 出 JPEG → 有长边就解码、缩小、再编码 →
  `write_with_exif` 解码再编码 → HDR 在解码出来的成片上拟合增益。不碰目标目录，返回要写的字节。

这里只依赖这次没有改的函数（`color_image`、`finish_array`、`_fit`、`_finish` 等），所以旧行为不会跟着新代码变。
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageOps

from gxr_studio.export import _DROP_EXIF_TAGS, ExportOptions, _clean_text, _finish
from gxr_studio.finish.compose import encode_jpeg, finish_array, has_visible_edit, load_jpeg, resolved_frame, resolved_stamp
from gxr_studio.finish.hdr_export import NO_RAW, NOT_JPEG, HdrTrack, _to_grid
from gxr_studio.finish.serve import _decode, _fit, _meta, _scale, _source_path, color_image, full_edge, load_defaults
from gxr_studio.finish.spec import factory_frame, factory_stamp
from gxr_studio.hdr.container import ContainerError
from gxr_studio.hdr.gain import HdrUnsupported, compute_gain, package, sdr_luma_grid
from gxr_studio.render.params import EditState


def legacy_render_stage(
    render: Any,
    lib: Any,
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
            "scale": _scale(shot, min(full_edge(shot), max_edge or full_edge(shot))),
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
    base_edge = max(rgb.shape[:2])
    if track is not None:
        track.begin(rgb)
    meta, taken, file_time = _meta(shot, lib)
    crop_raw = side.get("crop") if isinstance(side.get("crop"), dict) else {}
    raw_rect = crop_raw.get("rect") if isinstance(crop_raw, dict) else None
    crop_rect = None
    if isinstance(raw_rect, list) and len(raw_rect) == 4:
        crop_rect = (float(raw_rect[0]), float(raw_rect[1]), float(raw_rect[2]), float(raw_rect[3]))
    preset = crop_raw.get("preset") if isinstance(crop_raw, dict) else None
    use_custom = stage == "final" and frame.style != "none" and frame.logo not in ("", "ricoh", "none")
    logos_info = render.logos.info_map() if use_custom else None
    logo_images = render.logos.image_map(frame.logo) if use_custom else None
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
        logos=logos_info,
        logo_images=logo_images,
    )
    labels["valid"] = header or ""
    labels.setdefault("label", labels.get("kind") or "original")
    labels.setdefault("text", "原图")
    fitted = _fit(rgb, max_edge)
    labels["scale"] = _scale(shot, base_edge, max(rgb.shape[:2]), max(fitted.shape[:2]))
    return encode_jpeg(fitted, quality), labels


def legacy_write_with_exif(data: bytes, source: Path, note: str, *, quality: int, fmt: str) -> bytes:
    with Image.open(io.BytesIO(data)) as raw:
        im = ImageOps.exif_transpose(raw).convert("RGB")
    base: dict[Any, Any] = {}
    sub: dict[Any, Any] = {}
    try:
        with Image.open(source) as src:
            exif = src.getexif()
            if 33432 in exif:
                base[33432] = exif[33432]
            for tag in (271, 272, 306):
                text = _clean_text(exif.get(tag))
                if text:
                    base[tag] = text
            sub = {k: v for k, v in exif.get_ifd(0x8769).items() if k not in _DROP_EXIF_TAGS}
            if not _clean_text(sub.get(42036)):
                lens = _clean_text(exif.get(50708))
                if lens:
                    sub[42036] = lens.removeprefix("RICOH ").strip()
    except Exception:  # nosec B110  照抄旧代码
        pass
    base[270] = note
    payload, _size = _finish(im, base, sub, None, ExportOptions(None, quality, fmt))
    return payload


def legacy_finish_hdr(payload: bytes, track: HdrTrack, fmt: str) -> tuple[bytes, dict[str, Any]]:
    if fmt != "jpeg":
        return payload, {"hdr": False, "reason": NOT_JPEG}
    if track.layer is None:
        return payload, {"hdr": False, "reason": track.problem or NO_RAW}
    with Image.open(io.BytesIO(payload)) as im:
        rgb = np.asarray(im.convert("RGB"))
    p = track.params
    raw_y, clip = _to_grid(track.layer, (rgb.shape[1], rgb.shape[0]), p.map_factor)
    try:
        result = compute_gain(sdr_luma_grid(rgb, p.map_factor), raw_y, clip, p)
        out = package(payload, result, p)
    except HdrUnsupported as exc:
        return payload, {"hdr": False, "reason": exc.reason}
    except ContainerError:
        return payload, {"hdr": False, "reason": "container"}
    return out, {"hdr": True, "gain_max_stops": result.stats["gain_max_stops"]}


def legacy_export_payload(
    render: Any,
    lib: Any,
    shot: dict[str, Any],
    edit: EditState,
    side: dict[str, Any],
    *,
    quality: int,
    long_edge: int | None,
    fmt: str,
    base: str = "auto",
    hdr: bool = False,
) -> tuple[bytes, dict[str, str], dict[str, Any]]:
    """旧 `export_finish` 写盘之前的那一段。返回 (要写的字节, labels, HDR 说明)。"""
    files = shot["files"]
    track = (
        HdrTrack(
            lib.abspath(files["dng"]["relpath"]) if "dng" in files else None,
            jpeg=lib.abspath(files["jpg"]["relpath"]) if "jpg" in files else None,
        )
        if hdr
        else None
    )
    data, labels = legacy_render_stage(render, lib, shot, edit, side, stage="final", max_edge=None, quality=quality, base=base, track=track)
    if long_edge:
        data = encode_jpeg(_fit(_decode(data), long_edge), quality)
    note = labels.get("description") or labels.get("text") or "原图"
    hdr_info: dict[str, Any] = {}
    payload = legacy_write_with_exif(data, _source_path(lib, shot), note, quality=quality, fmt=fmt)
    if track is not None:
        payload, hdr_info = legacy_finish_hdr(payload, track, fmt)
    return payload, labels, hdr_info
