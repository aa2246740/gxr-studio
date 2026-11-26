"""电脑浏览器上的导入（2026-11-26，`browser_serial`）。

- 拖放：把 2 对 RAW+JPG 和 GXRCE.DAT、GXRFX.BIN 拖到开始页（CDP ``Input.dispatchDragEvent``，真实文件路径），和「选照片」一样进网格、
  记住相机配方文件；网格上再拖不重复；编辑器里取消默认动作、不导入、提示回到照片列表。带文件的拖动在每一屏都取消
  dragover / drop（浏览器不会拿拖进来的照片换掉应用），文字和链接的拖动不拦。悬停时铺满窗口的提示，松开、拖出窗口、Esc 收起。
- 电脑上有照片时刷新先问一声（beforeunload），没有照片、清空以后不问；WebKit 手机和 iPad（有触摸）不挂。
- 没用上的文件说一句（一个都没用上也说）；只选 GXRFX.BIN 说还差 GXRCE.DAT，换掉之前的错误。
- 看不见的选择框不进 Tab 顺序，可见的按钮照旧打开选择窗口。

只用本地 card250 实拍照片和 g250 相机配方文件；报告里没有文件名、拍摄时间或 EXIF。截图只在设了 ``GXR_DESKTOP_IMPORT_SHOTS`` 时拍。
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
DRIVER = REPO / "tests" / "support" / "mobile_desktop_import_browser.mjs"
HINT = "松开就能导入照片和相机配方文件"
EDITOR_NOTE = "回到照片列表再拖进来"
NOTHING = "没有能用的照片：这个文件不是 GXR 拍的照片（要 R 开头的 .JPG / .DNG）"
TWO_UNUSED = "2 个文件不是 GXR 拍的照片（要 R 开头的 .JPG / .DNG），没有用上"
BIN_ONLY = "还差 GXRCE.DAT，请把 GXRCE.DAT 和 GXRFX.BIN 一起选"


@pytest.fixture(scope="module")
def steps(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    _prereq()
    tmp_path = tmp_path_factory.mktemp("desktop_import")
    _raw, _lib, dat250, fx250 = _need_g185_or_g250(250)
    pair_a, pair_b, pair_c, pair_d = (_card250_pair(i) for i in (1, 3, 5, 7))
    renamed = []
    for name in ("IMG_0001.JPG", "photo.jpeg"):
        target = tmp_path / name
        shutil.copyfile(pair_c[1], target)
        renamed.append(str(target))
    txt = tmp_path / "notes.txt"
    txt.write_text("not a photo\n", encoding="utf-8")
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    bad_dat = bad_dir / "GXRCE.DAT"
    bad_dat.write_bytes(os.urandom(4096))
    same = []
    for folder, pair in (("cardA", pair_a), ("cardB", pair_b)):
        (tmp_path / folder).mkdir()
        target = tmp_path / folder / "R0000001.JPG"
        shutil.copyfile(pair[1], target)
        same.append(str(target))
    card = tmp_path / "card"
    card.mkdir()
    card_pairs = [_card250_pair(i) for i in range(1, 12)]
    card_files = []
    for pair in card_pairs:
        for src in pair:
            target = card / src.name
            shutil.copyfile(src, target)
            card_files.append(str(target))
    shots = os.environ.get("GXR_DESKTOP_IMPORT_SHOTS", "")
    if shots:
        Path(shots).mkdir(parents=True, exist_ok=True)
    report_path = tmp_path / "report.json"
    manifest = {
        "startDrop": [str(p) for pair in (pair_a, pair_b) for p in pair] + [str(dat250), str(fx250)],
        "gridDrop": [str(p) for pair in (pair_a, pair_c) for p in pair],
        "editorDrop": [str(p) for p in pair_d],
        "openStem": pair_a[0].stem,
        "pairs": [str(p) for pair in (pair_a, pair_b) for p in pair],
        "renamed": renamed,
        "txt": str(txt),
        "badDat": str(bad_dat),
        "dat": str(dat250),
        "bin": str(fx250),
        "sameA": same[0],
        "sameB": same[1],
        "cardDir": str(card),
        "cardFiles": card_files,
        "cardStems": [pair[0].stem for pair in card_pairs],
        "lostFiles": [str(card / src.name) for src in card_pairs[0]],
        "shotsDir": shots,
        "reportPath": str(report_path),
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = _drive(DRIVER, manifest_path, tmp_path, "", timeout=900)
    check(report_path.is_file(), "driver wrote its report")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    check(result.returncode == 0 and report.get("error") is None, f"driver completed without error: {report.get('error')}")
    check(report["pageErrors"] == [], f"no page errors: {report['pageErrors']}")
    return {**report["steps"], "flowErrors": report["flowErrors"]}


def _flows_ok(steps: dict[str, Any], *names: str) -> None:
    """驱动的每一段各记各的错：这几段都跑完了。"""
    errors = {name: steps["flowErrors"][name] for name in names if name in steps["flowErrors"]}
    check(not errors, f"driver flows completed: {errors}")


def _prevented(rows: list[dict[str, Any]], kind: str) -> list[bool]:
    return [row["prevented"] for row in rows if row["type"] == kind and row["files"]]


def test_mobile_desktop_drop_on_start_and_grid(steps: dict[str, Any]) -> None:
    _flows_ok(steps, "drop")
    drop = steps["drop"]
    hint = drop["startOverlay"]
    check(hint == {"on": True, "visible": True, "text": HINT}, f"start: full-window hint while files hover: {hint}")
    check(drop["overlayAfterDrop"]["on"] is False, "start: hint hides after the drop")
    over, dropped = _prevented(drop["startDrags"], "dragover"), _prevented(drop["startDrags"], "drop")
    check(over and all(over), f"start: every file dragover is cancelled: {over}")
    check(dropped == [True], f"start: the file drop is cancelled: {dropped}")
    check(drop["screenAfterStart"] == "grid" and drop["startCells"] == 2, f"start: the drop opens the grid with 2 photos: {drop['startCells']}")
    cam = drop["camfiles"]
    check(cam == {"generation": 250, "library": True, "saved": True}, f"start: dropped camera files are remembered: {cam}")
    check(drop["gridOverlay"]["on"] is True, "grid: hint while files hover")
    check(_prevented(drop["gridDrags"], "drop") == [True], "grid: the file drop is cancelled")
    check(drop["gridCells"] == 3, f"grid: one old pair + one new pair adds one photo: {drop['gridCells']}")
    check(drop["navigations"] == 0 and drop["sameApp"] is True, "the app is never replaced by a dropped file")


def test_mobile_desktop_drop_overlay_leave_and_esc(steps: dict[str, Any]) -> None:
    _flows_ok(steps, "drop")
    drop = steps["drop"]
    check(drop["leave"] == {"shownBeforeLeave": True, "afterLeave": False}, f"dragleave out of the window hides the hint: {drop['leave']}")
    check(drop["esc"] == {"shownBeforeEsc": True, "afterEsc": False}, f"Esc hides the hint: {drop['esc']}")
    check(drop["innerMoveKeepsOverlay"] is True, "moving between elements (dragleave then dragover) keeps the hint")


def test_mobile_desktop_drop_in_editor(steps: dict[str, Any]) -> None:
    _flows_ok(steps, "drop")
    drop = steps["drop"]
    check(drop["editorOverlay"]["on"] is False, "editor: no import hint")
    check(_prevented(drop["editorDrags"], "drop") == [True], f"editor: the drop is cancelled: {drop['editorDrags']}")
    check(drop["editorToast"] == EDITOR_NOTE, f"editor: toast says to go back to the photo list: {drop['editorToast']}")
    check(drop["editorScreen"] == "editor", "editor: stays in the editor")
    check(drop["cellsAfterEditorDrop"] == 3, f"editor: nothing was imported: {drop['cellsAfterEditorDrop']}")


def test_mobile_desktop_text_drag_not_cancelled(steps: dict[str, Any]) -> None:
    _flows_ok(steps, "drop")
    text = steps["drop"]["text"]
    check(text["synthOverText"] is False and text["synthDropText"] is False, "a text drag is not cancelled")
    check(text["synthOverLink"] is False, "a link drag is not cancelled")
    check(text["overlayOnText"] is False, "no hint for a text drag")
    check(all(not row["prevented"] for row in text["cdp"]), f"a real text drag is not cancelled: {text['cdp']}")


def test_mobile_desktop_unload_guard(steps: dict[str, Any]) -> None:
    _flows_ok(steps, "unload")
    unload = steps["unload"]
    check(unload["emptyStart"] == [], f"no photos: reload does not ask: {unload['emptyStart']}")
    check(unload["loaded"] == ["beforeunload"], f"photos loaded: reload asks first: {unload['loaded']}")
    check(unload["stayed"] is True, "dismissing the dialog keeps the photos")
    check(unload["afterClear"] == [], f"cleared: reload does not ask: {unload['afterClear']}")
    if steps.get("webkitMissing"):
        pytest.skip("Playwright WebKit is not installed")
    check(unload["blocksLoaded"] is True and unload["blocksCleared"] is False, f"computer: handler on with photos, off after clear: {unload}")
    for label in ("webkitPhone", "webkitIpad"):
        row = steps[label]
        check(row["cells"] == 2 and (row["touch"] > 0 or row["fine"] is False), f"{label}: photos loaded on a touch device: {row}")
        check(row["blocks"] is False and row["onbeforeunload"] is True, f"{label}: no beforeunload handler: {row}")
    check(steps["webkitIpad"]["touch"] == 5, "WebKit iPad reports 5 touch points")


def test_mobile_desktop_unused_files_note(steps: dict[str, Any]) -> None:
    _flows_ok(steps, "unusedPicks")
    picks = steps["picks"]
    check(picks["nothingToast"] == NOTHING and picks["nothingScreen"] == "start", f"one .txt: nothing-usable note: {picks['nothingToast']}")
    check(picks["someCells"] == 2 and picks["someToast"] == TWO_UNUSED, f"2 pairs + 2 renamed JPGs: '2 个文件…' note: {picks['someToast']}")


def test_mobile_desktop_lone_camera_file(steps: dict[str, Any]) -> None:
    _flows_ok(steps, "binOnlyStart", "datOnly", "binOnlyGrid")
    picks = steps["picks"]
    check(bool(picks["badDatError"]) and "无法读取相机配方文件" in picks["badDatError"], "a bad DAT shows its error first")
    check(picks["binOnlyStart"] == BIN_ONLY, f"start: only BIN says the DAT is missing and replaces the old error: {picks['binOnlyStart']}")
    paired = picks["datAfterBin"]
    check(paired == {"generation": 250, "library": True}, f"start: then the DAT alone pairs with that BIN: {paired}")
    check(bool(picks["datOnlyStart"]) and "缺少 GXRFX.BIN" in picks["datOnlyStart"], "start: only DAT names the missing BIN")
    check(bool(picks["datOnlyGrid"]) and "缺少 GXRFX.BIN" in picks["datOnlyGrid"], "grid: only DAT names the missing BIN")
    check(picks["binOnlyGrid"] == BIN_ONLY, f"grid: only BIN gives the same message as the start screen: {picks['binOnlyGrid']}")


def test_mobile_desktop_hidden_inputs_not_tab_stops(steps: dict[str, Any]) -> None:
    _flows_ok(steps, "tabOrder")
    picks = steps["picks"]
    check(len(picks["inputs"]) == 3, "three hidden file inputs")
    check(all(i == {"tab": -1, "hidden": "true"} for i in picks["inputs"]), f"hidden inputs: tabindex -1 and aria-hidden: {picks['inputs']}")
    check(not any(stop.startswith("input:file") for stop in picks["tabOrder"]), f"Tab never reaches a hidden input: {picks['tabOrder']}")
    check(any("#pick-photos" in stop for stop in picks["tabOrder"]), "Tab still reaches the visible buttons")
    check(picks["choosers"] == {"camfiles": True, "photos": True}, f"visible buttons still open the multi-file chooser: {picks['choosers']}")


def test_mobile_desktop_same_name_different_photo(steps: dict[str, Any]) -> None:
    _flows_ok(steps, "sameName")
    same = steps["sameName"]
    check(same["afterRepickA"] == 1, f"re-picking the same file does not duplicate: {same['afterRepickA']}")
    check(same["afterB"] == 2, f"a different photo with the same name is kept as a second photo: {same['afterB']}")
    check(same["afterRepickB"] == 2, f"re-picking that second file does not duplicate: {same['afterRepickB']}")
    check(same["drawn"] == 2, f"both photos show a thumbnail: {same['drawn']}")


def test_mobile_desktop_one_lost_file_is_not_a_pulled_card(steps: dict[str, Any]) -> None:
    _flows_ok(steps, "lostFile")
    lost = steps["lostFile"]
    one = lost["oneFile"]
    check(one["cells"] == 11, f"11 photos loaded: {one['cells']}")
    check(one["modal"] is False, "one deleted file: no 「存储卡被拔出了」 sheet")
    check(one["gone"] == 0, f"one deleted file: no cell gets the card mark: {one['gone']}")
    check(one["err"] == [lost["lostId"]], "one deleted file: only that photo is marked 读不了")
    whole = lost["wholeFolder"]
    check(whole["modal"] is True and whole["gone"] > 0, f"whole folder gone: the pulled-card sheet as before: {whole}")
