"""BLAS/LAPACK/FMA-free sequential Python oracle for `chain.bake_chroma` and `tables.bake_bytes`."""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Any

import numpy as np

from gxr_studio.camdata.unit_model import UnitModel
from gxr_studio.engine import knobs
from gxr_studio.engine import matrix_model as mm
from gxr_studio.gpu import tables
from gxr_studio.render import chain
from support.color_oracle_libm import (  # noqa: F401
    _elem_atan2,
    _elem_cos,
    _elem_exp,
    _elem_hypot,
    _elem_sin,
    _ndtr_vec,
    _ramp_vec,
    color_libm_available,
    libm_backend,
)
from support.lut_oracle import interp_1d_seq


def _c_mul(
    ar: np.ndarray,
    ai: np.ndarray,
    br: np.ndarray,
    bi: np.ndarray,
    *,
    use_seq_cplx: bool,
) -> tuple[np.ndarray, np.ndarray]:
    if not use_seq_cplx:
        c = (ar + 1j * ai) * (br + 1j * bi)
        return c.real.copy(), c.imag.copy()
    return ar * br - ai * bi, ar * bi + ai * br


def _c_div(
    in1r: np.ndarray,
    in1i: np.ndarray,
    in2r: np.ndarray,
    in2i: np.ndarray,
    *,
    use_seq_cplx: bool,
) -> tuple[np.ndarray, np.ndarray]:
    if not use_seq_cplx:
        with np.errstate(divide="ignore", invalid="ignore"):
            c = (in1r + 1j * in1i) / (in2r + 1j * in2i)
        return c.real.copy(), c.imag.copy()
    in2r_abs = np.abs(in2r)
    in2i_abs = np.abs(in2i)
    ge = in2r_abs >= in2i_abs
    re = np.empty(in1r.shape, dtype=np.float64)
    im = np.empty(in1r.shape, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        zero = ge & (in2r_abs == 0.0) & (in2i_abs == 0.0)
        if np.any(zero):
            re[zero] = in1r[zero] / in2r_abs[zero]
            im[zero] = in1i[zero] / in2r_abs[zero]
        g1 = ge & (~zero)
        if np.any(g1):
            rat = in2i[g1] / in2r[g1]
            scl = 1.0 / (in2r[g1] + in2i[g1] * rat)
            re[g1] = (in1r[g1] + in1i[g1] * rat) * scl
            im[g1] = (in1i[g1] - in1r[g1] * rat) * scl
        g2 = ~ge
        if np.any(g2):
            rat = in2r[g2] / in2i[g2]
            scl = 1.0 / (in2i[g2] + in2r[g2] * rat)
            re[g2] = (in1r[g2] * rat + in1i[g2]) * scl
            im[g2] = (in1i[g2] * rat - in1r[g2]) * scl
    return re, im


def _c_exp(re: np.ndarray, im: np.ndarray, *, use_libm: bool, use_seq_cplx: bool) -> tuple[np.ndarray, np.ndarray]:
    if not use_seq_cplx and not use_libm:
        c = np.exp(re + 1j * im)
        return c.real.copy(), c.imag.copy()
    e = _elem_exp(re, use_libm=use_libm)
    return np.where(im == 0.0, e, e * _elem_cos(im, use_libm=use_libm)), np.where(im == 0.0, im, e * _elem_sin(im, use_libm=use_libm))


def _hull_equations_seq(pts_in: list[tuple[float, float]]) -> list[tuple[float, float, float]]:
    pts = sorted(set(pts_in))

    def cross(o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0.0:
            lower.pop()
        lower.append(p)
    upper: list[tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0.0:
            upper.pop()
        upper.append(p)
    ring = lower[:-1] + upper[:-1]
    out = []
    for i in range(len(ring)):
        p0 = ring[i]
        p1 = ring[(i + 1) % len(ring)]
        n0 = p1[1] - p0[1]
        n1 = p0[0] - p1[0]
        norm = math.sqrt(n0 * n0 + n1 * n1)
        u0, u1 = n0 / norm, n1 / norm
        offset = -(p0[0] * u0 + p0[1] * u1)
        out.append((u0, u1, offset))
    return out


def _sum24(w: np.ndarray, *, use_seq_matmul: bool) -> np.ndarray:
    if not use_seq_matmul:
        return np.sum(w, axis=1)
    r = w[:, :8].copy()
    r += w[:, 8:16]
    r += w[:, 16:24]
    return ((r[:, 0] + r[:, 1]) + (r[:, 2] + r[:, 3])) + ((r[:, 4] + r[:, 5]) + (r[:, 6] + r[:, 7]))


@dataclass(frozen=True)
class OracleGridCache:
    """Reusable per-(model, step, switches) precomputed hull/weights cache."""

    step: float
    nx: int
    ny: int
    origin: tuple[float, float]
    lookup_matrix: np.ndarray
    lookup_offset: np.ndarray
    todo: np.ndarray
    pr: np.ndarray
    pi: np.ndarray
    w: np.ndarray
    s_of_re: np.ndarray
    s_of_im: np.ndarray
    s_re: np.ndarray
    s_im: np.ndarray
    gain_ps: np.ndarray
    k_coup: float
    d0: np.ndarray
    d1: np.ndarray
    coef: np.ndarray
    use_libm: bool
    use_seq_matmul: bool
    use_seq_cplx: bool


def _kernel_weights_grid(
    h: np.ndarray,
    cm: np.ndarray,
    table: Any,
    *,
    use_libm: bool,
    use_seq_matmul: bool,
    use_seq_cplx: bool,
) -> np.ndarray:
    nodes = np.array(table.nodes, dtype=np.float64)
    s0, kappa, kernel = float(table.s0), float(table.kappa), str(table.kernel)
    a_n = np.array([nodes[23] - 360.0 if s == 0 else nodes[s - 1] for s in range(24)], dtype=np.float64)
    c_n = np.array([nodes[0] + 360.0 if s == 23 else nodes[s + 1] for s in range(24)], dtype=np.float64)
    om = np.array([2.0 * math.pi * (m_i + 1) / 360.0 for m_i in range(32)], dtype=np.float64)
    om_grid = om[:, None]

    def e_mat(x_24: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        a1r, a1i = _c_mul(
            np.full((32, 1), -0.0),
            np.full((32, 1), -1.0),
            om_grid,
            np.zeros((32, 1)),
            use_seq_cplx=use_seq_cplx,
        )
        a2r, a2i = _c_mul(a1r, a1i, x_24[None, :], np.zeros((1, 24)), use_seq_cplx=use_seq_cplx)
        return _c_exp(a2r, a2i, use_libm=use_libm, use_seq_cplx=use_seq_cplx)

    if kernel == "linear":
        na = (nodes - a_n)[None, :]
        cn = (c_n - nodes)[None, :]
        ear, eai = e_mat(a_n)
        enr, eni = e_mat(nodes)
        ecr, eci = e_mat(c_n)
        fr, fi = _c_div(ear, eai, np.broadcast_to(na, ear.shape), np.zeros(ear.shape), use_seq_cplx=use_seq_cplx)
        sr, si = _c_mul(enr, eni, np.broadcast_to(1.0 / na + 1.0 / cn, enr.shape), np.zeros(enr.shape), use_seq_cplx=use_seq_cplx)
        tr, ti = _c_div(ecr, eci, np.broadcast_to(cn, ecr.shape), np.zeros(ecr.shape), use_seq_cplx=use_seq_cplx)
        numr = -((fr - sr) + tr)
        numi = -((fi - si) + ti)
        denr = np.broadcast_to(360.0 * (om_grid * om_grid), numr.shape)
        coef_re, coef_im = _c_div(numr, numi, denr, np.zeros(numr.shape), use_seq_cplx=use_seq_cplx)
        dc = (c_n - a_n) / 720.0
    else:
        enr, eni = e_mat(nodes)
        ecr, eci = e_mat(c_n)
        dr, di = _c_mul(np.zeros((32, 1)), np.full((32, 1), 360.0), om_grid, np.zeros((32, 1)), use_seq_cplx=use_seq_cplx)
        coef_re, coef_im = _c_div(
            enr - ecr,
            eni - eci,
            np.broadcast_to(dr, enr.shape),
            np.broadcast_to(di, enr.shape),
            use_seq_cplx=use_seq_cplx,
        )
        dc = (c_n - nodes) / 360.0

    tv = kappa / np.maximum(cm, 1e-12)
    sig = np.sqrt(s0 * s0 + tv * tv)
    w = np.empty((len(h), 24), dtype=np.float64)
    le30 = sig <= 30.0
    if np.any(le30):
        h_s = h[le30][:, None]
        sig_s = np.maximum(sig[le30], 1e-4)[:, None]
        folded = ((h_s - nodes[0]) % 360.0) + nodes[0]
        acc = np.zeros((len(h_s), 24), dtype=np.float64)
        ba = (nodes - a_n)[None, :]
        cb_d = (c_n - nodes)[None, :]
        inv_sum = 1.0 / ba + 1.0 / cb_d
        for shift in (-360.0, 0.0, 360.0):
            x = folded + shift
            if kernel == "linear":
                r0 = _ramp_vec(x - a_n[None, :], sig_s, use_libm=use_libm)
                r1 = _ramp_vec(x - nodes[None, :], sig_s, use_libm=use_libm)
                r2 = _ramp_vec(x - c_n[None, :], sig_s, use_libm=use_libm)
                term = (r0 / ba - r1 * inv_sum) + r2 / cb_d
            else:
                term = _ndtr_vec((x - nodes[None, :]) / sig_s, use_libm=use_libm) - _ndtr_vec((x - c_n[None, :]) / sig_s, use_libm=use_libm)
            acc = acc + term
        w[le30] = np.maximum(acc, 0.0)
    gt30 = ~le30
    if np.any(gt30):
        h_f = h[gt30][:, None]
        sig_f = sig[gt30][:, None]
        om_f = om[None, :]
        a1r, a1i = _c_mul(np.zeros(h_f.shape), np.ones(h_f.shape), h_f, np.zeros(h_f.shape), use_seq_cplx=use_seq_cplx)
        a2r, a2i = _c_mul(a1r, a1i, om_f, np.zeros(om_f.shape), use_seq_cplx=use_seq_cplx)
        rot_r, rot_i = _c_exp(a2r, a2i, use_libm=use_libm, use_seq_cplx=use_seq_cplx)
        so = sig_f * om_f
        damp = _elem_exp(-0.5 * (so * so), use_libm=use_libm)
        ph_r, ph_i = _c_mul(rot_r, rot_i, damp, np.zeros(damp.shape), use_seq_cplx=use_seq_cplx)
        if use_seq_matmul:
            rr = ph_r[:, 0:1] * coef_re[0:1, :]
            ii = ph_i[:, 0:1] * coef_im[0:1, :]
            for m_i in range(1, 32):
                rr = rr + ph_r[:, m_i : m_i + 1] * coef_re[m_i : m_i + 1, :]
                ii = ii + ph_i[:, m_i : m_i + 1] * coef_im[m_i : m_i + 1, :]
            real_dot = rr - ii
        else:
            real_dot = ((ph_r + 1j * ph_i) @ (coef_re + 1j * coef_im)).real
        w[gt30] = np.maximum(dc[None, :] + 2.0 * real_dot, 0.0)

    total = np.maximum(0.0 + _sum24(w, use_seq_matmul=use_seq_matmul), 1e-12)[:, None]
    return w / total


def build_grid_cache(
    model: UnitModel,
    step: float = chain.CHROMA_STEP,
    *,
    use_libm: bool = True,
    use_seq_matmul: bool = True,
    use_seq_cplx: bool = True,
) -> OracleGridCache:
    """Precompute grid geometry, hull mask, weights and S_of for `model` at `step`."""
    lookup = mm.MatrixRow.from_bytes(model.factory[216:244])
    lm = lookup.effective(100.0)
    loff = lookup.offsets - np.array([0.0, 128.0, 128.0])
    raw_corners = np.array([[r, g, b] for r in (0.0, 255.0) for g in (0.0, 255.0) for b in (0.0, 255.0)], dtype=np.float64)
    if use_seq_matmul:
        corners_cbcr: list[tuple[float, float]] = []
        for r, g, b in raw_corners:
            cb = ((float(r) * float(lm[1, 0]) + float(g) * float(lm[1, 1])) + float(b) * float(lm[1, 2])) + float(loff[1])
            cr = ((float(r) * float(lm[2, 0]) + float(g) * float(lm[2, 1])) + float(b) * float(lm[2, 2])) + float(loff[2])
            corners_cbcr.append((cb, cr))
        mn0, mx0 = min(c[0] for c in corners_cbcr), max(c[0] for c in corners_cbcr)
        mn1, mx1 = min(c[1] for c in corners_cbcr), max(c[1] for c in corners_cbcr)
        lo0 = math.floor(mn0 / step) * step - step
        lo1 = math.floor(mn1 / step) * step - step
        hi0 = math.ceil(mx0 / step) * step + step
        hi1 = math.ceil(mx1 / step) * step + step
    else:
        from scipy.spatial import ConvexHull

        proj = raw_corners @ lm.T + loff
        lo_a = np.floor(proj[:, 1:].min(axis=0) / step) * step - step
        hi_a = np.ceil(proj[:, 1:].max(axis=0) / step) * step + step
        lo0, lo1 = float(lo_a[0]), float(lo_a[1])
        hi0, hi1 = float(hi_a[0]), float(hi_a[1])
        hull_eq = ConvexHull(proj[:, 1:]).equations

    nx = int(round((hi0 - lo0) / step)) + 1
    ny = int(round((hi1 - lo1) / step)) + 1
    cb_vec = lo0 + step * np.arange(nx, dtype=np.float64)
    cr_vec = lo1 + step * np.arange(ny, dtype=np.float64)
    c_re = np.broadcast_to(cb_vec[None, :], (ny, nx)).reshape(-1)
    c_im = np.broadcast_to(cr_vec[:, None], (ny, nx)).reshape(-1)

    thr = 2.0 * step * math.sqrt(2.0)
    if use_seq_matmul:
        near = np.ones(nx * ny, dtype=bool)
        for e0, e1, e2 in _hull_equations_seq(corners_cbcr):
            near &= (e0 * c_re + e1 * c_im) + e2 <= thr
    else:
        near = np.all(hull_eq[:, :2] @ np.column_stack([c_re, c_im]).T + hull_eq[:, 2:3] <= thr, axis=0)
    todo = np.flatnonzero(near)

    cr_sel, ci_sel = c_re[todo], c_im[todo]
    c_mag = _elem_hypot(cr_sel, ci_sel, use_libm=use_libm)
    small = c_mag < 1e-9
    pr = np.where(small, 1e-3, cr_sel)
    pi = np.where(small, 0.0, ci_sel)
    cm = _elem_hypot(pr, pi, use_libm=use_libm)
    h = (_elem_atan2(pi, pr, use_libm=use_libm) * (180.0 / math.pi)) % 360.0

    table = model.engine.table
    w = _kernel_weights_grid(h, cm, table, use_libm=use_libm, use_seq_matmul=use_seq_matmul, use_seq_cplx=use_seq_cplx)
    s_re = np.array(table.S.real, dtype=np.float64)
    s_im = np.array(table.S.imag, dtype=np.float64)
    if use_seq_matmul:
        s_of_re = w[:, 0] * s_re[0]
        s_of_im = w[:, 0] * s_im[0]
        for s_i in range(1, 24):
            s_of_re = s_of_re + w[:, s_i] * s_re[s_i]
            s_of_im = s_of_im + w[:, s_i] * s_im[s_i]
    else:
        s_of = w @ table.S
        s_of_re, s_of_im = s_of.real.copy(), s_of.imag.copy()

    return OracleGridCache(
        step=float(step),
        nx=nx,
        ny=ny,
        origin=(float(lo0), float(lo1)),
        lookup_matrix=lm,
        lookup_offset=loff,
        todo=todo,
        pr=pr,
        pi=pi,
        w=w,
        s_of_re=s_of_re,
        s_of_im=s_of_im,
        s_re=s_re,
        s_im=s_im,
        gain_ps=np.array(table.gain, dtype=np.float64),
        k_coup=float(table.k),
        d0=np.array(model.engine.ctx.d0, dtype=np.float64),
        d1=np.array(model.engine.ctx.d1, dtype=np.float64),
        coef=model.input_coefficients,
        use_libm=use_libm,
        use_seq_matmul=use_seq_matmul,
        use_seq_cplx=use_seq_cplx,
    )


def _entry_gain_grid(
    cache: OracleGridCache,
    entry: bytes,
    *,
    use_libm: bool,
    use_seq_matmul: bool,
    use_seq_cplx: bool,
) -> tuple[np.ndarray, np.ndarray]:
    ny, nx = cache.ny, cache.nx
    gain_re = np.ones(nx * ny, dtype=np.float64)
    gain_im = np.zeros(nx * ny, dtype=np.float64)
    if entry[0] == 3:
        return gain_re, gain_im
    ab = np.array([[knobs.dec(z, 128) for z in entry[i : i + 4]] for i in range(115, 211, 4)], dtype=np.float64)
    if np.any(ab[:, 0] != ab[:, 3]) or np.any(ab[:, 1] != -ab[:, 2]):
        raise ValueError("frozen symmetric sector model cannot predict asymmetric rows")
    zr0, zi0 = _c_div(ab[:, 0] + cache.d0, -(ab[:, 1] + cache.d1), np.full(24, 32.0), np.zeros(24), use_seq_cplx=use_seq_cplx)
    zr, zi = _c_div(zr0, zi0, cache.s_re, cache.s_im, use_seq_cplx=use_seq_cplx)
    mag_z = _elem_hypot(zr, zi, use_libm=use_libm)
    ang_z = _elem_atan2(zi, zr, use_libm=use_libm)
    arg_re, arg_im = _c_mul(np.zeros(24), np.full(24, cache.k_coup), ang_z, np.zeros(24), use_seq_cplx=use_seq_cplx)
    exp_re, exp_im = _c_exp(arg_re, arg_im, use_libm=use_libm, use_seq_cplx=use_seq_cplx)
    eff_re, eff_im = _c_mul(mag_z, np.zeros(24), exp_re, exp_im, use_seq_cplx=use_seq_cplx)
    q_re, q_im = _c_mul(cache.s_re, cache.s_im, eff_re - 1.0, eff_im, use_seq_cplx=use_seq_cplx)

    w = cache.w
    gain_ps = cache.gain_ps
    if use_seq_matmul:
        wg0 = w[:, 0] * gain_ps[0]
        d_re = wg0 * q_re[0]
        d_im = wg0 * q_im[0]
        for s_i in range(1, 24):
            wgs = w[:, s_i] * gain_ps[s_i]
            d_re = d_re + wgs * q_re[s_i]
            d_im = d_im + wgs * q_im[s_i]
    else:
        d_c = (w * gain_ps) @ (q_re + 1j * q_im)
        d_re, d_im = d_c.real.copy(), d_c.imag.copy()

    ratio_re, ratio_im = _c_div(d_re, d_im, cache.s_of_re, cache.s_of_im, use_seq_cplx=use_seq_cplx)
    cb_r, cb_i = _c_mul(cache.pr, cache.pi, cache.s_of_re, cache.s_of_im, use_seq_cplx=use_seq_cplx)
    fw_r, fw_i = _c_mul(cb_r, cb_i, 1.0 + ratio_re, 0.0 + ratio_im, use_seq_cplx=use_seq_cplx)
    gr, gi = _c_div(fw_r, fw_i, cache.pr, cache.pi, use_seq_cplx=use_seq_cplx)
    gain_re[cache.todo] = gr
    gain_im[cache.todo] = gi
    return gain_re, gain_im


def pre_curves_f64(entry: bytes, *, use_seq_interp: bool = True) -> np.ndarray:
    """Evaluate the 3x4096 pre-table in float64 via sequential interp or host `np.interp`."""
    xs = np.array([0.0, *struct.unpack_from(">31H", entry, 20), 4095.0], dtype=np.float64)
    active = bool(entry[1] & 4)
    curves = (entry[379:412], entry[82:115], entry[412:445]) if active else (entry[82:115],) * 3
    ramp = np.arange(4096, dtype=np.float64)
    interp_fn = interp_1d_seq if use_seq_interp else np.interp
    return np.column_stack([interp_fn(ramp, xs, np.array(list(curves[j]), dtype=np.float64)) for j in range(3)])


def oracle_bake_chroma(
    model: UnitModel,
    entry: bytes,
    step: float = chain.CHROMA_STEP,
    *,
    grid_cache: OracleGridCache | None = None,
    use_libm: bool = True,
    use_seq_matmul: bool = True,
    use_seq_cplx: bool = True,
    use_seq_interp: bool = True,
) -> tuple[bytes, dict[str, Any], chain.ChromaBake, np.ndarray]:
    """Sequential oracle for `chain.bake_chroma` + `tables.bake_bytes`. Returns `(bytes, meta, ChromaBake, gain_f64)`."""
    if (
        grid_cache is None
        or grid_cache.step != float(step)
        or grid_cache.use_libm != use_libm
        or grid_cache.use_seq_matmul != use_seq_matmul
        or grid_cache.use_seq_cplx != use_seq_cplx
    ):
        cache = build_grid_cache(model, step, use_libm=use_libm, use_seq_matmul=use_seq_matmul, use_seq_cplx=use_seq_cplx)
    else:
        cache = grid_cache

    pre = pre_curves_f64(entry, use_seq_interp=use_seq_interp).astype(np.float32)

    row = mm.MatrixRow.from_bytes(entry[216:244])
    m = row.effective(100.0)
    off = row.offsets - np.array([0.0, 128.0, 128.0])
    gain_re, gain_im = _entry_gain_grid(cache, entry, use_libm=use_libm, use_seq_matmul=use_seq_matmul, use_seq_cplx=use_seq_cplx)
    gain_f64 = np.stack([gain_re, gain_im], axis=-1).reshape(cache.ny, cache.nx, 2)
    bk = chain.ChromaBake(
        pre=pre,
        matrix=m.astype(np.float32),
        offset=off.astype(np.float32),
        gain=gain_f64.astype(np.float32),
        origin=cache.origin,
        step=cache.step,
        input_coefficients=cache.coef,
        lookup_matrix=cache.lookup_matrix.astype(np.float32),
        lookup_offset=cache.lookup_offset.astype(np.float32),
    )
    data, meta = tables.bake_bytes(bk)
    return data, meta, bk, gain_f64
