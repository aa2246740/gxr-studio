"""用户自定义标志接口：列表、安全上传、图片读取、改名、删除与回退（DESIGN.md §10.4、§10.6）。"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, NoReturn
from urllib.parse import unquote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .. import privacy
from ..finish.logo_store import ERR_TOO_BIG, MAX_BYTES, MAX_LOGOS, LogoError, revert_logo_usages
from ..observe import log
from .errors import ApiError
from .routes import ctx

router = APIRouter()


class LogoRenameBody(BaseModel):
    name: str


ERR_LOGO_UNAVAILABLE = "标志库暂时不能用。请检查应用数据文件夹。"
ERR_LOGO_FILE = "这个标志的文件读不出来。请重新上传。"


def _api_error(exc: LogoError) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message, exc.extra)


def _raise_logo(exc: Exception) -> NoReturn:
    """把存储守卫和文件错误挡在接口边界：响应只有中文，路径留在本地日志里。"""
    if isinstance(exc, LogoError):
        raise _api_error(exc) from exc
    if isinstance(exc, privacy.PrivatePathError):
        log.event(logging.WARNING, "logo_path_refused", code=exc.code, detail=exc.detail)
        raise ApiError(422, "logo_unavailable", ERR_LOGO_UNAVAILABLE) from exc
    if isinstance(exc, OSError):
        log.event(logging.WARNING, "logo_file_unreadable", error=type(exc).__name__, errno=getattr(exc, "errno", None))
        raise ApiError(422, "logo_file_unreadable", ERR_LOGO_FILE) from exc
    raise exc


def _guard(action: Any) -> Any:
    try:
        return action()
    except Exception as exc:
        _raise_logo(exc)


@router.get("/api/logos", response_model=None)
def list_logos(request: Request) -> Response:
    c = ctx(request)
    logos = _guard(c.render.logos.list_logos)
    body = {"logos": logos, "max_logos": MAX_LOGOS}
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    etag = '"' + hashlib.sha256(raw).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "private, no-store"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(body, headers=headers)


@router.get("/api/logos/{logo_id}/image", response_model=None)
def get_logo_image(logo_id: str, request: Request) -> Response:
    c = ctx(request)
    png = _guard(lambda: c.render.logos.read_png(logo_id))
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, no-store"})


@router.post("/api/logos", response_model=None)
async def upload_logo(
    request: Request,
    name: str | None = None,
    filename: str | None = None,
    key_white: str = "auto",
    tol: int = 30,
) -> dict[str, Any]:
    if not 0 <= tol <= 60:
        raise ApiError(422, "bad_logo_tol", "去白底容差要在 0 到 60 之间。")
    if key_white not in ("auto", "true", "false", "1", "0"):
        raise ApiError(422, "bad_logo_key", "去白底选项无效。请重试。")
    hdr_name = unquote(request.headers.get("x-gxr-logo-name") or "")
    hdr_file = unquote(request.headers.get("x-gxr-filename") or "")
    chosen_name = name if name is not None else hdr_name
    chosen_file = filename if filename is not None else hdr_file
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BYTES:
            raise ApiError(422, "logo_too_big", ERR_TOO_BIG)
        chunks.append(chunk)
    c = ctx(request)

    def _run() -> dict[str, Any]:
        return dict(
            c.render.logos.add_logo(
                b"".join(chunks),
                name=chosen_name,
                filename=chosen_file,
                key_mode=key_white,
                tol=tol,
            )
        )

    try:
        return await run_in_threadpool(_run)
    except Exception as exc:
        _raise_logo(exc)


@router.put("/api/logos/{logo_id}", response_model=None)
def rename_logo(logo_id: str, body: LogoRenameBody, request: Request) -> dict[str, Any]:
    c = ctx(request)
    renamed = _guard(lambda: c.render.logos.rename_logo(logo_id, body.name))
    return dict(renamed)


@router.delete("/api/logos/{logo_id}", response_model=None)
def delete_logo(logo_id: str, request: Request) -> dict[str, Any]:
    c = ctx(request)
    removed, remaining = _guard(lambda: c.render.logos.delete_logo(logo_id))
    reverted_revs, reverted_defaults = revert_logo_usages(logo_id, c.lib, c.settings.home_dir)
    reverted_photos = len(reverted_revs)
    nm = removed["name"]
    if reverted_photos > 0 and reverted_defaults:
        msg = f"已删除「{nm}」，用到它的 {reverted_photos} 张照片和默认边框已改回 RICOH 标志。"
    elif reverted_photos > 0:
        msg = f"已删除「{nm}」，用到它的 {reverted_photos} 张照片已改回 RICOH 标志。"
    elif reverted_defaults:
        msg = f"已删除「{nm}」，默认边框已改回 RICOH 标志。"
    else:
        msg = f"已删除「{nm}」。"
    return {
        "deleted": logo_id,
        "name": nm,
        "reverted_photos": reverted_photos,
        "reverted_revs": reverted_revs,
        "reverted_defaults": reverted_defaults,
        "message": msg,
        "logos": remaining,
    }
