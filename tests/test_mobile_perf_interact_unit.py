"""perf-interact 单元测试（Node --experimental-strip-types，覆盖 6 项改动并在旧基线 a112f60 上必败）。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UNIT = REPO / "tests" / "support" / "mobile_perf_interact_unit.mjs"
UNIT_R2 = REPO / "tests" / "support" / "mobile_perf_interact_r2_unit.mjs"
UNIT_N5 = REPO / "tests" / "support" / "mobile_perf_interact_n5_unit.mjs"


def test_perf_interact_units() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    result = subprocess.run(
        [node, "--experimental-strip-types", "--test", str(UNIT), str(UNIT_R2), str(UNIT_N5)],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
