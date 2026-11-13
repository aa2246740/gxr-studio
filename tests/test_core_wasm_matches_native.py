"""The WASM build and gxr-cli must print the same bytes for the same DNGs.

Node loads ``mobile/src/wasm-pkg`` (``make core-wasm``) and runs
``mobile/scripts/wasm_vs_native.mjs``. Missing pieces skip, unless
``GXR_REQUIRE_CORE=1``, in which case the test fails. ``GXR_REQUIRE_GPU_TEST``
does not turn a missing package into a failure.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import numpy as np
import pytest

from support.dng_writer import write_dng
from test_core_parity_raw import REAL, _pattern, hostile_cases, strips_over_the_limit

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "mobile" / "scripts" / "wasm_vs_native.mjs"
PKG = REPO / "mobile" / "src" / "wasm-pkg" / "gxr_wasm.js"
CLI = REPO / "core" / "target" / "release" / "gxr-cli"


def _missing(message: str) -> None:
    if os.environ.get("GXR_REQUIRE_CORE") == "1":
        pytest.fail(message)
    pytest.skip(message)


def _need_tools() -> None:
    if shutil.which("node") is None:
        _missing("node is not on PATH")
    if not PKG.is_file() or not CLI.is_file() or not SCRIPT.is_file():
        _missing(f"need {PKG.name} and gxr-cli; run make core-wasm and make core")


def _run_script(paths: list[Path], cli: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    listed = tmp_path / "dngs.json"
    listed.write_text(json.dumps([str(path) for path in paths]), encoding="utf-8")
    return subprocess.run(
        ["node", str(SCRIPT), str(listed), str(cli)],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_wasm_describe_and_half_codes_match_cli(tmp_path: Path) -> None:
    if not REAL:
        _missing("no real DNGs for the wasm comparison")
    _need_tools()
    proc = _run_script(REAL, CLI, tmp_path)
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
    _need_tools()
    proc = _run_script(_synthetic_paths(tmp_path), CLI, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _write_cli(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def test_wasm_script_rejects_an_empty_cli_error(tmp_path: Path) -> None:
    _need_tools()
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
    proc = _run_script([sample], fake, tmp_path)
    assert proc.returncode != 0
    assert sample.name in (proc.stdout + proc.stderr)


def test_wasm_script_rejects_cli_success_when_wasm_errors(tmp_path: Path) -> None:
    _need_tools()
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
    proc = _run_script([garbage], fake, tmp_path)
    assert proc.returncode != 0
    assert garbage.name in (proc.stdout + proc.stderr)


@pytest.mark.parametrize("command", ["dng-half-codes", "dng-wb-block"])
@pytest.mark.parametrize("stdout", ["", "not JSON", '{"error":{"kind":"x","message":"y"}}'])
def test_wasm_script_rejects_bad_success_stdout(tmp_path: Path, command: str, stdout: str) -> None:
    """Exit 0 must still print one JSON result object; empty, invalid or error-shaped output fails."""
    _need_tools()
    sample = tmp_path / "valid.dng"
    sample.write_bytes(write_dng(_pattern(4, 6, (1, 2, 3, 4, 5, 6))))
    fake = tmp_path / "fake-quiet"
    _write_cli(
        fake,
        f"""#!/bin/sh
if [ "$1" = "{command}" ]; then
  {CLI} "$@" > /dev/null || exit $?
  printf '%s' '{stdout}'
  exit 0
fi
exec {CLI} "$@"
""",
    )
    proc = _run_script([sample], fake, tmp_path)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert sample.name in (proc.stdout + proc.stderr)


def test_wasm_refuses_strips_past_the_limit_like_native(tmp_path: Path) -> None:
    """65536 repeated strips add up to 2**32 bytes: WASM must give native's error, not trap (r4, R4-2)."""
    _need_tools()
    path = tmp_path / "strips-over-limit.dng"
    path.write_bytes(strips_over_the_limit())
    proc = _run_script([path], CLI, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
