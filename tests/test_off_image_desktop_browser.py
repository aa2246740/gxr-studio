"""桌面端「导出关机图」真浏览器验收测试（`browser_serial`）。"""

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
from typing import Any

import numpy as np
import pytest
import uvicorn
from PIL import Image

from conftest import REAL_HOME
from gxr_studio import off_image
from gxr_studio.app import create_app
from gxr_studio.cards.discovery import find_cards
from gxr_studio.cards.importer import ImportOptions, run_import
from gxr_studio.library.store import Library
from support import samples
from test_mobile_ui_browser import RUNTIME, _need

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
WEB = REPO / "web"
DRIVER = REPO / "tests" / "support" / "desktop_off_image_browser.mjs"

NOTE_CAMERA = "相机会把当天张数叠在关机画面上，下部别放重要内容。"
NOTE_CARD = "把 GXROFF.JPG 拷到存储卡最外层（和 GXRCE.DAT 放在一起）。"


def _make_off_image_script() -> Path:
    return samples.sample_parent() / "scripts" / "make_off_image.py"


def _run_ref_check(path: Path) -> str:
    script = _make_off_image_script()
    assert script.is_file(), f"missing reference checker: {script}"
    proc = subprocess.run(
        [sys.executable, "-I", str(script), "--check", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"--check failed: {proc.stdout} {proc.stderr}"
    return proc.stdout.strip()


def _synthetic_photo_jpg(dest: Path) -> None:
    y, x = np.mgrid[0:600, 0:900]
    arr = np.zeros((600, 900, 3), dtype=np.uint8)
    arr[..., 0] = (40 + x * 190 // 900).astype(np.uint8)
    arr[..., 1] = (50 + y * 170 // 600).astype(np.uint8)
    arr[..., 2] = (180 - (x + y) * 100 // 1500).astype(np.uint8)
    Image.fromarray(arr, mode="RGB").save(dest, "JPEG", quality=92)


def _synthetic_alpha_png(dest: Path) -> None:
    arr = np.zeros((500, 400, 4), dtype=np.uint8)
    arr[50:450, 40:360, 0] = 220
    arr[50:450, 40:360, 1] = 140
    arr[50:450, 40:360, 2] = 60
    arr[50:450, 40:360, 3] = 200
    Image.fromarray(arr, mode="RGBA").save(dest, "PNG")


def _run_driver(settings: Any, manifest: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    _need(shutil.which("node") is not None, "node is required for desktop browser test")
    _need(RUNTIME.is_file(), f"pinned Playwright runtime missing: {RUNTIME}")
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
        assert server.started, "desktop test server did not start"
        out_path = tmp_path / "desktop-off-image-out.json"
        m_path = tmp_path / "desktop-off-image-manifest.json"
        m_path.write_text(
            json.dumps({**manifest, "base": f"http://127.0.0.1:{port}", "token": settings.token, "out": str(out_path)}),
            encoding="utf-8",
        )
        proc = subprocess.run(
            ["node", str(DRIVER), str(RUNTIME), str(m_path)],
            cwd=str(DRIVER.parent),
            env={**os.environ, "HOME": str(REAL_HOME)},
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert proc.returncode == 0, f"driver failed (rc={proc.returncode}): {proc.stderr[-1500:]}"
        assert out_path.is_file()
        return json.loads(out_path.read_text(encoding="utf-8"))
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        sock.close()


def test_desktop_off_image_browser_end_to_end(settings: Any, lib: Library, volumes: Path, tmp_path: Path) -> None:
    card = volumes / "GXR_OFF_CARD"
    dcim = card / "DCIM" / "100RICOH"
    dcim.mkdir(parents=True)
    _synthetic_photo_jpg(dcim / "R0001001.JPG")
    settings.home_dir.mkdir(parents=True, exist_ok=True)
    (settings.home_dir / "flags.json").write_text(json.dumps({"gpu_preview": False}), encoding="utf-8")

    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    shot_id = str(lib.list_shots()[0]["shot_id"])
    lib.close()

    alt_png = tmp_path / "alt_alpha.png"
    _synthetic_alpha_png(alt_png)
    alt_out = tmp_path / "alt-export"
    alt_out.mkdir()

    report = _run_driver(
        settings,
        {
            "shotId": shot_id,
            "altPng": str(alt_png),
            "altOutDir": str(alt_out),
            "cardDestDir": str(card / "DCIM"),
        },
        tmp_path,
    )
    assert report["error"] is None
    assert report["pageErrors"] == []

    steps = report["steps"]
    assert steps["topExportLabel"] == "导出"
    assert steps["menuLabels"] == ["导出照片…", "做成关机图…"]

    init = steps["initial"]
    assert init["title"] == "导出关机图"
    assert init["naturalWidth"] == 640 and init["naturalHeight"] == 480
    assert init["srcIsDataUrl"] is True
    assert "640 × 480" in init["meta"] and "品质" in init["meta"]
    assert NOTE_CAMERA in init["notes"]
    assert NOTE_CARD in init["notes"]
    assert init["fileLabel"] == "GXROFF.JPG"
    assert init["exportDisabled"] is False

    exported_photo = settings.export_dir / "GXROFF.JPG"
    assert exported_photo.is_file()
    assert ": J7 ok" in _run_ref_check(exported_photo)
    assert off_image.check_off_image(exported_photo.read_bytes()).ok is True

    after = steps["afterReplace"]
    assert after["naturalWidth"] == 640 and after["naturalHeight"] == 480
    assert after["exportDisabled"] is False

    exported_alt = alt_out / "GXROFF.JPG"
    assert exported_alt.is_file()
    assert ": J7 ok" in _run_ref_check(exported_alt)
    assert exported_alt.read_bytes() == off_image.encode_off_image_bytes(alt_png.read_bytes()).data

    assert steps["cardRefusedError"]
    assert not (card / "GXROFF.JPG").exists()
    assert not (card / "DCIM" / "GXROFF.JPG").exists()

    fail = steps["forcedFail"]
    assert fail["exportDisabled"] is True
    assert fail["previewHidden"] is True
    assert "32768" in fail["error"]
