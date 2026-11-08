"""真实导出表单在 Node 中运行，文件夹选择和导出接口用替身。"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from test_edit_route import SUPPORT, prepare


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node")
def test_destination_picker_and_export_validation(tmp_path):
    folder = prepare(tmp_path)
    script = tmp_path / "export_destination.mjs"
    shutil.copy(SUPPORT / script.name, script)
    result = subprocess.run(["node", str(script), str(folder)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
