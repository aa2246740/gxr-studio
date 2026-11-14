"""Gate A / Gate B verification helpers and threshold/negative-control test runners."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any, Callable

import numpy as np

from gxr_studio.lut import spaces
from gxr_studio.lut.cube import Cube
from gxr_studio.lut.photo import _apply as _apply_ref

GRADE_MUL = np.array([1.05, 0.96, 0.91], dtype=np.float64)
GRADE_OFF = np.array([0.02, -0.01, 0.03], dtype=np.float64)
THRESHOLD_RED_DELTA = 0.00011095497837924685
THRESHOLD_OFF = np.array([0.02 - THRESHOLD_RED_DELTA, -0.01, 0.03], dtype=np.float64)


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
    """Gate B: mechanical attribution for CPU uint8 differences vs real Python (allowed iff Gate A holds and |diff| <= 1)."""
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
    from support.lut_oracle import SeqTarget, seq_apply

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
    from support.lut_oracle import SeqTarget, seq_display_lut_u8

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
    *,
    control: str = "libm",
) -> None:
    """Negative control: verify Gate A passes with portable kernels and fails at want_fail_px when host kernel is restored."""
    from support.core_cli import run_npy
    from support.lut_cases import assert_rel
    from support.lut_oracle import SeqTarget

    cube_path = tmp_path / f"{label}-neg.cube"
    cube_path.write_text(cube_text_str, encoding="utf-8")
    py_cube = Cube.read(cube_path)
    rgb_npy = tmp_path / f"{label}-neg-rgb.npy"
    np.save(rgb_npy, rgb)
    rs_f64 = run_npy(
        ["lut-apply", "--cube", str(cube_path), "--space", json.dumps(spec), "--rgb", str(rgb_npy)],
        tmp_path / f"{label}-neg-out.npy",
    )

    or_portable = SeqTarget(py_cube, **spec, use_libm=True, use_seq_interp=True).render(rgb)
    assert_rel(or_portable, rs_f64, label=f"{label}-{control}-gate-a")

    if control == "libm":
        or_host = SeqTarget(py_cube, **spec, use_libm=False, use_seq_interp=True).render(rgb)
    elif control == "interp":
        or_host = SeqTarget(py_cube, **spec, use_libm=True, use_seq_interp=False).render(rgb)
    else:
        raise ValueError(f"unknown negative control {control!r}")

    limit = 1e-12 * np.maximum(1.0, np.abs(or_host))
    rel = np.abs(or_host - rs_f64) / np.maximum(1.0, np.abs(or_host))
    bad = np.abs(or_host - rs_f64) > limit
    r_idx, c_idx = want_fail_px
    assert np.any(bad[r_idx, c_idx]), f"{label} host {control} unexpectedly passed Gate A at {want_fail_px}: max_rel={float(np.max(rel)):.3e}"
    print(f"[neg-control] {label} host {control} failed Gate A at {want_fail_px} max_rel={float(np.max(rel[r_idx, c_idx])):.3e}")


def check_blas_gamma24_and_threshold_points(tmp_path: Path, py_target_fn: Callable[..., Any]) -> None:
    """PORT-WAVE-1 platform BLAS rule: gamma24 [141, 136, 0] float64 and threshold.cube CPU u8 points."""
    from support.core_cli import run_npy
    from support.lut_cases import assert_rel, cube_text
    from support.lut_oracle import SeqTarget, fma_render_display_px, seq_apply

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
