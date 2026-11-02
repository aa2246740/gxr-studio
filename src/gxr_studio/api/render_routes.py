"""M2 的 HTTP 接口：配方列表、sidecar 里的配方/调整/曲线、电脑预测的预览图。业务逻辑在 render/ 里。

所有颜色结果都是"电脑预测"：响应头 `X-GXR-Prediction: 1`，`X-GXR-Label`（百分号编码的中文标签）说明是否已标定。
没有导入相机数据时，预览返回 409 加完整状态（`status`），而不是一张不对的图。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel

from ..crop import Crop, CropError
from ..library import sidecar as sc
from ..library.edits import RevisionConflict
from ..lut.errors import LutError
from ..lut.spaces import DISPLAY
from ..raw.dng import RawDecodeError
from ..render import edits as render_edits
from ..render.params import EditError, EditState, parse_adjust, parse_curve, parse_lut, parse_recipe
from ..render.service import NoRaw, RenderUnavailable
from .errors import ApiError
from .routes import _shot_or_404, ctx

router = APIRouter()

MAX_EDGE_LIMIT = 4096
# 正常情况下编辑记录坏了会自动退回"没有编辑"（见 Library.read_sidecar_*）；真读写不了（权限、磁盘）才到这里


def _sidecar_unreadable() -> ApiError:
    return ApiError(422, "sidecar_unreadable", "这张照片的编辑记录读写不了。请检查图库文件夹的权限后再试。")


class EditRequest(BaseModel):
    """出现在请求体里的块才会被改；值为 null = 还原这一块。"""

    recipe: dict[Any, Any] | None = None
    lut: str | None = None
    adjust: dict[Any, Any] | None = None
    curve: dict[Any, Any] | None = None
    geometry: dict[Any, Any] | None = None
    frame: dict[Any, Any] | None = None
    stamp: dict[Any, Any] | None = None
    expected_rev: int | None = None


class RenderRequest(BaseModel):
    """不落盘的实时预览：出现的块盖过 sidecar 里的，没出现的用 sidecar 的。"""

    recipe: dict[Any, Any] | None = None
    lut: str | None = None
    adjust: dict[Any, Any] | None = None
    curve: dict[Any, Any] | None = None
    geometry: dict[Any, Any] | None = None
    crop: dict[Any, Any] | None = None
    frame: dict[Any, Any] | None = None
    stamp: dict[Any, Any] | None = None


# ---- 小工具 -----------------------------------------------------------------


def _edit_error(exc: EditError) -> ApiError:
    extra = {"detail": exc.detail} if exc.detail else None
    return ApiError(422, exc.code, exc.message, extra)


def _unavailable(c: Any, exc: RenderUnavailable) -> ApiError:
    return ApiError(409, exc.code, exc.message, {"status": c.render.status()})


def _read_edit(c: Any, shot: dict[Any, Any]) -> tuple[EditState, dict[Any, Any]]:
    try:
        return render_edits.read(c.lib, shot)
    except sc.SidecarCorrupt as exc:
        raise _sidecar_unreadable() from exc
    except EditError as exc:
        raise _edit_error(exc) from exc


def _payload(c: Any, shot: dict[Any, Any], edit: EditState, side: dict[Any, Any], warning: str | None = None) -> dict[Any, Any]:
    out = render_edits.describe(side, edit)
    out["render"] = _render_info(c, shot, edit)
    if warning:
        out["warning"] = warning
    return out


def _render_info(c: Any, shot: dict[Any, Any], edit: EditState) -> dict[Any, Any]:
    svc = c.render
    if "dng" not in shot["files"]:
        from ..finish.approx import ENABLED

        approx = ENABLED and svc.available()
        return {
            "available": True,
            "reason": None if approx else "no_raw",
            "recipe": "approx" if approx else "none",
            "message": "" if approx else "配方需要 RAW 文件。拍摄时将画质设为 RAW+JPEG。",
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
    from ..finish.spec import parse_frame, parse_geometry, parse_stamp

    given = body.model_fields_set
    try:
        return EditState(
            recipe=parse_recipe(body.recipe) if "recipe" in given else edit.recipe,
            adjust=parse_adjust(body.adjust) if "adjust" in given else edit.adjust,
            master=parse_curve(body.curve) if "curve" in given else edit.master,
            geometry=parse_geometry(body.geometry) if "geometry" in given else edit.geometry,
            frame=parse_frame(body.frame) if "frame" in given else edit.frame,
            stamp=parse_stamp(body.stamp) if "stamp" in given else edit.stamp,
            lut=parse_lut(body.lut) if "lut" in given else edit.lut,
        )
    except EditError as exc:
        raise _edit_error(exc) from exc


def _crop_of(side: dict[Any, Any]) -> Crop | None:
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
    stage: str | None = None,
    size: str | None = None,
) -> Response:
    from ..finish.serve import SIZE_EDGE, render_stage

    c = ctx(request)
    shot = _shot_or_404(c, shot_id)
    if stage not in (None, "final", "uncropped", "original"):
        raise ApiError(422, "bad_stage", "请求无效。请刷新页面后重试。")
    if size not in (None, "thumb", "preview", "full"):
        raise ApiError(422, "bad_size", "请求无效。请刷新页面后重试。", {"detail": "尺寸只能是 thumb、preview 或 full"})
    if max_edge is None and size in SIZE_EDGE:
        max_edge = SIZE_EDGE[size]
    if max_edge is not None and not 64 <= max_edge <= MAX_EDGE_LIMIT:
        raise ApiError(422, "bad_size", "请求无效。请刷新页面后重试。", {"detail": f"max_edge 要在 64 到 {MAX_EDGE_LIMIT} 之间"})
    if not 30 <= quality <= 100:
        raise ApiError(422, "bad_quality", "请求无效。请刷新页面后重试。", {"detail": "品质要在 30 到 100 之间。"})
    edit, side = _read_edit(c, shot)
    edit = _merge(edit, body)
    if body is not None and "crop" in body.model_fields_set:
        side = {**side, "crop": body.crop}
    use_stage = stage or ("final" if "dng" not in shot["files"] or edit.lut is not None else None)
    if use_stage:
        try:
            jpeg, labels = render_stage(c.render, c.lib, shot, edit, side, stage=use_stage, max_edge=max_edge, quality=quality)
        except RenderUnavailable as exc:
            raise _unavailable(c, exc) from exc
        except NoRaw as exc:
            raise ApiError(409, "no_raw", str(exc) or "配方需要 RAW 文件。拍摄时将画质设为 RAW+JPEG。") from exc
        except EditError as exc:
            raise _edit_error(exc) from exc
        except RawDecodeError as exc:
            raise ApiError(422, "raw_unreadable", "这份 RAW 暂时读不出来。请换一张照片或重新导入后再试。") from exc
        headers = {"Cache-Control": "private, no-cache", "X-GXR-Cache": labels.get("cache") or "miss"}
        if use_stage == "uncropped" and labels.get("valid"):
            headers["X-GXR-Valid-Rect"] = labels["valid"]
        if use_stage == "final":
            machine = labels.get("label") if labels.get("label") in {"prediction", "approx", "original"} else "original"
            headers["X-GXR-Label"] = quote(str(machine))
            headers["X-GXR-Calibrated"] = "1" if labels.get("calibrated") == "1" else "0"
            if machine == "prediction":
                headers["X-GXR-Prediction"] = "1"
            elif machine == "approx":
                headers["X-GXR-Look"] = "approx"
        return Response(jpeg, media_type="image/jpeg", headers=headers)
    try:
        crop = _crop_of(side) if apply_crop else None
        out = c.render.render_preview(shot, edit, max_edge=max_edge, exact=exact, crop=crop, quality=quality)
    except RenderUnavailable as exc:
        raise _unavailable(c, exc) from exc
    except NoRaw as exc:
        raise ApiError(409, "no_raw", "配方需要 RAW 文件。拍摄时将画质设为 RAW+JPEG。") from exc
    except EditError as exc:
        raise _edit_error(exc) from exc
    except RawDecodeError as exc:
        raise ApiError(422, "raw_unreadable", "无法读取 RAW 文件。文件可能已损坏。", {"detail": str(exc)}) from exc
    except CropError as exc:
        raise ApiError(422, exc.code, exc.message) from exc
    except ValueError as exc:  # 例如色表行不对称：冻结模型不能预测这个条目
        raise ApiError(422, "cannot_predict", "无法生成电脑预测。请重试。", {"detail": str(exc)}) from exc
    etag = f'"{out.key}"'
    headers = {
        "ETag": etag,
        "Cache-Control": "private, no-cache",
        "X-GXR-Prediction": "1",
        "X-GXR-Calibrated": "1" if out.plan.calibrated else "0",
        "X-GXR-Label": "prediction",
        "X-GXR-Render-Key": out.key,
        "X-GXR-Cache": "hit" if out.cache_hit else "miss",
        "X-GXR-Timing": ";".join(f"{k}={v:.1f}" for k, v in out.ms.items()),
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(out.jpeg, media_type="image/jpeg", headers=headers)


# ---- 状态与配方列表 ------------------------------------------------------------


@router.get("/api/render/status", response_model=None)
def render_status(request: Request) -> Any:
    return ctx(request).render.status()


@router.get("/api/recipes", response_model=None)
def list_recipes(request: Request) -> dict[Any, Any]:
    """3 个单元 × 8 个底子，以及每个底子有哪些旋钮、每个旋钮能取哪些档位（含到顶标记）。没导入相机数据也能列。"""
    svc = ctx(request).render
    status = svc.status()
    return {
        "status": status,
        "units": svc.catalog(),
        "note": "",
    }


# ---- sidecar 里的编辑 -----------------------------------------------------------


@router.get("/api/photos/{shot_id}/edit", response_model=None)
def get_edit(shot_id: str, request: Request) -> Any:
    c = ctx(request)
    shot = _shot_or_404(c, shot_id)
    edit, side = _read_edit(c, shot)
    return _payload(c, shot, edit, side, c.lib.sidecar_problem(shot))


@router.put("/api/photos/{shot_id}/edit", response_model=None)
def put_edit(shot_id: str, body: EditRequest, request: Request) -> Any:
    c = ctx(request)
    shot = _shot_or_404(c, shot_id)
    changes = {k: getattr(body, k) for k in ("recipe", "lut", "adjust", "curve", "geometry", "frame", "stamp") if k in body.model_fields_set}
    warning = c.lib.sidecar_problem(shot)
    try:
        if changes.get("lut") is not None:
            try:
                target, _item = c.render.luts.target(changes["lut"])
            except LutError as exc:
                raise ApiError(exc.status, exc.code, exc.message, exc.extra) from exc
            if target.input_space not in DISPLAY and "dng" not in shot["files"]:
                raise EditError("lut_needs_raw", "这个 LUT 需要 RAW 文件")
        edit, side = render_edits.write(c.lib, shot, changes, c.render.recipe_menu(), expected_rev=body.expected_rev)
    except EditError as exc:
        raise _edit_error(exc) from exc
    except RevisionConflict as exc:
        raise ApiError(409, "rev_conflict", "这张照片已在别处修改。请刷新后重试。", {"detail": f"现在是第 {exc.current} 版"}) from exc
    except sc.SidecarTooNew as exc:
        raise ApiError(409, "sidecar_too_new", "这张照片的编辑来自更新的版本。请更新 GXR Studio。") from exc
    except sc.SidecarCorrupt as exc:
        raise _sidecar_unreadable() from exc
    return _payload(c, shot, edit, side, warning)


@router.delete("/api/photos/{shot_id}/edit", response_model=None)
def delete_edit(shot_id: str, request: Request) -> Any:
    c = ctx(request)
    shot = _shot_or_404(c, shot_id)
    warning = c.lib.sidecar_problem(shot)
    try:
        edit, side = render_edits.write(
            c.lib,
            shot,
            {"recipe": None, "lut": None, "adjust": None, "curve": None, "geometry": None, "frame": None, "stamp": None},
            c.render.menu,
        )
    except sc.SidecarTooNew as exc:
        raise ApiError(409, "sidecar_too_new", "这张照片的编辑来自更新的版本。请更新 GXR Studio。") from exc
    except sc.SidecarCorrupt as exc:
        raise _sidecar_unreadable() from exc
    return _payload(c, shot, edit, side, warning)


# ---- 预览图 ---------------------------------------------------------------------


@router.get("/api/photos/{shot_id}/render", response_model=None)
def get_render(
    shot_id: str,
    request: Request,
    max_edge: int | None = None,
    exact: bool = False,
    crop: bool = False,
    quality: int = 88,
    stage: str | None = None,
    size: str | None = None,
) -> Any:
    """按 sidecar 里的编辑渲染。`stage=final` 是成片，`uncropped` 是裁剪页的整幅画面。"""
    return _render(request, shot_id, None, max_edge, exact, crop, quality, stage, size)


@router.post("/api/photos/{shot_id}/render", response_model=None)
def post_render(
    shot_id: str,
    body: RenderRequest,
    request: Request,
    max_edge: int | None = None,
    exact: bool = False,
    crop: bool = False,
    quality: int = 88,
    stage: str | None = None,
    size: str | None = None,
) -> Any:
    """请求体里出现的块盖过 sidecar，不落盘。拖动滑块时用这个。"""
    return _render(request, shot_id, body, max_edge, exact, crop, quality, stage, size)
