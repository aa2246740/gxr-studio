"""desktop-import 的 Node 单测（2026-11-26）：拖进文件夹的展开（嵌套、点开头、层数和文件数上限），没用上的文件怎么数、怎么说，
只选 GXRFX.BIN 的提示。驱动在 ``tests/support/mobile_desktop_import_unit.mjs``，不读照片。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "support" / "mobile_desktop_import_unit.mjs"


def test_mobile_desktop_import_unit() -> None:
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
    assert "ℹ pass 8" in result.stdout or "# pass 8" in result.stdout, result.stdout
