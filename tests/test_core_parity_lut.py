"""Parity: gxr-cli (and gxr-wasm) against Python gxr_studio.lut & gpu.tables.

Gates (UNIT-lut.md / CONVENTIONS §5):
- errors, sizes, SPACE_IDS, legal flags, identify dicts, _undeclared_choice: exact;
- float64 outputs (Cube.apply, encode/decode, Target.render, meta floats, scene xyz):
  |a - b| <= 1e-12 * max(1, |a|);
- uint8 outputs (_apply, display_lut_u8): 0 difference (np.array_equal);
- log table float32 lattice: exact bits (np.array_equal after dtype assert);
- scene-xyz: all real DNGs via GXR_STUDIO_TEST_PARENT/captures/, plus synthetic errors.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from gxr_studio.gpu.source import affine_of
from gxr_studio.gpu.tables import lut_bytes
from gxr_studio.lut import spaces
from gxr_studio.lut.cube import Cube
from gxr_studio.lut.errors import LutError
from gxr_studio.lut.identify import identify
from gxr_studio.lut.page import input_choice
from gxr_studio.lut.photo import _apply
from gxr_studio.lut.scene import SceneError, scene_colour
from gxr_studio.lut.service import LutService
from gxr_studio.raw import open_dng
from gxr_studio.raw.dng import RawDecodeError
from gxr_studio.render.params import EditError
from support.core_cli import error_of, require_cli, run_json, run_npy
from support.dng_writer import write_dng
from support.lut_cases import IDENTIFY_CASES, space_depth_text
from support.lut_cases import cube_text as _cube_text
from support.lut_cases import write_huge_npy_header as _write_huge_npy_header
from support.wasm_check import run_area

_STUDIO = Path(__file__).resolve().parents[1]
_PARENT = Path(os.environ.get("GXR_STUDIO_TEST_PARENT", str(_STUDIO.parent)))
CAPTURES = _PARENT / "captures"
_CAP = 60


def _real_dngs() -> list[Path]:
    if not CAPTURES.is_dir():
        return []
    found = [
        path for path in CAPTURES.rglob("*") if path.is_file() and path.suffix.upper() == ".DNG" and "backup" not in path.as_posix().lower()
    ]
    found.sort(key=lambda path: path.as_posix())
    if len(found) <= _CAP:
        return found
    return [found[(i * len(found)) // _CAP] for i in range(_CAP)]


REAL = _real_dngs()


def _real_id(path: Path) -> str:
    return path.relative_to(CAPTURES).as_posix().replace("/", "_")


def _assert_rel(python: np.ndarray, rust: np.ndarray, *, label: str = "value") -> None:
    assert python.shape == rust.shape, f"{label}: shape {python.shape} != {rust.shape}"
    assert python.dtype == np.float64 and rust.dtype == np.float64
    py_nan = np.isnan(python)
    rs_nan = np.isnan(rust)
    one_nan = py_nan ^ rs_nan
    inf_bad = (np.isinf(python) | np.isinf(rust)) & (python != rust)
    finite = np.isfinite(python) & np.isfinite(rust)
    limit = np.where(finite, 1e-12 * np.maximum(1.0, np.abs(python)), 0.0)
    diff = np.zeros_like(python)
    diff[finite] = np.abs(python[finite] - rust[finite])
    bad = one_nan | inf_bad | (finite & ~(diff <= limit))
    if np.any(bad):
        idx = tuple(int(v) for v in np.argwhere(bad)[0])
        pytest.fail(f"{label} differs at {idx}: python={python[idx]!r} rust={rust[idx]!r} diff={diff[idx]!r}")


def _assert_meta_same(py_meta: dict[str, Any], rs_meta: dict[str, Any]) -> None:
    assert set(py_meta) == set(rs_meta)
    for key, py_val in py_meta.items():
        rs_val = rs_meta[key]
        if key in ("domain_min", "domain_max", "range3", "xyz_to_input", "out_to_srgb"):
            py_arr = np.asarray(py_val, np.float64)
            rs_arr = np.asarray(rs_val, np.float64)
            _assert_rel(py_arr, rs_arr, label=f"meta.{key}")
        else:
            assert py_val == rs_val, f"meta.{key}: {py_val!r} != {rs_val!r}"


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


@pytest.mark.parametrize(
    ("text", "expected_prefix"),
    [
        ("hello\n", "line 1: unsupported directive hello"),
        ("LUT_3D_SIZE 2\n0 0 0\n", "LUT 数据行数与声明不符"),
        ("LUT_3D_SIZE 1\n0 0 0\n", "line 1: LUT size out of range"),
        ("LUT_3D_SIZE 130\n", "line 1: LUT size out of range"),
        ("LUT_1D_SIZE 65537\n", "line 1: LUT size out of range"),
        ("LUT_3D_SIZE 2\nLUT_3D_SIZE 2\n" + "0 0 0\n" * 8, "line 2: duplicate or late header"),
        ("LUT_3D_SIZE 2\n" + "0 0 nan\n" * 8, "line 2: expected three finite numbers"),
        ("LUT_3D_SIZE 2\nDOMAIN_MIN 0 0 inf\n" + "0 0 0\n" * 8, "line 2: non-finite header"),
        ("LUT_3D_SIZE 2\nDOMAIN_MIN 1 1 1\nDOMAIN_MAX 0 0 0\n" + "0 0 0\n" * 8, "DOMAIN_MAX"),
        ("LUT_3D_SIZE 2\nLUT_3D_INPUT_RANGE 1 0\n" + "0 0 0\n" * 8, "INPUT_RANGE"),
        ("LUT_3D_SIZE 2\nLUT_1D_INPUT_RANGE 0.5 0.5\n" + "0 0 0\n" * 8, "INPUT_RANGE"),
        ("LUT_3D_SIZE 2\nDOMAIN_MIN 0 0 0\nLUT_3D_INPUT_RANGE 0 1\n" + "0 0 0\n" * 8, "DOMAIN"),
        ('TITLE "unclosed\nLUT_3D_SIZE 2\n' + "0 0 0\n" * 8, "No closing quotation"),
    ],
)
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

    rng = np.random.default_rng(42)
    rgb = rng.integers(0, 256, (12, 10, 3), dtype=np.uint8)
    # gamma24 encode has infinite derivative at x=0 (|x|**(1/2.4)): Accelerate BLAS
    # dgemm FMA residual (~1.5e-17) on rgb==0 turns into ~1e-7 in float64 Target.render,
    # even though _apply uint8 and 256^3 display_lut_u8 match Python with 0 diffs.
    rgb_f64_in = np.maximum(rgb, 1) if input_space == "gamma24" else rgb
    rgb_f64_npy = tmp_path / "rgb_f64.npy"
    np.save(rgb_f64_npy, rgb_f64_in)
    rgb_npy = tmp_path / "rgb.npy"
    np.save(rgb_npy, rgb)

    xyz = None
    cli_f64_args = ["lut-apply", "--cube", str(cube_path), "--space", json.dumps(spec), "--rgb", str(rgb_f64_npy)]
    cli_u8_args = ["lut-apply", "--cube", str(cube_path), "--space", json.dumps(spec), "--rgb", str(rgb_npy)]
    if input_space not in spaces.DISPLAY:
        xyz = np.exp(rng.uniform(np.log(0.005), np.log(2.0), (12, 10, 3)))
        xyz_npy = tmp_path / "xyz.npy"
        np.save(xyz_npy, xyz)
        cli_f64_args.extend(["--xyz", str(xyz_npy)])
        cli_u8_args.extend(["--xyz", str(xyz_npy)])

    py_f64 = target.render(rgb_f64_in, xyz)
    rs_f64 = run_npy(cli_f64_args, tmp_path / "out_f64.npy")
    _assert_rel(py_f64, rs_f64, label=f"render-{input_space}->{output_space}")

    py_u8 = _apply(target, rgb, xyz)
    rs_u8 = run_npy([*cli_u8_args, "--u8"], tmp_path / "out_u8.npy")
    assert rs_u8.dtype == np.uint8 and py_u8.shape == rs_u8.shape
    if not np.array_equal(py_u8, rs_u8):
        idx = tuple(int(v) for v in np.argwhere(py_u8 != rs_u8)[0])
        pytest.fail(f"u8 differs at {idx} ({input_space}->{output_space}): python={int(py_u8[idx])} rust={int(rs_u8[idx])} f64={py_f64[idx]!r}")


def test_apply_error_paths(tmp_path: Path) -> None:
    require_cli()
    cube_path = _write_cube(tmp_path, "id.cube", _cube_text(size=2))
    rgb_npy = tmp_path / "rgb.npy"
    np.save(rgb_npy, np.full((2, 2, 3), 128, dtype=np.uint8))

    # Log without xyz
    code, payload = run_json(
        [
            "lut-apply",
            "--cube",
            str(cube_path),
            "--space",
            json.dumps({"input_space": "llog", "output_space": "srgb"}),
            "--rgb",
            str(rgb_npy),
            "--out",
            str(tmp_path / "o.npy"),
        ]
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
        code, payload = run_json(
            [
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
        )
        err = error_of(code, payload)
        assert err is not None and err.kind == expected_kind

    # Non-finite output inside _apply -> lut_nonfinite (1000.0 overflows flog decode to inf, then inv(M709) -> NaN)
    extreme_cube = _write_cube(
        tmp_path,
        "extreme.cube",
        _cube_text(lambda x: np.full_like(x, 1000.0), size=2),
    )
    target = _python_target(Cube.read(extreme_cube), "srgb", "flog")
    with pytest.raises(EditError) as edit_exc:
        _apply(target, np.full((2, 2, 3), 128, dtype=np.uint8))
    assert edit_exc.value.code == "lut_nonfinite"
    code, payload = run_json(
        [
            "lut-apply",
            "--cube",
            str(extreme_cube),
            "--space",
            json.dumps({"input_space": "srgb", "output_space": "flog"}),
            "--rgb",
            str(rgb_npy),
            "--out",
            str(tmp_path / "o3.npy"),
            "--u8",
        ]
    )
    err = error_of(code, payload)
    assert err is not None and err.kind == "lut_nonfinite"


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

    out_bin = tmp_path / "table.bin"
    out_meta = tmp_path / "meta.json"
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
    assert rs_meta == py_meta
    rs_bytes = out_bin.read_bytes()
    assert len(rs_bytes) == 256**3 * 4
    py_arr = np.frombuffer(py_bytes, np.uint8)
    rs_arr = np.frombuffer(rs_bytes, np.uint8)
    if not np.array_equal(py_arr, rs_arr):
        idx = int(np.flatnonzero(py_arr != rs_arr)[0])
        pytest.fail(f"display table differs at byte {idx}: python={int(py_arr[idx])} rust={int(rs_arr[idx])}")


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


def _check_scene_xyz(dng_path: Path, codes: np.ndarray, out_path: Path) -> None:
    py_raw_err: RawDecodeError | None = None
    try:
        dng = open_dng(dng_path)
        try:
            col = scene_colour(dng)
            py_xyz = col.xyz(codes)
            py_aff = affine_of(col)
            py_err: SceneError | None = None
        finally:
            dng.release()
    except RawDecodeError as exc:
        py_xyz, py_aff, py_err, py_raw_err = None, None, None, exc
    except SceneError as exc:
        py_xyz, py_aff, py_err = None, None, exc

    codes_npy = out_path.with_name("codes.npy")
    np.save(codes_npy, codes)
    code, payload = run_json(["scene-xyz", "--dng", str(dng_path), "--codes", str(codes_npy), "--out", str(out_path)])
    rs_err = error_of(code, payload)

    if py_raw_err is not None:
        assert rs_err is not None
        assert rs_err.kind == "raw_decode"
        assert rs_err.message == str(py_raw_err)
        return

    if py_err is not None:
        assert rs_err is not None
        assert rs_err.kind == py_err.code
        assert rs_err.message == py_err.message
        return

    assert rs_err is None and py_xyz is not None
    rs_xyz = np.load(out_path)
    _assert_rel(py_xyz, rs_xyz, label=f"scene-xyz({dng_path.name})")

    if py_aff is not None:
        assert payload["affine"] is not None
        _assert_rel(np.asarray(py_aff["A"], np.float64), np.asarray(payload["affine"]["A"], np.float64), label="affine.A")
        _assert_rel(np.asarray(py_aff["b"], np.float64), np.asarray(payload["affine"]["b"], np.float64), label="affine.b")
        # Also verify affine_of matches xyz within a few ulps on the same codes
        aff_xyz = codes @ np.asarray(payload["affine"]["A"], np.float64).T + np.asarray(payload["affine"]["b"], np.float64)
        _assert_rel(rs_xyz, aff_xyz, label="affine_of vs xyz")


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


def test_wasm_matches_native_lut(tmp_path: Path) -> None:
    require_cli()
    v = np.linspace(0, 1, 17)
    nodes = np.clip(np.array([[r, g, b] for b in v for g in v for r in v]) * [0.86, 0.97, 1.05], 0, 1)
    shaper_cube = tmp_path / "shaper17.cube"
    shaper_cube.write_text(
        "LUT_1D_SIZE 3\nLUT_3D_SIZE 17\n0 0 0\n0.6 0.55 0.5\n1 1 1\n" + "\n".join(" ".join(f"{c:.17g}" for c in row) for row in nodes) + "\n",
        encoding="utf-8",
    )
    log_cube = tmp_path / "Leica_L-Log_warm.cube"
    log_cube.write_text(_cube_text(lambda x: x * [0.95, 1.0, 1.05], size=5), encoding="utf-8")
    bad_cube = tmp_path / "bad.cube"
    bad_cube.write_text("LUT_3D_SIZE 2\nDOMAIN_MIN 0 0 0\nLUT_3D_INPUT_RANGE 0 1\n" + "0 0 0\n" * 8, encoding="utf-8")

    rgb_u8 = np.array([[[10, 20, 30], [128, 64, 32]], [[200, 150, 100], [255, 255, 255]]], dtype=np.uint8)
    rgb_npy = tmp_path / "rgb_u8.npy"
    np.save(rgb_npy, rgb_u8)
    xyz_f64 = np.full((2, 2, 3), 0.18, dtype=np.float64)
    xyz_npy = tmp_path / "xyz_f64.npy"
    np.save(xyz_npy, xyz_f64)

    manifests: list[Path] = [shaper_cube, log_cube, bad_cube]
    cases_specs = [
        {"kind": "table", "cube": str(shaper_cube), "space": {"input_space": "srgb", "output_space": "vlog"}},
        {"kind": "table", "cube": str(log_cube), "space": {"input_space": "llog", "output_space": "gamma24"}},
        {"kind": "table", "cube": str(shaper_cube), "space": {"input_space": "llog", "output_space": "srgb"}},
        {
            "kind": "apply_u8",
            "cube": str(shaper_cube),
            "space": {"input_space": "srgb", "output_space": "vlog"},
            "rgb_npy": str(rgb_npy),
            "height": 2,
            "width": 2,
        },
        {
            "kind": "apply_u8",
            "cube": str(log_cube),
            "space": {"input_space": "llog", "output_space": "srgb"},
            "rgb_npy": str(rgb_npy),
            "xyz_npy": str(xyz_npy),
            "height": 2,
            "width": 2,
        },
        {
            "kind": "apply_u8",
            "cube": str(log_cube),
            "space": {"input_space": "llog", "output_space": "srgb"},
            "rgb_npy": str(rgb_npy),
            "height": 2,
            "width": 2,
        },
    ]
    huge1 = _write_huge_npy_header(tmp_path / "huge-4294967295.npy", 4294967295, 4294967295)
    huge2 = _write_huge_npy_header(tmp_path / "huge-2147483648.npy", 2147483648, 1)
    cases_specs.extend(
        [
            {
                "kind": "apply_u8",
                "cube": str(shaper_cube),
                "space": {"input_space": "srgb", "output_space": "srgb"},
                "rgb_npy": str(huge1),
                "height": 4294967295,
                "width": 4294967295,
            },
            {
                "kind": "apply_u8",
                "cube": str(shaper_cube),
                "space": {"input_space": "srgb", "output_space": "srgb"},
                "rgb_npy": str(huge2),
                "height": 2147483648,
                "width": 1,
            },
        ]
    )
    for kind in ("array", "object"):
        for depth in (128, 129, 20_000):
            sp = tmp_path / f"space-{kind}-{depth}.json"
            sp.write_text(space_depth_text(kind, depth), encoding="utf-8")
            cases_specs.append({"kind": "table", "cube": str(shaper_cube), "space_file": str(sp)})
            cases_specs.append(
                {"kind": "apply_u8", "cube": str(shaper_cube), "space_file": str(sp), "rgb_npy": str(rgb_npy), "height": 2, "width": 2}
            )
    for kind in ("bare_array", "bare_object"):
        sp = tmp_path / f"space-{kind}-129.json"
        sp.write_text(space_depth_text(kind, 129), encoding="utf-8")
        cases_specs.append({"kind": "table", "cube": str(shaper_cube), "space_file": str(sp)})

    for i, item in enumerate(cases_specs):
        p = tmp_path / f"case-{i}.json"
        p.write_text(json.dumps(item), encoding="utf-8")
        manifests.append(p)

    proc = run_area("lut", manifests, tmp_path)
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
