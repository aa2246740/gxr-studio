"""相机数据与标定包的导入接口（第一次使用时用）。业务逻辑在 `camdata/intake.py`。

- `GET  /api/camdata/status`          是否已导入、来源（卡/文件 SHA-256/代数）、三个单元是否齐全、标定状态、插着的卡里哪张有 GXRCE.DAT；
- `POST /api/camdata/import-card`     从插着的卡上只读取出 GXRCE.DAT 并导入（卡不会被写）；
- `POST /api/camdata/import-file`     从本地文件（或装着它的文件夹）导入，没有卡时用；
- `POST /api/calibration/import`      从本地文件夹导入标定包（需要先导入相机数据）。

导入成功后渲染缓存清空、没有 JPG 的照片的缩略图缓存删除；`data_version` 变了，照片链接里的 `?v=` 跟着变。
理光字节只写到应用数据目录。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from ..camdata import intake
from ..library import thumbs
from .errors import ApiError
from .routes import ctx

router = APIRouter()


class ImportCardRequest(BaseModel):
    card_id: str | None = None  # 插着不止一张带 GXRCE.DAT 的卡时才需要


class PathRequest(BaseModel):
    path: str


def _api(exc: intake.IntakeError) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message, exc.extra)


def _project_dir() -> str | None:
    """工作室所在的项目文件夹（studio/ 的上一级，里面有 research/ 才算）。导入颜色标定时预填，免得手敲路径。"""
    root = Path(__file__).resolve().parents[4]
    return str(root) if (root / "research").is_dir() else None


def _status(c: Any) -> dict[Any, Any]:
    home = c.settings.home_dir
    rs = c.render.status()
    cam, cal = intake.describe(home), intake.describe_calibration(home)
    cards = [
        {"id": s.card.key, "label": s.card.label, "has_dat": s.has_dat, "dat_size": s.dat_size}
        for s in intake.scan_cards_for_dat(c.settings.volumes_roots, c.reader)
    ]
    if rs["camera_data"]["state"] != "ready":
        nxt, why = "import_camera_data", rs["camera_data"]["message"]
    elif rs["calibration"]["state"] == "missing":
        nxt, why = "import_calibration", "请先导入校准数据"
    elif rs["calibration"]["state"] != "ready":
        nxt, why = "import_calibration", rs["calibration"]["message"]
    else:
        nxt, why = None, None
    return {
        "ready": rs["ready"],
        "camera_data": {
            **rs["camera_data"],
            "imported": cam is not None,
            "source": cam["source"] if cam else None,
            "units": cam["units"] if cam else None,
            "all_units_complete": cam["all_units_complete"] if cam else False,
        },
        "calibration": {
            **rs["calibration"],
            "imported": cal is not None,
            "imported_isos": cal["isos"] if cal else [],
            "imported_at": cal["imported_at"] if cal else None,
            "origin": cal["origin"] if cal else None,
        },
        "cards": cards,
        "data_version": c.render.data_version(),
        "project_dir": _project_dir(),
        "next": nxt,
        "next_message": why,
        "hint": rs["hint"],
    }


def _install(c: Any, raw: bytes, name: str, origin: dict[Any, Any]) -> dict[Any, Any]:
    """校验（不过就原样保留旧数据）→ 与已导入的相同就不动 → 否则在渲染锁里替换并让缓存失效。"""
    try:
        info = intake.inspect_dat(raw)
        home = c.settings.home_dir
        with c.camdata_lock:
            unchanged = intake.existing_source_sha(home) == info.sha256
            removed = 0
            if not unchanged:
                c.render.replace_data(lambda: intake.install_dat(raw, home, source_name=name, origin=origin))
                removed = thumbs.purge_variants(c.lib)
    except intake.IntakeError as exc:
        raise _api(exc) from exc
    status = _status(c)
    where = f"存储卡「{origin['label']}」" if origin.get("kind") == "card" else "你选的文件"
    msg = (
        f"相机配方文件已经是最新的（{where}，第 {info.generation} 代），没有改动"
        if unchanged
        else f"已从{where}导入相机配方文件：第 {info.generation} 代，3 个单元 × 8 个内置配方。"
    )
    if status["next"] == "import_calibration":
        msg += " 还需要导入校准数据才能使用配方。"
    return {
        "imported": not unchanged,
        "unchanged": unchanged,
        "generation": info.generation,
        "sha256": info.sha256,
        "message": msg,
        "invalidated": {"render_cache": not unchanged, "thumbnails_removed": removed},
        "status": status,
    }


@router.get("/api/camdata/status", response_model=None)
def camdata_status(request: Request) -> Any:
    return _status(ctx(request))


@router.post("/api/camdata/import-card", response_model=None)
def import_from_card(request: Request, body: ImportCardRequest | None = None) -> Any:
    c = ctx(request)
    scans = intake.scan_cards_for_dat(c.settings.volumes_roots, c.reader)
    if body and body.card_id:
        pick = next((s for s in scans if s.card.key == body.card_id), None)
        if pick is None:
            raise ApiError(404, "card_not_found", "找不到存储卡。请重新插入后重试。")
    else:
        with_dat = [s for s in scans if s.has_dat]
        if len(with_dat) > 1:
            raise ApiError(
                409,
                "multiple_cards",
                "插着不止一张带 GXRCE.DAT 的存储卡。请选一张。",
                {"cards": [{"id": s.card.key, "label": s.card.label} for s in with_dat]},
            )
        if not with_dat:
            if not scans:
                raise ApiError(
                    404,
                    "no_card",
                    "找不到存储卡。请重新插入后重试。",
                )
            raise ApiError(
                404,
                "dat_not_found",
                "存储卡上没有相机配方文件（GXRCE.DAT）",
            )
        pick = with_dat[0]
    if not pick.has_dat:
        raise ApiError(404, "dat_not_found", "存储卡上没有相机配方文件（GXRCE.DAT）")
    try:
        raw = intake.read_dat_from_card(c.reader, pick)
    except intake.IntakeError as exc:
        raise _api(exc) from exc
    return _install(c, raw, pick.dat_path.name, {"kind": "card", "label": pick.card.label})  # type: ignore[union-attr]


@router.post("/api/camdata/import-file", response_model=None)
def import_from_file(body: PathRequest, request: Request) -> Any:
    c = ctx(request)
    try:
        raw, name = intake.read_dat_from_path(Path(body.path))
    except intake.IntakeError as exc:
        raise _api(exc) from exc
    return _install(c, raw, name, {"kind": "file"})


@router.post("/api/camdata/import-upload", response_model=None)
async def import_from_upload(request: Request) -> Any:
    """浏览器的文件选择器拿不到本地路径：界面把选中的 GXRCE.DAT 的原始字节直接 POST 过来（文件名放在
    `X-GXR-Filename`，只用来显示）。校验、替换和 `import-file` 完全相同；超过上限边读边拒绝。"""
    c = ctx(request)
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > intake.MAX_DAT_BYTES:
            raise ApiError(
                422,
                "dat_invalid",
                "无法导入相机配方文件。文件过大。",
                {"detail": f"超过 {intake.MAX_DAT_BYTES // 1024} KB"},
            )
        chunks.append(chunk)
    name = Path(request.headers.get("x-gxr-filename") or intake.DAT_NAME).name or intake.DAT_NAME
    return await run_in_threadpool(_install, c, b"".join(chunks), name, {"kind": "file"})


@router.post("/api/calibration/import", response_model=None)
def import_calibration(body: PathRequest, request: Request) -> dict[Any, Any]:
    c = ctx(request)
    home = c.settings.home_dir
    try:
        with c.camdata_lock:
            _, kind = c.render.replace_data(lambda: intake.import_calibration_folder(home, Path(body.path)))
            removed = thumbs.purge_variants(c.lib)
    except intake.IntakeError as exc:
        raise _api(exc) from exc
    status = _status(c)
    msg = "已导入校准数据" + ("（开发机研究目录）" if kind == "dev" else "") + "，ISO 200 和 ISO 1600 两套。"
    return {
        "imported": True,
        "kind": kind,
        "message": msg,
        "invalidated": {"render_cache": True, "thumbnails_removed": removed},
        "status": status,
    }
