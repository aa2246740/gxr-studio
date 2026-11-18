"""曲线页的纯逻辑：点的规则、预览 JSON、调整组的复制粘贴、拖动被打断。Node 直接跑 TypeScript。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UNIT = REPO / "tests" / "support" / "mobile_ui_curves_unit.mjs"


def test_mobile_ui_curves_gestures_paste_and_lifecycle() -> None:
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
