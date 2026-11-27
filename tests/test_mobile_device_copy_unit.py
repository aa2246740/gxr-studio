"""电脑上的设备名和「这台…」短语（lib/device.ts）：手机、iPad 原话不变，电脑上说「这台电脑」。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from support.export_pipeline_cases import check

REPO = Path(__file__).resolve().parents[1]
UNIT = REPO / "tests" / "support" / "mobile_device_copy_unit.mjs"


def test_mobile_device_copy_units() -> None:
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
    check(result.returncode == 0, f"node:test mobile_device_copy_unit passed:\n{result.stdout}\n{result.stderr}")
