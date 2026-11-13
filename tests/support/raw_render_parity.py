"""Parity helpers for full-size raw demosaic and neutral rendering."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from gxr_studio.raw.demosaic import demosaic_full, demosaic_strips
from gxr_studio.raw.dng import Dng, RawDecodeError, open_dng
from gxr_studio.raw.render import NEUTRAL, render_full
from support.core_cli import CoreCliError, error_of, run_json, run_npy


def assert_f32_exact(python: np.ndarray, rust: np.ndarray) -> None:
    assert python.dtype == np.float32 and rust.dtype == np.float32
    assert python.shape == rust.shape
    if np.array_equal(python, rust):
        return
    index = tuple(int(v) for v in np.argwhere(python != rust)[0])
    pytest.fail(f"float32 differ at {index}: python={python[index]!r} rust={rust[index]!r}")


def assert_u8_exact(python: np.ndarray, rust: np.ndarray, *, dng: Dng | None = None) -> None:
    assert python.dtype == np.uint8 and rust.dtype == np.uint8
    assert python.shape == rust.shape
    if np.array_equal(python, rust):
        return
    index = tuple(int(v) for v in np.argwhere(python != rust)[0])
    extra = ""
    if dng is not None and dng.orientation == 1:
        y, x, c = index
        for y0, strip in demosaic_strips(dng):
            if y0 <= y < y0 + strip.shape[0]:
                extra = f" float32_code={float(strip[y - y0, x, c])!r}"
                break
    pytest.fail(f"uint8 differ at {index}: python={int(python[index])} rust={int(rust[index])}{extra}")


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
    assert_u8_exact(python, rust, dng=dng)


def expect_demosaic_error(path: Path, y0: int, y1: int, tmp_path: Path, expected_message: str) -> None:
    code, payload = run_json(["dng-demosaic", "--dng", str(path), "--y0", str(y0), "--y1", str(y1), "--out", str(tmp_path / "err.npy")])
    err = error_of(code, payload)
    assert err is not None and err.kind == "raw_decode" and err.message == expected_message
