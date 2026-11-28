"""手机横屏 LUT 面板适配与 320 竖屏导入报错间距的 WebKit 回归验收（`browser_serial`）。

覆盖：
1. 手机横屏（844×390 与 956×440，含安全区与无安全区）在空 LUT 库和导入 3 个 LUT 两种状态下：
   - 「不用 LUT」卡、全部已导入 LUT 卡、导入按钮、色彩空间按钮和空库说明均完整落在视口与 `.panel.lut` 框内；
   - LUT 面板内任何元素不与「配方 | LUT」切换键或照片舞台重叠，且各交互热区 ≥ 44pt；
   - 合并菜单瓷砖（`.lut-menu-tile`）在横屏下保持隐藏，不出现重复的导入按钮。
2. 320×568 手机竖屏空库状态下导入损坏的 `.cube` 文件时，报错条（`.lut-err`）底沿与空库说明（`.lut-foot`）顶沿之间
   至少留出 4 px 间距，且空库说明不超出面板或被裁切。
3. 手机竖屏（320/375/390/440）和 iPad（1024×1366、1366×1024）在两种状态下的每个框，和 5d972bf8 存下的基线
   （`support/lut_landscape_baseline.json`）相差不超过 1 px；有没有说明条都不出屏幕，也没有东西和卡片重叠。
4. 横屏的检查再在藏掉无显卡说明条的版式（有 WebGPU 的 Safari，面板更高）上做一遍。

仅使用合成 DNG+JPG 与合成 `.cube` 夹具，不接触真实照片；stdout 仅输出脱敏 JSON。
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
from raw_support import bayer_scene, build_dng
from support import g262_cases as g262
from support.dng_writer import COLOR_MATRIX
from test_mobile_ui_browser import MODULES, PKG, RUNTIME, SAMPLES, _free_port, _need

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
# 竖屏和 iPad 的框在 5d972bf8 上量出来的值（固定 WebKit）。横屏改动不许动它们。
BASELINE = Path(__file__).resolve().parent / "support" / "lut_landscape_baseline.json"
BOX_TOLERANCE = 1.0
BOX_KEYS = (
    "panel",
    "stage",
    "band",
    "bar",
    "dock",
    "ipadMeta",
    "notice",
    "sw",
    "wrap",
    "pin",
    "strip",
    "importBtn",
    "spaceBtn",
    "menuTile",
    "foot",
    "err",
)
FORBIDDEN_PORTS = {60887, 18772, 18801, 18802, 18803, 5290}

DRIVER = Path(__file__).resolve().parent / "support" / "mobile_lut_landscape_webkit.mjs"


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


def _boxes(measure: dict[str, Any]) -> dict[str, list[float] | None]:
    """一次 measureLutPanel 里的每个框（x、y、w、h），卡片按顺序和名字记。"""
    out: dict[str, list[float] | None] = {}
    for key in BOX_KEYS:
        value = measure[key]
        out[key] = None if value is None else [value["x"], value["y"], value["w"], value["h"]]
    for i, card in enumerate(measure["cards"]):
        box = card["box"]
        out[f"card{i}:{card['name']}"] = None if box is None else [box["x"], box["y"], box["w"], box["h"]]
    return out


def _box_drift(want: dict[str, Any], got: dict[str, Any]) -> list[str]:
    drift: list[str] = []
    for state, views in want.items():
        for label, boxes in views.items():
            now = _boxes(got[state][label])
            for key in sorted(set(boxes) | set(now)):
                a, b = boxes.get(key, "missing"), now.get(key, "missing")
                same = a == b or (
                    isinstance(a, list) and isinstance(b, list) and all(abs(x - y) <= BOX_TOLERANCE for x, y in zip(a, b, strict=True))
                )
                if not same:
                    drift.append(f"{state} {label} {key}: {a} -> {b}")
    return drift


def _cube(title: str, shift: float) -> str:
    lines = [f'TITLE "{title}"', "# Input: sRGB", "LUT_3D_SIZE 2"]
    for b in (0, 1):
        for g in (0, 1):
            for r in (0, 1):
                rr = min(1.0, max(0.0, r * (1.0 - shift) + shift))
                lines.append(f"{rr:.3f} {float(g):.3f} {float(b):.3f}")
    return "\n".join(lines) + "\n"


def _synth_pair(out_dir: Path, stem: str) -> tuple[Path, Path]:
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
    when = "2026:11:26 08:00:00"
    dng_bytes = build_dng(
        bayer_scene(height, width, scene * np.array(neutral), white=4095),
        bps=16,
        white=4095,
        neutral=neutral,
        orientation=1,
        iso=200,
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
    sub[34855], sub[36867], sub[36868], sub[42036] = 200, when, when, g262.LENS_50
    rgb8 = np.clip(np.round(np.power(scene, 1.0 / 2.2) * 255.0), 0, 255).astype(np.uint8)
    Image.fromarray(rgb8, "RGB").save(jpg_path, "JPEG", quality=92, exif=exif)
    return dng_path, jpg_path


def test_mobile_lut_landscape_and_320_error_spacing_webkit(tmp_path: Path) -> None:
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")

    dng_path, jpg_path = _synth_pair(tmp_path, "R9980201")
    cubes = []
    for idx, (fname, title, shift) in enumerate(
        (
            ("warm_a.cube", "Warm A", 0.04),
            ("cool_b.cube", "Cool B", 0.08),
            ("film_c.cube", "Film C", 0.12),
        )
    ):
        cube_path = tmp_path / fname
        cube_path.write_text(_cube(title, shift + idx * 0.01), encoding="utf-8")
        cubes.append(str(cube_path))
    broken_cube = tmp_path / "broken.cube"
    broken_cube.write_text('TITLE "Broken"\nLUT_3D_SIZE 2\nnot a number\n', encoding="utf-8")

    manifest: dict[str, Any] = {
        "files": [str(dng_path), str(jpg_path)],
        "stem": dng_path.stem,
        "cubes": cubes,
        "brokenCube": str(broken_cube),
        "shotsDir": os.environ.get("GXR_LUT_LANDSCAPE_SHOTS_DIR", ""),
        "shotsPrefix": os.environ.get("GXR_LUT_LANDSCAPE_SHOTS_PREFIX", "after"),
    }
    manifest_path = tmp_path / "manifest.json"
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
            timeout=240,
        )
    finally:
        _stop(vite)

    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    assert lines, _redacted_tail(result.stdout + "\n" + result.stderr)
    report: dict[str, Any] = json.loads(lines[-1])
    if report.get("skipped"):
        pytest.skip(str(report["skipped"]))

    out_report = os.environ.get("GXR_LUT_LANDSCAPE_REPORT", "")
    if out_report:
        Path(out_report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    assert result.returncode == 0, report.get("failures")
    assert report["ok"], report.get("failures")
    assert report["pageErrors"] == [], report["pageErrors"]
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))["boxes"]
    assert {state: sorted(views) for state, views in baseline.items()} == {
        state: sorted(report["summary"]["baseline"][state]) for state in ("empty", "three")
    }
    drift = _box_drift(baseline, report["summary"]["baseline"])
    assert drift == [], "portrait / iPad LUT boxes moved more than 1 px from 5d972bf8:\n" + "\n".join(drift)
