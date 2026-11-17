"""HDR 预览的条件、生命周期和拖动时不重算。Node 跑 TypeScript，合成数据。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UNIT = REPO / "tests" / "support" / "mobile_ui_hdr_preview_unit.mjs"


def test_mobile_ui_hdr_preview_units() -> None:
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
        pytest.fail(result.stdout + result.stderr, pytrace=False)
