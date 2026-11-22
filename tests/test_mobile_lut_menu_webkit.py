"""手机竖屏 390×844 的 LUT 合并菜单 WebKit 触控验收测试（polish-2 G1 / G2）。

在 Playwright WebKit（hasTouch=True, isMobile=True）下用真实触摸 tap 覆盖：
- G1：原生 command invoker 路径下点瓷砖打开、再点瓷砖或外部关闭、点「导入 LUT」与「色彩空间」生效；
- G2：禁用 command invoker 的回退路径下（模拟 Safari 17/18）再次点击/触摸瓷砖仍可关闭菜单。
本机未安装 Playwright WebKit 时以清晰原因跳过，不阻塞无 WebKit 环境。stdout 不含照片名、拍摄时间或 EXIF。
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from support import samples
from test_mobile_ui_browser import RUNTIME, SAMPLES, _drive, _need, _pairs

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "support" / "mobile_lut_menu_webkit.mjs"


def _cube(title: str) -> str:
    lines = [f'TITLE "{title}"', "# Input: sRGB", "LUT_3D_SIZE 2"]
    for b in (0, 1):
        for g in (0, 1):
            for r in (0, 1):
                lines.append(f"{r:.1f} {g:.1f} {b:.1f}")
    return "\n".join(lines) + "\n"


def _redact(text: str) -> str:
    return re.sub(r"/(?:Volumes|Users|private|var/folders|tmp)/\S+", "[path]", text)[-500:]


def test_phone_lut_menu_touch_tap_in_webkit(tmp_path: Path) -> None:
    files = _pairs()
    samples.need_sample(len(files) >= 2, f"need a DNG+JPG pair in {SAMPLES}")
    _need(len(files) >= 2, f"need a DNG+JPG pair in {SAMPLES}")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")

    lut_a = tmp_path / "warm_touch.cube"
    lut_a.write_text(_cube("Warm Touch"), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"files": [str(files[0]), str(files[1])], "lutA": str(lut_a)}), encoding="utf-8")

    proc = _drive(DRIVER, manifest, tmp_path, "", timeout=240)
    raw = [line for line in proc.stdout.splitlines() if line.startswith("{")]
    assert raw, _redact((proc.stdout or proc.stderr or "")[-400:])
    got = json.loads(raw[-1])
    if got.get("skipped"):
        pytest.skip(str(got["skipped"]))
    assert got.get("error") is None, got["error"]

    for key in ("native", "noInvokers"):
        run = got[key]
        assert run is not None, got
        assert run["pageErrors"] == [], (key, run["pageErrors"])
        assert len(run["touchCycles"]) == 3, (key, run["touchCycles"])
        assert all(c["opened"] and c["closed"] for c in run["touchCycles"]), (key, run["touchCycles"])
        assert all(c["expandedOpen"] == "true" and c["expandedClose"] == "false" for c in run["touchCycles"]), (key, run["touchCycles"])
        assert len(run["mouseCycles"]) == 2 and all(c["opened"] and c["closed"] for c in run["mouseCycles"]), (key, run["mouseCycles"])
        assert run["outsideTapClosed"] is True, (key, run)
        assert run["importWorks"] is True, (key, run)
        assert run["spaceWorks"] is True, (key, run)
        assert run["keyboardWorks"] is True, (key, run)

    assert got["noInvokers"]["initial"]["hasInvokers"] is False, got["noInvokers"]["initial"]
    assert got["noInvokers"]["initial"]["hasCommandForAttr"] is False, got["noInvokers"]["initial"]
