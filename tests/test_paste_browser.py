"""真浏览器里的复制 / 粘贴调整：⇧⌘C / ⇧⌘V、编辑器粘贴一次撤销、图库批量粘贴（进度、结果说明、缩略图、撤销）、输入框里不触发。

起一个真服务器（合成的假卡，只听 127.0.0.1）和无头 Chrome，由 tests/support/paste_browser.mjs 用 DevTools 协议按键点按。
找不到 Chrome 或 node 时跳过。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from conftest import PORT
from gpu_support import require_chrome
from gxr_studio.app import create_app
from test_api import import_all

WEB = Path(__file__).resolve().parents[1] / "web"
DRIVER = Path(__file__).parent / "support" / "paste_browser.mjs"
DEADLINE_S = 240

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过真浏览器测试")


@pytest.fixture
def live(settings, card):
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        import_all(c)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(settings.with_(port=port, web_dir=WEB)), log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "服务器没有起来"
    yield {"port": port, "token": settings.token}
    server.should_exit = True
    thread.join(timeout=20)
    sock.close()


def drive(live, tmp_path) -> dict:
    chrome = require_chrome()
    flags = ["--no-sandbox", "--disable-dev-shm-usage"] if sys.platform.startswith("linux") and os.environ.get("CI") else []
    env = {**os.environ, "CHROME_FLAGS": json.dumps(flags), "PASTE_DEADLINE_MS": str(DEADLINE_S * 1000)}
    try:
        res = subprocess.run(  # noqa: S603
            ["node", str(DRIVER), chrome, str(live["port"]), live["token"], str(tmp_path / "chrome-paste")],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=DEADLINE_S + 40,
            encoding="utf-8",
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"驱动脚本没有在时限内结束\n{exc.stderr}")
    assert res.stdout.strip(), res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert "error" not in out, f"{json.dumps(out, ensure_ascii=False, indent=1)}\n进度：\n{res.stderr[-3000:]}"
    return out  # type: ignore[no-any-return]


def test_copy_and_paste_in_a_real_browser(live, tmp_path):
    out = drive(live, tmp_path)
    assert out["ready"] is True
    # 编辑器：顶栏有按钮；没复制之前「粘贴调整」是灰的；⇧⌘C 复制并说明
    assert "复制调整" in out["titlebar"] and "粘贴调整" in out["titlebar"]
    assert out["pasteDisabledBeforeCopy"] == "true" and out["pasteDisabledAfterCopy"] is None
    assert out["copyToast"].startswith("已复制：配方 / 风格、调整") and out["clipFrom"] == "R0000101"
    # 在输入框里打字时两个键都不触发（不复制、不粘贴、不弹提示）
    assert out["typing"] == {"newToasts": 0, "newPosts": 0, "clipFrom": "R0000101"}
    # 快捷键表里写了新键
    copy_keys, paste_keys = ("⇧⌘C", "⇧⌘V") if out["platform"] == "mac" else ("Ctrl+Shift+C", "Ctrl+Shift+V")  # 和应用一样按平台写
    assert copy_keys in out["helpText"] and paste_keys in out["helpText"]
    # 编辑器里 ⇧⌘V：一次请求，写进去了；撤销一次就全部回到粘贴之前
    assert out["editorPosts"] == 1 and out["editorToast"].startswith("已套用：配方 / 风格、调整")
    assert out["afterEditorPaste"] == {"contrast": 8, "slot": 2, "curve": True}
    assert out["afterOneUndo"] == {"contrast": 0, "slot": None, "curve": False}
    # 图库：选中全部，选择栏上有粘贴按钮；分批发送，进度在走；结束后说明结果并给撤销
    assert f"粘贴调整到 {out['photoCount']} 张" in out["selbar"]
    total = out["photoCount"] - 1  # 复制来源那一张不粘贴
    assert out["progress"] == [f"正在粘贴调整… 0 / {total}", f"正在粘贴调整… 10 / {total}"]
    assert out["progressBar"] and out["progressGone"], (out["progressBar"], out["progressGone"])
    assert out["batchPosts"] == 2, "每 10 张一次请求"
    assert out["resultToast"].startswith(f"已套用到 {total} 张；")
    assert "张只有 JPG，没有套用配方" in out["resultToast"], "只有 JPG 的照片没有套用配方，如实说"
    assert out["resultHasUndo"]
    assert out["afterBatch"]["target"] == {"contrast": 8, "slot": 2, "curve": True}
    assert out["afterBatch"]["jpgOnly"] == {"contrast": 8, "slot": None, "curve": True}, "调整套用了，配方没有"
    assert out["thumbAfter"] != out["thumbBefore"], "缩略图地址带上了新的版本号，会重新加载"
    # 撤销整批：每张都回到粘贴之前；选择还在
    assert out["undoToast"].startswith(f"已撤销 {total} 张")
    assert out["afterUndo"] == {
        "target": {"contrast": 0, "slot": None, "curve": False},
        "jpgOnly": {"contrast": 0, "slot": None, "curve": False},
    }
    assert out["thumbRestored"] != out["thumbAfter"] and out["selectionKept"]
    assert out["console"] == [], out["console"]
