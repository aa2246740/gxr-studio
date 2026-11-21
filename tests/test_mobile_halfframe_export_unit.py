"""phone-halfframe-export：半格导出模式偏好、单格切分、MPF/标签解析与多文件排队单测。"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UNIT_SCRIPT = REPO / "tests" / "support" / "mobile_halfframe_export_unit.mjs"


def test_mobile_halfframe_export_unit() -> None:
    proc = subprocess.run(
        ["node", "--experimental-strip-types", "--test", str(UNIT_SCRIPT)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"mobile_halfframe_export_unit 失败:\n{proc.stdout}\n{proc.stderr}"
