"""显卡一致性测试：用无头 Chrome 的软件适配器（SwiftShader）跑 web/assets/gpu/engine.js，读回像素给 Python 比。

不需要显卡，CI 上也能跑。找不到 Chrome 时跳过；设了 `GXR_REQUIRE_GPU_TEST=1`（CI）时找不到就失败。
页面和数据由这里起的本机 HTTP 服务提供，只听 127.0.0.1，测试结束就关。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
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

_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def find_chrome() -> str | None:
    env = os.environ.get("GXR_CHROME")
    if env:
        return env
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        path = shutil.which(name)
        if path:
            return path
    for path in _CANDIDATES:
        if Path(path).is_file():
            return path
    return None


def require_chrome() -> str:
    chrome = find_chrome()
    if chrome is None:
        if os.environ.get("GXR_REQUIRE_GPU_TEST") == "1":
            pytest.fail("GXR_REQUIRE_GPU_TEST=1，但找不到 Chrome")
        pytest.skip("本机没有 Chrome，跳过显卡一致性测试")
    return chrome


class _Server:
    def __init__(self, files: dict[str, bytes], harness: Path = HARNESS) -> None:
        self.files = {"/": PAGE, "/harness.mjs": harness.read_bytes(), "/engine.js": ENGINE.read_bytes(), **files}
        self.results: dict[str, tuple[int, int, bytes]] = {}
        self.done = threading.Event()
        self.summary: dict[str, Any] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
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
                    outer.results[url.path[len("/result/") :]] = (int(q["w"][0]), int(q["h"][0]), body)
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
        "--enable-unsafe-webgpu",
        "--enable-unsafe-swiftshader",
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
            finished = srv.done.wait(timeout)
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
            pytest.fail(f"显卡一致性页面超时没有结束\n{detail}")
        if "error" in srv.summary:
            if os.environ.get("GXR_REQUIRE_GPU_TEST") != "1" and "no WebGPU adapter" in srv.summary["error"]:
                pytest.skip("这台机器的 Chrome 拿不到 WebGPU 软件适配器")
            pytest.fail(f"页面出错：{srv.summary['error']}\n{detail}")
        print(json.dumps(srv.summary))
        out = {k: np.frombuffer(b, np.uint8).reshape(h, w, 4)[..., :3].copy() for k, (w, h, b) in srv.results.items()}
        return out, srv.summary
