"""从同一张 DNG 算"高光还能再亮多少"（log2 增益），只是 RAW 亮度的函数，不是两张图逐点相除。

来源：research/hdr_gainmap/gxr_hdr.py 的 `align`、`fit_curve`、`compute_gain`（REPORT.md §3.1）。RAW 读取换成
工作室的 `raw` 模块（MOUNT 的 0/0 有理数、非有限值都在那里处理），算式不变：

1. RAW 半分辨率白平衡码值 ÷ 4095，亮度 = 0.25R + 0.5G + 0.25B；任一传感器点 ≥ 白电平的 98% 记为"RAW 过曝"；
2. 对齐：RAW 缩放到增益图网格，相位相关找平移，±2% 试缩放，按高通相关系数选最好的；
   再核对分块正相关、局部位移和连续亮度残差；无法确认的区域判为对不齐（`HdrUnsupported`）；
3. 在增益图网格上把 log2 RAW 亮度对 log2 SDR 线性亮度做分箱中位数，得到这张照片"RAW → SDR"的单调曲线 T；
4. SDR 线性亮度 `knee` 对应的 RAW 位置是拐点：拐点以下增益严格为 1，以上按场景线性继续变亮，软上限 `max_stops`；
5. 本块 SDR 亮度低于 `gate_lo` 的增益为 0，到 `gate_hi` 平滑过渡。
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from typing import Any, cast

import numpy as np
from PIL import Image
from scipy import ndimage

from ..raw import half_res_codes, orient_array
from ..raw.codes import CODE_MAX
from ..raw.dng import Dng
from . import capture
from .container import GainMeta, assemble, encode_gainmap, jpeg_segments

LUMA = np.array([0.2126, 0.7152, 0.0722], np.float32)
RAW_WEIGHTS = np.array([0.25, 0.5, 0.25], np.float32)
MIN_CORRELATION = 0.5  # MOUNT 15 张 0.68–0.96，50mm 抽查 16 张 0.88–0.98；拿错 DNG 的 7 对 0.01–0.47（research/studio_hdr_r1）
MAX_SHIFT = 0.02  # 平移不超过增益图边长的 2%（和缩放的搜索范围一样）；真正的一对 RAW+JPG 是 0
TILES = 4  # 分块核对局部还要不要再挪：4×4 块，每块在 ±TILE_REACH 像素内直接找最好的位置
TILE_REACH = 3
TILE_TEXTURE = 0.1  # 高通后的标准差（档）低于它的块太平，不参加
TILE_GAIN = 0.03  # 有可靠匹配时，一格的局部位移也不能当成同一次拍摄的几何关系
TILE_MATCH = 0.8
MIN_TILE_CORRELATION = 0.5  # 局部必须在当前位置直接对得上，不能以小范围搜索无改进作为配准证据
RESIDUAL_EDGE = 192  # 独立的亮度残差检查尺度；均值取样抑制机内锐化、降噪的细节差异
RESIDUAL_WINDOWS = ((16, 4), (32, 8), (64, 32))  # 窗口边长、连续异常的最少网格点数
SCALES = (1.0, 0.99, 1.01, 0.98, 1.02)


class HdrUnsupported(Exception):
    """这张照片做不了 HDR（RAW 和 JPG 对不齐、画面里没有可拟合的亮度…）。`reason` 是机器值。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Params:
    map_factor: int = 4  # 增益图 = 主图的 1/4
    max_stops: float = 2.5  # 增益上限（档）
    knee: float = 0.45  # SDR 线性亮度高于它才可能提亮（≈ sRGB 码值 180）
    hdr_slope: float = 1.0  # 拐点以上 HDR 曲线的对数斜率（1 = 按场景线性继续变亮）
    gate_lo: float = 0.30  # 本块 SDR 亮度低于它 → 增益为 0（深色天空、红滤镜压暗的地方不提亮）
    gate_hi: float = 0.50
    quality: int = 90


@dataclass
class RawLuma:
    """转正后的 RAW 亮度（半分辨率）和过曝掩码。"""

    y: np.ndarray
    clip: np.ndarray
    capture: capture.Capture = field(default_factory=dict)


@dataclass(frozen=True)
class Alignment:
    scale: float
    dy: int  # 增益图网格上的像素
    dx: int
    correlation: float
    tiles_moved: int = 0
    shift_frac: float = 0.0  # 平移占增益图边长的比例
    tiles_unverified: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "scale": self.scale,
            "shift": [self.dy, self.dx],
            "correlation": round(self.correlation, 3),
            "tiles_moved": self.tiles_moved,
            "tiles_unverified": self.tiles_unverified,
        }


@dataclass
class GainResult:
    delta: np.ndarray  # log2 增益，增益图网格
    meta: GainMeta
    stats: dict[str, Any] = field(default_factory=dict)


def raw_luma(dng: Dng) -> RawLuma:
    """白平衡后的 RAW 亮度（码值 ÷ 4095，与研究代码 `Dng.half_res` 同口径）和过曝掩码，都已按 DNG 方向转正。"""
    try:
        try:
            metadata = capture.from_dng(dng.data)
        except (ValueError, IndexError, TypeError) as exc:
            raise HdrUnsupported("metadata_unreadable", "无法核对 RAW 拍摄信息") from exc
        codes = half_res_codes(dng)
        h2, w2 = codes.shape[:2]
        crop = dng.crop_mosaic()[: 2 * h2, : 2 * w2]
        clip = (crop.reshape(h2, 2, w2, 2) >= dng.white * 0.98).any(axis=(1, 3))
    finally:
        dng.release()
    y = np.maximum((codes / np.float32(CODE_MAX)) @ RAW_WEIGHTS, 1e-6).astype(np.float32)
    return RawLuma(np.ascontiguousarray(orient_array(y, dng.orientation)), np.ascontiguousarray(orient_array(clip, dng.orientation)), metadata)


def check_capture(jpeg: bytes, raw: RawLuma) -> None:
    try:
        metadata = capture.from_jpeg(jpeg)
    except (ValueError, OSError, SyntaxError) as exc:
        raise HdrUnsupported("metadata_unreadable", "无法核对 JPG 拍摄信息") from exc
    if capture.differs(raw.capture, metadata):
        raise HdrUnsupported("capture_mismatch", capture.MISMATCH)


_INVERSE = {6: 8, 8: 6}


def to_storage(a: np.ndarray, orientation: int) -> np.ndarray:
    """转正的数组 → 方向为 `orientation` 的 JPEG 的存储方向（`orient_array` 的逆）。"""
    return np.ascontiguousarray(orient_array(a, _INVERSE.get(int(orientation), int(orientation))))


def srgb_to_linear(rgb: np.ndarray) -> np.ndarray:
    v = rgb.astype(np.float32) / 255.0
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4).astype(np.float32)


def grid_shape(shape: tuple[int, ...], factor: int) -> tuple[int, int]:
    return math.ceil(shape[0] / factor), math.ceil(shape[1] / factor)


def block_mean(a: np.ndarray, factor: int) -> np.ndarray:
    """按 factor×factor 块取平均；边上不足一块的先按边缘值补齐（增益图尺寸向上取整）。"""
    gh, gw = grid_shape(a.shape, factor)
    pad = ((0, gh * factor - a.shape[0]), (0, gw * factor - a.shape[1])) + ((0, 0),) * (a.ndim - 2)
    a = np.pad(a, pad, mode="edge")
    return cast(np.ndarray, a.reshape(gh, factor, gw, factor, *a.shape[2:]).mean(axis=(1, 3)))


def sdr_luma_grid(rgb: np.ndarray, factor: int) -> np.ndarray:
    """SDR 线性亮度的块平均（线性值上平均）。"""
    return block_mean(srgb_to_linear(rgb) @ LUMA, factor)


def _hp(a: np.ndarray) -> np.ndarray:
    a = np.log2(np.maximum(a, 1e-4))
    return cast(np.ndarray, a - ndimage.gaussian_filter(a, 8))


def _norm(a: np.ndarray) -> np.ndarray:
    return cast(np.ndarray, (a - a.mean()) / (a.std() + 1e-6))


def warp(a: np.ndarray, shape: tuple[int, int], s: float, dy: float, dx: float, order: int = 1) -> np.ndarray:
    """把 a 以中心为不动点缩放到 shape（再乘 s），然后平移 (dy, dx)；一次插值完成。"""
    h, w = shape
    zy, zx = h / a.shape[0] * s, w / a.shape[1] * s
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    sy = (yy - dy - (h - 1) / 2) / zy + (a.shape[0] - 1) / 2
    sx = (xx - dx - (w - 1) / 2) / zx + (a.shape[1] - 1) / 2
    return cast(np.ndarray, ndimage.map_coordinates(a.astype(np.float32), [sy, sx], order=order, mode="nearest"))


def _phase_shift(a: np.ndarray, b: np.ndarray) -> tuple[int, int]:
    f = np.fft.fft2(a) * np.conj(np.fft.fft2(b))
    pc = np.real(np.fft.ifft2(f / (np.abs(f) + 1e-9)))
    iy, ix = np.unravel_index(int(np.argmax(pc)), pc.shape)
    h, w = pc.shape
    return int(iy if iy <= h // 2 else iy - h), int(ix if ix <= w // 2 else ix - w)


def _tile_moved(ta: np.ndarray, b: np.ndarray, y0: int, x0: int) -> bool:
    """`ta`（SDR 高通的一块，左上角在 (y0, x0)）在 RAW 高通 `b` 上 ±TILE_REACH 像素内直接找最好的位置。"""
    r = TILE_REACH
    tn = _norm(ta)
    h, w = ta.shape
    here = best = float(np.mean(tn * _norm(b[y0 : y0 + h, x0 : x0 + w])))
    far = False
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            c = float(np.mean(tn * _norm(b[y0 + dy : y0 + dy + h, x0 + dx : x0 + dx + w])))
            if c > best:
                best, far = c, max(abs(dy), abs(dx)) >= 1
    return far and ((best >= TILE_MATCH and best - here > TILE_GAIN) or best - here > 0.1)


def _tiles_moved(a: np.ndarray, b: np.ndarray) -> int:
    """有纹理且存在可靠局部位移的块数，例如机内校正后边角对不上。"""
    h, w = a.shape
    r = TILE_REACH
    moved = 0
    for i in range(TILES):
        for j in range(TILES):
            y0, y1 = max(r, i * h // TILES), min(h - r, (i + 1) * h // TILES)
            x0, x1 = max(r, j * w // TILES), min(w - r, (j + 1) * w // TILES)
            ta = a[y0:y1, x0:x1]
            if ta.size < 64 or float(ta.std()) < TILE_TEXTURE:
                continue
            moved += _tile_moved(ta, b, y0, x0)
    return moved


def _tiles_unverified(sdr_y: np.ndarray, raw_y: np.ndarray) -> int:
    """先拟合共同亮度范围，再在可见纹理上直接核对局部相关，避免把高光截断差异算作错位。"""
    x, y = np.log2(np.maximum(raw_y, 1e-6)), np.log2(np.maximum(sdr_y, 1e-5))
    try:
        cx, cy = fit_curve(x, y, (sdr_y > 0.003) & (sdr_y < 0.97))
    except HdrUnsupported:
        return 1
    a = _hp(sdr_y)
    b = _hp(np.exp2(np.interp(x, cx, cy)))
    h, w = a.shape
    unverified, verified = 0, 0
    visible = (sdr_y > 0.01) & (sdr_y < 0.95)
    for i in range(TILES):
        for j in range(TILES):
            box = (slice(i * h // TILES, (i + 1) * h // TILES), slice(j * w // TILES, (j + 1) * w // TILES))
            mask = visible[box]
            ta, tb = a[box][mask], b[box][mask]
            if not ta.size or float(ta.std()) < TILE_TEXTURE:
                continue
            if ta.size < 32 or float(np.mean(_norm(ta) * _norm(tb))) < MIN_TILE_CORRELATION:
                unverified += 1
            else:
                verified += 1
    return unverified if verified else max(unverified, 1)


def _local_residual(sdr_y: np.ndarray, raw_y: np.ndarray) -> bool:
    """局部亮度关系的残差必须落在观测误差内；连续区域不能被整块背景的高相关掩盖。"""
    factor = math.ceil(max(sdr_y.shape) / RESIDUAL_EDGE)
    sdr, raw = block_mean(sdr_y, factor), block_mean(raw_y, factor)
    a = ndimage.gaussian_filter(np.log2(np.maximum(sdr, 1e-5)), 0.5).astype(np.float64)
    b = ndimage.gaussian_filter(np.log2(np.maximum(raw, 1e-6)), 0.5).astype(np.float64)
    # 截断边缘的混合像素无法拟合局部亮度关系，仍由上面的结构相关检查核对。
    mask = ndimage.minimum_filter(((sdr > 0.01) & (sdr < 0.95)).astype(np.float64), 3)
    for size, support in RESIDUAL_WINDOWS:

        def mean(value: np.ndarray) -> np.ndarray:
            return cast(np.ndarray, ndimage.uniform_filter(value, size, mode="reflect"))

        weight = mean(mask)
        w = np.maximum(weight, 1e-6)
        ma, mb = mean(a * mask) / w, mean(b * mask) / w
        va = np.maximum(mean(a * a * mask) / w - ma * ma, 0)
        vb = np.maximum(mean(b * b * mask) / w - mb * mb, 0)
        cov = mean(a * b * mask) / w - ma * mb
        slope = np.clip(cov / np.maximum(vb, 1e-8), 0, 8)
        residual = np.abs((a - ma) - slope * (b - mb))
        sigma = np.sqrt(np.maximum(va - 2 * slope * cov + slope * slope * vb, 0))
        uncertain = (residual > np.maximum(0.125, 5 * sigma)) & (weight > 0.5) & (mask > 0)
        labels, _count = ndimage.label(uncertain)
        counts = np.bincount(labels.ravel())
        blocks = ndimage.minimum_filter(uncertain.astype(np.uint8), 2) > 0
        if bool(np.any(blocks & (counts[labels] >= support))):
            return True
    return False


def align(sdr_y: np.ndarray, raw_y: np.ndarray) -> Alignment:
    """把 RAW 亮度（已转到 SDR 的方向）对齐到 SDR 的增益图网格。"""
    a = _norm(_hp(sdr_y))

    def corr_at(s: float, dy: int, dx: int) -> float:
        return float(np.mean(a * _norm(_hp(warp(raw_y, a.shape, s, dy, dx)))))

    best: tuple[float, float, int, int] | None = None
    for s in SCALES:
        dy, dx = _phase_shift(a, _norm(_hp(warp(raw_y, a.shape, s, 0, 0))))
        corr = corr_at(s, dy, dx)
        if s == 1.0 and dy == 0 and dx == 0 and corr > 0.6:
            best = (corr, s, dy, dx)
            break  # 已经对齐得很好，不再试缩放
        # 相位相关的峰偶尔差一格：在 ±1 格里直接比相关系数
        phase_y, phase_x = dy, dx
        for cand in ((corr_at(s, phase_y + i, phase_x + j), s, phase_y + i, phase_x + j) for i in (-1, 0, 1) for j in (-1, 0, 1) if i or j):
            if cand[0] > corr:
                corr, dy, dx = cand[0], cand[2], cand[3]
        if best is None or corr > best[0]:
            best = (corr, s, dy, dx)
    assert best is not None  # nosec B101  SCALES 不是空的
    corr, s, dy, dx = best
    registered = warp(raw_y, a.shape, s, dy, dx)
    ha, hb = _hp(sdr_y), _hp(registered)
    moved = _tiles_moved(ha, hb)
    unverified = _tiles_unverified(sdr_y, registered) + int(_local_residual(sdr_y, registered))
    return Alignment(s, dy, dx, corr, moved, max(abs(dy) / a.shape[0], abs(dx) / a.shape[1]), unverified)


def check_alignment(al: Alignment) -> None:
    if al.correlation < MIN_CORRELATION:
        raise HdrUnsupported("not_aligned", f"RAW 与 JPG 对不齐（相关系数 {al.correlation:.2f}）")
    if al.shift_frac > MAX_SHIFT:
        raise HdrUnsupported("not_aligned", f"RAW 与 JPG 错开太多（{al.dy}, {al.dx}）")
    if al.tiles_moved:
        raise HdrUnsupported("not_aligned", f"RAW 与 JPG 局部对不齐（{al.tiles_moved} 块）")
    if al.tiles_unverified:
        raise HdrUnsupported("not_aligned", f"无法确认 RAW 与 JPG 局部对齐（{al.tiles_unverified} 块）")


def register(raw: RawLuma, shape: tuple[int, int], al: Alignment, grid: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    """按对齐结果把 RAW 亮度和过曝掩码重采样到 `shape`（`grid` 是对齐时用的网格，平移按比例换算）。"""
    ky, kx = shape[0] / grid[0], shape[1] / grid[1]
    y = np.maximum(warp(raw.y, shape, al.scale, al.dy * ky, al.dx * kx), 1e-6)
    clip = warp(raw.clip.astype(np.float32), shape, al.scale, al.dy * ky, al.dx * kx) > 0.01
    return y, clip


def fit_curve(x: np.ndarray, y: np.ndarray, valid: np.ndarray, step: float = 0.125) -> tuple[np.ndarray, np.ndarray]:
    """log2 RAW 亮度 → log2 SDR 亮度 的单调曲线（分箱中位数 + 单调化 + 5 点平滑）。"""
    xs, ys = x[valid], y[valid]
    if xs.size < 100:
        raise HdrUnsupported("no_curve", "可用于拟合的像素太少")
    lo, hi = np.percentile(xs, 0.5), np.percentile(xs, 99.95)
    edges = np.arange(lo, hi + step, step)
    idx = np.digitize(xs, edges)
    cx: list[float] = []
    cy: list[float] = []
    for k in range(1, len(edges)):
        sel = idx == k
        if sel.sum() >= 30:
            cx.append(float(0.5 * (edges[k - 1] + edges[k])))
            cy.append(float(np.median(ys[sel])))
    if len(cx) < 2:
        raise HdrUnsupported("no_curve", "亮度范围太窄，拟合不出曲线")
    ax, ay = np.array(cx), np.maximum.accumulate(np.array(cy))
    if len(ay) >= 5:
        ay = np.convolve(np.pad(ay, 2, mode="edge"), np.ones(5) / 5, mode="valid")
    return ax, ay


def compute_gain(sdr_y: np.ndarray, raw_y: np.ndarray, clip: np.ndarray, p: Params) -> GainResult:
    """增益图网格上的 SDR 线性亮度、对齐好的 RAW 亮度（NaN = 这里不是照片：边框、日期戳）、过曝掩码 → log2 增益。"""
    photo = np.isfinite(raw_y)
    x = np.log2(np.where(photo, np.maximum(raw_y, 1e-6), 1.0))
    y = np.log2(np.maximum(sdr_y, 1e-5))
    valid = photo & ~clip & (sdr_y > 0.003) & (sdr_y < 0.97)
    cx, cy = fit_curve(x, y, valid)
    mid = valid & (sdr_y > 0.10) & (sdr_y < 0.30)
    ev = float(np.median(y[mid] - x[mid])) if mid.sum() > 100 else float("nan")
    yk = math.log2(p.knee)
    if cy.max() <= yk:  # 整张图都没有亮到拐点（很暗的夜景）：拐点放在曲线最高处
        xk, yk = float(cx[-1]), float(cy[-1])
    else:
        xk = float(np.interp(yk, cy, cx))
    t_sdr = np.minimum(np.interp(x, cx, cy, left=cy[0], right=cy[-1]), 0.0)  # 超出数据范围按最高值平顶
    y_hdr = np.where(x > xk, yk + p.hdr_slope * (x - xk), t_sdr)
    delta = np.clip(y_hdr - t_sdr, 0.0, None)
    delta = p.max_stops * np.tanh(delta / p.max_stops)  # 软上限
    delta = np.where(photo, delta, 0.0)
    delta = ndimage.gaussian_filter(delta.astype(np.float32), 0.6)  # 轻微平滑（在门控之前，免得把增益抹进暗块）
    gate = np.clip((sdr_y - p.gate_lo) / (p.gate_hi - p.gate_lo), 0.0, 1.0)
    gate = gate * gate * (3 - 2 * gate)
    delta = (delta * gate * photo).astype(np.float32)
    gmax = round(float(max(delta.max(), 0.05)), 4)
    meta = GainMeta(gain_min=0.0, gain_max=gmax, cap_min=0.0, cap_max=gmax)
    stats = {
        "gainmap_wh": [int(delta.shape[1]), int(delta.shape[0])],
        "raw_clipped_frac": round(float(clip[photo].mean()) if photo.any() else 0.0, 4),
        "ev_raw_to_sdr_mid": None if math.isnan(ev) else round(ev, 3),
        "knee_raw_log2": round(xk, 3),
        "gain_max_stops": round(gmax, 3),
        "frac_gain_gt_0p1_stop": round(float((delta > 0.1).mean()), 4),
        "frac_gain_gt_1_stop": round(float((delta > 1.0).mean()), 4),
    }
    return GainResult(delta, meta, stats)


def decode_rgb(jpeg: bytes) -> np.ndarray:
    """按存储方向解码（不看 EXIF 方向）：增益图要跟主图的存储方向一致。"""
    _segs, _sos, eoi = jpeg_segments(jpeg)
    with Image.open(io.BytesIO(jpeg[:eoi])) as im:
        return np.asarray(im.convert("RGB"))


def jpeg_orientation(jpeg: bytes) -> int:
    with Image.open(io.BytesIO(jpeg)) as im:
        return int(im.getexif().get(274, 1) or 1)


def package(jpeg: bytes, result: GainResult, p: Params) -> bytes:
    return assemble(jpeg, encode_gainmap(result.delta, result.meta, p.quality), result.meta)


def hdr_from_camera_jpeg(jpeg: bytes, raw: RawLuma, p: Params | None = None) -> tuple[bytes, dict[str, Any]]:
    """相机 JPG 原样做主图，加上从 RAW 算出的增益图。对不齐抛 `HdrUnsupported`。"""
    p = p or Params()
    check_capture(jpeg, raw)
    rgb = decode_rgb(jpeg)
    orientation = jpeg_orientation(jpeg)
    sdr_y = sdr_luma_grid(rgb, p.map_factor)
    stored = RawLuma(to_storage(raw.y, orientation), to_storage(raw.clip, orientation))
    al = align(sdr_y, stored.y)
    check_alignment(al)
    raw_y, clip = register(stored, sdr_y.shape, al, sdr_y.shape)
    result = compute_gain(sdr_y, raw_y, clip, p)
    out = package(jpeg, result, p)
    return out, {**result.stats, "alignment": al.to_dict(), "jpeg_orientation": orientation}
