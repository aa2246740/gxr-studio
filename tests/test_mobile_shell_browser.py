"""开发壳在无头 Chrome 里走一遍：WASM 描述、显卡出图、读回不是一片纯色。

SwiftShader 上的帧时间没有意义，报告里记下来，不断言。服务器和浏览器在结束时关掉。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from gpu_support import find_chrome
from gxr_studio.gpu.source import describe
from gxr_studio.raw.dng import Dng
from test_core_parity_raw import _same

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
PARENT = Path(os.environ.get("GXR_STUDIO_TEST_PARENT", str(REPO.parent)))
SAMPLES = PARENT / "captures/a12-50/2026-10-19/stage2c-first-run"
PKG = REPO / "mobile" / "src" / "wasm-pkg" / "gxr_wasm.js"
FLAGS = [
    "--headless=new",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    "--use-mock-keychain",
    "--password-store=basic",
    "--enable-unsafe-webgpu",
    "--enable-unsafe-swiftshader",
    "--use-angle=swiftshader",
    "--use-webgpu-adapter=swiftshader",
]


def _need(ok: bool, message: str) -> None:
    if ok:
        return
    if os.environ.get("GXR_REQUIRE_CORE") == "1" or os.environ.get("GXR_REQUIRE_GPU_TEST") == "1":
        pytest.fail(message)
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


def _wait_http(port: int, proc: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"vite exited {proc.returncode}")
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


def test_mobile_shell_autotest(tmp_path: Path) -> None:
    dngs = sorted(path for path in SAMPLES.iterdir() if path.is_file() and path.suffix.upper() == ".DNG") if SAMPLES.is_dir() else []
    _need(bool(dngs), f"no DNGs in {SAMPLES}")
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(shutil.which("pnpm") is not None and shutil.which("node") is not None, "pnpm and node are required")
    chrome = find_chrome()
    _need(chrome is not None, "pinned Chrome is missing")
    assert chrome is not None
    sample = dngs[0]
    port = _free_port()
    name = f"shell{os.getpid()}"
    report_path = REPO / "mobile" / ".reports" / f"{name}.json"
    report_path.unlink(missing_ok=True)
    env = os.environ.copy()
    env["GXR_MOBILE_SAMPLES"] = str(SAMPLES)
    log = tmp_path / "vite.log"
    vite: subprocess.Popen[str] | None = None
    browser: subprocess.Popen[str] | None = None
    try:
        with log.open("w", encoding="utf-8") as stream:
            vite = subprocess.Popen(
                ["pnpm", "-C", str(REPO / "mobile"), "exec", "vite", "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
                cwd=REPO,
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        _wait_http(port, vite)
        browser = subprocess.Popen(
            [
                chrome,
                *FLAGS,
                f"--user-data-dir={tmp_path / 'chrome'}",
                f"http://127.0.0.1:{port}/?autotest=1&report={name}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline and not report_path.is_file():
            if browser.poll() is not None and not report_path.is_file():
                break
            time.sleep(0.5)
        if not report_path.is_file():
            pytest.fail(f"no autotest report\n{log.read_text(encoding='utf-8', errors='replace')[-4000:]}")
        report = json.loads(report_path.read_text(encoding="utf-8"))
    finally:
        _stop(browser)
        _stop(vite)
        report_path.unlink(missing_ok=True)

    assert report["pageErrors"] == []
    assert report["error"] is None
    assert report["webgpu"] is True
    assert report["frame"] is not None
    readback = report["readback"]
    assert readback is not None and readback["min"] != readback["max"]
    python = asdict(describe(Dng(sample), with_scene=True))
    _same(python, {"layout": report["layout"], "reason": report["reason"], "scene": report["scene"]})
    assert "perf" in report
