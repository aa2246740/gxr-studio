"""Native-vs-WASM parity for all scalar numeric parameters in wave-1 exports.

Every ``#[wasm_bindgen]`` export taking numeric inputs in ``core/crates/gxr-wasm/src/``
must accept JS ``number`` (``f64``) and validate finiteness, integer-ness, and range
before narrowing, returning the exact same success bytes or ``CoreError`` JSON as
``gxr-cli`` instead of silently wrapping modulo ``2**32``.
"""

from __future__ import annotations

import json
import struct
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

from support.core_cli import require_cli
from support.dng_writer import write_dng
from support.wasm_check import CLI, run_area

U32_WRAP = 2**32
EDGE_VALUES: tuple[int | float | str, ...] = (
    U32_WRAP + 1,
    -1,
    2**53,
    1.5,
    "NaN",
    "Infinity",
    "-Infinity",
)


def _is_valid_nonneg_int(v: int | float | str) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _write_fake_npy_header(path: Path, descr: str, shape: tuple[int, ...]) -> None:
    shape_str = ", ".join(str(d) for d in shape)
    if len(shape) == 1:
        shape_str += ","
    body = f"{{'descr': '{descr}', 'fortran_order': False, 'shape': ({shape_str}), }}".encode("latin-1")
    pad = (64 - (10 + len(body) + 1) % 64) % 64
    hdr = body + (b" " * pad) + b"\n"
    path.write_bytes(b"\x93NUMPY\x01\x00" + struct.pack("<H", len(hdr)) + hdr)


def _dng_4x4(tmp_path: Path, name: str = "synthetic.dng") -> Path:
    img = np.arange(16, dtype=np.uint16).reshape(4, 4) * 100 + 200
    p = tmp_path / name
    p.write_bytes(write_dng(img, black=(10.0, 10.0, 10.0, 10.0), black_repeat=(2, 2)))
    return p


def _param_values(normal: int) -> list[int | float | str]:
    return [normal, normal + U32_WRAP, normal - U32_WRAP, *EDGE_VALUES]


def test_wasm_numbers_raw_wb_block(tmp_path: Path) -> None:
    require_cli()
    dng = _dng_4x4(tmp_path)
    specs: list[Path] = []

    def add_case(tag: str, *, named: bool, y0: int | float | str, y1: int | float | str, cy0: int, cy1: int) -> None:
        p = tmp_path / f"raw-{len(specs)}-{tag}.json"
        p.write_text(
            json.dumps({"dng": str(dng), "name": dng.name, "named": named, "y0": y0, "y1": y1, "cliY0": cy0, "cliY1": cy1}),
            encoding="utf-8",
        )
        specs.append(p)

    for named in (False, True):
        kind = "named" if named else "anon"
        add_case(f"{kind}-normal", named=named, y0=0, y1=2, cy0=0, cy1=2)
        for v in _param_values(0)[1:]:
            cli_v = v if isinstance(v, int) else -1
            add_case(f"{kind}-y0", named=named, y0=v, y1=2, cy0=cli_v, cy1=2)
        for v in _param_values(2)[1:]:
            cli_v = v if isinstance(v, int) else -1
            add_case(f"{kind}-y1", named=named, y0=0, y1=v, cy0=0, cy1=cli_v)
        # Corrupt DNG error must still take precedence over out-of-range numeric rows.
        bad = tmp_path / f"corrupt-{kind}.dng"
        bad.write_bytes(b"NOT_A_DNG_FILE")
        p = tmp_path / f"raw-{len(specs)}-{kind}-corrupt.json"
        p.write_text(
            json.dumps({"dng": str(bad), "name": bad.name, "named": named, "y0": "NaN", "y1": 2, "cliY0": -1, "cliY1": 2}),
            encoding="utf-8",
        )
        specs.append(p)

    proc = run_area("raw", specs, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_wasm_numbers_geometry_final_box_and_warp(tmp_path: Path) -> None:
    require_cli()
    rgb_path = tmp_path / "rgb2x2.npy"
    np.save(rgb_path, np.full((2, 2, 3), 32, dtype=np.uint8))
    specs: list[Path] = []

    def add_geom(tag: str, payload: dict[str, Any]) -> None:
        p = tmp_path / f"geom-{len(specs)}-{tag}.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        specs.append(p)

    # Normal baseline for both exports
    add_geom("normal", {"w": 2, "h": 2, "geometry": {}, "crop": None, "rgbNpy": str(rgb_path), "overrideDims": True})

    # geom_final_box: CLI parses --w/--h as u64 > 0, so only >= 1 can express CoreError(bad_geometry: ARRAY_TOO_BIG).
    for field in ("w", "h"):
        for v in _param_values(2)[1:]:
            cli_v = v if (isinstance(v, int) and v >= 1) else U32_WRAP + 1
            add_geom(
                f"box-{field}",
                {
                    "w": v if field == "w" else 2,
                    "h": v if field == "h" else 2,
                    "cliW": cli_v if field == "w" else 2,
                    "cliH": cli_v if field == "h" else 2,
                    "geometry": {},
                    "crop": None,
                },
            )

    # geom_warp: >= 2**32 triggers ARRAY_TOO_BIG via CLI u64; <= 0 / non-finite / fractional triggers bad_geometry via mismatched dim.
    for field in ("w", "h"):
        for v in _param_values(2)[1:]:
            if isinstance(v, int) and v >= 1:
                cli_v = v
            else:
                cli_v = 1
            add_geom(
                f"warp-{field}",
                {
                    "w": v if field == "w" else 2,
                    "h": v if field == "h" else 2,
                    "cliW": cli_v if field == "w" else 2,
                    "cliH": cli_v if field == "h" else 2,
                    "geometry": {},
                    "crop": None,
                    "checkFinalBox": False,
                    "rgbNpy": str(rgb_path),
                    "overrideDims": True,
                    "expectError": True,
                },
            )

    proc = run_area("geometry", specs, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_wasm_numbers_lut_apply_u8(tmp_path: Path) -> None:
    require_cli()
    cube = tmp_path / "id.cube"
    cube.write_text("LUT_1D_SIZE 2\n0 0 0\n1 1 1\n", encoding="utf-8")
    rgb_path = tmp_path / "rgb2x2.npy"
    np.save(rgb_path, np.full((2, 2, 3), 64, dtype=np.uint8))
    bad_shape_npy = tmp_path / "rgb_bad_shape.npy"
    np.save(bad_shape_npy, np.zeros((2, 2), dtype=np.uint8))

    space = {"input_space": "srgb", "output_space": "srgb"}
    specs: list[Path] = []

    def add_lut(tag: str, h: int | float | str, w: int | float | str, cli_rgb: Path) -> None:
        p = tmp_path / f"lut-{len(specs)}-{tag}.json"
        p.write_text(
            json.dumps(
                {
                    "kind": "apply_u8",
                    "cube": str(cube),
                    "space": space,
                    "rgb_npy": str(rgb_path),
                    "cli_rgb_npy": str(cli_rgb),
                    "height": h,
                    "width": w,
                }
            ),
            encoding="utf-8",
        )
        specs.append(p)

    add_lut("normal", 2, 2, rgb_path)
    for field in ("height", "width"):
        for idx, v in enumerate(_param_values(2)[1:]):
            h = v if field == "height" else 2
            w = v if field == "width" else 2
            if _is_valid_nonneg_int(v):
                big_npy = tmp_path / f"huge-{field}-{idx}.npy"
                _write_fake_npy_header(big_npy, "|u1", (h if isinstance(h, int) else 2, w if isinstance(w, int) else 2, 3))
                cli_rgb = big_npy
            else:
                cli_rgb = bad_shape_npy
            add_lut(f"{field}-{idx}", h, w, cli_rgb)

    proc = run_area("lut", specs, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_wasm_numbers_jpeg_encode(tmp_path: Path) -> None:
    require_cli()
    npy_path = tmp_path / "rgb2x2.npy"
    np.save(npy_path, np.full((2, 2, 3), 80, dtype=np.uint8))
    raw_path = tmp_path / "rgb2x2.bin"
    raw_path.write_bytes(bytes([80] * 12))
    exif_path = tmp_path / "exif.json"
    exif_path.write_text("{}", encoding="utf-8")

    specs: list[Path] = []

    def add_jpeg(tag: str, payload: dict[str, Any]) -> None:
        p = tmp_path / f"jpeg-{len(specs)}-{tag}.json"
        payload["out"] = str(tmp_path / f"out-{len(specs)}.jpg")
        payload["exif"] = str(exif_path)
        p.write_text(json.dumps(payload), encoding="utf-8")
        specs.append(p)

    # Normal baseline checks byte-identical JPEG vs CLI.
    add_jpeg("normal", {"npy": str(npy_path), "quality": 90})

    for field in ("width", "height"):
        for idx, v in enumerate(_param_values(2)[1:]):
            cli_v = 65536 if (isinstance(v, int) and v > 65535) else 0
            add_jpeg(
                f"{field}-{idx}",
                {
                    "raw": str(raw_path),
                    "width": v if field == "width" else 2,
                    "height": v if field == "height" else 2,
                    "cliWidth": cli_v if field == "width" else 2,
                    "cliHeight": cli_v if field == "height" else 2,
                    "quality": 90,
                },
            )

    for idx, v in enumerate(_param_values(90)[1:]):
        cli_q = v if (isinstance(v, int) and -2_147_483_648 <= v <= 2_147_483_647) else -1
        add_jpeg(
            f"quality-{idx}",
            {
                "raw": str(raw_path),
                "width": 2,
                "height": 2,
                "quality": v,
                "cliQuality": cli_q,
            },
        )

    proc = run_area("jpeg", specs, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_wasm_numbers_hdr_exports(tmp_path: Path) -> None:
    require_cli()
    folder = tmp_path / "hdr_nums"
    folder.mkdir()
    plane = ((np.arange(1024, dtype=np.float32) % 31) / 100.0 + 0.1).reshape(32, 32)
    mask = np.zeros((32, 32), dtype=np.uint8)
    fft_in = np.array([[1.0, 0.0], [2.0, 0.0], [3.0, 0.0], [4.0, 0.0]], dtype=np.float32).reshape(1, 4, 2)
    np.save(folder / "plane.npy", plane)
    np.save(folder / "mask.npy", mask)
    np.save(folder / "fft_in.npy", fft_in)

    cases: list[dict[str, Any]] = []

    def cli_dim(v: int | float | str) -> int:
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            return v
        return 0

    # hdr_align: normal + 4 numeric fields (sdr_h, sdr_w, raw_h, raw_w)
    cases.append(
        {"fn": "hdr_align", "label": "align_normal", "sdr": "plane.npy", "raw": "plane.npy", "sdr_h": 32, "sdr_w": 32, "raw_h": 32, "raw_w": 32}
    )
    for field in ("sdr_h", "sdr_w", "raw_h", "raw_w"):
        for idx, v in enumerate(_param_values(32)[1:]):
            c = {
                "fn": "hdr_align",
                "label": f"align_{field}_{idx}",
                "sdr": "plane.npy",
                "raw": "plane.npy",
                "sdr_h": 32,
                "sdr_w": 32,
                "raw_h": 32,
                "raw_w": 32,
            }
            c[field] = v
            c[f"cli_{field}"] = cli_dim(v)
            cases.append(c)

    # hdr_gain: normal (both codes & report) + height, width
    cases.append(
        {
            "fn": "hdr_gain",
            "label": "gain_normal_codes",
            "sdr": "plane.npy",
            "raw": "plane.npy",
            "clip": "mask.npy",
            "h": 32,
            "w": 32,
            "check_codes": True,
        }
    )
    cases.append(
        {"fn": "hdr_gain", "label": "gain_normal_report", "sdr": "plane.npy", "raw": "plane.npy", "clip": "mask.npy", "h": 32, "w": 32}
    )
    for field in ("h", "w"):
        for idx, v in enumerate(_param_values(32)[1:]):
            c = {"fn": "hdr_gain", "label": f"gain_{field}_{idx}", "sdr": "plane.npy", "raw": "plane.npy", "clip": "mask.npy", "h": 32, "w": 32}
            c[field] = v
            c[f"cli_{field}"] = cli_dim(v)
            cases.append(c)

    # hdr_fft: normal (1, 4) + rows, n
    cases.append({"fn": "hdr_fft", "label": "fft_normal", "input": "fft_in.npy", "rows": 1, "n": 4})
    for field, normal in (("rows", 1), ("n", 4)):
        for idx, v in enumerate(_param_values(normal)[1:]):
            c = {"fn": "hdr_fft", "label": f"fft_{field}_{idx}", "input": "fft_in.npy", "rows": 1, "n": 4}
            c[field] = v
            c[f"cli_{field}"] = cli_dim(v)
            cases.append(c)

    # hdr_phase_plane: normal (both plane & report) + height, width
    cases.append(
        {"fn": "hdr_phase_plane", "label": "pc_normal_plane", "a": "plane.npy", "b": "plane.npy", "h": 32, "w": 32, "check_plane": True}
    )
    cases.append({"fn": "hdr_phase_plane", "label": "pc_normal_report", "a": "plane.npy", "b": "plane.npy", "h": 32, "w": 32})
    for field in ("h", "w"):
        for idx, v in enumerate(_param_values(32)[1:]):
            c = {"fn": "hdr_phase_plane", "label": f"pc_{field}_{idx}", "a": "plane.npy", "b": "plane.npy", "h": 32, "w": 32}
            c[field] = v
            c[f"cli_{field}"] = cli_dim(v)
            cases.append(c)

    # Empty shapes where one axis is 0 and the other is small or >= 2**32:
    # WASM must follow the exact native CoreError order instead of failing early on wasm32 usize conversion.
    empty_plane = np.zeros((0, 0), dtype=np.float32)
    empty_mask = np.zeros((0, 0), dtype=np.uint8)
    empty_fft = np.zeros((0, 4, 2), dtype=np.float32)
    np.save(folder / "empty_plane.npy", empty_plane)
    np.save(folder / "empty_mask.npy", empty_mask)
    np.save(folder / "empty_fft.npy", empty_fft)

    zero_shapes: tuple[tuple[int, int], ...] = (
        (0, 0),
        (0, 32),
        (32, 0),
        (U32_WRAP - 1, 0),
        (U32_WRAP, 0),
        (U32_WRAP + 1, 0),
        (2**53, 0),
        (0, U32_WRAP - 1),
        (0, U32_WRAP),
        (0, U32_WRAP + 1),
        (0, 2**53),
    )
    for idx, (h, w) in enumerate(zero_shapes):
        cases.append(
            {
                "fn": "hdr_align",
                "label": f"align_empty_{idx}_{h}x{w}",
                "sdr": "empty_plane.npy",
                "raw": "empty_plane.npy",
                "sdr_h": h,
                "sdr_w": w,
                "raw_h": h,
                "raw_w": w,
            }
        )
        for p_tag, params in (("default", ""), ("bad_params", "[]")):
            cases.append(
                {
                    "fn": "hdr_gain",
                    "label": f"gain_empty_{p_tag}_{idx}_{h}x{w}",
                    "sdr": "empty_plane.npy",
                    "raw": "empty_plane.npy",
                    "clip": "empty_mask.npy",
                    "h": h,
                    "w": w,
                    "params": params,
                }
            )
        cases.append(
            {
                "fn": "hdr_phase_plane",
                "label": f"pc_empty_{idx}_{h}x{w}",
                "a": "empty_plane.npy",
                "b": "empty_plane.npy",
                "h": h,
                "w": w,
            }
        )
    cases.append(
        {
            "fn": "hdr_align",
            "label": "align_mixed_empty_right",
            "sdr": "plane.npy",
            "raw": "empty_plane.npy",
            "sdr_h": 32,
            "sdr_w": 32,
            "raw_h": U32_WRAP + 1,
            "raw_w": 0,
        }
    )
    cases.append(
        {
            "fn": "hdr_align",
            "label": "align_mixed_empty_left",
            "sdr": "empty_plane.npy",
            "raw": "plane.npy",
            "sdr_h": 0,
            "sdr_w": U32_WRAP + 1,
            "raw_h": 32,
            "raw_w": 32,
        }
    )
    cases.append(
        {
            "fn": "hdr_gain",
            "label": "gain_empty_bad_clip_len",
            "sdr": "empty_plane.npy",
            "raw": "empty_plane.npy",
            "clip": "mask.npy",
            "h": U32_WRAP + 1,
            "w": 0,
        }
    )
    for inverse in (False, True):
        for two_d in (False, True):
            for idx, rows in enumerate((0, 1, U32_WRAP - 1, U32_WRAP, U32_WRAP + 1, 2**53)):
                cases.append(
                    {
                        "fn": "hdr_fft",
                        "label": f"fft_empty_n0_inv{int(inverse)}_2d{int(two_d)}_{idx}",
                        "input": "empty_fft.npy",
                        "rows": rows,
                        "n": 0,
                        "inverse": inverse,
                        "two_d": two_d,
                    }
                )
            cases.append(
                {
                    "fn": "hdr_fft",
                    "label": f"fft_empty_rows0_n4_inv{int(inverse)}_2d{int(two_d)}",
                    "input": "empty_fft.npy",
                    "rows": 0,
                    "n": 4,
                    "inverse": inverse,
                    "two_d": two_d,
                }
            )
            cases.append(
                {
                    "fn": "hdr_fft",
                    "label": f"fft_empty_rows0_nbig_inv{int(inverse)}_2d{int(two_d)}",
                    "input": "empty_fft.npy",
                    "rows": 0,
                    "n": U32_WRAP + 1,
                    "inverse": inverse,
                    "two_d": two_d,
                }
            )

    (folder / "numeric_cases.json").write_text(json.dumps(cases), encoding="utf-8")
    proc = run_area("hdr", [folder], tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_native_cli_unrepresentable_token_probes(tmp_path: Path) -> None:
    """Confirm which tokens gxr-cli rejects at CLI parsing (exit 1) vs CoreError (exit 2)."""
    require_cli()
    dng = _dng_4x4(tmp_path)
    raw = tmp_path / "rgb.bin"
    raw.write_bytes(bytes(12))

    # CLI exits 1 (usage error) on fractional/non-finite float tokens in integer flags.
    for tok in ("1.5", "NaN", "Infinity"):
        p_dng = subprocess.run(
            [str(CLI), "dng-wb-block", str(dng), "--rows", f"{tok}:2", "--out", str(tmp_path / "o.npy")],
            check=False,
            capture_output=True,
            text=True,
        )
        assert p_dng.returncode == 1
        p_geom = subprocess.run(
            [str(CLI), "geom-final-box", "--w", tok, "--h", "2", "--geometry", "{}", "--crop", "null"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert p_geom.returncode == 1
        p_jpg = subprocess.run(
            [str(CLI), "jpeg-encode", "--raw", str(raw), "--width", "2", "--height", "2", "--quality", tok, "--out", str(tmp_path / "o.jpg")],
            check=False,
            capture_output=True,
            text=True,
        )
        assert p_jpg.returncode == 1

    # CLI exits 2 with JSON CoreError for representable out-of-range integers.
    p_wb = subprocess.run(
        [str(CLI), "dng-wb-block", str(dng), "--rows", "4294967296:2", "--out", str(tmp_path / "wb.npy")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert p_wb.returncode == 2 and json.loads(p_wb.stdout)["error"]["kind"] == "index_error"

    p_box = subprocess.run(
        [str(CLI), "geom-final-box", "--w", "4294967298", "--h", "2", "--geometry", "{}", "--crop", "null"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert p_box.returncode == 2 and json.loads(p_box.stdout)["error"]["kind"] == "bad_geometry"
