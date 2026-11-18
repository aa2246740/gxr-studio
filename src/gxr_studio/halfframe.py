"""半格双联 JPG 检测：认出相机拼接的双联（2088–2199 / 2120–2167 竖黑缝），供桌面把第 2 张按只有 JPG 处理。"""

from __future__ import annotations

import hashlib
import os
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np
from PIL import Image

FULL_WIDTH = 4288
FULL_HEIGHT = 2848
SCALE = 8
DRAFT_WIDTH = FULL_WIDTH // SCALE
DRAFT_HEIGHT = FULL_HEIGHT // SCALE
LUMA_THRESHOLD = 12

# 全分辨率下的双联黑缝列范围（含端点）：g250 为 112 px，早期测试固件 legacy48 为 48 px。
SEAMS: tuple[tuple[int, int], ...] = (
    (2088, 2199),
    (2120, 2167),
)
# 只取完全落在黑缝内的 1/8 列；外侧紧邻的 1 列对应全分辨率下两边各 8 列。
_SEAM_COLS: tuple[tuple[int, int], ...] = tuple(((x0 + SCALE - 1) // SCALE, (x1 - (SCALE - 1)) // SCALE) for x0, x1 in SEAMS)

_CACHE_LIMIT = 4096
_CACHE_LOCK = threading.Lock()
_CACHE: OrderedDict[str, bool] = OrderedDict()


def _cache_get(key: str) -> bool | None:
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
        return hit


def _cache_put(key: str, value: bool) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = value
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_LIMIT:
            _CACHE.popitem(last=False)


def _file_sha256(path: Path) -> str | None:
    try:
        with path.open("rb") as fh:
            return hashlib.file_digest(fh, "sha256").hexdigest()
    except OSError:
        return None


def is_diptych_luma(luma: np.ndarray) -> bool:
    """检查 1/8 尺度灰度图（未按 EXIF 转正，约 536×356）是否落在已知双联黑缝规则上。"""
    if luma.shape != (DRAFT_HEIGHT, DRAFT_WIDTH):
        return False
    for c0, c1 in _SEAM_COLS:
        if c0 <= 0 or c1 + 1 >= DRAFT_WIDTH:
            continue
        if (
            int(luma[:, c0 : c1 + 1].max()) <= LUMA_THRESHOLD
            and int(luma[:, c0 - 1].max()) > LUMA_THRESHOLD
            and int(luma[:, c1 + 1].max()) > LUMA_THRESHOLD
        ):
            return True
    return False


def _detect_path(path: Path) -> bool | None:
    try:
        with Image.open(path) as im:
            if im.format not in ("JPEG", "MPO") or im.size != (FULL_WIDTH, FULL_HEIGHT):
                return False
            im.draft("L", (DRAFT_WIDTH, DRAFT_HEIGHT))
            im.load()
            gray: Image.Image = im
            if gray.size != (DRAFT_WIDTH, DRAFT_HEIGHT):
                gray = gray.convert("L").resize((DRAFT_WIDTH, DRAFT_HEIGHT), Image.Resampling.BOX)
            elif gray.mode != "L":
                gray = gray.convert("L")
            luma = np.asarray(gray, dtype=np.uint8)
    except OSError:
        return None
    except (ValueError, SyntaxError):
        return False
    return is_diptych_luma(luma)


def is_diptych(jpg_path: str | os.PathLike[str], *, sha256: str | None = None) -> bool:
    """判断一份 JPEG 是否是半格双联；结果按文件 SHA-256 缓存。读不到或不是目标 JPEG 返回 False。"""
    path = Path(jpg_path)
    key = sha256 or _file_sha256(path)
    if key is None:
        return False
    cached = _cache_get(key)
    if cached is not None:
        return cached
    result = _detect_path(path)
    if result is None:
        return False
    _cache_put(key, result)
    return result
