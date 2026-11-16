"""UI-M3-HDR 的纯逻辑（开关、混着只有 JPG 的那一行、记住的选择、请求里的 hdr、结果那一行、HDR 批次的待存上限）。
Node 直接跑 TypeScript，全是合成数据。

这个文件也在导出管线的隐私 lint 范围里（test_core_parity_export_pipeline_guard.py），所以不用裸 assert。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UNIT = REPO / "tests" / "support" / "mobile_ui_export_hdr_unit.mjs"


def test_mobile_ui_export_hdr_units() -> None:
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
        pytest.fail("node:test HDR export units failed; run tests/support/mobile_ui_export_hdr_unit.mjs with node --test", pytrace=False)
