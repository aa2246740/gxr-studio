"""M3a 的 LUT 导入接口（ARCHITECTURE §6.4 路线 1）。业务逻辑在 `lut/service.py`、`lut/fit.py`。

- `POST /api/lut/upload`                  .cube 的原始字节作请求体（不用 multipart），文件名放在 `X-GXR-Filename`；
- `GET  /api/lut/spaces`                  可选的输入/输出色彩空间；
- `GET  /api/lut/{cube_id}/preview`       原图 / 加 LUT 后（`which=original|lut`，可选 `photo=<照片 id>`）；
- `POST /api/lut/fit`                     开始拟合（子进程），返回 `job_id`；
- `GET  /api/lut/fit/{job_id}`            进度、状态，做完带结果；
- `POST /api/lut/fit/{job_id}/cancel`     取消；
- `GET/POST/PUT/DELETE /api/my-recipes`   电脑端的"我的配方"（只存在电脑里，这一版不写相机卡）。

所有数字都是"电脑预测"；28mm / MOUNT 没有标定，拒绝导入。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from ..lut import cube as cubemod
from ..lut.library import file_parts
from ..lut.service import LutError, spaces_listing
from .errors import ApiError
from .lut_library_routes import call
from .routes import _shot_or_404, ctx

router = APIRouter()


class SpaceArgs(BaseModel):
    space: str = "srgb"
    output: str = "srgb"
    input_gamut: str | None = None
    output_gamut: str | None = None
    input_range: str = "full"
    output_range: str = "full"

    def spec(self) -> dict[Any, Any]:
        return {k: getattr(self, k) for k in SpaceArgs.model_fields}


class FitRequest(SpaceArgs):
    cube_id: str
    unit: str = "jlaunch"
    photo_ids: list[str] | None = None
    profile: str = "normal"
    space_guessed: bool = False


class SaveRecipeRequest(BaseModel):
    job_id: str
    candidate: int = 0
    name: str


class RenameRequest(BaseModel):
    name: str


def _api(exc: LutError) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message, exc.extra)


def _svc(request: Request) -> Any:
    return ctx(request).lut


@router.get("/api/lut/spaces", response_model=None)
def lut_spaces() -> dict[Any, Any]:
    return {
        "spaces": spaces_listing(),
        "default_input": None,
        "default_output": "srgb",
        "note": "",
    }


@router.post("/api/lut/upload", response_model=None)
async def lut_upload(request: Request) -> Any:
    name = unquote(request.headers.get("x-gxr-filename") or "")
    group = unquote(request.headers["x-gxr-group"]) if "x-gxr-group" in request.headers else None
    filename, _folder, skip = file_parts(name)
    if skip:
        return {"skipped": True, "filename": filename}
    if name and Path(filename).suffix.lower() != ".cube":
        raise ApiError(422, "cube_unsupported", "不支持这种 LUT 格式")
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cubemod.MAX_BYTES:
            raise ApiError(422, "cube_too_big", "无法读取 LUT。文件过大。", {"detail": "超过 64 MB"})
        chunks.append(chunk)
    return await run_in_threadpool(call, _svc(request).add_cube, b"".join(chunks), name, group)


@router.get("/api/lut/{cube_id}/preview", response_model=None)
def lut_preview(
    cube_id: str,
    request: Request,
    which: str = "lut",
    photo: str | None = None,
    max_edge: int = 640,
    space: str = "srgb",
    output: str = "srgb",
    input_gamut: str | None = None,
    output_gamut: str | None = None,
    input_range: str = "full",
    output_range: str = "full",
) -> Any:
    c = ctx(request)
    if not 64 <= max_edge <= 1600:
        raise ApiError(422, "bad_size", "请求无效。请刷新页面后重试。", {"detail": "max_edge 要在 64 到 1600 之间"})
    shot = _shot_or_404(c, photo) if photo else None
    spec = SpaceArgs(
        space=space,
        output=output,
        input_gamut=input_gamut,
        output_gamut=output_gamut,
        input_range=input_range,
        output_range=output_range,
    ).spec()
    try:
        jpeg, info = c.lut.preview(cube_id, which, spec, shot, max_edge)
    except LutError as exc:
        raise _api(exc) from exc
    label = {"04-simulation": "04 电脑模拟", "neutral": "中性转换", "camera-jpg": "相机 JPG", "chart": "示例照片"}[info["mode"]]
    return Response(
        jpeg,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "private, no-store",
            "X-GXR-Prediction": "1",
            "X-GXR-Preview-Mode": info["mode"],
            "X-GXR-Label": quote(label),
            "X-GXR-Nominal-Scene": "1" if info.get("nominal_scene") else "0",
        },
    )


@router.post("/api/lut/fit", response_model=None)
def lut_fit(body: FitRequest, request: Request) -> Any:
    svc = _svc(request)
    try:
        return svc.start_fit(
            body.cube_id,
            body.spec(),
            unit=body.unit,
            photo_ids=body.photo_ids,
            profile=body.profile,
            guessed=body.space_guessed,
        )
    except LutError as exc:
        raise _api(exc) from exc


@router.get("/api/lut/fit/{job_id}", response_model=None)
def lut_fit_status(job_id: str, request: Request) -> Any:
    try:
        return _svc(request).status(job_id)
    except LutError as exc:
        raise _api(exc) from exc


@router.post("/api/lut/fit/{job_id}/cancel", response_model=None)
def lut_fit_cancel(job_id: str, request: Request) -> Any:
    try:
        return _svc(request).cancel(job_id)
    except LutError as exc:
        raise _api(exc) from exc


# ---- 我的配方 ---------------------------------------------------------------


@router.get("/api/my-recipes", response_model=None)
def recipes_list(request: Request) -> dict[Any, Any]:
    return {"recipes": _svc(request).list_recipes()}


@router.post("/api/my-recipes", response_model=None)
def recipes_save(body: SaveRecipeRequest, request: Request) -> Any:
    try:
        return _svc(request).save_recipe(body.job_id, body.candidate, body.name)
    except LutError as exc:
        raise _api(exc) from exc


@router.get("/api/my-recipes/{rid}", response_model=None)
def recipes_get(rid: str, request: Request) -> Any:
    try:
        return _svc(request).get_recipe(rid)
    except LutError as exc:
        raise _api(exc) from exc


@router.put("/api/my-recipes/{rid}", response_model=None)
def recipes_rename(rid: str, body: RenameRequest, request: Request) -> Any:
    try:
        return _svc(request).rename_recipe(rid, body.name)
    except LutError as exc:
        raise _api(exc) from exc


@router.delete("/api/my-recipes/{rid}", response_model=None)
def recipes_delete(rid: str, request: Request) -> dict[Any, Any]:
    try:
        _svc(request).delete_recipe(rid)
    except LutError as exc:
        raise _api(exc) from exc
    return {"deleted": rid}
