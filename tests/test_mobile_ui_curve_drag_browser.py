"""手机上拖曲线点：点跟着手指走，曲线盒子在拖动中不挪位。

在 390×844 和 430×932 里用真实触摸事件拖主曲线。旧版式会在按下时把盒子挪走，
这个回归必须在修之前失败。stdout 只留测量数字，不印照片名。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import REAL_HOME
from gpu_support import find_chrome
from support import samples
from test_mobile_ui_browser import MODULES, PKG, RUNTIME, SAMPLES, _free_port, _need, _pairs

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "support" / "mobile_ui_curve_drag_browser.mjs"
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


def test_curve_point_follows_the_finger(tmp_path: Path) -> None:
    files = _pairs()
    samples.need_sample(len(files) >= 2, f"need a DNG+JPG pair in {SAMPLES}")
    _need(len(files) >= 2, f"need a DNG+JPG pair in {SAMPLES}")
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(find_chrome() is not None or RUNTIME.is_file(), "pinned Chrome is missing")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")
    manifest = tmp_path / "files.json"
    manifest.write_text(json.dumps({"pairs": [str(files[0]), str(files[1])]}), encoding="utf-8")
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
            ["node", str(DRIVER), str(RUNTIME), f"http://127.0.0.1:{port}", str(manifest)],
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
    report: dict[str, Any] = json.loads(line) if line.startswith("{") else {"ok": False, "failures": ["driver produced no JSON"]}
    assert result.returncode == 0, report.get("failures")
    assert report["ok"], report.get("failures")
    assert report["pointerTypes"] == ["touch"]
    assert [row["viewport"] for row in report["runs"]] == ["390x844", "430x932"]
