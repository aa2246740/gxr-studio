"""Sequential IEEE 754 oracle (Gate A) and FMA attribution helpers (Gate B) for Pillow 12.2.0 resize."""

from __future__ import annotations

import math
from typing import Callable

import numpy as np

PRECISION_BITS = 22
SCALE_8BPC = float(1 << PRECISION_BITS)


def _box_filter(x: float) -> float:
    return 1.0 if -0.5 < x <= 0.5 else 0.0


def _bilinear_filter(x: float) -> float:
    if x < 0.0:
        x = -x
    return 1.0 - x if x < 1.0 else 0.0


def _bicubic_filter(x: float) -> float:
    a = -0.5
    if x < 0.0:
        x = -x
    if x < 1.0:
        return ((a + 2.0) * x - (a + 3.0)) * x * x + 1.0
    if x < 2.0:
        return (((x - 5.0) * x + 8.0) * x - 4.0) * a
    return 0.0


def _sinc_filter(x: float) -> float:
    if x == 0.0:
        return 1.0
    px = x * math.pi
    return math.sin(px) / px


def _lanczos_filter(x: float) -> float:
    if -3.0 <= x < 3.0:
        return _sinc_filter(x) * _sinc_filter(x / 3.0)
    return 0.0


_FILTERS: dict[str, tuple[Callable[[float], float], float]] = {
    "box": (_box_filter, 0.5),
    "bilinear": (_bilinear_filter, 1.0),
    "bicubic": (_bicubic_filter, 2.0),
    "lanczos": (_lanczos_filter, 3.0),
}


def precompute_coeffs_seq(
    in_size: int,
    in0: float,
    in1: float,
    out_size: int,
    filter_name: str,
) -> tuple[int, list[tuple[int, int]], list[list[float]]]:
    """Sequential IEEE 754 port of ``Resample.c:precompute_coeffs`` (no FMA)."""
    fn, base_support = _FILTERS[filter_name]
    diff_f32 = float(np.float32(in1) - np.float32(in0))
    scale = diff_f32 / float(out_size)
    filterscale = 1.0 if scale < 1.0 else scale
    support = base_support * filterscale
    ksize = int(math.ceil(support)) * 2 + 1
    ss = 1.0 / filterscale

    bounds: list[tuple[int, int]] = []
    kk: list[list[float]] = []
    in0_f64 = float(np.float32(in0))
    for xx in range(out_size):
        center = in0_f64 + (float(xx) + 0.5) * scale
        xmin = max(0, int(center - support + 0.5))
        xmax_end = min(in_size, max(0, int(center + support + 0.5)))
        xmax = min(ksize, max(0, xmax_end - xmin))
        row = [0.0] * ksize
        ww = 0.0
        for x in range(xmax):
            w = fn((float(x + xmin) - center + 0.5) * ss)
            row[x] = w
            ww += w
        if ww != 0.0:
            for x in range(xmax):
                row[x] /= ww
        bounds.append((xmin, xmax))
        kk.append(row)
    return ksize, bounds, kk


def _inner_f32(
    src: np.ndarray,
    out_w: int,
    out_h: int,
    filter_name: str,
    *,
    use_fma: bool,
) -> np.ndarray:
    in_h, in_w = src.shape
    need_h = out_w != in_w
    need_v = out_h != in_h
    if not need_h and not need_v:
        return src.copy()

    bounds_h: list[tuple[int, int]] = []
    kk_h: list[list[float]] = []
    if need_h:
        _, bounds_h, kk_h = precompute_coeffs_seq(in_w, 0.0, float(in_w), out_w, filter_name)

    bounds_v: list[tuple[int, int]] = []
    kk_v: list[list[float]] = []
    if need_v:
        _, bounds_v, kk_v = precompute_coeffs_seq(in_h, 0.0, float(in_h), out_h, filter_name)
        ybox_first = bounds_v[0][0]
        ybox_last = bounds_v[-1][0] + bounds_v[-1][1]
    else:
        ybox_first = 0
        ybox_last = in_h

    cur = src
    if need_h:
        if need_v:
            bounds_v = [(ymin - ybox_first, ymax) for ymin, ymax in bounds_v]
        temp_h = max(0, ybox_last - ybox_first)
        temp = np.empty((temp_h, out_w), dtype=np.float32)
        for yy in range(temp_h):
            row_in = [float(v) for v in cur[yy + ybox_first]]
            for xx in range(out_w):
                xmin, xmax = bounds_h[xx]
                k = kk_h[xx]
                if not use_fma:
                    ss = 0.0
                    for x in range(xmax):
                        ss += row_in[x + xmin] * k[x]
                elif xmax >= 8:
                    # Apple Clang arm64 8-wide NEON unfused fmul.2d + fadd, then fma tail
                    acc = [0.0] * 8
                    x = 0
                    while x + 8 <= xmax:
                        for lane in range(8):
                            acc[lane] += row_in[x + xmin + lane] * k[x + lane]
                        x += 8
                    s0 = acc[0] + acc[4]
                    s1 = acc[1] + acc[5]
                    s2 = acc[2] + acc[6]
                    s3 = acc[3] + acc[7]
                    ss = (s0 + s2) + (s1 + s3)
                    while x < xmax:
                        ss = math.fma(row_in[x + xmin], k[x], ss)
                        x += 1
                else:
                    ss = 0.0
                    for x in range(xmax):
                        ss = math.fma(row_in[x + xmin], k[x], ss)
                temp[yy, xx] = np.float32(ss)
        cur = temp

    if need_v:
        out = np.empty((out_h, cur.shape[1]), dtype=np.float32)
        cur_list = [[float(v) for v in row] for row in cur]
        for yy in range(out_h):
            ymin, ymax = bounds_v[yy]
            k = kk_v[yy]
            for xx in range(cur.shape[1]):
                if not use_fma:
                    ss = 0.0
                    for y in range(ymax):
                        ss += cur_list[y + ymin][xx] * k[y]
                else:
                    ss = 0.0
                    for y in range(ymax):
                        ss = math.fma(cur_list[y + ymin][xx], k[y], ss)
                out[yy, xx] = np.float32(ss)
        return out
    return cur


def oracle_resample_f32(src: np.ndarray, out_w: int, out_h: int, filter_name: str, *, use_fma: bool = False) -> np.ndarray:
    """Sequential IEEE (or FMA-contracted) Python oracle for Pillow mode ``'F'`` resize."""
    assert src.ndim == 2 and src.dtype == np.float32
    in_h, in_w = src.shape
    if in_w == out_w and in_h == out_h:
        return src.copy()
    if in_h > in_w * 100 and out_h < in_h:
        vert = _inner_f32(src, in_w, out_h, filter_name, use_fma=use_fma)
        return _inner_f32(vert, out_w, out_h, filter_name, use_fma=use_fma)
    return _inner_f32(src, out_w, out_h, filter_name, use_fma=use_fma)


def _dot_horiz_f32(row: list[float], kk: list[float], xmax: int, *, use_fma: bool) -> float:
    if not use_fma:
        ss = 0.0
        for x in range(xmax):
            ss += row[x] * kk[x]
        return float(np.float32(ss))
    if xmax >= 8:
        acc = [0.0] * 8
        x = 0
        while x + 8 <= xmax:
            for lane in range(8):
                acc[lane] += row[x + lane] * kk[x + lane]
            x += 8
        s0 = acc[0] + acc[4]
        s1 = acc[1] + acc[5]
        s2 = acc[2] + acc[6]
        s3 = acc[3] + acc[7]
        ss = (s0 + s2) + (s1 + s3)
        while x < xmax:
            ss = math.fma(row[x], kk[x], ss)
            x += 1
        return float(np.float32(ss))
    ss = 0.0
    for x in range(xmax):
        ss = math.fma(row[x], kk[x], ss)
    return float(np.float32(ss))


def oracle_pixel_f32(
    src: np.ndarray,
    xx: int,
    yy: int,
    bounds_h: list[tuple[int, int]],
    kk_h: list[list[float]],
    bounds_v: list[tuple[int, int]],
    kk_v: list[list[float]],
    *,
    use_fma: bool,
) -> np.float32:
    """Evaluate a single ``(yy, xx)`` output pixel using the sequential or FMA-contracted oracle."""
    xmin, xmax = bounds_h[xx]
    ymin, ymax = bounds_v[yy]
    kh = kk_h[xx]
    kv = kk_v[yy]
    ss = 0.0
    for y in range(ymax):
        row = [float(v) for v in src[ymin + y, xmin : xmin + xmax]]
        t = _dot_horiz_f32(row, kh, xmax, use_fma=use_fma)
        ss = math.fma(t, kv[y], ss) if use_fma else (ss + t * kv[y])
    return np.float32(ss)
