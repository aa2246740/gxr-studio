"""真实页面的 LUT 操作与首屏布局。拟合状态用接口夹具，算法另由 test_lut_page 验证。"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from conftest import PORT, REAL_HOME
from gxr_studio.app import create_app
from test_api import import_all
from test_lut_library import cube_bytes

WEB = Path(__file__).resolve().parents[1] / "web"
DRIVER = Path(__file__).parent / "support/lut_page_browser.mjs"
RUNTIME = Path(os.environ.get("GXR_PLAYWRIGHT_RUNTIME", str(REAL_HOME / ".codex/playwright-runtime/runtime.mjs")))

pytestmark = pytest.mark.skipif(shutil.which("node") is None or not RUNTIME.is_file(), reason="需要固定的 Playwright 运行环境")


@pytest.fixture
def live(settings, card):
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        import_all(client)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(settings.with_(port=port, web_dir=WEB)), log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        yield {"port": port, "token": settings.token}
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        sock.close()


def test_lut_defaults_units_save_and_above_fold_in_pinned_browser(live, tmp_path):
    for filename, title, comments in (
        ("Warm_Street.cube", "", ""),
        ("Declared.cube", "Declared look", "# Input: sRGB\n"),
        ("Guessed_sRGB.cube", "Guessed look", ""),
    ):
        (tmp_path / filename).write_bytes(cube_bytes(lambda x: 1 - x, title=title, comments=comments, size=5))
    result = subprocess.run(
        ["node", str(DRIVER), str(RUNTIME), str(live["port"]), live["token"], str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=120,
        encoding="utf-8",
        env={**os.environ, "HOME": str(REAL_HOME)},  # 只供浏览器定位全局固定运行环境；应用仍用临时 Settings。
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["default"] == {"space": "srgb", "source": "默认", "unit": "qlaunch"}
    assert out["request"]["unit"] == "jlaunch"
    assert out["request"]["space"] == "srgb" and out["request"]["space_guessed"]
    assert out["name"] == "Warm Street"
    assert out["saved"]["name"] == "Warm Street"
    assert out["saved"]["recipe"]["unit"] == "jlaunch"
    assert len(out["viewports"]) == 4
    assert all(v["visible"] and v["bottom"] <= v["height"] for v in out["viewports"])
    assert out["manual"] == {"space": "rec709", "source": "已手选"}
    assert out["declared"] == {"space": "srgb", "source": "文件已写明"}
    assert out["guessed"] == {"space": "srgb", "source": "推测"}
    assert out["calibratedUnits"] == ["qlaunch", "l08firm"]
    assert out["logLutUnits"] == [False, False, False, True]
    assert out["queryUnit"] == "l08firm"
    assert out["invalidQueryUnit"] == "l08firm"
    assert out["narrowOverflow"] is False, out["narrowOutliers"]
    assert out["photoRequests"] == 1, "初始化与导入应共用同一次照片读取"
    assert out["errors"] == []
