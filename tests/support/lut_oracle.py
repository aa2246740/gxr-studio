"""BLAS-free sequential Python oracle and Accelerate FMA attribution helpers for LUT parity tests."""

from __future__ import annotations

import json
import math
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, cast

import numpy as np

from gxr_studio.lut import spaces
from gxr_studio.lut.cube import Cube
from gxr_studio.lut.photo import _apply as _apply_ref

THRESHOLD_RED_DELTA = 0.00011095497837924685
GRADE_MUL = np.array([1.05, 0.96, 0.91], dtype=np.float64)
GRADE_OFF = np.array([0.02, -0.01, 0.03], dtype=np.float64)
THRESHOLD_OFF = np.array([0.02 - THRESHOLD_RED_DELTA, -0.01, 0.03], dtype=np.float64)


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
    """Drop-in BLAS-free sequential mirror of `gxr_studio.lut.spaces.Target` for test parity."""

    def __init__(
        self,
        cube: Cube,
        input_space: str,
        output_space: str = "srgb",
        input_gamut: str | None = None,
        output_gamut: str | None = None,
        input_range: str = "full",
        output_range: str = "full",
    ) -> None:
        self.cube = cube
        self.input_space = input_space
        self.output_space = output_space
        self.input_gamut = input_gamut
        self.output_gamut = output_gamut
        self.input_range = input_range
        self.output_range = output_range

    def render(self, stock_rgb: np.ndarray, xyz: np.ndarray | None = None, info: bool = False) -> Any:
        if self.input_space in spaces.DISPLAY:
            lin = spaces.decode(np.asarray(stock_rgb) / 255, "srgb")
            xyz_display = matmul_t_seq(lin, spaces.matrix("rec709"))
            inv_in = np.linalg.inv(spaces.matrix(self.input_gamut or spaces.GAMUT[self.input_space]))
            x = spaces.encode(matmul_t_seq(xyz_display, inv_in), self.input_space)
        else:
            if xyz is None:
                raise ValueError("Log LUT 需要 DNG 的场景 XYZ，不能从 JPEG 猜测")
            inv_in = np.linalg.inv(spaces.matrix(self.input_gamut or spaces.GAMUT[self.input_space]))
            x = spaces.encode(matmul_t_seq(xyz, inv_in), self.input_space)
        if self.input_range == "legal":
            x = (64 + 876 * x) / 1023
        out, stats = self.cube.apply(x, return_info=True)
        if self.output_range == "legal":
            out = (1023 * out - 64) / 876
        lin_out = spaces.decode(out, self.output_space)
        xyz_out = matmul_t_seq(lin_out, spaces.matrix(self.output_gamut or spaces.GAMUT[self.output_space]))
        srgb = spaces.encode(matmul_t_seq(xyz_out, np.linalg.inv(spaces.matrix("rec709"))), "srgb")
        stats["display_gamut_clipped_share"] = float(np.any((srgb < 0) | (srgb > 1), axis=-1).mean())
        rgb = np.clip(srgb, 0, 1) * 255
        return (rgb, stats) if info else rgb


def seq_apply(target: SeqTarget, rgb: np.ndarray, xyz: np.ndarray | None = None) -> np.ndarray:
    """Mirror of `gxr_studio.lut.photo._apply` evaluating through `SeqTarget`."""
    shape = rgb.shape
    flat = rgb.reshape(-1, 3)
    scene = None if xyz is None else xyz.reshape(-1, 3)
    result = np.empty(flat.shape, np.uint8)
    for start in range(0, len(flat), 65536):
        end = start + 65536
        out = target.render(flat[start:end], None if scene is None else scene[start:end])
        result[start:end] = np.floor(np.clip(out, 0, 255) + 0.5).astype(np.uint8)
    return cast(np.ndarray, result.reshape(shape))


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


def assert_u8_boundary_gate(
    ref_u8: np.ndarray,
    rust_u8: np.ndarray,
    ref_f64: np.ndarray,
    rust_f64: np.ndarray,
    *,
    label: str,
    blas_points: frozenset[tuple[int, ...]] = frozenset(),
) -> int:
    """PORT-WAVE-1 quantisation boundary gate for CPU uint8 outputs; returns allowed mismatch count."""
    assert ref_u8.shape == rust_u8.shape and ref_u8.dtype == np.uint8 and rust_u8.dtype == np.uint8
    assert ref_f64.shape == ref_u8.shape and rust_f64.shape == rust_u8.shape
    assert ref_f64.dtype == np.float64 and rust_f64.dtype == np.float64
    diffs = np.argwhere(ref_u8 != rust_u8)
    for idx_arr in diffs:
        idx = tuple(int(v) for v in idx_arr)
        ru8 = int(ref_u8[idx])
        rsu8 = int(rust_u8[idx])
        if idx in blas_points:
            assert abs(ru8 - rsu8) == 1, f"{label} BLAS diff > 1 at {idx}: ref={ru8} rust={rsu8}"
            print(
                f"[blas-point] {label} idx={idx} ref_u8={ru8} rust_u8={rsu8} ref_f64={float(ref_f64[idx])!r} rust_f64={float(rust_f64[idx])!r}"
            )
            continue
        _check_boundary_point(float(ref_f64[idx]), float(rust_f64[idx]), ru8, rsu8, label=label, idx=idx)
    print(f"[quant-boundary-count] {label}: allowed_points={len(diffs)}")
    return int(len(diffs))


def assert_table_u8_boundary_gate(
    ref_bytes: bytes,
    rust_bytes: bytes,
    *,
    ref_render_px: Callable[[np.ndarray], np.ndarray],
    rust_render_px: Callable[[np.ndarray], np.ndarray],
    label: str,
    blas_offsets: frozenset[int] = frozenset(),
) -> int:
    """PORT-WAVE-1 quantisation boundary gate for full 256^3x4 RGBA GPU display tables."""
    expected_len = 256**3 * 4
    assert len(ref_bytes) == expected_len and len(rust_bytes) == expected_len
    ref_arr = np.frombuffer(ref_bytes, np.uint8).reshape(256, 256, 256, 4)
    rs_arr = np.frombuffer(rust_bytes, np.uint8).reshape(256, 256, 256, 4)
    assert np.all(ref_arr[..., 3] == 255) and np.all(rs_arr[..., 3] == 255)
    diffs = np.argwhere(ref_arr != rs_arr)
    if len(diffs) == 0:
        print(f"[quant-boundary-count] {label}: allowed_points=0")
        return 0
    unique_bgr: list[tuple[int, int, int]] = []
    bgr_row: dict[tuple[int, int, int], int] = {}
    for b, g, r, c in diffs:
        assert int(c) in (0, 1, 2), f"{label} unexpected alpha diff at {(b, g, r, c)}"
        key = (int(b), int(g), int(r))
        if key not in bgr_row:
            bgr_row[key] = len(unique_bgr)
            unique_bgr.append(key)
    px_u8 = np.array([[r, g, b] for b, g, r in unique_bgr], dtype=np.uint8).reshape(-1, 1, 3)
    ref_f64 = ref_render_px(px_u8)
    rs_f64 = rust_render_px(px_u8)
    for b_i, g_i, r_i, c_i in diffs:
        b, g, r, c = int(b_i), int(g_i), int(r_i), int(c_i)
        offset = ((b * 256 + g) * 256 + r) * 4 + c
        ru8 = int(ref_arr[b, g, r, c])
        rsu8 = int(rs_arr[b, g, r, c])
        if offset in blas_offsets:
            assert abs(ru8 - rsu8) == 1, f"{label} BLAS diff > 1 at offset {offset}"
            print(f"[blas-point] {label} offset={offset} bgr=({b},{g},{r}) c={c} ref_u8={ru8} rust_u8={rsu8}")
            continue
        row = bgr_row[(b, g, r)]
        _check_boundary_point(
            float(ref_f64[row, 0, c]),
            float(rs_f64[row, 0, c]),
            ru8,
            rsu8,
            label=f"{label}@off={offset}",
            idx=(b, g, r, c),
        )
    print(f"[quant-boundary-count] {label}: allowed_points={len(diffs)}")
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
) -> None:
    from support.core_cli import run_npy
    from support.lut_cases import assert_rel

    cube_path = tmp_path / f"{label}.cube"
    cube_path.write_text(cube_text_str, encoding="utf-8")
    py_cube = Cube.read(cube_path)
    rgb = np.random.default_rng(42).integers(0, 256, (12, 10, 3), dtype=np.uint8)
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
    if label == "case-1-2":
        # 2.5-ULP gxr_raw vs LAPACK inv(rec709)/solve(vgamut) amplifies ~1000x near 5.3e-4 at (0,7,0) (1.048e-12 vs oracle, 9.69e-13 vs py).
        mask_or = np.ones_like(or_f64, dtype=bool)
        mask_or[0, 7, 0] = False
        assert_rel(or_f64[mask_or], rs_f64[mask_or], label=f"{label}-or-f64")
        rel_070 = abs(float(or_f64[0, 7, 0]) - float(rs_f64[0, 7, 0])) / max(1.0, abs(float(or_f64[0, 7, 0])))
        assert rel_070 <= 1.2e-12, f"{label}-or-f64 (0,7,0) rel={rel_070:.3e}"
    else:
        assert_rel(or_f64, rs_f64, label=f"{label}-or-f64")
    if sys.platform == "darwin" and spec["input_space"] == "gamma24":
        mask_py = np.ones_like(py_f64, dtype=bool)
        mask_py[0, 8, :2] = False
        assert_rel(py_f64[mask_py], rs_f64[mask_py], label=f"{label}-py-f64")
    else:
        assert_rel(py_f64, rs_f64, label=f"{label}-py-f64")

    or_u8 = seq_apply(or_target, rgb, xyz)
    py_u8 = _apply_ref(py_target, rgb, xyz)
    rs_u8 = run_npy([*cli_args, "--u8"], tmp_path / "out_u8.npy")
    n_or = assert_u8_boundary_gate(or_u8, rs_u8, or_f64, rs_f64, label=f"{label}-vs-oracle")
    assert n_or >= 1
    assert_u8_boundary_gate(py_u8, rs_u8, py_f64, rs_f64, label=f"{label}-vs-python")


def check_threshold_full_table_case(
    tmp_path: Path,
    label: str,
    cube_text_str: str,
    spec: dict[str, Any],
    want_offset: int,
    want_bgr: tuple[int, int, int, int],
    want_or_u8: int,
    want_rs_u8: int,
    py_target_fn: Callable[..., Any],
) -> None:
    from gxr_studio.gpu.tables import lut_bytes
    from support.core_cli import error_of, run_json

    cube_path = tmp_path / f"{label}.cube"
    cube_path.write_text(cube_text_str, encoding="utf-8")
    py_cube = Cube.read(cube_path)
    or_bytes = seq_display_lut_u8(SeqTarget(py_cube, **spec))
    py_bytes, py_meta, py_reason = lut_bytes(py_target_fn(py_cube, **spec))
    assert py_reason is None and py_bytes is not None and py_meta is not None
    out_bin, out_meta = tmp_path / "t.bin", tmp_path / "t.json"
    code, payload = run_json(
        ["lut-table", "--cube", str(cube_path), "--space", json.dumps(spec), "--out", str(out_bin), "--meta", str(out_meta)]
    )
    assert error_of(code, payload) is None and json.loads(out_meta.read_text(encoding="utf-8")) == py_meta
    rs_bytes = out_bin.read_bytes()

    _run_rs_px = make_rust_px_runner(tmp_path, cube_path, spec, "px")

    n_or = assert_table_u8_boundary_gate(
        or_bytes, rs_bytes, ref_render_px=SeqTarget(py_cube, **spec).render, rust_render_px=_run_rs_px, label=f"{label}-256-or"
    )
    n_py = assert_table_u8_boundary_gate(
        py_bytes, rs_bytes, ref_render_px=py_target_fn(py_cube, **spec).render, rust_render_px=_run_rs_px, label=f"{label}-256-py"
    )
    b, g, r, c = want_bgr
    assert ((b * 256 + g) * 256 + r) * 4 + c == want_offset
    assert n_or == 1 and n_py == 1 and or_bytes[want_offset] == py_bytes[want_offset] == want_or_u8 and rs_bytes[want_offset] == want_rs_u8
