"""界面偏好：引导是否已经结束，以及导出时的 HDR 开关。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from .. import privacy
from ..prefs import load_prefs, update_prefs
from .errors import ApiError
from .routes import _forbidden_roots, ctx

router = APIRouter()


class PrefsUpdate(BaseModel):
    onboarding_dismissed: bool | None = None
    export_hdr: bool | None = None


def _forbidden(c: Any) -> list[Path]:
    return privacy.forbidden_for(c.settings, _forbidden_roots(c))


@router.get("/api/prefs", response_model=None)
def get_prefs(request: Request) -> dict[str, bool]:
    c = ctx(request)
    return load_prefs(c.settings.home_dir, _forbidden(c))


@router.put("/api/prefs", response_model=None)
def put_prefs(body: PrefsUpdate, request: Request) -> dict[str, bool]:
    raw = body.model_dump(exclude_unset=True)
    changes: dict[str, bool] = {}
    for key, value in raw.items():
        if not isinstance(value, bool):
            raise ApiError(422, "bad_prefs", "请求无效。请刷新页面后重试。", {"detail": str(key)})
        changes[key] = value
    c = ctx(request)
    return update_prefs(c.settings.home_dir, _forbidden(c), changes)
