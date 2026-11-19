"""g250 半格一对检测、图库级配对缓存（`.studio/halfframe-pairs-v1.json`）与照片配对注解。"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from . import halfframe as hf
from .observe import log

PAIRS_VERSION = 1
PAIRS_FILE = "halfframe-pairs-v1.json"
PAIRS_REL = f".studio/{PAIRS_FILE}"

VALID_ORIENTATIONS: frozenset[int] = frozenset({1, 3, 6, 8})
MPF_WIDTH = 640
MPF_HEIGHT = 424
MPF_LEFT_MAX = 163  # 0..163 含端点
MPF_RIGHT_MIN = 476  # 476..639 含端点

# g250 全分辨率及 1/8 尺度几何常数
G250_HALF_W = 2088
G250_SEAM_X0 = 2088
G250_SEAM_X1 = 2199
G250_STRIP_X0_JPG = 1100
G250_STRIP_X0_DNG = 1099
DRAFT_HALF_W = G250_HALF_W // hf.SCALE  # 261
DRAFT_RIGHT_X0 = (G250_SEAM_X1 + 1) // hf.SCALE  # 275
DRAFT_STRIP_X0 = G250_STRIP_X0_JPG // hf.SCALE  # 137（精确起点为 137.5）

MAD_PASS = 3.0
CORR_PASS = 0.99
MAD_OTHER_MIN = 8.0

_TRAILING_DIGITS = re.compile(r"(\d+)$")


@dataclass(frozen=True)
class PairDecision:
    first_half: str  # "left" | "right"
    flat: bool
    mad: tuple[float, float]
    corr: tuple[float, float]


def extract_number(name: str | None) -> int | None:
    """提取文件名（去扩展名后）末尾的连续数字编号。"""
    if not name:
        return None
    m = _TRAILING_DIGITS.search(Path(name).stem)
    return int(m.group(1)) if m else None


def g250_first_half_for_orientation(orientation: int) -> str:
    """g250 方向规则：EXIF 1/6 → 第 1 张在传感器左半，3/8 → 在传感器右半。"""
    if orientation in (1, 6):
        return "left"
    if orientation in (3, 8):
        return "right"
    raise ValueError(f"unsupported orientation: {orientation}")


def is_first_like_preview(luma: np.ndarray) -> bool:
    """检查 MPF 640×424 预览灰度图是否符合 g250 第 1 张：两侧 0–163 与 476–639 全黑（≤12），中间有内容。"""
    if luma.shape != (MPF_HEIGHT, MPF_WIDTH):
        return False
    return (
        int(luma[:, : MPF_LEFT_MAX + 1].max()) <= hf.LUMA_THRESHOLD
        and int(luma[:, MPF_RIGHT_MIN:].max()) <= hf.LUMA_THRESHOLD
        and int(luma[:, MPF_LEFT_MAX + 1 : MPF_RIGHT_MIN].max()) > hf.LUMA_THRESHOLD
    )


def detect_first_like(jpg_path: str | os.PathLike[str]) -> bool | None:
    """读取 4288×2848 JPEG 的 MPF 640×424 预览，判断是否符合 g250 `first_like`；不可读返回 None。"""
    path = Path(jpg_path)
    try:
        with Image.open(path) as im:
            if im.format not in ("JPEG", "MPO") or im.size != (hf.FULL_WIDTH, hf.FULL_HEIGHT):
                return False
            if int(getattr(im, "n_frames", 1)) < 2:
                return False
            im.seek(1)
            if im.size != (MPF_WIDTH, MPF_HEIGHT):
                return False
            luma = np.asarray(im.convert("L"), dtype=np.uint8)
    except OSError:
        return None
    except (ValueError, SyntaxError, EOFError):
        return False
    return is_first_like_preview(luma)


FLAT_STD = 2.0


def _half_metrics(a_strip: np.ndarray, b_half: np.ndarray) -> tuple[float, float]:
    mad = float(np.mean(np.abs(a_strip - b_half)))
    da = a_strip - float(a_strip.mean())
    db = b_half - float(b_half.mean())
    n = float(a_strip.size)
    na = float(np.sum(da * da))
    nb = float(np.sum(db * db))
    if np.sqrt(na / n) < FLAT_STD and np.sqrt(nb / n) < FLAT_STD:
        corr = 1.0 if mad <= MAD_PASS else 0.0
    elif na < 1e-12 or nb < 1e-12:
        corr = 0.0
    else:
        corr = float(np.sum(da * db) / np.sqrt(na * nb))
    return mad, corr


def fingerprint_metrics(
    a_luma: np.ndarray,
    b_luma: np.ndarray,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """计算 1/8 尺度指纹：返回 `((mad_left, mad_right), (corr_left, corr_right))`。"""
    expected = (hf.DRAFT_HEIGHT, hf.DRAFT_WIDTH)
    if a_luma.shape != expected or b_luma.shape != expected:
        return None
    # 1100/8 = 137.5，用相邻两列平均对齐半像素起点。
    s0 = DRAFT_STRIP_X0
    s1 = s0 + DRAFT_HALF_W
    a_strip = 0.5 * (a_luma[:, s0:s1].astype(np.float64) + a_luma[:, s0 + 1 : s1 + 1].astype(np.float64))
    b_left = b_luma[:, :DRAFT_HALF_W].astype(np.float64)
    b_right = b_luma[:, DRAFT_RIGHT_X0 : DRAFT_RIGHT_X0 + DRAFT_HALF_W].astype(np.float64)
    mad_l, corr_l = _half_metrics(a_strip, b_left)
    mad_r, corr_r = _half_metrics(a_strip, b_right)
    return (mad_l, mad_r), (corr_l, corr_r)


def decide_pair(
    mad: tuple[float, float],
    corr: tuple[float, float],
    *,
    orientation: int,
    same_orientation: bool = True,
    a_first_like: bool = True,
    b_seam_kind: str | None = hf.SEAM_G250,
    adjacent: bool = True,
) -> PairDecision | None:
    """纯规则表：把候选特征与 1/8 指纹数值映射成确定配对或拒绝（只记数字日志，不记路径或文件名）。"""
    if not adjacent or not a_first_like or b_seam_kind != hf.SEAM_G250:
        return None
    if not same_orientation or orientation not in VALID_ORIENTATIONS:
        return None
    mad_l, mad_r = float(mad[0]), float(mad[1])
    corr_l, corr_r = float(corr[0]), float(corr[1])
    left_pass = mad_l <= MAD_PASS and corr_l >= CORR_PASS
    right_pass = mad_r <= MAD_PASS and corr_r >= CORR_PASS
    if left_pass and right_pass:
        return PairDecision(
            first_half=g250_first_half_for_orientation(orientation),
            flat=True,
            mad=(mad_l, mad_r),
            corr=(corr_l, corr_r),
        )
    if left_pass and mad_r > MAD_OTHER_MIN:
        return PairDecision(first_half="left", flat=False, mad=(mad_l, mad_r), corr=(corr_l, corr_r))
    if right_pass and mad_l > MAD_OTHER_MIN:
        return PairDecision(first_half="right", flat=False, mad=(mad_l, mad_r), corr=(corr_l, corr_r))
    log.event(
        logging.INFO,
        "halfframe_pair_rejected",
        mad_l=round(mad_l, 3),
        mad_r=round(mad_r, 3),
        corr_l=round(corr_l, 4),
        corr_r=round(corr_r, 4),
        left_pass=left_pass,
        right_pass=right_pass,
    )
    return None


def evaluate_candidate(
    a_path: str | os.PathLike[str],
    b_path: str | os.PathLike[str],
    *,
    orientation_a: int,
    orientation_b: int,
    a_first_like: bool = True,
    adjacent: bool = True,
) -> PairDecision | None:
    """对候选 `(A, B)` 解码 1/8 灰度并执行 g250 黑缝校验与指纹匹配。"""
    if not adjacent or not a_first_like or orientation_a != orientation_b or orientation_b not in VALID_ORIENTATIONS:
        return None
    ok_b, b_luma = hf.decode_draft_luma(b_path)
    if not ok_b or b_luma is None:
        return None
    seam = hf.seam_kind_luma(b_luma)
    if seam != hf.SEAM_G250:
        return None
    ok_a, a_luma = hf.decode_draft_luma(a_path)
    if not ok_a or a_luma is None:
        return None
    metrics = fingerprint_metrics(a_luma, b_luma)
    if metrics is None:
        return None
    return decide_pair(
        metrics[0],
        metrics[1],
        orientation=orientation_b,
        same_orientation=True,
        a_first_like=True,
        b_seam_kind=seam,
        adjacent=True,
    )


def _normalize_pair_entry(key: str, raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    first_sha = raw.get("first_sha")
    second_sha = raw.get("second_sha")
    first_half = raw.get("first_half")
    flat = raw.get("flat")
    mad = raw.get("mad")
    corr = raw.get("corr")
    if (
        not isinstance(first_sha, str)
        or not first_sha
        or not isinstance(second_sha, str)
        or second_sha != key
        or first_half not in ("left", "right")
        or not isinstance(flat, bool)
        or not isinstance(mad, list)
        or len(mad) != 2
        or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in mad)
        or not isinstance(corr, list)
        or len(corr) != 2
        or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in corr)
    ):
        return None
    return {
        "first_sha": first_sha,
        "second_sha": second_sha,
        "first_half": first_half,
        "flat": flat,
        "mad": [round(float(mad[0]), 4), round(float(mad[1]), 4)],
        "corr": [round(float(corr[0]), 6), round(float(corr[1]), 6)],
    }


def make_pair_record(first_sha: str, second_sha: str, decision: PairDecision) -> dict[str, Any]:
    return {
        "first_sha": first_sha,
        "second_sha": second_sha,
        "first_half": decision.first_half,
        "flat": decision.flat,
        "mad": [round(decision.mad[0], 4), round(decision.mad[1], 4)],
        "corr": [round(decision.corr[0], 6), round(decision.corr[1], 6)],
    }


class PairStore(hf.CoalescedStore):
    """图库级 g250 配对缓存（`.studio/halfframe-pairs-v1.json`），只存 SHA 与纯数值证据。"""

    def __init__(
        self,
        path: Path,
        check_write: Callable[[], None] | None = None,
        *,
        flush_interval: float = hf.FLUSH_INTERVAL,
    ) -> None:
        super().__init__(path, check_write=check_write, flush_interval=flush_interval, save_error_event="halfframe_pairs_save_failed")
        self._first_like: dict[str, bool] = {}
        self._pairs: dict[str, dict[str, Any]] = {}
        self._by_first: dict[str, str] = {}
        self._rejected: set[tuple[str, str]] = set()

    def _rebuild_reverse_locked(self) -> None:
        self._by_first = {rec["first_sha"]: sec_sha for sec_sha, rec in self._pairs.items()}

    def _serialize_locked(self) -> tuple[dict[str, Any], Any]:
        fl_snap = dict(self._first_like)
        p_snap = {k: dict(v) for k, v in self._pairs.items()}
        return {"version": PAIRS_VERSION, "first_like": fl_snap, "pairs": p_snap}, (fl_snap, p_snap)

    def _unchanged_locked(self, snapshot: Any) -> bool:
        fl_snap, p_snap = snapshot
        return bool(self._first_like == fl_snap and self._pairs == p_snap)

    def load(self) -> None:
        loaded_fl: dict[str, bool] = {}
        loaded_pairs: dict[str, dict[str, Any]] = {}
        future_version: int | None = None
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            ver = data.get("version")
            if isinstance(ver, int) and not isinstance(ver, bool) and ver > PAIRS_VERSION:
                future_version = ver
            elif ver == PAIRS_VERSION:
                raw_fl = data.get("first_like")
                if isinstance(raw_fl, dict):
                    for k, v in raw_fl.items():
                        if isinstance(k, str) and isinstance(v, bool):
                            loaded_fl[k] = v
                raw_pairs = data.get("pairs")
                if isinstance(raw_pairs, dict):
                    for k, v in raw_pairs.items():
                        if isinstance(k, str):
                            norm = _normalize_pair_entry(k, v)
                            if norm is not None:
                                loaded_pairs[k] = norm
        with self._lock:
            self._first_like = loaded_fl
            self._pairs = loaded_pairs
            self._rebuild_reverse_locked()
            self._rejected.clear()
            timer, already_future = self._finish_load_locked(future_version)
        if timer is not None:
            timer.cancel()
        if future_version is not None and not already_future:
            log.event(logging.WARNING, "halfframe_pairs_future_version", version=future_version, supported=PAIRS_VERSION)

    def get_first_like(self, sha: str | None) -> bool | None:
        if not sha:
            return None
        with self._lock:
            return self._first_like.get(sha)

    def has_first_like(self, sha: str | None) -> bool:
        if not sha:
            return False
        with self._lock:
            return sha in self._first_like

    def has_pairs(self) -> bool:
        with self._lock:
            return bool(self._pairs)

    def all_pairs(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._pairs.items()}

    def pair_for_second(self, second_sha: str | None) -> dict[str, Any] | None:
        if not second_sha:
            return None
        with self._lock:
            rec = self._pairs.get(second_sha)
            return dict(rec) if rec is not None else None

    def pair_for_first(self, first_sha: str | None) -> dict[str, Any] | None:
        if not first_sha:
            return None
        with self._lock:
            sec = self._by_first.get(first_sha)
            if not sec:
                return None
            rec = self._pairs.get(sec)
            return dict(rec) if rec is not None else None

    def is_rejected(self, first_sha: str, second_sha: str) -> bool:
        with self._lock:
            return (first_sha, second_sha) in self._rejected

    def mark_rejected(self, first_sha: str, second_sha: str) -> None:
        with self._lock:
            self._rejected.add((first_sha, second_sha))

    def put_first_like(self, sha: str, value: bool, *, save: bool = True, coalesce: bool = False, strict: bool = False) -> None:
        with self._lock:
            changed = self._first_like.get(sha) is not value
            self._first_like[sha] = value
            if changed:
                self._dirty = True
            writable = not self._readonly
            dirty = self._dirty
        if not writable:
            return
        if coalesce:
            if dirty:
                self._flush_coalesced(strict=strict)
        elif save and changed:
            self._flush(strict=strict)

    def put_pair(self, record: dict[str, Any], *, save: bool = True, coalesce: bool = False, strict: bool = False) -> None:
        sec_sha = record["second_sha"]
        norm = _normalize_pair_entry(sec_sha, record)
        if norm is None:
            return
        with self._lock:
            changed = self._pairs.get(sec_sha) != norm
            self._pairs[sec_sha] = norm
            self._rebuild_reverse_locked()
            self._rejected.discard((norm["first_sha"], sec_sha))
            if changed:
                self._dirty = True
            writable = not self._readonly
            dirty = self._dirty
        if not writable:
            return
        if coalesce:
            if dirty:
                self._flush_coalesced(strict=strict)
        elif save and changed:
            self._flush(strict=strict)

    def put_batch(
        self,
        first_like: dict[str, bool],
        pairs: dict[str, dict[str, Any]],
        *,
        replace: bool = False,
        save: bool = True,
        strict: bool = False,
    ) -> None:
        norm_pairs: dict[str, dict[str, Any]] = {}
        for k, v in pairs.items():
            norm = _normalize_pair_entry(k, v)
            if norm is not None:
                norm_pairs[k] = norm
        with self._lock:
            if replace:
                changed = self._first_like != first_like or self._pairs != norm_pairs
                self._first_like = dict(first_like)
                self._pairs = norm_pairs
            else:
                changed = False
                for sha, flag in first_like.items():
                    if self._first_like.get(sha) is not flag:
                        changed = True
                    self._first_like[sha] = flag
                for sec_sha, rec in norm_pairs.items():
                    if self._pairs.get(sec_sha) != rec:
                        changed = True
                    self._pairs[sec_sha] = rec
                    self._rejected.discard((rec["first_sha"], sec_sha))
            self._rebuild_reverse_locked()
            if changed:
                self._dirty = True
            writable = not self._readonly
        if writable and save and (changed or (replace and not self.path.exists())):
            self._flush(strict=strict)


# ---- 照片字典助手 -------------------------------------------------------------


def is_pair_second(shot: dict[Any, Any]) -> bool:
    pair = shot.get("pair")
    return isinstance(pair, dict) and pair.get("role") == "second"


def has_pair_raw(shot: dict[Any, Any]) -> bool:
    """已配对的第 2 张且两半在图库里都有 DNG、方向合法一致，才可走两张 DNG 合成渲染。"""
    if not is_pair_second(shot):
        return False
    sources = shot.get("pair_sources")
    if not isinstance(sources, dict):
        return False
    first = sources.get("first")
    second = sources.get("second")
    if not isinstance(first, dict) or not isinstance(second, dict):
        return False
    ori1 = first.get("orientation")
    ori2 = second.get("orientation")
    if ori1 != ori2 or ori2 not in VALID_ORIENTATIONS:
        return False
    f1 = first.get("all_files") or {}
    f2 = second.get("all_files") or {}
    return "dng" in f1 and "dng" in f2
