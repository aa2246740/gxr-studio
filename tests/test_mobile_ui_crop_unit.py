"""UI-M4：裁剪预设、参考线、手势状态机与编辑提交规则的 Node 单元测试。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UNIT = REPO / "tests" / "support" / "mobile_ui_crop_unit.mjs"


def test_mobile_ui_crop_unit() -> None:
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
    if result.returncode != 0 or ("ℹ fail 0" not in result.stdout and "# fail 0" not in result.stdout):
        pytest.fail("node:test crop units failed; run tests/support/mobile_ui_crop_unit.mjs with node --test", pytrace=False)
