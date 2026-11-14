"""Synthetic identify / _undeclared_choice test cases shared by LUT parity tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

IDENTIFY_CASES: list[tuple[str, str, str, Any]] = [
    ("#LUMIXPHOTOSTYLE VLOG\n", "FLog2C_to_Style", "FLog2C_to_Style_VLog.cube", lambda x: x),
    ("# Input: Panasonic V-Log / V-Gamut\n", "L-Log_to_Style", "L-Log_to_Style_VLog.cube", lambda x: x),
    ("# INPUT_COLORSPACE V-Log\n", "S-Log3_style", "S-Log3_style_VLog.cube", lambda x: x),
    ("# Input Space = sRGB\n# Output: Rec709\n", "V-Log style", "L-Log.cube", lambda x: 1 - x),
    ("# input_colour_space: S-Log3\n", "FLog2C style", "vlog.cube", lambda x: 1 - x),
    ("# Input: linear\n", "synthetic", "FLog2C.cube", lambda x: 1 - x),
    ("", "V-Log to sRGB", "FLog2C_Style.cube", lambda x: 1 - x),
    ("", "sRGB to V-Log", "vlog_style.cube", lambda x: 1 - x),
    ("", "synthetic", "FLog2C_to_Style_VLog.cube", lambda x: 1 - x),
    ("", "synthetic", "Leica_L-Log_warm.cube", lambda x: 1 - x),
    ("", "Input V-Log / V-Gamut; Target FLog2C", "film.cube", lambda x: 1 - x),
    ("", "synthetic", "input_srgb_target_FLog2C.cube", lambda x: 1 - x),
    ("# Input: V-Log; Output: sRGB\n", "FLog2C style", "film.cube", lambda x: 1 - x),
    ("", "L-Log_to_style", "input_vlog.cube", lambda x: 1 - x),
    ("", "L-Log_to_style", "FLog2C_to_Style_VLog.cube", lambda x: 1 - x),
    ("# Output: BT.709\n", "synthetic", "film.cube", lambda x: 1 - x),
    ("# OUTPUT_COLORSPACE Rec.709 primaries / D65 / BT.709 OETF\n", "synthetic", "film.cube", lambda x: 1 - x),
    ("# OUTPUT_COLORSPACE Rec.709 primaries / D65 / Gamma 2.4\n", "synthetic", "film.cube", lambda x: 1 - x),
    ("# Output transfer function: BT1886\n", "synthetic", "film_sRGB.cube", lambda x: 1 - x),
    ("# Output: sRGB\n", "synthetic", "film_Rec709.cube", lambda x: 1 - x),
    ("# Output gamma: 2.4\n", "synthetic", "film.cube", lambda x: 1 - x),
    ("# Gamma 2.4\n", "synthetic", "film.cube", lambda x: 1 - x),
    ("", "synthetic", "Film_Rec709_BT1886_VLog.cube", lambda x: 1 - x),
    ("# Input: sRGB\n", "synthetic", "sRGB_to_VLog.cube", lambda x: 1 - x),
    ("# Created by Synthetic Author\n", "Synthetic creator", "Creative_1234.cube", lambda x: x),
    ("# Input: V-Log\n# Input: F-Log2\n", "synthetic", "vlog.cube", lambda x: x),
    ("# Input: unsupported-input\n", "synthetic", "vlog.cube", lambda x: x),
    ("#LUMIXPHOTOSTYLE VLOG\n# Input: unsupported\n", "synthetic", "vlog.cube", lambda x: x),
    ("", "input_vlog", "input_srgb.cube", lambda x: 1 - x),
    ("", "synthetic", "FLog2C.cube", lambda x: x),
    ("# Input: V-Log\n# Output: sRGB\n# Output: BT709\n", "synthetic", "film.cube", lambda x: x),
    ("", "film", "srgb.cube", lambda x: x),
    ("", "film", "vlog.cube", lambda x: np.clip(x * 1.6, 0, 1)),
    ("", "film", "plain.cube", lambda x: np.clip(x * 1.6, 0, 1)),
    ("", "film", "plain.cube", lambda x: 1 / (1 + np.exp(-14 * (x - 0.5)))),
    ("", "film", "plain.cube", lambda x: x**1.8),
    ("", "film", "plain.cube", lambda x: x[:, [1, 2, 0]]),
    ("# Creative look for V-Log\n", "film", "film.cube", lambda x: 1 - x),
    ("", "warm film", "warm_film.cube", lambda x: 1 - x),
    ("", "Analog Street", "Analog_Street.cube", lambda x: 1 - x),
    ("", "Catalog look", "catalog.cube", lambda x: 1 - x),
    ("# Output: Rec.709\n", "warm film", "warm_film.cube", lambda x: 1 - x),
    ("# sRGB look\n", "FLog2C style", "FLog2C.cube", lambda x: 1 - x),
    ("# Author: V-Log master\n", "film", "film.cube", lambda x: 1 - x),
    ("", "synthetic", "input_lİnear.cube", lambda x: 1 - x),
    ("# Output: Gamma 2.4\n", "synthetic", "input_lİnear.cube", lambda x: 1 - x),
    ("", "synthetic", "input_ſRGB.cube", lambda x: 1 - x),
    ("# inputtransferfunction: S-Log3\n", "synthetic", "input_ſRGB.cube", lambda x: 1 - x),
    ("", "synthetic", "ſRGB_to_VLog.cube", lambda x: 1 - x),
    ("", "synthetic", "ſlog_custom.cube", lambda x: 1 - x),
    ("", "synthetic", "xİlinear.cube", lambda x: 1 - x),
    ("", "synthetic", "linearK.cube", lambda x: 1 - x),
    ("", "synthetic", "input_vlog\nto_srgb.cube", lambda x: 1 - x),
    ("", "synthetic", "input_vlog\nsrgb.cube", lambda x: 1 - x),
    ("", "synthetic", "input_\nsrgb.cube", lambda x: 1 - x),
    ("", "synthetic", "input_\ninput_vlog.cube", lambda x: 1 - x),
]


def space_depth_text(kind: str, depth: int) -> str:
    """Build a RenderSpec JSON with `extra` nested to `depth` levels (root object is level 1)."""
    if kind == "array":
        inner = "[" * (depth - 1) + "]" * (depth - 1)
    elif kind == "object":
        inner = '{"a":' * (depth - 1) + "null" + "}" * (depth - 1)
    elif kind == "bare_array":
        return "[" * depth + "]" * depth
    elif kind == "bare_object":
        return '{"a":' * depth + "null" + "}" * depth
    else:
        raise ValueError(kind)
    return '{"input_space":"srgb","extra":' + inner + "}"


def cube_text(fun: Any = lambda x: x, *, size: int = 17, title: str = "synthetic", comments: str = "", extra_headers: str = "") -> str:
    v = np.linspace(0, 1, size)
    b, g, r = np.meshgrid(v, v, v, indexing="ij")
    nodes = np.stack([r, g, b], axis=-1).reshape(-1, 3)
    rows = fun(nodes)
    title_line = f'TITLE "{title}"\n' if title is not None else ""
    return (
        comments + title_line + f"LUT_3D_SIZE {size}\n" + extra_headers + "\n".join(" ".join(f"{c:.17g}" for c in row) for row in rows) + "\n"
    )


def write_huge_npy_header(path: Path, height: int, width: int) -> Path:
    body = f"{{'descr': '|u1', 'fortran_order': False, 'shape': ({height}, {width}, 3), }}"
    pad = (64 - (10 + len(body) + 1) % 64) % 64
    header = body.encode("latin1") + b" " * pad + b"\n"
    path.write_bytes(b"\x93NUMPY\x01\x00" + len(header).to_bytes(2, "little") + header)
    return path


def write_wasm_lut_manifests(tmp_path: Path) -> list[Path]:
    """Build synthetic cubes, npy inputs, depth-limit specs, and JSON manifests for `wasm_cases/lut.mjs`."""
    import json

    v = np.linspace(0, 1, 17)
    nodes = np.clip(np.array([[r, g, b] for b in v for g in v for r in v]) * [0.86, 0.97, 1.05], 0, 1)
    shaper_cube = tmp_path / "shaper17.cube"
    shaper_cube.write_text(
        "LUT_1D_SIZE 3\nLUT_3D_SIZE 17\n0 0 0\n0.6 0.55 0.5\n1 1 1\n" + "\n".join(" ".join(f"{c:.17g}" for c in row) for row in nodes) + "\n",
        encoding="utf-8",
    )
    log_cube = tmp_path / "Leica_L-Log_warm.cube"
    log_cube.write_text(cube_text(lambda x: x * [0.95, 1.0, 1.05], size=5), encoding="utf-8")
    bad_cube = tmp_path / "bad.cube"
    bad_cube.write_text("LUT_3D_SIZE 2\nDOMAIN_MIN 0 0 0\nLUT_3D_INPUT_RANGE 0 1\n" + "0 0 0\n" * 8, encoding="utf-8")
    steep_cube = tmp_path / "finite-steep.cube"
    steep_cube.write_text("LUT_1D_SIZE 3\n0 0 0\n1e308 1e308 1e308\n1e308 1e308 1e308\n", encoding="utf-8")
    px64_npy = tmp_path / "px64_u8.npy"
    np.save(px64_npy, np.full((1, 1, 3), 64, dtype=np.uint8))

    rgb_u8 = np.array([[[10, 20, 30], [128, 64, 32]], [[200, 150, 100], [255, 255, 255]]], dtype=np.uint8)
    rgb_npy = tmp_path / "rgb_u8.npy"
    np.save(rgb_npy, rgb_u8)
    xyz_f64 = np.full((2, 2, 3), 0.18, dtype=np.float64)
    xyz_npy = tmp_path / "xyz_f64.npy"
    np.save(xyz_npy, xyz_f64)

    u_size = tmp_path / "underscore-size.cube"
    u_size.write_text("LUT_3D_SIZE 0_2\n" + "0 0 0\n" * 8, encoding="utf-8")
    u_float = tmp_path / "underscore-float.cube"
    u_float.write_text("LUT_3D_SIZE 2\n" + "0 0 0_0\n" * 8, encoding="utf-8")
    uni_size = tmp_path / "unicode-size.cube"
    uni_size.write_text("LUT_3D_SIZE ２\n" + "0 0 0\n" * 8, encoding="utf-8")
    huge64 = tmp_path / "huge64.cube"
    huge64.write_text("LUT_3D_SIZE 18446744073709551615\n", encoding="utf-8")

    inv2 = tmp_path / "inv2.cube"
    inv2.write_text(cube_text(lambda x: 1 - x, size=2, title="synthetic"), encoding="utf-8")
    manifests: list[Path] = [shaper_cube, log_cube, bad_cube, u_size, u_float, uni_size, huge64]
    cases_specs: list[dict[str, Any]] = [
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
        {"kind": "identify", "cube": str(inv2), "name": "input_lİnear.cube"},
        {"kind": "identify", "cube": str(inv2), "name": "input_ſRGB.cube"},
        {"kind": "identify", "cube": str(inv2), "name": "input_vlog\nto_srgb.cube"},
        {"kind": "identify", "cube": str(inv2), "name": "input_vlog\nsrgb.cube"},
        {
            "kind": "apply_u8",
            "cube": str(steep_cube),
            "space": {"input_space": "srgb"},
            "rgb_npy": str(px64_npy),
            "height": 1,
            "width": 1,
        },
    ]
    huge1 = write_huge_npy_header(tmp_path / "huge-4294967295.npy", 4294967295, 4294967295)
    huge2 = write_huge_npy_header(tmp_path / "huge-2147483648.npy", 2147483648, 1)
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
    return manifests


def assert_rel(python: np.ndarray, rust: np.ndarray, *, label: str = "value") -> None:
    import pytest

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


def assert_meta_same(py_meta: dict[str, Any], rs_meta: dict[str, Any]) -> None:
    assert set(py_meta) == set(rs_meta)
    for key, py_val in py_meta.items():
        rs_val = rs_meta[key]
        if key in ("domain_min", "domain_max", "range3", "xyz_to_input", "out_to_srgb"):
            assert_rel(np.asarray(py_val, np.float64), np.asarray(rs_val, np.float64), label=f"meta.{key}")
        else:
            assert py_val == rs_val, f"meta.{key}: {py_val!r} != {rs_val!r}"


def real_dngs(captures_dir: Path, cap: int = 60) -> list[Path]:
    if not captures_dir.is_dir():
        return []
    found = [
        path for path in captures_dir.rglob("*") if path.is_file() and path.suffix.upper() == ".DNG" and "backup" not in path.as_posix().lower()
    ]
    found.sort(key=lambda path: path.as_posix())
    if len(found) <= cap:
        return found
    return [found[(i * len(found)) // cap] for i in range(cap)]


def check_scene_xyz(dng_path: Path, codes: np.ndarray, out_path: Path) -> None:
    from gxr_studio.gpu.source import affine_of
    from gxr_studio.lut.scene import SceneError, scene_colour
    from gxr_studio.raw import open_dng
    from gxr_studio.raw.dng import RawDecodeError
    from support.core_cli import error_of, run_json

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
        assert rs_err is not None and rs_err.kind == "raw_decode" and rs_err.message == str(py_raw_err)
        return
    if py_err is not None:
        assert rs_err is not None and rs_err.kind == py_err.code and rs_err.message == py_err.message
        return

    assert rs_err is None and py_xyz is not None
    rs_xyz = np.load(out_path)
    assert_rel(py_xyz, rs_xyz, label=f"scene-xyz({dng_path.name})")

    if py_aff is not None:
        assert payload["affine"] is not None
        assert_rel(np.asarray(py_aff["A"], np.float64), np.asarray(payload["affine"]["A"], np.float64), label="affine.A")
        assert_rel(np.asarray(py_aff["b"], np.float64), np.asarray(payload["affine"]["b"], np.float64), label="affine.b")
        # Also verify affine_of matches xyz within 1e-12 relative (measured up to 44 ULP)
        aff_xyz = codes @ np.asarray(payload["affine"]["A"], np.float64).T + np.asarray(payload["affine"]["b"], np.float64)
        assert_rel(rs_xyz, aff_xyz, label="affine_of vs xyz")
