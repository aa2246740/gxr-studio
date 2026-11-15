"""Gate A (Rust == sequential oracle exact) and Gate B (mechanical host BLAS/libm attribution) for color-bake."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

import numpy as np

from gxr_studio.camdata.unit_model import UnitModel
from gxr_studio.engine import knobs
from gxr_studio.engine import matrix_model as mm
from gxr_studio.gpu import tables
from gxr_studio.render import chain
from support.color_oracle import (
    OracleGridCache,
    _c_div,
    _c_exp,
    _c_mul,
    _elem_atan2,
    _elem_hypot,
    _kernel_weights_grid,
    libm_backend,
    oracle_bake_chroma,
)


def float64_rel_gap(a: np.ndarray, b: np.ndarray) -> str | None:
    """Return failure string if non-finite mismatch or `|a - b| > 1e-12 * max(1, |a|)`; else None."""
    af = np.asarray(a, np.float64).reshape(-1)
    bf = np.asarray(b, np.float64).reshape(-1)
    if af.shape != bf.shape:
        return f"shapes differ: {af.shape} vs {bf.shape}"
    na, nb = np.isnan(af), np.isnan(bf)
    if np.any(na != nb):
        i = int(np.flatnonzero(na != nb)[0])
        return f"NaN on one side at {i}: {af[i]!r} vs {bf[i]!r}"
    inf_bad = (np.isinf(af) | np.isinf(bf)) & (~na) & (af != bf)
    if np.any(inf_bad):
        i = int(np.flatnonzero(inf_bad)[0])
        return f"infinity differs at {i}: {af[i]!r} vs {bf[i]!r}"
    fin = np.isfinite(af) & np.isfinite(bf)
    diff = np.zeros_like(af)
    diff[fin] = np.abs(af[fin] - bf[fin])
    bad = np.flatnonzero(fin & (diff > 1e-12 * np.maximum(1.0, np.abs(af))))
    if not len(bad):
        return None
    i = int(bad[0])
    return f"{len(bad)} over 1e-12; first {i}: {af[i]!r} vs {bf[i]!r}"


def _cell_gains(
    model: UnitModel,
    entry: bytes,
    bk: chain.ChromaBake,
    nx: int,
    cell: int,
    *,
    use_libm: bool,
    use_seq_matmul: bool,
    use_seq_cplx: bool,
) -> tuple[complex, complex, complex, complex]:
    """Recompute gain at `cell` using Python batched/single + oracle(switches) + stored."""
    import math

    from scipy.spatial import ConvexHull

    ny = bk.gain.shape[0]
    cb = bk.origin[0] + bk.step * np.arange(nx)
    cr = bk.origin[1] + bk.step * np.arange(ny)
    c = (cb[None, :] + 1j * cr[:, None]).reshape(-1)
    probe = np.where(np.abs(c) < 1e-9, 1e-3 + 0j, c)
    lookup = mm.MatrixRow.from_bytes(model.factory[216:244])
    lm = lookup.effective(100.0)
    lo = lookup.offsets - np.array([0.0, 128.0, 128.0])
    corners = np.array([[r, g, b] for r in (0, 255) for g in (0, 255) for b in (0, 255)], np.float64) @ lm.T + lo
    hull = ConvexHull(corners[:, 1:])
    near = np.all(hull.equations[:, :2] @ np.column_stack([c.real, c.imag]).T + hull.equations[:, 2:3] <= 2.0 * bk.step * np.sqrt(2.0), axis=0)
    todo = np.flatnonzero(near)
    pos = int(np.searchsorted(todo, cell))
    assert pos < len(todo) and todo[pos] == cell, f"cell {cell} was not computed by the bake"
    start = (pos // 4096) * 4096
    sel = todo[start : start + 4096]
    z = chain._sector_z(model, entry)
    table = model.engine.table
    batched = complex((table.forward(probe[sel], z)[0] / probe[sel])[pos - start])
    single = complex(table.forward(probe[[cell]], z)[0][0] / probe[cell])
    stored = complex(float(bk.gain[cell // nx, cell % nx, 0]), float(bk.gain[cell // nx, cell % nx, 1]))

    probe_slice = probe[sel] if not use_seq_matmul else probe[[cell]]
    pr_s, pi_s = probe_slice.real, probe_slice.imag
    c_m = _elem_hypot(pr_s, pi_s, use_libm=use_libm)
    h_s = (_elem_atan2(pi_s, pr_s, use_libm=use_libm) * (180.0 / math.pi)) % 360.0
    w_s = _kernel_weights_grid(h_s, c_m, table, use_libm=use_libm, use_seq_matmul=use_seq_matmul, use_seq_cplx=use_seq_cplx)

    ab = np.array([[knobs.dec(v, 128) for v in entry[i : i + 4]] for i in range(115, 211, 4)], dtype=np.float64)
    d0, d1 = np.array(model.engine.ctx.d0, dtype=np.float64), np.array(model.engine.ctx.d1, dtype=np.float64)
    s_re, s_im = np.array(table.S.real, dtype=np.float64), np.array(table.S.imag, dtype=np.float64)
    zr0, zi0 = _c_div(ab[:, 0] + d0, -(ab[:, 1] + d1), np.full(24, 32.0), np.zeros(24), use_seq_cplx=use_seq_cplx)
    zr, zi = _c_div(zr0, zi0, s_re, s_im, use_seq_cplx=use_seq_cplx)
    arg_r, arg_i = _c_mul(
        np.zeros(24), np.full(24, float(table.k)), _elem_atan2(zi, zr, use_libm=use_libm), np.zeros(24), use_seq_cplx=use_seq_cplx
    )
    exp_r, exp_i = _c_exp(arg_r, arg_i, use_libm=use_libm, use_seq_cplx=use_seq_cplx)
    eff_re, eff_im = _c_mul(_elem_hypot(zr, zi, use_libm=use_libm), np.zeros(24), exp_r, exp_i, use_seq_cplx=use_seq_cplx)
    q_re, q_im = _c_mul(s_re, s_im, eff_re - 1.0, eff_im, use_seq_cplx=use_seq_cplx)
    if use_seq_matmul:
        s_of_re = w_s[:, 0] * s_re[0]
        s_of_im = w_s[:, 0] * s_im[0]
        for s_i in range(1, 24):
            s_of_re = s_of_re + w_s[:, s_i] * s_re[s_i]
            s_of_im = s_of_im + w_s[:, s_i] * s_im[s_i]
        gain_ps = np.array(table.gain, dtype=np.float64)
        wg0 = w_s[:, 0] * gain_ps[0]
        d_re = wg0 * q_re[0]
        d_im = wg0 * q_im[0]
        for s_i in range(1, 24):
            wgs = w_s[:, s_i] * gain_ps[s_i]
            d_re = d_re + wgs * q_re[s_i]
            d_im = d_im + wgs * q_im[s_i]
    else:
        s_of = w_s @ table.S
        s_of_re, s_of_im = s_of.real.copy(), s_of.imag.copy()
        d_c = (w_s * np.array(table.gain, dtype=np.float64)) @ (q_re + 1j * q_im)
        d_re, d_im = d_c.real.copy(), d_c.imag.copy()
    rr, ri = _c_div(d_re, d_im, s_of_re, s_of_im, use_seq_cplx=use_seq_cplx)
    cb_r, cb_i = _c_mul(pr_s, pi_s, s_of_re, s_of_im, use_seq_cplx=use_seq_cplx)
    fw_r, fw_i = _c_mul(cb_r, cb_i, 1.0 + rr, 0.0 + ri, use_seq_cplx=use_seq_cplx)
    gr, gi = _c_div(fw_r, fw_i, pr_s, pi_s, use_seq_cplx=use_seq_cplx)
    k = (pos - start) if not use_seq_matmul else 0
    return batched, single, stored, complex(float(gr[k]), float(gi[k]))


def _midpoint_side(v: float, shared_mid: float | None = None) -> tuple[str, float, float]:
    """Return ('below'|'above'|'on', float32_mid, signed_distance_in_f64_ulp)."""
    if shared_mid is None:
        f = np.float32(v)
        lo = f if float(f) <= v else np.nextafter(f, np.float32(-np.inf))
        hi = np.nextafter(lo, np.float32(np.inf))
        mid = (float(lo) + float(hi)) / 2.0
    else:
        mid = shared_mid
    side = "below" if v < mid else ("above" if v > mid else "on")
    return side, mid, (v - mid) / float(np.spacing(abs(v)))


def _unpack_rust_bake(res: tuple[Any, ...]) -> tuple[bytes, dict[str, Any], Any | None]:
    if len(res) == 2:
        return res[0], res[1], None
    return res[0], res[1], res[2]


@dataclass(frozen=True)
class CapturedBake:
    """Frozen single-run production bake output bound to its pre-float32 float64 gain witness."""

    bk: chain.ChromaBake
    data: bytes
    meta: dict[str, Any]
    witness: np.ndarray

    def __post_init__(self) -> None:
        if not isinstance(self.bk, chain.ChromaBake):
            raise TypeError(f"CapturedBake.bk must be ChromaBake, got {type(self.bk).__name__}")
        if not isinstance(self.data, bytes):
            raise TypeError(f"CapturedBake.data must be bytes, got {type(self.data).__name__}")
        if not isinstance(self.meta, dict):
            raise TypeError(f"CapturedBake.meta must be dict, got {type(self.meta).__name__}")
        if not isinstance(self.witness, np.ndarray) or self.witness.dtype != np.float64:
            got_type = type(self.witness).__name__
            got_dtype = getattr(self.witness, "dtype", None)
            raise TypeError(f"CapturedBake.witness must be float64 ndarray, got {got_type}({got_dtype!r})")
        w = np.array(self.witness, dtype=np.float64, copy=True)
        w.setflags(write=False)
        meta_copy = {k: (list(v) if isinstance(v, list) else v) for k, v in self.meta.items()}
        object.__setattr__(self, "witness", w)
        object.__setattr__(self, "meta", meta_copy)


_GRID_CACHE: dict[tuple[Any, ...], OracleGridCache] = {}
_CAPTURE_LOCK = threading.Lock()
_CAPTURE_OWNER = threading.local()


class _ReshapeCapture:
    def __init__(self, arr: np.ndarray, captured: list[np.ndarray]) -> None:
        self._arr = arr
        self._captured = captured

    def astype(self, dtype: Any, *args: Any, **kwargs: Any) -> np.ndarray:
        if np.dtype(dtype) == np.dtype(np.float32):
            if self._arr.dtype != np.dtype(np.float64):
                raise RuntimeError(f"expected float64 gain grid before float32 cast, got {self._arr.dtype}")
            self._captured.append(np.array(self._arr, dtype=np.float64, copy=True))
        return self._arr.astype(dtype, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._arr, name)


class _StackCapture:
    def __init__(self, arr: np.ndarray, captured: list[np.ndarray]) -> None:
        self._arr = arr
        self._captured = captured

    def reshape(self, *shape: Any, **kwargs: Any) -> _ReshapeCapture:
        return _ReshapeCapture(self._arr.reshape(*shape, **kwargs), self._captured)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._arr, name)


class _NumpyCaptureProxy:
    def __init__(self, orig_np: Any, captured: list[np.ndarray], owner_tid: int) -> None:
        self._orig_np = orig_np
        self._captured = captured
        self._owner_tid = owner_tid

    def stack(self, *args: Any, **kwargs: Any) -> Any:
        out = self._orig_np.stack(*args, **kwargs)
        if threading.get_ident() != self._owner_tid:
            return out
        return _StackCapture(out, self._captured)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._orig_np, name)


def capture_bake_chroma(
    model: UnitModel,
    entry: bytes,
    step: float = chain.CHROMA_STEP,
    *,
    workers: int = 1,
) -> CapturedBake:
    """Run production `chain.bake_chroma` once and return frozen `CapturedBake` (`bk`, `data`, `meta`, read-only f64 `witness`)."""
    if getattr(_CAPTURE_OWNER, "active", False):
        raise RuntimeError("nested capture_bake_chroma on the same thread is refused")
    captured: list[np.ndarray] = []
    owner_tid = threading.get_ident()
    with _CAPTURE_LOCK:
        orig_np = chain.np
        if isinstance(orig_np, _NumpyCaptureProxy):
            raise RuntimeError("chain.np already holds a capture proxy; refusing nested capture")
        _CAPTURE_OWNER.active = True
        chain.np = _NumpyCaptureProxy(orig_np, captured, owner_tid)
        try:
            bk = chain.bake_chroma(model, entry, step, workers=workers)
        finally:
            chain.np = orig_np
            _CAPTURE_OWNER.active = False
    if len(captured) != 1:
        raise RuntimeError(f"chain.bake_chroma gain witness hook fired {len(captured)} times, expected 1")
    witness = captured[0]
    if witness.dtype != np.float64 or witness.shape != bk.gain.shape or witness.ndim != 3 or witness.shape[2] != 2:
        raise RuntimeError(f"captured gain witness shape/dtype mismatch: {(witness.shape, witness.dtype)} vs {bk.gain.shape}")
    if not np.array_equal(witness.astype(np.float32), bk.gain, equal_nan=True):
        raise RuntimeError("captured float64 witness cast to float32 does not match ChromaBake.gain")
    data, meta = tables.bake_bytes(bk)
    return CapturedBake(bk=bk, data=data, meta=meta, witness=witness)


def _cache_key(model: UnitModel, step: float, backend: str) -> tuple[Any, ...]:
    import hashlib

    h = hashlib.sha256()
    t = model.engine.table
    h.update(model.factory[216:244])
    h.update(np.asarray(t.nodes, np.float64).tobytes())
    h.update(np.asarray(t.S, np.complex128).tobytes())
    h.update(np.asarray(t.gain, np.float64).tobytes())
    h.update(np.asarray(model.engine.ctx.d0, np.float64).tobytes())
    h.update(np.asarray(model.engine.ctx.d1, np.float64).tobytes())
    h.update(np.asarray(model.input_coefficients, np.float64).tobytes())
    return (h.hexdigest(), float(t.s0), float(t.kappa), str(t.kernel), float(t.k), float(step), backend)


def _cached_grid(model: UnitModel, step: float) -> OracleGridCache:
    from support.color_oracle import build_grid_cache

    key = _cache_key(model, step, libm_backend())
    cached = _GRID_CACHE.get(key)
    if cached is not None:
        return cached
    if len(_GRID_CACHE) >= 8:
        _GRID_CACHE.pop(next(iter(_GRID_CACHE)))
    seq_c = build_grid_cache(model, step, use_libm=True, use_seq_matmul=True, use_seq_cplx=True)
    _GRID_CACHE[key] = seq_c
    return seq_c


def _validate_witness_and_meta(
    data: bytes,
    meta: dict[str, Any],
    bk: chain.ChromaBake | None,
    real_gain_f64: np.ndarray,
    r_data: bytes,
    r_meta: dict[str, Any],
    summary: Any,
    model: UnitModel,
) -> str | None:
    if isinstance(summary, dict) and "bytes" in summary and summary["bytes"] != len(r_data):
        return f"summary byte count: {summary['bytes']} != {len(r_data)}"
    for key in ("nx", "ny", "origin", "step"):
        if meta[key] != r_meta[key]:
            return f"meta {key}: python {meta[key]!r} rust {r_meta[key]!r}"
    coef = np.asarray(r_meta["input_coefficients"], np.float64)
    if not np.array_equal(coef, np.asarray(model.input_coefficients, np.float64)):
        return "input_coefficients differ"

    nx, ny = int(meta["nx"]), int(meta["ny"])
    if real_gain_f64.shape != (ny, nx, 2):
        return f"witness shape {real_gain_f64.shape} != expected {(ny, nx, 2)}"
    head = tables.PRE_FLOATS + 9 + 3 + 30 + 12
    expected_bytes = (head + ny * nx * 2) * 4
    if len(data) != expected_bytes:
        return f"py byte length {len(data)} != expected {expected_bytes}"
    if bk is not None:
        if bk.gain.shape != (ny, nx, 2):
            return f"ChromaBake.gain shape {bk.gain.shape} != meta {(ny, nx, 2)}"
        if [float(bk.origin[0]), float(bk.origin[1])] != [float(meta["origin"][0]), float(meta["origin"][1])]:
            return f"ChromaBake.origin {bk.origin!r} != meta {meta['origin']!r}"
        if float(bk.step) != float(meta["step"]):
            return f"ChromaBake.step {bk.step!r} != meta {meta['step']!r}"
        if np.ascontiguousarray(bk.gain, "<f4").tobytes() != data[head * 4 :]:
            return "ChromaBake.gain bytes differ from py bake bytes"
        # Sol r4: every serialized field (pre, matrix, offset, input_coefficients, lookup_*) must still be
        # the captured bake; an in-place edit through a NumPy alias would otherwise pass unnoticed.
        cur_data, cur_meta = tables.bake_bytes(bk)
        if cur_data != data or cur_meta != meta:
            return "ChromaBake bytes/meta differ from the captured snapshot (bk changed after capture)"

    witness_f32_bytes = np.ascontiguousarray(real_gain_f64.astype(np.float32), "<f4").tobytes()
    if witness_f32_bytes != data[head * 4 :]:
        w_f32 = np.frombuffer(witness_f32_bytes, "<f4")
        p_gain_f32 = np.frombuffer(data[head * 4 :], "<f4")
        mismatch = np.flatnonzero(w_f32.view(np.uint32) != p_gain_f32.view(np.uint32))
        i = int(mismatch[0])
        return f"witness.astype(float32) differs from py at gain float {i}: witness {w_f32[i]!r} vs py {p_gain_f32[i]!r}"
    return None


def _attribute_gate_b(
    diffs: np.ndarray,
    py_f32: np.ndarray,
    rs_f32: np.ndarray,
    model: UnitModel,
    entry: bytes,
    bk: chain.ChromaBake | None,
    or_bk: chain.ChromaBake,
    nx: int,
    head: int,
    backend: str,
) -> tuple[str | None, list[str]]:
    allowed: list[str] = []
    use_bk = bk if bk is not None else or_bk
    for idx in diffs:
        i = int(idx)
        text = f"float {i}: python {py_f32[i]!r} rust {rs_f32[i]!r}"
        if i < head:
            return text + " (outside the gain grid)", allowed
        cell, part = divmod(i - head, 2)
        pick = (lambda c: c.imag) if part else (lambda c: c.real)
        batched, single, stored, or_c = _cell_gains(model, entry, use_bk, nx, cell, use_libm=True, use_seq_matmul=True, use_seq_cplx=True)
        _b2, _s2, _st2, host_ctrl = _cell_gains(model, entry, use_bk, nx, cell, use_libm=False, use_seq_matmul=False, use_seq_cplx=False)
        or_v, py_v, single_v, host_v = pick(or_c), pick(batched), pick(single), pick(host_ctrl)
        if np.float32(or_v) != rs_f32[i]:
            return text + f"; cell {cell} Gate A consistency mismatch: np.float32({or_v!r}) != {rs_f32[i]!r}", allowed
        if bk is not None and np.float32(py_v) != np.float32(pick(stored)):
            return text + f"; cell {cell} recomputed Python batch ({py_v!r}) != stored ({pick(stored)!r})", allowed
        rel_gap = abs(or_v - py_v)
        rel_lim = 1e-12 * max(1.0, abs(py_v))
        if rel_gap > rel_lim:
            return text + f"; cell {cell} |or_f64 - py_f64| {rel_gap:.3e} > 1e-12 rel {rel_lim:.3e}", allowed
        if np.float32(host_v) != py_f32[i]:
            return (
                text + f"; cell {cell} negative control failed: host_f64 {host_v!r} -> {np.float32(host_v)!r} != py {py_f32[i]!r}",
                allowed,
            )
        neighbour = rs_f32[i] in (np.nextafter(py_f32[i], np.float32(np.inf)), np.nextafter(py_f32[i], np.float32(-np.inf)))
        if not neighbour and abs(py_v) >= 1e-4:
            return text + f"; cell {cell} Gate B diff exceeds 1 float32 ULP (or_f64={or_v!r}, py_f64={py_v!r})", allowed
        shared_mid = (float(py_f32[i]) + float(rs_f32[i])) / 2.0 if neighbour else None
        or_side, mid, or_ulp = _midpoint_side(or_v, shared_mid)
        py_side, _mid, py_ulp = _midpoint_side(py_v, shared_mid)
        span_note = "" if neighbour else f" [near-zero gap {rel_gap:.3e} <= 1e-12]"
        allowed.append(
            f"{text}; cell {cell} {'imag' if part else 'real'}: "
            f"or_f64={or_v!r} ({or_side} mid {mid!r} by {or_ulp:+.1f} ulp), "
            f"py_f64 batched={py_v!r} single={single_v!r} ({py_side} mid by {py_ulp:+.1f} ulp), "
            f"neg_ctrl_host={host_v!r} -> {np.float32(host_v)!r} (Gate A exact via {backend}){span_note}"
        )
    return None, allowed


def bake_gate(
    py: CapturedBake,
    rust: tuple[Any, ...],
    model: UnitModel,
    entry: bytes,
    *,
    grid_cache: OracleGridCache | None = None,
    host_cache: OracleGridCache | None = None,
) -> tuple[str | None, list[str]]:
    """Gate A (Rust == sequential oracle exact, zero masks) and Gate B (mechanical host attribution vs Python)."""
    _ = host_cache  # kept for caller signature compatibility; host check runs only on Gate B float32 diffs
    if not isinstance(py, CapturedBake):
        return f"py must be CapturedBake (from capture_bake_chroma), got {type(py).__name__}", []
    data, meta, bk, real_gain_f64 = py.data, py.meta, py.bk, py.witness
    r_data, r_meta, summary = _unpack_rust_bake(rust)
    val_err = _validate_witness_and_meta(data, meta, bk, real_gain_f64, r_data, r_meta, summary, model)
    if val_err is not None:
        return val_err, []

    step = float(meta["step"])
    if grid_cache is None:
        grid_cache = _cached_grid(model, step)
    or_data, or_meta, or_bk, or_gain_f64 = oracle_bake_chroma(model, entry, step, grid_cache=grid_cache)
    for key in ("nx", "ny", "origin", "step"):
        if or_meta[key] != r_meta[key]:
            return f"Gate A oracle meta {key}: oracle {or_meta[key]!r} rust {r_meta[key]!r}", []

    rel_err = float64_rel_gap(real_gain_f64, or_gain_f64)
    if rel_err is not None:
        return f"Real Python vs Oracle float64 > 1e-12 rel: {rel_err}", []

    or_f32 = np.frombuffer(or_data, "<f4")
    rs_f32 = np.frombuffer(r_data, "<f4")
    py_f32 = np.frombuffer(data, "<f4")
    if or_f32.shape != rs_f32.shape or py_f32.shape != rs_f32.shape:
        return f"table sizes differ: py={py_f32.size} or={or_f32.size} rs={rs_f32.size}", []

    # Gate A: Rust vs sequential oracle exact (zero masks).
    gate_a_bad = np.flatnonzero(or_f32.view(np.uint32) != rs_f32.view(np.uint32))
    head = tables.PRE_FLOATS + 9 + 3 + 30 + 12
    backend = libm_backend()
    if len(gate_a_bad):
        i = int(gate_a_bad[0])
        where = "header" if i < head else f"cell {(i - head) // 2} {'imag' if (i - head) % 2 else 'real'}"
        detail = ""
        if i >= head:
            cell, part = divmod(i - head, 2)
            v_or = float(or_gain_f64[cell // meta["nx"], cell % meta["nx"], part])
            detail = f"; or_f64={v_or!r}"
        return (
            f"Gate A failed ({len(gate_a_bad)} float32 diffs, backend={backend}); "
            f"first float {i} ({where}): oracle {or_f32[i]!r} rust {rs_f32[i]!r}{detail}"
        ), []

    # Gate B: any Python != Rust difference must be mechanically attributed where Gate A holds.
    diffs = np.flatnonzero(py_f32.view(np.uint32) != rs_f32.view(np.uint32))
    if not len(diffs):
        return None, []
    return _attribute_gate_b(diffs, py_f32, rs_f32, model, entry, bk, or_bk, int(meta["nx"]), head, backend)
