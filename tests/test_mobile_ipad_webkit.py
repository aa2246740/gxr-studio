"""iPad Safari / WebKit 无 WebGPU 缺陷回归验收测试（D1–D7，browser_serial）。

在固定 Playwright WebKit（hasTouch=True, isMobile=True）下覆盖 1032×1376、1376×1032、
820×1180、手机横屏 844×390 与手机竖屏 390×844：
- D1：无 WebGPU 说明在全部版式可见（含「预览只显示相机 JPG，导出仍会套用配方和 LUT」），不遮挡控件，无空白直方图框；
- D2：无 WebGPU 时切换配方卡，编辑器顶栏标题同步更新；
- D3：无 WebGPU 时清图提锐、导出与横竖旋转前后，照片矩形位移 <= 1 px；
- D4：iPad 图库缩略图宽度落在 150–200 pt，手机竖屏保持 3 列，日期头/角标/选框正常贴合；
- D5：图库每张缩略图按钮都有非空无障碍名称（含序号、类型及「已编辑」「已选中」状态），绝不含文件名或拍摄时间；
- D6：iPadOS（Macintosh UA + maxTouchPoints=5）全站文案为「iPad」且无「iPhone」；iPhone UA（含横屏）保持「iPhone」；
- D7：iPad 横屏与竖屏首屏内容限宽 <= 720 pt 且居中，手机首屏不变。

本机未安装 Playwright WebKit 时以清晰原因跳过。stdout 仅输出脱敏结构化 JSON。
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

import pytest

from conftest import REAL_HOME
from gxr_studio.camdata import calib_export, store
from support.mobile_ui_calib import pack_files
from test_g250 import _need_g185_or_g250
from test_mobile_camera_recipe_browser import _card250_pair
from test_mobile_ui_browser import MODULES, PKG, RUNTIME, SAMPLES, _free_port, _need

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "support" / "mobile_ipad_webkit.mjs"
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


def _redacted_tail(text: str) -> str:
    cleaned = re.sub(r"/(?:Volumes|Users|private|var/folders|tmp)/\S+", "[path]", text)
    return cleaned[-500:].strip()


def test_mobile_ipad_safari_webkit_fixes_d1_to_d5(tmp_path: Path) -> None:
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")

    raw250, lib250, dat250, fx250 = _need_g185_or_g250(250)
    # card250: #1 为 3:2 横图（4288x2848），#2/#3/#5 补足多张照片便于测多列网格与无障碍名称。
    dng_l1, jpg_l1 = _card250_pair(1)
    dng_2, jpg_2 = _card250_pair(2)
    dng_p1, jpg_p1 = _card250_pair(3)
    dng_p2, jpg_p2 = _card250_pair(5)

    jpg_only = tmp_path / "R9990099.JPG"
    shutil.copyfile(jpg_l1, jpg_only)

    _parsed, entries = store.entries_from_dat(raw250, library=lib250)
    all_units = ("jlaunch", "qlaunch", "l08firm")
    pack = pack_files({name: entries[(unit, 4)] for unit, name in enumerate(all_units)})
    pack_zip = tmp_path / "GXR 校准数据 2026-11-22.zip"
    pack_zip.write_bytes(calib_export.zip_bytes(pack, (2026, 11, 22, 9, 0, 0)))

    manifest: dict[str, Any] = {
        "files": [
            str(dng_l1),
            str(jpg_l1),
            str(dng_2),
            str(jpg_2),
            str(dng_p1),
            str(jpg_p1),
            str(dng_p2),
            str(jpg_p2),
            str(jpg_only),
            str(dat250),
            str(fx250),
        ],
        "pack": str(pack_zip),
        "landscapeStem": dng_l1.stem,
        "stems": [dng_l1.stem, dng_2.stem, dng_p1.stem, dng_p2.stem, jpg_only.stem],
    }
    manifest_path = tmp_path / "ipad-webkit-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

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
            ["node", str(DRIVER), str(RUNTIME), f"http://127.0.0.1:{port}", str(manifest_path)],
            cwd=REPO,
            env={**os.environ, "HOME": str(REAL_HOME)},
            check=False,
            capture_output=True,
            text=True,
            timeout=480,
        )
    finally:
        _stop(vite)

    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    assert lines, _redacted_tail(result.stdout + "\n" + result.stderr)
    report: dict[str, Any] = json.loads(lines[-1])
    if report.get("skipped"):
        pytest.skip(str(report["skipped"]))

    out_report = os.environ.get("GXR_IPAD_WEBKIT_REPORT", "")
    if out_report:
        Path(out_report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    assert result.returncode == 0, report.get("failures")
    assert report["ok"], report.get("failures")
    assert report["pageErrors"] == [], report["pageErrors"]
