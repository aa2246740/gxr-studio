"""显卡预览的只读接口（功能开关 `gpu_preview`）。都不写文件、不改编辑记录（LUT 库的旧格式升级在启动时做）。

- `GET /api/photos/{id}/gpu`：这张照片能不能在显卡上预览、RAW 布局、Log LUT 要的场景色彩、各数据的链接；
- `GET /api/photos/{id}/raw`：DNG 原字节（链接里带 SHA，浏览器可以长期缓存）；
- `GET /api/photos/{id}/camera-jpg`：相机 JPG 原文件（浏览器按 EXIF 方向解码，实测与 PIL 逐字节相同）；
- `GET /api/photos/{id}/bake?recipe=…`：这张照片在这组配方下的烘焙表（服务器缓存 + ETag）；
- `GET /api/luts/{cube_id}/table`：显示类 LUT 的 8 位精确表（256³，与 Python 逐像素相同），Log 类 LUT 的 3D 表；
- `POST /api/photos/{id}/gpu/box`：给定几何与裁切，成片在颜色层上的裁切框和有效区（与 `finish_array` 相同）。

照片文件一律经 `library.media.open_media` 打开：只读、拒绝图库外路径和任何一级链接，按已打开的 fd 发送和解析。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from collections import OrderedDict
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Body, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from ..crop import PRESETS
from ..finish.spec import parse_geometry
from ..flags import flag_enabled
from ..gpu.box import final_box
from ..gpu.source import RawSource, describe
from ..gpu.tables import bake_bytes, lut_bytes
from ..library.media import FdResponse, MediaRefused, open_media, read_all
from ..lut.errors import LutError
from ..raw import open_dng
from ..raw.dng import RawDecodeError
from ..render.params import EditError, EditState, parse_recipe
from ..render.service import ENGINE, NoRaw, RenderUnavailable
from .errors import ApiError
from .render_routes import _edit_error, _unavailable
from .routes import _shot_or_404, ctx

router = APIRouter()

LONG_CACHE = "private, max-age=31536000, immutable"
_layouts: OrderedDict[str, RawSource] = OrderedDict()
_layouts_lock = threading.Lock()
LAYOUT_CACHE = 256
# 显示类 LUT 的精确表每张 64 MB，算一次约 4 秒：按 (cube_id, 版本) 留最近两张；同一张同时只算一次
_tables: OrderedDict[tuple[str, str], tuple[bytes, dict[str, Any]]] = OrderedDict()
_tables_lock = threading.Lock()
_table_locks: dict[tuple[str, str], threading.Lock] = {}
TABLE_CACHE = 2
UNAVAILABLE_FILE = "这张照片的文件不在图库里，或者已被替换成链接。请重新导入这张照片。"


def _open(c: Any, shot: dict[str, Any], kind: str) -> int:
    try:
        return open_media(c.lib, shot["files"][kind]["relpath"])
    except MediaRefused as exc:
        raise ApiError(409, "photo_file_unavailable", UNAVAILABLE_FILE, {"detail": str(exc)}) from exc
    except FileNotFoundError as exc:
        raise ApiError(404, "photo_file_missing", "找不到这张照片的文件。请重新导入。") from exc


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
        fd = open_media(c.lib, meta["relpath"])
    except MediaRefused as exc:
        return RawSource(None, f"refused:{exc}", None)
    except OSError as exc:
        return RawSource(None, f"unreadable:{type(exc).__name__}", None)
    try:
        data = read_all(fd)
    finally:
        os.close(fd)
    try:
        dng = open_dng(data, meta["relpath"])
    except (RawDecodeError, ValueError, NotImplementedError) as exc:
        return RawSource(None, f"unreadable:{type(exc).__name__}", None)
    try:
        src = describe(dng, with_scene=True)  # Log 类 LUT 现在所有单元的 RAW 都能用（lut/photo.py）
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
    if not flag_enabled(c.settings, "gpu_preview"):
        return {"enabled": False}  # 开关关着：不读 DNG、不预热
    out: dict[str, Any] = {
        "enabled": True,
        "unit": c.render.photo_unit(shot),
        "raw": None,
        "raw_reason": None if "dng" in files else "no_raw",
        "jpg": None,
        "scene": None,
        "render_available": c.render.available(c.render.photo_unit(shot) or "jlaunch"),
    }
    if "dng" in files:
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
    fd = _open(c, shot, "dng")
    return FdResponse(
        fd, media_type="application/octet-stream", headers={"Cache-Control": LONG_CACHE}, range_header=request.headers.get("range")
    )


@router.get("/api/photos/{shot_id}/camera-jpg", response_model=None)
def gpu_camera_jpg(shot_id: str, request: Request) -> Any:
    c = ctx(request)
    shot = _shot_or_404(c, shot_id)
    if "jpg" not in shot["files"]:
        raise ApiError(404, "no_jpg", "这张照片没有相机 JPG。")
    fd = _open(c, shot, "jpg")
    return FdResponse(fd, media_type="image/jpeg", headers={"Cache-Control": LONG_CACHE}, range_header=request.headers.get("range"))


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
    identity = (
        f"{ENGINE}:{plan.unit}:{plan.record_sha256}:{plan.entry_sha256}:"
        f"{plan.model_iso}:{meta['step']}:{plan.binding_sha256}:{plan.selection_id}"
    )
    key = hashlib.sha256(identity.encode()).hexdigest()[:24]
    meta.update(
        {
            "key": key,
            "entry_sha256": plan.entry_sha256,
            "record_sha256": plan.record_sha256,
            "binding_sha256": plan.binding_sha256,
            "selection_id": plan.selection_id,
            "quality": plan.quality,
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


def _lut_table(c: Any, cube_id: str) -> tuple[bytes, dict[str, Any]]:
    """只读：LUT 库按 persist=False 读，不写盘。表按 (cube_id, 版本) 缓存，同一张同时只算一次。"""
    try:
        target, _item, token = c.render.luts.target_and_token(cube_id, persist=False)
    except LutError as exc:
        raise ApiError(exc.status, exc.code, exc.message) from exc
    key = (cube_id, token)
    with _tables_lock:
        hit = _tables.get(key)
        if hit is not None:
            _tables.move_to_end(key)
            return hit
        lock = _table_locks.setdefault(key, threading.Lock())
    with lock:
        with _tables_lock:
            hit = _tables.get(key)
        if hit is not None:
            return hit
        data, meta, reason = lut_bytes(target)
        if data is None or meta is None:
            raise ApiError(422, "lut_gpu_unsupported", "这个 LUT 不能在显卡上预览。", {"detail": reason})
        meta["key"] = f"{cube_id[:16]}-{token}"
        with _tables_lock:
            _tables[key] = (data, meta)
            while len(_tables) > TABLE_CACHE:
                _tables.popitem(last=False)
            _table_locks.pop(key, None)
        return data, meta


@router.get("/api/luts/{cube_id}/table", response_model=None)
def gpu_lut_table(cube_id: str, request: Request) -> Response:
    c = ctx(request)
    data, meta = _lut_table(c, cube_id)
    etag = f'"{meta["key"]}"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache", "X-GXR-Lut": quote(json.dumps(meta))}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(data, media_type="application/octet-stream", headers=headers)


class BoxRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: StrictInt = Field(ge=1, le=20000)
    height: StrictInt = Field(ge=1, le=20000)
    geometry: dict[str, Any] | None = None
    crop: dict[str, Any] | None = None


def _bad_box(detail: str) -> ApiError:
    return ApiError(422, "bad_box", "请求无效。请刷新页面后重试。", {"detail": detail})


def _crop_of(raw: dict[str, Any] | None) -> tuple[tuple[float, float, float, float] | None, str | None]:
    """严格解析：只认 rect（4 个有限数，在 0..1 里，宽高为正）和 preset（已知名字）。"""
    if raw is None:
        return None, None
    if set(raw) - {"rect", "preset", "orientation"}:
        raise _bad_box("crop 有无法识别的键")
    rect = raw.get("rect")
    out = None
    if rect is not None:
        if not isinstance(rect, list) or len(rect) != 4:
            raise _bad_box("rect 要是 4 个数")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in rect):
            raise _bad_box("rect 要是有限数")
        x, y, w, h = (float(v) for v in rect)
        if not (0 <= x <= 1 and 0 <= y <= 1 and 0 < w <= 1 and 0 < h <= 1 and x + w <= 1 + 1e-6 and y + h <= 1 + 1e-6):
            raise _bad_box("rect 超出画面")
        out = (x, y, w, h)
    preset = raw.get("preset")
    if preset is not None and (not isinstance(preset, str) or (preset not in PRESETS and preset not in ("free", "original"))):
        raise _bad_box("preset 无法识别")
    return out, preset


def _pixel_budget(c: Any, shot: dict[str, Any]) -> int:
    """裁切框请求的尺寸不能超过这张照片自己的像素数（RAW 颜色层、全尺寸或相机 JPG 里最大的那个）。"""
    budget = int(shot.get("width") or 0) * int(shot.get("height") or 0)
    if "dng" in shot["files"]:
        layout = _raw_source(c, shot).layout
        if layout is not None:
            budget = max(budget, layout["full"][0] * layout["full"][1])
    return budget


_BOX_BODY = {"required": True, "content": {"application/json": {"schema": BoxRequest.model_json_schema()}}}


@router.post("/api/photos/{shot_id}/gpu/box", response_model=None, openapi_extra={"requestBody": _BOX_BODY})
def gpu_box(shot_id: str, request: Request, raw: Any = Body(None)) -> dict[str, Any]:  # noqa: B008  FastAPI 的写法
    c = ctx(request)
    shot = _shot_or_404(c, shot_id)
    try:  # 在这里校验，错误和别的接口一样是 {"error": ...}
        body = BoxRequest.model_validate(raw)
    except ValidationError as exc:
        raise _bad_box("; ".join(f"{'.'.join(map(str, e['loc']))}: {e['type']}" for e in exc.errors())) from exc
    if body.width * body.height > _pixel_budget(c, shot):
        raise _bad_box("尺寸超过这张照片")
    try:
        geom = parse_geometry(body.geometry) if body.geometry else parse_geometry({})
    except EditError as exc:
        raise _edit_error(exc) from exc
    rect, preset = _crop_of(body.crop)
    box, valid = final_box(body.width, body.height, geom, rect, preset)
    return {"box": list(box), "valid": [round(v, 6) for v in valid]}
