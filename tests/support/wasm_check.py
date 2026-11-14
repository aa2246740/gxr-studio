"""Run ``mobile/scripts/wasm_vs_native.mjs`` for one area.

The driver compares one area's WASM exports with ``gxr-cli``. A missing WASM
package or a missing ``node`` skips the test, unless ``GXR_REQUIRE_CORE=1``,
in which case the test fails. ``GXR_REQUIRE_GPU_TEST`` is not consulted.

``run_area`` writes the input list as JSON, runs the driver, and on a non-zero
exit fails with the driver's own output.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "mobile" / "scripts" / "wasm_vs_native.mjs"
PKG = REPO / "mobile" / "src" / "wasm-pkg" / "gxr_wasm.js"
CLI = REPO / "core" / "target" / "release" / "gxr-cli"


def _missing(message: str) -> None:
    if os.environ.get("GXR_REQUIRE_CORE") == "1":
        pytest.fail(message)
    pytest.skip(message)


def run_area(
    area: str,
    inputs: list[Path],
    tmp_path: Path,
    *,
    cli: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Build the inputs JSON and run ``--area``.

    ``cli`` overrides the native binary (the one-sided-failure regressions use
    a fake). ``check`` fails the test with the driver's stdout and stderr.
    """
    if shutil.which("node") is None:
        _missing("node is not on PATH")
    if not PKG.is_file() or not SCRIPT.is_file():
        _missing(f"need {PKG.name}; run make core-wasm")
    if not CLI.is_file():
        _missing("need gxr-cli; run make core")
    binary = CLI if cli is None else cli
    if not binary.is_file():
        _missing(f"need {binary}")
    listed = tmp_path / "wasm-inputs.json"
    listed.write_text(json.dumps([str(path) for path in inputs]), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(SCRIPT), "--area", area, "--cli", str(binary), "--inputs", str(listed)],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if check and proc.returncode != 0:
        pytest.fail(proc.stdout + proc.stderr)
    return proc
