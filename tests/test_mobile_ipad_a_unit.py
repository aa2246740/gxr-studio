"""iPad 方案 A + 手机横屏单元测试（版式分类、直方图默认记忆、胶片条分组、文案核对）。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from support.export_pipeline_cases import check

REPO = Path(__file__).resolve().parents[1]
UNIT = REPO / "tests" / "support" / "mobile_ipad_a_unit.mjs"


def test_mobile_ipad_a_units() -> None:
    node = shutil.which("node")
    check(node is not None, "node is required")
    assert node is not None
    result = subprocess.run(
        [node, "--experimental-strip-types", "--test", str(UNIT)],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    check(result.returncode == 0, f"node:test mobile_ipad_a_unit passed:\n{result.stdout}\n{result.stderr}")
