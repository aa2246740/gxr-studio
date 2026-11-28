"""The dev server must not follow a symlink out of the sample or report directory."""

from __future__ import annotations

import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

from support.dng_writer import write_dng

pytestmark = [pytest.mark.browser_serial, pytest.mark.browser_alone]

REPO = Path(__file__).resolve().parents[1]


def _need(ok: bool, message: str) -> None:
    if ok:
        return
    if os.environ.get("GXR_REQUIRE_CORE") == "1":
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


def _http(
    port: int,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    content_type: str = "application/json",
) -> tuple[int, bytes]:
    """Talk to Vite from a child process. The pytest process itself stays offline."""
    script = """
const [url, method, payload, contentType] = process.argv.slice(1);
const headers = payload ? { 'content-type': contentType } : {};
const body = payload ? Buffer.from(payload, 'base64') : undefined;
fetch(url, { method, body, headers })
  .then(async (response) => {
    const text = Buffer.from(await response.arrayBuffer()).toString('base64');
    process.stdout.write(JSON.stringify({ status: response.status, body: text }));
  })
  .catch((err) => {
    process.stdout.write(JSON.stringify({ status: 0, body: '', error: String(err) }));
    process.exitCode = 1;
  });
"""
    proc = subprocess.run(
        [
            "node",
            "-e",
            script,
            f"http://127.0.0.1:{port}{path}",
            method,
            "" if body is None else base64.b64encode(body).decode("ascii"),
            content_type,
        ],
        check=False,
        capture_output=True,
        timeout=15,
    )
    if not proc.stdout:
        return 0, proc.stderr
    payload = json.loads(proc.stdout)
    return int(payload["status"]), base64.b64decode(payload["body"])


def test_dev_samples_and_reports_do_not_follow_symlinks(tmp_path: Path) -> None:
    node_modules = REPO / "mobile" / "node_modules"
    _need(node_modules.is_dir(), "mobile/node_modules is missing")
    vite_bin = REPO / "mobile" / "node_modules" / ".bin" / "vite"
    _need(vite_bin.is_file() and shutil.which("node") is not None, "vite and node are required")
    samples = tmp_path / "samples"
    samples.mkdir()
    real = samples / "plain.dng"
    real.write_bytes(write_dng(np.zeros((4, 6), dtype=np.uint16)))
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret-bytes")
    (samples / "escape.dng").symlink_to(outside)
    linked = tmp_path / "linked-dir"
    linked.mkdir()
    (linked / "hidden.dng").write_bytes(b"hidden")
    (samples / "alias").symlink_to(linked, target_is_directory=True)

    reports = REPO / "mobile" / ".reports"
    reports.mkdir(exist_ok=True)
    secret = tmp_path / "secret.json"
    secret.write_text("untouched", encoding="utf-8")
    link = reports / "guard.json"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(secret)
    secret_bin = tmp_path / "secret.bin"
    secret_bin.write_bytes(b"untouched-bin")
    link_bin = reports / "guard.bin"
    if link_bin.is_symlink() or link_bin.exists():
        link_bin.unlink()
    link_bin.symlink_to(secret_bin)

    port = _free_port()
    env = os.environ.copy()
    env["GXR_MOBILE_SAMPLES"] = str(samples)
    log = tmp_path / "vite.log"
    vite: subprocess.Popen[str] | None = None
    try:
        with log.open("w", encoding="utf-8") as stream:
            vite = subprocess.Popen(
                [str(vite_bin), "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
                cwd=REPO / "mobile",
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.monotonic() + 60
        ready = False
        while time.monotonic() < deadline:
            if vite.poll() is not None:
                pytest.fail(f"vite exited {vite.returncode}\n{log.read_text(encoding='utf-8', errors='replace')[-2000:]}")
            status, _body = _http(port, "/")
            if status == 200:
                ready = True
                break
            time.sleep(0.4)
        if not ready:
            pytest.fail("vite did not answer")

        status, body = _http(port, "/__dev/samples")
        assert status == 200
        assert json.loads(body) == ["plain.dng"]
        status, escaped = _http(port, "/__dev/sample/escape.dng")
        assert status == 404
        assert escaped != b"secret-bytes"
        status, _body = _http(port, "/__dev/sample/..%2Foutside.txt")
        assert status == 404
        status, _body = _http(port, "/__dev/sample/%2Fetc%2Fpasswd.dng")
        assert status == 404
        posted, _body = _http(port, "/__dev/report/guard", method="POST", body=b'{"ok":true}')
        assert posted == 400
        assert secret.read_text(encoding="utf-8") == "untouched"
        posted_bin, _body = _http(
            port,
            "/__dev/report/guard",
            method="POST",
            body=b"\x01\x00\x00\x00\x01\x00\x00\x00\xff\x00\x00",
            content_type="application/octet-stream",
        )
        assert posted_bin == 400
        assert secret_bin.read_bytes() == b"untouched-bin"
    finally:
        _stop(vite)
        if link.is_symlink() or link.exists():
            link.unlink()
        if link_bin.is_symlink() or link_bin.exists():
            link_bin.unlink()


def test_mobile_check_rejects_leaked_ricoh_content_in_ordinary_file() -> None:
    from conftest import REAL_HOME

    dist_assets = REPO / "mobile" / "dist" / "assets"
    _need(dist_assets.is_dir(), "mobile/dist/assets is missing; run make mobile-check first")
    env = os.environ.copy()
    for candidate in (
        REAL_HOME / ".cache" / "node" / "corepack",
        REAL_HOME / "Library" / "Caches" / "node" / "corepack",
    ):
        if candidate.is_dir():
            env.setdefault("COREPACK_HOME", str(candidate))
            break
    env.setdefault("COREPACK_ENABLE_NETWORK", "0")
    rustup = shutil.which("rustup")
    if rustup:
        toolchain = (REPO / "core" / "rust-toolchain.toml").read_text(encoding="utf-8")
        for line in toolchain.splitlines():
            if line.startswith("channel"):
                channel = line.split('"')[1]
                which_cargo = subprocess.run(
                    [rustup, "which", "--toolchain", channel, "cargo"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if which_cargo.returncode == 0 and which_cargo.stdout.strip():
                    env["PATH"] = f"{Path(which_cargo.stdout.strip()).parent}:{env.get('PATH', '')}"
                break

    clean = subprocess.run(
        ["make", "-o", "mobile-build", "mobile-check"],
        cwd=REPO,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert clean.returncode == 0, clean.stdout + clean.stderr

    for payload in ("Ricoh_logo_2005.svg", "ricoh.png"):
        bait = dist_assets / "ordinary-bundle-chunk.js"
        try:
            bait.write_text(f'const x = "{payload}";\n', encoding="utf-8")
            bad = subprocess.run(
                ["make", "-o", "mobile-build", "mobile-check"],
                cwd=REPO,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert bad.returncode != 0, f"mobile-check failed to catch {payload!r} inside {bait.name}"
        finally:
            if bait.exists():
                bait.unlink()

    after = subprocess.run(
        ["make", "-o", "mobile-build", "mobile-check"],
        cwd=REPO,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert after.returncode == 0, after.stdout + after.stderr
