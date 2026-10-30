"""健康之外的运维接口：指标、本地功能使用计数、告警。都走同一套令牌。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

router = APIRouter()


def _ctx(request: Request):
    return request.app.state.ctx


@router.get("/api/metrics")
def metrics(request: Request) -> Response:
    return Response(_ctx(request).metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/api/usage")
def usage(request: Request) -> dict:
    return _ctx(request).metrics.usage()


@router.get("/api/alerts")
def alerts(request: Request) -> dict:
    return {"alerts": _ctx(request).alerts.recent()}
