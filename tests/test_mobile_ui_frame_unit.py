"""UI-M4 边框、字体、日期戳与标志纯函数的 Node 单元测试。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UNIT = REPO / "tests" / "support" / "mobile_ui_frame_unit.mjs"


LIFECYCLE = REPO / "tests" / "support" / "mobile_ui_frame_lifecycle_unit.mjs"


def test_mobile_ui_frame_unit() -> None:
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
    assert "ℹ fail 0" in result.stdout or "# fail 0" in result.stdout, result.stdout


def test_mobile_ui_frame_lifecycle_unit() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    result = subprocess.run(
        [node, "--experimental-strip-types", "--test", str(LIFECYCLE)],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ℹ fail 0" in result.stdout or "# fail 0" in result.stdout, result.stdout
