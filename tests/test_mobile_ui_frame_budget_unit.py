"""Sol final-r2 P2-05: the export page budget is checked before each photo starts (real ``ExportBatch``, synthetic results).

The regressions live in ``tests/support/mobile_ui_frame_budget_unit.mjs``; Node runs the TypeScript directly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UNIT = REPO / "tests" / "support" / "mobile_ui_frame_budget_unit.mjs"


def test_mobile_ui_frame_export_budget_preflight() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    result = subprocess.run(
        [node, "--experimental-strip-types", "--test", str(UNIT)],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0 or "ℹ fail 0" not in result.stdout:
        pytest.fail("node:test budget units failed; run tests/support/mobile_ui_frame_budget_unit.mjs with node --test", pytrace=False)
