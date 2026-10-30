"""M2 的 HTTP 接口：配方列表、sidecar 里的配方/调整/曲线、电脑预测的预览图。业务逻辑在 render/ 里。

所有颜色结果都是"电脑预测"：响应头 `X-GXR-Prediction: 1`，`X-GXR-Label`（百分号编码的中文标签）说明是否已标定。
没有导入相机数据时，预览返回 409 加完整状态（`status`），而不是一张不对的图。
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel

from ..crop import Crop, CropError
from ..library import sidecar as sc
from ..library.edits import RevisionConflict
from ..raw.dng import RawDecodeError
from ..render import edits as render_edits
from ..render.params import EditError, EditState, parse_adjust, parse_curve, parse_recipe
from ..render.service import NoRaw, RenderUnavailable
from .errors import ApiError
from .routes import _shot_or_404, ctx

router = APIRouter()

MAX_EDGE_LIMIT = 4096
# 正常情况下编辑记录坏了会自动退回"没有编辑"（见 Library.read_sidecar_*）；真读写不了（权限、磁盘）才到这里


def _sidecar_unreadable() -> ApiError:
    return ApiError(422, "sidecar_unreadable", "这张照片的编辑记录读写不了（照片本身没事）。检查一下图库文件夹的权限后再试。")


class EditRequest(BaseModel):
    """出现在请求体里的块才会被改；值为 null = 还原这一块。"""

    recipe: dict | None = None
    adjust: dict | None = None
    curve: dict | None = None
    expected_rev: int | None = None


class RenderRequest(BaseModel):
    """不落盘的实时预览：出现的块盖过 sidecar 里的，没出现的用 sidecar 的。"""

    recipe: dict | None = None
    adjust: dict | None = None
    curve: dict | None = None


# ---- 小工具 -----------------------------------------------------------------


def _edit_error(exc: EditError) -> ApiError:
    return ApiError(422, exc.code, exc.message)


def _unavailable(c, exc: RenderUnavailable) -> ApiError:
    return ApiError(409, exc.code, exc.message, {"status": c.render.status()})


def _read_edit(c, shot: dict):
    try:
        return render_edits.read(c.lib, shot)
    except sc.SidecarCorrupt as exc:
        raise _sidecar_unreadable() from exc
    except EditError as exc:
        raise _edit_error(exc) from exc


def _payload(c, shot: dict, edit: EditState, side: dict, warning: str | None = None) -> dict:
    out = render_edits.describe(side, edit)
    out["render"] = _render_info(c, shot, edit)
    if warning:
        out["warning"] = warning
    return out


def _render_info(c, shot: dict, edit: EditState) -> dict:
    svc = c.render
    if "dng" not in shot["files"]:
        return {
            "available": False,
            "reason": "no_raw",
            "message": "这张照片没有 RAW，不能套配方（预览和导出都是相机 JPG）。",
        }
    try:
        plan = svc.plan(shot, edit)
    except RenderUnavailable as exc:
        return {"available": False, "reason": exc.code, "message": exc.message, "status": svc.status()}
    except EditError as exc:
        return {"available": False, "reason": exc.code, "message": exc.message}
    return {
        "available": True,
        "prediction": True,
        "label": plan.label,
        "calibrated": plan.calibrated,
        "reasons": list(plan.reasons),
        "unit": plan.unit,
        "slot": plan.slot,
        "defaulted": plan.defaulted,
        "effective_levels": plan.levels,
        "entry_sha256": plan.entry_sha256,
        "model_iso": plan.model_iso,
    }


def _merge(edit: EditState, body: RenderRequest | None) -> EditState:
    if body is None:
        return edit
    given = body.model_fields_set
    try:
        return EditState(
            recipe=parse_recipe(body.recipe) if "recipe" in given else edit.recipe,
            adjust=parse_adjust(body.adjust) if "adjust" in given else edit.adjust,
            master=parse_curve(body.curve) if "curve" in given else edit.master,
        )
    except EditError as exc:
        raise _edit_error(exc) from exc


def _crop_of(side: dict) -> Crop | None:
    raw = side.get("crop")
    return Crop(raw["preset"], tuple(raw["rect"]), float(raw.get("angle_deg", 0.0))) if raw else None


def _render(
    request: Request,
    shot_id: str,
    body: RenderRequest | None,
    max_edge: int | None,
    exact: bool,
    apply_crop: bool,
    quality: int,
) -> Response:
    c = ctx(request)
    shot = _shot_or_404(c, shot_id)
    if max_edge is not None and not 64 <= max_edge <= MAX_EDGE_LIMIT:
        raise ApiError(422, "bad_size", f"max_edge 要在 64 到 {MAX_EDGE_LIMIT} 之间")
    if not 30 <= quality <= 100:
        raise ApiError(422, "bad_quality", "quality 要在 30 到 100 之间")
    edit, side = _read_edit(c, shot)
    edit = _merge(edit, body)
    try:
        crop = _crop_of(side) if apply_crop else None
        out = c.render.render_preview(shot, edit, max_edge=max_edge, exact=exact, crop=crop, quality=quality)
    except RenderUnavailable as exc:
        raise _unavailable(c, exc) from exc
    except NoRaw as exc:
        raise ApiError(409, "no_raw", "这张照片没有 RAW，只有相机 JPG，不能套配方。") from exc
    except EditError as exc:
        raise _edit_error(exc) from exc
    except RawDecodeError as exc:
        raise ApiError(422, "raw_unreadable", f"这份 RAW 暂时读不出来。（{exc}）") from exc
    except CropError as exc:
        raise ApiError(422, exc.code, exc.message) from exc
    except ValueError as exc:  # 例如色表行不对称：冻结模型不能预测这个条目
        raise ApiError(422, "cannot_predict", f"这个配方的电脑预测算不出来：{exc}") from exc
    etag = f'"{out.key}"'
    headers = {
        "ETag": etag,
        "Cache-Control": "private, no-cache",
        "X-GXR-Prediction": "1",
        "X-GXR-Calibrated": "1" if out.plan.calibrated else "0",
        "X-GXR-Label": quote(out.plan.label),
        "X-GXR-Render-Key": out.key,
        "X-GXR-Cache": "hit" if out.cache_hit else "miss",
        "X-GXR-Timing": ";".join(f"{k}={v:.1f}" for k, v in out.ms.items()),
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(out.jpeg, media_type="image/jpeg", headers=headers)


# ---- 状态与配方列表 ------------------------------------------------------------


@router.get("/api/render/status")
def render_status(request: Request):
    return ctx(request).render.status()


@router.get("/api/recipes")
def list_recipes(request: Request):
    """3 个单元 × 8 个底子，以及每个底子有哪些旋钮、每个旋钮能取哪些档位（含到顶标记）。没导入相机数据也能列。"""
    svc = ctx(request).render
    status = svc.status()
    return {
        "status": status,
        "units": svc.catalog(),
        "note": "旋钮范围与档位来自相机的 knobs v2 菜单表；颜色只是电脑预测，没有相机实测。",
    }


# ---- sidecar 里的编辑 -----------------------------------------------------------


@router.get("/api/photos/{shot_id}/edit")
def get_edit(shot_id: str, request: Request):
    c = ctx(request)
    shot = _shot_or_404(c, shot_id)
    edit, side = _read_edit(c, shot)
    return _payload(c, shot, edit, side, c.lib.sidecar_problem(shot))


@router.put("/api/photos/{shot_id}/edit")
def put_edit(shot_id: str, body: EditRequest, request: Request):
    c = ctx(request)
    shot = _shot_or_404(c, shot_id)
    changes = {k: getattr(body, k) for k in ("recipe", "adjust", "curve") if k in body.model_fields_set}
    warning = c.lib.sidecar_problem(shot)
    try:
        edit, side = render_edits.write(c.lib, shot, changes, c.render.menu, expected_rev=body.expected_rev)
    except EditError as exc:
        raise _edit_error(exc) from exc
    except RevisionConflict as exc:
        raise ApiError(409, "rev_conflict", f"这张照片的编辑已经被改过（现在是第 {exc.current} 版），请刷新后再试") from exc
    except sc.SidecarTooNew as exc:
        raise ApiError(409, "sidecar_too_new", "这张照片的编辑文件来自更新的版本，只读") from exc
    except sc.SidecarCorrupt as exc:
        raise _sidecar_unreadable() from exc
    return _payload(c, shot, edit, side, warning)


@router.delete("/api/photos/{shot_id}/edit")
def delete_edit(shot_id: str, request: Request):
    c = ctx(request)
    shot = _shot_or_404(c, shot_id)
    warning = c.lib.sidecar_problem(shot)
    try:
        edit, side = render_edits.write(c.lib, shot, {"recipe": None, "adjust": None, "curve": None}, c.render.menu)
    except sc.SidecarTooNew as exc:
        raise ApiError(409, "sidecar_too_new", "这张照片的编辑文件来自更新的版本，只读") from exc
    except sc.SidecarCorrupt as exc:
        raise _sidecar_unreadable() from exc
    return _payload(c, shot, edit, side, warning)


# ---- 预览图 ---------------------------------------------------------------------


@router.get("/api/photos/{shot_id}/render")
def get_render(
    shot_id: str,
    request: Request,
    max_edge: int | None = None,
    exact: bool = False,
    crop: bool = False,
    quality: int = 88,
):
    """按 sidecar 里的编辑渲染（半分辨率，烘焙路径）。`max_edge` 再缩小（拖滑块时用）；`crop` 套上裁切与拉直。"""
    return _render(request, shot_id, None, max_edge, exact, crop, quality)


@router.post("/api/photos/{shot_id}/render")
def post_render(
    shot_id: str,
    body: RenderRequest,
    request: Request,
    max_edge: int | None = None,
    exact: bool = False,
    crop: bool = False,
    quality: int = 88,
):
    """同上，但请求体里的 recipe/adjust/curve 盖过 sidecar（不落盘）：拖滑块的实时预览。"""
    return _render(request, shot_id, body, max_edge, exact, crop, quality)
