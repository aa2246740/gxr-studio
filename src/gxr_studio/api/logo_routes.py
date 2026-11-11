"""用户自定义标志接口：列表、安全上传、图片读取、改名、删除与回退（DESIGN.md §10.4、§10.6）。"""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from ..finish.logo_store import ERR_TOO_BIG, MAX_BYTES, MAX_LOGOS, LogoError, revert_logo_usages
from .errors import ApiError
from .routes import ctx

router = APIRouter()


class LogoRenameBody(BaseModel):
    name: str


def _api_error(exc: LogoError) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message, exc.extra)


@router.get("/api/logos", response_model=None)
def list_logos(request: Request) -> dict[str, Any]:
    c = ctx(request)
    try:
        logos = c.render.logos.list_logos()
    except LogoError as exc:
        raise _api_error(exc) from exc
    return {"logos": logos, "max_logos": MAX_LOGOS}


@router.get("/api/logos/{logo_id}/image", response_model=None)
def get_logo_image(logo_id: str, request: Request) -> Response:
    c = ctx(request)
    try:
        png = c.render.logos.read_png(logo_id)
    except LogoError as exc:
        raise _api_error(exc) from exc
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
    except LogoError as exc:
        raise _api_error(exc) from exc


@router.put("/api/logos/{logo_id}", response_model=None)
def rename_logo(logo_id: str, body: LogoRenameBody, request: Request) -> dict[str, Any]:
    c = ctx(request)
    try:
        return dict(c.render.logos.rename_logo(logo_id, body.name))
    except LogoError as exc:
        raise _api_error(exc) from exc


@router.delete("/api/logos/{logo_id}", response_model=None)
def delete_logo(logo_id: str, request: Request) -> dict[str, Any]:
    c = ctx(request)
    try:
        removed, remaining = c.render.logos.delete_logo(logo_id)
    except LogoError as exc:
        raise _api_error(exc) from exc
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
