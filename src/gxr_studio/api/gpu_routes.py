"""显卡预览的只读接口（功能开关 `gpu_preview`）。都不写文件、不改编辑记录。

- `GET /api/photos/{id}/gpu`：这张照片能不能在显卡上预览、RAW 布局、Log LUT 要的场景色彩、各数据的链接；
- `GET /api/photos/{id}/raw`：DNG 原字节（链接里带 SHA，浏览器可以长期缓存）；
- `GET /api/photos/{id}/camera-jpg`：相机 JPG 原文件（浏览器按 EXIF 方向解码，实测与 PIL 逐字节相同）；
- `GET /api/photos/{id}/bake?recipe=…`：这张照片在这组配方下的烘焙表（服务器缓存 + ETag）；
- `GET /api/luts/{cube_id}/table`：LUT 的 3D 表；
- `POST /api/photos/{id}/gpu/box`：给定几何与裁切，成片在颜色层上的裁切框和有效区（与 `finish_array` 相同）。
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from ..finish.spec import parse_geometry
from ..flags import flag_enabled
from ..gpu.box import final_box
from ..gpu.source import RawSource, describe
from ..gpu.tables import bake_bytes, lut_bytes
from ..lut.errors import LutError
from ..raw import open_dng
from ..raw.dng import RawDecodeError
from ..render.params import EditError, EditState, parse_recipe
from ..render.service import NoRaw, RenderUnavailable
from .errors import ApiError
from .render_routes import _edit_error, _unavailable
from .routes import _shot_or_404, ctx

router = APIRouter()

LONG_CACHE = "private, max-age=31536000, immutable"
_layouts: OrderedDict[str, RawSource] = OrderedDict()
_layouts_lock = threading.Lock()
LAYOUT_CACHE = 256


def _version(sha: str) -> str:
    return sha[:16]


def _raw_source(c: Any, shot: dict[str, Any]) -> RawSource:
    """DNG 的布局按 SHA-256 缓存：同一个文件只解析一次 TIFF。"""
    meta = shot["files"]["dng"]
    sha = str(meta["sha256"])
    with _layouts_lock:
        hit = _layouts.get(sha)
        if hit is not None:
            _layouts.move_to_end(sha)
            return hit
    try:
        dng = open_dng(c.lib.abspath(meta["relpath"]))
    except (RawDecodeError, ValueError, NotImplementedError, OSError) as exc:
        return RawSource(None, f"unreadable:{type(exc).__name__}", None)
    try:
        src = describe(dng, with_scene=c.render.photo_unit(shot) == "jlaunch")
    except RawDecodeError:
        src = RawSource(None, "no_white_balance", None)
    finally:
        dng.release()
    with _layouts_lock:
        _layouts[sha] = src
        while len(_layouts) > LAYOUT_CACHE:
            _layouts.popitem(last=False)
    return src


@router.get("/api/photos/{shot_id}/gpu", response_model=None)
def gpu_info(shot_id: str, request: Request) -> dict[str, Any]:
    c = ctx(request)
    shot = _shot_or_404(c, shot_id)
    files = shot["files"]
    out: dict[str, Any] = {
        "enabled": flag_enabled(c.settings, "gpu_preview"),
        "unit": c.render.photo_unit(shot),
        "raw": None,
        "raw_reason": None if "dng" in files else "no_raw",
        "jpg": None,
        "scene": None,
        "render_available": c.render.available(),
    }
    if "dng" in files:
        if out["enabled"]:
            c.render.warm_preview_bakes(shot)
        src = _raw_source(c, shot)
        out["raw_reason"] = src.reason
        out["scene"] = src.scene
        if src.layout is not None:
            out["raw"] = {**src.layout, "url": f"/api/photos/{shot_id}/raw?v={_version(files['dng']['sha256'])}"}
    if "jpg" in files:
        out["jpg"] = {"url": f"/api/photos/{shot_id}/camera-jpg?v={_version(str(files['jpg'].get('sha256') or ''))}"}
    return out


@router.get("/api/photos/{shot_id}/raw", response_model=None)
def gpu_raw(shot_id: str, request: Request) -> Any:
    c = ctx(request)
    shot = _shot_or_404(c, shot_id)
    if "dng" not in shot["files"]:
        raise ApiError(404, "no_raw", "这张照片没有 RAW 文件。")
    path = c.lib.abspath(shot["files"]["dng"]["relpath"])
    return FileResponse(path, media_type="application/octet-stream", headers={"Cache-Control": LONG_CACHE})


@router.get("/api/photos/{shot_id}/camera-jpg", response_model=None)
def gpu_camera_jpg(shot_id: str, request: Request) -> Any:
    c = ctx(request)
    shot = _shot_or_404(c, shot_id)
    if "jpg" not in shot["files"]:
        raise ApiError(404, "no_jpg", "这张照片没有相机 JPG。")
    return FileResponse(c.lib.abspath(shot["files"]["jpg"]["relpath"]), media_type="image/jpeg", headers={"Cache-Control": LONG_CACHE})


@router.get("/api/photos/{shot_id}/bake", response_model=None)
def gpu_bake(shot_id: str, request: Request, recipe: str | None = None) -> Response:
    c = ctx(request)
    shot = _shot_or_404(c, shot_id)
    if "dng" not in shot["files"]:
        raise ApiError(409, "no_raw", "配方需要 RAW 文件。拍摄时将画质设为 RAW+JPEG。")
    try:
        sel = parse_recipe(json.loads(recipe)) if recipe else None
    except (ValueError, TypeError) as exc:
        raise ApiError(422, "bad_recipe", "请求无效。请刷新页面后重试。") from exc
    except EditError as exc:
        raise _edit_error(exc) from exc
    try:
        plan = c.render.plan(shot, EditState(recipe=sel))
        bake = c.render.preview_bake(plan)
    except RenderUnavailable as exc:
        raise _unavailable(c, exc) from exc
    except NoRaw as exc:
        raise ApiError(409, "no_raw", "配方需要 RAW 文件。拍摄时将画质设为 RAW+JPEG。") from exc
    except EditError as exc:
        raise _edit_error(exc) from exc
    except ValueError as exc:  # 色表行不对称：冻结模型不能预测这个条目
        raise ApiError(422, "cannot_predict", "无法生成电脑预测。请重试。", {"detail": str(exc)}) from exc
    data, meta = bake_bytes(bake)
    key = hashlib.sha256(f"{plan.entry_sha256}:{plan.model_iso}:{meta['step']}:{c.render.engine_pin()}".encode()).hexdigest()[:24]
    meta.update(
        {
            "key": key,
            "entry_sha256": plan.entry_sha256,
            "model_iso": plan.model_iso,
            "calibrated": plan.calibrated,
            "label": plan.label,
            "unit": plan.unit,
            "slot": plan.slot,
            "defaulted": plan.defaulted,
        }
    )
    etag = f'"{key}"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache", "X-GXR-Bake": quote(json.dumps(meta, ensure_ascii=False))}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(data, media_type="application/octet-stream", headers=headers)


@router.get("/api/luts/{cube_id}/table", response_model=None)
def gpu_lut_table(cube_id: str, request: Request) -> Response:
    c = ctx(request)
    try:
        target, _item = c.render.luts.target(cube_id)
    except LutError as exc:
        raise ApiError(exc.status, exc.code, exc.message) from exc
    data, meta, reason = lut_bytes(target)
    if data is None or meta is None:
        raise ApiError(422, "lut_gpu_unsupported", "这个 LUT 不能在显卡上预览。", {"detail": reason})
    token = c.render.luts.render_token(cube_id)
    meta["key"] = f"{cube_id[:16]}-{token}"
    etag = f'"{meta["key"]}"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache", "X-GXR-Lut": quote(json.dumps(meta))}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(data, media_type="application/octet-stream", headers=headers)


class BoxRequest(BaseModel):
    width: int = Field(ge=1, le=20000)
    height: int = Field(ge=1, le=20000)
    geometry: dict[str, Any] | None = None
    crop: dict[str, Any] | None = None


@router.post("/api/photos/{shot_id}/gpu/box", response_model=None)
def gpu_box(shot_id: str, body: BoxRequest, request: Request) -> dict[str, Any]:
    c = ctx(request)
    _shot_or_404(c, shot_id)
    try:
        geom = parse_geometry(body.geometry) if body.geometry else parse_geometry({})
    except EditError as exc:
        raise _edit_error(exc) from exc
    if geom.expands():
        raise ApiError(422, "gpu_geometry_unsupported", "透视和畸变还不能在显卡上预览。")
    rect = None
    preset = None
    if body.crop:
        raw = body.crop.get("rect")
        if isinstance(raw, list) and len(raw) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in raw):
            rect = (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
        p = body.crop.get("preset")
        preset = p if isinstance(p, str) else None
    box, valid = final_box(body.width, body.height, geom, rect, preset)
    return {"box": list(box), "valid": [round(v, 6) for v in valid]}
