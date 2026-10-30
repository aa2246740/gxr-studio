"""M1 的 HTTP 接口。业务逻辑都在 cards/ library/ export/ 里，这里只做参数校验和翻译。"""

from __future__ import annotations

import shutil
import threading
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from .. import __version__
from .. import crop as cropmod
from ..cards import importer
from ..cards.discovery import Card, find_cards
from ..crop import Crop, CropError
from ..export import ExportError, ExportOptions, export_shot
from ..library import edits, thumbs
from ..library import sidecar as sc
from ..raw import NEUTRAL
from ..render import edits as render_edits
from ..render.params import EditError, EditState
from ..render.service import RenderUnavailable
from .errors import ApiError

router = APIRouter()
TEMP_HOME = Path(__file__).with_name("temp_home.html")
EXPORT_SIDECAR_NOTE = "这张照片的编辑记录坏了或不见了，导出时按没有裁切、没有编辑处理；照片本身没事。"
DENIED_NOTE = (
    "电脑不让工作室读取外接的存储卡。请打开「系统设置 → 隐私与安全性 → 文件和文件夹」，"
    "给运行工作室的「终端」打开「可移除的卷」（或在「完全磁盘访问权限」里打开终端），然后把卡拔出来再插一次。"
)


def ctx(request: Request):
    return request.app.state.ctx


# ---- 请求体 -----------------------------------------------------------------


class ImportRequest(BaseModel):
    card_id: str
    shift_seconds: int = Field(0, ge=-366 * 86400, le=366 * 86400)
    force_verify: bool = False


class CropRequest(BaseModel):
    preset: str | None = None
    rect: list[float] | None = None
    angle_deg: float = 0.0
    flip: bool = False
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


class BatchExportRequest(ExportRequest):
    ids: list[str] | None = None
    date: str | None = None


# ---- 小工具 -----------------------------------------------------------------


def _cards(c) -> list[Card]:
    return find_cards(c.settings.volumes_roots, c.reader)


def _card_or_404(c, card_id: str) -> Card:
    card = next((x for x in _cards(c) if x.key == card_id), None)
    if card is None:
        raise ApiError(404, "card_not_found", "找不到这张卡（是不是已经拔出了？）")
    return card


def _shot_or_404(c, shot_id: str) -> dict:
    shot = c.lib.get_shot(shot_id)
    if shot is None:
        raise ApiError(404, "photo_not_found", "找不到这张照片")
    return shot


def photo_json(shot: dict, data_version: str | None = None, render=None, lib=None) -> dict:
    """`data_version`（相机数据 + 标定包的版本）只加在没有 JPG 的照片的图片链接上：它们的缩略图随相机数据重画，
    链接变了浏览器缓存的旧图才不会被继续用（图片响应是 immutable 缓存）。
    `preview_look` 说明默认画面是什么（相机 JPG / 原厂 04 电脑模拟=电脑预测 / 中性转换），界面据此标注。"""
    sid = shot["shot_id"]
    vq = f"?v={data_version}" if data_version and "jpg" not in shot["files"] else ""
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
        "thumb_url": f"/api/photos/{sid}/thumb{vq}",
        "preview_url": f"/api/photos/{sid}/preview{vq}",
        "preview_kind": thumbs.preview_kind(shot),
        "preview_look": thumbs.look_of(shot, render, lib),
        "crop": shot["crop"],
        "sidecar_rev": shot["sidecar_rev"],
    }


def _crop_error(exc: CropError) -> ApiError:
    return ApiError(422, exc.code, exc.message)


def _crop_for_export(c, shot: dict) -> Crop | None:
    side, _ = c.lib.read_sidecar_or_default(shot)   # 编辑记录坏了/不见了：按没有裁切导出
    raw = side.get("crop")
    return Crop(raw["preset"], tuple(raw["rect"]), float(raw.get("angle_deg", 0.0))) if raw else None


def _forbidden_roots(c) -> list[Path]:
    return [card.root for card in _cards(c)]


def _export_source(c, shot: dict, req: ExportRequest) -> tuple[str, EditState]:
    """决定这张照片从哪里出图：'recipe' | 'camera-jpg' | 'neutral'，以及它的编辑状态。"""
    files = shot["files"]
    try:
        edit, _ = render_edits.read(c.lib, shot)
    except (sc.SidecarCorrupt, EditError):
        edit = EditState()  # 编辑读不出来不挡导出；退回没有编辑
    if req.source not in ("auto", "recipe", "camera-jpg", "neutral"):
        raise ApiError(422, "bad_source", "画面来源只能是 auto / recipe / camera-jpg / neutral")
    want = req.source
    if want == "auto":
        edited = edit.recipe is not None or not edit.adjust.is_identity or edit.master is not None
        if "dng" in files and (edited or "jpg" not in files) and c.render.available():
            want = "recipe"
        elif "jpg" in files:
            want = "camera-jpg"
        else:
            want = "neutral"
    if want == "recipe" and "dng" not in files:
        raise ApiError(409, "no_raw", "这张照片没有 RAW，只有相机 JPG，不能套配方。")
    if want == "camera-jpg" and "jpg" not in files:
        raise ApiError(409, "no_jpg", "这张照片没有配对的相机 JPG。")
    if want == "neutral" and "dng" not in files:
        raise ApiError(409, "no_raw", "这张照片没有 RAW，不能出中性转换。")
    return want, edit


def _export_one(c, shot: dict, req: ExportRequest) -> dict:
    dest = Path(req.dest_dir).expanduser() if req.dest_dir else c.settings.export_dir
    if not dest.is_absolute():
        raise ApiError(422, "bad_destination", "导出文件夹要写完整路径")
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
        renderer, use_raw = NEUTRAL, want == "neutral"
        if want == "recipe":
            renderer, use_raw = c.render.renderer(shot, edit, precise=req.precise), True
        res = export_shot(c.lib, shot, _crop_for_export(c, shot), dest, opts, _forbidden_roots(c), renderer, use_raw)
    except ExportError as exc:
        raise ApiError(409 if exc.code in ("no_jpg_source", "raw_unreadable") else 422, exc.code, exc.message) from exc
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
        "render_label": res.render_label,
        "prediction": want == "recipe",
        "calibrated": renderer.calibrated if want == "recipe" else None,
        **({"warning": warning} if warning else {}),
    }


# ---- 页面与基础 -------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    web = ctx(request).settings.web_dir
    if web is not None and Path(web, "index.html").is_file():
        return HTMLResponse(Path(web, "index.html").read_text(encoding="utf-8"))
    return HTMLResponse(TEMP_HOME.read_text(encoding="utf-8"))


@router.get("/api/health")
def health():
    return {"ok": True, "version": __version__}


@router.get("/api/settings")
def get_settings(request: Request):
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
    }


@router.get("/api/audit")
def get_audit(request: Request):
    rec = ctx(request).recorder
    return {"write_count": len(rec.writes), "recent": [{"op": w.op, "path": w.path, "blocked": w.blocked} for w in rec.writes[-100:]]}


# ---- 卡与导入 ---------------------------------------------------------------


@router.get("/api/cards")
def list_cards(request: Request):
    return [{"id": card.key, "label": card.label} for card in _cards(ctx(request))]


@router.get("/api/cards/status")
def cards_status(request: Request):
    """卡没找到时用：是不是因为系统不让读（macOS 没给「可移除的卷」权限）。"""
    c = ctx(request)
    denied: list[str] = []
    cards = find_cards(c.settings.volumes_roots, c.reader, denied)
    return {"cards": len(cards), "access_denied": bool(denied) and not cards,
            "message": DENIED_NOTE if denied and not cards else None}


@router.get("/api/cards/{card_id}/summary")
def card_summary(card_id: str, request: Request):
    c = ctx(request)
    card = _card_or_404(c, card_id)
    return {"id": card.key, "label": card.label, **importer.card_summary(c.lib, card, c.reader)}


@router.post("/api/import", status_code=202)
def start_import(body: ImportRequest, request: Request):
    c = ctx(request)
    card = _card_or_404(c, body.card_id)
    with c.import_lock:
        if c.jobs.active("import"):
            raise ApiError(409, "import_running", "已经有一个导入在进行了")
        base = c.import_options or importer.ImportOptions(min_free_bytes=c.settings.min_free_bytes)
        opts = replace(
            base,
            shift_seconds=body.shift_seconds,
            force_verify=body.force_verify,
            undated_policy=c.settings.undated_policy,
        )
        progress = importer.ImportProgress()

        def work(job):
            importer.run_import(c.lib, card, opts, progress, job.cancel, c.reader)

        job = c.jobs.start("import", progress, work)
    return job.to_dict()


@router.get("/api/jobs")
def list_jobs(request: Request):
    return [j.to_dict() for j in ctx(request).jobs.all()]


@router.get("/api/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    job = ctx(request).jobs.get(job_id)
    if job is None:
        raise ApiError(404, "job_not_found", "没有这个任务")
    return job.to_dict()


@router.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request):
    job = ctx(request).jobs.get(job_id)
    if job is None:
        raise ApiError(404, "job_not_found", "没有这个任务")
    job.cancel.set()
    return job.to_dict()


# ---- 图库 -------------------------------------------------------------------


@router.get("/api/dates")
def get_dates(request: Request):
    return ctx(request).lib.dates()


@router.get("/api/photos")
def list_photos(request: Request, date: str | None = None, limit: int = 500, offset: int = 0):
    c = ctx(request)
    limit = max(1, min(limit, 5000))
    version = c.render.data_version()
    return [photo_json(s, version, c.render, c.lib) for s in c.lib.list_shots(date, limit, max(0, offset))]


@router.get("/api/photos/{shot_id}")
def get_photo(shot_id: str, request: Request):
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
        raise ApiError(404, "no_image", "这张照片读不出图像")
    headers = {"Cache-Control": "private, max-age=31536000, immutable"}
    if path.stem.endswith("-" + thumbs.EMBEDDED_VARIANT):
        headers.update({"X-GXR-Look": "embedded", "X-GXR-Label": quote(thumbs.EMBEDDED_LABEL)})
    else:
        look = thumbs.look_of(shot, c.render)       # 这张图不是内嵌的，所以不传 lib
        if look["kind"] != "camera-jpg":
            headers.update({"X-GXR-Look": look["kind"], "X-GXR-Label": quote(look["label"])})
        if look["prediction"]:
            headers["X-GXR-Prediction"] = "1"
    return Response(data, media_type="image/jpeg", headers=headers)


@router.get("/api/photos/{shot_id}/thumb")
def photo_thumb(shot_id: str, request: Request):
    return _image(request, shot_id, thumbs.THUMB_SIZE)


@router.get("/api/photos/{shot_id}/preview")
def photo_preview(shot_id: str, request: Request):
    return _image(request, shot_id, thumbs.PREVIEW_SIZE)


@router.post("/api/library/rebuild")
def rebuild_index(request: Request, verify: bool = False):
    c = ctx(request)
    if c.jobs.active("import"):
        raise ApiError(409, "import_running", "导入进行中，稍后再重建索引")
    rep = c.lib.rebuild(verify=verify)
    return {"shots": rep.shots, "duplicates": rep.duplicates, "hashed": rep.hashed,
            "mismatches": rep.mismatches, "seen_restored": rep.seen_restored,
            "sidecars_created": rep.sidecars_created, "corrupt_backups": rep.corrupt_backups}


# ---- 裁切 -------------------------------------------------------------------


@router.get("/api/photos/{shot_id}/crop")
def get_crop(shot_id: str, request: Request):
    c = ctx(request)
    shot = _shot_or_404(c, shot_id)
    try:
        return edits.read_crop(c.lib, shot)
    except CropError as exc:
        raise _crop_error(exc) from exc


def _put_crop(request: Request, shot_id: str, body: CropRequest | None, reset: bool = False) -> dict:
    c = ctx(request)
    shot = _shot_or_404(c, shot_id)
    warning = c.lib.sidecar_problem(shot)
    try:
        out = edits.write_crop(
            c.lib,
            shot,
            preset=body.preset if body else None,
            rect=body.rect if body else None,
            angle_deg=body.angle_deg if body else 0.0,
            flip=body.flip if body else False,
            reset=reset,
            expected_rev=body.expected_rev if body else None,
        )
    except CropError as exc:
        raise _crop_error(exc) from exc
    except edits.RevisionConflict as exc:
        raise ApiError(409, "rev_conflict", f"这张照片的编辑已经被改过（现在是第 {exc.current} 版），请刷新后再试") from exc
    except sc.SidecarTooNew as exc:
        raise ApiError(409, "sidecar_too_new", "这张照片的编辑文件来自更新的版本，只读") from exc
    return {**out, "warning": warning} if warning else out


@router.put("/api/photos/{shot_id}/crop")
def put_crop(shot_id: str, body: CropRequest, request: Request):
    return _put_crop(request, shot_id, body)


@router.delete("/api/photos/{shot_id}/crop")
def delete_crop(shot_id: str, request: Request):
    return _put_crop(request, shot_id, None, reset=True)


@router.get("/api/crop-presets")
def crop_presets():
    return [{"id": k, "ratio": list(v) if v else None} for k, v in cropmod.PRESETS.items()]


# ---- 导出 -------------------------------------------------------------------


@router.post("/api/photos/{shot_id}/export")
def export_photo(shot_id: str, body: ExportRequest, request: Request):
    c = ctx(request)
    return _export_one(c, _shot_or_404(c, shot_id), body)


@router.post("/api/export", status_code=202)
def export_batch(body: BatchExportRequest, request: Request):
    c = ctx(request)
    missing: list[str] = []     # 勾选之后照片 id 变了/被删了：这几张记为没导出，其余照常导
    if body.ids is not None:
        shots = []
        for i in dict.fromkeys(body.ids):
            shot = c.lib.get_shot(i)
            if shot is None:
                missing.append(i)
            else:
                shots.append(shot)
    elif body.date:
        shots = c.lib.list_shots(body.date, limit=100000)
    else:
        raise ApiError(422, "nothing_to_export", "要给 ids 或 date")

    class Progress:
        def __init__(self):
            self.state, self.total, self.done = "running", len(shots) + len(missing), len(missing)
            self.results: list[dict] = [
                {"id": i, "error": "photo_not_found",
                 "message": "这张照片已经不在图库里了（可能刚补全了另一半、id 变了，或被删掉），没有导出；请刷新后重新选一下"}
                for i in missing
            ]
            self._lock = threading.Lock()

        def to_dict(self):
            with self._lock:
                return {"state": self.state, "total": self.total, "done": self.done, "results": list(self.results)}

    progress = Progress()

    def work(job):
        for shot in shots:
            if job.cancel.is_set():
                progress.state = "cancelled"
                return
            try:
                item = _export_one(c, shot, body)
            except ApiError as exc:
                item = {"id": shot["shot_id"], "error": exc.code, "message": exc.message}
            except Exception as exc:  # 一张坏了不要拖垮整批；提示用白话，异常原文（可能带路径）不给界面
                item = {"id": shot["shot_id"], "error": "failed", "code": type(exc).__name__,
                        "message": f"这张没导出成功，其余的不受影响（错误代码：{type(exc).__name__}）。照片本身没有被改动。"}
            with progress._lock:
                progress.results.append(item)
                progress.done += 1
        progress.state = "done"

    return c.jobs.start("export", progress, work).to_dict()
