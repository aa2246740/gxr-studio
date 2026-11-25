"""手机版导览 r2 三项视觉缺陷的 WebKit 布局与状态回归验收（`browser_serial`）。

覆盖：
1. 状态瓷砖「校准范围外（推算）」在 320/375/390/440 竖屏与横屏下最多两行，且「（推算）」不断行；点击仍可打开说明表。
2. 「配方 | LUT」切换键拥有独立空间，不与配方/LUT 横条或任何卡片重叠（320–440 竖屏与手机横屏）。
3. 导出就绪条显示蓝色「存进相册」时，底栏「存到相册」退为次级非蓝按钮（当前照片已就绪时进一步降权并更新无障碍说明，
   切到另一张可继续排队导出）；用户点击就绪条分享后恢复主蓝按钮。

本机未安装 Playwright WebKit 时以清晰原因跳过。stdout 仅输出脱敏 JSON。
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

import numpy as np
import pytest
from PIL import Image

from conftest import REAL_HOME
from gxr_studio.camdata import calib_export, store
from raw_support import bayer_scene, build_dng
from support import g262_cases as g262
from support.dng_writer import COLOR_MATRIX
from support.mobile_ui_calib import pack_files
from test_g250 import _need_g185_or_g250
from test_mobile_ui_browser import MODULES, PKG, RUNTIME, SAMPLES, _free_port, _need

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "support" / "mobile_ux_tour_fixes_webkit.mjs"
FORBIDDEN_PORTS = {60887, 18772, 18801, 18802, 18803, 5290}


def _port() -> int:
    for _ in range(20):
        port = _free_port()
        if port not in FORBIDDEN_PORTS:
            return port
    pytest.fail("no free port")


def _stop(proc: subprocess.Popen[bytes] | None) -> None:
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
    return cleaned[-500:].strip()


def _synth_pair(out_dir: Path, stem: str, *, iso: int, when: str) -> tuple[Path, Path]:
    """构造 50mm 合成 DNG+JPG（ISO 1250 触发「校准范围外（推算）」，ISO 200 作为第二张对比导出）。"""
    height, width = 128, 192
    yy, xx = np.mgrid[0:height, 0:width]
    scene = np.stack(
        [
            0.14 + 0.56 * (xx / (width - 1)),
            0.18 + 0.44 * (yy / (height - 1)),
            0.24 + 0.34 * (1.0 - xx / (width - 1)),
        ],
        axis=-1,
    )
    neutral = [0.65, 1.0, 0.58]
    dng_bytes = build_dng(
        bayer_scene(height, width, scene * np.array(neutral), white=4095),
        bps=16,
        white=4095,
        neutral=neutral,
        orientation=1,
        iso=iso,
        when=when,
        lens=f"RICOH {g262.LENS_50}",
        exif_entries=[(42036, "ASCII", g262.LENS_50)],
        root_extra=[
            (50721, "SRATIONAL", COLOR_MATRIX),
            (50727, "RATIONAL", [1.0, 1.0, 1.0]),
            (50778, "SHORT", [21]),
        ],
    )
    dng_path, jpg_path = out_dir / f"{stem}.DNG", out_dir / f"{stem}.JPG"
    dng_path.write_bytes(dng_bytes)
    exif = Image.Exif()
    exif[271], exif[272], exif[274], exif[306] = "RICOH", "GXR A12", 1, when
    sub = exif.get_ifd(0x8769)
    sub[34855], sub[36867], sub[36868], sub[42036] = iso, when, when, g262.LENS_50
    rgb8 = np.clip(np.round(np.power(scene, 1.0 / 2.2) * 255.0), 0, 255).astype(np.uint8)
    Image.fromarray(rgb8, "RGB").save(jpg_path, "JPEG", quality=92, exif=exif)
    return dng_path, jpg_path


def test_mobile_ux_tour_fixes_webkit(tmp_path: Path) -> None:
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")

    raw250, lib250, dat250, fx250 = _need_g185_or_g250(250)
    iso_dng, iso_jpg = _synth_pair(tmp_path, "R9980101", iso=1250, when="2026:11:25 10:00:00")
    sec_dng, sec_jpg = _synth_pair(tmp_path, "R9980102", iso=200, when="2026:11:25 10:01:00")

    _parsed, entries = store.entries_from_dat(raw250, library=lib250)
    all_units = ("jlaunch", "qlaunch", "l08firm")
    pack = pack_files({name: entries[(unit, 4)] for unit, name in enumerate(all_units)})
    pack_zip = tmp_path / "GXR 校准数据 2026-11-25.zip"
    pack_zip.write_bytes(calib_export.zip_bytes(pack, (2026, 11, 25, 10, 0, 0)))

    manifest: dict[str, Any] = {
        "files": [str(iso_dng), str(iso_jpg), str(sec_dng), str(sec_jpg), str(dat250), str(fx250)],
        "pack": str(pack_zip),
        "isoOutStem": iso_dng.stem,
        "secondStem": sec_dng.stem,
        "shotsDir": os.environ.get("GXR_UX_TOUR_SHOTS_DIR", ""),
        "shotsPrefix": os.environ.get("GXR_UX_TOUR_SHOTS_PREFIX", "after"),
    }
    manifest_path = tmp_path / "ux-tour-fixes-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    port = _port()
    env = os.environ.copy()
    env["GXR_MOBILE_SAMPLES"] = str(SAMPLES)
    env["HOME"] = str(REAL_HOME)
    log = tmp_path / "vite.log"
    vite: subprocess.Popen[bytes] | None = None
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
            timeout=360,
        )
    finally:
        _stop(vite)

    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    assert lines, _redacted_tail(result.stdout + "\n" + result.stderr)
    report: dict[str, Any] = json.loads(lines[-1])
    if report.get("skipped"):
        pytest.skip(str(report["skipped"]))

    out_report = os.environ.get("GXR_UX_TOUR_REPORT", "")
    if out_report:
        Path(out_report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    assert result.returncode == 0, report.get("failures")
    assert report["ok"], report.get("failures")
    assert report["pageErrors"] == [], report["pageErrors"]
