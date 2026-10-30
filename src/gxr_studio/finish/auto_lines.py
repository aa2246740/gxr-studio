"""自动拉直和垂直。没有把握时调用方保留当前值。"""

from __future__ import annotations

import math

import numpy as np

from .geometry import _slider_s


def estimate(rgb: np.ndarray) -> dict[str, object]:
    small = _shrink(rgb)
    gray = small.mean(axis=-1) if small.ndim == 3 else small.astype(np.float32)
    h, w = gray.shape
    margin = max(6, int(round(min(h, w) * 0.04)))
    gy, gx = np.gradient(gray)
    mag = np.hypot(gx, gy)
    mag[:margin, :] = 0
    mag[-margin:, :] = 0
    mag[:, :margin] = 0
    mag[:, -margin:] = 0
    if not np.any(mag > 0):
        return _miss()
    cutoff = float(np.quantile(mag[mag > 0], 0.92))
    ys, xs = np.nonzero(mag >= max(cutoff, 1.0))
    if len(xs) < 30:
        return _miss()
    acc, theta = _hough(xs.astype(np.float64), ys.astype(np.float64))
    horizontal = _peak(acc, theta, math.radians(75), math.radians(105))
    verticals = _peaks(acc, theta, math.radians(165), math.radians(15), 6)
    straighten = 0.0
    horizon = False
    if horizontal is not None:
        votes, th = horizontal
        tilt = math.degrees(th - math.pi / 2.0)
        while tilt > 90:
            tilt -= 180
        while tilt < -90:
            tilt += 180
        baseline = float(np.median(acc.max(axis=0)))
        if abs(tilt) <= 20 and votes >= max(20.0, 2.5 * max(baseline, 1.0)):
            horizon = True
            straighten = float(np.clip(-tilt, -45, 45))
    upright, vertical = _vertical(verticals, w, h)
    if not horizon and not upright:
        return _miss()
    if horizon and abs(straighten) < 1.0:
        straighten = 0.0
    if upright and abs(vertical) < 8.0:
        vertical = 0.0
    return {"straighten": round(straighten, 2), "vertical": round(vertical, 2), "confident": True, "horizon": horizon, "upright": upright}


def _miss() -> dict[str, object]:
    return {"straighten": 0.0, "vertical": 0.0, "confident": False, "horizon": False, "upright": False}


def _shrink(rgb: np.ndarray, edge: int = 480) -> np.ndarray:
    h, w = rgb.shape[:2]
    scale = edge / max(h, w)
    if scale >= 1:
        return rgb
    ys = np.linspace(0, h - 1, max(2, int(h * scale))).astype(int)
    xs = np.linspace(0, w - 1, max(2, int(w * scale))).astype(int)
    return np.asarray(rgb[ys][:, xs])


def _hough(xs: np.ndarray, ys: np.ndarray, n_theta: int = 180, n_rho: int = 240) -> tuple[np.ndarray, np.ndarray]:
    theta = np.linspace(0.0, math.pi, n_theta, endpoint=False)
    rho = xs[:, None] * np.cos(theta) + ys[:, None] * np.sin(theta)
    rho_max = float(np.hypot(xs.max() + 1, ys.max() + 1)) + 2
    step = (2 * rho_max) / n_rho
    rho_i = np.clip(((rho + rho_max) / step).astype(np.int32), 0, n_rho - 1)
    acc = np.zeros((n_rho, n_theta), np.int32)
    th_i = np.broadcast_to(np.arange(n_theta), rho_i.shape)
    np.add.at(acc, (rho_i.ravel(), th_i.ravel()), 1)
    return acc, theta


def _peaks(acc: np.ndarray, theta: np.ndarray, lo: float, hi: float, limit: int) -> list[tuple[float, float, float]]:
    band = np.array([(lo <= t < hi) if lo <= hi else (t >= lo or t < hi) for t in theta])
    work = acc.copy()
    work[:, ~band] = 0
    found: list[tuple[float, float, float]] = []
    for _ in range(limit):
        idx = int(np.argmax(work))
        votes = float(work.flat[idx])
        if votes < 8:
            break
        rho_i, th_i = np.unravel_index(idx, work.shape)
        found.append((votes, float(theta[th_i]), float(rho_i)))
        r0, r1 = max(0, int(rho_i) - 6), min(work.shape[0], int(rho_i) + 7)
        t0, t1 = max(0, int(th_i) - 3), min(work.shape[1], int(th_i) + 4)
        work[r0:r1, t0:t1] = 0
    return found


def _peak(acc: np.ndarray, theta: np.ndarray, lo: float, hi: float) -> tuple[float, float] | None:
    found = _peaks(acc, theta, lo, hi, 1)
    if not found:
        return None
    votes, th, _rho = found[0]
    return votes, th


def _vertical(peaks: list[tuple[float, float, float]], w: int, h: int) -> tuple[bool, float]:
    if not peaks:
        return False, 0.0
    ranked = []
    for votes, th, _rho in peaks:
        tilt = math.degrees(th - math.pi / 2.0)
        while tilt > 90:
            tilt -= 180
        while tilt < -90:
            tilt += 180
        phi = tilt - 90.0 if tilt >= 0 else tilt + 90.0
        if abs(phi) > 25 or abs(math.cos(th)) < 0.15:
            continue
        ranked.append((votes, phi, th))
    if len(ranked) < 2:
        return False, 0.0
    # 用左右两条最强竖线的倾角差估计上边该放宽多少。
    left = min(ranked, key=lambda item: item[2])
    right = max(ranked, key=lambda item: item[2])
    if left[0] < 15 or right[0] < 15:
        return False, 0.0
    delta = right[1] - left[1]
    sv = math.radians(delta) * (h / max(w, 1)) * 0.5
    slider = float(np.clip(-sv / _slider_s(100.0) * 100.0, -100, 100))
    return True, slider
