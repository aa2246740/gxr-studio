"""FastAPI 应用工厂。路由在 `api/routes.py`，这里只装配：设置、图库、任务表、审计、安全中间件。"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from . import __version__, audit, privacy
from .alerts import StudioNotifier, register_alert_service
from .api.camdata_routes import router as camdata_router
from .api.errors import ApiError
from .api.finish_routes import router as finish_router
from .api.gpu_routes import router as gpu_router
from .api.lut_library_routes import router as lut_library_router
from .api.lut_routes import router as lut_router
from .api.ops_routes import router as ops_router
from .api.prefs_routes import router as prefs_router
from .api.render_routes import router as render_router
from .api.routes import router
from .camdata import store as camdata_store
from .cards.discovery import CardReader
from .cards.importer import ImportOptions
from .cards.session import SessionRegistry
from .config import Settings
from .export import ExportLedger
from .jobs import JobRegistry
from .library.location import card_roots
from .library.sidecar import SidecarCorrupt
from .library.store import SIDECAR_RESET_NOTE, Library
from .lut.service import LutService
from .observe import Metrics, ObserveMiddleware
from .render.service import RenderService
from .security import SecurityMiddleware
from .tracking import init_error_tracking, shutdown_error_tracking


@dataclass
class Context:
    settings: Settings
    lib: Library
    jobs: JobRegistry
    recorder: audit.AuditRecorder
    render: RenderService
    lut: LutService
    reader: CardReader = field(default_factory=CardReader)
    import_options: ImportOptions | None = None  # 测试可以注入（空间、钩子）
    import_lock: threading.Lock = field(default_factory=threading.Lock)
    camdata_lock: threading.Lock = field(default_factory=threading.Lock)  # 相机数据/标定包同一时间只有一个在导入
    hdr_lock: threading.Lock = field(default_factory=threading.Lock)  # HDR 后台任务同一时间只有一个
    hdr_stopping: bool = False  # 与启动共用 hdr_lock；关闭开始后不能再创建 HDR 任务
    metrics: Metrics = field(default_factory=Metrics)
    alerts: StudioNotifier = field(default_factory=StudioNotifier)
    exports: ExportLedger = field(default_factory=ExportLedger)
    sessions: SessionRegistry = field(default_factory=SessionRegistry)  # 挂载会话：接口里的 card_id


class NoCacheStaticFiles(StaticFiles):
    """前端资源跟着这次启动的页面走，不让浏览器按 URL 一直用旧的 app.js。"""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(
    settings: Settings,
    *,
    reader: CardReader | None = None,
    import_options: ImportOptions | None = None,
    sessions: SessionRegistry | None = None,
) -> FastAPI:
    # 上次换相机数据/标定包到一半被打断：先恢复（按已核对的目录 fd；目录不合格就不碰）
    camdata_store.recover_interrupted_swaps(settings.home_dir, privacy.forbidden_for(settings))
    init_error_tracking()
    # 第一次写图库之前，先只读找出插着的卡；图库或 .studio 解析到卡上（或图库外面）就拒绝启动（复审 A1a R3-P1-01）
    lib = Library(settings.library_dir, card_roots(settings.volumes_roots)).open()
    recorder = audit.AuditRecorder(limit=5000)
    recorder.__enter__()  # 应用存活期间一直记录写入
    render = RenderService(settings.home_dir, lib, forbidden=tuple(privacy.forbidden_for(settings)))
    jobs = JobRegistry()
    metrics = Metrics()
    alerts = StudioNotifier()
    register_alert_service(jobs, alerts)
    ctx = Context(
        settings,
        lib,
        jobs,
        recorder,
        render,
        LutService(settings.home_dir, lib, render),
        reader or CardReader(),
        import_options,
        metrics=metrics,
        alerts=alerts,
        sessions=sessions or SessionRegistry(),
    )
    privacy.startup_check(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        yield
        with ctx.hdr_lock:
            ctx.hdr_stopping = True
            hdr_jobs = [job for job in jobs.all() if job.kind == "hdr"]
            for job in hdr_jobs:
                job.cancel.set()
        for job in hdr_jobs:
            if job.thread is not None:
                job.thread.join()
        shutdown_error_tracking()
        ctx.lut.close()
        recorder.__exit__(None, None, None)
        lib.close()

    app = FastAPI(title="GXR Studio", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx

    @app.exception_handler(ApiError)
    async def _api_error(_: Any, exc: ApiError) -> Any:
        return JSONResponse({"error": exc.code, "message": exc.message, **exc.extra}, status_code=exc.status)

    @app.exception_handler(SidecarCorrupt)
    async def _sidecar_corrupt(_: Any, exc: SidecarCorrupt) -> Any:
        # 兜底：编辑记录读不出来时不给 500，也不把路径和解析错误交给界面
        return JSONResponse({"error": "sidecar_corrupt", "message": SIDECAR_RESET_NOTE}, status_code=422)

    @app.exception_handler(privacy.PrivatePathError)
    async def _private_path_error(_: Any, exc: privacy.PrivatePathError) -> Any:
        return JSONResponse({"error": exc.code, "message": exc.message, "detail": exc.detail}, status_code=422)

    app.include_router(router)
    app.include_router(prefs_router)
    app.include_router(ops_router)
    app.include_router(render_router)
    app.include_router(finish_router)
    app.include_router(camdata_router)
    app.include_router(lut_router)
    app.include_router(lut_library_router)
    app.include_router(gpu_router)
    web = settings.web_dir
    if web is not None and Path(web, "assets").is_dir():
        app.mount("/assets", NoCacheStaticFiles(directory=Path(web) / "assets"), name="assets")
    app.add_middleware(ObserveMiddleware, metrics=metrics)
    app.add_middleware(SecurityMiddleware, settings=settings)
    return app
