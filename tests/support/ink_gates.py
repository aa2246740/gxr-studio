"""Gate A / Gate B verification helpers and pinned libm / sequential oracles for M4 `ink`."""

from __future__ import annotations

import atexit
import math
import os
import struct
import subprocess  # nosec B404
import threading
from typing import Any

import numpy as np

from support.color_oracle_libm import _elem_exp, _elem_sin
from support.ziggurat_tables import (
    FI_DOUBLE,
    KI_DOUBLE,
    WI_DOUBLE,
    ZIGGURAT_NOR_INV_R,
    ZIGGURAT_NOR_R,
)

_INK_OP_LN = 0
_INK_OP_LOG1P = 1
_INK_OP_EXP = 2
_INK_OP_SIN = 3
_INK_OP_COS = 4


def _ulp_shift(val: float, ulps: int) -> float:
    if ulps == 0 or not math.isfinite(val):
        return val
    bits = struct.unpack("<Q", struct.pack("<d", val))[0]
    shifted = (bits + ulps) & 0xFFFF_FFFF_FFFF_FFFF
    return float(struct.unpack("<d", struct.pack("<Q", shifted))[0])


class _InkLibmStream:
    """Persistent `gxr-cli ink-libm --stream` client for pinned `gxr_math`/libm ops (`ln`/`log1p`/`exp`/`sin`/`cos`)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None

    def _spawn(self) -> subprocess.Popen[bytes]:
        from support.core_cli import CLI

        if not CLI.is_file() or not os.access(CLI, os.X_OK):
            raise RuntimeError(f"gxr-cli binary not built at {CLI}")
        return subprocess.Popen(  # noqa: S603  # nosec B603
            [str(CLI), "ink-libm", "--stream"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def _ensure_proc(self) -> subprocess.Popen[bytes]:
        if self._proc is None or self._proc.poll() is not None:
            self._proc = self._spawn()
        return self._proc

    @staticmethod
    def _kill_proc(proc: subprocess.Popen[bytes]) -> None:
        try:
            if proc.stdin is not None:
                proc.stdin.close()
            proc.wait(timeout=2)
        except Exception:
            proc.kill()

    def call_unary(self, tag: int, x: np.ndarray) -> np.ndarray:
        arr = np.ascontiguousarray(x, dtype="<f8")
        if arr.size == 0:
            return np.empty(arr.shape, dtype=np.float64)
        payload = struct.pack("<BdQ", tag, 0.0, arr.size) + arr.tobytes()
        with self._lock:
            proc = self._ensure_proc()
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(payload)
            proc.stdin.flush()
            need = arr.size * 8
            chunks: list[bytes] = []
            rem = need
            while rem > 0:
                chunk = proc.stdout.read(rem)
                if not chunk:
                    self._kill_proc(proc)
                    self._proc = None
                    raise RuntimeError(f"gxr-cli ink-libm --stream exited on tag {tag}")
                chunks.append(chunk)
                rem -= len(chunk)
        return np.frombuffer(b"".join(chunks), dtype="<f8").copy().reshape(arr.shape)

    def close(self) -> None:
        with self._lock:
            if self._proc is not None:
                self._kill_proc(self._proc)
                self._proc = None


_INK_LIBM = _InkLibmStream()
atexit.register(_INK_LIBM.close)


def libm_log1p_scalar(x: float, *, use_libm: bool = True, perturb_ulp: int = 0) -> float:
    val = float(_INK_LIBM.call_unary(_INK_OP_LOG1P, np.array([x], dtype="<f8"))[0]) if use_libm else math.log1p(x)
    return _ulp_shift(val, perturb_ulp)


def libm_exp_scalar(x: float, *, use_libm: bool = True, perturb_ulp: int = 0) -> float:
    val = float(_elem_exp(np.array([x], dtype=np.float64), use_libm=use_libm)[0])
    return _ulp_shift(val, perturb_ulp)


def libm_exp_vec(x: np.ndarray, *, use_libm: bool = True, perturb_ulp: int = 0) -> np.ndarray:
    arr = np.asarray(_elem_exp(x, use_libm=use_libm), dtype=np.float64)
    if perturb_ulp == 0:
        return arr
    return np.array([_ulp_shift(float(v), perturb_ulp) for v in arr.flat], dtype=np.float64).reshape(arr.shape)


def libm_sin_vec(x: np.ndarray, *, use_libm: bool = True, perturb_ulp: int = 0) -> np.ndarray:
    arr = np.asarray(_elem_sin(x, use_libm=use_libm), dtype=np.float64)
    if perturb_ulp == 0:
        return arr
    return np.array([_ulp_shift(float(v), perturb_ulp) for v in arr.flat], dtype=np.float64).reshape(arr.shape)


def run_ziggurat_from_raw(
    raw_u64: np.ndarray,
    num_draws: int,
    *,
    use_libm: bool = True,
    perturb_ulp: int = 0,
) -> tuple[np.ndarray, list[int], list[int]]:
    """Run exact NumPy Ziggurat `random_standard_normal` over `raw_u64`, tracking tail and wedge draws."""
    inv_2p53 = 1.0 / 9_007_199_254_740_992.0
    out = np.empty(num_draws, dtype=np.float64)
    tail_indices: list[int] = []
    wedge_indices: list[int] = []
    raw_idx = 0
    raw_len = len(raw_u64)

    def next_u64() -> int:
        nonlocal raw_idx
        if raw_idx >= raw_len:
            raise IndexError("raw_u64 stream exhausted")
        v = int(raw_u64[raw_idx])
        raw_idx += 1
        return v

    def next_double() -> float:
        return (next_u64() >> 11) * inv_2p53

    for d_i in range(num_draws):
        while True:
            r = next_u64()
            idx = r & 0xFF
            r >>= 8
            sign = r & 0x1
            rabs = (r >> 1) & 0x000F_FFFF_FFFF_FFFF
            x = rabs * WI_DOUBLE[idx]
            if sign != 0:
                x = -x
            if rabs < KI_DOUBLE[idx]:
                out[d_i] = x
                break
            if idx == 0:
                tail_indices.append(d_i)
                while True:
                    xx = -ZIGGURAT_NOR_INV_R * libm_log1p_scalar(-next_double(), use_libm=use_libm, perturb_ulp=perturb_ulp)
                    yy = -libm_log1p_scalar(-next_double(), use_libm=use_libm, perturb_ulp=perturb_ulp)
                    if yy + yy > xx * xx:
                        out[d_i] = -(ZIGGURAT_NOR_R + xx) if ((rabs >> 8) & 0x1) != 0 else (ZIGGURAT_NOR_R + xx)
                        break
                break
            wedge_indices.append(d_i)
            lhs = (FI_DOUBLE[idx - 1] - FI_DOUBLE[idx]) * next_double() + FI_DOUBLE[idx]
            rhs = libm_exp_scalar(-0.5 * x * x, use_libm=use_libm, perturb_ulp=perturb_ulp)
            if lhs < rhs:
                out[d_i] = x
                break
    return out, tail_indices, wedge_indices


def check_ziggurat_negative_control(
    raw_u64: np.ndarray,
    num_draws: int,
    rust_draws: np.ndarray,
    tail_indices: list[int],
    wedge_indices: list[int],
) -> None:
    """Verify Gate A passes with pinned libm and fails on tail/wedge draws when libm is perturbed by 1 ULP."""
    oracle_good, t_idx, w_idx = run_ziggurat_from_raw(raw_u64, num_draws, use_libm=True, perturb_ulp=0)
    assert t_idx == tail_indices and w_idx == wedge_indices
    assert np.array_equal(oracle_good.view(np.uint64), rust_draws.view(np.uint64)), "Gate A Ziggurat mismatch vs pinned libm oracle"

    oracle_plus, _, _ = run_ziggurat_from_raw(raw_u64, num_draws, use_libm=True, perturb_ulp=1)
    oracle_minus, _, _ = run_ziggurat_from_raw(raw_u64, num_draws, use_libm=True, perturb_ulp=-1)
    assert tail_indices and wedge_indices, "need both tail and wedge hits for negative control"
    tail_diff = bool(
        np.any(oracle_plus[tail_indices].view(np.uint64) != rust_draws[tail_indices].view(np.uint64))
        or np.any(oracle_minus[tail_indices].view(np.uint64) != rust_draws[tail_indices].view(np.uint64))
    )
    assert tail_diff, "Negative control failed: 1-ULP log1p perturbation did not fail Gate A on tail draw"
    wedge_x = float(rust_draws[wedge_indices[0]])
    exp_good = libm_exp_scalar(-0.5 * wedge_x * wedge_x, use_libm=True, perturb_ulp=0)
    exp_bad = libm_exp_scalar(-0.5 * wedge_x * wedge_x, use_libm=True, perturb_ulp=1)
    assert exp_good != exp_bad, "Negative control failed: 1-ULP exp perturbation matched wedge exp"


def attribute_ziggurat_gate_b(
    label: str,
    seed: int,
    rs_u64: np.ndarray,
    rs_norm: np.ndarray,
    py_norm: np.ndarray,
) -> int:
    """Mechanically attribute every differing draw in `rs_norm != py_norm` (Gate B).

    Verifies that every non-identical draw is a Ziggurat tail or wedge draw, that the
    pinned-`libm` oracle matches Rust bitwise at every diff (Gate A), and that the
    host-`libm` (`math.log1p`/`math.exp`) oracle matches real NumPy bitwise.
    """
    diffs = np.argwhere(rs_norm.view(np.uint64) != py_norm.view(np.uint64)).ravel()
    if len(diffs) == 0:
        report_gate_b(label, 0)
        return 0

    max_need = int(diffs[-1]) + 1
    # Replay the u64 stream via fast integer arithmetic up to max_need, evaluating transcendental tail/wedge only when hit.
    inv_2p53 = 1.0 / 9_007_199_254_740_992.0
    raw_idx = 0
    raw_len = len(rs_u64)
    diff_set = {int(d) for d in diffs}
    chains: list[str] = []

    def next_u64() -> int:
        nonlocal raw_idx
        if raw_idx >= raw_len:
            raise IndexError("raw_u64 stream exhausted")
        v = int(rs_u64[raw_idx])
        raw_idx += 1
        return v

    for d_i in range(max_need):
        while True:
            r = next_u64()
            idx = r & 0xFF
            r >>= 8
            sign = r & 0x1
            rabs = (r >> 1) & 0x000F_FFFF_FFFF_FFFF
            if rabs < KI_DOUBLE[idx]:
                if d_i in diff_set:
                    raise AssertionError(f"{label} seed={seed} diff at fast-path draw {d_i}!")
                break
            x = rabs * WI_DOUBLE[idx]
            if sign != 0:
                x = -x
            if idx == 0:
                # Tail draw: consume inner loop draws; compare pinned libm vs host libm if in diff_set.
                is_diff = d_i in diff_set
                while True:
                    u1 = -((next_u64() >> 11) * inv_2p53)
                    u2 = -((next_u64() >> 11) * inv_2p53)
                    xx_h = -ZIGGURAT_NOR_INV_R * math.log1p(u1)
                    yy_h = -math.log1p(u2)
                    near_boundary = abs((yy_h + yy_h) - (xx_h * xx_h)) <= 1e-10
                    if is_diff or near_boundary:
                        xx = -ZIGGURAT_NOR_INV_R * libm_log1p_scalar(u1, use_libm=True)
                        yy = -libm_log1p_scalar(u2, use_libm=True)
                        accept = yy + yy > xx * xx
                    else:
                        xx = xx_h
                        accept = yy_h + yy_h > xx_h * xx_h
                    if accept:
                        val_pin = -(ZIGGURAT_NOR_R + xx) if ((rabs >> 8) & 0x1) != 0 else (ZIGGURAT_NOR_R + xx)
                        if is_diff:
                            val_host = -(ZIGGURAT_NOR_R + xx_h) if ((rabs >> 8) & 0x1) != 0 else (ZIGGURAT_NOR_R + xx_h)
                            b_rs = int(rs_norm[d_i].view(np.uint64))
                            b_py = int(py_norm[d_i].view(np.uint64))
                            b_pin = struct.unpack("<Q", struct.pack("<d", val_pin))[0]
                            b_host = struct.unpack("<Q", struct.pack("<d", val_host))[0]
                            assert b_pin == b_rs, f"{label} tail {d_i}: Gate A mismatch 0x{b_pin:016x} vs 0x{b_rs:016x}"
                            assert b_host == b_py, f"{label} tail {d_i}: host mismatch 0x{b_host:016x} vs 0x{b_py:016x}"
                            chains.append(f"seed={seed} draw={d_i} kind=tail(log1p) py=0x{b_py:016x} rs=0x{b_rs:016x} ulp={abs(b_py - b_rs)}")
                        break
                break
            # Wedge draw: consume 1 double
            u = (next_u64() >> 11) * inv_2p53
            lhs = (FI_DOUBLE[idx - 1] - FI_DOUBLE[idx]) * u + FI_DOUBLE[idx]
            arg = -0.5 * x * x
            rhs_h = math.exp(arg)
            if abs(lhs - rhs_h) <= 1e-11 or d_i in diff_set:
                rhs = libm_exp_scalar(arg, use_libm=True)
            else:
                rhs = rhs_h
            if lhs < rhs:
                if d_i in diff_set:
                    b_rs = int(rs_norm[d_i].view(np.uint64))
                    b_py = int(py_norm[d_i].view(np.uint64))
                    b_pin = struct.unpack("<Q", struct.pack("<d", x))[0]
                    assert b_pin == b_rs
                    chains.append(f"seed={seed} draw={d_i} kind=wedge(exp) py=0x{b_py:016x} rs=0x{b_rs:016x}")
                break
            elif d_i in diff_set:
                # Divergence caused by lhs < exp(-0.5*x*x) flip; verify host exp agrees with reject/accept
                assert lhs >= rhs and lhs < rhs_h, f"{label} wedge {d_i}: diff without exp threshold flip"
                b_rs = int(rs_norm[d_i].view(np.uint64))
                b_py = int(py_norm[d_i].view(np.uint64))
                chains.append(f"seed={seed} draw={d_i} kind=wedge-branch-flip(exp) py=0x{b_py:016x} rs=0x{b_rs:016x}")

    report_gate_b(label, len(diffs), chains=tuple(chains))
    return int(len(diffs))


def report_gate_b(label: str, allowed_points: int, *, chains: tuple[str, ...] = ()) -> None:
    """Print structured Gate B count (and optional value chains) outside redacted test modules."""
    for chain in chains:
        print(f"[gate-b-chain] {label}: {chain}")
    print(f"[gate-b-count] {label}: allowed_points={allowed_points}")


def _pairwise_f32(a: list[np.float32]) -> np.float32:
    n = len(a)
    if n < 8:
        res = np.float32(-0.0)
        for v in a:
            res = np.float32(res + v)
        return res
    if n <= 128:
        r = [a[i] for i in range(8)]
        i = 8
        while i < n - n % 8:
            for j in range(8):
                r[j] = np.float32(r[j] + a[i + j])
            i += 8
        res = np.float32(
            np.float32(np.float32(r[0] + r[1]) + np.float32(r[2] + r[3])) + np.float32(np.float32(r[4] + r[5]) + np.float32(r[6] + r[7]))
        )
        while i < n:
            res = np.float32(res + a[i])
            i += 1
        return res
    n2 = (n // 2) - ((n // 2) % 8)
    return np.float32(_pairwise_f32(a[:n2]) + _pairwise_f32(a[n2:]))


def seq_alpha_luma(rgba: np.ndarray) -> np.float32:
    """Sequential Gate A oracle matching `_alpha_luma` without BLAS reordering."""
    alphas: list[np.float32] = []
    weighted: list[np.float32] = []
    w0, w1, w2 = np.float32(0.299), np.float32(0.587), np.float32(0.114)
    inv255 = np.float32(255.0)
    for r, g, b, a in rgba.reshape(-1, 4):
        af = np.float32(np.float32(a) / inv255)
        luma = np.float32(np.float32(np.float32(r) * w0 + np.float32(g) * w1) + np.float32(b) * w2)
        alphas.append(af)
        weighted.append(np.float32(luma * af))
    alpha_sum = np.float32(np.float32(0.0) + _pairwise_f32(alphas))
    weighted_luma = np.float32(np.float32(0.0) + _pairwise_f32(weighted))
    denom = alpha_sum if float(alpha_sum) > 1e-6 else np.float32(1e-6)
    return np.float32(weighted_luma / denom)


def wobble_oracle(
    n: int,
    rng: np.random.Generator,
    wavelength: float,
    notches: int,
    notch_w: float,
    *,
    use_libm: bool = True,
    perturb_ulp: int = 0,
) -> np.ndarray:
    t = np.arange(n, dtype=np.float64)
    out = np.zeros(n, dtype=np.float64)
    for k, amp in enumerate((1.0, 0.55, 0.3)):
        wl = wavelength / (1.0 + 1.7 * k)
        out += amp * libm_sin_vec(2.0 * math.pi * t / wl + rng.uniform(0.0, 6.28), use_libm=use_libm, perturb_ulp=perturb_ulp)
    out /= 1.85
    for _ in range(notches):
        pos = rng.uniform(0.0, float(n))
        wid = rng.uniform(0.5, 1.3) * notch_w
        scale = rng.choice([-1.0, 1.0]) * rng.uniform(0.7, 1.4)
        out += scale * libm_exp_vec(-(((t - pos) / wid) ** 2), use_libm=use_libm, perturb_ulp=perturb_ulp)
    out += rng.normal(0.0, 0.05, n)
    return np.clip(out, -1.4, 1.4)


def carrier_contours_oracle(
    w: int,
    h: int,
    c: int,
    jit: int,
    seed: int,
    *,
    use_libm: bool = True,
    perturb_ulp: int = 0,
) -> tuple[list[list[float]], list[list[float]]]:
    short = min(w, h)
    rng = np.random.default_rng(seed=seed)
    wob = {
        s: wobble_oracle(
            w // 2 + 4 if s in "tb" else h // 2 + 4,
            rng,
            0.35 * short,
            4,
            0.04 * short,
            use_libm=use_libm,
            perturb_ulp=perturb_ulp,
        )
        for s in "trbl"
    }
    gap = {
        s: np.clip(
            wobble_oracle(
                len(wob[s]),
                rng,
                0.2 * short,
                3,
                0.03 * short,
                use_libm=use_libm,
                perturb_ulp=perturb_ulp,
            )
            * 1.4
            + 1.2,
            0.0,
            3.2,
        )
        for s in "trbl"
    }
    outer: list[list[float]] = []
    inner: list[list[float]] = []
    for side, length in (("t", w), ("r", h), ("b", w), ("l", h)):
        for i in range(length // 2 + 1):
            index = min(i, len(wob[side]) - 1)
            d = c + 0.55 * jit * float(wob[side][index]) + 0.45 * jit
            g = float(gap[side][index])
            along = i * 2
            if side == "t":
                outer.append([float(along), -d])
                inner.append([float(along), -g])
            elif side == "r":
                outer.append([w + d, float(along)])
                inner.append([w + g, float(along)])
            elif side == "b":
                outer.append([float(w - along), h + d])
                inner.append([float(w - along), h + g])
            else:
                outer.append([-d, float(h - along)])
                inner.append([-g, float(h - along)])
    return outer, inner


def check_gate_a_arrays(or_arr: np.ndarray, rs_arr: np.ndarray, label: str, *, rel_tol: float = 1e-12) -> None:
    diff = np.abs(or_arr - rs_arr)
    limit = rel_tol * np.maximum(1.0, np.abs(or_arr))
    bad = int(np.sum(diff > limit))
    assert bad == 0, f"{label}: Gate A exceeded {rel_tol} on {bad} values (max_diff={float(np.max(diff)):.3e})"


def gate_b_carrier_report(
    py_outer: list[list[float]],
    py_inner: list[list[float]],
    rs_outer: list[list[float]],
    rs_inner: list[list[float]],
    py_pixels: np.ndarray,
    rs_pixels: np.ndarray,
    label: str,
) -> tuple[int, int]:
    """Compare real Python contours & canvas pixels against Rust, attributing any contour diffs."""
    pt_diffs: list[str] = []
    for tag, p_pts, r_pts in (("outer", py_outer, rs_outer), ("inner", py_inner, rs_inner)):
        assert len(p_pts) == len(r_pts)
        for i, (pp, rp) in enumerate(zip(p_pts, r_pts, strict=True)):
            if pp[0] != rp[0] or pp[1] != rp[1]:
                dx = abs(pp[0] - rp[0])
                dy = abs(pp[1] - rp[1])
                pt_diffs.append(f"{tag}[{i}] py=({pp[0]!r},{pp[1]!r}) rs=({rp[0]!r},{rp[1]!r}) delta=({dx:.3e},{dy:.3e})")
    px_diff = int(np.sum(py_pixels != rs_pixels))
    report_gate_b(f"{label}-contours", len(pt_diffs), chains=tuple(pt_diffs))
    report_gate_b(f"{label}-pixels", px_diff)
    assert px_diff == 0, f"{label}: expected 0 canvas pixel diffs, got {px_diff}"
    return len(pt_diffs), px_diff


def unwrap(v: Any) -> Any:
    return v
