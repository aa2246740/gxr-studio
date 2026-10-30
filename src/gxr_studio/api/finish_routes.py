"""边框、时间戳、纠正和能力查询。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..finish.auto_lines import estimate
from ..finish.catalog import frames_document
from ..finish.meta_text import meta_payload
from ..finish.serve import load_defaults, render_stage, save_defaults
from ..finish.spec import parse_frame, parse_stamp
from ..render.params import EditError, parse_edit
from .errors import ApiError
from .render_routes import _edit_error
from .routes import _shot_or_404, ctx

router = APIRouter()


class FinishDefaults(BaseModel):
    frame: dict[str, Any] | None = None
    stamp: dict[str, Any] | None = None


def _capabilities(c: Any, shot: dict[str, Any]) -> dict[str, Any]:
    from ..finish.approx import ENABLED

    raw = "dng" in shot["files"]
    if raw and c.render.available():
        return {"raw": True, "recipe": "full", "reason": ""}
    if raw:
        return {"raw": True, "recipe": "none", "reason": "请先导入相机配方文件"}
    if ENABLED and c.render.available():
        return {"raw": False, "recipe": "approx", "reason": "近似"}
    return {"raw": False, "recipe": "none", "reason": "配方需要 RAW 文件"}


@router.get("/api/photos/{shot_id}/capabilities", response_model=None)
def capabilities(shot_id: str, request: Request) -> dict[str, Any]:
    c = ctx(request)
    return _capabilities(c, _shot_or_404(c, shot_id))


@router.get("/api/frames", response_model=None)
def frames() -> dict[str, Any]:
    return frames_document()


@router.get("/api/photos/{shot_id}/meta", response_model=None)
def photo_meta(shot_id: str, request: Request) -> dict[str, Any]:
    c = ctx(request)
    shot = _shot_or_404(c, shot_id)
    return meta_payload(c.lib, shot)


@router.get("/api/defaults/finish", response_model=None)
def get_finish_defaults(request: Request) -> dict[str, Any]:
    frame, stamp = load_defaults(ctx(request).settings.home_dir)
    return {"frame": frame.to_json(), "stamp": stamp.to_json()}


@router.put("/api/defaults/finish", response_model=None)
def put_finish_defaults(body: FinishDefaults, request: Request) -> dict[str, Any]:
    try:
        frame = parse_frame(body.frame) if body.frame is not None else parse_frame({"style": "none"})
        stamp = parse_stamp(body.stamp) if body.stamp is not None else parse_stamp({"enabled": False})
    except EditError as exc:
        raise _edit_error(exc) from exc
    if frame is None or stamp is None:
        raise ApiError(422, "bad_frame", "边框或时间戳的默认值不完整。请重新选择后再试。")
    return save_defaults(ctx(request).settings.home_dir, frame.to_json(), stamp.to_json())


@router.post("/api/photos/{shot_id}/geometry/auto", response_model=None)
def geometry_auto(shot_id: str, request: Request) -> dict[str, Any]:
    c = ctx(request)
    shot = _shot_or_404(c, shot_id)
    side, _problem = c.lib.read_sidecar_or_default(shot)
    edit = parse_edit(side)
    from dataclasses import replace

    current_s = float(getattr(edit.geometry, "straighten", 0.0) or 0.0)
    current_v = int(round(float(getattr(edit.geometry, "vertical", 0.0) or 0.0)))
    clean = replace(edit, geometry=None)
    try:
        jpeg, _labels = render_stage(c.render, c.lib, shot, clean, side, stage="uncropped", max_edge=640, quality=80)
    except Exception as exc:
        raise ApiError(422, "auto_failed", "无法分析这张照片。请手动调整拉直和垂直。") from exc
    import io

    import numpy as np
    from PIL import Image

    rgb = np.asarray(Image.open(io.BytesIO(jpeg)).convert("RGB"))
    guess = estimate(rgb)
    if not guess["confident"]:
        return {"straighten": current_s, "vertical": current_v, "confident": False, "message": "未检测到明显的水平线"}
    raw_s = guess["straighten"] if isinstance(guess["straighten"], (int, float)) and not isinstance(guess["straighten"], bool) else 0.0
    raw_v = guess["vertical"] if isinstance(guess["vertical"], (int, float)) and not isinstance(guess["vertical"], bool) else 0.0
    straighten = round(float(raw_s), 4) if guess["horizon"] else current_s
    vertical = int(round(float(raw_v))) if guess["upright"] else current_v
    return {
        "straighten": max(-45.0, min(45.0, straighten)),
        "vertical": max(-100, min(100, vertical)),
        "confident": True,
        "message": "",
    }
