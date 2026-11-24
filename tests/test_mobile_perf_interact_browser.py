"""perf-interact 真实浏览器回归测试（`browser_serial`）。

覆盖：
1) 手机编辑器 WebGPU 滑块拖动 0 次同步布局回流；
2) 手机横屏（844×390）焦点顺序与视觉顺序一致（工具栏 + 存到相册在配方面板之前），转回竖屏还原；
3) 无 WebGPU 时（无 GPU Chromium）调整滑块有禁用样式与可见的单行说明，且在手机竖屏 844 高度下不被裁切；
4) 桌面端 gallery.js 前 16 张缩略图 eager + fetchpriority=high、离屏日期分组 content-visibility: auto、
   frame_logo_ui.js 中文输入法组合态按 Enter 不误提交。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from conftest import PORT, REAL_HOME
from gpu_support import find_chrome
from gxr_studio.app import create_app
from gxr_studio.camdata import calib_export, store
from support.mobile_ui_calib import pack_files
from test_api import import_all
from test_g250 import _need_g185_or_g250
from test_mobile_camera_recipe_browser import _card250_pair
from test_mobile_ui_browser import MODULES, PKG, RUNTIME, SAMPLES, _free_port, _need

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
WEB = REPO / "web"
DRIVER = REPO / "tests" / "support" / "mobile_perf_interact_browser.mjs"
FORBIDDEN_PORTS = {60887, 18772, 18801, 18802, 18803, 5290}


def _port() -> int:
    for _ in range(20):
        port = _free_port()
        if port not in FORBIDDEN_PORTS:
            return port
    pytest.fail("no free port")


def _stop(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)


def _redacted_tail(text: str) -> str:
    cleaned = re.sub(r"/(?:Volumes|Users|private|var/folders|tmp)/\S+", "[path]", text)
    return cleaned[-400:].strip()


def test_mobile_and_desktop_perf_interact_regressions(settings: Any, card: Any, tmp_path: Path) -> None:
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(find_chrome() is not None or RUNTIME.is_file(), "pinned Chrome is missing")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")

    raw250, lib250, dat250, fx250 = _need_g185_or_g250(250)
    dng1, jpg1 = _card250_pair(1)
    dng2, jpg2 = _card250_pair(3)

    _parsed, entries = store.entries_from_dat(raw250, library=lib250)
    pack = pack_files({name: entries[(unit, 4)] for unit, name in enumerate(("jlaunch", "qlaunch", "l08firm"))})
    pack_zip = tmp_path / "GXR 校准数据 2026-11-24.zip"
    pack_zip.write_bytes(calib_export.zip_bytes(pack, (2026, 11, 24, 9, 0, 0)))

    # 准备桌面端临时库（含 1 个已上传标志供管理弹窗重命名 IME 测试）
    logo_png = tmp_path / "mark.png"
    img = Image.new("RGBA", (200, 100), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle((10, 10, 190, 90), fill=(30, 40, 50, 255))
    img.save(logo_png, format="PNG")

    with TestClient(
        create_app(settings),
        base_url=f"http://127.0.0.1:{PORT}",
        headers={"X-GXR-Token": settings.token},
    ) as client:
        import_all(client)
        r = client.post(
            "/api/logos?name=旧标志&filename=mark.png&key_white=false&tol=30",
            content=logo_png.read_bytes(),
        )
        assert r.status_code == 200, r.text

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    desktop_port = int(sock.getsockname()[1])
    while desktop_port in FORBIDDEN_PORTS:
        sock.close()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        desktop_port = int(sock.getsockname()[1])

    server = uvicorn.Server(uvicorn.Config(create_app(settings.with_(port=desktop_port, web_dir=WEB)), log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()

    vite_port = _port()
    vite_log = tmp_path / "vite.log"
    vite: subprocess.Popen[bytes] | None = None
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started

        env = os.environ.copy()
        env["GXR_MOBILE_SAMPLES"] = str(SAMPLES)
        env["HOME"] = str(REAL_HOME)
        with vite_log.open("w", encoding="utf-8") as stream:
            vite = subprocess.Popen(
                ["node", str(MODULES / "vite" / "bin" / "vite.js"), "--host", "127.0.0.1", "--port", str(vite_port), "--strictPort"],
                cwd=REPO / "mobile",
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if vite.poll() is not None:
                pytest.fail(f"vite exited {vite.returncode}")
            probe = subprocess.run(
                [
                    "node",
                    "-e",
                    "fetch(process.argv[1]).then((r)=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))",
                    f"http://127.0.0.1:{vite_port}/",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if probe.returncode == 0:
                break
            time.sleep(0.4)
        else:
            pytest.fail("vite did not answer")

        manifest = {
            "mobileUrl": f"http://127.0.0.1:{vite_port}",
            "desktopUrl": f"http://127.0.0.1:{desktop_port}/?token={settings.token}",
            "files": [str(dng1), str(jpg1), str(dng2), str(jpg2), str(dat250), str(fx250)],
            "pack": str(pack_zip),
            "stem1": dng1.stem,
        }
        manifest_path = tmp_path / "perf-interact-manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        result = subprocess.run(
            ["node", str(DRIVER), str(RUNTIME), str(manifest_path)],
            cwd=REPO,
            env={**os.environ, "HOME": str(REAL_HOME)},
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    finally:
        _stop(vite)
        server.should_exit = True
        thread.join(timeout=20)
        sock.close()

    line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    assert line.startswith("{"), _redacted_tail(result.stdout + "\n" + result.stderr)
    report: dict[str, Any] = json.loads(line)
    assert result.returncode == 0, report.get("failures")
    assert report["ok"], report.get("failures")
    assert report["pageErrors"] == [], report["pageErrors"]
