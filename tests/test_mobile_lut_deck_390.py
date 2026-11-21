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
    empty_menu = got["emptyMenu"]
    filled = got["filled"]
    selected = got["selected"]
    selected_menu = got["selectedMenu"]
    assert empty["footText"], empty
    assert empty["footClipped"] is False, empty["foot"]
    assert empty["footHitsName"] is False, empty["foot"]
    # 手机竖屏已把「导入 LUT」与「色彩空间」合并进一颗 44pt 瓷砖，不再占满 56pt 色彩空间瓷砖。
    assert empty["space"] is None, empty["space"]
    assert empty["menuTile"] and empty["menuTile"]["w"] >= 43.5 and empty["menuTile"]["h"] >= 43.5, empty["menuTile"]
    assert empty_menu["importItem"]["hitOk"] is True and empty_menu["importItem"]["disabled"] is False, empty_menu
    assert empty_menu["importItem"]["w"] >= 43.5 and empty_menu["importItem"]["h"] >= 43.5, empty_menu
    assert empty_menu["spaceItem"]["hitOk"] is True and empty_menu["spaceItem"]["disabled"] is True, empty_menu
    assert empty_menu["spaceItem"]["w"] >= 43.5 and empty_menu["spaceItem"]["h"] >= 43.5, empty_menu
    # 78pt 卡片不缩小，按横条可见视口度量，空选和选中态都能完整放下两张已导入 LUT 卡。
    imported = filled["imported"]
    assert [item["w"] for item in imported] == [78, 78], imported
    assert all(item["fully"] and item["visibleW"] >= 77.5 for item in imported), (filled["strip"], imported)
    assert filled["visibleCards"] >= 2, (filled["strip"], imported)
    sel_imported = selected["imported"]
    assert [item["w"] for item in sel_imported] == [78, 78], sel_imported
    assert selected["visibleCards"] >= 2, (selected["strip"], sel_imported)
    assert selected_menu["spaceItem"]["disabled"] is False and selected_menu["spaceItem"]["val"] == "sRGB · 文件已写明", selected_menu
    assert got["dotSelected"] is True, got
    assert got["dotAfterRebuild"] is False, got
    for btn in filled["switchBtns"]:
        assert btn and btn["w"] >= 43.5 and btn["h"] >= 43.5, filled["switchBtns"]
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
