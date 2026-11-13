"""Parity helpers for full-size raw demosaic and neutral rendering."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from decimal import Decimal, localcontext
from pathlib import Path

import numpy as np
import pytest

from gxr_studio.raw.demosaic import demosaic_full, demosaic_strips
from gxr_studio.raw.dng import Dng, RawDecodeError, open_dng
from gxr_studio.raw.render import NEUTRAL, render_full
from support.core_cli import CLI, CoreCliError, error_of, run_json, run_npy
from support.dng_writer import write_dng
from support.wasm_check import PKG, run_area


def assert_f32_exact(python: np.ndarray, rust: np.ndarray) -> None:
    assert python.dtype == np.float32 and rust.dtype == np.float32
    assert python.shape == rust.shape
    if np.array_equal(python, rust):
        return
    index = tuple(int(v) for v in np.argwhere(python != rust)[0])
    pytest.fail(f"float32 differ at {index}: python={python[index]!r} rust={rust[index]!r}")


def _decimal_to_f32_round_half_even(value: Decimal) -> np.float32:
    approx = np.float32(float(value))
    cands = [
        np.nextafter(approx, np.float32(-np.inf)),
        approx,
        np.nextafter(approx, np.float32(np.inf)),
    ]

    def rank(cand: np.float32) -> tuple[Decimal, int]:
        return (abs(value - Decimal.from_float(float(cand))), int(cand.view(np.uint32)) & 1)

    return min(cands, key=rank)


def _correctly_rounded_powf(a: np.float32) -> np.float32:
    inv32 = np.float32(1.0 / 2.4)
    with localcontext() as ctx:
        ctx.prec = 60
        exact = (Decimal.from_float(float(a)).ln() * Decimal.from_float(float(inv32))).exp()
        return _decimal_to_f32_round_half_even(exact)


def _unoriented_yx(y: int, x: int, h: int, w: int, orientation: int) -> tuple[int, int]:
    if orientation == 1:
        return y, x
    if orientation == 2:
        return y, w - 1 - x
    if orientation == 3:
        return h - 1 - y, w - 1 - x
    if orientation == 4:
        return h - 1 - y, x
    if orientation == 5:
        return x, y
    if orientation == 6:
        return h - 1 - x, y
    if orientation == 7:
        return h - 1 - x, w - 1 - y
    if orientation == 8:
        return x, w - 1 - y
    raise AssertionError(f"unexpected orientation {orientation}")


def assert_u8_exact(
    python: np.ndarray,
    rust: np.ndarray,
    *,
    dng: Dng | None = None,
    path: Path | None = None,
    tmp_path: Path | None = None,
) -> None:
    assert python.dtype == np.uint8 and rust.dtype == np.uint8
    assert python.shape == rust.shape
    if np.array_equal(python, rust):
        return
    diffs = np.argwhere(python != rust)
    if dng is None or path is None or tmp_path is None:
        index = tuple(int(v) for v in diffs[0])
        pytest.fail(f"uint8 differ at {index}: python={int(python[index])} rust={int(rust[index])}")

    h, w = dng.crop_size[1], dng.crop_size[0]
    py_strips = list(demosaic_strips(dng))
    rs_strips: dict[int, np.ndarray] = {}
    pow_cache: dict[int, tuple[str, str]] = {}
    print(f"powf_oracle_check: input={path.name} diff_count={len(diffs)}")
    for raw_idx in diffs:
        y, x, c = (int(v) for v in raw_idx)
        uy, ux = _unoriented_yx(y, x, h, w, dng.orientation)
        strip_y0 = -1
        py_strip = None
        for y0, strip in py_strips:
            if y0 <= uy < y0 + strip.shape[0]:
                strip_y0 = y0
                py_strip = strip
                break
        assert py_strip is not None
        if strip_y0 not in rs_strips:
            rs_strips[strip_y0] = run_npy(
                ["dng-demosaic", "--dng", str(path), "--y0", str(strip_y0), "--y1", str(strip_y0 + py_strip.shape[0])],
                tmp_path / f"dm-oracle-{strip_y0}.npy",
            )
        rs_strip = rs_strips[strip_y0]
        # 1. Pre-quantisation float32 code is bit-identical between Python and Rust.
        assert_f32_exact(py_strip, rs_strip)
        code = py_strip[uy - strip_y0, ux, c]
        assert code.tobytes() == rs_strip[uy - strip_y0, ux, c].tobytes()

        # 2. Independent 60-digit Decimal powf rounded half-even to f32 matches Rust's actual
        # power float32 bits and differs from libSystem's.
        a = np.clip(code / np.float32(4095.0), np.float32(0.0), np.float32(1.0))
        assert a > np.float32(0.0031308), f"unexpected disagreement in linear segment at {(y, x, c)}: a={float(a)!r}"
        a_bits = int(a.view(np.uint32))
        if a_bits not in pow_cache:
            rc, pow_json = run_json(["dng-srgb-pow", "--bits", f"0x{a_bits:08x}"])
            assert rc == 0 and "power_bits" in pow_json
            pow_cache[a_bits] = (str(pow_json["power_bits"]), str(pow_json["srgb_bits"]))
        rust_pow_bits, rust_srgb_bits = pow_cache[a_bits]

        p_oracle = _correctly_rounded_powf(a)
        oracle_pow_bits = f"0x{int(p_oracle.view(np.uint32)):08x}"
        p_libsystem = np.power(np.array([a], np.float32), 1 / 2.4, dtype=np.float32)[0]
        libsystem_pow_bits = f"0x{int(p_libsystem.view(np.uint32)):08x}"
        assert rust_pow_bits == oracle_pow_bits, f"rust power bits {rust_pow_bits} != oracle {oracle_pow_bits} for input 0x{a_bits:08x}"
        assert oracle_pow_bits != libsystem_pow_bits, (
            f"non-powf mismatch at {(y, x, c)}: python={int(python[y, x, c])} rust={int(rust[y, x, c])} "
            f"code={float(code)!r} p_oracle={oracle_pow_bits}"
        )

        # 3. Rust's uint8 equals srgb_encode evaluated with that correctly rounded value,
        # while Python's uint8 matches libSystem's 1-ULP-short powf.
        enc_oracle = np.float32(1.055) * p_oracle - np.float32(0.055)
        assert f"0x{int(enc_oracle.view(np.uint32)):08x}" == rust_srgb_bits
        u8_oracle = int(np.rint(enc_oracle * np.float32(255.0)))
        enc_libsystem = np.float32(1.055) * p_libsystem - np.float32(0.055)
        u8_libsystem = int(np.rint(enc_libsystem * np.float32(255.0)))
        assert int(python[y, x, c]) == u8_libsystem, f"python uint8 {int(python[y, x, c])} != libSystem {u8_libsystem} at {(y, x, c)}"
        assert int(rust[y, x, c]) == u8_oracle, f"rust uint8 {int(rust[y, x, c])} != oracle {u8_oracle} at {(y, x, c)} code={float(code)!r}"

        # 4. Print input, index and both values.
        print(
            f"powf_oracle_override: input={path.name} index=({y}, {x}, {c}) "
            f"python={int(python[y, x, c])} rust={int(rust[y, x, c])} "
            f"a=0x{a_bits:08x} rust_pow={rust_pow_bits} libsystem_pow={libsystem_pow_bits}"
        )


def message_ok(python: str, rust: str) -> bool:
    if rust == python:
        return True
    prefix = "不是可识别的 GXR DNG（ValueError: "
    return rust.startswith(prefix) and rust.endswith("）") and python.startswith(prefix)


def expect_demosaic_strip(path: Path, y0: int, y1: int, tmp_path: Path) -> None:
    dng = open_dng(path)
    python = demosaic_full(dng)[y0:y1]
    rust = run_npy(["dng-demosaic", "--dng", str(path), "--y0", str(y0), "--y1", str(y1)], tmp_path / f"dm-{y0}-{y1}.npy")
    assert_f32_exact(python, rust)


def expect_neutral(path: Path, tmp_path: Path) -> None:
    dng: Dng | None = None
    try:
        dng = open_dng(path)
        python = render_full(dng, NEUTRAL)
        py_err: BaseException | None = None
    except Exception as exc:  # noqa: BLE001 — parity checks error kinds too
        python, py_err = None, exc
    try:
        rust = run_npy(["dng-neutral", "--dng", str(path)], tmp_path / "neutral.npy")
        rs_err = None
    except CoreCliError as exc:
        rust, rs_err = None, exc
    if isinstance(py_err, RawDecodeError):
        assert rs_err is not None and rs_err.kind == "raw_decode"
        assert message_ok(str(py_err), rs_err.message), f"{py_err} vs {rs_err.message}"
        return
    if py_err is not None:
        assert rs_err is not None, f"python {type(py_err).__name__}: {py_err}"
        if "array is too big" in str(py_err) and rs_err.message.startswith("RAW 数据读不全（"):
            assert rs_err.kind == "raw_decode"
            return
        assert str(py_err) in rs_err.message, f"{py_err} vs {rs_err.message}"
        return
    assert rs_err is None, f"rust {rs_err}"
    assert python is not None and rust is not None
    assert_u8_exact(python, rust, dng=dng, path=path, tmp_path=tmp_path)


def expect_demosaic_error(path: Path, y0: int, y1: int, tmp_path: Path, expected_message: str) -> None:
    code, payload = run_json(["dng-demosaic", "--dng", str(path), "--y0", str(y0), "--y1", str(y1), "--out", str(tmp_path / "err.npy")])
    err = error_of(code, payload)
    assert err is not None and err.kind == "raw_decode" and err.message == expected_message


def verify_negative_crop_origin_no_alloc(tmp_path: Path, patch_fn: object) -> None:
    """Reject negative crop origin + enlarged size in native & WASM without allocating declared bytes."""
    base = write_dng(np.arange(24, dtype=np.uint16).reshape(4, 6) * 100 + 100)
    neg_4m_path: Path | None = None
    cases = [
        ("neg-x-2", (50719, 9, [-2, 0]), (50720, 4, [8, 4]), "block [0, 4) x [-2, 6) outside the raw plane"),
        ("neg-x-100k", (50719, 9, [-100_000, 0]), (50720, 4, [100_006, 4]), "block [0, 4) x [-100000, 6) outside the raw plane"),
        ("neg-x-4m", (50719, 9, [-4_000_000, 0]), (50720, 4, [4_000_006, 4]), "block [0, 4) x [-4000000, 6) outside the raw plane"),
        ("neg-y-4m", (50719, 9, [0, -4_000_000]), (50720, 4, [6, 4_000_004]), "block [-4000000, 4) x [0, 6) outside the raw plane"),
    ]
    for name, origin_patch, size_patch, expect_msg in cases:
        payload = patch_fn(patch_fn(base, *origin_patch), *size_patch)  # type: ignore[operator]
        path = tmp_path / f"{name}.dng"
        path.write_bytes(payload)
        if name == "neg-x-4m":
            neg_4m_path = path

        for cmd in (
            ["dng-neutral", "--dng", str(path), "--out", str(tmp_path / f"{name}-out.npy")],
            ["dng-demosaic", "--dng", str(path), "--y0", "0", "--y1", "4", "--out", str(tmp_path / f"{name}-dm.npy")],
        ):
            if sys.platform == "darwin" and Path("/usr/bin/time").is_file():
                proc = subprocess.run(["/usr/bin/time", "-l", str(CLI), *cmd], capture_output=True, text=True, check=False, timeout=30)
                rss_match = re.search(r"(\d+)\s+maximum resident set size", proc.stderr)
                assert rss_match is not None
                assert int(rss_match[1]) < 16 * 1024 * 1024, f"{name} rss={rss_match[1]}"
            else:
                proc = subprocess.run([str(CLI), *cmd], capture_output=True, text=True, check=False, timeout=30)
            assert proc.returncode == 2
            parsed = json.loads(proc.stdout)
            assert parsed["error"]["kind"] == "index_error"
            assert parsed["error"]["message"] == expect_msg

    # Also test negative origin relative to a non-zero ActiveArea (where cy0/cx0 >= 0 in plane).
    active_neg = write_dng(np.arange(48, dtype=np.uint16).reshape(6, 8) * 100 + 100, active=(1, 1, 5, 7))
    active_payload = patch_fn(patch_fn(active_neg, 50719, 9, [-1, 0]), 50720, 4, [7, 4])  # type: ignore[operator]
    active_path = tmp_path / "neg-active.dng"
    active_path.write_bytes(active_payload)
    code, payload_json = run_json(["dng-neutral", "--dng", str(active_path), "--out", str(tmp_path / "neg-active.npy")])
    err = error_of(code, payload_json)
    assert err is not None and err.kind == "index_error"

    assert neg_4m_path is not None
    if shutil.which("node") is None or not PKG.is_file():
        if os.environ.get("GXR_REQUIRE_CORE") == "1":
            pytest.fail("node and gxr_wasm.js required for negative crop WASM test")
        return

    proc_area = run_area("raw", [neg_4m_path, active_path], tmp_path)
    assert proc_area.returncode == 0, proc_area.stdout + proc_area.stderr

    wasm_bg = PKG.with_name("gxr_wasm_bg.wasm")
    script = (
        "import { readFileSync } from 'node:fs';"
        f"const mod = await import({json.dumps(PKG.as_uri())});"
        f"const inst = mod.initSync({{ module: readFileSync({json.dumps(str(wasm_bg))}) }});"
        "const before = inst.memory.buffer.byteLength;"
        f"const bytes = new Uint8Array(readFileSync({json.dumps(str(neg_4m_path))}));"
        "let msg = '';"
        "try { mod.dng_neutral_named(bytes, 'neg-x-4m.dng'); } catch (e) { msg = String(e); }"
        "const after = inst.memory.buffer.byteLength;"
        "console.log(JSON.stringify({ before, after, msg }));"
    )
    probe = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True, timeout=30)
    info = json.loads(probe.stdout)
    assert info["after"] <= info["before"] and info["after"] < 8 * 1024 * 1024, info
    err_obj = json.loads(info["msg"])["error"]
    assert err_obj == {"kind": "index_error", "message": "block [0, 4) x [-4000000, 6) outside the raw plane"}


def verify_powf_boundary_counterexamples(tmp_path: Path) -> None:
    """Permanent 4x4 flat synthetic DNGs where macOS libSystem powf differs by 1 ULP from oracle."""
    cases = [
        (0x3CC1C2A9, 0x42C1B68D, 0x3E57273C, 42, 43),
        (0x3DA23384, 0x43A22961, 0x3EB1FE6F, 79, 80),
        (0x3EADD76B, 0x44ADCC8E, 0x3F233874, 157, 158),
    ]
    for input_bits, code_bits, pow_bits, py_u8, rs_u8 in cases:
        code = np.uint32(code_bits).view(np.float32)
        num, den = float(code).as_integer_ratio()
        path = tmp_path / f"pow-{input_bits:08x}.dng"
        path.write_bytes(
            write_dng(
                np.ones((4, 4), np.uint16),
                bps=12,
                white=4095,
                neutral=((den, num), (den, num), (den, num)),
            )
        )
        dng = open_dng(path)
        py = render_full(dng, NEUTRAL)
        rs = run_npy(["dng-neutral", "--dng", str(path)], tmp_path / f"pow-{input_bits:08x}.npy")
        assert np.all(py == py_u8) and np.all(rs == rs_u8)
        assert int(np.count_nonzero(py != rs)) == 48
        rc, pow_json = run_json(["dng-srgb-pow", "--bits", f"0x{input_bits:08x}"])
        assert rc == 0 and pow_json["power_bits"] == f"0x{pow_bits:08x}"
        assert_u8_exact(py, rs, dng=dng, path=path, tmp_path=tmp_path)
