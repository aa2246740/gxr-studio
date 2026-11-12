"""Parity: gxr-cli against the Python DNG reader on synthetic files and real samples.

Tolerances are the ones in the mobile foundation spec. Integers, strings, shapes,
mosaic samples and half-resolution float32 codes are exact. Only ``black``,
``white``, ``wb``, ``scene.A`` and ``scene.b`` may differ by ``1e-12`` relative.
A miss reports the first index and both values.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from gxr_studio.gpu.source import describe
from gxr_studio.raw.codes import half_res_codes, orient_array
from gxr_studio.raw.dng import Dng, RawDecodeError, open_dng
from support.core_cli import CoreCliError, error_of, require_cli, run, run_json, run_npy
from support.dng_writer import write_dng

_STUDIO = Path(__file__).resolve().parents[1]
_PARENT = Path(os.environ.get("GXR_STUDIO_TEST_PARENT", str(_STUDIO.parent)))
CAPTURES = _PARENT / "captures"
_CAP = 60
_FLOAT_AT = {("layout", "white"), ("scene", "b")}


def _real_dngs() -> list[Path]:
    """Up to 60 DNGs under captures/, spread in sorted order, skipping backup paths."""
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


def _pattern(height: int, width: int, values: tuple[int, ...]) -> np.ndarray:
    tiled = np.resize(np.array(values, np.uint16), height * width)
    return tiled.reshape(height, width)


def _floats(path: tuple[Any, ...]) -> bool:
    if path[:2] in {("layout", "black"), ("layout", "wb"), ("scene", "A")}:
        return True
    return path in _FLOAT_AT or (len(path) >= 2 and path[:2] == ("scene", "b"))


def _walk(python: Any, rust: Any, path: tuple[Any, ...], bad: list[str]) -> None:
    if _floats(path) and isinstance(python, (int, float)) and isinstance(rust, (int, float)) and not isinstance(python, bool):
        limit = 1e-12 * max(1.0, abs(float(python)))
        if abs(float(python) - float(rust)) > limit:
            bad.append(f"{_where(path)}: python={python!r} rust={rust!r}")
        return
    if isinstance(python, dict) and isinstance(rust, dict):
        if set(python) != set(rust):
            bad.append(f"{_where(path)}: keys {sorted(python)} vs {sorted(rust)}")
            return
        for key in python:
            _walk(python[key], rust[key], (*path, key), bad)
        return
    if isinstance(python, list) and isinstance(rust, list):
        if len(python) != len(rust):
            bad.append(f"{_where(path)}: length {len(python)} vs {len(rust)}")
            return
        for index, (left, right) in enumerate(zip(python, rust)):
            _walk(left, right, (*path, index), bad)
        return
    if python != rust:
        bad.append(f"{_where(path)}: python={python!r} rust={rust!r}")


def _where(path: tuple[Any, ...]) -> str:
    return ".".join(str(part) for part in path) or "<root>"


def _same(python: Any, rust: Any) -> None:
    bad: list[str] = []
    _walk(python, rust, (), bad)
    if bad:
        pytest.fail(bad[0])


def _normalize(value: Any) -> Any:
    """Make ``Dng.summary()`` comparable with the CLI's JSON (bytes become int lists)."""
    if isinstance(value, (bytes, bytearray)):
        return list(value)
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, np.ndarray):
        return _normalize(value.tolist())
    if isinstance(value, np.generic):
        return value.item()
    return value


def _assert_error(caught: RawDecodeError, rust: CoreCliError) -> None:
    assert rust.kind == "raw_decode"
    assert rust.message == str(caught)


def _python_describe(path: Path) -> tuple[dict[str, Any] | None, RawDecodeError | None]:
    try:
        source = describe(open_dng(path), with_scene=True)
    except RawDecodeError as exc:
        return None, exc
    return asdict(source), None


def _rust_describe(path: Path) -> tuple[Any, CoreCliError | None]:
    code, payload = run_json(["dng-describe", str(path)])
    return payload, error_of(code, payload)


def _write(tmp_path: Path, name: str, mosaic: np.ndarray, **kwargs: Any) -> Path:
    dest = tmp_path / f"{name}.dng"
    dest.write_bytes(write_dng(mosaic, **kwargs))
    return dest


def _bits() -> np.ndarray:
    return _pattern(6, 8, (0x000, 0xFFF, 0xA5A, 0x5A5, 0x123, 0xC3C))


def _wide_bits() -> np.ndarray:
    return _pattern(4, 8, (0x0000, 0xFFFF, 0x1234, 0xA5A5, 0x00FF, 0xFF00))


def test_cli_version_and_usage() -> None:
    code, payload = run_json(["version"])
    assert code == 0
    assert isinstance(payload.get("version"), str) and payload["version"]
    proc = run(["not-a-command"])
    assert proc.returncode == 1


def test_real_sample_set() -> None:
    """The Pro has real DNGs. The count and the unit mix are what the spread selected."""
    require_cli()
    if not REAL:
        if os.environ.get("GXR_REQUIRE_CORE") == "1":
            pytest.fail(f"no DNGs under {CAPTURES}")
        pytest.skip(f"no DNGs under {CAPTURES}")
    units = Counter(path.relative_to(CAPTURES).parts[0] for path in REAL)
    print(f"real_dngs={len(REAL)} units={dict(sorted(units.items()))}")
    assert REAL


@pytest.mark.parametrize("path", REAL, ids=_real_id if REAL else None)
def test_real_describe_summary_mosaic_and_half(path: Path, tmp_path: Path) -> None:
    require_cli()
    py_desc, py_err = _python_describe(path)
    rs_desc, rs_err = _rust_describe(path)
    if py_err is not None or rs_err is not None:
        assert py_err is not None and rs_err is not None
        _assert_error(py_err, rs_err)
        return
    assert py_desc is not None and rs_desc is not None
    _same(py_desc, rs_desc)

    code, summary = run_json(["dng-summary", str(path)])
    assert error_of(code, summary) is None
    _same(_normalize(Dng(path).summary()), summary)

    mosaic = run_npy(["dng-mosaic", str(path)], tmp_path / "mosaic.npy")
    assert mosaic.dtype == np.uint16
    assert np.array_equal(mosaic, open_dng(path).mosaic())

    half = run_npy(["dng-half-codes", str(path)], tmp_path / "half.npy")
    dng = open_dng(path)
    python_half = orient_array(half_res_codes(dng), dng.orientation)
    _assert_f32(python_half, half)


def _assert_f32(python: np.ndarray, rust: np.ndarray) -> None:
    left = np.ascontiguousarray(python, np.float32)
    right = np.ascontiguousarray(rust, np.float32)
    assert left.shape == right.shape
    if np.array_equal(left, right):
        return
    index = tuple(int(v) for v in np.argwhere(left != right)[0])
    pytest.fail(f"half-codes differ at {index}: python={left[index]!r} rust={right[index]!r}")


def _expect_describe(path: Path) -> None:
    py_desc, py_err = _python_describe(path)
    rs_desc, rs_err = _rust_describe(path)
    if py_err is not None or rs_err is not None:
        assert py_err is not None and rs_err is not None
        _assert_error(py_err, rs_err)
        return
    assert py_desc is not None and rs_desc is not None
    _same(py_desc, rs_desc)


def _expect_mosaic(path: Path, tmp_path: Path) -> None:
    try:
        python = open_dng(path).mosaic()
        py_err = None
    except RawDecodeError as exc:
        python, py_err = None, exc
    try:
        rust = run_npy(["dng-mosaic", str(path)], tmp_path / "mosaic.npy")
        rs_err = None
    except CoreCliError as exc:
        rust, rs_err = None, exc
    if py_err is not None or rs_err is not None:
        assert py_err is not None and rs_err is not None
        _assert_error(py_err, rs_err)
        return
    assert python is not None and rust is not None
    assert rust.dtype == np.uint16
    assert python.dtype == np.uint16
    assert python.shape == rust.shape
    if not np.array_equal(python, rust):
        index = tuple(int(v) for v in np.argwhere(python != rust)[0])
        pytest.fail(f"mosaic differs at {index}: python={int(python[index])} rust={int(rust[index])}")


def _expect_half(path: Path, tmp_path: Path) -> None:
    try:
        dng = open_dng(path)
        python = orient_array(half_res_codes(dng), dng.orientation)
        py_err = None
    except RawDecodeError as exc:
        python, py_err = None, exc
    try:
        rust = run_npy(["dng-half-codes", str(path)], tmp_path / "half.npy")
        rs_err = None
    except CoreCliError as exc:
        rust, rs_err = None, exc
    if py_err is not None or rs_err is not None:
        assert py_err is not None and rs_err is not None
        _assert_error(py_err, rs_err)
        return
    assert python is not None and rust is not None
    _assert_f32(python, rust)


def test_synthetic_16_both_endians_and_strips(tmp_path: Path) -> None:
    """16-bit samples, both endians, several strips, a short padded last strip, orientations 1–8."""
    image = _wide_bits()
    for endian, tag in ((">", "be"), ("<", "le")):
        for orientation in range(1, 9):
            path = _write(
                tmp_path,
                f"16-{tag}-o{orientation}",
                image,
                endian=endian,
                bps=16,
                rows_per_strip=3,
                pad_last=4,
                orientation=orientation,
            )
            _expect_describe(path)
            _expect_mosaic(path, tmp_path)
            _expect_half(path, tmp_path)


def test_synthetic_12_bit_patterns_and_odd_width(tmp_path: Path) -> None:
    """12-bit packing, both endians, including an odd width the layout refuses."""
    even = _bits()
    odd = _pattern(4, 5, (0x000, 0xFFF, 0xA5A, 0x5A5, 0x010))
    for endian, tag in ((">", "be"), ("<", "le")):
        path = _write(tmp_path, f"12-{tag}", even, endian=endian, bps=12, rows_per_strip=2, pad_last=2)
        _expect_describe(path)
        _expect_mosaic(path, tmp_path)
        _expect_half(path, tmp_path)
        odd_path = _write(tmp_path, f"12-odd-{tag}", odd, endian=endian, bps=12)
        described, err = _python_describe(odd_path)
        assert err is None and described is not None and described["reason"] == "odd_width_12bit"
        _expect_describe(odd_path)
        _expect_mosaic(odd_path, tmp_path)
        _expect_half(odd_path, tmp_path)


def test_synthetic_black_crop_matrix_and_table(tmp_path: Path) -> None:
    """Black levels, crop windows, a linearization table, and the two matrix failures."""
    image = _pattern(8, 8, (10, 20, 30, 40, 50, 60, 70, 80))
    path = _write(tmp_path, "black-1", image, black=(1.0,), black_repeat=(1, 1))
    _expect_describe(path)
    _expect_half(path, tmp_path)

    path = _write(tmp_path, "black-2", image, black=(1.0, 2.0, 3.0, 4.5), black_repeat=(2, 2))
    _expect_describe(path)
    _expect_half(path, tmp_path)

    path = _write(tmp_path, "black-3", image, black=(1, 2, 3, 4, 5, 6), black_repeat=(3, 2))
    described, err = _python_describe(path)
    assert err is None and described is not None and described["reason"] == "black_level_pattern"
    _expect_describe(path)
    _expect_half(path, tmp_path)

    path = _write(tmp_path, "delta", image, black_dh=tuple(float(i) for i in range(8)), black_dv=(0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0))
    described, err = _python_describe(path)
    assert err is None and described is not None and described["reason"] == "black_level_pattern"
    _expect_describe(path)
    _expect_mosaic(path, tmp_path)
    _expect_half(path, tmp_path)

    table = tuple(range(0, 256, 2))
    path = _write(tmp_path, "linear", image, linearization=table)
    described, err = _python_describe(path)
    assert err is None and described is not None and described["reason"] == "linearization_table"
    _expect_describe(path)
    _expect_half(path, tmp_path)

    path = _write(tmp_path, "crop", image, active=(1, 1, 7, 7), crop_origin=(1, 1), crop_size=(4, 4))
    _expect_describe(path)
    _expect_mosaic(path, tmp_path)
    _expect_half(path, tmp_path)

    path = _write(tmp_path, "tiny", image, crop_size=(1, 1))
    described, err = _python_describe(path)
    assert err is None and described is not None and described["reason"] == "too_small"
    _expect_describe(path)
    _expect_half(path, tmp_path)

    path = _write(tmp_path, "no-matrix", image, color_matrix=None)
    described, err = _python_describe(path)
    assert err is None and described is not None and described["scene"] is None and described["layout"] is not None
    _expect_describe(path)

    path = _write(tmp_path, "nan-matrix", image, nan_in_matrix=True)
    described, err = _python_describe(path)
    assert err is None and described is not None and described["scene"] is None
    _expect_describe(path)


def test_synthetic_neutral_and_bps(tmp_path: Path) -> None:
    image = _pattern(4, 4, (1, 2, 3, 4))
    for name, kwargs in (
        ("no-neutral", {"neutral": None}),
        ("zero-neutral", {"neutral": (0.0, 1.0, 0.5)}),
    ):
        path = _write(tmp_path, name, image, **kwargs)
        _expect_describe(path)
        _expect_half(path, tmp_path)

    fourteen = _pattern(4, 4, (0, 1, 100, 0x3FFF))
    path = _write(tmp_path, "bps14", fourteen, bps=14)
    described, err = _python_describe(path)
    assert err is None and described is not None and described["reason"] == "bits_per_sample_14"
    _expect_describe(path)
    _expect_mosaic(path, tmp_path)


def test_synthetic_errors_and_lazy_describe(tmp_path: Path) -> None:
    """Open failures share their text. A header with no strip bytes still describes."""
    image = _pattern(4, 4, (1, 2, 3, 4))
    cfa = ((0, 1, 2), (1, 2, 0), (2, 0, 1))
    path = _write(tmp_path, "cfa3", image, cfa=cfa)
    _expect_describe(path)
    _expect_mosaic(path, tmp_path)

    path = _write(tmp_path, "tiled", image, tiled=True)
    _expect_describe(path)

    path = _write(tmp_path, "compressed", image, compression=7)
    _expect_describe(path)

    path = _write(tmp_path, "past-end", image, strip_offset_past_end=True)
    described, err = _python_describe(path)
    assert err is None and described is not None and described["layout"] is not None
    _expect_describe(path)
    _expect_mosaic(path, tmp_path)
    _expect_half(path, tmp_path)

    short = tmp_path / "short.dng"
    short.write_bytes(b"II\x00\x00")
    _expect_describe(short)

    magic = tmp_path / "magic.dng"
    magic.write_bytes(b"II" + (0).to_bytes(2, "little") + (8).to_bytes(4, "little"))
    _expect_describe(magic)

    orphan = tmp_path / "orphan.dng"
    orphan.write_bytes(b"II" + (42).to_bytes(2, "little") + (1000).to_bytes(4, "little"))
    _expect_describe(orphan)
