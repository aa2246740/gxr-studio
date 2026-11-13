"""The WASM build and gxr-cli must print the same bytes for the same DNGs.

Node loads ``mobile/src/wasm-pkg`` (``make core-wasm``) and runs
``mobile/scripts/wasm_vs_native.mjs`` through ``support.wasm_check.run_area``.
Missing pieces skip, unless ``GXR_REQUIRE_CORE=1``, in which case the test
fails. ``GXR_REQUIRE_GPU_TEST`` does not turn a missing package into a failure.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import numpy as np
import pytest

from support.dng_writer import write_dng
from support.wasm_check import CLI, run_area
from test_core_parity_raw import REAL, _pattern, hostile_cases


def _missing(message: str) -> None:
    if os.environ.get("GXR_REQUIRE_CORE") == "1":
        pytest.fail(message)
    pytest.skip(message)


def test_wasm_describe_and_half_codes_match_cli(tmp_path: Path) -> None:
    if not REAL:
        _missing("no real DNGs for the wasm comparison")
    proc = run_area("raw", REAL, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _synthetic_paths(tmp_path: Path) -> list[Path]:
    paths: list[Path] = []
    wide = _pattern(4, 8, (0x0000, 0xFFFF, 0x1234, 0xA5A5, 0x00FF, 0xFF00))
    bits = _pattern(6, 8, (0x000, 0xFFF, 0xA5A, 0x5A5, 0x123, 0xC3C))
    image = _pattern(8, 8, (10, 20, 30, 40, 50, 60, 70, 80))

    def put(name: str, payload: bytes) -> None:
        path = tmp_path / f"{name}.dng"
        path.write_bytes(payload)
        paths.append(path)

    put("be16", write_dng(wide, endian=">", bps=16))
    put("le16", write_dng(wide, endian="<", bps=16, rows_per_strip=3))
    put("be12", write_dng(bits, endian=">", bps=12))
    put("black", write_dng(image, black=(1.0, 2.0, 3.0, 4.5), black_repeat=(2, 2)))
    put("crop", write_dng(image, active=(1, 1, 7, 7), crop_origin=(1, 1), crop_size=(4, 4)))
    put("compressed", write_dng(image, compression=7))
    for name, payload in hostile_cases().items():
        put(name, payload)
    return paths


def test_wasm_matches_native_on_synthetic_and_hostile_inputs(tmp_path: Path) -> None:
    proc = run_area("raw", _synthetic_paths(tmp_path), tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _write_cli(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def test_wasm_script_rejects_an_empty_cli_error(tmp_path: Path) -> None:
    sample = tmp_path / "valid.dng"
    sample.write_bytes(write_dng(_pattern(4, 6, (1, 2, 3, 4, 5, 6))))
    fake = tmp_path / "fake-cli"
    _write_cli(
        fake,
        f"""#!/bin/sh
if [ "$1" = "dng-half-codes" ]; then
  exit 2
fi
exec {CLI} "$@"
""",
    )
    proc = run_area("raw", [sample], tmp_path, cli=fake, check=False)
    assert proc.returncode != 0
    assert sample.name in (proc.stdout + proc.stderr)


def test_wasm_script_rejects_cli_success_when_wasm_errors(tmp_path: Path) -> None:
    garbage = tmp_path / "garbage.dng"
    garbage.write_bytes(b"NOT_A_TIFF_123456789")
    npy = tmp_path / "ok.npy"
    np.save(npy, np.zeros((1, 1), np.float32))
    fake = tmp_path / "fake-ok"
    _write_cli(
        fake,
        f"""#!/bin/sh
out=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "--out" ]; then out=$arg; fi
  prev=$arg
done
if [ -n "$out" ]; then cp {npy} "$out"; fi
printf '%s\\n' '{{"layout":null,"reason":null,"scene":null}}'
exit 0
""",
    )
    proc = run_area("raw", [garbage], tmp_path, cli=fake, check=False)
    assert proc.returncode != 0
    assert garbage.name in (proc.stdout + proc.stderr)
