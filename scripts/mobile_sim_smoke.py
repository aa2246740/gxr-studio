#!/usr/bin/env python3
"""在已经存在的 iOS 模拟器里打开开发壳的自动测试，把截图和回执留在报告目录。

不创建新模拟器。截图先写到 /tmp，再复制出来：直接写到 /Volumes 会被系统拒绝。
用完关掉开发服务器，再关掉模拟器。不进 make check。
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

UDID = "06C44EFA-9591-4DC6-85A1-AECC7BE98E2C"
PORT = 5199


def _stop(proc: subprocess.Popen[str] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = args.report
    report.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["GXR_MOBILE_SAMPLES"] = str(args.samples.resolve())
    vite: subprocess.Popen[str] | None = None
    try:
        subprocess.run(["xcrun", "simctl", "boot", UDID], check=False)
        subprocess.run(["xcrun", "simctl", "bootstatus", UDID, "-b"], check=True)
        vite = subprocess.Popen(
            ["pnpm", "-C", str(repo / "mobile"), "exec", "vite", "--host", "127.0.0.1", "--port", str(PORT), "--strictPort"],
            cwd=repo,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if vite.poll() is not None:
                raise SystemExit(f"vite exited {vite.returncode}")
            probe = subprocess.run(
                ["node", "-e", "fetch(process.argv[1]).then((r)=>process.exit(0)).catch(()=>process.exit(1))", f"http://127.0.0.1:{PORT}/"],
                check=False,
            )
            if probe.returncode == 0:
                break
            time.sleep(0.4)
        else:
            raise SystemExit("vite did not answer")
        subprocess.run(["xcrun", "simctl", "openurl", UDID, f"http://127.0.0.1:{PORT}/?autotest=1&report=simulator"], check=True)
        written = repo / "mobile" / ".reports" / "simulator.json"
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline and not written.is_file():
            time.sleep(0.5)
        if not written.is_file():
            raise SystemExit("simulator did not post a report within 180s")
        kept = report / "simulator.json"
        shutil.copyfile(written, kept)
        shot = Path("/tmp") / "gxr-mobile-sim.png"
        subprocess.run(["xcrun", "simctl", "io", UDID, "screenshot", str(shot)], check=True)
        shutil.copyfile(shot, report / "simulator.png")
        print(f"report {kept}")
        print(f"screenshot {report / 'simulator.png'}")
        return 0
    finally:
        _stop(vite)
        subprocess.run(["xcrun", "simctl", "shutdown", UDID], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
