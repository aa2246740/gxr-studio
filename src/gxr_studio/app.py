"""FastAPI 应用工厂。路由在 `api/routes.py`，这里只装配：设置、图库、任务表、审计、安全中间件。"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, audit
from .alerts import StudioNotifier, register_alert_service
from .api.camdata_routes import router as camdata_router
from .api.errors import ApiError
from .api.lut_routes import router as lut_router
from .api.ops_routes import router as ops_router
from .api.render_routes import router as render_router
from .api.routes import router
from .camdata import store as camdata_store
from .cards.discovery import CardReader
from .cards.importer import ImportOptions
from .config import Settings
from .jobs import JobRegistry
from .library.sidecar import SidecarCorrupt
from .library.store import SIDECAR_RESET_NOTE, Library
from .lut.service import LutService
from .observe import Metrics, ObserveMiddleware
from .render.service import RenderService
from .security import SecurityMiddleware


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
    metrics: Metrics = field(default_factory=Metrics)
    alerts: StudioNotifier = field(default_factory=StudioNotifier)


def create_app(
    settings: Settings,
    *,
    reader: CardReader | None = None,
    import_options: ImportOptions | None = None,
) -> FastAPI:
    camdata_store.recover_interrupted_swaps(settings.home_dir)  # 上次换相机数据/标定包到一半被打断：先恢复
    lib = Library(settings.library_dir).open()
    recorder = audit.AuditRecorder(limit=5000)
    recorder.__enter__()  # 应用存活期间一直记录写入
    render = RenderService(settings.home_dir, lib)
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
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        ctx.lut.close()
        recorder.__exit__(None, None, None)
        lib.close()

    app = FastAPI(title="GXR Studio", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx

    @app.exception_handler(ApiError)
    async def _api_error(_, exc: ApiError):
        return JSONResponse({"error": exc.code, "message": exc.message, **exc.extra}, status_code=exc.status)

    @app.exception_handler(SidecarCorrupt)
    async def _sidecar_corrupt(_, exc: SidecarCorrupt):
        # 兜底：编辑记录读不出来时不给 500，也不把路径和解析错误交给界面
        return JSONResponse({"error": "sidecar_corrupt", "message": SIDECAR_RESET_NOTE}, status_code=422)

    app.include_router(router)
    app.include_router(ops_router)
    app.include_router(render_router)
    app.include_router(camdata_router)
    app.include_router(lut_router)
    web = settings.web_dir
    if web is not None and Path(web, "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=Path(web) / "assets"), name="assets")
    app.add_middleware(ObserveMiddleware, metrics=metrics)
    app.add_middleware(SecurityMiddleware, settings=settings)
    return app
