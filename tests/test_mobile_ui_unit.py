"""EXIF 和分组的纯函数。Node 直接跑 TypeScript，不另装测试框架。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UNIT = REPO / "tests" / "support" / "mobile_ui_unit.mjs"


def test_mobile_ui_exif_and_grouping() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    result = subprocess.run(
        [node, "--experimental-strip-types", "--test", str(UNIT)],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
