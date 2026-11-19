"""U1 phone-fast-export 单元测试（prefs 默认值与持久化、就绪条文案、队列内存预算）。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from support.export_pipeline_cases import check

REPO = Path(__file__).resolve().parents[1]
UNIT = REPO / "tests" / "support" / "mobile_fast_export_unit.mjs"


def test_mobile_fast_export_units() -> None:
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
    check(result.returncode == 0, "node:test mobile_fast_export_unit passed")
