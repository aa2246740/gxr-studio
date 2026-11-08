"""边框、时间戳、纠正和能力查询。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from ..finish.auto_lines import estimate
from ..finish.catalog import frames_document
from ..finish.fonts import ASSETS, FONT_FILES, font_table_json
from ..finish.meta_text import meta_payload
from ..finish.serve import load_defaults, render_stage, save_defaults
from ..finish.sizes import output_sizes
from ..finish.spec import parse_frame, parse_stamp
from ..library.media import MediaRefused
from ..lut.errors import LutError
from ..raw.dng import RawDecodeError
from ..render.params import EditError, EditState, parse_edit
from ..render.service import RenderUnavailable
from .errors import ApiError
from .gpu_routes import _crop_of
from .render_routes import RenderRequest, _edit_error, _merge, _read_edit, _unavailable
from .routes import _shot_or_404, ctx

router = APIRouter()
FONT_DIR = ASSETS / "fonts"
LOGO_DIR = ASSETS / "logo"


class FinishDefaults(BaseModel):
    frame: dict[str, Any] | None = None
    stamp: dict[str, Any] | None = None


def _capabilities(c: Any, shot: dict[str, Any]) -> dict[str, Any]:
    from ..finish.approx import ENABLED

    raw = "dng" in shot["files"]
    if raw and c.render.available():
        return {"raw": True, "recipe": "full", "reason": ""}
    if raw:
        status = c.render.status()
        camera = status["camera_data"]
        if camera["state"] != "ready":
            reason = camera["message"]
            code = {"unsupported": "generation_unsupported", "invalid": "camera_data_invalid"}.get(camera["state"], "camera_data_missing")
        else:
            reason = "请先导入校准数据" if status["calibration"]["state"] == "missing" else status["calibration"]["message"]
            code = "calibration_missing"
        return {"raw": True, "recipe": "none", "reason": reason, "reason_code": code}
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


# 浏览器预览画边框用的字体和标志：和导出用的是同一批文件（只读，只有这几个名字）
ASSET_TYPES = {".ttf": "font/ttf", ".png": "image/png"}
ASSET_FILES: dict[str, Path] = {
    **{name: FONT_DIR / name for name in FONT_FILES.values()},
    "ricoh.png": LOGO_DIR / "ricoh.png",
}


# 每个字体的版面用表（步进宽度、字形外框、上沿高度）：和导出读的是同一份，从字体文件里读（finish/sfnt.py）
TABLE_NAMES = {f"{font}.json": font for font in FONT_FILES}


@router.get("/api/finish/assets/{name}", response_model=None)
def finish_asset(name: str) -> Response:
    headers = {"Cache-Control": "private, no-cache"}
    table = TABLE_NAMES.get(name)
    if table is not None:
        return Response(font_table_json(table), media_type="application/json", headers=headers)
    path = ASSET_FILES.get(name)
    if path is None:
        raise ApiError(404, "asset_not_found", "找不到这个文件。请刷新页面后重试。")
    return FileResponse(path, media_type=ASSET_TYPES[path.suffix], headers=headers)


@router.get("/api/photos/{shot_id}/meta", response_model=None)
def photo_meta(shot_id: str, request: Request) -> dict[str, Any]:
    c = ctx(request)
    shot = _shot_or_404(c, shot_id)
    return meta_payload(c.lib, shot)


@router.post("/api/photos/{shot_id}/output-size", response_model=None)
def photo_output_size(shot_id: str, request: Request, body: RenderRequest | None = None) -> dict[str, Any]:
    c = ctx(request)
    shot = _shot_or_404(c, shot_id)
    edit, side = _read_edit(c, shot)
    edit = _merge(edit, body)
    crop = body.crop if body is not None and "crop" in body.model_fields_set else side.get("crop")
    if isinstance(crop, dict):
        crop = {key: value for key, value in crop.items() if key != "angle_deg"}
    rect, preset = _crop_of(crop)
    try:
        return output_sizes(c.render, c.lib, shot, edit, rect, preset)
    except EditError as exc:
        raise _edit_error(exc) from exc
    except RenderUnavailable as exc:
        raise _unavailable(c, exc) from exc
    except LutError as exc:
        raise ApiError(exc.status, exc.code, exc.message) from exc
    except (RawDecodeError, MediaRefused, OSError) as exc:
        raise ApiError(422, "size_unavailable", "无法读取成品尺寸。请检查照片文件后重试。") from exc


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
    current_s = float(getattr(edit.geometry, "straighten", 0.0) or 0.0)
    current_v = int(round(float(getattr(edit.geometry, "vertical", 0.0) or 0.0)))
    try:
        # 分析的是原片（stage=original：不带配方、调整、几何、边框和已有的裁剪）。
        # 以前带着编辑记录渲染：照片一有任何编辑就从相机 JPG 改走 RAW 的电脑预测，第一次要两秒多，
        # 结果还和没编辑时不一样（同一张照片点两次「自动」得到两个答案）。
        jpeg, _labels = render_stage(c.render, c.lib, shot, EditState(), {}, stage="original", max_edge=640, quality=80)
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
