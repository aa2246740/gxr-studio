"""UI-M6：1376×1032、1032×1376、600×1032。不把照片名写进断言信息。"""

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
from support import samples
from test_mobile_shell_browser import core_prerequisite

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
PARENT = samples.sample_parent()
SAMPLES = PARENT / "captures/a12-50/2026-10-19/stage2c-first-run"
PKG = REPO / "mobile" / "src" / "wasm-pkg" / "gxr_wasm.js"
MODULES = REPO / "mobile" / "node_modules"
RUNTIME = Path(os.environ.get("GXR_PLAYWRIGHT_RUNTIME", str(REAL_HOME / ".codex/playwright-runtime/runtime.mjs")))
DRIVER = REPO / "tests" / "support" / "mobile_ui_ipad_browser.mjs"
SHOTS = Path("/Volumes/Workspace/Scratch/gxr-studio-hillclimb/round-6/mobile-ui-m6-ipad/report/shots")


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
        if len(found) >= 8:
            break
    return found


def test_ipad_layouts_edit_rotate_and_export(tmp_path: Path) -> None:
    files = _pairs()
    samples.need_sample(len(files) >= 2, "need a DNG+JPG pair")
    _need(len(files) >= 2, "need a DNG+JPG pair")
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(find_chrome() is not None or RUNTIME.is_file(), "pinned Chrome is missing")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")
    manifest = tmp_path / "files.json"
    manifest.write_text(json.dumps({"pairs": [str(path) for path in files]}), encoding="utf-8")
    SHOTS.mkdir(parents=True, exist_ok=True)
    port = 0
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
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
                pytest.fail(f"vite exited {vite.returncode}\n{log.read_text(encoding='utf-8', errors='replace')[-1500:]}")
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
            ["node", str(DRIVER), str(RUNTIME), f"http://127.0.0.1:{port}/", str(manifest), str(SHOTS)],
            cwd=REPO,
            env={**os.environ, "HOME": str(REAL_HOME)},
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    finally:
        if vite is not None and vite.poll() is None:
            os.killpg(vite.pid, signal.SIGTERM)
            try:
                vite.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(vite.pid, signal.SIGKILL)
                vite.wait(timeout=10)
    if result.returncode != 0:
        tail = log.read_text(encoding="utf-8", errors="replace")[-1500:]
        pytest.fail(result.stdout + "\n" + result.stderr + "\n" + tail)
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["pageErrors"] == []
    assert report.get("failed") in (None, "")
    for key in ("landscape", "slider", "crop", "lut", "frame", "hold", "copy", "portrait", "split", "return", "midDrag", "midExport"):
        assert report["checks"].get(key) is True, key
