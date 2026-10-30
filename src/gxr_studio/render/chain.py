"""配方链：白平衡后码值 →（精确）逐像素 `Model.predict`，或（烘焙）把它拆成能快速套用的几段（ARCHITECTURE §5.1、§5.2）。

精确链（`exact_*`）：
    码值 × 增益 → `Model.predict(entry, ·)`（E0 冻结的电脑预测模型）→ 显示域调子表 → 量化。
    增益和调子表都是恒等时，结果与 E0 的 `predict(quantize=True)` 逐字节相同。

`Model.predict` 的结构：每通道一维曲线 `pre` → 线性矩阵（Y、Cb、Cr）→ **只依赖 (Cb, Cr) 的**24 扇区色表 → BT.601 → RGB。
一维曲线的节点本来就在整数码值上，所以 4096 项的表加线性插值和 np.interp 一致到浮点误差。有两种烘焙：

* `chroma`（线上用的）：保留线性矩阵，只把色表那一步烘成 (Cb, Cr) 平面上的**复数增益**网格 G(c) = 输出色度 / 输入色度，
  双线性插值后再乘回输入色度。G 在灰轴附近（kernel 的 σ 随色度变小而变大）是平滑的，乘回 c 以后灰轴上严格不动。
* `lut3d`（ARCHITECTURE §5.2 原方案：33³ 三维表 + 四面体插值）：留作对照。色表在灰轴附近的色相依赖在 RGB 网格里表达不好，
  实测误差大得多（见 research/studio_render_s6/REPORT.md）。

两者的"后半段"都不复制研究代码：`lut3d` 用 `Model` 的浅拷贝（`pre` 原样通过）直接调 `predict`；
`chroma` 里取系数的几行与 `predict` 逐字相同，测试里用 24 个条目对拍。
"""

from __future__ import annotations

import copy
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, cast

import numpy as np
from scipy.spatial import ConvexHull

from ..engine import knobs as k
from ..engine.model import Model
from .tones import apply_table

DEFAULT_GRID = 33
CODE_MAX = 4095
CHUNK = 1 << 16  # 烘焙套用时每块的像素数
EXACT_CHUNK = 1 << 15  # 精确链每块的像素数（predict 的中间数组很大）
WORKERS = max(1, min(4, os.cpu_count() or 1))

_pool: ThreadPoolExecutor | None = None


def pool() -> ThreadPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="gxr-render")
    return _pool


def _post_model(model: Any) -> Any:
    """`Model` 的浅拷贝，`pre` 原样通过：`predict` 剩下的就是矩阵 + 色表 + YCC 的后半段（逐字取自 E0）。"""
    shim = getattr(model, "_gxr_post", None)
    if shim is None:
        shim = copy.copy(model)
        shim.pre = lambda b, x, channel_mode=None: x
        model._gxr_post = shim
    return shim


def _map_chunks(fn: Any, n: int, chunk: int, workers: int) -> list[Any]:
    spans = [(a, min(n, a + chunk)) for a in range(0, n, chunk)]
    if workers <= 1 or len(spans) == 1:
        return [fn(a, b) for a, b in spans]
    return list(pool().map(lambda ab: fn(*ab), spans))


def quantize(rgb: np.ndarray) -> np.ndarray:
    """与 `Model.predict(quantize=True)` 同一个取整：clip 之后 floor(x + .5)。"""
    return cast(np.ndarray, np.floor(np.clip(rgb, 0, 255) + 0.5).astype(np.uint8))


# ---------------------------------------------------------------- 精确链


def exact_float(
    model: Any,
    entry: bytes,
    codes: np.ndarray,
    gains: np.ndarray | None,
    table: np.ndarray | None,
    *,
    workers: int = WORKERS,
) -> np.ndarray:
    """(…, 3) 码值 → (n, 3) 0..255 浮点（未量化）。"""
    x = codes.reshape(-1, 3)
    out = np.empty((len(x), 3), np.float32 if table is not None else np.float64)

    def run(a: int, b: int) -> None:
        xi = x[a:b] if gains is None else x[a:b].astype(np.float64) * gains
        rgb = model.predict(entry, xi)
        out[a:b] = apply_table(rgb, table) if table is not None else rgb

    _map_chunks(run, len(x), EXACT_CHUNK, workers)
    return out


def exact_u8(model: Any, entry: bytes, codes: np.ndarray, gains: Any, table: Any, *, workers: int = WORKERS) -> np.ndarray:
    return quantize(exact_float(model, entry, codes, gains, table, workers=workers)).reshape(codes.shape)


# ---------------------------------------------------------------- 烘焙


@dataclass(frozen=True)
class Bake:
    """一个配方条目在一个模型上的烘焙结果（不含显示域调子表，所以可以在多种调子下复用）。"""

    pre: np.ndarray  # (4096, 3) float32：每通道一维曲线在整数码值上的输出（0..255）
    lut: np.ndarray  # (G, G, G, 3) float32：曲线输出域三个数 → 模型输出 RGB（0..255，已夹紧），索引 [r][g][b]
    grid: int

    def folded(self, table: np.ndarray | None) -> np.ndarray:
        """把显示域调子表折进 3D 表节点：返回 (G³, 3) float32，供 `apply_baked` 用。"""
        flat = self.lut.reshape(-1, 3)
        return np.ascontiguousarray(apply_table(flat, table) if table is not None else flat, np.float32)


def bake_model(model: Any, entry: bytes, grid: int = DEFAULT_GRID, *, workers: int = WORKERS) -> Bake:
    pre = model.pre(entry, np.repeat(np.arange(CODE_MAX + 1, dtype=np.float64)[:, None], 3, axis=1))
    axis = np.linspace(0.0, 255.0, grid)
    nodes = np.stack(np.meshgrid(axis, axis, axis, indexing="ij"), axis=-1).reshape(-1, 3)
    post = _post_model(model)
    lut = np.empty((len(nodes), 3), np.float32)

    def run(a: int, b: int) -> None:
        lut[a:b] = post.predict(entry, nodes[a:b])

    _map_chunks(run, len(nodes), 4096, workers)
    return Bake(pre.astype(np.float32), lut.reshape(grid, grid, grid, 3), grid)


def _lookup_chunk(x: np.ndarray, gains32: Any, pre_flat: np.ndarray, lut: np.ndarray, grid: int) -> np.ndarray:
    """(n, 3) float32 码值 → (n, 3) float32 0..255（未量化）。"""
    if gains32 is not None:
        x = x * gains32
    x = np.clip(x, 0.0, CODE_MAX)
    i = np.minimum(x.astype(np.int32), CODE_MAX - 1)
    f = x - i
    idx = i * 3 + np.arange(3, dtype=np.int32)
    v0 = np.take(pre_flat, idx)
    p = v0 + (np.take(pre_flat, idx + 3) - v0) * f  # 一维曲线输出，0..255
    q = p * np.float32((grid - 1) / 255.0)
    cell = np.minimum(q.astype(np.int32), grid - 2)
    fr = q - cell
    s0, s1 = grid * grid, grid
    base = cell[:, 0] * s0 + cell[:, 1] * s1 + cell[:, 2]
    # 四面体：把小数部分从大到小排，沿着对角线逐轴累加
    a1 = np.argmax(fr, axis=1)
    a3 = np.argmin(fr, axis=1)
    a3 = np.where(a1 == a3, 2, a3)  # 三个小数全相等时 argmax 和 argmin 都是 0
    a2 = 3 - a1 - a3
    strides = np.array((s0, s1, 1), np.int32)
    off1 = strides[a1]
    off2 = off1 + strides[a2]
    f1 = np.take_along_axis(fr, a1[:, None], 1)
    f3 = np.take_along_axis(fr, a3[:, None], 1)
    f2 = np.take_along_axis(fr, a2[:, None], 1)
    v0 = np.take(lut, base, axis=0)
    v1 = np.take(lut, base + off1, axis=0)
    v2 = np.take(lut, base + off2, axis=0)
    v3 = np.take(lut, base + (s0 + s1 + 1), axis=0)
    return cast(np.ndarray, v0 + (v1 - v0) * f1 + (v2 - v1) * f2 + (v3 - v2) * f3)


def apply_baked_float(codes: np.ndarray, bake: Bake, lut_flat: np.ndarray, gains: np.ndarray | None, *, workers: int = WORKERS) -> np.ndarray:
    """(…, 3) 码值 → (n, 3) float32，0..255 未量化。"""
    x = np.ascontiguousarray(codes.reshape(-1, 3), np.float32)
    pre_flat = np.ascontiguousarray(bake.pre).reshape(-1)
    g32 = None if gains is None else gains.astype(np.float32)
    out = np.empty((len(x), 3), np.float32)

    def run(a: int, b: int) -> None:
        out[a:b] = _lookup_chunk(x[a:b], g32, pre_flat, lut_flat, bake.grid)

    _map_chunks(run, len(x), CHUNK, workers)
    return out


def apply_baked(codes: np.ndarray, bake: Bake, lut_flat: np.ndarray, gains: np.ndarray | None, *, workers: int = WORKERS) -> np.ndarray:
    """(…, 3) 码值 → uint8，形状与输入相同。"""
    x = np.ascontiguousarray(codes.reshape(-1, 3), np.float32)
    pre_flat = np.ascontiguousarray(bake.pre).reshape(-1)
    g32 = None if gains is None else gains.astype(np.float32)
    out = np.empty((len(x), 3), np.uint8)

    def run(a: int, b: int) -> None:
        out[a:b] = quantize(_lookup_chunk(x[a:b], g32, pre_flat, lut_flat, bake.grid))

    _map_chunks(run, len(x), CHUNK, workers)
    return out.reshape(codes.shape)


# ---------------------------------------------------------------- 烘焙（线上用）：矩阵 + (Cb, Cr) 复数增益网格

CHROMA_STEP = 2.0  # 增益网格的间距（Cb、Cr 码值）


@dataclass(frozen=True)
class ChromaBake:
    pre: np.ndarray  # (4096, 3) float32：一维曲线（同 Bake.pre）
    matrix: np.ndarray  # (3, 3) float32：有效矩阵（Q10 系数 / 1024），行 = Y、Cb、Cr
    offset: np.ndarray  # (3,) float32：输出偏移（Y 的偏移、Cb/Cr 已减去 128 的中心）
    gain: np.ndarray  # (ny, nx, 2) float32：G(c) 的实部、虚部；索引 [Cr 格][Cb 格]
    origin: tuple[float, float]  # 网格第 0 格的 (Cb, Cr)
    step: float


def _sector_z(model: Model, entry: bytes) -> np.ndarray:
    """与 `Model.predict` 里取系数、解码 Z 的几行逐字相同。"""
    b = entry
    coeff = np.array([[k.dec(z, 128) for z in b[i : i + 4]] for i in range(115, 211, 4)])
    if np.any(coeff[:, 0] != coeff[:, 3]) or np.any(coeff[:, 1] != -coeff[:, 2]):
        raise ValueError("frozen symmetric sector model cannot predict asymmetric rows")
    ab = coeff[:, :2]
    return model.fs.Z_of_words(ab[:, 0], ab[:, 1], model.engine.ctx)


def bake_chroma(model: Any, entry: bytes, step: float = CHROMA_STEP, *, workers: int = WORKERS) -> ChromaBake:
    pre = model.pre(entry, np.repeat(np.arange(CODE_MAX + 1, dtype=np.float64)[:, None], 3, axis=1))
    row = model.mm.MatrixRow.from_bytes(entry[216:244])
    m = row.effective(100.0)
    off = row.offsets - np.array([0.0, 128.0, 128.0])
    corners = np.array([[r, g, b] for r in (0, 255) for g in (0, 255) for b in (0, 255)], np.float64) @ m.T + off
    lo = np.floor(corners[:, 1:].min(axis=0) / step) * step - step
    hi = np.ceil(corners[:, 1:].max(axis=0) / step) * step + step
    nx, ny = int(round((hi[0] - lo[0]) / step)) + 1, int(round((hi[1] - lo[1]) / step)) + 1
    cb = lo[0] + step * np.arange(nx)
    cr = lo[1] + step * np.arange(ny)
    c = (cb[None, :] + 1j * cr[:, None]).reshape(-1)
    gain = np.ones(len(c), complex)
    if entry[0] != 3:  # 03 黑白：predict 里色度直接过（conditional），增益恒为 1
        z = _sector_z(model, entry)
        table = model.engine.table
        probe = np.where(np.abs(c) < 1e-9, 1e-3 + 0j, c)  # 灰轴上的格点：用无穷小色度的增益
        # 只算套用时会用到的格点：8 个立方体角投影出的凸多边形，外扩两格（双线性要用到相邻格）
        hull = ConvexHull(corners[:, 1:])
        near = np.all(
            hull.equations[:, :2] @ np.column_stack([c.real, c.imag]).T + hull.equations[:, 2:3] <= 2.0 * step * np.sqrt(2.0),
            axis=0,
        )
        todo = np.flatnonzero(near)
        out = np.ones(len(c), complex)

        def run(a: int, b: int) -> None:
            sel = todo[a:b]
            out[sel] = table.forward(probe[sel], z)[0] / probe[sel]

        _map_chunks(run, len(todo), 4096, workers)
        gain = out
    g = np.stack([gain.real, gain.imag], axis=-1).reshape(ny, nx, 2).astype(np.float32)
    return ChromaBake(
        pre.astype(np.float32),
        m.astype(np.float32),
        off.astype(np.float32),
        g,
        (float(lo[0]), float(lo[1])),
        float(step),
    )


def _chroma_chunk(x: np.ndarray, g32: Any, pre_flat: np.ndarray, bk: ChromaBake, table: Any) -> np.ndarray:
    """(n, 3) float32 码值 → (n, 3) float32 0..255（未量化）。"""
    if g32 is not None:
        x = x * g32
    x = np.clip(x, 0.0, CODE_MAX)
    i = np.minimum(x.astype(np.int32), CODE_MAX - 1)
    idx = i * 3 + np.arange(3, dtype=np.int32)
    v0 = np.take(pre_flat, idx)
    p = v0 + (np.take(pre_flat, idx + 3) - v0) * (x - i)
    ycc = p @ bk.matrix.T + bk.offset
    y, cb, cr = ycc[:, 0], ycc[:, 1], ycc[:, 2]
    ny, nx = bk.gain.shape[:2]
    u = (cb - np.float32(bk.origin[0])) * np.float32(1.0 / bk.step)
    v = (cr - np.float32(bk.origin[1])) * np.float32(1.0 / bk.step)
    iu = np.clip(u.astype(np.int32), 0, nx - 2)
    iv = np.clip(v.astype(np.int32), 0, ny - 2)
    fu = (u - iu)[:, None]
    fv = (v - iv)[:, None]
    flat = bk.gain.reshape(-1, 2)
    at = iv * nx + iu
    gg = (np.take(flat, at, axis=0) * (1 - fu) + np.take(flat, at + 1, axis=0) * fu) * (1 - fv) + (
        np.take(flat, at + nx, axis=0) * (1 - fu) + np.take(flat, at + nx + 1, axis=0) * fu
    ) * fv
    cbo = cb * gg[:, 0] - cr * gg[:, 1]
    cro = cb * gg[:, 1] + cr * gg[:, 0]
    out = np.empty((len(x), 3), np.float32)
    out[:, 0] = y + np.float32(1.402) * cro
    out[:, 1] = y - np.float32(0.344136) * cbo - np.float32(0.714136) * cro
    out[:, 2] = y + np.float32(1.772) * cbo
    np.clip(out, 0.0, 255.0, out=out)
    return apply_table(out, table) if table is not None else out


def chroma_float(
    codes: np.ndarray, bk: ChromaBake, gains: np.ndarray | None, table: np.ndarray | None, *, workers: int = WORKERS
) -> np.ndarray:
    """(…, 3) 码值 → (n, 3) float32，0..255 未量化。"""
    x = np.ascontiguousarray(codes.reshape(-1, 3), np.float32)
    pre_flat = np.ascontiguousarray(bk.pre).reshape(-1)
    g32 = None if gains is None else gains.astype(np.float32)
    out = np.empty((len(x), 3), np.float32)

    def run(a: int, b: int) -> None:
        out[a:b] = _chroma_chunk(x[a:b], g32, pre_flat, bk, table)

    _map_chunks(run, len(x), CHUNK, workers)
    return out


def chroma_u8(codes: np.ndarray, bk: ChromaBake, gains: np.ndarray | None, table: np.ndarray | None, *, workers: int = WORKERS) -> np.ndarray:
    """(…, 3) 码值 → uint8，形状与输入相同。每块直接量化，避免整幅 float32 的中间结果。"""
    x = np.ascontiguousarray(codes.reshape(-1, 3), np.float32)
    pre_flat = np.ascontiguousarray(bk.pre).reshape(-1)
    g32 = None if gains is None else gains.astype(np.float32)
    out = np.empty((len(x), 3), np.uint8)

    def run(a: int, b: int) -> None:
        out[a:b] = quantize(_chroma_chunk(x[a:b], g32, pre_flat, bk, table))

    _map_chunks(run, len(x), CHUNK, workers)
    return out.reshape(codes.shape)
