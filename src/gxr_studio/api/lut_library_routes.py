"""LUT 库管理。cube 导出只写到应用数据目录的 luts/exports。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from ..lut.errors import LutError
from ..privacy import PrivatePathError
from .errors import ApiError
from .routes import ctx

router = APIRouter()


class LutPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str | None = None
    group: str | None = None
    starred: bool | None = None
    input_space: str | None = None
    output_space: str | None = None
    input_gamut: str | None = None
    output_gamut: str | None = None
    input_range: str | None = None
    output_range: str | None = None
    source: str | None = None
    author: str | None = None
    license: str | None = None


class GroupName(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str


class GroupExport(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    group: str | None = None


def call(action: Callable[..., Any], *args: Any) -> Any:
    try:
        return action(*args)
    except LutError as exc:
        raise ApiError(exc.status, exc.code, exc.message, exc.extra) from exc
    except PrivatePathError as exc:
        raise ApiError(422, exc.code, exc.message, {"detail": exc.detail}) from exc
    except OSError as exc:
        raise ApiError(422, "lut_io_error", "无法读写 LUT 库。请检查应用数据。", {"detail": str(exc)}) from exc


@router.get("/api/luts", response_model=None)
def list_luts(request: Request) -> Any:
    return call(ctx(request).lut.library.listing)


@router.patch("/api/luts/{cube_id}", response_model=None)
def patch_lut(cube_id: str, body: LutPatch, request: Request) -> Any:
    c = ctx(request)
    changes = body.model_dump(exclude_unset=True)
    result = call(c.lut.library.patch, cube_id, changes)
    if {"input_space", "output_space", "input_gamut", "output_gamut", "input_range", "output_range"} & changes.keys():
        call(c.lut.invalidate_cube, cube_id)
        c.render.invalidate()
    return result


@router.delete("/api/luts/{cube_id}", response_model=None)
def delete_lut(cube_id: str, request: Request) -> Any:
    c = ctx(request)
    call(c.lut.library.get, cube_id)
    call(c.lut.invalidate_cube, cube_id)
    call(c.lut.library.delete, cube_id)
    c.render.invalidate()
    return {"deleted": True}


@router.post("/api/luts/{cube_id}/export", response_model=None)
def export_lut(cube_id: str, request: Request) -> Any:
    return call(ctx(request).lut.library.export, cube_id)


@router.post("/api/lut-groups", response_model=None)
def add_group(body: GroupName, request: Request) -> Any:
    return call(ctx(request).lut.library.add_group, body.name)


@router.patch("/api/lut-groups/{group_id}", response_model=None)
def rename_group(group_id: str, body: GroupName, request: Request) -> Any:
    call(ctx(request).lut.library.change_group, group_id, body.name)
    return {"updated": True}


@router.delete("/api/lut-groups/{group_id}", response_model=None)
def delete_group(group_id: str, request: Request) -> Any:
    call(ctx(request).lut.library.change_group, group_id, None)
    return {"deleted": True}


@router.post("/api/lut-groups/export", response_model=None)
def export_group(body: GroupExport, request: Request) -> Any:
    return {"exports": call(ctx(request).lut.library.export_group, body.group)}
