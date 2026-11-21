"""手机竖屏 390×844 的 LUT 甲板（polish-2 第 1 项）。

只量可见 LUT 卡、提示与甲板底部 22pt 的重叠，以及「配方 | LUT」切换键在两种模式下的 x。
照片、带子和底栏矩形必须保持 390×844 无安全区的基线。stdout 不含照片名、拍摄时间或 EXIF。
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
DRIVER = REPO / "tests" / "support" / "mobile_lut_deck_390.mjs"

# 390×844、安全区为 0 时 ipad-a 量得的照片、带子和底栏。
BASE_RECTS = {
    "photo": {"x": 0.0, "y": 44.0, "w": 390.0, "h": 616.0},
    "band": {"x": 0.0, "y": 660.0, "w": 390.0, "h": 128.0},
    "bar": {"x": 0.0, "y": 788.0, "w": 390.0, "h": 56.0},
}


def _cube(title: str) -> str:
    lines = [f'TITLE "{title}"', "# Input: sRGB", "LUT_3D_SIZE 2"]
    for b in (0, 1):
        for g in (0, 1):
            for r in (0, 1):
                lines.append(f"{r:.1f} {g:.1f} {b:.1f}")
    return "\n".join(lines) + "\n"


def _redact(text: str) -> str:
    return re.sub(r"/(?:Volumes|Users|private|var/folders|tmp)/\S+", "[path]", text)[-500:]


def test_phone_lut_deck_fits_two_cards_at_390(tmp_path: Path) -> None:
    files = _pairs()
    samples.need_sample(len(files) >= 2, f"need a DNG+JPG pair in {SAMPLES}")
    _need(len(files) >= 2, f"need a DNG+JPG pair in {SAMPLES}")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")

    lut_a = tmp_path / "a.cube"
    lut_b = tmp_path / "b.cube"
    lut_a.write_text(_cube("Warm A"), encoding="utf-8")
    lut_b.write_text(_cube("Cool B"), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"files": [str(files[0]), str(files[1])], "lutA": str(lut_a), "lutB": str(lut_b)}),
        encoding="utf-8",
    )

    proc = _drive(DRIVER, manifest, tmp_path, "", timeout=180)
    raw = [line for line in proc.stdout.splitlines() if line.startswith("{")]
    assert raw, _redact((proc.stdout or "")[-400:])
    got = json.loads(raw[-1])
    assert got.get("error") is None, got["error"]

    empty = got["empty"]
    filled = got["filled"]
    assert empty["footText"], empty
    assert empty["footClipped"] is False, empty["foot"]
    assert empty["footHitsName"] is False, empty["foot"]
    assert empty["spaceW"] <= 52.5, empty["space"]
    assert filled["visibleCards"] >= 2, filled["cards"]
    assert filled["noticeText"] == "已经导入过", filled["noticeText"]
    assert filled["noticeOverlapBottom22"] == 0, filled["notice"]
    assert filled["noticeHitsName"] is False, filled["notice"]
    assert abs(filled["switchBox"]["x"] - got["recipeSwitch"]["x"]) <= 0.5, {
        "lut": filled["switchBox"],
        "recipe": got["recipeSwitch"],
    }
    for key in ("photo", "band", "bar"):
        box = got[key]
        ref = BASE_RECTS[key]
        for part in ("x", "y", "w", "h"):
            assert abs(float(box[part]) - ref[part]) <= 1.0, (key, part, box, ref)
