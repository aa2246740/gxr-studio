"""LUT 导入与完整本机配方接口。业务逻辑在 `lut/service.py`、`recipes/`。

- `POST /api/lut/upload`                  .cube 的原始字节作请求体（不用 multipart），文件名放在 `X-GXR-Filename`；
- `GET  /api/lut/spaces`                  可选的输入/输出色彩空间；
- `GET  /api/lut/{cube_id}/preview`       原图 / 加 LUT 后（`which=original|lut`，可选 `photo=<照片 id>`）；
- `POST /api/lut/fit`                     开始拟合（子进程），返回 `job_id`；
- `GET  /api/lut/fit/{job_id}`            进度、状态，做完带结果；
- `POST /api/lut/fit/{job_id}/cancel`     取消；
- `GET/POST/PUT/DELETE /api/my-recipes`   电脑端的"我的配方"（只存在电脑里，这一版不写相机卡）。

所有数字都是"电脑预测"；按单元核对已导入校准和显式质量输入。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from ..lut import cube as cubemod
from ..lut.library import file_parts
from ..lut.service import LutError, spaces_listing
from ..recipes import RecipeError
from ..render.recipes import UNIT_INFO
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
    model_config = ConfigDict(extra="forbid")
    cube_id: str
    unit: str = "jlaunch"
    photo_ids: list[str] | None = None
    profile: str = "normal"
    space_guessed: bool = False
    quality_source: str | None = None


class SaveRecipeRequest(BaseModel):
    job_id: str
    candidate: int = 0
    name: str


class RenameRequest(BaseModel):
    name: str
    expected_revision: int


class QualityImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    folder: str


def _api(exc: LutError | RecipeError) -> ApiError:
    if isinstance(exc, LutError):
        return ApiError(exc.status, exc.code, exc.message, exc.extra)
    status = 404 if exc.code == "recipe_not_found" else 409 if "conflict" in exc.code or exc.code.endswith("changed") else 422
    return ApiError(status, exc.code, exc.message)


def _svc(request: Request) -> Any:
    return ctx(request).lut


@router.post("/api/lut/quality", response_model=None)
def quality_import(body: QualityImportRequest, request: Request) -> Any:
    return call(_svc(request).contexts.import_pack, Path(body.folder).expanduser())


@router.get("/api/lut/quality", response_model=None)
def quality_list(request: Request) -> Any:
    return {"sources": call(_svc(request).contexts.listing)}


@router.get("/api/lut/spaces", response_model=None)
def lut_spaces(request: Request) -> dict[Any, Any]:
    return {
        "spaces": spaces_listing(),
        "default_input": "srgb",
        "default_output": "srgb",
        "note": "",
        "units": [
            {
                **unit,
                "calibrated": ctx(request).render.available(unit["id"]),
                "fit_supported": ctx(request).render.available(unit["id"]),
                "calibration": ctx(request).render.status()["calibration"]["units"][unit["id"]],
            }
            for unit in UNIT_INFO
        ],
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
    label = {
        "04-simulation": "04 电脑模拟",
        "recipe-prediction": "配方电脑预测",
        "neutral": "中性转换",
        "camera-jpg": "相机 JPG",
        "chart": "示例照片",
    }[info["mode"]]
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
            quality_source=body.quality_source,
        )
    except (LutError, RecipeError) as exc:
        raise _api(exc) from exc


@router.get("/api/lut/fit/{job_id}", response_model=None)
def lut_fit_status(job_id: str, request: Request) -> Any:
    try:
        return _svc(request).status(job_id)
    except (LutError, RecipeError) as exc:
        raise _api(exc) from exc


@router.post("/api/lut/fit/{job_id}/cancel", response_model=None)
def lut_fit_cancel(job_id: str, request: Request) -> Any:
    try:
        return _svc(request).cancel(job_id)
    except (LutError, RecipeError) as exc:
        raise _api(exc) from exc


# ---- 我的配方 ---------------------------------------------------------------


@router.get("/api/my-recipes", response_model=None)
def recipes_list(request: Request) -> dict[Any, Any]:
    svc = _svc(request)
    legacy = svc.home / "my_recipes"
    return {"recipes": call(svc.list_recipes), "legacy_unmigrated": 0 if legacy.is_symlink() else len(list(legacy.glob("*.json")))}


@router.post("/api/my-recipes", response_model=None)
def recipes_save(body: SaveRecipeRequest, request: Request) -> Any:
    try:
        return _svc(request).save_recipe(body.job_id, body.candidate, body.name)
    except (LutError, RecipeError) as exc:
        raise _api(exc) from exc


@router.get("/api/my-recipes/{rid}", response_model=None)
def recipes_get(rid: str, request: Request) -> Any:
    try:
        return _svc(request).get_recipe(rid)
    except (LutError, RecipeError) as exc:
        raise _api(exc) from exc


@router.put("/api/my-recipes/{rid}", response_model=None)
def recipes_rename(rid: str, body: RenameRequest, request: Request) -> Any:
    try:
        return _svc(request).rename_recipe(rid, body.name, body.expected_revision)
    except (LutError, RecipeError) as exc:
        raise _api(exc) from exc


@router.delete("/api/my-recipes/{rid}", response_model=None)
def recipes_delete(rid: str, request: Request, expected_revision: int) -> dict[Any, Any]:
    try:
        _svc(request).delete_recipe(rid, expected_revision)
    except (LutError, RecipeError) as exc:
        raise _api(exc) from exc
    return {"deleted": rid}
