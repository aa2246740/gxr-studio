"""半格双联 JPG 检测：认出相机拼接的双联（2088–2199 / 2120–2167 竖黑缝），供桌面把第 2 张按只有 JPG 处理。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

FULL_WIDTH = 4288
FULL_HEIGHT = 2848
SCALE = 8
DRAFT_WIDTH = FULL_WIDTH // SCALE
DRAFT_HEIGHT = FULL_HEIGHT // SCALE
LUMA_THRESHOLD = 12
HALFFRAME_VERSION = 1
HALFFRAME_FILE = "halfframe-v1.json"
HALFFRAME_REL = f".studio/{HALFFRAME_FILE}"

# 全分辨率下的双联黑缝列范围（含端点）：g250 为 112 px，早期测试固件 legacy48 为 48 px。
SEAMS: tuple[tuple[int, int], ...] = (
    (2088, 2199),
    (2120, 2167),
)
# 只取完全落在黑缝内的 1/8 列；外侧紧邻的 1 列对应全分辨率下两边各 8 列。
_SEAM_COLS: tuple[tuple[int, int], ...] = tuple(((x0 + SCALE - 1) // SCALE, (x1 - (SCALE - 1)) // SCALE) for x0, x1 in SEAMS)

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, bool] = {}


def _cache_get(key: str) -> bool | None:
    with _CACHE_LOCK:
        return _CACHE.get(key)


def _cache_put(key: str, value: bool) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = value


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


def detect_diptych(jpg_path: str | os.PathLike[str]) -> bool | None:
    """无全局缓存地检查一份 JPEG 是否为半格双联；文件不可读返回 None，其它情况返回 bool。"""
    path = Path(jpg_path)
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
    """判断一份 JPEG 是否是半格双联；结果按文件 SHA-256 无限期记在内存表里。读不到或不是目标 JPEG 返回 False。"""
    path = Path(jpg_path)
    key = sha256 or _file_sha256(path)
    if key is None:
        return False
    cached = _cache_get(key)
    if cached is not None:
        return cached
    result = detect_diptych(path)
    if result is None:
        return False
    _cache_put(key, result)
    return result


class VerdictStore:
    """图库级半格双联判定缓存（`.studio/halfframe-v1.json`）及其无上限内存镜像。"""

    def __init__(self, path: Path, check_write: Callable[[], None] | None = None) -> None:
        self.path = Path(path)
        self._check_write = check_write
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._verdicts: dict[str, bool] = {}

    def load(self) -> None:
        loaded: dict[str, bool] = {}
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict) and data.get("version") == HALFFRAME_VERSION and isinstance(data.get("verdicts"), dict):
            for k, v in data["verdicts"].items():
                if isinstance(k, str) and isinstance(v, bool):
                    loaded[k] = v
        with self._lock:
            self._verdicts = loaded

    def get(self, sha: str | None) -> bool | None:
        if not sha:
            return None
        with self._lock:
            return self._verdicts.get(sha)

    def has(self, sha: str | None) -> bool:
        if not sha:
            return False
        with self._lock:
            return sha in self._verdicts

    def diptych_shas(self) -> set[str]:
        with self._lock:
            return {sha for sha, flag in self._verdicts.items() if flag}

    def put(self, sha: str, value: bool, *, save: bool = True) -> None:
        with self._lock:
            changed = self._verdicts.get(sha) is not value
            self._verdicts[sha] = value
        if save and changed:
            self.save()

    def put_many(self, items: dict[str, bool], *, replace: bool = False, save: bool = True) -> None:
        with self._lock:
            if replace:
                changed = self._verdicts != items
                self._verdicts = dict(items)
            else:
                changed = False
                for sha, value in items.items():
                    if self._verdicts.get(sha) is not value:
                        changed = True
                    self._verdicts[sha] = value
        if save and (changed or (replace and not self.path.exists())):
            self.save()

    def save(self) -> None:
        with self._save_lock:
            if self._check_write is not None:
                self._check_write()
            with self._lock:
                snapshot = dict(self._verdicts)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.parent / f".{self.path.name}.{uuid.uuid4().hex}.tmp"
            payload = json.dumps({"version": HALFFRAME_VERSION, "verdicts": snapshot}, ensure_ascii=False, sort_keys=True)
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise


@dataclass
class HalfframeProgress:
    state: str = "running"  # running done cancelled
    total: int = 0
    done: int = 0
    diptychs: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def advance(self, diptych: bool) -> None:
        with self._lock:
            self.done += 1
            if diptych:
                self.diptychs += 1

    def finish(self, state: str) -> None:
        with self._lock:
            self.state = state

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {"state": self.state, "total": self.total, "done": self.done, "diptychs": self.diptychs}


def run_backfill(
    lib: Any,
    todo: list[tuple[str, str]],
    progress: HalfframeProgress,
    cancel: threading.Event,
    *,
    batch_size: int = 32,
) -> HalfframeProgress:
    """在 `lib.lock` 外批量检测旧图库还没落盘的 4288×2848 JPG，分批写回 `.studio/halfframe-v1.json`。"""
    batch: dict[str, bool] = {}
    for relpath, sha in todo:
        if cancel.is_set():
            break
        verdict = detect_diptych(lib.abspath(relpath))
        if verdict is not None:
            batch[sha] = verdict
        progress.advance(bool(verdict))
        if len(batch) >= batch_size:
            lib.record_halfframe_batch(batch)
            batch.clear()
    if batch:
        lib.record_halfframe_batch(batch)
    progress.finish("cancelled" if cancel.is_set() else "done")
    return progress


def is_target_dims(width: int | None, height: int | None) -> bool:
    """只有 4288×2848（含竖拍存储维度）或尺寸未知时才可能是相机双联。"""
    return width is None or height is None or (width, height) in ((FULL_WIDTH, FULL_HEIGHT), (FULL_HEIGHT, FULL_WIDTH))


def annotate_shot(
    d: dict[Any, Any],
    raw_files: dict[str, dict[Any, Any]],
    store: VerdictStore,
    abspath: Callable[[str], Path],
    *,
    allow_decode: bool,
) -> dict[Any, Any]:
    """给单张 shot 字典写入 `all_files`、`files`、`diptych` 与 `has_raw`；`allow_decode` 只对单张读取打开。"""
    all_files = {k: dict(v) for k, v in raw_files.items()}
    files = {k: dict(v) for k, v in raw_files.items()}
    jpg_info = all_files.get("jpg")
    diptych = False
    if jpg_info and is_target_dims(d.get("width"), d.get("height")):
        jpg_sha = jpg_info.get("sha256")
        stored = store.get(jpg_sha)
        if stored is not None:
            diptych = stored
        elif allow_decode and jpg_info.get("relpath"):
            detected = detect_diptych(abspath(jpg_info["relpath"]))
            if detected is not None:
                diptych = detected
                if jpg_sha:
                    store.put(jpg_sha, detected, save=True)
    if diptych:
        files.pop("dng", None)
        d["has_raw"] = 0
    d["diptych"] = diptych
    d["all_files"] = all_files
    d["files"] = files
    return d


def resolve_shot(shot: dict[Any, Any], store: VerdictStore, abspath: Callable[[str], Path]) -> dict[Any, Any]:
    """对列表路径跳过解码的条目，在 `lib.lock` 外补算并落盘明确的双联判定。"""
    all_files = shot.get("all_files") or shot.get("files") or {}
    jpg_info = all_files.get("jpg")
    if not isinstance(jpg_info, dict) or not jpg_info.get("relpath"):
        return shot
    if not is_target_dims(shot.get("width"), shot.get("height")):
        return shot
    jpg_sha = jpg_info.get("sha256")
    stored = store.get(jpg_sha)
    if stored is not None:
        diptych = stored
    else:
        detected = detect_diptych(abspath(jpg_info["relpath"]))
        if detected is None:
            return shot
        diptych = detected
        if jpg_sha:
            store.put(jpg_sha, detected, save=True)
    if diptych == bool(shot.get("diptych")) and ("all_files" in shot):
        if not diptych or ("dng" not in shot.get("files", {}) and shot.get("has_raw") == 0):
            return shot
    updated = dict(shot)
    files = {k: dict(v) for k, v in all_files.items()}
    if diptych:
        files.pop("dng", None)
        updated["has_raw"] = 0
    updated["diptych"] = diptych
    updated["all_files"] = {k: dict(v) for k, v in all_files.items()}
    updated["files"] = files
    return updated


def ensure_verdict(
    relpath: str,
    sha256: str | None,
    dims: tuple[int | None, int | None],
    store: VerdictStore,
    abspath: Callable[[str], Path],
) -> bool:
    """导入或补全时计算一次 4288×2848 JPG 的双联判定并落盘。"""
    if not sha256 or not is_target_dims(dims[0], dims[1]):
        return False
    stored = store.get(sha256)
    if stored is not None:
        return stored
    verdict = detect_diptych(abspath(relpath))
    if verdict is None:
        return False
    store.put(sha256, verdict, save=True)
    return verdict


def rebuild_verdicts(
    items: list[tuple[str, str, int | None, int | None]],
    store: VerdictStore,
    abspath: Callable[[str], Path],
) -> None:
    """`Library.rebuild` 在释放 `lib.lock` 后重建所有 4288×2848 JPG 的判定文件。"""
    verdicts: dict[str, bool] = {}
    for relpath, sha, width, height in items:
        if not sha or not is_target_dims(width, height):
            continue
        verdict = detect_diptych(abspath(relpath))
        if verdict is not None:
            verdicts[sha] = verdict
    store.put_many(verdicts, replace=True, save=True)


def pending_verdicts(db: sqlite3.Connection, store: VerdictStore) -> list[tuple[str, str]]:
    """查出索引里还没有落盘判定的 4288×2848（或未知尺寸）JPG：[(relpath, sha256)]。"""
    rows = db.execute(
        """SELECT f.relpath, f.sha256
           FROM files f
           JOIN shots s ON s.shot_id = f.shot_id
           WHERE f.kind = 'jpg'
             AND (s.width IS NULL OR s.height IS NULL
                  OR (s.width = 4288 AND s.height = 2848)
                  OR (s.width = 2848 AND s.height = 4288))
           ORDER BY s.date DESC, s.captured_at DESC, s.name, s.shot_id"""
    ).fetchall()
    seen: set[str] = set()
    todo: list[tuple[str, str]] = []
    for r in rows:
        sha = r["sha256"]
        if not sha or sha in seen or store.has(sha):
            continue
        seen.add(sha)
        todo.append((r["relpath"], sha))
    return todo


def diptych_raw_counts(db: sqlite3.Connection, shas: set[str]) -> dict[str, int]:
    """按日期统计 `has_raw = 1` 但已判定为双联的照片数量，供 `Library.dates` 从 `raw_count` 扣除。"""
    if not shas:
        return {}
    counts: dict[str, int] = {}
    sha_list = sorted(shas)
    for start in range(0, len(sha_list), 500):
        chunk = sha_list[start : start + 500]
        marks = ",".join("?" * len(chunk))
        sql = (
            f"SELECT COALESCE(s.date, 'unknown') AS d, COUNT(*) AS n "  # nosec B608  marks 只是问号占位，值在参数里
            f"FROM files f JOIN shots s ON s.shot_id = f.shot_id "
            f"WHERE f.kind = 'jpg' AND s.has_raw = 1 AND f.sha256 IN ({marks}) GROUP BY d"
        )
        for r in db.execute(sql, chunk).fetchall():
            counts[r["d"]] = counts.get(r["d"], 0) + int(r["n"])
    return counts
