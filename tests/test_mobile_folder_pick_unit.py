"""「选文件夹」的 Node 单测（2026-11-28）：._ 和别的点开头文件在分类时跳过（拖和选都走这里），选文件夹拿到的文件按拖文件夹的规矩筛
（点开头的路径段、层数和文件数上限），留下的是原来的对象。驱动在 ``tests/support/mobile_folder_pick_unit.mjs``，不读照片。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "support" / "mobile_folder_pick_unit.mjs"


def test_mobile_folder_pick_unit() -> None:
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
    assert "ℹ pass 6" in result.stdout or "# pass 6" in result.stdout, result.stdout
