"""同名照片（机主 2026-11-28）的 Node 单测：替换还是另外留下、按文件夹和拍摄时间配 RAW+JPG、头尾指纹、「名字 (2)」和导出文件名、
半格第 1 张怎么挑。驱动在 ``tests/support/mobile_samename_unit.mjs``，文件是内存里造的小 TIFF / JPEG，不读照片。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "support" / "mobile_samename_unit.mjs"


def test_mobile_samename_unit() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    result = subprocess.run(
        [node, "--experimental-strip-types", "--test", str(DRIVER)],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ℹ fail 0" in result.stdout or "# fail 0" in result.stdout, result.stdout
    assert "ℹ pass 17" in result.stdout or "# pass 17" in result.stdout, result.stdout
