"""新手启动方式和设置里的开发用标定入口：文件在、能执行、内容对。

真正双击、点按钮的效果在浏览器走查里核对（research/studio_integration/）。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

STUDIO = Path(__file__).resolve().parents[1]
COMMAND = STUDIO / "GXR Studio.command"
ASSETS = STUDIO / "web" / "assets"


def test_command_file_is_double_clickable():
    text = COMMAND.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/bash\n")
    assert os.access(COMMAND, os.X_OK), "双击要能执行：chmod +x"
    assert subprocess.run(["bash", "-n", str(COMMAND)], capture_output=True).returncode == 0
    assert text.index("uv sync --frozen") < text.index("uv run --frozen gxr-studio")       # 先装依赖，再启动
    assert 'cd "$(dirname "$0")"' in text                                   # 从任意位置双击都在 studio/ 里干活
    assert '"$@"' in text                                                   # 终端里可以加 --no-browser 等参数


def test_readme_starts_with_three_steps():
    readme = (STUDIO / "README.md").read_text(encoding="utf-8")
    head = readme[: readme.index("## 更详细的说明")]
    assert "三步开始用" in head and "GXR Studio.command" in head
    assert head.count("\n1. ") == 1 and head.count("\n2. ") == 1 and head.count("\n3. ") == 1


def test_settings_has_a_plainly_labelled_dev_calibration_entry():
    camdata = (ASSETS / "camdata.js").read_text(encoding="utf-8")
    assert "导入颜色标定（开发用）" in camdata
    assert "目前只在开发这个工具的电脑上能用" in camdata
    assert "/api/calibration/import" in camdata
    onboarding = (ASSETS / "onboarding.js").read_text(encoding="utf-8")
    assert "calibrationImportForm" in onboarding and "cameraImportControls" in onboarding


def test_frontend_uses_the_endpoints_the_backend_really_has():
    """前端的导入走 import-card 和 import-upload（字节），不再把文件当 multipart 表单发给只认 JSON 路径的接口。"""
    camdata = (ASSETS / "camdata.js").read_text(encoding="utf-8")
    assert "/api/camdata/import-upload" in camdata and "/api/camdata/import-card" in camdata
    assert "FormData" not in camdata and "import-file" not in camdata
    assert "data_version" in camdata and "camdata-changed" in camdata


def test_crop_and_edit_share_one_revision():
    """裁切和配方/调整写同一份 sidecar：前端两边的版本号必须一起更新（否则先调色再裁切会被 409）。"""
    preview = (ASSETS / "preview.js").read_text(encoding="utf-8")
    crop = (ASSETS / "crop.js").read_text(encoding="utf-8")
    assert "export function syncRev" in preview
    assert preview.count("syncRev(view,") >= 3
    assert "syncRev(view, c.rev)" in crop
