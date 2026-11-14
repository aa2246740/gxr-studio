"""BLAS-free sequential Python oracle (with seq LU and gxr_math libm) and Gate A/B helpers."""

from __future__ import annotations

import atexit
import math
import struct
import subprocess  # nosec B404
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np

from gxr_studio.lut import spaces
from gxr_studio.lut.cube import Cube
from gxr_studio.lut.photo import _apply as _apply_ref

_OP_POW = 0
_OP_POW_BASE = 1
_OP_LOG10 = 2
_OP_LOG2 = 3


class _LibmStream:
    """Persistent `gxr-cli lut-libm --stream` subprocess evaluating pinned gxr_math/libm ops."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None

    def _ensure_proc(self) -> subprocess.Popen[bytes]:
        if self._proc is None or self._proc.poll() is not None:
            import os

            from support.core_cli import CLI

            if not CLI.is_file() or not os.access(CLI, os.X_OK):
                raise RuntimeError(f"gxr-cli binary not built at {CLI}")
            self._proc = subprocess.Popen(  # noqa: S603  # nosec B603
                [str(CLI), "lut-libm", "--stream"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
            )
        return self._proc

    def call(self, op_tag: int, param: float, values: np.ndarray) -> np.ndarray:
        arr = np.ascontiguousarray(values, dtype="<f8")
        if arr.size == 0:
            return np.empty(arr.shape, dtype=np.float64)
        payload = struct.pack("<BdQ", op_tag, float(param), arr.size) + arr.tobytes()
        nbytes = arr.size * 8
        with self._lock:
            proc = self._ensure_proc()
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(payload)
            proc.stdin.flush()
            chunks = []
            rem = nbytes
            while rem > 0:
                chunk = proc.stdout.read(rem)
                if not chunk:
                    raise RuntimeError("gxr-cli lut-libm --stream exited unexpectedly")
                chunks.append(chunk)
                rem -= len(chunk)
        return np.frombuffer(b"".join(chunks), dtype="<f8").reshape(arr.shape).copy()

    def close(self) -> None:
        with self._lock:
            if self._proc is not None:
                try:
                    if self._proc.stdin is not None:
                        self._proc.stdin.close()
                    self._proc.wait(timeout=5)
                except Exception:
                    self._proc.kill()
                finally:
                    self._proc = None


_LIBM_STREAM = _LibmStream()
atexit.register(_LIBM_STREAM.close)


def _libm_pow(x: np.ndarray, y: float, *, use_libm: bool) -> np.ndarray:
    arr = np.asarray(x, dtype=np.float64)
    return _LIBM_STREAM.call(_OP_POW, y, arr) if use_libm else arr**y


def _libm_pow_base(base: float, exp: np.ndarray, *, use_libm: bool) -> np.ndarray:
    arr = np.asarray(exp, dtype=np.float64)
    return _LIBM_STREAM.call(_OP_POW_BASE, base, arr) if use_libm else base**arr


def _libm_log10(x: np.ndarray, *, use_libm: bool) -> np.ndarray:
    arr = np.asarray(x, dtype=np.float64)
    return _LIBM_STREAM.call(_OP_LOG10, 0.0, arr) if use_libm else np.log10(arr)


def _libm_log2(x: np.ndarray, *, use_libm: bool) -> np.ndarray:
    arr = np.asarray(x, dtype=np.float64)
    return _LIBM_STREAM.call(_OP_LOG2, 0.0, arr) if use_libm else np.log2(arr)


def _getrf_3x3(a: list[list[float]]) -> list[int]:
    """Exact sequential IEEE `dgetf2` LU with partial pivoting matching `gxr-raw::linalg::getrf`."""
    ipiv = [0, 0, 0]
    singular = False
    min_pos = sys.float_info.min
    for j in range(3):
        p = j
        best = abs(a[j][j])
        for i in range(j + 1, 3):
            if abs(a[i][j]) > best:
                best = abs(a[i][j])
                p = i
        ipiv[j] = p
        if a[p][j] == 0.0:
            singular = True
        else:
            a[j], a[p] = a[p], a[j]
            pivot = a[j][j]
            if abs(pivot) >= min_pos:
                r = 1.0 / pivot
                for i in range(j + 1, 3):
                    a[i][j] *= r
            else:
                for i in range(j + 1, 3):
                    a[i][j] /= pivot
        for i in range(j + 1, 3):
            l_val = a[i][j]
            for k in range(j + 1, 3):
                t = -a[j][k]
                a[i][k] += l_val * t
    if singular:
        raise np.linalg.LinAlgError("Singular matrix")
    return ipiv


def _getrs_3x3(lu: list[list[float]], ipiv: list[int], b: list[float]) -> list[float]:
    """Exact sequential IEEE `dgetrs` substitution matching `gxr-raw::linalg::getrs`."""
    x = [float(b[0]), float(b[1]), float(b[2])]
    for j in range(3):
        p = ipiv[j]
        x[j], x[p] = x[p], x[j]
    for k in range(3):
        if x[k] != 0.0:
            for i in range(k + 1, 3):
                x[i] -= x[k] * lu[i][k]
    for k in range(2, -1, -1):
        if x[k] != 0.0:
            x[k] /= lu[k][k]
            for i in range(k):
                x[i] -= x[k] * lu[i][k]
    return x


def solve_seq(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Sequential IEEE `np.linalg.solve(a, b)` for 3x3 float64 matrices (no LAPACK/FMA)."""
    lu = [[float(a[i, j]) for j in range(3)] for i in range(3)]
    ipiv = _getrf_3x3(lu)
    return np.array(_getrs_3x3(lu, ipiv, [float(b[0]), float(b[1]), float(b[2])]), dtype=np.float64)


def inv_seq(a: np.ndarray) -> np.ndarray:
    """Sequential IEEE `np.linalg.inv(a)` for 3x3 float64 matrices (no LAPACK/FMA)."""
    lu = [[float(a[i, j]) for j in range(3)] for i in range(3)]
    ipiv = _getrf_3x3(lu)
    out = [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]
    eye = ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0])
    for j, e in enumerate(eye):
        col = _getrs_3x3(lu, ipiv, e)
        for i in range(3):
            out[i][j] = col[i]
    return np.array(out, dtype=np.float64)


def matrix_seq(gamut: str) -> np.ndarray:
    """Sequential IEEE mirror of `gxr_studio.lut.spaces.matrix(gamut)`."""
    xy = np.asarray(spaces.PRIMARIES[gamut], dtype=np.float64)
    xyz = np.stack([xy[:, 0] / xy[:, 1], np.ones(3), (1.0 - xy.sum(axis=1)) / xy[:, 1]])
    white = np.array([0.3127 / 0.3290, 1.0, (1.0 - 0.3127 - 0.3290) / 0.3290], dtype=np.float64)
    return xyz * solve_seq(xyz, white)[None, :]


def seq_encode(x: np.ndarray, space: str, *, use_libm: bool = True) -> np.ndarray:
    """Mirror of `spaces.encode` routing elementary functions through `gxr_math`/libm when `use_libm=True`."""
    arr = np.asarray(x, dtype=np.float64)
    if space == "linear":
        return arr.copy()
    if space == "srgb":
        return np.where(arr <= 0.0031308, 12.92 * arr, 1.055 * _libm_pow(np.maximum(arr, 0.0), 1.0 / 2.4, use_libm=use_libm) - 0.055)
    if space == "rec709":
        return np.where(arr < 0.018, 4.5 * arr, 1.099 * _libm_pow(np.maximum(arr, 0.0), 0.45, use_libm=use_libm) - 0.099)
    if space == "gamma24":
        return np.sign(arr) * _libm_pow(np.abs(arr), 1.0 / 2.4, use_libm=use_libm)
    if space in ("flog", "flog2"):
        a, b, c, d, e, f, cut = (
            (0.555556, 0.009468, 0.344676, 0.790453, 8.735631, 0.092864, 0.00089)
            if space == "flog"
            else (5.555556, 0.064829, 0.245281, 0.384316, 8.799461, 0.092864, 0.000889)
        )
        return np.where(arr < cut, e * arr + f, c * _libm_log10(np.maximum(a * arr + b, 1e-30), use_libm=use_libm) + d)
    if space in ("llog", "llog709"):
        return np.where(arr <= 0.006, 8.0 * arr + 0.09, 0.27 * _libm_log10(np.maximum(1.3 * arr + 0.0115, 1e-30), use_libm=use_libm) + 0.6)
    if space == "vlog":
        return np.where(arr < 0.01, 5.6 * arr + 0.125, 0.241514 * _libm_log10(np.maximum(arr + 0.00873, 1e-30), use_libm=use_libm) + 0.598206)
    if space in ("slog3", "slog3-sgamut3"):
        return np.where(
            arr < 0.01125,
            (95.0 + arr * (171.2102946929 - 95.0) / 0.01125) / 1023.0,
            (420.0 + 261.5 * _libm_log10(np.maximum((arr + 0.01) / 0.19, 1e-30), use_libm=use_libm)) / 1023.0,
        )
    if space == "applelog":
        r0, rt, c, beta, gamma, delta = -0.05641088, 0.01, 47.28711236, 0.00964052, 0.08550479, 0.69336945
        return np.where(
            arr >= rt,
            gamma * _libm_log2(np.maximum(arr + beta, 1e-30), use_libm=use_libm) + delta,
            c * _libm_pow(np.maximum(arr - r0, 0.0), 2.0, use_libm=use_libm),
        )
    raise ValueError(f"不支持输入空间：{space}")


def seq_decode(x: np.ndarray, space: str, *, use_libm: bool = True) -> np.ndarray:
    """Mirror of `spaces.decode` routing elementary functions through `gxr_math`/libm when `use_libm=True`."""
    arr = np.asarray(x, dtype=np.float64)
    if space == "linear":
        return arr.copy()
    if space == "srgb":
        return np.where(arr <= 0.04045, arr / 12.92, _libm_pow(np.maximum((arr + 0.055) / 1.055, 0.0), 2.4, use_libm=use_libm))
    if space == "rec709":
        return np.where(arr < 0.081, arr / 4.5, _libm_pow(np.maximum((arr + 0.099) / 1.099, 0.0), 1.0 / 0.45, use_libm=use_libm))
    if space == "gamma24":
        return np.sign(arr) * _libm_pow(np.abs(arr), 2.4, use_libm=use_libm)
    if space in ("flog", "flog2"):
        a, b, c, d, e, f, cut = (
            (0.555556, 0.009468, 0.344676, 0.790453, 8.735631, 0.092864, 0.100537775223865)
            if space == "flog"
            else (5.555556, 0.064829, 0.245281, 0.384316, 8.799461, 0.092864, 0.100686685370811)
        )
        return np.where(arr < cut, (arr - f) / e, (_libm_pow_base(10.0, (arr - d) / c, use_libm=use_libm) - b) / a)
    if space in ("llog", "llog709"):
        return np.where(arr <= 0.1380, (arr - 0.09) / 8.0, (_libm_pow_base(10.0, (arr - 0.6) / 0.27, use_libm=use_libm) - 0.0115) / 1.3)
    if space == "vlog":
        return np.where(arr < 0.181, (arr - 0.125) / 5.6, _libm_pow_base(10.0, (arr - 0.598206) / 0.241514, use_libm=use_libm) - 0.00873)
    if space in ("slog3", "slog3-sgamut3"):
        return np.where(
            arr < 171.2102946929 / 1023.0,
            (arr * 1023.0 - 95.0) * 0.01125 / (171.2102946929 - 95.0),
            _libm_pow_base(10.0, (arr * 1023.0 - 420.0) / 261.5, use_libm=use_libm) * 0.19 - 0.01,
        )
    if space == "applelog":
        r0, rt, c, beta, gamma, delta = -0.05641088, 0.01, 47.28711236, 0.00964052, 0.08550479, 0.69336945
        cut_val = float(c * _libm_pow(np.array([rt - r0], dtype=np.float64), 2.0, use_libm=use_libm)[0])
        return np.where(
            arr >= cut_val,
            _libm_pow_base(2.0, (arr - delta) / gamma, use_libm=use_libm) - beta,
            np.sqrt(np.maximum(arr, 0.0) / c) + r0,
        )
    raise ValueError(f"不支持输出空间：{space}")


def matmul_t_seq(x: np.ndarray, m: np.ndarray) -> np.ndarray:
    """Evaluate `x @ m.T` in plain sequential IEEE index order (no BLAS, no FMA)."""
    a = np.asarray(x, np.float64)
    mat = np.asarray(m, np.float64)
    out = np.empty_like(a)
    for i in range(3):
        out[..., i] = (a[..., 0] * mat[i, 0] + a[..., 1] * mat[i, 1]) + a[..., 2] * mat[i, 2]
    return out


def dot_fma_left(v: np.ndarray, row: np.ndarray) -> float:
    """Left-to-right `math.fma` reproducing Accelerate's FMA contraction order on a 3-vector dot."""
    return math.fma(float(v[2]), float(row[2]), math.fma(float(v[1]), float(row[1]), float(v[0]) * float(row[0])))


def matmul_t_fma(px3: np.ndarray, m: np.ndarray) -> np.ndarray:
    """Single-pixel `px3 @ m.T` using `dot_fma_left`."""
    return np.array([dot_fma_left(px3, m[i]) for i in range(3)], dtype=np.float64)


def linspace_0_1_seq(num: int) -> np.ndarray:
    """Sequential IEEE mirror of `gxr-lut::cube::linspace_0_1` matching `np.linspace(0, 1, num)`."""
    if num == 0:
        return np.empty(0, dtype=np.float64)
    if num == 1:
        return np.array([0.0], dtype=np.float64)
    step = 1.0 / (num - 1)
    out = np.empty(num, dtype=np.float64)
    out[:-1] = np.arange(num - 1, dtype=np.float64) * step
    out[-1] = 1.0
    return out


def interp_1d_seq(x: np.ndarray, xp: np.ndarray, fp: np.ndarray) -> np.ndarray:
    """Sequential IEEE port of numpy's `np.interp(x, xp, fp)` matching `gxr-lut::cube::interp_1d`."""
    x_arr = np.asarray(x, dtype=np.float64)
    xp_arr = np.asarray(xp, dtype=np.float64)
    fp_arr = np.asarray(fp, dtype=np.float64)
    n = len(xp_arr)
    if n == 0:
        raise ValueError("array of sample points is empty")
    if n == 1:
        return np.where(np.isnan(x_arr), np.nan, fp_arr[0])
    out = np.empty(x_arr.shape, dtype=np.float64)
    is_nan = np.isnan(x_arr)
    hi = x_arr >= xp_arr[-1]
    lo = x_arr < xp_arr[0]
    interior = ~(is_nan | hi | lo)
    out[is_nan] = np.nan
    out[hi] = fp_arr[-1]
    out[lo] = fp_arr[0]
    if np.any(interior):
        xi = x_arr[interior]
        idx = np.searchsorted(xp_arr, xi, side="right")
        j = np.clip(idx - 1, 0, n - 2)
        xp_j = xp_arr[j]
        xp_j1 = xp_arr[j + 1]
        fp_j = fp_arr[j]
        fp_j1 = fp_arr[j + 1]
        with np.errstate(all="ignore"):
            slope = (fp_j1 - fp_j) / (xp_j1 - xp_j)
            prod0 = slope * (xi - xp_j)
            val = prod0 + fp_j
            val_nan = np.isnan(val)
            if np.any(val_nan):
                prod1 = slope * (xi - xp_j1)
                val2 = prod1 + fp_j1
                val = np.where(val_nan, val2, val)
                still_nan = np.isnan(val) & (fp_j == fp_j1)
                val = np.where(still_nan, fp_j, val)
            val = np.where(xi == xp_j, fp_j, val)
        out[interior] = val
    return out


def seq_cube_apply(cube: Cube, rgb: Any, return_info: bool = False, *, use_seq_interp: bool = True) -> Any:
    """Mirror of `Cube.apply` replacing host `np.interp` with `interp_1d_seq` when `use_seq_interp=True`."""
    x = np.asarray(rgb, float)
    if x.shape[-1:] != (3,) or not np.isfinite(x).all():
        raise ValueError("LUT 输入必须为有限 RGB")
    shape = x.shape
    x = x.reshape(-1, 3)
    x = (x - cube.domain_min) / (cube.domain_max - cube.domain_min)
    clipped = np.zeros(len(x), bool)
    if cube.table1 is not None:
        x = (x - cube.range1[0]) / (cube.range1[1] - cube.range1[0])
        clipped |= np.any((x < 0) | (x > 1), axis=1)
        nodes = linspace_0_1_seq(len(cube.table1)) if use_seq_interp else np.linspace(0, 1, len(cube.table1))
        interp_fn = interp_1d_seq if use_seq_interp else np.interp
        x = np.stack([interp_fn(x[:, c], nodes, cube.table1[:, c]) for c in range(3)], axis=-1)
    if cube.table3 is not None:
        x = (x - cube.range3[0]) / (cube.range3[1] - cube.range3[0])
        clipped |= np.any((x < 0) | (x > 1), axis=1)
        x = cube.tetrahedral(np.clip(x, 0, 1))
    y = x.reshape(shape)
    return (y, {"input_clipped_share": float(clipped.mean())}) if return_info else y


class SeqTarget:
    """Drop-in BLAS/LAPACK-free sequential mirror of `gxr_studio.lut.spaces.Target` for Gate A."""

    def __init__(
        self,
        cube: Cube,
        input_space: str,
        output_space: str = "srgb",
        input_gamut: str | None = None,
        output_gamut: str | None = None,
        input_range: str = "full",
        output_range: str = "full",
        *,
        use_libm: bool = True,
        use_seq_interp: bool = True,
    ) -> None:
        self.cube = cube
        self.input_space = input_space
        self.output_space = output_space
        self.input_gamut = input_gamut
        self.output_gamut = output_gamut
        self.input_range = input_range
        self.output_range = output_range
        self.use_libm = use_libm
        self.use_seq_interp = use_seq_interp

    def render(self, stock_rgb: np.ndarray, xyz: np.ndarray | None = None, info: bool = False) -> Any:
        if self.input_space in spaces.DISPLAY:
            lin = seq_decode(np.asarray(stock_rgb) / 255, "srgb", use_libm=self.use_libm)
            xyz_display = matmul_t_seq(lin, matrix_seq("rec709"))
            inv_in = inv_seq(matrix_seq(self.input_gamut or spaces.GAMUT[self.input_space]))
            x = seq_encode(matmul_t_seq(xyz_display, inv_in), self.input_space, use_libm=self.use_libm)
        else:
            if xyz is None:
                raise ValueError("Log LUT 需要 DNG 的场景 XYZ，不能从 JPEG 猜测")
            inv_in = inv_seq(matrix_seq(self.input_gamut or spaces.GAMUT[self.input_space]))
            x = seq_encode(matmul_t_seq(xyz, inv_in), self.input_space, use_libm=self.use_libm)
        if self.input_range == "legal":
            x = (64 + 876 * x) / 1023
        out, stats = seq_cube_apply(self.cube, x, return_info=True, use_seq_interp=self.use_seq_interp)
        if self.output_range == "legal":
            out = (1023 * out - 64) / 876
        lin_out = seq_decode(out, self.output_space, use_libm=self.use_libm)
        xyz_out = matmul_t_seq(lin_out, matrix_seq(self.output_gamut or spaces.GAMUT[self.output_space]))
        srgb = seq_encode(matmul_t_seq(xyz_out, inv_seq(matrix_seq("rec709"))), "srgb", use_libm=self.use_libm)
        stats["display_gamut_clipped_share"] = float(np.any((srgb < 0) | (srgb > 1), axis=-1).mean())
        rgb = np.clip(srgb, 0, 1) * 255
        return (rgb, stats) if info else rgb


def seq_apply(target: SeqTarget, rgb: np.ndarray, xyz: np.ndarray | None = None) -> np.ndarray:
    """Reuse `gxr_studio.lut.photo._apply` directly with `SeqTarget`."""
    return _apply_ref(target, rgb, xyz)


def seq_display_lut_u8(target: SeqTarget) -> bytes:
    """Mirror of `gxr_studio.gpu.tables.display_lut_u8` evaluating through `seq_apply`."""
    ramp = np.arange(256, dtype=np.uint8)

    def block(b0: int) -> np.ndarray:
        grid = np.empty((32, 256, 256, 3), np.uint8)
        grid[..., 0] = ramp[None, None, :]
        grid[..., 1] = ramp[None, :, None]
        grid[..., 2] = np.arange(b0, b0 + 32, dtype=np.uint8)[:, None, None]
        out = np.empty((32, 256, 256, 4), np.uint8)
        out[..., :3] = seq_apply(target, grid.reshape(-1, 256, 3)).reshape(32, 256, 256, 3)
        out[..., 3] = 255
        return out

    with ThreadPoolExecutor(2, thread_name_prefix="gxr-lut-oracle") as pool:
        return b"".join(part.tobytes() for part in pool.map(block, range(0, 256, 32)))


def fma_render_display_px(
    cube: Cube,
    px_u8: np.ndarray,
    *,
    input_space: str,
    output_space: str,
    input_range: str = "full",
    output_range: str = "full",
    input_gamut: str | None = None,
    output_gamut: str | None = None,
) -> np.ndarray:
    """Reproduce Accelerate's FMA-fused batch result on one display pixel using `math.fma`."""
    lin = spaces.decode(np.asarray(px_u8, np.float64) / 255.0, "srgb")
    xyz_d = matmul_t_fma(lin, spaces.matrix("rec709"))
    inv_in = np.linalg.inv(spaces.matrix(input_gamut or spaces.GAMUT[input_space]))
    x = spaces.encode(matmul_t_fma(xyz_d, inv_in), input_space)
    if input_range == "legal":
        x = (64 + 876 * x) / 1023
    out = cube.apply(x[None, :])[0]
    if output_range == "legal":
        out = (1023 * out - 64) / 876
    lin_o = spaces.decode(out, output_space)
    xyz_o = matmul_t_fma(lin_o, spaces.matrix(output_gamut or spaces.GAMUT[output_space]))
    srgb = spaces.encode(matmul_t_fma(xyz_o, np.linalg.inv(spaces.matrix("rec709"))), "srgb")
    return np.clip(srgb, 0, 1) * 255.0


from support.lut_gates import (  # noqa: E402, F401
    GRADE_MUL,
    GRADE_OFF,
    THRESHOLD_OFF,
    THRESHOLD_RED_DELTA,
    assert_table_u8_boundary_gate,
    assert_table_u8_gate_b,
    assert_u8_boundary_gate,
    assert_u8_gate_b,
    check_blas_gamma24_and_threshold_points,
    check_negative_control_case,
    check_threshold_cpu_case,
    check_threshold_full_table_case,
    make_rust_px_runner,
)
