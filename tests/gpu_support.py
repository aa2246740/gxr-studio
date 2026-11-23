"""显卡一致性测试：用无头 Chrome 的软件适配器（SwiftShader）跑 web/assets/gpu/engine.js，读回像素给 Python 比。

不需要显卡，CI 上也能跑。浏览器用固定版本的 Playwright Chromium（GXR_CHROME 或运行时里的那份），
找不到时跳过；设了 `GXR_REQUIRE_GPU_TEST=1`（CI）时找不到就失败。
页面和数据由这里起的本机 HTTP 服务提供，只听 127.0.0.1，测试结束就关。
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "web" / "assets" / "gpu" / "engine.js"
HARNESS = Path(__file__).resolve().parent / "support" / "gpu_harness.mjs"
PAGE = b'<!doctype html><meta charset="utf-8"><script type="module" src="/harness.mjs"></script>'
TYPES = {".js": "text/javascript", ".mjs": "text/javascript", ".json": "application/json", ".jpg": "image/jpeg", ".png": "image/png"}

# Linux 和 Windows 上的系统浏览器查找（Windows 适配暂停，代码保留）。macOS 不用，见 find_chrome。
_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)
_PATH_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")
# 导入时的 HOME，和 conftest.REAL_HOME 一样：测试里会把 HOME 换成临时目录。
_REAL_HOME = Path.home()
_IS_MAC = sys.platform == "darwin"
_PINNED_PATH_SCRIPT = "const m = await import(process.argv[1]); process.stdout.write(String(m.pinnedRuntime?.executablePath ?? ''));"


def pinned_runtime() -> Path:
    """固定版本的 Playwright 运行时（runtime.mjs）。GXR_PLAYWRIGHT_RUNTIME 优先。"""
    configured = os.environ.get("GXR_PLAYWRIGHT_RUNTIME")
    return Path(configured) if configured else _REAL_HOME / ".codex" / "playwright-runtime" / "runtime.mjs"


@functools.cache
def _pinned_executable(runtime: str) -> str | None:
    """问运行时它固定的 Chromium 在哪（pinnedRuntime.executablePath）；没有运行时或 Node 就是 None。"""
    node = shutil.which("node")
    if node is None or not Path(runtime).is_file():
        return None
    try:
        result = subprocess.run(  # noqa: S603
            [node, "--input-type=module", "-e", _PINNED_PATH_SCRIPT, Path(runtime).as_uri()],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    path = result.stdout.strip()
    return path if result.returncode == 0 and path and Path(path).is_file() else None


def find_chrome() -> str | None:
    """测试用的 Chrome：GXR_CHROME，否则固定版本 Playwright 运行时里的 Chromium。

    macOS 上不再退回本机装的 Google Chrome。它每次启动都会把整个应用复制到内置盘的系统临时目录
    （code_sign_clone，约 1.4 GB），进程没有正常退出时副本就留下（2026-11 积了 432 份）。
    Linux 和 Windows 仍按原来的顺序找系统浏览器（CI 的 Linux 任务靠它）。
    """
    env = os.environ.get("GXR_CHROME")
    if env:
        return env
    pinned = _pinned_executable(str(pinned_runtime()))
    if pinned:
        return pinned
    if _IS_MAC:
        return None
    for name in _PATH_NAMES:
        path = shutil.which(name)
        if path:
            return path
    for path in _CANDIDATES:
        if Path(path).is_file():
            return path
    return None


def require_chrome() -> str:
    if os.environ.get("GXR_PLAYWRIGHT_RUNTIME") and not os.environ.get("GXR_CHROME"):
        pytest.fail("设置 GXR_PLAYWRIGHT_RUNTIME 后必须显式设置 GXR_CHROME，禁止退回系统浏览器。")
    chrome = find_chrome()
    if chrome is None:
        if os.environ.get("GXR_REQUIRE_GPU_TEST") == "1":
            pytest.fail("GXR_REQUIRE_GPU_TEST=1，但找不到固定版本的 Chromium（设 GXR_CHROME）")
        pytest.skip("本机没有固定版本的 Chromium（设 GXR_CHROME 或装好 Playwright 运行时），跳过显卡一致性测试")
    return chrome


class _Server:
    def __init__(self, files: dict[str, bytes], harness: Path = HARNESS) -> None:
        self.files = {"/": PAGE, "/harness.mjs": harness.read_bytes(), "/engine.js": ENGINE.read_bytes(), **files}
        self.results: dict[str, tuple[int, int, bytes]] = {}
        self.kinds: dict[str, str] = {}
        self.done = threading.Event()
        self.summary: dict[str, Any] = {}
        self.progress: dict[str, Any] = {}
        self.requests: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                outer.requests.append(path)
                body = outer.files.get(path)
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", TYPES.get(Path(path).suffix, "text/html" if path == "/" else "application/octet-stream"))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                url = urlparse(self.path)
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if url.path.startswith("/result/"):
                    q = parse_qs(url.query)
                    key = url.path[len("/result/") :]
                    outer.results[key] = (int(q["w"][0]), int(q["h"][0]), body)
                    outer.kinds[key] = q.get("fmt", ["u8"])[0]
                elif url.path == "/progress":
                    outer.progress = json.loads(body)
                elif url.path == "/done":
                    outer.summary = json.loads(body or b"{}")
                    outer.done.set()
                self.send_response(204)
                self.end_headers()

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            request_queue_size = 64

        self.httpd = Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> _Server:
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/"


def run_engine(
    tmp_path: Path, files: dict[str, bytes], cases: list[dict[str, Any]], timeout: float = 180.0, harness: Path = HARNESS
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    """跑一组用例，返回 {用例 id: (h, w, 3) uint8} 和页面的总结（适配器信息或错误）。"""
    chrome = require_chrome()
    files = {**files, "/cases.json": json.dumps(cases).encode()}
    args = [
        chrome,
        "--headless=new",
        f"--user-data-dir={tmp_path / 'chrome'}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        # 与现有浏览器驱动相同：临时 HOME 下不让 macOS Chrome 等待系统钥匙串。
        "--use-mock-keychain",
        "--password-store=basic",
        "--enable-unsafe-webgpu",
        "--enable-unsafe-swiftshader",
        "--use-angle=swiftshader",
        "--use-webgpu-adapter=swiftshader",
    ]
    if sys.platform.startswith("linux") and os.environ.get("CI"):
        args += ["--no-sandbox", "--disable-dev-shm-usage"]
    if sys.platform == "win32":
        # Windows 的 WebGPU 初始化先向 ANGLE 取 D3D11 设备；ANGLE 用 WARP，WebGPU 仍选 SwiftShader。
        args += [
            "--use-gl=angle",
            "--use-angle=d3d11-warp",
            "--enable-logging",
            f"--log-file={tmp_path / 'chrome-internal.log'}",
        ]
    with _Server(files, harness) as srv, (tmp_path / "chrome.log").open("wb") as stderr:
        proc = subprocess.Popen([*args, srv.url], stdout=subprocess.DEVNULL, stderr=stderr)  # noqa: S603
        try:
            deadline = time.monotonic() + timeout
            while not srv.done.wait(min(0.1, max(0.0, deadline - time.monotonic()))):
                if proc.poll() is not None or time.monotonic() >= deadline:
                    break
            finished, exit_code = srv.done.is_set(), proc.poll()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        detail = (tmp_path / "chrome.log").read_text(encoding="utf-8", errors="replace")[-6000:]
        internal = tmp_path / "chrome-internal.log"
        if internal.is_file():
            detail += internal.read_text(encoding="utf-8", errors="replace")[-10000:]
        if not finished:
            context = {"phase": srv.progress, "completed": list(srv.results), "requests": srv.requests, "exit_code": exit_code}
            reason = "显卡一致性浏览器提前退出" if exit_code is not None else "显卡一致性页面超时没有结束"
            pytest.fail(f"{reason}\n{json.dumps(context, ensure_ascii=False)}\n{detail}")
        if "error" in srv.summary:
            if os.environ.get("GXR_REQUIRE_GPU_TEST") != "1" and "no WebGPU adapter" in srv.summary["error"]:
                pytest.skip("这台机器的 Chrome 拿不到 WebGPU 软件适配器")
            pytest.fail(f"页面出错：{srv.summary['error']}\n{detail}")
        print(json.dumps(srv.summary))
        out: dict[str, np.ndarray] = {}
        for key, (w, h, body) in srv.results.items():
            if srv.kinds.get(key) == "f32":
                out[key] = np.frombuffer(body, np.float32).reshape(h, w, 4).copy()
            else:
                out[key] = np.frombuffer(body, np.uint8).reshape(h, w, 4)[..., :3].copy()
        return out, srv.summary
