"""U1 phone-fast-export 真实照片浏览器验收测试（390×844 与 430×932，真 card250 + g250）。

验收范围：
1. 点按次数（390×844 和 430×932，只点屏幕内可见控件）：留着拍摄配方 4 下、换配方 5 下；点「存到相册」不弹导出选项表；无尾随「好」。
2. 就绪条与队列：第 3 张导出后滑到第 4 张再导出，两张都在列且都能保存，栏上写出正确照片，不丢结果。
3. 顶栏 HDR：首次有 RAW 默认开；关掉重载后记住；仅 JPG 与 P0 双联置灰并带既有原因文案。
4. 顶栏与「⋯」：中间是配方名 + 第几张/共几张（不含文件名）；⋯ 全部 5 项均打开既有面板。
5. 图库与起始页减负：复制前无「套用到这一组」；单张组无组标题；排序进菜单；胶片柜移除；起始页已导入校准收成一行。
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from gxr_studio.camdata import calib_export, store
from support.export_pipeline_cases import check
from support.mobile_ui_calib import pack_files
from test_g250 import _need_g185_or_g250
from test_mobile_camera_recipe_browser import _card250_pair, _prereq
from test_mobile_ui_browser import _drive

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "support" / "mobile_fast_export_browser.mjs"
DATE = re.compile(rb"\d{4}:\d\d:\d\d \d\d:\d\d:\d\d")


def _shifted(src: Path, dst: Path, seconds: int) -> None:
    from gxr_studio.meta import read_jpeg_meta

    taken = read_jpeg_meta(src.with_suffix(".JPG")).taken
    check(taken is not None, "sample photo has capture time")
    assert taken is not None
    old = taken.strftime("%Y:%m:%d %H:%M:%S").encode()
    new = (taken + timedelta(seconds=seconds)).strftime("%Y:%m:%d %H:%M:%S").encode()
    data = src.read_bytes()
    check(old in data and len(DATE.findall(new)) == 1, "capture timestamp rewritten")
    dst.write_bytes(data.replace(old, new))


def test_mobile_fast_export_acceptance(tmp_path: Path) -> None:
    _prereq()
    raw250, lib250, dat250, fx250 = _need_g185_or_g250(250)
    pair1 = _card250_pair(1)
    pair2 = _card250_pair(2)  # P0 diptych (JPG-only on phone)
    pair3 = _card250_pair(3)
    pair5 = _card250_pair(5)

    # Synthetic calibration pack from g250 stock04 covering all 3 units
    _parsed, entries = store.entries_from_dat(raw250, library=lib250)
    all_units = ("jlaunch", "qlaunch", "l08firm")
    pack = pack_files({name: entries[(unit, 4)] for unit, name in enumerate(all_units)})
    pack_zip = tmp_path / "GXR 校准数据 2026-11-18.zip"
    pack_zip.write_bytes(calib_export.zip_bytes(pack, (2026, 11, 18, 9, 0, 0)))

    # A JPG-only photo in the main group (+20s from pair1) and a solo photo shifted +45m away (1-photo group)
    jpg_only = tmp_path / "R9990101.JPG"
    _shifted(pair1[1], jpg_only, 20)
    solo_jpg = tmp_path / "R9990102.JPG"
    solo_dng = tmp_path / "R9990102.DNG"
    _shifted(pair1[1], solo_jpg, 2700)
    _shifted(pair1[0], solo_dng, 2700)

    base_files = [str(p) for pair in (pair1, pair2, pair3, pair5) for p in pair] + [str(dat250), str(fx250)]
    grid_files = [*base_files, str(jpg_only), str(solo_jpg), str(solo_dng)]

    report_path = tmp_path / "u1-report.json"
    manifest: dict[str, Any] = {
        "pack": str(pack_zip),
        "files": base_files,
        "count": 4,
        "gridFiles": grid_files,
        "gridCount": 6,
        "rawStems": [pair1[0].stem, pair3[0].stem, pair5[0].stem],
        "diptychStem": pair2[0].stem,
        "jpgOnlyStem": jpg_only.stem,
        "soloStem": solo_dng.stem,
        "reportPath": str(report_path),
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = _drive(DRIVER, manifest_path, tmp_path, "", timeout=900)
    check(report_path.is_file(), "u1 browser driver wrote report file")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report_out = __import__("os").environ.get("GXR_U1_REPORT_OUT")
    if report_out:
        Path(report_out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    check(result.returncode == 0 and report.get("error") is None, "u1 browser driver completed without error")
    check(report["pageErrors"] == [], "no page errors during u1 acceptance")

    steps = report["steps"]

    # Acceptance 1: Tap counts at 390x844 and 430x932
    for key in ("tap390", "tap430"):
        tap = steps[key]
        check(tap["keepTaps"] == 4, "keep shooting recipe takes 4 taps")
        check(tap["changeTaps"] == 5, "change recipe takes 5 taps")
        check(tap["sheetAppearedKeep"] is False, "export sheet never appears on 存到相册 (keep)")
        check(tap["sheetAppearedChange"] is False, "export sheet never appears on 存到相册 (change)")
        check(tap["hasTrailingOkKeep"] is False and tap["hasTrailingOkChange"] is False, "no trailing 好 after save")
        check(tap["exportBtnTextBefore"] == "存到相册", "editor export button says 存到相册")
        check(tap["exportBtnTextWhileReady"] == "存到相册", "editor export button never morphs")
        check(tap["hdrOnFirstRun"] == "true", "first-run RAW photo has HDR on")
        check("做好了 · 存进相册" in tap["readyTextKeep"], "ready bar says 做好了 · 存进相册")
        check(tap["navMatchesPickedRecipe"] is True, "editor top bar shows picked recipe name")
        check(tap["keepHdrResult"]["count"] == 1 and tap["keepHdrResult"]["active"] is True, "share called with user activation")

    # Acceptance 2: Ready bar queue (photo 3 + swipe + photo 4 -> both listed and both save) + running bar share-ready
    q = steps["readyQueue"]
    check(q["navHasFilename"] is False, "editor top bar hides filename")
    check(q["navSubMatchesPos"] is True, "editor top bar shows position 3 / 6")
    check(q["readyHasPos3"] is True, "ready bar names photo 3")
    check(q["readyBarPhoto3Height"] <= 52, "single-photo collapsed ready bar is <= 52pt tall")
    check(q["readyBarPhoto3ClearsToolbar"] is True, "single-photo ready bar never covers toolbar or 存到相册")
    check(
        q["runningShareSeen"]["seen"] is True and "存进相册" in q["runningShareSeen"]["text"],
        "running bar offers direct 存进相册 for already-finished carryover",
    )
    check(q["readyHasPairCount"] is True, "ready bar counts 2 photos")
    check(q["readyBarPairCollapsedHeight"] <= 52, "multi-photo collapsed ready bar is <= 52pt tall")
    check(q["readyBarPairClearsToolbar"] is True, "multi-photo ready bar never covers toolbar or 存到相册")
    check(q["readyNamesBoth"] is True, "expanded ready bar details name both photo 3 and photo 4")
    check(q["sharedTotal"] == 2 and q["hasPhoto3"] and q["hasPhoto4"], "both photo 3 and photo 4 are saved without loss")

    # Acceptance 2b: Double tap on 存到相册 produces 1 export and 1 share with 1 file
    dt = steps["doubleTap"]
    check(dt["callCount"] == 1 and dt["totalFiles"] == 1, "double tap exports once and shares 1 file")
    check("第 4 张做好了 · 存进相册" in dt["readyText"], "double tap ready bar stays single photo")

    # Acceptance 3: HDR top bar (including filled accent contrast on vs off)
    hdr = steps["hdr"]
    check(hdr["hdrInitial"] == "true", "first-run RAW photo starts with HDR on")
    check(
        hdr["hdrStyleOn"]["bg"] == "rgb(42, 100, 204)" and hdr["hdrStyleOn"]["color"] == "rgb(255, 255, 255)",
        "HDR on state uses filled accent background with white text",
    )
    check(hdr["hdrAfterOff"] == "false", "toggling HDR off updates switch")
    check(hdr["hdrStyleOff"]["bg"] != hdr["hdrStyleOn"]["bg"], "HDR off state has distinct background")
    check(hdr["hdrAfterReload"] == "false", "saved HDR off survives reload")
    check(hdr["persistedPrefs"] == {"size": "medium", "quality": "low", "hdr": "0"}, "size, quality, and HDR persist across reload")
    check(hdr["jpgHdrDisabled"] is True and hdr["jpgHdrReason"] == "HDR 需要 RAW 文件", "JPG-only HDR disabled with reason")
    check(hdr["jpgHdrToast"] == "HDR 需要 RAW 文件", "tapping disabled HDR shows reason text")
    check(hdr["diptychHdrDisabled"] is True and hdr["diptychHdrReason"] == "HDR 需要 RAW 文件", "P0-diptych HDR disabled with reason")

    # Acceptance 4: Working ⋯ (hides paste/apply before copying, shows all 6 after copying, approved footnote)
    more = steps["more"]
    check(
        more["moreEntriesBeforeCopy"] == ["复制调整", "导出选项", "做成关机图", "照片信息"],
        "⋯ hides 粘贴 and 套用到这一组 before copying",
    )
    check(
        more["moreEntriesAfterCopy"] == ["复制调整", "粘贴", "套用到这一组", "导出选项", "做成关机图", "照片信息"],
        "⋯ has all 6 entries in order after copying",
    )
    check(more["photoInfoHasStem"] is True, "照片信息 shows filename")
    check(more["exportOptionsTitle"] == "导出选项", "导出选项 sheet opens with correct title")
    check(
        more["exportOptionsFoot"] == "「存到相册」每次都按这里的设置做。做好后点「存进相册」，在系统面板里点「存储图像」。",
        "导出选项 footnote matches manager-approved copy",
    )
    check(more["copySheetOpened"] is True, "复制调整 opens copy sheet")
    check(more["applyGroupAfterCopy"] > 0, "套用到这一组 appears on grid after copying")
    check(more["pasteConfirmTitle"] == "套用到这张？", "粘贴 opens confirm sheet")
    check("套用到这一组的" in more["groupConfirmTitle"], "套用到这一组 opens confirm sheet")

    # Acceptance 5: Grid & start screen calm-down (+ solo separation + grid ⋯ header alignment)
    grid = steps["grid"]
    check(grid["startCollapsed"]["height"] <= 68, "start screen calib collapses to one line")
    check(
        grid["startCollapsed"]["unitsVisible"] is False and grid["startCollapsed"]["actsVisible"] is False,
        "lens list and replace/delete collapsed",
    )
    check(grid["startCollapsed"]["sheetOpens"] is True, "tapping collapsed calib opens full sheet")
    check(grid["gridBeforeCopy"]["applyGroupButtons"] == 0, "no 套用到这一组 before copying")
    check(grid["gridBeforeCopy"]["topSortButtons"] == 0, "sort removed from top of grid")
    check(grid["gridBeforeCopy"]["singleGroupHeadCount"] == 0, "single-photo group has no separate title")
    check(
        grid["gridBeforeCopy"]["soloRowInfo"]["hasSoloSep"] is True and grid["gridBeforeCopy"]["soloRowInfo"]["gapPx"] >= 10,
        "headerless solo photo has visible separation from group above",
    )
    check(grid["gridMenuHeaderSameLine"] is True, "grid ⋯ sheet keeps 完成 on the same line as 更多")
    check(grid["hasSortInMenu"] is True and grid["sortReversed"] is True, "sort works inside grid ⋯ menu")
    check(grid["hasFilmCabinet"] is False, "胶片柜 removed from grid menu")
