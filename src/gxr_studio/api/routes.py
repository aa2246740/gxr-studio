"""M1 的 HTTP 接口。业务逻辑都在 cards/ library/ export/ 里，这里只做参数校验和翻译。"""

from __future__ import annotations

import shutil
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from .. import __version__
from .. import crop as cropmod
from ..cards import coverage, importer
from ..cards.coverage import Identity
from ..cards.discovery import find_cards
from ..cards.session import MountSession, SessionError
from ..crop import Crop, CropError
from ..export import ExportError, ExportOptions, RevealError, check_destination, reveal_export
from ..flags import flag_enabled
from ..folder_picker import FolderPickerError, choose_export_folder
from ..hdr import derived as hdr_derived
from ..library import edits, thumbs
from ..library import sidecar as sc
from ..render import edits as render_edits
from ..render.camera_recipe import recipe_tag_payload
from ..render.params import EditError, EditState
from ..render.service import RenderUnavailable
from .errors import ApiError

router = APIRouter()
TEMP_HOME = Path(__file__).with_name("temp_home.html")
_UNSET_DEFAULTS = "0"  # nosec B105  默认装饰还没保存过时的版本号，不是口令
EXPORT_SIDECAR_NOTE = "无法读取编辑记录，已按原图显示"
HDR_UNSUPPORTED = "这张照片不支持 HDR"
HDR_NOTES = {"no_raw": "HDR 需要 RAW 文件", "not_jpeg": "HDR 需要 JPEG 格式", "capture_mismatch": "RAW 与 JPG 不是同一次拍摄"}
DENIED_NOTE = "需要访问存储卡的权限。请在“系统设置 › 隐私与安全性 › 文件与文件夹”中允许。"


def ctx(request: Request) -> Any:
    return request.app.state.ctx


# ---- 请求体 -----------------------------------------------------------------


class ImportRequest(BaseModel):
    card_id: str
    shift_seconds: int = Field(0, ge=-366 * 86400, le=366 * 86400)
    force_verify: bool = False


class CropRequest(BaseModel):
    preset: str | None = None
    rect: list[float] | None = None
    angle_deg: float | None = None  # 不传就保持 sidecar 里的角度，不归零
    flip: bool = False
    orientation: str | None = None
    fit: bool = False  # true：rect 是目标，服务器收成离它最近的合格框（比例、有效区），不拒绝。撤销、重做、转 90° 之后的框用
    expected_rev: int | None = None


class ExportRequest(BaseModel):
    long_edge: int | None = None  # 3000 / 2048 / 1600 / null（原始）
    quality: int = 95
    format: str = "jpeg"  # jpeg | png
    size: str | None = None  # 尺寸档 large/medium/small/original，给了就盖过 long_edge
    quality_tier: str | None = None  # 质量档 high/medium/low，给了就盖过 quality
    dest_dir: str | None = None
    # 画面来源：auto = 有编辑（配方/调整/曲线）或只有 RAW 时从 RAW 渲染（电脑预测），否则用相机 JPG；
    # recipe = 一定从 RAW 渲染；camera-jpg = 一定用相机 JPG；neutral = RAW 中性转换（没有配方）
    source: str = "auto"
    precise: bool = False  # recipe 时用逐像素精确链（很慢），默认用烘焙表
    hdr: bool = False  # 带增益图的 HDR JPEG（按成片重新算）；做不了的照片照常导出，结果里 hdr=false 并给出 hdr_note


class ExportFile(BaseModel):
    """单张导出的结果。render_label 是机器值，render_text 是给人看的字。"""

    id: str
    path: str
    width: int
    height: int
    bytes: int
    format: str
    render: str
    render_label: Literal["prediction", "approx", "original"]
    render_text: str
    prediction: bool
    calibrated: bool | None = None
    export_id: str | None = None
    detail: str | None = None
    warning: str | None = None
    hdr: bool | None = None  # 没要 HDR 时为空
    hdr_note: str | None = None


class BatchExportRequest(ExportRequest):
    ids: list[str] | None = None
    date: str | None = None


class ExportDestinationRequest(BaseModel):
    dest_dir: str | None = None


# ---- 小工具 -----------------------------------------------------------------


def _cards(c: Any) -> list[MountSession]:
    """发现到的卡，每张对应一个挂载会话。"""
    return cast(list[MountSession], c.sessions.attach_all(find_cards(c.settings.volumes_roots, c.reader)))


def resolve_session(c: Any, card_id: str, *, write: bool = False) -> MountSession:
    """所有带 card_id 的接口共用（设计 §4.1）：格式不对 400，作废 404，写入时身份歧义 409。"""
    _cards(c)
    try:
        return cast(MountSession, c.sessions.resolve(card_id, write=write))
    except SessionError as exc:
        raise ApiError(exc.status, exc.code, exc.message) from exc


def identity_of(session: MountSession) -> Identity:
    """导入和照片核对用的卷身份：UUID 缺失或歧义时不关联任何历史；带上会话的完整指纹核验（复审 A1a P1-02）。"""
    fp = session.fingerprint
    return Identity(session.history_uuid, (fp.root_dev, fp.root_ino), verify=session.still_valid)


def _shot_or_404(c: Any, shot_id: str) -> dict[Any, Any]:
    shot = cast(dict[str, Any] | None, c.lib.get_shot(shot_id))
    if shot is None:
        raise ApiError(404, "photo_not_found", "找不到这张照片。请刷新页面后重试。")
    return shot


def photo_json(shot: dict[Any, Any], data_version: str | None = None, render: Any = None, lib: Any = None) -> dict[Any, Any]:
    """`data_version`（相机数据 + 标定包的版本）只加在没有 JPG 的照片的图片链接上：它们的缩略图随相机数据重画，
    链接变了浏览器缓存的旧图才不会被继续用（图片响应是 immutable 缓存）。
    `preview_look` 说明默认画面是什么（相机 JPG / 原厂 04 电脑模拟=电脑预测 / 中性转换），界面据此标注。"""
    sid = shot["shot_id"]
    tag = shot.get("recipe_tag")
    rev = int(shot.get("sidecar_rev") or 0)
    token = _UNSET_DEFAULTS
    if render is not None:
        from ..finish.serve import defaults_token

        token = defaults_token(render.home)
    if data_version and "jpg" not in shot["files"]:
        vq = f"?v={data_version}" + (f"&r={rev}" if rev else "")
    elif rev:
        vq = f"?v={rev}"
    else:
        vq = ""
    if token != _UNSET_DEFAULTS:
        vq = f"{vq}&d={token}" if vq else f"?d={token}"
    if render is not None and lib is not None:
        side, _problem = lib.read_sidecar_or_default(shot)
        lut_id = side.get("lut")
        if isinstance(lut_id, str):
            lut_token = render.luts.render_token(lut_id)
            vq = f"{vq}&l={lut_token}" if vq else f"?l={lut_token}"
    return {
        "id": sid,
        "name": shot["name"],
        "date": shot["date"],
        "date_source": shot["date_source"],
        "captured_at": shot["captured_at"],
        "has_raw": bool(shot["has_raw"]),
        "has_jpg": bool(shot["has_jpg"]),
        "iso": shot["iso"],
        "lens": shot["lens"],
        "unit": shot["unit"],
        "width": shot["width"],
        "height": shot["height"],
        "orientation": shot["orientation"],
        "copyright": shot.get("copyright"),
        "recipe_tag": recipe_tag_payload(tag, render.recipe_menu() if render is not None and tag else None),
        "thumb_url": f"/api/photos/{sid}/thumb{vq}",
        "preview_url": f"/api/photos/{sid}/preview{vq}",
        "preview_kind": thumbs.preview_kind(shot),
        "preview_look": thumbs.look_of(shot, render, lib),
        "hdr": hdr_derived.status_of(lib, shot) if lib is not None else None,  # ready / unsupported / pending / none（没有 RAW 或 JPG）
        "crop": shot["crop"],
        "sidecar_rev": shot["sidecar_rev"],
    }


def _crop_error(exc: CropError) -> ApiError:
    extra = {"detail": exc.detail} if exc.detail else None
    return ApiError(422, exc.code, exc.message, extra)


def _crop_for_export(c: Any, shot: dict[Any, Any]) -> Crop | None:
    side, _ = c.lib.read_sidecar_or_default(shot)  # 编辑记录坏了/不见了：按没有裁切导出
    raw = side.get("crop")
    return Crop(raw["preset"], tuple(raw["rect"]), float(raw.get("angle_deg", 0.0))) if raw else None


def _forbidden_roots(c: Any) -> list[Path]:
    return [s.root for s in _cards(c)]


def _export_source(c: Any, shot: dict[Any, Any], req: ExportRequest) -> tuple[str, EditState]:
    """决定这张照片从哪里出图：'recipe' | 'camera-jpg' | 'neutral'，以及它的编辑状态。"""
    files = shot["files"]
    try:
        edit, _ = render_edits.read(c.lib, shot, c.render.recipe_menu() if req.source in ("auto", "recipe") else None)
    except EditError as exc:
        if exc.code.startswith("camera_"):
            raise ApiError(409, exc.code, exc.message) from exc
        edit = EditState()
    except sc.SidecarCorrupt:
        edit = EditState()  # 编辑读不出来不挡导出；退回没有编辑
    if req.source not in ("auto", "recipe", "camera-jpg", "neutral"):
        raise ApiError(422, "bad_source", "请求无效。请刷新页面后重试。", {"detail": "画面来源无法识别。"})
    # auto 交给成片流水线：选了配方用 RAW，否则有相机 JPG 就用相机 JPG，只有 RAW 再用 04 标准。
    want = req.source
    if want == "recipe" and "dng" not in files and edit.recipe is None:
        raise ApiError(409, "no_raw", "配方需要 RAW 文件。拍摄时将画质设为 RAW+JPEG。")
    if want == "camera-jpg" and "jpg" not in files:
        raise ApiError(409, "no_jpg", "无法导出相机 JPG。这张照片没有相机 JPG。")
    if want == "neutral" and "dng" not in files:
        raise ApiError(409, "no_raw", "无法导出中性转换。这张照片没有 RAW 文件。")
    return want, edit


def _needs_finish_export(edit: EditState) -> bool:
    """边框、时间戳、透视、畸变、旋转、翻转和四项新调整走成片，不再走旧的裁剪导出。"""
    geom = edit.geometry
    if geom is not None and (geom.expands() or geom.rotate90 or geom.flip):
        return True
    if edit.frame is not None and edit.frame.style != "none":
        return True
    if edit.stamp is not None and edit.stamp.enabled:
        return True
    return any(getattr(edit.adjust, name) for name in ("black_point", "saturation", "vibrance", "vignette"))


def _export_one(c: Any, shot: dict[Any, Any], req: ExportRequest) -> dict[Any, Any]:
    dest = Path(req.dest_dir).expanduser() if req.dest_dir else c.settings.export_dir
    if not dest.is_absolute():
        raise ApiError(422, "bad_destination", "无法导出。请填写导出到的完整路径。")
    want, edit = _export_source(c, shot, req)
    warning = EXPORT_SIDECAR_NOTE if c.lib.sidecar_problem(shot) else None
    try:
        opts = ExportOptions(req.long_edge, req.quality, req.format)
        if req.size is not None or req.quality_tier is not None:
            tiers = ExportOptions.from_tiers(req.size, req.quality_tier, req.format)
            opts = ExportOptions(
                tiers.long_edge if req.size is not None else req.long_edge,
                tiers.quality if req.quality_tier is not None else req.quality,
                req.format,
            )
        if want == "recipe" and "dng" not in shot["files"] and edit.recipe is None:
            raise ApiError(409, "no_raw", "配方需要 RAW 文件。拍摄时将画质设为 RAW+JPEG。")
        from ..finish.serve import export_finish

        side, _problem = c.lib.read_sidecar_or_default(shot)
        res = export_finish(
            c.render,
            c.lib,
            shot,
            edit,
            side,
            dest,
            _forbidden_roots(c),
            quality=opts.quality,
            long_edge=opts.long_edge,
            fmt=opts.fmt,
            base=want,
            hdr=req.hdr,
        )
    except ExportError as exc:
        extra = {"detail": exc.detail} if exc.detail else None
        raise ApiError(409 if exc.code in ("no_jpg_source", "raw_unreadable") else 422, exc.code, exc.message, extra) from exc
    except RenderUnavailable as exc:
        raise ApiError(409, exc.code, exc.message, {"status": c.render.status()}) from exc
    except EditError as exc:
        raise ApiError(422, exc.code, exc.message) from exc
    return {
        "id": shot["shot_id"],
        "path": str(res.path),
        "width": res.width,
        "height": res.height,
        "bytes": res.bytes,
        "format": res.format,
        "render": res.render,
        "render_label": res.render_kind,
        "render_text": res.render_label,
        "prediction": res.render_kind == "prediction",
        "calibrated": res.calibrated,
        **({"detail": res.detail} if res.detail else {}),
        **({"warning": warning} if warning else {}),
        **({"hdr": res.hdr} if res.hdr is not None else {}),
        **({"hdr_note": HDR_NOTES.get(res.hdr_reason or "", HDR_UNSUPPORTED)} if res.hdr is False else {}),
    }


def _remember_export(c: Any, export_id: str, item: dict[Any, Any]) -> dict[Any, Any]:
    path = Path(item["path"])
    c.exports.note(export_id, path.parent, path)
    return {**item, "export_id": export_id}


# ---- 页面与基础 -------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def home(request: Request) -> Any:
    web = ctx(request).settings.web_dir
    if web is not None and Path(web, "index.html").is_file():
        return HTMLResponse(Path(web, "index.html").read_text(encoding="utf-8"))
    return HTMLResponse(TEMP_HOME.read_text(encoding="utf-8"))


@router.get("/api/health", response_model=None)
def health() -> dict[Any, Any]:
    return {"ok": True, "version": __version__}


@router.get("/api/settings", response_model=None)
def get_settings(request: Request) -> dict[Any, Any]:
    c = ctx(request)
    s = c.settings
    return {
        "library_dir": str(s.library_dir),
        "export_dir": str(s.export_dir),
        "home_dir": str(s.home_dir),
        "undated_policy": s.undated_policy,
        "min_free_bytes": s.min_free_bytes,
        "free_bytes": shutil.disk_usage(c.lib.root).free,
        "photo_count": c.lib.count(),
        # 前端据此决定要不要走显卡预览；关着就不请求 /gpu
        "gpu_preview": flag_enabled(c.settings, "gpu_preview"),
    }


@router.get("/api/audit", response_model=None)
def get_audit(request: Request) -> dict[Any, Any]:
    rec = ctx(request).recorder
    return {
        "write_count": len(rec.writes),
        "recent": [{"op": w.op, "path": w.path, "blocked": w.blocked} for w in rec.writes[-100:]],
    }


# ---- 卡与导入 ---------------------------------------------------------------


@router.get("/api/cards", response_model=None)
def list_cards(request: Request) -> list[Any]:
    return [s.public() for s in _cards(ctx(request))]


@router.get("/api/cards/status", response_model=None)
def cards_status(request: Request) -> dict[Any, Any]:
    """卡没找到时用：是不是因为系统不让读（macOS 没给「可移除的卷」权限）。"""
    c = ctx(request)
    denied: list[str] = []
    cards = find_cards(c.settings.volumes_roots, c.reader, denied)
    return {
        "cards": len(cards),
        "access_denied": bool(denied) and not cards,
        "message": DENIED_NOTE if denied and not cards else None,
    }


@router.get("/api/cards/{card_id}/summary", response_model=None)
def card_summary(card_id: str, request: Request) -> dict[Any, Any]:
    c = ctx(request)
    session = resolve_session(c, card_id)
    return {**session.public(), **coverage.card_summary(c.lib, session.card, c.reader, identity_of(session))}


@router.post("/api/import", status_code=202, response_model=None)
def start_import(body: ImportRequest, request: Request) -> Any:
    c = ctx(request)
    session = resolve_session(c, body.card_id)
    card, ident = session.card, identity_of(session)
    with c.import_lock:
        if c.jobs.active("import"):
            raise ApiError(409, "import_running", "无法开始导入。已经有一个导入在进行。")
        base = c.import_options or importer.ImportOptions(min_free_bytes=c.settings.min_free_bytes)
        opts = replace(
            base,
            shift_seconds=body.shift_seconds,
            force_verify=body.force_verify,
            undated_policy=c.settings.undated_policy,
        )
        progress = importer.ImportProgress(card_id=session.session_id)

        def work(job: Any) -> None:
            try:
                importer.run_import(c.lib, card, opts, progress, job.cancel, c.reader, ident)
            finally:
                start_hdr_job(c)  # 导入的结果和"都已导入"的判断这时已经定了；HDR 另起一个任务

        job = c.jobs.start("import", progress, work)
    return job.to_dict()


def start_hdr_job(c: Any) -> None:
    """导入结束后在后台给还没做过的 RAW+JPG 照片生成 HDR 版本。正在跑的那一轮做完会再看一次有没有新的。"""
    if not flag_enabled(c.settings, "hdr_on_import"):
        return
    with c.hdr_lock:
        if c.hdr_stopping or c.jobs.active("hdr"):
            return
        progress = hdr_derived.HdrProgress()
        c.jobs.start("hdr", progress, lambda job: hdr_derived.run_pending(c.lib, progress, job.cancel))


@router.get("/api/jobs", response_model=None)
def list_jobs(request: Request) -> list[Any]:
    return [j.to_dict() for j in ctx(request).jobs.all()]


@router.get("/api/jobs/{job_id}", response_model=None)
def get_job(job_id: str, request: Request) -> Any:
    job = ctx(request).jobs.get(job_id)
    if job is None:
        raise ApiError(404, "job_not_found", "找不到这个任务。请刷新页面后重试。")
    return job.to_dict()


@router.post("/api/jobs/{job_id}/cancel", response_model=None)
def cancel_job(job_id: str, request: Request) -> Any:
    job = ctx(request).jobs.get(job_id)
    if job is None:
        raise ApiError(404, "job_not_found", "找不到这个任务。请刷新页面后重试。")
    job.cancel.set()
    return job.to_dict()


# ---- 图库 -------------------------------------------------------------------


@router.get("/api/dates", response_model=None)
def get_dates(request: Request) -> Any:
    return ctx(request).lib.dates()


@router.get("/api/photos", response_model=None)
def list_photos(request: Request, date: str | None = None, limit: int = 500, offset: int = 0) -> list[Any]:
    c = ctx(request)
    limit = max(1, min(limit, 5000))
    version = c.render.data_version()
    return [photo_json(s, version, c.render, c.lib) for s in c.lib.list_shots(date, limit, max(0, offset))]


@router.get("/api/photos/{shot_id}", response_model=None)
def get_photo(shot_id: str, request: Request) -> Any:
    c = ctx(request)
    return photo_json(_shot_or_404(c, shot_id), c.render.data_version(), c.render, c.lib)


def _image(request: Request, shot_id: str, size: int) -> Response:
    c = ctx(request)
    shot = _shot_or_404(c, shot_id)
    # 导入相机数据/标定包会 purge_variants 删缓存；ensure 返回路径后文件可能马上被删，所以立刻把字节读进内存
    # （不用 FileResponse：它到发送时才打开文件），读不到就重画一次。
    path = data = None
    for _ in range(4):
        path = thumbs.ensure(c.lib, shot, size, c.render)
        if path is None:
            break
        try:
            data = path.read_bytes()
            break
        except FileNotFoundError:
            continue
    if path is None or data is None:
        raise ApiError(404, "no_image", "无法读取这张照片。文件可能已损坏。")
    tail = path.stem.rsplit("-", 1)[-1]
    mutable = tail.startswith("e") and any(ch.isdigit() for ch in tail)
    headers = {"Cache-Control": "private, no-cache" if mutable else "private, max-age=31536000, immutable"}
    if path.stem.endswith("-" + thumbs.EMBEDDED_VARIANT):
        headers.update({"X-GXR-Look": "embedded", "X-GXR-Label": quote(thumbs.EMBEDDED_LABEL)})
    else:
        look = thumbs.look_of(shot, c.render)  # 这张图不是内嵌的，所以不传 lib
        if look["kind"] != "camera-jpg":
            headers.update({"X-GXR-Look": look["kind"], "X-GXR-Label": quote(look["label"])})
        if look["prediction"]:
            headers["X-GXR-Prediction"] = "1"
    return Response(data, media_type="image/jpeg", headers=headers)


@router.get("/api/photos/{shot_id}/thumb", response_model=None)
def photo_thumb(shot_id: str, request: Request) -> Any:
    return _image(request, shot_id, thumbs.THUMB_SIZE)


@router.get("/api/photos/{shot_id}/preview", response_model=None)
def photo_preview(shot_id: str, request: Request) -> Any:
    return _image(request, shot_id, thumbs.PREVIEW_SIZE)


@router.post("/api/library/rebuild", response_model=None)
def rebuild_index(request: Request, verify: bool = False) -> dict[Any, Any]:
    c = ctx(request)
    if not flag_enabled(c.settings, "library_rebuild"):
        raise ApiError(403, "feature_disabled", "无法重建索引。这个功能已关闭。")
    if c.jobs.active("import"):
        raise ApiError(409, "import_running", "无法重建索引。请等导入完成后再试。")
    rep = c.lib.rebuild(verify=verify)
    return {
        "shots": rep.shots,
        "duplicates": rep.duplicates,
        "hashed": rep.hashed,
        "mismatches": rep.mismatches,
        "seen_restored": rep.seen_restored,
        "sidecars_created": rep.sidecars_created,
        "corrupt_backups": rep.corrupt_backups,
    }


# ---- 裁切 -------------------------------------------------------------------


@router.get("/api/photos/{shot_id}/crop", response_model=None)
def get_crop(shot_id: str, request: Request) -> Any:
    c = ctx(request)
    shot = _shot_or_404(c, shot_id)
    try:
        return edits.read_crop(c.lib, shot)
    except CropError as exc:
        raise _crop_error(exc) from exc


def _put_crop(request: Request, shot_id: str, body: CropRequest | None, reset: bool = False) -> dict[Any, Any]:
    c = ctx(request)
    shot = _shot_or_404(c, shot_id)
    warning = c.lib.sidecar_problem(shot)
    try:
        flip = body.flip if body else False
        angle = body.angle_deg if body is not None and "angle_deg" in body.model_fields_set else None
        # 横竖相对于几何变换之后的画布（转过 90° 以后横图是竖的），在 write_crop 里按那块画布换成 flip
        orientation = body.orientation if body and body.orientation in ("landscape", "portrait") else None
        out = edits.write_crop(
            c.lib,
            shot,
            preset=body.preset if body else None,
            rect=body.rect if body else None,
            angle_deg=angle,
            flip=flip,
            orientation=orientation,
            reset=reset,
            expected_rev=body.expected_rev if body else None,
            fit=bool(body and body.fit),
        )
    except CropError as exc:
        raise _crop_error(exc) from exc
    except edits.RevisionConflict as exc:
        raise ApiError(409, "rev_conflict", "这张照片已在别处修改。请刷新后重试。", {"detail": f"现在是第 {exc.current} 版"}) from exc
    except sc.SidecarTooNew as exc:
        raise ApiError(409, "sidecar_too_new", "这张照片的编辑来自更新的版本。请更新 GXR Studio。") from exc
    return {**out, "warning": warning} if warning else out


@router.put("/api/photos/{shot_id}/crop", response_model=None)
def put_crop(shot_id: str, body: CropRequest, request: Request) -> Any:
    return _put_crop(request, shot_id, body)


@router.delete("/api/photos/{shot_id}/crop", response_model=None)
def delete_crop(shot_id: str, request: Request) -> Any:
    return _put_crop(request, shot_id, None, reset=True)


@router.get("/api/crop-presets", response_model=None)
def crop_presets() -> list[Any]:
    return [{"id": k, "ratio": list(v) if v else None} for k, v in cropmod.PRESETS.items()]


# ---- 导出 -------------------------------------------------------------------


@router.post("/api/export/destination", response_model=None)
def pick_export_destination(body: ExportDestinationRequest, request: Request) -> dict[str, str | bool | None]:
    """显式点击才弹出系统选择器；选定路径仍经导出的卡卷/图库检查，导出时再核对写入权限。"""
    c = ctx(request)
    default = Path(body.dest_dir).expanduser() if body.dest_dir else c.settings.export_dir
    if not default.is_absolute():
        default = c.settings.export_dir
    try:
        chosen = choose_export_folder(default)
        if chosen is None:
            return {"cancelled": True, "path": None}
        dest = check_destination(chosen, [*_forbidden_roots(c), c.lib.root])
    except FolderPickerError as exc:
        raise ApiError(exc.status, exc.code, exc.message) from exc
    except ExportError as exc:
        raise ApiError(422, exc.code, exc.message, {"detail": exc.detail}) from exc
    return {"cancelled": False, "path": str(dest)}


@router.post("/api/photos/{shot_id}/export", response_model=ExportFile)
def export_photo(shot_id: str, body: ExportRequest, request: Request) -> Any:
    c = ctx(request)
    item = _export_one(c, _shot_or_404(c, shot_id), body)
    path = Path(item["path"])
    item["export_id"] = c.exports.note_new(path.parent, path)
    return item


@router.post("/api/export", status_code=202, response_model=None)
def export_batch(body: BatchExportRequest, request: Request) -> Any:
    c = ctx(request)
    missing: list[str] = []  # 勾选之后照片 id 变了/被删了：这几张记为没导出，其余照常导
    if body.ids is not None:
        shots = []
        for i in dict[Any, Any].fromkeys(body.ids):
            shot = c.lib.get_shot(i)
            if shot is None:
                missing.append(i)
            else:
                shots.append(shot)
    elif body.date:
        shots = c.lib.list_shots(body.date, limit=100000)
    else:
        raise ApiError(422, "nothing_to_export", "请求无效。请刷新页面后重试。", {"detail": "要给出照片或日期"})

    class Progress:
        def __init__(self) -> None:
            self.state, self.total, self.done = "running", len(shots) + len(missing), len(missing)
            self.results: list[dict[Any, Any]] = [
                {
                    "id": i,
                    "error": "photo_not_found",
                    "message": "找不到这张照片。请刷新后重新选择。",
                }
                for i in missing
            ]
            self._lock = threading.Lock()

        def to_dict(self) -> dict[Any, Any] | None:
            with self._lock:
                return {"state": self.state, "total": self.total, "done": self.done, "results": list(self.results)}

    progress = Progress()

    def work(job: Any) -> None:
        for shot in shots:
            if job.cancel.is_set():
                progress.state = "cancelled"
                return
            try:
                item = _remember_export(c, job.id, _export_one(c, shot, body))
            except ApiError as exc:
                item = {"id": shot["shot_id"], "error": exc.code, "message": exc.message}
                if exc.extra.get("detail"):
                    item["detail"] = exc.extra["detail"]
            except Exception as exc:  # 一张坏了不要拖垮整批；提示用白话，异常原文（可能带路径）不给界面
                item = {
                    "id": shot["shot_id"],
                    "error": "failed",
                    "code": type(exc).__name__,
                    "message": "导出失败。请重试。",
                }
            with progress._lock:
                progress.results.append(item)
                progress.done += 1
        progress.state = "done"

    return c.jobs.start("export", progress, work).to_dict()


@router.post("/api/exports/{export_id}/reveal", response_model=None)
def reveal_exported(export_id: str, request: Request) -> dict[str, Any]:
    """在访达中显示这次导出的第一张。不接受路径参数，只打开本进程记下的文件。"""
    found = ctx(request).exports.first(export_id)
    if found is None:
        raise ApiError(404, "export_not_found", "找不到这次导出。请重新导出后再试。")
    root, path, ident = found
    try:
        shown = reveal_export(root, path, ident)
    except RevealError as exc:
        raise ApiError(exc.status, exc.code, exc.message) from exc
    return {"ok": True, "path": str(shown)}
