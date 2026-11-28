"""27 寸大屏单元测试：layout.ts 的门槛、分列、缩略图盒子和配方卡长边，queue.ts 的大缩略图字节上限。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from support.export_pipeline_cases import check

REPO = Path(__file__).resolve().parents[1]
UNIT = REPO / "tests" / "support" / "mobile_big_screen_unit.mjs"


def test_mobile_big_screen_units() -> None:
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
    check(result.returncode == 0, f"node:test mobile_big_screen_unit passed:\n{result.stdout}\n{result.stderr}")
