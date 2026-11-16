"""UI-M3 的纯逻辑（导出请求、批次状态机、共享分批、文件头里的宽高）。Node 直接跑 TypeScript，全是合成数据。

这个文件也在导出管线的隐私 lint 范围里（test_core_parity_export_pipeline_guard.py），所以不用裸 assert。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UNIT = REPO / "tests" / "support" / "mobile_ui_export_unit.mjs"


def test_mobile_ui_export_request_batch_and_share_units() -> None:
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
        pytest.fail("node:test export units failed; run tests/support/mobile_ui_export_unit.mjs with node --test", pytrace=False)
