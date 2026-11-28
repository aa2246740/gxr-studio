"""同名照片（机主 2026-11-28，``browser_serial``）。

- 原来那张好好的，又选进一张同名、不是同一份的（文件夹 B）：两张都留下，第 2 份标「(2)」、读屏名称带「名字 (2)」，
  说一句「有 1 张同名照片另外留下了」。原来那张 id、调整和显卡里的源都不动（再打开不再上传）；第 2 份打开传进显卡的是 B 的 DNG，
  编辑页顶栏和照片信息写「名字 (2)」。
- 倒着、不带文件夹、重新拷过（修改时间变了）再选回来：认出各是哪一份，不多留一张，调整还在原来那份上。
- 两份一起导出（电脑上是 ZIP 加兜底链接）和单张导出：文件名「名字_gxr.jpg」和「名字 (2)_gxr.jpg」，不撞名。
- 没有文件夹、一次选进同名的两个 RAW 和一个 JPG：按拍摄时间配，JPG 配上同一次拍的 RAW，另一个 RAW 单独一张。

只用本地 card250 实拍照片（第 1、3 对；第 3 对改成第 1 对的名字）和 g250 相机配方文件；报告里没有文件名、拍摄时间或 EXIF。
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from support.export_pipeline_cases import check
from test_g250 import _need_g185_or_g250
from test_mobile_camera_recipe_browser import _card250_pair, _prereq
from test_mobile_ui_browser import _drive

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "support" / "mobile_samename_browser.mjs"
KEPT_ONE = "有 1 张同名照片另外留下了"


def _copy(src: Path, dst: Path, mtime: float | None = None) -> str:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    if mtime is not None:
        os.utime(dst, (mtime, mtime))
    return str(dst)


@pytest.fixture(scope="module")
def steps(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    _prereq()
    tmp = tmp_path_factory.mktemp("samename")
    _raw, _lib, dat250, fx250 = _need_g185_or_g250(250)
    a_dng, a_jpg = _card250_pair(1)
    b_dng, b_jpg = _card250_pair(3)
    stem = a_dng.stem
    # B：另一张照片，改成 A 的名字，放在自己的文件夹里（选文件夹时带上 webkitRelativePath）。
    folder_b = tmp / "pickB"
    _copy(b_dng, folder_b / a_dng.name)
    _copy(b_jpg, folder_b / a_jpg.name)
    first = [_copy(a_dng, tmp / "first" / a_dng.name), _copy(a_jpg, tmp / "first" / a_jpg.name)]
    # 重新拷一遍（修改时间换成现在往后一小时），倒着、交错着给，不带文件夹。
    later = time.time() + 3600
    again = [
        _copy(b_jpg, tmp / "again" / "B" / a_jpg.name, later),
        _copy(a_dng, tmp / "again" / "A" / a_dng.name, later),
        _copy(b_dng, tmp / "again" / "B" / a_dng.name, later),
        _copy(a_jpg, tmp / "again" / "A" / a_jpg.name, later),
    ]
    # 拍摄时间配对：A 的 RAW、B 的 RAW（同名）、A 的 JPG，不带文件夹。
    trio = [
        _copy(a_dng, tmp / "trio" / "A" / a_dng.name),
        _copy(b_dng, tmp / "trio" / "B" / a_dng.name),
        _copy(a_jpg, tmp / "trio" / "A" / a_jpg.name),
    ]
    report_path = tmp / "report.json"
    manifest = {
        "stem": stem,
        "first": first,
        "dat": str(dat250),
        "bin": str(fx250),
        "folderB": str(folder_b),
        "again": again,
        "trio": trio,
        "aSize": a_dng.stat().st_size,
        "bSize": b_dng.stat().st_size,
        "reportPath": str(report_path),
    }
    check(manifest["aSize"] != manifest["bSize"], "the two DNGs differ in size (the upload size tells them apart)")
    manifest_path = tmp / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = _drive(DRIVER, manifest_path, tmp, "", timeout=900)
    check(report_path.is_file(), "driver wrote its report")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    check(result.returncode == 0 and report.get("error") is None, f"driver completed without error: {report.get('error')}")
    check(report["pageErrors"] == [], f"no page errors: {report['pageErrors']}")
    return {**report["steps"], "flowErrors": report["flowErrors"], "ms": report["ms"]}


def _flow_ok(steps: dict[str, Any], name: str) -> dict[str, Any]:
    check(name not in steps["flowErrors"], f"driver flow {name} completed: {steps['flowErrors'].get(name)}")
    return steps[name]


def test_mobile_samename_two_different_photos_both_stay(steps: dict[str, Any]) -> None:
    keep = _flow_ok(steps, "keep")
    check(keep["gpu"] is True, "WebGPU (SwiftShader) is on")
    check(keep["firstPlain"] is True, "the first photo keeps the plain id (file name, lower case)")
    ids = keep["ids"]
    check(ids == {"count": 2, "firstKept": True, "secondFromFile": True}, f"both stay; the second id comes from its file: {ids}")
    check(keep["toast"] == KEPT_ONE, f"toast says one was kept: {keep['toast']}")
    second, first = keep["secondCell"], keep["firstCell"]
    check(second["copyBadge"] == "(2)" and second["ariaCopy"] is True, f"grid: the second copy shows (2) and its name: {second}")
    check(first["copyBadge"] is None and first["ariaCopy"] is False, f"grid: the first copy has no mark: {first}")
    check(first["rawJpg"] and second["rawJpg"], "each copy is a RAW+JPG pair from its own folder")
    check(keep["rows"] == {"firstIsA": True, "secondIsA": False}, f"the first copy still holds A's files: {keep['rows']}")
    opened = keep["secondOpen"]
    check(opened["uploadB"] is True, "opening the second copy uploads B's DNG")
    check(opened["posNamed"] is True and opened["navId"] is True, f"editor header names the second copy: {opened}")
    check(opened["exposure"] == "0.0", f"the second copy starts unedited: {opened['exposure']}")
    check(keep["secondInfo"] is True, "photo info file name ends in (2)")


def test_mobile_samename_first_copy_keeps_its_edits_and_gpu_source(steps: dict[str, Any]) -> None:
    keep = _flow_ok(steps, "keep")
    check(keep["firstUpload"] is True and keep["firstPosPlain"] is True, "first open uploads A's DNG; header has no name")
    check(keep["edited"] not in ("0.0", ""), f"exposure was edited: {keep['edited']}")
    reopen = keep["firstReopen"]
    check(reopen["uploads"] == 1, f"reopening the first copy needs no new upload (its GPU source is still current): {reopen}")
    check(reopen["exposure"] == keep["edited"] and reopen["posPlain"] is True, f"the first copy keeps its edit: {reopen}")
    check(keep["firstCell"]["pencil"] is True and keep["secondCell"]["pencil"] is False, "the pencil stays on the first copy")


def test_mobile_samename_recopied_files_map_back_to_their_copies(steps: dict[str, Any]) -> None:
    again = _flow_ok(steps, "keep")["again"]
    check(again["sameIds"] is True, "re-picking identical re-copies (new mtime) keeps the same two photos, none added")
    check(again["toast"] != KEPT_ONE, f"nothing new was kept: {again['toast']}")
    check(again["firstIsA"] is True and again["secondIsA"] is False, f"each file went back to its own copy: {again}")
    check(again["firstEdited"] is True and again["secondEdited"] is False, f"edits stay on the right copy: {again}")
    check(again["firstPencil"] is True and again["secondPencil"] is False, f"grid pencils follow the edits: {again}")


def test_mobile_samename_export_names_do_not_collide(steps: dict[str, Any]) -> None:
    keep = _flow_ok(steps, "keep")
    batch = keep["batch"]
    check(batch == {"zip": True, "count": 2, "unique": True, "firstName": True, "secondName": True}, f"batch export names: {batch}")
    check(keep["single"] == {"count": 1, "secondName": True}, f"single download of the second copy: {keep['single']}")


def test_mobile_samename_without_folders_pairs_by_capture_time(steps: dict[str, Any]) -> None:
    timed = _flow_ok(steps, "time")
    check(timed["kinds"] == {"pair": 1, "rawOnly": 1}, f"one RAW+JPG pair and one lone RAW: {timed['kinds']}")
    check(timed["timesDiffer"] is True, "the JPG paired with the RAW shot at the same time (the lone RAW is the other shot)")
    check(timed["toast"] == KEPT_ONE, f"toast says one was kept: {timed['toast']}")
