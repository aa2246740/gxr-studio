"""编辑器 P3-18（2026-11-29 backlog-4b）：顶栏「配方 + LUT 名」、未写明输入色彩空间的 LUT 面板先标默认（sRGB）。

Node --experimental-strip-types + mini_dom，见 `tests/support/mobile_p318_unit.mjs`。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UNIT = REPO / "tests" / "support" / "mobile_p318_unit.mjs"


def test_mobile_p318_units() -> None:
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
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
