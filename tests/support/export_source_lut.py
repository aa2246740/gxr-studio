"""LUT gates for the export source's full-size LUT pictures (PORT-WAVE-1 Gate A / Gate B).

- Display input: every distinct input colour of the base picture goes through the sequential
  oracle (`SeqTarget`) and through `gxr-cli lut-apply` (Rust float64). Gate A: Rust uint8 equals
  the oracle except at proven `k + 0.5` boundary points. Gate B: every pixel where Rust and the
  real Python differ must be a Gate-A-clean colour, by exactly one code.
- Log input: the scene XYZ of sampled pixels comes from `gxr-cli scene-xyz` (checked against
  Python within 1e-12) and feeds both the oracle and `lut-apply --xyz`; Gate A on the sample and
  Gate B at every differing pixel.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from gxr_studio.lut.cube import Cube
from gxr_studio.lut.scene import scene_colour
from gxr_studio.raw import open_dng
from gxr_studio.raw.demosaic import demosaic_full
from gxr_studio.raw.render import _neutral_display
from support.core_cli import run_json, run_npy
from support.lut_cases import assert_rel
from support.lut_gates import _assert_gate_a_elem, assert_u8_boundary_gate
from support.lut_oracle import SeqTarget
from support.raw_render_parity import _unoriented_yx


def _q(v: np.ndarray) -> np.ndarray:
    return np.floor(np.clip(v, 0, 255) + 0.5).astype(np.uint8)


def _rust_f64(tmp: Path, cube: Path, spec: dict[str, Any], rgb: np.ndarray, xyz: np.ndarray | None = None) -> np.ndarray:
    rgb_npy = tmp / "gate-rgb.npy"
    np.save(rgb_npy, rgb.astype(np.uint8))
    args = ["lut-apply", "--cube", str(cube), "--space", json.dumps(spec), "--rgb", str(rgb_npy)]
    if xyz is not None:
        xyz_npy = tmp / "gate-xyz.npy"
        np.save(xyz_npy, xyz)
        args += ["--xyz", str(xyz_npy)]
    return run_npy(args, tmp / "gate-f64.npy", timeout=600)


def _gate_b(py: np.ndarray, rs: np.ndarray, at: np.ndarray, or_u8: np.ndarray, or_f64: np.ndarray, rs_f64: np.ndarray, label: str) -> int:
    """`py`, `rs`: per-pixel uint8 `(n, 3)`; `at[i]` is the gate row of pixel i."""
    diffs = np.argwhere(py != rs)
    for p, c in diffs:
        u = int(at[p])
        assert abs(int(py[p, c]) - int(rs[p, c])) == 1, (label, int(p), int(c), int(py[p, c]), int(rs[p, c]))
        _assert_gate_a_elem(float(or_f64[u, c]), float(rs_f64[u, c]), int(or_u8[u, c]), int(rs[p, c]), label=f"{label}-gate-b", idx=(u, int(c)))
    print(f"[gate-b-count] {label}: allowed_points={len(diffs)}")
    return int(len(diffs))


def display_gate(tmp: Path, cube: Path, spec: dict[str, Any], base: np.ndarray, py: np.ndarray, rs: np.ndarray, label: str) -> tuple[int, int]:
    flat = base.reshape(-1, 3)
    uniq, first, inv = np.unique(flat, axis=0, return_index=True, return_inverse=True)
    inv = inv.reshape(-1)
    rs_flat, py_flat = rs.reshape(-1, 3), py.reshape(-1, 3)
    rs_uniq = rs_flat[first]
    assert np.array_equal(rs_flat, rs_uniq[inv]), f"{label}: Rust is not a per-colour function"
    oracle = SeqTarget(Cube.read(cube), **spec)
    or_f64 = np.asarray(oracle.render(uniq.reshape(-1, 1, 3))).reshape(-1, 3)
    rs_f64 = _rust_f64(tmp, cube, spec, uniq.reshape(-1, 1, 3)).reshape(-1, 3)
    assert np.array_equal(_q(rs_f64), rs_uniq), f"{label}: lut-apply disagrees with the full render"
    n_a = assert_u8_boundary_gate(_q(or_f64)[:, None], rs_uniq[:, None], or_f64[:, None], rs_f64[:, None], label=f"{label}-gate-a")
    n_b = _gate_b(py_flat, rs_flat, inv, _q(or_f64), or_f64, rs_f64, label)
    print(f"[lut-display] {label}: colours={len(uniq)} gate_a_points={n_a} gate_b_points={n_b}")
    return n_a, n_b


def log_gate(
    tmp: Path, cube: Path, spec: dict[str, Any], dng_path: Path, py: np.ndarray, rs: np.ndarray, label: str, *, sample: int = 20000
) -> tuple[int, int]:
    dng = open_dng(dng_path)
    try:
        codes = demosaic_full(dng)
        colour = scene_colour(dng)
        orientation = int(dng.orientation)
    finally:
        dng.release()
    h, w = codes.shape[:2]
    oh, ow = py.shape[:2]
    rng = np.random.default_rng(len(label))
    picks = {(int(y), int(x)) for y, x in zip(rng.integers(0, oh, sample), rng.integers(0, ow, sample))}
    picks |= {(int(y), int(x)) for y, x, _c in np.argwhere(py != rs)}
    pos = sorted(picks)
    src = np.array([_unoriented_yx(y, x, h, w, orientation) for y, x in pos])
    px_codes = codes[src[:, 0], src[:, 1]].reshape(-1, 1, 3)
    codes_npy = tmp / "gate-codes.npy"
    np.save(codes_npy, px_codes)
    code, payload = run_json(["scene-xyz", "--dng", str(dng_path), "--codes", str(codes_npy), "--out", str(tmp / "gate-xyz-rs.npy")])
    assert code == 0, payload
    xyz_rs = np.load(tmp / "gate-xyz-rs.npy")
    assert_rel(colour.xyz(px_codes), xyz_rs, label=f"{label}-xyz")
    neutral = _neutral_display(px_codes)
    oracle = SeqTarget(Cube.read(cube), **spec)
    or_f64 = np.asarray(oracle.render(neutral, xyz_rs)).reshape(-1, 3)
    rs_f64 = _rust_f64(tmp, cube, spec, neutral, xyz_rs).reshape(-1, 3)
    ys, xs = np.array([p[0] for p in pos]), np.array([p[1] for p in pos])
    rs_px, py_px = rs[ys, xs], py[ys, xs]
    assert np.array_equal(_q(rs_f64), rs_px), f"{label}: lut-apply disagrees with the full render"
    n_a = assert_u8_boundary_gate(_q(or_f64)[:, None], rs_px[:, None], or_f64[:, None], rs_f64[:, None], label=f"{label}-gate-a")
    n_b = _gate_b(py_px, rs_px, np.arange(len(pos)), _q(or_f64), or_f64, rs_f64, label)
    assert n_b == int((py != rs).sum()), (label, n_b, int((py != rs).sum()))
    print(f"[lut-log] {label}: sampled={len(pos)} gate_a_points={n_a} gate_b_points={n_b}")
    return n_a, n_b
