"""关机图（GXROFF.JPG）桌面 HTTP 接口：预览生成与安全落盘。"""

from __future__ import annotations

import base64
import logging
import secrets
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import APIRouter, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .. import off_image, privacy
from ..export import ExportError
from ..finish.serve import render_stage_rgb
from ..observe import log
from ..raw.dng import RawDecodeError
from ..render.params import EditError
from ..render.service import NoRaw, RenderUnavailable
from .errors import ApiError
from .routes import ExportRequest, _export_source, _forbidden_roots, _shot_or_404, ctx

router = APIRouter()

MAX_UPLOAD_BYTES = 64 * 1024 * 1024  # 64 MiB
_CACHE_LIMIT = 8


class OffImageCache:
    """进程内有界预览缓存：导出时直接写刚预览并通过校验的那份字节，避免重复编码。"""

    def __init__(self, limit: int = _CACHE_LIMIT) -> None:
        self._lock = threading.Lock()
        self._limit = limit
        self._items: OrderedDict[str, off_image.OffImageResult] = OrderedDict()

    def put(self, result: off_image.OffImageResult) -> str:
        token = secrets.token_hex(12)
        with self._lock:
            self._items[token] = result
            self._items.move_to_end(token)
            while len(self._items) > self._limit:
                self._items.popitem(last=False)
        return token

    def get(self, token: str) -> off_image.OffImageResult | None:
        with self._lock:
            item = self._items.get(token)
            if item is not None:
                self._items.move_to_end(token)
            return item


class OffImageExportRequest(BaseModel):
    preview_token: str | None = None
    shot_id: str | None = None
    dest_dir: str | None = None


def _api_error(exc: ExportError) -> ApiError:
    """响应只有中文；细节可能带路径，只留在本地日志里。"""
    if exc.detail:
        log.event(logging.WARNING, "off_image_refused", code=exc.code, detail=exc.detail)
    return ApiError(422, exc.code, exc.message)


def _render_library_rgb(c: Any, shot_id: str) -> np.ndarray:
    shot = _shot_or_404(c, shot_id)
    want, edit = _export_source(c, shot, ExportRequest(source="auto"))
    side, _problem = c.lib.read_sidecar_or_default(shot)
    try:
        rgb, _labels = render_stage_rgb(
            c.render,
            c.lib,
            shot,
            edit,
            side,
            stage="final",
            max_edge=None,
            quality=95,
            base=want,
        )
    except RenderUnavailable as exc:
        raise ApiError(409, exc.code, exc.message, {"status": c.render.status()}) from exc
    except NoRaw as exc:
        raise ApiError(409, "no_raw", str(exc) or "配方需要 RAW 文件。拍摄时将画质设为 RAW+JPEG。") from exc
    except EditError as exc:
        raise ApiError(422, exc.code, exc.message) from exc
    except RawDecodeError as exc:
        log.event(logging.WARNING, "off_image_raw_unreadable", error=type(exc).__name__)
        raise ApiError(422, "raw_unreadable", "无法读取 RAW 文件。文件可能已损坏。") from exc
    return rgb


def _preview_payload(c: Any, result: off_image.OffImageResult) -> dict[str, Any]:
    chk = result.check
    errors = [{"code": issue.code, "message": issue.message} for issue in chk.errors]
    if not chk.ok:
        return {
            "ok": False,
            "bytes": result.bytes,
            "quality": result.quality,
            "width": result.width,
            "height": result.height,
            "dat_state": chk.dat_state,
            "errors": errors,
            "sha256": result.sha256,
            "preview_data_url": None,
            "preview_token": None,  # nosec B105
        }
    token = c.off_images.put(result)
    b64 = base64.b64encode(result.data).decode("ascii")
    return {
        "ok": True,
        "bytes": result.bytes,
        "quality": result.quality,
        "width": result.width,
        "height": result.height,
        "dat_state": chk.dat_state,
        "errors": [],
        "sha256": result.sha256,
        "preview_data_url": f"data:image/jpeg;base64,{b64}",
        "preview_token": token,
    }


@router.post("/api/off-image/preview", response_model=None)
async def preview_off_image(request: Request, shot_id: str | None = None) -> dict[str, Any]:
    """生成 640×480 GXROFF.JPG 预览：传 shot_id 用图库照片的当前成片；否则读取上传的原始图片字节。"""
    c = ctx(request)
    if shot_id:

        def _from_shot() -> off_image.OffImageResult:
            rgb = _render_library_rgb(c, shot_id)
            return off_image.encode_off_image_rgb(rgb)

        try:
            result = await run_in_threadpool(_from_shot)
        except ExportError as exc:
            raise _api_error(exc) from exc
        return _preview_payload(c, result)

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise ApiError(422, "image_too_big", "图片文件过大，请选择 64 MB 以内的图片。")
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw:
        raise ApiError(422, "missing_source", "请选择要做成关机图的照片或图片文件。")

    try:
        result = await run_in_threadpool(off_image.encode_off_image_bytes, raw)
    except ExportError as exc:
        raise _api_error(exc) from exc
    return _preview_payload(c, result)


@router.post("/api/off-image/export", response_model=None)
async def export_off_image(body: OffImageExportRequest, request: Request) -> dict[str, Any]:
    """将通过严格校验的 GXROFF.JPG 写入目标文件夹，拒绝挂载的存储卡与受保护目录。"""
    c = ctx(request)
    dest = Path(body.dest_dir).expanduser() if body.dest_dir else c.settings.export_dir
    if not dest.is_absolute():
        raise ApiError(422, "bad_destination", "无法导出。请填写导出到的完整路径。")

    def _run() -> dict[str, Any]:
        result: off_image.OffImageResult | None = None
        if body.preview_token:
            result = c.off_images.get(body.preview_token)
            if result is None:
                raise ApiError(404, "preview_expired", "预览已过期，请重新生成后再导出。")
        elif body.shot_id:
            rgb = _render_library_rgb(c, body.shot_id)
            result = off_image.encode_off_image_rgb(rgb)
        else:
            raise ApiError(422, "missing_source", "请先选择要做成关机图的照片。")

        forbidden = privacy.forbidden_for(c.settings, _forbidden_roots(c))
        saved = off_image.save_off_image(result.data, dest, forbidden)
        export_id = c.exports.note_new(saved.parent, saved)
        return {
            "path": str(saved),
            "bytes": result.bytes,
            "quality": result.quality,
            "width": result.width,
            "height": result.height,
            "sha256": result.sha256,
            "export_id": export_id,
        }

    try:
        return await run_in_threadpool(_run)
    except ExportError as exc:
        raise _api_error(exc) from exc
