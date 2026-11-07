"""随机交错测试（edit_fuzz.mjs）用的测试专用路由，装在真服务器上。

写编辑、写框先调用真正的处理函数（路由、sidecar、收框、校验都是真的），再用真代码检查写下去的框：
- `fit` 写进去的框：落在有效区里（`warp_valid` + `window_full`）；不比请求里的目标大（收只会缩小，太小的目标除外）；
  中心保留（目标中心处放同样大小的框如果放得下，结果的中心就必须在那里，差不超过 2 像素）；
  收得不比必要的更小（定比例的预设：下一档整数倍放不放得下；自由、原始：放大 2% 放不放得下）。
- 写完以后（`stored-check`）存下来的框必须通得过严格校验 `validate_crop(..., mirrored=flip)`。
- 点比例以外，服务器自己算的居中框（`rect` 为 null 的写入）有几次。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request

from gxr_studio import crop as cropmod
from gxr_studio.api import render_routes, routes
from gxr_studio.crop import CropError
from gxr_studio.finish.geometry import _place_ratio, output_size, warp_valid, window_full
from gxr_studio.finish.spec import Geometry, parse_geometry

STATS: dict[str, int] = {}  # 照片 id → 服务器自己算框的写入次数（多个 node 进程各用一张照片并行跑）


def _side(request: Request, shot_id: str) -> tuple[dict[Any, Any], Any]:
    c = routes.ctx(request)
    shot = routes._shot_or_404(c, shot_id)
    side, _ = c.lib.read_sidecar_or_default(shot)
    return side, shot


def _geometry(side: dict[Any, Any]) -> Geometry:
    raw = side.get("geometry")
    return parse_geometry(raw) if isinstance(raw, dict) else Geometry()


def _canvas(side: dict[Any, Any]) -> tuple[int, int, int, int, Geometry]:
    frame = side.get("frame") or {}
    sw, sh = int(frame["width"]), int(frame["height"])
    geom = _geometry(side)
    w, h = output_size(sw, sh, geom)
    return sw, sh, w, h, geom


def stored_problems(side: dict[Any, Any]) -> list[str]:
    raw = side.get("crop")
    if not isinstance(raw, dict):
        return []
    sw, sh, w, h, geom = _canvas(side)
    try:
        cropmod.validate_crop(
            raw["preset"], raw["rect"], float(raw.get("angle_deg", 0.0)), w, h, mirrored=geom.flip, check_region=not geom.expands()
        )
        if geom.expands():  # 拉开过的画布：有效区是真正的蒙版，框必须整个落在里面
            x, y, bw, bh = cropmod.crop_box(tuple(raw["rect"]), w, h)  # type: ignore[arg-type]
            valid = warp_valid(sw, sh, geom)
            if not (window_full(valid, x, y, bw, bh) or window_full(valid, x + 1, y + 1, max(1, bw - 2), max(1, bh - 2))):
                return [f"存下来的框不在有效区里：{raw['preset']} {raw['rect']} 几何 {side.get('geometry')}"]
    except CropError as exc:
        return [f"存下来的框通不过 validate_crop：{exc.code}（{raw['preset']} {raw['rect']} 画布 {w}×{h} 几何 {side.get('geometry')}）"]
    return []


def fit_problems(side: dict[Any, Any], target: list[float], preset: str, result: list[float]) -> list[str]:
    sw, sh, w, h, geom = _canvas(side)
    tx, ty, tw, th = (float(v) for v in target)
    if not (0 <= tx and 0 <= ty and tx + tw <= 1 + 1e-9 and ty + th <= 1 + 1e-9) or tw * w < 40 or th * h < 40:
        return []  # 服务器会先把目标整理一下（放进画面、放大太小的），这里只查正常的目标
    valid = warp_valid(sw, sh, geom)
    x, y, bw, bh = cropmod.crop_box(tuple(result), w, h)  # type: ignore[arg-type]
    out: list[str] = []
    if not (window_full(valid, x, y, bw, bh) or window_full(valid, x + 1, y + 1, max(1, bw - 2), max(1, bh - 2))):
        out.append(f"fit 的结果不在有效区里：{result}")
    cx, cy = (tx + tw / 2) * w, (ty + th / 2) * h
    hx = min(max(round(cx - bw / 2), 0), max(0, w - bw))
    hy = min(max(round(cy - bh / 2), 0), max(0, h - bh))
    if window_full(valid, hx, hy, bw, bh) and (abs(x - hx) > 2 or abs(y - hy) > 2):
        out.append(f"fit 把中心移开了：目标中心 ({cx:.0f},{cy:.0f}) 同样大小放得下，结果在 ({x + bw / 2:.0f},{y + bh / 2:.0f})")
    limit_w, limit_h = min(w, max(1, round(tw * w))), min(h, max(1, round(th * h)))
    if bw > limit_w + 2 or bh > limit_h + 2:  # 收只会缩小：结果比请求里的目标还大，说明目标被放大过头了
        out.append(f"fit 把框放大了：请求的目标是 {limit_w}×{limit_h}，结果是 {bw}×{bh}")
    base = cropmod.PRESETS.get(preset)
    if base is not None and preset != "original":
        ratio = cropmod.target_ratio(preset, w, h)
        if ratio is not None:
            if (tw * w >= th * h) != (ratio >= 1):
                ratio = 1 / ratio
            n, d = ratio.numerator, ratio.denominator
            k = bw // n
            if k >= 1 and bw == k * n and bh == k * d and limit_w >= (k + 1) * n and limit_h >= (k + 1) * d:
                bigger = _place_ratio(k + 1, n, d, cx, cy, w, h)
                if bigger is not None and window_full(valid, *bigger):
                    out.append(f"fit 收得比必要的小：{preset} 的 {k + 1} 倍（{(k + 1) * n}×{(k + 1) * d}）放得下，结果只有 {bw}×{bh}")
    else:
        w2, h2 = round(bw * 1.02) + 1, round(bh * 1.02) + 1
        if w2 <= limit_w and h2 <= limit_h:
            bx = min(max(round(cx - w2 / 2), 0), max(0, w - w2))
            by = min(max(round(cy - h2 / 2), 0), max(0, h - h2))
            if window_full(valid, bx, by, w2, h2):
                out.append(f"fit 收得比必要的小：放大 2%（{w2}×{h2}）放得下，结果只有 {bw}×{bh}")
    return out


def install(app: FastAPI) -> None:
    def put_edit(shot_id: str, body: render_routes.EditRequest, request: Request) -> dict[str, Any]:
        resp = render_routes.put_edit(shot_id, body, request)
        return {"resp": resp, "problems": []}

    def put_crop(shot_id: str, body: routes.CropRequest, request: Request) -> dict[str, Any]:
        resp = routes.put_crop(shot_id, body, request)
        problems: list[str] = []
        if body.rect is None:
            STATS[shot_id] = STATS.get(shot_id, 0) + 1
        elif body.fit and body.preset:
            side, _ = _side(request, shot_id)
            raw = side.get("crop") or {}
            problems = fit_problems(side, list(body.rect), body.preset, list(raw.get("rect") or resp["rect"]))
        return {"resp": resp, "problems": problems}

    def stored_check(shot_id: str, request: Request) -> dict[str, Any]:
        side, _ = _side(request, shot_id)
        return {"problems": stored_problems(side)}

    def frame(body: dict[str, Any], request: Request) -> dict[str, int]:
        """这套几何下的画布宽高（真的 output_size）：界面的预览画布就是按这个出的。"""
        side, _ = _side(request, body["shot_id"])
        sw, sh = int(side["frame"]["width"]), int(side["frame"]["height"])
        w, h = output_size(sw, sh, parse_geometry(body["geometry"]))
        return {"width": w, "height": h}

    def stats(shot_id: str) -> dict[str, int]:
        return {"null_crop_writes": STATS.get(shot_id, 0)}

    def stats_reset(shot_id: str) -> dict[str, int]:
        STATS[shot_id] = 0
        return {"null_crop_writes": 0}

    app.add_api_route("/__test/photos/{shot_id}/edit", put_edit, methods=["PUT"])
    app.add_api_route("/__test/photos/{shot_id}/crop", put_crop, methods=["PUT"])
    app.add_api_route("/__test/photos/{shot_id}/stored-check", stored_check, methods=["GET"])
    app.add_api_route("/__test/frame", frame, methods=["POST"])
    app.add_api_route("/__test/photos/{shot_id}/stats", stats, methods=["GET"])
    app.add_api_route("/__test/photos/{shot_id}/stats/reset", stats_reset, methods=["POST"])
