"""网格空闲预烘焙（`mobile/src/app/prebake.ts`）真实浏览器验收（`browser_serial`）。

手机竖屏（4 个 Worker）和 iPad 竖屏（3 个 Worker）：网格静下来以后预烘焙做完；打开第一张时舞台的表和第一屏每一张卡的表
都是打开之前就做好的，点下去同步换好；网格阶段同时在跑的烘焙不超过 workers - 2 张。只用本地 card250 实拍照片和 g250 的
相机配方文件；stdout 只有结构化 JSON，不打印照片名、拍摄时间或 EXIF。
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
DRIVER = REPO / "tests" / "support" / "mobile_prebake_browser.mjs"
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
    return cleaned[-400:].strip()


def _wait_vite(vite: subprocess.Popen[str], port: int) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if vite.poll() is not None:
            pytest.fail(f"vite exited {vite.returncode}")
        probe = subprocess.run(
            ["node", "-e", "fetch(process.argv[1]).then((r)=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))", f"http://127.0.0.1:{port}/"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if probe.returncode == 0:
            return
        time.sleep(0.4)
    pytest.fail("vite did not answer")


def test_mobile_grid_prebake_makes_first_switch_warm(tmp_path: Path) -> None:
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(find_chrome() is not None or RUNTIME.is_file(), "pinned Chrome is missing")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")

    raw250, lib250, dat250, fx250 = _need_g185_or_g250(250)
    pairs = [_card250_pair(1), _card250_pair(3)]
    _parsed, entries = store.entries_from_dat(raw250, library=lib250)
    pack = pack_files({name: entries[(unit, 4)] for unit, name in enumerate(("jlaunch", "qlaunch", "l08firm"))})
    pack_zip = tmp_path / "GXR 校准数据 2026-11-28.zip"
    pack_zip.write_bytes(calib_export.zip_bytes(pack, (2026, 11, 28, 9, 0, 0)))

    manifest: dict[str, Any] = {
        "files": [str(p) for pair in pairs for p in pair] + [str(dat250), str(fx250)],
        "photos": len(pairs),
        "pack": str(pack_zip),
    }
    manifest_path = tmp_path / "prebake-manifest.json"
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
        _wait_vite(vite, port)
        result = subprocess.run(
            ["node", str(DRIVER), str(RUNTIME), f"http://127.0.0.1:{port}", str(manifest_path)],
            cwd=REPO,
            env={**os.environ, "HOME": str(REAL_HOME)},
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    finally:
        _stop(vite)

    line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if line.startswith("{"):
        report: dict[str, Any] = json.loads(line)
    else:
        report = {"ok": False, "failures": [f"driver produced no JSON rc={result.returncode} {_redacted_tail(result.stderr)}".strip()]}
    print(json.dumps(report.get("summary", {}), ensure_ascii=False))
    assert result.returncode == 0, report.get("failures")
    assert report.get("ok") is True, report.get("failures")
    assert report.get("pageErrors") == [], report.get("pageErrors")
