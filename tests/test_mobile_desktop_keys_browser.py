"""电脑浏览器上的键盘和鼠标（2026-11-26，desktop-keys，`browser_serial`）。

- Esc 一次只关最上面的一层：开始页帮助、网格 ⋯、读不了、拔卡、配方文件不匹配、编辑器 ⋯ 和它打开的面板（导出选项、照片信息、
  复制调整）、底栏「更多」、曲线、配方筛选、配方状态、LUT 色彩空间；自己处理 Esc 的关机图只关它自己。面板之后是全图、
  选择和「选要套用的照片」，最后是回网格。文字框里的 Esc 只归文字框；拖着滑块按 Esc 不切屏，也不留下 data-drag。
- 面板打开时焦点进去，Tab 在里面转，关上回到打开它的按钮。
- 从编辑器回网格：滚动位置不变，焦点在编辑器最后看的那张。
- 选择模式里 Shift 点一段、Cmd/Ctrl 点一张、Cmd/Ctrl+A 全选；不在选择模式时 Cmd/Ctrl 点一张就进去。
- 方向键、Home、End 在格子之间走；编辑器里 ←/→ 换照片，滑块拿着方向键时不换。
- 电脑上格子悬停是手形，清空前先问；有触摸的手机和 iPad 不变。

真实照片：本机 7 个拍摄文件夹的 53 对 RAW+JPG；card250 两对和 g250 相机配方文件；g185 配方文件做不匹配。
报告里没有文件名、拍摄时间或 EXIF。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from support import samples
from support.export_pipeline_cases import check
from support.lut_cases import cube_text
from test_g250 import _need_g185_or_g250
from test_mobile_camera_recipe_browser import _card250_pair, _prereq
from test_mobile_ui_browser import _drive

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "support" / "mobile_desktop_keys_browser.mjs"
FOLDERS = (
    "r6-field-20261118/card250",
    "r6-field-20261118/kubik_r6",
    "r6-field-20261118/vertical",
    "r6-field-20261118/card249",
    "r6-field-20261118/probe251",
    "hf256-field-20261120/card",
    "hf258-field-20261120/card",
)


def _pairs(folder: Path) -> list[tuple[Path, Path]]:
    if not folder.is_dir():
        samples.missing(f"captures/{folder.name} 不在本机")
    out: list[tuple[Path, Path]] = []
    for stem in sorted({p.stem for p in folder.iterdir() if p.suffix.upper() in (".DNG", ".JPG")}):
        dng = next((p for p in folder.iterdir() if p.stem == stem and p.suffix == ".DNG"), None)
        jpg = next((p for p in folder.iterdir() if p.stem == stem and p.suffix == ".JPG"), None)
        if dng is not None and jpg is not None:
            out.append((dng, jpg))
    return out


def _run(tmp_path: Path) -> dict[str, Any]:
    _prereq()
    _raw250, _lib250, dat250, fx250 = _need_g185_or_g250(250)
    _raw185, _lib185, dat185, fx185 = _need_g185_or_g250(185)
    pairs = [pair for folder in FOLDERS for pair in _pairs(samples.captures() / folder)]
    if len(pairs) < 53:
        samples.missing(f"need 53 RAW+JPG pairs, found {len(pairs)}")
    pairs = pairs[:53]
    a, b = _card250_pair(1), _card250_pair(3)

    bad = tmp_path / "R9990001.DNG"
    bad.write_bytes(b"not a tiff")
    card = tmp_path / "card"
    card.mkdir()
    card_files: list[str] = []
    for pair in pairs[10:20]:
        for src in pair:
            dst = card / src.name
            shutil.copyfile(src, dst)
            card_files.append(str(dst))
    lut = tmp_path / "plain9.cube"
    lut.write_text(cube_text(lambda x: x, size=9, title="Plain 9"), encoding="utf-8")
    shots = Path(os.environ.get("GXR_DESKTOP_KEYS_SHOTS", str(tmp_path / "shots")))
    shots.mkdir(parents=True, exist_ok=True)

    report_path = tmp_path / "report.json"
    manifest = {
        "files53": [str(p) for pair in pairs for p in pair],
        "count53": 53,
        "files2": [str(p) for pair in (a, b) for p in pair] + [str(dat250), str(fx250)],
        "badFiles": [str(p) for p in a] + [str(bad)],
        "cardFiles": card_files,
        "cardDir": str(card),
        "mismatchFiles": [str(p) for p in a] + [str(dat185), str(fx185)],
        "lut": str(lut),
        "debugShots": os.environ.get("GXR_DESKTOP_KEYS_DEBUG", ""),
        "shots": str(shots),
        "reportPath": str(report_path),
        # 调试时只跑其中几段：GXR_DESKTOP_KEYS_ONLY=computer,others（整份检查仍要全跑）。
        "only": [x for x in os.environ.get("GXR_DESKTOP_KEYS_ONLY", "").split(",") if x],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = _drive(DRIVER, manifest_path, tmp_path, "", timeout=3000)
    check(report_path.is_file(), "driver wrote its report")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    check(result.returncode == 0 and report.get("error") is None, f"driver completed without error: {report.get('error')}")
    return report


def _sheet(step: dict[str, Any], what: str, focus: bool = True, opener: bool = False) -> None:
    check(step.get("closed") is True, f"{what}: one Esc closes it")
    check(step.get("sameScreen") is True, f"{what}: the same Esc leaves nothing else")
    if focus:
        check(step.get("focusOpen") is True, f"{what}: focus moves into the sheet when it opens")
        check(step.get("focusTab") is True and step.get("focusBack") is True, f"{what}: Tab and Shift-Tab stay inside")
    if opener:
        check(step["after"].get("opener") is True, f"{what}: focus returns to the button that opened it")


def test_mobile_desktop_keys(tmp_path: Path) -> None:
    _verify(_run(tmp_path))


def _verify(report: dict[str, Any]) -> None:
    steps = report["steps"]
    errors = {k: v for k, v in steps.items() if k.endswith("Error")}
    check(report["pageErrors"] == [], f"no page errors: {report['pageErrors']}")

    pc = steps.get("computer", {})
    check(pc.get("env", {}).get("touch") == 0 and pc["env"]["fine"] is True, "computer context has no touch and a fine pointer")
    _sheet(pc["help"], "start help sheet", opener=True)
    check(pc["grid"]["cols"] == 9, f"1440 wide: 9 columns ({pc['grid']['cols']})")
    check(pc["hoverBefore"]["cursor"] == "pointer" and pc["hover"]["cursor"] == "pointer", "computer: cells use the hand cursor")
    lit = pc["hover"]["filter"] != pc["hoverBefore"]["filter"]
    check(lit and pc["hover"]["ring"] not in ("", "none"), "computer: hover brightens and rings the cell")
    _sheet(pc["menu"], "grid ⋯ menu", opener=True)
    check(pc["shiftRange"] == "已选 5 张", f"Shift-click 1 → 5 selects 5 ({pc['shiftRange']})")
    check(pc["shiftShrink"] == "已选 3 张", f"a second Shift-click 1 → 3 replaces the range ({pc['shiftShrink']})")
    check(pc["modOff"] == "已选 4 张" and pc["modOn"] == "已选 5 张", f"Cmd/Ctrl-click toggles one ({pc['modOff']}, {pc['modOn']})")
    check(pc["selectAll"] == "已选 53 张", f"Cmd/Ctrl+A selects all ({pc['selectAll']})")
    check(pc["selectEsc"] == {"title": "照片", "sel": 0, "choose": "选择"}, f"Esc leaves select mode with nothing selected ({pc['selectEsc']})")
    check(pc["modEnter"] == {"title": "已选 1 张", "screen": "grid"}, f"Cmd/Ctrl-click outside the mode enters it with one ({pc['modEnter']})")
    check(pc["modEnterEsc"] == "照片", "Esc leaves it again")
    down = pc["arrowDown"]
    check(down["want"] is not None and down["id"] == down["want"], "ArrowDown lands on the cell below in the same column")
    check(pc["arrowStart"] is not None, "found a full row of 9 with another row of the same group below")
    check(
        down["domIndex"] == pc["arrowStart"] + 9,
        f"9 columns: ArrowDown from cell 1 of a row lands on cell 10 ({down['domIndex'] - pc['arrowStart'] + 1})",
    )
    check(pc["arrowRight"]["id"] == pc["arrowRightWant"], "ArrowRight moves one cell on")
    check(pc["arrowLeft"]["id"] == pc["arrowDown"]["id"], "ArrowLeft moves one cell back")
    check(pc["arrowUp"]["id"] == pc["arrowUp"]["want"], "ArrowUp goes back to the cell above")
    check(pc["end"]["isLast"] is True and pc["end"]["visible"] is True, "End focuses the last cell and scrolls it into view")
    check(pc["home"]["id"] == pc["home"]["want"] and pc["home"]["visible"] is True, "Home focuses the first cell")
    check(pc["enterOpens"] == "editor", "Enter still opens the photo")
    check(pc["enterOpens"] == "editor" and pc["escFromEditor"] == "grid", "Esc in the editor goes back to the grid")

    position = pc["position"]
    check(position["topBefore"] > 100, f"the grid was scrolled ({position['topBefore']})")
    moved = f"{position['topBefore']} → {position['topAfter']}"
    check(abs(position["topAfter"] - position["topBefore"]) <= 2, f"back from the editor keeps scrollTop ({moved})")
    check(position["focus"]["id"] == position["want"], "back from the editor focuses the photo it showed")
    arrow = pc["editorArrow"]
    check(arrow["before"].startswith("1 /") and arrow["after"].startswith("2 /"), f"ArrowRight in the editor: next photo ({arrow})")
    check(pc["collapsed"] == "1", "F collapses the panels (full view)")
    folded = pc["collapsedEsc"]
    check(folded == {"collapsed": "0", "screen": "editor", "tool": "adjust"}, f"Esc leaves full view first, back on 调整 ({folded})")
    for ask in pc.get("askEsc", []):
        check(ask == {"closed": True, "screen": "editor"}, f"「还差相机配方文件」: one Esc closes only it ({ask})")
    slider = pc["sliderArrow"]
    check(slider["posAfter"] == slider["posBefore"], "a focused slider keeps the arrow keys: no photo change")
    if not slider["disabled"]:
        check(slider["valueAfter"] != slider["valueBefore"], "the slider moved with the arrow key")
        drag = pc["dragEsc"]
        check(drag["midDrag"] == "1" and drag["screenMid"] == "editor", f"Esc while dragging a slider does not leave the editor ({drag})")
        check(drag["dragAfter"] == "0", "after the release nothing is stuck: data-drag is 0")
    check(pc["second"]["focus"]["id"] == pc["second"]["want"], "moved on in the editor: back focuses the second photo")
    _sheet(pc["editorMore"], "editor ⋯ opened with Enter", opener=True)
    _sheet(pc["exportOptions"], "导出选项", opener=True)
    _sheet(pc["photoInfo"], "照片信息", opener=True)
    _sheet(pc["offImage"], "关机图 (handles Esc itself)", focus=False)
    check(pc["editorStill"] == "editor", "the export and off-image sheets closed without leaving the editor")
    _sheet(pc["copySheet"], "复制调整 (paste overlay)", opener=True)
    check(pc["pasteShift"] == "已选 4 张", f"paste mode: Shift-click selects a range ({pc['pasteShift']})")
    check(pc["pasteAll"]["sub"] == "已选 53 张", f"paste mode: Cmd/Ctrl+A selects all ({pc['pasteAll']})")
    check(pc["pasteAll"]["go"] == pc["pasteAllButton"], "Cmd/Ctrl+A gives the same as 全选")
    check(pc["pasteEsc"] == "照片", "Esc leaves paste mode")
    clear = pc["clear"]
    asked = "清空这 53 张照片？" in clear["text"] and "调整也会一起丢掉。" in clear["text"]
    check(clear["dialog"] is True and asked, f"computer: clearing asks first ({clear['text']})")
    check("清空" in clear["text"] and "取消" in clear["text"] and clear["focusIn"] is True, "the question has 清空 / 取消 and takes focus")
    if clear["dialog"]:
        check(pc["clearEsc"] == {"dialog": 0, "screen": "grid", "cells": True}, f"Esc cancels and keeps the photos ({pc['clearEsc']})")
        check(pc["clearGo"] == "start", "清空 clears")
    else:
        check(False, "no confirmation to cancel with Esc")

    ph = steps.get("computerPhone", {})
    check(ph.get("layout", "").startswith("phone"), f"small computer window uses the phone layout ({ph.get('layout')})")
    _sheet(ph["more"], "底栏「更多」", opener=True)
    check(ph["moreState"] == {"esc": True, "button": True}, "更多: model after Esc equals after 关闭")
    curves = ph["curvesEsc"]
    check(curves == {"closed": True, "screen": "editor", "focusInSheet": True}, f"曲线: focus inside, one Esc closes it ({curves})")
    check(ph["arrowNext"].startswith("2 /"), f"phone layout on a computer: ArrowRight goes to the next photo ({ph['arrowNext']})")
    check(ph["curvesState"] == {"same": True, "changed": True}, f"曲线: model after Esc equals after 完成 ({ph['curvesState']})")
    check(ph["stampEsc"] == {"closed": True, "kept": True}, f"时间戳: Esc closes and keeps the typed text, like 完成 ({ph['stampEsc']})")
    check("missing" not in ph["filter"], "配方筛选 tile is there")
    _sheet(ph["filter"], "配方筛选", opener=True)
    check("missing" not in ph["status"], "配方状态 tile is there")
    _sheet(ph["status"], "配方状态")
    _sheet(ph["lutSpace"], "LUT 输入色彩空间")
    logo = ph["logoField"]
    check(logo == {"sheet": 1, "screen": "editor", "focus": "名称"}, f"Esc in the logo name field closes nothing ({logo})")

    others = steps.get("others", {})
    check(others.get("unreadableAuto") is True, "the 读不了 sheet that pops up by itself closes with one Esc")
    _sheet(others["unreadable"], "读不了", opener=True)
    _sheet(others["card"], "存储卡被拔出了")
    _sheet(others["mismatch"], "相机配方文件不匹配")

    if not steps.get("webkitMissing"):
        wk = steps["webkitComputer"]
        check(wk["env"]["touch"] == 0, "WebKit computer has no touch")
        check(wk["cursor"] == "pointer", "WebKit computer: hand cursor")
        _sheet(wk["menu"], "WebKit grid ⋯ menu")
        check(wk["shift"] == "已选 2 张" and wk["selectEsc"] == "照片", "WebKit: Shift range and Esc leave select mode")
        check(wk["editorArrow"]["after"].startswith("2 /"), "WebKit: ArrowRight in the editor")
        check(wk["escBack"] == "grid" and wk["focus"]["id"] is not None, "WebKit: Esc back to the grid, focus on the photo")
        check(wk["clearDialog"] == 1, "WebKit computer: clearing asks first")
        for label in ("webkitPhone", "webkitIpad"):
            touch = steps[label]
            check(touch["env"]["fine"] is False and touch["env"]["hover"] is False, f"{label}: touch device (no fine pointer, no hover)")
            check(touch["cursor"] != "pointer" and touch["ring"] in ("", "none"), f"{label}: no hover cue ({touch['cursor']})")
            check(touch["clearDialog"] == 0 and touch["clearScreen"] == "start", f"{label}: clearing is one tap as today")
    check(errors == {}, f"no driver step failed: {errors}")
