"""Parity: gxr-cli (and gxr-wasm) against Python gxr_studio.lut & gpu.tables.

Gates (UNIT-lut.md / CONVENTIONS §5):
- errors, sizes, SPACE_IDS, legal flags, identify dicts, _undeclared_choice: exact;
- float64 outputs (Cube.apply, encode/decode, Target.render, meta floats, scene xyz):
  |a - b| <= 1e-12 * max(1, |a|);
- uint8 outputs (_apply, display_lut_u8): exact or PORT-WAVE-1 quantisation boundary / platform BLAS rules;
- log table float32 lattice: exact bits (np.array_equal after dtype assert);
- scene-xyz: all real DNGs via GXR_STUDIO_TEST_PARENT/captures/, plus synthetic errors.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from gxr_studio.gpu.tables import lut_bytes
from gxr_studio.lut import spaces
from gxr_studio.lut.cube import Cube
from gxr_studio.lut.errors import LutError
from gxr_studio.lut.identify import identify
from gxr_studio.lut.page import input_choice
from gxr_studio.lut.photo import _apply
from gxr_studio.lut.service import LutService
from gxr_studio.render.params import EditError
from support import lut_cases
from support.core_cli import error_of, require_cli, run_json, run_npy
from support.dng_writer import write_dng
from support.lut_cases import (
    AMPLIFIED_CASES,
    BAD_CUBE_CASES,
    IDENTIFY_CASES,
    SHAPER_APPLELOG_CUBE,
    SHAPER_APPLELOG_SPEC,
    THRESHOLD_CASES,
    r3_threshold_rgb,
    shaper_applelog_rgb,
    space_depth_text,
    threshold_cube_text,
)
from support.lut_cases import assert_meta_same as _assert_meta_same
from support.lut_cases import assert_rel as _assert_rel
from support.lut_cases import check_scene_xyz as _check_scene_xyz
from support.lut_cases import cube_text as _cube_text
from support.lut_cases import real_dngs as _real_dngs
from support.lut_oracle import (
    SeqTarget,
    assert_table_u8_boundary_gate,
    assert_table_u8_gate_b,
    assert_u8_boundary_gate,
    assert_u8_gate_b,
    check_blas_gamma24_and_threshold_points,
    check_gate_b_classification_controls,
    check_negative_control_case,
    check_r5_pathological_applelog,
    check_r5_steep_slope_family,
    check_shaper_applelog_full_table,
    check_shaper_zero_cases,
    check_threshold_cpu_case,
    check_threshold_cube_full_256_gpu_table_and_blas_point,
    check_threshold_full_table_case,
    make_rust_px_runner,
    seq_apply,
    seq_display_lut_u8,
)
from support.wasm_check import run_area

_STUDIO = Path(__file__).resolve().parents[1]
_PARENT = Path(os.environ.get("GXR_STUDIO_TEST_PARENT", str(_STUDIO.parent)))
CAPTURES = _PARENT / "captures"
_CAP = 60


REAL = _real_dngs(CAPTURES, _CAP)


def _real_id(path: Path) -> str:
    return path.relative_to(CAPTURES).as_posix().replace("/", "_")


def _write_cube(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _python_target(
    cube: Cube,
    input_space: str,
    output_space: str = "srgb",
    input_gamut: str | None = None,
    output_gamut: str | None = None,
    input_range: str = "full",
    output_range: str = "full",
) -> spaces.Target:
    return LutService.target(
        None,  # type: ignore[arg-type]
        cube,
        input_space,
        output_space,
        input_gamut=input_gamut,
        output_gamut=output_gamut,
        input_range=input_range,
        output_range=output_range,
    )


# ---- Unit A: cube parse & tetrahedral, spaces encode/decode & Target.render ----


@pytest.mark.parametrize("size", [2, 17, 33, 65])
def test_parse_and_identity_sizes(tmp_path: Path, size: int) -> None:
    require_cli()
    text = _cube_text(size=size, title=f"id-{size}")
    cube_path = _write_cube(tmp_path, f"id-{size}.cube", text)
    py_cube = Cube.read(cube_path)

    code, payload = run_json(["lut-parse", "--cube", str(cube_path)])
    assert error_of(code, payload) is None
    assert payload["title"] == py_cube.title
    assert payload["sha256"] == py_cube.source_sha256
    assert payload["size_1d"] == 0 and payload["size_3d"] == size
    assert payload["shape_3d"] == [size, size, size, 3]
    assert payload["shape_1d"] is None
    assert payload["domain_min"] == [0.0, 0.0, 0.0]
    assert payload["domain_max"] == [1.0, 1.0, 1.0]

    rng = np.random.default_rng(1)
    rgb = (rng.uniform(0, 1, (500, 3)) * 255.0).astype(np.float64)
    in_npy = tmp_path / "rgb.npy"
    np.save(in_npy, rgb)
    spec = {"input_space": "srgb", "output_space": "srgb"}
    py_out = _python_target(py_cube, **spec).render(rgb)
    rs_out = run_npy(
        ["lut-apply", "--cube", str(cube_path), "--space", json.dumps(spec), "--rgb", str(in_npy)],
        tmp_path / "out.npy",
    )
    _assert_rel(py_out, rs_out, label=f"identity-{size}")


def test_parse_variants_bom_cr_title_and_1d_shaper(tmp_path: Path) -> None:
    require_cli()
    # BOM + CR-only line endings + escaped quote & # inside TITLE + 1D shaper + 3D
    lines = ["0 0 0", "0.2 0.25 0.3", "0.7 0.75 0.8", "1 1 1"]
    cube3 = [f"{r} {g} {b}" for b in (0, 1) for g in (0, 1) for r in (0, 1)]
    cube3[1] = "0.9 0.1 0.2"  # R-fastest axis check
    body = (
        'TITLE "My \\"Quoted\\" #1 Title" # trailing comment\r'
        "LUT_1D_SIZE 4\r"
        "LUT_3D_SIZE 2\r"
        "LUT_1D_INPUT_RANGE 0.0 1.0\r"
        "LUT_3D_INPUT_RANGE -0.1 1.1\r" + "\r".join(lines + cube3) + "\r"
    )
    cube_path = tmp_path / "shaper.cube"
    cube_path.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
    py_cube = Cube.read(cube_path)

    code, payload = run_json(["lut-parse", "--cube", str(cube_path)])
    assert error_of(code, payload) is None
    assert payload["title"] == py_cube.title
    assert payload["sha256"] == py_cube.source_sha256
    assert payload["kind"] == "1D+3D"
    assert payload["shape_1d"] == [4, 3] and payload["shape_3d"] == [2, 2, 2, 3]
    assert payload["comments"] == list(py_cube.comments)

    # Probe exact tetrahedral tie-breaking & clamping through shaper
    rgb = np.array(
        [
            [0.0, 0.0, 0.0],
            [255.0, 0.0, 0.0],
            [0.0, 255.0, 0.0],
            [128.0, 128.0, 51.0],
            [51.0, 230.0, 230.0],
            [-10.0, 128.0, 300.0],
        ],
        dtype=np.float64,
    )
    in_npy = tmp_path / "tie.npy"
    np.save(in_npy, rgb)
    spec = {"input_space": "srgb", "output_space": "srgb"}
    py_out = _python_target(py_cube, **spec).render(rgb)
    rs_out = run_npy(
        ["lut-apply", "--cube", str(cube_path), "--space", json.dumps(spec), "--rgb", str(in_npy)],
        tmp_path / "tie-out.npy",
    )
    _assert_rel(py_out, rs_out, label="shaper-tie")


@pytest.mark.parametrize(("text", "expected_prefix"), BAD_CUBE_CASES)
def test_bad_cubes_match_errors(tmp_path: Path, text: str, expected_prefix: str) -> None:
    require_cli()
    p = _write_cube(tmp_path, "bad.cube", text)
    with pytest.raises(ValueError) as exc_info:
        Cube.read(p)
    assert expected_prefix in str(exc_info.value)

    code, payload = run_json(["lut-parse", "--cube", str(p)])
    err = error_of(code, payload)
    assert err is not None
    assert expected_prefix in err.message


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("underscore-size", "LUT_3D_SIZE 0_2\n" + "0 0 0\n" * 8),
        ("underscore-float", "LUT_3D_SIZE 2\n" + "0 0 0_0\n" * 8),
        ("unicode-size", "LUT_3D_SIZE ２\n" + "0 0 0\n" * 8),
        ("unicode-float-ws", "LUT_3D_SIZE +0_2\nLUT_3D_INPUT_RANGE 0_0 １.０\n" + "\x1f0\x1f 0 １_２e-１\n" * 8),
    ],
)
def test_parse_python_numeric_syntax_and_unicode(tmp_path: Path, name: str, text: str) -> None:
    require_cli()
    p = _write_cube(tmp_path, f"{name}.cube", text)
    py_cube = Cube.read(p)
    code, payload = run_json(["lut-parse", "--cube", str(p)])
    assert error_of(code, payload) is None
    assert payload["size_3d"] == py_cube.table3.shape[0] and payload["sha256"] == py_cube.source_sha256


@pytest.mark.parametrize("space", spaces.SPACES)
def test_encode_and_decode_all_spaces(tmp_path: Path, space: str) -> None:
    require_cli()
    # Cover negative, zero, kink boundaries, midtones and >1 values
    vals = np.array(
        [-0.2, -0.05, 0.0, 0.00089, 0.0031308, 0.006, 0.01, 0.01125, 0.018, 0.04045, 0.081, 0.138, 0.18, 0.181, 0.5, 1.0, 2.5],
        dtype=np.float64,
    )
    in_npy = tmp_path / "vals.npy"
    np.save(in_npy, vals)

    py_enc = spaces.encode(vals, space)
    rs_enc = run_npy(["lut-encode", "--space", space, "--in", str(in_npy)], tmp_path / "enc.npy")
    _assert_rel(py_enc, rs_enc, label=f"encode-{space}")

    py_dec = spaces.decode(vals, space)
    rs_dec = run_npy(["lut-encode", "--space", space, "--in", str(in_npy), "--decode"], tmp_path / "dec.npy")
    _assert_rel(py_dec, rs_dec, label=f"decode-{space}")


def test_encode_decode_unknown_space_errors(tmp_path: Path) -> None:
    require_cli()
    in_npy = tmp_path / "v.npy"
    np.save(in_npy, np.array([0.5], dtype=np.float64))
    code, payload = run_json(["lut-encode", "--space", "bogus", "--in", str(in_npy), "--out", str(tmp_path / "o.npy")])
    err = error_of(code, payload)
    assert err is not None and "不支持输入空间：bogus" in err.message

    code, payload = run_json(["lut-encode", "--space", "bogus", "--in", str(in_npy), "--out", str(tmp_path / "o2.npy"), "--decode"])
    err = error_of(code, payload)
    assert err is not None and "不支持输出空间：bogus" in err.message


# ---- Unit B part 1: _apply uint8 & display/log Target.render ----


@pytest.mark.parametrize(
    ("input_space", "output_space", "input_range", "output_range", "input_gamut", "output_gamut"),
    [
        ("srgb", "srgb", "full", "full", None, None),
        ("rec709", "srgb", "full", "full", None, None),
        ("gamma24", "rec709", "legal", "legal", None, "rec2020"),
        ("srgb", "vlog", "full", "legal", None, None),
        ("llog", "srgb", "full", "full", None, None),
        ("llog709", "gamma24", "legal", "full", None, None),
        ("slog3", "rec709", "full", "full", None, None),
        ("slog3-sgamut3", "linear", "legal", "legal", None, None),
        ("flog", "srgb", "full", "full", None, None),
        ("flog2", "srgb", "full", "full", "rec2020", "rec709"),
        ("vlog", "srgb", "full", "full", None, None),
        ("applelog", "srgb", "full", "full", None, None),
        ("linear", "srgb", "full", "full", None, None),
    ],
)
def test_target_render_and_apply_u8_parity(
    tmp_path: Path,
    input_space: str,
    output_space: str,
    input_range: str,
    output_range: str,
    input_gamut: str | None,
    output_gamut: str | None,
) -> None:
    require_cli()
    cube_path = _write_cube(
        tmp_path,
        "grade.cube",
        _cube_text(lambda x: np.clip(x * [1.05, 0.96, 0.91] + [0.02, -0.01, 0.03], 0, 1), size=9),
    )
    py_cube = Cube.read(cube_path)
    spec = {
        "input_space": input_space,
        "output_space": output_space,
        "input_gamut": input_gamut,
        "output_gamut": output_gamut,
        "input_range": input_range,
        "output_range": output_range,
    }
    target = _python_target(py_cube, **spec)

    oracle = SeqTarget(py_cube, **spec)
    rng = np.random.default_rng(42)
    rgb = rng.integers(0, 256, (12, 10, 3), dtype=np.uint8)
    rgb_npy = tmp_path / "rgb.npy"
    np.save(rgb_npy, rgb)

    xyz = None
    cli_args = ["lut-apply", "--cube", str(cube_path), "--space", json.dumps(spec), "--rgb", str(rgb_npy)]
    if input_space not in spaces.DISPLAY:
        xyz = np.exp(rng.uniform(np.log(0.005), np.log(2.0), (12, 10, 3)))
        xyz_npy = tmp_path / "xyz.npy"
        np.save(xyz_npy, xyz)
        cli_args.extend(["--xyz", str(xyz_npy)])

    or_f64 = oracle.render(rgb, xyz)
    py_f64 = target.render(rgb, xyz)
    rs_f64 = run_npy(cli_args, tmp_path / "out_f64.npy")
    _assert_rel(or_f64, rs_f64, label=f"render-{input_space}->{output_space}")

    or_u8 = seq_apply(oracle, rgb, xyz)
    py_u8 = _apply(target, rgb, xyz)
    rs_u8 = run_npy([*cli_args, "--u8"], tmp_path / "out_u8.npy")
    n_or = assert_u8_boundary_gate(or_u8, rs_u8, or_f64, rs_f64, label=f"or-{input_space}->{output_space}")
    n_py = assert_u8_gate_b(py_u8, rs_u8, or_u8, or_f64, rs_f64, label=f"py-{input_space}->{output_space}", py_f64=py_f64)
    assert n_or == 0 and n_py == 0


def test_quantisation_boundary_gate_rejects_violations() -> None:
    # Gate A: diff > 1, float gap > 1e-12, away from k+0.5 rejected; Gate B: rejects if Gate A fails or |py - rust| > 1.
    with pytest.raises(AssertionError, match="u8 diff > 1"):
        assert_u8_boundary_gate(np.array([10], np.uint8), np.array([12], np.uint8), np.array([10.5]), np.array([11.5]), label="neg-step2")
    with pytest.raises(AssertionError, match="1e-12 gate"):
        assert_u8_boundary_gate(np.array([46], np.uint8), np.array([47], np.uint8), np.array([46.499]), np.array([46.501]), label="neg-gap")
    with pytest.raises(AssertionError, match="rounded code mismatch"):
        assert_u8_boundary_gate(
            np.array([46], np.uint8), np.array([47], np.uint8), np.array([46.4]), np.array([46.4 + 1e-13]), label="neg-nonbound"
        )
    with pytest.raises(AssertionError, match="Gate A float64 diff exceeds 1e-12"):
        assert_u8_gate_b(
            np.array([10], np.uint8), np.array([11], np.uint8), np.array([11], np.uint8), np.array([10.0]), np.array([10.1]), label="neg-b"
        )
    with pytest.raises(AssertionError, match="Gate B diff > 1"):
        assert_u8_gate_b(
            np.array([10], np.uint8),
            np.array([12], np.uint8),
            np.array([12], np.uint8),
            np.array([11.5]),
            np.array([11.5]),
            label="neg-b-step2",
        )
    arr = np.zeros((256, 256, 256, 4), np.uint8)
    arr[..., 3] = 255
    arr[0, 0, 0, 0] = 10
    pb = arr.tobytes()
    arr[0, 0, 0, 0] = 12
    rb = arr.tobytes()
    with pytest.raises(AssertionError, match="Gate B diff > 1"):
        assert_table_u8_gate_b(
            pb,
            rb,
            rb,
            or_render_px=lambda px: np.full(px.shape, 11.5, dtype=np.float64),
            rust_render_px=lambda px: np.full(px.shape, 11.5, dtype=np.float64),
            label="neg-tbl-step2",
        )
    check_gate_b_classification_controls()


@pytest.mark.parametrize(("label", "shaper", "offset", "spec"), THRESHOLD_CASES, ids=[c[0] for c in THRESHOLD_CASES])
def test_threshold_15_cpu_quantisation_boundary(tmp_path: Path, label: str, shaper: bool, offset: float, spec: dict[str, Any]) -> None:
    require_cli()
    check_threshold_cpu_case(tmp_path, label, threshold_cube_text(offset, shaper), spec, _python_target)


@pytest.mark.parametrize(("label", "shaper", "offset", "spec"), AMPLIFIED_CASES, ids=[c[0] for c in AMPLIFIED_CASES])
def test_threshold_amplified_cpu_cases(tmp_path: Path, label: str, shaper: bool, offset: float, spec: dict[str, Any]) -> None:
    require_cli()
    check_threshold_cpu_case(tmp_path, label, threshold_cube_text(offset, shaper), spec, _python_target, rgb_override=r3_threshold_rgb())


@pytest.mark.parametrize(
    ("label", "want_px"),
    [("case-1-2", (0, 7)), ("amplified-24", (6, 25)), ("amplified-25", (6, 25)), ("amplified-35", (6, 25))],
)
def test_gate_a_negative_control_host_pow(tmp_path: Path, label: str, want_px: tuple[int, int]) -> None:
    require_cli()
    shaper, offset, spec = next((sh, off, sp) for lbl, sh, off, sp in (*THRESHOLD_CASES, *AMPLIFIED_CASES) if lbl == label)
    rgb = np.random.default_rng(42).integers(0, 256, (12, 10, 3), dtype=np.uint8) if label == "case-1-2" else r3_threshold_rgb()
    check_negative_control_case(tmp_path, label, threshold_cube_text(offset, shaper), spec, rgb, want_px)


def test_gate_a_negative_control_host_interp(tmp_path: Path) -> None:
    """Negative control: Gate A passes with interp_1d_seq and fails when host np.interp is restored."""
    require_cli()
    check_negative_control_case(
        tmp_path, "shaper-applelog", SHAPER_APPLELOG_CUBE, SHAPER_APPLELOG_SPEC, shaper_applelog_rgb(), (0, 0), control="interp"
    )


def test_shaper_applelog_r4_cpu(tmp_path: Path) -> None:
    """Sol r4 minimal shaper-applelog.cube with RGB [137, 201, 83]: Gate A=0, Gate B <= 1 code."""
    require_cli()
    n_or, n_py = check_threshold_cpu_case(
        tmp_path,
        "shaper-applelog",
        SHAPER_APPLELOG_CUBE,
        SHAPER_APPLELOG_SPEC,
        _python_target,
        rgb_override=shaper_applelog_rgb(),
    )
    assert n_or == 0
    if sys.platform == "darwin":
        assert n_py == 1


def test_shaper_zero_42_constructions(tmp_path: Path) -> None:
    """Sol r4 42 near-zero 1D shaper constructions: Gate A 1e-12 with no masks, classified Gate B."""
    require_cli()
    check_shaper_zero_cases(tmp_path, _python_target)


def test_r5_pathological_applelog_cpu(tmp_path: Path) -> None:
    """Sol r5 pathological +-3e13 LUT: accept parity, Gate A=0, uncapped Gate B with attribution."""
    require_cli()
    check_r5_pathological_applelog(tmp_path, _python_target)


@pytest.mark.parametrize("kind", ["1d", "3d", "1d3d"])
def test_r5_steep_slope_lut_families(tmp_path: Path, kind: str) -> None:
    """Sol r5 three steep-slope ill-conditioned LUT families (`1d`, `3d`, `1d3d`)."""
    require_cli()
    check_r5_steep_slope_family(tmp_path, kind, _python_target)


def test_shaper_applelog_full_256_gpu_table(tmp_path: Path) -> None:
    """Sol r4 full 256^3 GPU display table for shaper-applelog.cube: Gate A=0, every Gate B diff <= 1 code."""
    require_cli()
    n_or, n_py = check_shaper_applelog_full_table(tmp_path, _python_target)
    assert n_or == 0
    if sys.platform == "darwin":
        assert n_py == 28820


@pytest.mark.parametrize(
    ("label", "want_offset", "want_bgr", "want_or_u8", "want_py_u8", "want_rs_u8"),
    [
        ("case-0-1", 47724632, (182, 14, 22, 0), 47, 46, 47),
        ("case-1-0", 139828, (0, 136, 141, 0), 142, 141, 142),
        ("amplified-25", 35184641, (134, 56, 0, 1), 85, 86, 85),
    ],
)
def test_threshold_full_256_gpu_tables(
    tmp_path: Path, label: str, want_offset: int, want_bgr: tuple[int, int, int, int], want_or_u8: int, want_py_u8: int, want_rs_u8: int
) -> None:
    require_cli()
    shaper, offset, spec = next((sh, off, sp) for lbl, sh, off, sp in (*THRESHOLD_CASES, *AMPLIFIED_CASES) if lbl == label)
    check_threshold_full_table_case(
        tmp_path, label, threshold_cube_text(offset, shaper), spec, want_offset, want_bgr, want_or_u8, want_py_u8, want_rs_u8, _python_target
    )


def test_blas_gamma24_and_threshold_points(tmp_path: Path) -> None:
    """PORT-WAVE-1 platform BLAS rule: gamma24 [141, 136, 0] float64 and threshold.cube CPU u8 points."""
    require_cli()
    check_blas_gamma24_and_threshold_points(tmp_path, _python_target)


def test_threshold_cube_full_256_gpu_table_and_blas_point(tmp_path: Path) -> None:
    """Full 256^3 GPU table for threshold.cube: exact vs oracle; sole Accelerate diff at byte 139828."""
    require_cli()
    check_threshold_cube_full_256_gpu_table_and_blas_point(tmp_path, _python_target)


def test_apply_error_paths(tmp_path: Path) -> None:
    require_cli()
    cube_path = _write_cube(tmp_path, "id.cube", _cube_text(size=2))
    rgb_npy = tmp_path / "rgb.npy"
    np.save(rgb_npy, np.full((2, 2, 3), 128, dtype=np.uint8))

    # Log without xyz
    sp_log = json.dumps({"input_space": "llog", "output_space": "srgb"})
    code, payload = run_json(
        ["lut-apply", "--cube", str(cube_path), "--space", sp_log, "--rgb", str(rgb_npy), "--out", str(tmp_path / "o.npy")]
    )
    err = error_of(code, payload)
    assert err is not None and "Log LUT 需要 DNG 的场景 XYZ" in err.message

    # Target validation errors (bad_space, bad_gamut, bad_range)
    for bad_spec, expected_kind in (
        ({"input_space": "bogus"}, "bad_space"),
        ({"input_space": "srgb", "output_space": "bogus"}, "bad_space"),
        ({"input_space": "srgb", "input_gamut": "bogus"}, "bad_gamut"),
        ({"input_space": "srgb", "input_range": "weird"}, "bad_range"),
        ({}, "lut_space_required"),
    ):
        with pytest.raises((LutError, KeyError)):
            if "input_space" in bad_spec:
                _python_target(Cube.read(cube_path), **bad_spec)  # type: ignore[arg-type]
            else:
                raise KeyError("lut_space_required")
        args_bad = [
            "lut-apply",
            "--cube",
            str(cube_path),
            "--space",
            json.dumps(bad_spec),
            "--rgb",
            str(rgb_npy),
            "--out",
            str(tmp_path / "o2.npy"),
        ]
        code, payload = run_json(args_bad)
        err = error_of(code, payload)
        assert err is not None and err.kind == expected_kind

    # Non-finite output inside _apply -> lut_nonfinite (1000.0 overflows flog decode to inf, then inv(M709) -> NaN)
    extreme_cube = _write_cube(tmp_path, "extreme.cube", _cube_text(lambda x: np.full_like(x, 1000.0), size=2))
    target = _python_target(Cube.read(extreme_cube), "srgb", "flog")
    with pytest.raises(EditError) as edit_exc:
        _apply(target, np.full((2, 2, 3), 128, dtype=np.uint8))
    assert edit_exc.value.code == "lut_nonfinite"
    sp_ext = json.dumps({"input_space": "srgb", "output_space": "flog"})
    code, payload = run_json(
        ["lut-apply", "--cube", str(extreme_cube), "--space", sp_ext, "--rgb", str(rgb_npy), "--out", str(tmp_path / "o3.npy"), "--u8"]
    )
    err = error_of(code, payload)
    assert err is not None and err.kind == "lut_nonfinite"

    # Finite 1D table with slope overflow -> Inf (not endpoint fallback) -> lut_nonfinite
    steep_cube = _write_cube(tmp_path, "finite-steep.cube", "LUT_1D_SIZE 3\n0 0 0\n1e308 1e308 1e308\n1e308 1e308 1e308\n")
    px64 = tmp_path / "px64.npy"
    px64_arr = np.full((1, 1, 3), 64, dtype=np.uint8)
    np.save(px64, px64_arr)
    steep_py = Cube.read(steep_cube)
    with pytest.raises(EditError) as steep_exc:
        _apply(_python_target(steep_py, "srgb"), px64_arr)
    with pytest.raises(EditError) as seq_exc:
        seq_apply(SeqTarget(steep_py, "srgb"), px64_arr)
    assert steep_exc.value.code == "lut_nonfinite"
    assert (seq_exc.value.code, seq_exc.value.message) == (steep_exc.value.code, steep_exc.value.message)
    code, payload = run_json(
        [
            "lut-apply",
            "--cube",
            str(steep_cube),
            "--space",
            '{"input_space":"srgb"}',
            "--rgb",
            str(px64),
            "--out",
            str(tmp_path / "o4.npy"),
            "--u8",
        ]
    )
    err = error_of(code, payload)
    assert err is not None and (err.kind, err.message) == (steep_exc.value.code, steep_exc.value.message)


# ---- Unit B part 2: gpu/tables.py display_lut_u8 and lut_bytes ----


def test_gpu_display_table_with_shaper_matches_python_bytes(tmp_path: Path) -> None:
    require_cli()
    v = np.linspace(0, 1, 17)
    nodes = np.clip(np.array([[r, g, b] for b in v for g in v for r in v]) * [0.86, 0.97, 1.05], 0, 1)
    shaped = "LUT_1D_SIZE 3\nLUT_3D_SIZE 17\n0 0 0\n0.6 0.55 0.5\n1 1 1\n"
    cube_path = _write_cube(tmp_path, "shaper17.cube", shaped + "\n".join(" ".join(f"{c:.17g}" for c in row) for row in nodes) + "\n")
    py_cube = Cube.read(cube_path)
    spec = {"input_space": "srgb", "output_space": "vlog"}
    py_bytes, py_meta, py_reason = lut_bytes(_python_target(py_cube, **spec))
    assert py_reason is None and py_bytes is not None and py_meta is not None

    out_bin, out_meta = tmp_path / "table.bin", tmp_path / "meta.json"
    code, payload = run_json(
        ["lut-table", "--cube", str(cube_path), "--space", json.dumps(spec), "--out", str(out_bin), "--meta", str(out_meta)]
    )
    assert error_of(code, payload) is None
    rs_meta = json.loads(out_meta.read_text(encoding="utf-8"))
    assert rs_meta == py_meta
    rs_bytes = out_bin.read_bytes()

    _run_rs_px = make_rust_px_runner(tmp_path, cube_path, spec, "sh_px")
    or_target = SeqTarget(py_cube, **spec)
    or_bytes = seq_display_lut_u8(or_target)
    assert (
        assert_table_u8_boundary_gate(or_bytes, rs_bytes, ref_render_px=or_target.render, rust_render_px=_run_rs_px, label="shaper17-or") == 0
    )
    assert (
        assert_table_u8_gate_b(
            py_bytes,
            rs_bytes,
            or_bytes,
            or_render_px=or_target.render,
            rust_render_px=_run_rs_px,
            label="shaper17-py",
            py_render_px=_python_target(py_cube, **spec).render,
        )
        == 0
    )


@pytest.mark.parametrize(
    ("input_space", "output_space", "input_range", "output_range"),
    [
        ("llog", "gamma24", "full", "full"),
        ("llog709", "srgb", "legal", "full"),
        ("slog3", "rec709", "full", "legal"),
        ("slog3-sgamut3", "linear", "legal", "legal"),
        ("flog", "srgb", "full", "full"),
        ("flog2", "rec709", "full", "full"),
        ("vlog", "srgb", "full", "full"),
        ("applelog", "srgb", "full", "full"),
        ("linear", "srgb", "full", "full"),
    ],
)
def test_gpu_log_lut_bytes_and_meta(
    tmp_path: Path,
    input_space: str,
    output_space: str,
    input_range: str,
    output_range: str,
) -> None:
    require_cli()
    cube_path = _write_cube(
        tmp_path,
        "log.cube",
        _cube_text(lambda x: x * [0.95, 1.0, 1.05], size=5, extra_headers="LUT_3D_INPUT_RANGE -0.05 1.1\n"),
    )
    py_cube = Cube.read(cube_path)
    spec = {
        "input_space": input_space,
        "output_space": output_space,
        "input_range": input_range,
        "output_range": output_range,
    }
    py_bytes, py_meta, py_reason = lut_bytes(_python_target(py_cube, **spec))
    assert py_reason is None and py_bytes is not None and py_meta is not None

    out_bin = tmp_path / "log.bin"
    out_meta = tmp_path / "log.meta.json"
    code, payload = run_json(
        [
            "lut-table",
            "--cube",
            str(cube_path),
            "--space",
            json.dumps(spec),
            "--out",
            str(out_bin),
            "--meta",
            str(out_meta),
        ]
    )
    assert error_of(code, payload) is None
    rs_meta = json.loads(out_meta.read_text(encoding="utf-8"))
    _assert_meta_same(py_meta, rs_meta)

    py_f32 = np.frombuffer(py_bytes, "<f4")
    rs_f32 = np.frombuffer(out_bin.read_bytes(), "<f4")
    assert py_f32.dtype == np.float32 and rs_f32.dtype == np.float32
    assert np.array_equal(py_f32, rs_f32, equal_nan=True)


def test_gpu_lut_bytes_refusals(tmp_path: Path) -> None:
    require_cli()
    cube_1d3d = _write_cube(
        tmp_path,
        "shaper.cube",
        "LUT_1D_SIZE 2\nLUT_3D_SIZE 2\n0 0 0\n1 1 1\n" + "\n".join(f"{r} {g} {b}" for b in (0, 1) for g in (0, 1) for r in (0, 1)) + "\n",
    )
    cube_1d_only = _write_cube(tmp_path, "only1d.cube", "LUT_1D_SIZE 2\n0 0 0\n1 1 1\n")
    cube_3d = _write_cube(tmp_path, "plain3d.cube", _cube_text(size=3))

    for cube_path, spec, want_reason in (
        (cube_1d3d, {"input_space": "llog", "output_space": "srgb"}, "lut_1d_shaper"),
        (cube_1d_only, {"input_space": "llog", "output_space": "srgb"}, "lut_1d_shaper"),
        (cube_3d, {"input_space": "llog", "output_space": "vlog"}, "lut_output_space"),
    ):
        py_bytes, py_meta, py_reason = lut_bytes(_python_target(Cube.read(cube_path), **spec))
        assert py_bytes is None and py_meta is None and py_reason == want_reason
        out_bin = tmp_path / "r.bin"
        out_meta = tmp_path / "r.meta.json"
        code, payload = run_json(
            ["lut-table", "--cube", str(cube_path), "--space", json.dumps(spec), "--out", str(out_bin), "--meta", str(out_meta)]
        )
        assert error_of(code, payload) is None
        assert payload["reason"] == want_reason
        assert json.loads(out_meta.read_text(encoding="utf-8"))["reason"] == want_reason


# ---- Unit B part 3: identify & _undeclared_choice ----


@pytest.mark.parametrize(("comments", "title", "filename", "fun"), IDENTIFY_CASES)
def test_identify_and_undeclared_choice_parity(tmp_path: Path, comments: str, title: str, filename: str, fun: Any) -> None:
    require_cli()
    cube_path = _write_cube(tmp_path, "case.cube", _cube_text(fun, size=5, title=title, comments=comments))
    py_cube = Cube.read(cube_path)
    py_ident = identify(py_cube, filename)
    py_choice = input_choice({**py_ident, "filename": filename, "title": py_cube.title, "comments": list(py_cube.comments)})
    expected = {**py_ident, **py_choice}

    code, got = run_json(["lut-identify", "--cube", str(cube_path), "--name", filename])
    assert error_of(code, got) is None
    assert set(got) == set(expected)
    for key, val in expected.items():
        if key == "space_evidence":
            assert set(got[key]) == set(val)
            for ek, ev in val.items():
                if isinstance(ev, float):
                    _assert_rel(np.asarray([ev], np.float64), np.asarray([got[key][ek]], np.float64), label=f"space_evidence.{ek}")
                else:
                    assert got[key][ek] == ev, f"space_evidence.{ek}: {got[key][ek]!r} != {ev!r}"
        else:
            assert got[key] == val, f"{key}: {got[key]!r} != {val!r}"


# ---- Unit C: scene-xyz ----


def test_scene_xyz_synthetic_and_errors(tmp_path: Path) -> None:
    require_cli()
    image = np.full((4, 4), 1000, dtype=np.uint16)
    codes = np.array([[100.0, 2000.0, 500.0], [0.0, 0.0, 0.0], [4095.0, 4095.0, 4095.0]], dtype=np.float64)

    for name, kwargs in (
        ("valid", {}),
        ("no-neutral", {"neutral": None}),
        ("zero-neutral", {"neutral": (0.0, 1.0, 0.5)}),
        ("no-matrix", {"color_matrix": None}),
        ("nan-matrix", {"nan_in_matrix": True}),
    ):
        dng_path = tmp_path / f"{name}.dng"
        dng_path.write_bytes(write_dng(image, **kwargs))
        _check_scene_xyz(dng_path, codes, tmp_path / f"{name}-xyz.npy")

    # bad_colour: overflow to Inf when codes are 1e308
    bad_codes = np.array([[1e308, 1e308, 1e308]], dtype=np.float64)
    _check_scene_xyz(tmp_path / "valid.dng", bad_codes, tmp_path / "bad-colour.npy")


@pytest.mark.parametrize("path", REAL, ids=_real_id if REAL else None)
def test_scene_xyz_real_dngs(path: Path, tmp_path: Path) -> None:
    require_cli()
    codes = np.array([[100.0, 2000.0, 500.0], [420.0, 420.0, 420.0], [4095.0, 4095.0, 4095.0]], dtype=np.float64)
    _check_scene_xyz(path, codes, tmp_path / "real-xyz.npy")


# ---- WASM matches native ----


@pytest.mark.parametrize("label", (None, "case-0-1", "case-1-0", "amplified-25", "shaper-applelog"))
def test_wasm_matches_native_lut(tmp_path: Path, label: str | None) -> None:
    require_cli()
    inputs = lut_cases.write_wasm_lut_manifests(tmp_path) if label is None else lut_cases.write_wasm_threshold_table_manifest(tmp_path, label)
    proc = run_area("lut", inputs, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_float_gate_handles_nan_and_inf() -> None:
    _assert_rel(np.array([np.nan, np.inf, -np.inf, 1.0]), np.array([np.nan, np.inf, -np.inf, 1.0 + 1e-13]))
    for a, b in (([np.nan], [0.0]), ([0.0], [np.nan]), ([np.nan], [np.inf]), ([np.inf], [1.0]), ([1.0], [np.inf]), ([np.inf], [-np.inf])):
        with pytest.raises(pytest.fail.Exception):
            _assert_rel(np.array(a, dtype=np.float64), np.array(b, dtype=np.float64))


def test_json_nesting_limit(tmp_path: Path) -> None:
    require_cli()
    cube_path = _write_cube(tmp_path, "id.cube", _cube_text(size=2))
    for kind in ("array", "object"):
        for depth, ok in ((128, True), (129, False), (20_000, False)):
            sp = tmp_path / f"{kind}-{depth}.json"
            sp.write_text(space_depth_text(kind, depth), encoding="utf-8")
            code, payload = run_json(
                [
                    "lut-table",
                    "--cube",
                    str(cube_path),
                    "--space",
                    str(sp),
                    "--out",
                    str(tmp_path / "t.bin"),
                    "--meta",
                    str(tmp_path / "m.json"),
                ]
            )
            err = error_of(code, payload)
            if ok:
                assert err is None, (kind, depth, payload)
            else:
                assert err is not None and (err.kind, err.message) == ("json", "JSON 嵌套太深"), (kind, depth, err)
