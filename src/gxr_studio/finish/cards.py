"""边框标签里样式卡片的缩略图（`size=card`）。只服务卡片预览，导出和大图不走这里。

一张照片有六七种边框样式，卡片每种都要一张小图。逐张走完整成片管线，每张都要重新出色、转几何、
盖日期戳、加边框，再缩小；这里把「边框之前」的部分只做一次（缩到 BASE_EDGE 长边，按照片和编辑缓存），
每种样式只做 `apply_frame` 和缩小。边框的尺寸都是照片边长的比例，BASE_EDGE 以上与全尺寸成片的版式一致。
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..library.store import Library
from ..render.params import EditState
from ..render.service import RenderService
from . import serve
from .compose import encode_jpeg, finish_unframed, resolved_frame, resolved_stamp
from .frame import apply_frame
from .spec import StampSpec

CARD_EDGE = 240  # 卡片图的长边（含边框）
BASE_EDGE = 1200  # 边框之前的底图长边
KEEP = 6  # 最多缓存几张照片的底图（每张约 3 MB）
TTL_SECONDS = 300.0  # 缓存键盖不到的变化（例如换了同名 LUT 的内容）最迟这么久后失效


@dataclass(frozen=True)
class CardBase:
    rgb: np.ndarray
    meta: dict[str, str]
    labels: dict[str, str]


_lock = threading.Lock()
_cache: OrderedDict[str, tuple[float, CardBase]] = OrderedDict()
_flights: dict[str, threading.Lock] = {}


def clear() -> None:
    with _lock:
        _cache.clear()


def _key(render: RenderService, shot: dict[str, Any], edit: EditState, side: dict[str, Any], stamp: StampSpec) -> str | None:
    """底图的缓存键：照片文件、颜色编辑、几何、裁切、日期戳和相机数据版本。边框样式不在里面。"""
    lut = ""
    if edit.lut is not None:
        try:
            lut = render.luts.target_and_token(edit.lut, persist=False)[2]
        except Exception:  # noqa: BLE001  LUT 读不出来就不缓存，真正出图时会报出原因
            return None
    parts = [
        shot.get("shot_id"),
        shot.get("files"),
        shot.get("orientation"),
        edit.canonical(),
        repr(edit.geometry),
        repr(stamp),
        side.get("crop") if isinstance(side.get("crop"), dict) else None,
        lut,
        render.data_version(),
    ]
    return json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))


def _build(
    render: RenderService, lib: Library, shot: dict[str, Any], edit: EditState, side: dict[str, Any], stamp: StampSpec, quality: int
) -> CardBase:
    rgb, from_raw, labels = serve.color_image(render, shot, edit, max_edge=BASE_EDGE, quality=quality, lib=lib, proxy_edge=BASE_EDGE)
    meta, taken, file_time = serve._meta(shot, lib)
    side_crop = side.get("crop")
    crop_raw: dict[str, Any] = side_crop if isinstance(side_crop, dict) else {}
    raw_rect = crop_raw.get("rect")
    crop_rect = None
    if isinstance(raw_rect, list) and len(raw_rect) == 4:
        crop_rect = (float(raw_rect[0]), float(raw_rect[1]), float(raw_rect[2]), float(raw_rect[3]))
    preset = crop_raw.get("preset")
    rgb, _ = finish_unframed(
        rgb,
        edit,
        stage="final",
        stamp=stamp,
        crop_rect=crop_rect,
        taken=taken,
        file_time=file_time,
        from_raw=from_raw,
        crop_preset=preset if isinstance(preset, str) else None,
    )
    labels.setdefault("label", labels.get("kind") or "original")
    labels.setdefault("text", "原图")
    return CardBase(rgb, meta, labels)


def _base(
    render: RenderService, lib: Library, shot: dict[str, Any], edit: EditState, side: dict[str, Any], stamp: StampSpec, quality: int
) -> CardBase:
    key = _key(render, shot, edit, side, stamp)
    if key is None:
        return _build(render, lib, shot, edit, side, stamp, quality)
    with _lock:
        hit = _cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < TTL_SECONDS:
            _cache.move_to_end(key)
            return hit[1]
        flight = _flights.setdefault(key, threading.Lock())
    # 同一张底图同时被几张卡片要：只有一个请求去算，其余等它算完直接用
    with flight:
        with _lock:
            hit = _cache.get(key)
            if hit is not None and time.monotonic() - hit[0] < TTL_SECONDS:
                return hit[1]
        try:
            built = _build(render, lib, shot, edit, side, stamp, quality)
        finally:
            with _lock:
                if _flights.get(key) is flight:
                    del _flights[key]
        with _lock:
            _cache[key] = (time.monotonic(), built)
            _cache.move_to_end(key)
            while len(_cache) > KEEP:
                _cache.popitem(last=False)
        return built


def render_card(
    render: RenderService, lib: Library, shot: dict[str, Any], edit: EditState, side: dict[str, Any], *, quality: int
) -> tuple[bytes, dict[str, str]]:
    """一种边框样式的卡片图：底图（缓存）+ 这一种边框，缩到 CARD_EDGE。"""
    frame_default, stamp_default = serve.load_defaults(render.home)
    frame = resolved_frame(edit.frame, frame_default)
    stamp = resolved_stamp(edit.stamp, stamp_default)
    base = _base(render, lib, shot, edit, side, stamp, quality)
    rgb = apply_frame(base.rgb, frame, base.meta)
    return encode_jpeg(serve._fit(rgb, CARD_EDGE), quality), dict(base.labels)
