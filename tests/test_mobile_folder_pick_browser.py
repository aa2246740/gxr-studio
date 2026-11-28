"""电脑上的「选文件夹」（2026-11-28，`browser_serial`）。

- 开始页（没记住和记住了相机配方文件两种）和网格「更多」里，「选照片」旁边有「选文件夹」，旁边一行小字说浏览器的「上传」只是本机读取；
  1280×800、1440×900 下两个按钮并排、小字一行、开始页不滚动。
- 选一个像存储卡的文件夹：两对照片、GXRCE.DAT、GXRFX.BIN、Mac 的 ._ 文件、.Trashes 里的一张、一个无关文件。
  只进来两张照片，记住相机配方文件，只为那个无关文件说一句「没用上」。网格上再选一个文件夹（一对新照片和它的 ._ 文件）：加一张，不说。
- WebKit 手机和 iPad（有触摸）：开始页和「更多」里都没有，也没有目录选择框。

只用本地 card250 实拍照片和 g250 相机配方文件；报告里没有文件名、拍摄时间或 EXIF。截图只在设了 ``GXR_FOLDER_PICK_SHOTS`` 时拍。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from support.export_pipeline_cases import check
from test_g250 import _need_g185_or_g250
from test_mobile_camera_recipe_browser import _card250_pair, _prereq
from test_mobile_ui_browser import _drive

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "support" / "mobile_folder_pick_browser.mjs"
LABEL = "选文件夹"
NOTE = "浏览器可能会问是否“上传”，其实文件只在这台电脑上读取，不会传出去。"
ONE_UNUSED = "1 个文件不是 GXR 拍的照片（要 R 开头的 .JPG / .DNG），没有用上"
# 合成的 AppleDouble 头（magic 0x00051607、版本 2），不是哪张照片的元数据。
APPLEDOUBLE = b"\x00\x05\x16\x07\x00\x02\x00\x00" + b"\x00" * 120


def _copy(src: Path, folder: Path, name: str | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (name or src.name)
    shutil.copyfile(src, target)
    return target


@pytest.fixture(scope="module")
def steps(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    _prereq()
    tmp_path = tmp_path_factory.mktemp("folder_pick")
    _raw, _lib, dat250, fx250 = _need_g185_or_g250(250)
    pair_a, pair_b, pair_c = (_card250_pair(i) for i in (1, 3, 5))
    card = tmp_path / "CARD"
    dcim = card / "DCIM" / "100RICOH"
    for pair in (pair_a, pair_b):
        for src in pair:
            _copy(src, dcim)
    _copy(dat250, card, "GXRCE.DAT")
    _copy(fx250, card, "GXRFX.BIN")
    (dcim / f"._{pair_a[1].name}").write_bytes(APPLEDOUBLE)
    (card / ".DS_Store").write_bytes(b"Bud1")
    _copy(pair_c[1], card / ".Trashes" / "501")
    (card / "MISC").mkdir()
    (card / "MISC" / "notes.txt").write_text("not a photo\n", encoding="utf-8")
    more = tmp_path / "MORE" / "101RICOH"
    for src in pair_c:
        _copy(src, more)
    (more / f"._{pair_c[0].name}").write_bytes(APPLEDOUBLE)
    shots = os.environ.get("GXR_FOLDER_PICK_SHOTS", "")
    if shots:
        Path(shots).mkdir(parents=True, exist_ok=True)
    report_path = tmp_path / "report.json"
    manifest = {
        "cardDir": str(card),
        "cardStems": [pair_a[0].stem, pair_b[0].stem],
        "moreDir": str(more.parent),
        "pair": [str(p) for p in pair_a],
        "shotsDir": shots,
        "reportPath": str(report_path),
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = _drive(DRIVER, manifest_path, tmp_path, "", timeout=600)
    check(report_path.is_file(), "driver wrote its report")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    check(result.returncode == 0 and report.get("error") is None, f"driver completed without error: {report.get('error')}")
    check(report["pageErrors"] == [], f"no page errors: {report['pageErrors']}")
    return {**report["steps"], "flowErrors": report["flowErrors"]}


def _start_rows_ok(rows: list[dict[str, Any]], what: str) -> None:
    for row in rows:
        size = row["size"]
        check(row["folders"] == 1 and row["label"] == LABEL, f"{what} {size}: one 「选文件夹」 button: {row}")
        check(row["note"] == NOTE, f"{what} {size}: the note says files are only read on this computer: {row['note']}")
        check(row["sideBySide"] is True, f"{what} {size}: 「选文件夹」 sits beside the photo button, same height: {row}")
        one_line = row["noteLines"] == 1 and row["noteInside"] is True and row["noteBelow"] is True
        check(one_line, f"{what} {size}: the note is one line under the buttons: {row}")
        check(row["noteDim"] is True, f"{what} {size}: the note is dimmer than the heading: {row}")
        check(row["labelsFit"] is True, f"{what} {size}: button labels are not cut: {row}")
        check(row["scrolls"] is False, f"{what} {size}: the start screen still fits without scrolling: {row}")


def test_mobile_folder_pick_entry_on_computer_start(steps: dict[str, Any]) -> None:
    check("computer" in steps and not steps["flowErrors"].get("computer"), f"computer flow completed: {steps['flowErrors']}")
    comp = steps["computer"]
    _start_rows_ok(comp["startNoDat"], "start (no camera files)")
    _start_rows_ok(comp["startDat"], "start (camera files remembered)")
    check(comp["startDatState"] == "remembered", f"second start screen is the remembered-camera-files one: {comp['startDatState']}")
    check(comp["inputsBefore"] == 3, f"no extra file input before the folder button is used: {comp['inputsBefore']}")
    check(comp["multiple"] is True, "the folder button opens a chooser")
    inp = comp["input"]
    check(inp == {"dir": True, "multiple": True, "tab": -1, "hidden": "true", "visible": False}, f"hidden webkitdirectory input: {inp}")


def test_mobile_folder_pick_imports_like_a_dropped_folder(steps: dict[str, Any]) -> None:
    check("computer" in steps and not steps["flowErrors"].get("computer"), f"computer flow completed: {steps['flowErrors']}")
    comp = steps["computer"]
    check(comp["screen"] == "grid", f"picking the folder opens the grid: {comp['screen']}")
    check(comp["cells"] == comp["wantCells"], f"only the two photo pairs come in (no ._ or .Trashes copy): {len(comp['cells'])} cells")
    check(comp["camfiles"] == {"generation": 250, "library": True}, f"camera files in the folder are remembered: {comp['camfiles']}")
    check(comp["toast"] == ONE_UNUSED, f"only the unrelated file gets the 「没用上」 note: {comp['toast']}")


def test_mobile_folder_pick_in_grid_more_sheet(steps: dict[str, Any]) -> None:
    check("computer" in steps and not steps["flowErrors"].get("computer"), f"computer flow completed: {steps['flowErrors']}")
    comp = steps["computer"]
    titles = [row["title"] for row in comp["gridRows"]]
    check(LABEL in titles and titles.index(LABEL) == titles.index("添加照片") + 1, f"更多: 「选文件夹」 right under 「添加照片」: {titles}")
    check(comp["gridRows"][titles.index(LABEL)]["detail"] == NOTE, "更多: the row carries the same note")
    check(comp["gridMultiple"] is True, "更多: the row opens a chooser")
    check(comp["gridCells"] == 3, f"grid: a folder with one new pair adds one photo: {comp['gridCells']}")
    check(comp["gridToast"] is None, f"grid: a ._ file alone is not reported as unused: {comp['gridToast']}")


def test_mobile_folder_pick_absent_on_phone_and_ipad(steps: dict[str, Any]) -> None:
    if steps.get("webkitMissing"):
        pytest.skip("Playwright WebKit is not installed")
    for label in ("webkitPhone", "webkitIpad"):
        row = steps[label]
        check(row["touch"] > 0 or row["fine"] is False, f"{label}: touch device: {row}")
        check(row["start"] == {"folders": 0, "notes": 0, "photos": 1}, f"{label}: start screen has only 「选照片」: {row['start']}")
        titles = [r["title"] for r in row["gridRows"]]
        check("添加照片" in titles and LABEL not in titles, f"{label}: 更多 has no 「选文件夹」: {titles}")
        check(row["dirInputs"] == 0, f"{label}: no folder chooser input: {row['dirInputs']}")
