"""BLAS-free sequential Python oracle (with seq LU and gxr_math libm) and Gate A/B helpers."""

from __future__ import annotations

import atexit
import json
import math
import struct
import subprocess  # nosec B404
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import numpy as np

from gxr_studio.lut import spaces
from gxr_studio.lut.cube import Cube
from gxr_studio.lut.photo import _apply as _apply_ref

THRESHOLD_RED_DELTA = 0.00011095497837924685
GRADE_MUL = np.array([1.05, 0.96, 0.91], dtype=np.float64)
GRADE_OFF = np.array([0.02, -0.01, 0.03], dtype=np.float64)
THRESHOLD_OFF = np.array([0.02 - THRESHOLD_RED_DELTA, -0.01, 0.03], dtype=np.float64)

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
    ) -> None:
        self.cube = cube
        self.input_space = input_space
        self.output_space = output_space
        self.input_gamut = input_gamut
        self.output_gamut = output_gamut
        self.input_range = input_range
        self.output_range = output_range
        self.use_libm = use_libm

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
        out, stats = self.cube.apply(x, return_info=True)
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


def _quant_u8_scalar(v: float) -> int:
    return int(math.floor(min(max(v, 0.0), 255.0) + 0.5))


def _check_boundary_point(
    a: float,
    b: float,
    ru8: int,
    rsu8: int,
    *,
    label: str,
    idx: tuple[int, ...],
) -> None:
    if abs(ru8 - rsu8) != 1:
        raise AssertionError(f"{label} u8 diff > 1 at {idx}: ref={ru8} rust={rsu8} ref_f64={a!r} rust_f64={b!r}")
    if not (math.isfinite(a) and math.isfinite(b)):
        raise AssertionError(f"{label} non-finite pre-quantisation value at {idx}: ref_f64={a!r} rust_f64={b!r}")
    gate_a = 1e-12 * max(1.0, abs(a))
    gate_b = 1e-12 * max(1.0, abs(b))
    if abs(a - b) > gate_a:
        raise AssertionError(f"{label} float diff exceeds 1e-12 gate at {idx}: ref_f64={a!r} rust_f64={b!r} diff={abs(a - b)!r}")
    if _quant_u8_scalar(a) != ru8 or _quant_u8_scalar(b) != rsu8:
        raise AssertionError(f"{label} rounded code mismatch at {idx}: ref_f64={a!r}->{ru8} rust_f64={b!r}->{rsu8}")
    k = min(ru8, rsu8)
    boundary = k + 0.5
    ac = min(max(a, 0.0), 255.0)
    bc = min(max(b, 0.0), 255.0)
    between = min(ac, bc) <= boundary <= max(ac, bc)
    within_gate = abs(ac - boundary) <= gate_a and abs(bc - boundary) <= gate_b
    if not (between or within_gate):
        raise AssertionError(f"{label} mismatch not at k+0.5 boundary at {idx}: ref_f64={a!r} rust_f64={b!r} boundary={boundary}")
    print(
        f"[quant-boundary] {label} idx={idx} ref_u8={ru8} rust_u8={rsu8} "
        f"ref_f64={a!r} rust_f64={b!r} boundary={boundary} rel={abs(a - b) / max(1.0, abs(a)):.3e}"
    )


def _assert_gate_a_elem(
    or_val: float,
    rs_val: float,
    or_u8: int,
    rs_u8: int,
    *,
    label: str,
    idx: tuple[int, ...],
) -> None:
    if not (math.isfinite(or_val) and math.isfinite(rs_val)):
        raise AssertionError(f"{label} non-finite Gate A value at {idx}: or_f64={or_val!r} rs_f64={rs_val!r}")
    gate = 1e-12 * max(1.0, abs(or_val))
    if abs(or_val - rs_val) > gate:
        raise AssertionError(
            f"{label} Gate A float64 diff exceeds 1e-12 at {idx}: or_f64={or_val!r} rs_f64={rs_val!r} diff={abs(or_val - rs_val)!r}"
        )
    if or_u8 != rs_u8:
        _check_boundary_point(or_val, rs_val, or_u8, rs_u8, label=label, idx=idx)


def assert_u8_boundary_gate(
    ref_u8: np.ndarray,
    rust_u8: np.ndarray,
    ref_f64: np.ndarray,
    rust_f64: np.ndarray,
    *,
    label: str,
) -> int:
    """Gate A: quantisation boundary gate for CPU uint8 outputs vs oracle; returns allowed mismatch count."""
    assert ref_u8.shape == rust_u8.shape and ref_u8.dtype == np.uint8 and rust_u8.dtype == np.uint8
    assert ref_f64.shape == ref_u8.shape and rust_f64.shape == rust_u8.shape
    assert ref_f64.dtype == np.float64 and rust_f64.dtype == np.float64
    diffs = np.argwhere(ref_u8 != rust_u8)
    for idx_arr in diffs:
        idx = tuple(int(v) for v in idx_arr)
        _check_boundary_point(float(ref_f64[idx]), float(rust_f64[idx]), int(ref_u8[idx]), int(rust_u8[idx]), label=label, idx=idx)
    print(f"[quant-boundary-count] {label}: allowed_points={len(diffs)}")
    return int(len(diffs))


def assert_u8_gate_b(
    py_u8: np.ndarray,
    rust_u8: np.ndarray,
    or_u8: np.ndarray,
    or_f64: np.ndarray,
    rust_f64: np.ndarray,
    *,
    label: str,
    py_f64: np.ndarray | None = None,
) -> int:
    """Gate B: mechanical attribution for CPU uint8 differences vs real Python (allowed iff Gate A holds)."""
    assert py_u8.shape == rust_u8.shape == or_u8.shape
    assert py_u8.dtype == rust_u8.dtype == or_u8.dtype == np.uint8
    assert or_f64.shape == rust_f64.shape == py_u8.shape
    assert or_f64.dtype == rust_f64.dtype == np.float64
    diffs = np.argwhere(py_u8 != rust_u8)
    for idx_arr in diffs:
        idx = tuple(int(v) for v in idx_arr)
        pu8, rsu8, ou8 = int(py_u8[idx]), int(rust_u8[idx]), int(or_u8[idx])
        assert abs(pu8 - rsu8) == 1, f"{label} Gate B diff > 1 at {idx}: py={pu8} rust={rsu8}"
        ov, rsv = float(or_f64[idx]), float(rust_f64[idx])
        _assert_gate_a_elem(ov, rsv, ou8, rsu8, label=f"{label}-gate-a", idx=idx)
        extra = f" py_f64={float(py_f64[idx])!r}" if py_f64 is not None else ""
        print(f"[gate-b] {label} idx={idx} py_u8={pu8} rs_u8={rsu8} or_u8={ou8} or_f64={ov!r} rs_f64={rsv!r}{extra}")
    print(f"[gate-b-count] {label}: allowed_points={len(diffs)}")
    return int(len(diffs))


def _unique_diff_pixels(diffs: np.ndarray) -> tuple[dict[tuple[int, int, int], int], np.ndarray]:
    unique_bgr: list[tuple[int, int, int]] = []
    bgr_row: dict[tuple[int, int, int], int] = {}
    for b, g, r, c in diffs:
        assert int(c) in (0, 1, 2), f"unexpected alpha diff at {(b, g, r, c)}"
        key = (int(b), int(g), int(r))
        if key not in bgr_row:
            bgr_row[key] = len(unique_bgr)
            unique_bgr.append(key)
    px_u8 = np.array([[r, g, b] for b, g, r in unique_bgr], dtype=np.uint8).reshape(-1, 1, 3)
    return bgr_row, px_u8


def assert_table_u8_boundary_gate(
    ref_bytes: bytes,
    rust_bytes: bytes,
    *,
    ref_render_px: Callable[[np.ndarray], np.ndarray],
    rust_render_px: Callable[[np.ndarray], np.ndarray],
    label: str,
) -> int:
    """Gate A: quantisation boundary gate for full 256^3x4 RGBA GPU display tables vs oracle."""
    expected_len = 256**3 * 4
    assert len(ref_bytes) == expected_len and len(rust_bytes) == expected_len
    ref_arr = np.frombuffer(ref_bytes, np.uint8).reshape(256, 256, 256, 4)
    rs_arr = np.frombuffer(rust_bytes, np.uint8).reshape(256, 256, 256, 4)
    assert np.all(ref_arr[..., 3] == 255) and np.all(rs_arr[..., 3] == 255)
    diffs = np.argwhere(ref_arr != rs_arr)
    if len(diffs) == 0:
        print(f"[quant-boundary-count] {label}: allowed_points=0")
        return 0
    bgr_row, px_u8 = _unique_diff_pixels(diffs)
    ref_f64 = ref_render_px(px_u8)
    rs_f64 = rust_render_px(px_u8)
    for b_i, g_i, r_i, c_i in diffs:
        b, g, r, c = int(b_i), int(g_i), int(r_i), int(c_i)
        offset = ((b * 256 + g) * 256 + r) * 4 + c
        row = bgr_row[(b, g, r)]
        _check_boundary_point(
            float(ref_f64[row, 0, c]),
            float(rs_f64[row, 0, c]),
            int(ref_arr[b, g, r, c]),
            int(rs_arr[b, g, r, c]),
            label=f"{label}@off={offset}",
            idx=(b, g, r, c),
        )
    print(f"[quant-boundary-count] {label}: allowed_points={len(diffs)}")
    return int(len(diffs))


def assert_table_u8_gate_b(
    py_bytes: bytes,
    rust_bytes: bytes,
    or_bytes: bytes,
    *,
    or_render_px: Callable[[np.ndarray], np.ndarray],
    rust_render_px: Callable[[np.ndarray], np.ndarray],
    label: str,
    py_render_px: Callable[[np.ndarray], np.ndarray] | None = None,
) -> int:
    """Gate B: mechanical attribution for full 256^3x4 RGBA GPU display tables vs real Python."""
    expected_len = 256**3 * 4
    assert len(py_bytes) == expected_len and len(rust_bytes) == expected_len and len(or_bytes) == expected_len
    py_arr = np.frombuffer(py_bytes, np.uint8).reshape(256, 256, 256, 4)
    rs_arr = np.frombuffer(rust_bytes, np.uint8).reshape(256, 256, 256, 4)
    or_arr = np.frombuffer(or_bytes, np.uint8).reshape(256, 256, 256, 4)
    assert np.all(py_arr[..., 3] == 255) and np.all(rs_arr[..., 3] == 255) and np.all(or_arr[..., 3] == 255)
    diffs = np.argwhere(py_arr != rs_arr)
    if len(diffs) == 0:
        print(f"[gate-b-count] {label}: allowed_points=0")
        return 0
    bgr_row, px_u8 = _unique_diff_pixels(diffs)
    or_f64 = or_render_px(px_u8)
    rs_f64 = rust_render_px(px_u8)
    py_f64 = py_render_px(px_u8) if py_render_px is not None else None
    for b_i, g_i, r_i, c_i in diffs:
        b, g, r, c = int(b_i), int(g_i), int(r_i), int(c_i)
        offset = ((b * 256 + g) * 256 + r) * 4 + c
        pu8, rsu8, ou8 = int(py_arr[b, g, r, c]), int(rs_arr[b, g, r, c]), int(or_arr[b, g, r, c])
        assert abs(pu8 - rsu8) == 1, f"{label} Gate B diff > 1 at offset {offset}: py={pu8} rust={rsu8}"
        row = bgr_row[(b, g, r)]
        ov, rsv = float(or_f64[row, 0, c]), float(rs_f64[row, 0, c])
        _assert_gate_a_elem(ov, rsv, ou8, rsu8, label=f"{label}@off={offset}-gate-a", idx=(b, g, r, c))
        extra = f" py_f64={float(py_f64[row, 0, c])!r}" if py_f64 is not None else ""
        print(
            f"[gate-b] {label} offset={offset} bgr=({b},{g},{r}) c={c} py_u8={pu8} rs_u8={rsu8} or_u8={ou8} or_f64={ov!r} rs_f64={rsv!r}{extra}"
        )
    print(f"[gate-b-count] {label}: allowed_points={len(diffs)}")
    return int(len(diffs))


def make_rust_px_runner(tmp_path: Path, cube_path: Path, spec: dict[str, Any], prefix: str = "px") -> Callable[[np.ndarray], np.ndarray]:
    from support.core_cli import run_npy

    def _run(px: np.ndarray) -> np.ndarray:
        in_p = tmp_path / f"{prefix}.npy"
        np.save(in_p, px)
        return run_npy(["lut-apply", "--cube", str(cube_path), "--space", json.dumps(spec), "--rgb", str(in_p)], tmp_path / f"{prefix}_out.npy")

    return _run


def check_threshold_cpu_case(
    tmp_path: Path,
    label: str,
    cube_text_str: str,
    spec: dict[str, Any],
    py_target_fn: Callable[..., Any],
    *,
    rgb_override: np.ndarray | None = None,
    expect_or_diffs: int = 0,
) -> tuple[int, int]:
    from support.core_cli import run_npy
    from support.lut_cases import assert_rel

    cube_path = tmp_path / f"{label}.cube"
    cube_path.write_text(cube_text_str, encoding="utf-8")
    py_cube = Cube.read(cube_path)
    rgb = rgb_override if rgb_override is not None else np.random.default_rng(42).integers(0, 256, (12, 10, 3), dtype=np.uint8)
    rgb_npy = tmp_path / "rgb.npy"
    np.save(rgb_npy, rgb)
    cli_args = ["lut-apply", "--cube", str(cube_path), "--space", json.dumps(spec), "--rgb", str(rgb_npy)]
    xyz = None
    if spec["input_space"] not in spaces.DISPLAY:
        xyz = np.exp(np.random.default_rng(71).uniform(np.log(0.005), np.log(1.0), rgb.shape))
        xyz_npy = tmp_path / "xyz.npy"
        np.save(xyz_npy, xyz)
        cli_args.extend(["--xyz", str(xyz_npy)])

    or_target = SeqTarget(py_cube, **spec)
    py_target = py_target_fn(py_cube, **spec)
    or_f64 = or_target.render(rgb, xyz)
    py_f64 = py_target.render(rgb, xyz)
    rs_f64 = run_npy(cli_args, tmp_path / "out_f64.npy")
    assert_rel(or_f64, rs_f64, label=f"{label}-or-f64")

    or_u8 = seq_apply(or_target, rgb, xyz)
    py_u8 = _apply_ref(py_target, rgb, xyz)
    rs_u8 = run_npy([*cli_args, "--u8"], tmp_path / "out_u8.npy")
    n_or = assert_u8_boundary_gate(or_u8, rs_u8, or_f64, rs_f64, label=f"{label}-vs-oracle")
    assert n_or == expect_or_diffs, f"{label}: expected {expect_or_diffs} oracle u8 diffs, got {n_or}"
    n_py = assert_u8_gate_b(py_u8, rs_u8, or_u8, or_f64, rs_f64, label=f"{label}-vs-python", py_f64=py_f64)
    return n_or, n_py


def check_threshold_full_table_case(
    tmp_path: Path,
    label: str,
    cube_text_str: str,
    spec: dict[str, Any],
    want_offset: int,
    want_bgr: tuple[int, int, int, int],
    want_or_u8: int | None,
    want_py_u8: int,
    want_rs_u8: int,
    py_target_fn: Callable[..., Any],
    *,
    expect_or_diffs: int = 0,
    expect_py_diffs: int = 1,
) -> None:
    from gxr_studio.gpu.tables import lut_bytes
    from support.core_cli import error_of, run_json

    cube_path = tmp_path / f"{label}.cube"
    cube_path.write_text(cube_text_str, encoding="utf-8")
    py_cube = Cube.read(cube_path)
    or_target = SeqTarget(py_cube, **spec)
    py_target = py_target_fn(py_cube, **spec)
    or_bytes = seq_display_lut_u8(or_target)
    py_bytes, py_meta, py_reason = lut_bytes(py_target)
    assert py_reason is None and py_bytes is not None and py_meta is not None
    out_bin, out_meta = tmp_path / "t.bin", tmp_path / "t.json"
    code, payload = run_json(
        ["lut-table", "--cube", str(cube_path), "--space", json.dumps(spec), "--out", str(out_bin), "--meta", str(out_meta)]
    )
    assert error_of(code, payload) is None and json.loads(out_meta.read_text(encoding="utf-8")) == py_meta
    rs_bytes = out_bin.read_bytes()

    _run_rs_px = make_rust_px_runner(tmp_path, cube_path, spec, "px")

    n_or = assert_table_u8_boundary_gate(or_bytes, rs_bytes, ref_render_px=or_target.render, rust_render_px=_run_rs_px, label=f"{label}-256-or")
    n_py = assert_table_u8_gate_b(
        py_bytes,
        rs_bytes,
        or_bytes,
        or_render_px=or_target.render,
        rust_render_px=_run_rs_px,
        label=f"{label}-256-py",
        py_render_px=py_target.render,
    )
    b, g, r, c = want_bgr
    assert ((b * 256 + g) * 256 + r) * 4 + c == want_offset
    assert n_or == expect_or_diffs and n_py == expect_py_diffs
    if want_or_u8 is not None:
        assert or_bytes[want_offset] == want_or_u8
    assert py_bytes[want_offset] == want_py_u8 and rs_bytes[want_offset] == want_rs_u8


def check_negative_control_case(
    tmp_path: Path,
    label: str,
    cube_text_str: str,
    spec: dict[str, Any],
    rgb: np.ndarray,
    want_fail_px: tuple[int, int],
) -> None:
    """Negative control: verify Gate A passes with use_libm=True and fails at want_fail_px with host **."""
    from support.core_cli import run_npy
    from support.lut_cases import assert_rel

    cube_path = tmp_path / f"{label}-neg.cube"
    cube_path.write_text(cube_text_str, encoding="utf-8")
    py_cube = Cube.read(cube_path)
    rgb_npy = tmp_path / f"{label}-neg-rgb.npy"
    np.save(rgb_npy, rgb)
    rs_f64 = run_npy(
        ["lut-apply", "--cube", str(cube_path), "--space", json.dumps(spec), "--rgb", str(rgb_npy)],
        tmp_path / f"{label}-neg-out.npy",
    )

    or_libm = SeqTarget(py_cube, **spec, use_libm=True).render(rgb)
    assert_rel(or_libm, rs_f64, label=f"{label}-libm-gate-a")

    or_host = SeqTarget(py_cube, **spec, use_libm=False).render(rgb)
    limit = 1e-12 * np.maximum(1.0, np.abs(or_host))
    rel = np.abs(or_host - rs_f64) / np.maximum(1.0, np.abs(or_host))
    bad = np.abs(or_host - rs_f64) > limit
    r_idx, c_idx = want_fail_px
    assert np.any(bad[r_idx, c_idx]), f"{label} host ** unexpectedly passed Gate A at {want_fail_px}: max_rel={float(np.max(rel)):.3e}"
    print(f"[neg-control] {label} host ** failed Gate A at {want_fail_px} max_rel={float(np.max(rel[r_idx, c_idx])):.3e}")


def check_blas_gamma24_and_threshold_points(tmp_path: Path, py_target_fn: Callable[..., Any]) -> None:
    """PORT-WAVE-1 platform BLAS rule: gamma24 [141, 136, 0] float64 and threshold.cube CPU u8 points."""
    from support.core_cli import run_npy
    from support.lut_cases import assert_rel, cube_text

    rgb = np.random.default_rng(42).integers(0, 256, (12, 10, 3), dtype=np.uint8)
    assert rgb[0, 8].tolist() == [141, 136, 0]
    rgb_npy = tmp_path / "rgb.npy"
    np.save(rgb_npy, rgb)
    spec = {"input_space": "gamma24", "output_space": "rec709", "input_range": "legal", "output_range": "legal", "output_gamut": "rec2020"}

    grade_cube = tmp_path / "grade.cube"
    grade_cube.write_text(cube_text(lambda x: np.clip(x * GRADE_MUL + GRADE_OFF, 0, 1), size=9), encoding="utf-8")
    py_grade = Cube.read(grade_cube)
    or_grade_f64 = SeqTarget(py_grade, **spec).render(rgb)
    py_grade_f64 = py_target_fn(py_grade, **spec).render(rgb)
    rs_grade_f64 = run_npy(["lut-apply", "--cube", str(grade_cube), "--space", json.dumps(spec), "--rgb", str(rgb_npy)], tmp_path / "grade.npy")
    assert_rel(or_grade_f64, rs_grade_f64, label="grade-vs-oracle")
    if sys.platform == "darwin":
        assert abs(float(py_grade_f64[0, 8, 0]) - float(or_grade_f64[0, 8, 0])) > 1e-7
        fma_px = fma_render_display_px(py_grade, rgb[0, 8], **spec)
        assert float(py_grade_f64[0, 8, 0]) == float(fma_px[0]) and float(py_grade_f64[0, 8, 1]) == float(fma_px[1])
    assert_rel(or_grade_f64[0:1, 8:9, :], py_target_fn(py_grade, **spec).render(rgb[0:1, 8:9, :]), label="grade-1px-vs-oracle")

    thresh_cube = tmp_path / "threshold.cube"
    thresh_cube.write_text(
        cube_text(lambda x: np.clip(x * GRADE_MUL + THRESHOLD_OFF, 0, 1), size=9, title="review threshold synthetic"), encoding="utf-8"
    )
    py_thresh = Cube.read(thresh_cube)
    or_t, py_t = SeqTarget(py_thresh, **spec), py_target_fn(py_thresh, **spec)
    or_t_f64, py_t_f64 = or_t.render(rgb), py_t.render(rgb)
    rs_t_f64 = run_npy(["lut-apply", "--cube", str(thresh_cube), "--space", json.dumps(spec), "--rgb", str(rgb_npy)], tmp_path / "t_f64.npy")
    assert_rel(or_t_f64, rs_t_f64, label="threshold-vs-oracle")
    or_t_u8, py_t_u8 = seq_apply(or_t, rgb), _apply_ref(py_t, rgb)
    rs_t_u8 = run_npy(
        ["lut-apply", "--cube", str(thresh_cube), "--space", json.dumps(spec), "--rgb", str(rgb_npy), "--u8"], tmp_path / "t_u8.npy"
    )
    assert np.array_equal(or_t_u8, rs_t_u8) and int(rs_t_u8[0, 8, 0]) == int(or_t_u8[0, 8, 0]) == 189
    if sys.platform == "darwin":
        diff_idx = np.argwhere(py_t_u8 != rs_t_u8).tolist()
        assert diff_idx == [[0, 8, 0]], diff_idx
        assert int(py_t_u8[0, 8, 0]) == 188
        fma_t = fma_render_display_px(py_thresh, rgb[0, 8], **spec)
        assert float(py_t_f64[0, 8, 0]) == float(fma_t[0]) < 188.5 < float(rs_t_f64[0, 8, 0])
    assert int(_apply_ref(py_t, rgb[0:1, 8:9, :])[0, 0, 0]) == 189
