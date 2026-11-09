"""在固定的真实 Chromium 中拖 RGB 曲线，核对图像、保存、切换、重置和撤销。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import REAL_HOME
from test_paste_browser import live as live

DRIVER = Path(__file__).parent / "support" / "rgb_curves_browser.mjs"
RUNTIME = REAL_HOME / ".codex/playwright-runtime/runtime.mjs"


def test_rgb_curve_switch_drag_save_reset_and_undo_in_chromium(live, tmp_path):
    if shutil.which("node") is None or not RUNTIME.is_file():
        pytest.skip("需要本机固定的 Codex Playwright 运行时；不安装或切换浏览器")
    result = subprocess.run(  # noqa: S603
        ["node", str(DRIVER), str(RUNTIME), f"http://127.0.0.1:{live['port']}/?token={live['token']}"],  # noqa: S607
        # 应用服务器仍用测试 HOME；只有固定运行时在其实际安装位置启动浏览器。
        env={**os.environ, "HOME": str(REAL_HOME)},
        capture_output=True,
        text=True,
        timeout=240,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["blueChangedImage"] and out["blueChangedHistogram"]
    assert out["blueSaveKeys"] == ["b"] and out["blueMarked"]
    assert out["switchKeptPoints"] and out["ghosts"] == ["g", "master", "r"]
    assert out["doubleClickRemovedOnlyBlue"] and out["resetOneKeptOthers"]
    assert out["undoKeptAll"] and out["redoResetAll"] and out["reloadedAll"]
    assert out["pageErrors"] == [] and out["httpErrors"] == []
