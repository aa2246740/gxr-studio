"""手机端拍摄配方与「全部重置」的纯逻辑单元测试（Node 直接跑 TypeScript）。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RESET_UNIT = REPO / "tests" / "support" / "mobile_camera_recipe_reset_unit.mjs"
LOGIC_UNIT = REPO / "tests" / "support" / "mobile_camera_recipe_logic_unit.mjs"


def _run_node_test(path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    result = subprocess.run(
        [node, "--experimental-strip-types", "--test", str(path)],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0 or "ℹ fail 0" not in result.stdout:
        pytest.fail(result.stdout + result.stderr, pytrace=False)


def test_mobile_adjust_reset_clears_curve_and_knobs() -> None:
    _run_node_test(RESET_UNIT)


def test_mobile_camera_recipe_logic() -> None:
    _run_node_test(LOGIC_UNIT)
