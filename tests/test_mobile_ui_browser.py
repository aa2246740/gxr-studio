"""手机界面在无头 Chrome 里走一遍。SwiftShader 的帧时间只记下来，不断言。

服务器和浏览器在结束时关掉。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest

from conftest import REAL_HOME
from gpu_support import find_chrome
from test_mobile_shell_browser import core_prerequisite

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
PARENT = Path(os.environ.get("GXR_STUDIO_TEST_PARENT", str(REPO.parent)))
SAMPLES = PARENT / "captures/a12-50/2026-10-19/stage2c-first-run"
PKG = REPO / "mobile" / "src" / "wasm-pkg" / "gxr_wasm.js"
MODULES = REPO / "mobile" / "node_modules"
RUNTIME = Path(os.environ.get("GXR_PLAYWRIGHT_RUNTIME", str(REAL_HOME / ".codex/playwright-runtime/runtime.mjs")))
DRIVER = REPO / "tests" / "support" / "mobile_ui_browser.mjs"


def _need(ok: bool, message: str) -> None:
    action = core_prerequisite(
        ok,
        require_core=os.environ.get("GXR_REQUIRE_CORE") == "1",
        require_gpu=os.environ.get("GXR_REQUIRE_GPU_TEST") == "1",
    )
    if action == "fail":
        pytest.fail(message)
    if action == "skip":
        pytest.skip(message)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _stop(proc: subprocess.Popen[str] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)


def _pairs() -> list[Path]:
    if not SAMPLES.is_dir():
        return []
    found: list[Path] = []
    dngs = sorted(path for path in SAMPLES.iterdir() if path.is_file() and path.suffix.upper() == ".DNG")
    for dng in dngs:
        jpg = next((dng.with_suffix(ext) for ext in (".JPG", ".jpg") if dng.with_suffix(ext).is_file()), None)
        if jpg is None:
            continue
        found.extend((dng, jpg))
        if len(found) >= 12:
            break
    return found


def test_mobile_ui_pick_grid_edit_and_bad_file(tmp_path: Path) -> None:
    files = _pairs()
    # 地基用的那份样本只有 4 对。够 6 对就取 6 对，不够就用现有的。
    _need(len(files) >= 2, f"need a DNG+JPG pair in {SAMPLES}")
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("pnpm") is not None and shutil.which("node") is not None, "pnpm and node are required")
    _need(find_chrome() is not None or RUNTIME.is_file(), "pinned Chrome is missing")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")
    bad = tmp_path / "R9990001.DNG"
    bad.write_bytes(b"not a tiff")
    dat = tmp_path / "GXRCE.DAT"
    dat.write_bytes(b"dat")
    manifest = tmp_path / "files.json"
    manifest.write_text(json.dumps({"pairs": [str(path) for path in [*files, dat]], "bad": str(bad)}), encoding="utf-8")
    port = _free_port()
    env = os.environ.copy()
    env["GXR_MOBILE_SAMPLES"] = str(SAMPLES)
    log = tmp_path / "vite.log"
    vite: subprocess.Popen[str] | None = None
    try:
        with log.open("w", encoding="utf-8") as stream:
            vite_bin = REPO / "mobile" / "node_modules" / "vite" / "bin" / "vite.js"
            vite = subprocess.Popen(
                ["node", str(vite_bin), "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
                cwd=REPO / "mobile",
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if vite.poll() is not None:
                pytest.fail(f"vite exited {vite.returncode}\n{log.read_text(encoding='utf-8', errors='replace')[-2000:]}")
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
            ["node", str(DRIVER), str(RUNTIME), f"http://127.0.0.1:{port}/?perf=1", str(manifest)],
            cwd=REPO,
            env={**os.environ, "HOME": str(REAL_HOME)},
            check=False,
            capture_output=True,
            text=True,
            timeout=240,
        )
    finally:
        _stop(vite)
    assert result.returncode == 0, result.stdout + result.stderr + log.read_text(encoding="utf-8", errors="replace")[-2000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["pageErrors"] == []
    assert report["groups"] >= 1
    assert report["orderBefore"]
    assert report["orderAfter"] == list(reversed(report["orderBefore"]))
    assert report["perf"]["samples"] > 0
    assert "原图" in report["held"]
    assert report["swipe"][0] != report["swipe"][1]
    assert "读不了" in report["bad"]
    assert "从这次去掉" in report["bad"]
