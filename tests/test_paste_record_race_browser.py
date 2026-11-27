"""换照片后编辑记录还在读时点「粘贴调整」「选择复制哪些内容」：点击不丢（等这一次读取结束再做一次）。

合成假卡、固定的 Playwright 运行时；tests/support/paste_record_race_browser.mjs 扣住编辑记录的 GET 再放行，时序确定。
也测等的时候换走（不做）和读取失败（说出来）。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient

from conftest import PORT, REAL_HOME
from gxr_studio.app import create_app
from test_api import find, import_all

WEB = Path(__file__).resolve().parents[1] / "web"
DRIVER = Path(__file__).parent / "support" / "paste_record_race_browser.mjs"
ENV_RUNTIME = os.environ.get("GXR_PLAYWRIGHT_RUNTIME")
RUNTIME = Path(ENV_RUNTIME) if ENV_RUNTIME else REAL_HOME / ".codex" / "playwright-runtime" / "runtime.mjs"
NAMES = ("R0000102", "R0000103", "R0000104")

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or (not ENV_RUNTIME and not RUNTIME.is_file()),
    reason="需要固定的 Playwright 运行环境",
)


@pytest.fixture
def live(settings: Any, card: Any) -> Any:
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        import_all(client)
        photos = [find(client, n) for n in NAMES]
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
        yield {"port": port, "token": settings.token, "ids": [p["id"] for p in photos], "names": [p["name"] for p in photos]}
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        sock.close()


def test_copy_paste_click_during_record_load_is_kept(live: Any, tmp_path: Path) -> None:
    assert RUNTIME.is_file(), f"固定的 Playwright 运行时缺失（禁止回退系统 Chrome）：{RUNTIME}"
    result = subprocess.run(
        [
            "node",
            str(DRIVER),
            str(RUNTIME),
            str(live["port"]),
            live["token"],
            json.dumps(live["ids"]),
            json.dumps(live["names"]),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        encoding="utf-8",
        env={**os.environ, "HOME": str(REAL_HOME)},
    )
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert "error" not in out, out
    assert out["errors"] == [], out["errors"]
    assert out["busyWhenLoaded"] == [None, None], "读到记录后顶栏按钮不带 aria-busy"

    paste = out["scenarios"]["paste"]
    assert paste["loadedAtClick"] is False, f"点击时记录应还在读（扣住了 GET）：{paste}"
    assert paste["pastedWhileHeld"] == 0, paste
    assert paste["sent"] is True and paste["pasteCount"] == 1 and paste["target"] is True, f"读取期间点的粘贴丢了：{paste}"
    assert paste["busy"] == ["true", "true"], f"读取期间粘贴和复制选项带 aria-busy：{paste}"
    assert paste["recordGets"] == 1, f"等待不能另发一次记录请求：{paste}"
    assert paste["busyAfter"] == [None, None], paste

    options = out["scenarios"]["options"]
    assert options["loadedAtClick"] is False and options["openWhileHeld"] is False, options
    assert options["opened"] is True, f"读取期间点的复制选项丢了：{options}"

    away = out["scenarios"]["switchAway"]
    assert away == {"pasteCount": 0, "newErrors": 0, "newToasts": 0}, f"等的时候换走了，应当什么都不做：{away}"

    failed = out["scenarios"]["failed"]
    assert failed["shown"] is True and "无法读取编辑记录" in (failed["text"] or ""), f"读取失败时点粘贴要说明：{failed}"
    assert failed["pasteCount"] == 0 and failed["busyAfter"] == [None, None], failed
