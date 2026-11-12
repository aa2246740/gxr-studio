"""The WASM build and gxr-cli must print the same bytes for the same DNGs.

Node loads ``mobile/src/wasm-pkg`` (``make core-wasm``) and runs
``mobile/scripts/wasm_vs_native.mjs``. Missing pieces skip, unless
``GXR_REQUIRE_CORE=1``, in which case the test fails.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from test_core_parity_raw import REAL

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "mobile" / "scripts" / "wasm_vs_native.mjs"
PKG = REPO / "mobile" / "src" / "wasm-pkg" / "gxr_wasm.js"
CLI = REPO / "core" / "target" / "release" / "gxr-cli"


def _missing(message: str) -> None:
    if os.environ.get("GXR_REQUIRE_CORE") == "1":
        pytest.fail(message)
    pytest.skip(message)


def test_wasm_describe_and_half_codes_match_cli(tmp_path: Path) -> None:
    if not REAL:
        _missing("no real DNGs for the wasm comparison")
    if shutil.which("node") is None:
        _missing("node is not on PATH")
    if not PKG.is_file() or not CLI.is_file() or not SCRIPT.is_file():
        _missing(f"need {PKG.name} and gxr-cli; run make core-wasm and make core")
    listed = tmp_path / "dngs.json"
    listed.write_text(json.dumps([str(path) for path in REAL]), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(SCRIPT), str(listed), str(CLI)],
        check=False,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
