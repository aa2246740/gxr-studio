"""手机单带编辑器（U2 phone-editor-band）真实浏览器验收测试（`browser_serial`）。

只用本地 card250 实拍照片和合成校准包；stdout 只打印结构化度量与点击路径，绝不打印照片名、拍摄时间或 EXIF。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import REAL_HOME
from gpu_support import find_chrome
from gxr_studio.camdata import calib_export, store
from support.mobile_ui_calib import pack_files
from test_g250 import _need_g185_or_g250
from test_mobile_camera_recipe_browser import _card250_pair
from test_mobile_ui_browser import MODULES, PKG, RUNTIME, SAMPLES, _free_port, _need

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "support" / "mobile_editor_band_browser.mjs"
FORBIDDEN_PORTS = {60887, 18772, 18801, 18802, 18803, 5290}


def _port() -> int:
    for _ in range(20):
        port = _free_port()
        if port not in FORBIDDEN_PORTS:
            return port
    pytest.fail("no free port")


def _stop(proc: subprocess.Popen[str] | None) -> None:
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
    return cleaned[-300:].strip()


def _write_minimal_cube(path: Path) -> None:
    lines = ['TITLE "TestWarm"', "LUT_3D_SIZE 2"]
    for b in (0, 1):
        for g in (0, 1):
            for r in (0, 1):
                lines.append(f"{min(1.0, r * 0.95 + 0.04):.4f} {g:.4f} {b * 0.92:.4f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_mobile_editor_band_photo_rect_reachability_and_behaviour(tmp_path: Path) -> None:
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(find_chrome() is not None or RUNTIME.is_file(), "pinned Chrome is missing")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")

    raw250, lib250, dat250, fx250 = _need_g185_or_g250(250)
    # card250: #3 与 #5 为 2:3 竖图，#1 为 3:2 横图
    dng_p1, jpg_p1 = _card250_pair(3)
    dng_l1, jpg_l1 = _card250_pair(1)
    dng_p2, jpg_p2 = _card250_pair(5)

    _parsed, entries = store.entries_from_dat(raw250, library=lib250)
    all_units = ("jlaunch", "qlaunch", "l08firm")
    pack = pack_files({name: entries[(unit, 4)] for unit, name in enumerate(all_units)})
    pack_zip = tmp_path / "GXR 校准数据 2026-11-18.zip"
    pack_zip.write_bytes(calib_export.zip_bytes(pack, (2026, 11, 18, 9, 0, 0)))

    lut_path = tmp_path / "test_warm.cube"
    _write_minimal_cube(lut_path)

    from PIL import Image, ImageOps

    def _aspect(path: Path) -> float:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            return im.width / im.height

    screenshots_dir = os.environ.get("GXR_EDITOR_BAND_SCREENSHOTS", "")
    manifest: dict[str, Any] = {
        "files": [
            str(dng_p1),
            str(jpg_p1),
            str(dng_l1),
            str(jpg_l1),
            str(dng_p2),
            str(jpg_p2),
            str(dat250),
            str(fx250),
        ],
        "pack": str(pack_zip),
        "lut": str(lut_path),
        "portrait1Stem": dng_p1.stem,
        "landscapeStem": dng_l1.stem,
        "portrait2Stem": dng_p2.stem,
        "portraitAspect": _aspect(jpg_p1),
        "landscapeAspect": _aspect(jpg_l1),
        "screenshotsDir": screenshots_dir,
    }
    manifest_path = tmp_path / "editor-band-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    port = _port()
    env = os.environ.copy()
    env["GXR_MOBILE_SAMPLES"] = str(SAMPLES)
    env["HOME"] = str(REAL_HOME)
    log = tmp_path / "vite.log"
    vite: subprocess.Popen[str] | None = None
    try:
        with log.open("w", encoding="utf-8") as stream:
            vite = subprocess.Popen(
                ["node", str(MODULES / "vite" / "bin" / "vite.js"), "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
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
                    f"http://127.0.0.1:{port}/",
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
        result = subprocess.run(
            ["node", str(DRIVER), str(RUNTIME), f"http://127.0.0.1:{port}", str(manifest_path)],
            cwd=REPO,
            env={**os.environ, "HOME": str(REAL_HOME)},
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
        )
    finally:
        _stop(vite)

    line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if line.startswith("{"):
        report: dict[str, Any] = json.loads(line)
    else:
        report = {"ok": False, "failures": [f"driver produced no JSON rc={result.returncode} {_redacted_tail(result.stderr)}".strip()]}

    out_report = os.environ.get("GXR_EDITOR_BAND_REPORT", "")
    if out_report:
        Path(out_report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    assert result.returncode == 0, report.get("failures")
    assert report["ok"], report.get("failures")
