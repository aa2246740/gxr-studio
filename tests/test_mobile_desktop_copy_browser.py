"""电脑浏览器上的文案不提 iPhone、相册、共享面板、隔空投送、「文件」或触摸手势（desktop-copy，2026-11-26，`browser_serial`）。

- Chromium 1440×900，没有触摸点、指针精确：起始页、帮助、相机配方文件记住以后的起始页、网格 ⋯、拔卡表、编辑器的校准说明、
  调整提示、LUT、编辑器 ⋯、导出选项（HDR 那句和「下载」按钮的图标）、关机图说明、标志表。每屏的可见文字和
  aria-label / title / placeholder 都不含禁词；起始页的图不画手机；「下载」按钮不用共享图标。
- WebKit 手机（390×844）和 iPad（1032×1376，Mac UA + 5 个触摸点）：同样这些屏，文字和属性与 3b5b535d 上抓下来的
  逐字相同（`support/mobile_desktop_copy_baseline.json`，日期和钟点换成 #）。

默认校准由开发服务器 404（未校准那几句才出来）。只用本地 card250 实拍照片和 g250 相机配方文件；报告里没有文件名、拍摄时间或 EXIF。
重抓基线：`GXR_DESKTOP_COPY_CAPTURE=<文件>`（只在旧提交上用），这时只写文件、不断言。
"""

from __future__ import annotations

import json
import os
import re
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
DRIVER = REPO / "tests" / "support" / "mobile_desktop_copy_browser.mjs"
BASELINE = REPO / "tests" / "support" / "mobile_desktop_copy_baseline.json"
SHARE_GLYPH = "M12 15V3M7.5 7.5 12 3l4.5 4.5M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-7"
FORBIDDEN = re.compile(r"iPhone|iPad|相册|共享面板|隔空投送|AirDrop|「文件」|“文件”|轻点|长按|滑动|捏合|双指|点两下")
# 电脑上每一屏都要走到（拔卡表除外的那几屏缺一不可）。
COMPUTER_SCREENS = (
    "start",
    "help",
    "gridMore",
    "calib",
    "askDat",
    "editor",
    "recipeStatus",
    "adjust",
    "lut",
    "editorMore",
    "exportOptions",
    "offImage",
    "logo",
    "editorPage",
    "remembered",
    "rememberedHelp",
    "rememberedCalib",
    "pulled",
)
COMPARED = tuple(name for name in COMPUTER_SCREENS if name != "editorPage")


def _card_copy(tmp_path: Path, label: str, pairs: list[tuple[Path, Path]]) -> dict[str, Any]:
    folder = tmp_path / f"card-{label}"
    folder.mkdir()
    files = []
    for pair in pairs:
        for src in pair:
            dst = folder / src.name
            shutil.copyfile(src, dst)
            files.append(str(dst))
    return {"dir": str(folder), "files": files, "count": len(pairs)}


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, only: list[str]) -> dict[str, Any]:
    _prereq()
    _raw, _lib, dat250, fx250 = _need_g185_or_g250(250)
    pairs = [_card250_pair(i) for i in (1, 3, 5)]
    monkeypatch.setenv("GXR_MOBILE_DEFAULT_CALIB", "off")
    report_path = tmp_path / "report.json"
    manifest = {
        "only": only,
        "photos": [str(p) for pair in pairs for p in pair],
        "count": len(pairs),
        "dat": [str(dat250), str(fx250)],
        "cards": {label: _card_copy(tmp_path, label, pairs) for label in only},
        "shareGlyph": SHARE_GLYPH,
        "reportPath": str(report_path),
        "debugDir": os.environ.get("GXR_DESKTOP_COPY_DEBUG"),
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    try:
        result = _drive(DRIVER, manifest_path, tmp_path, "", timeout=1800)
    finally:
        for card in manifest["cards"].values():
            shutil.rmtree(card["dir"], ignore_errors=True)
    check(report_path.is_file(), "driver wrote its report")
    report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    check(result.returncode == 0 and report.get("error") is None, f"driver completed without error: {report.get('error')}")
    return report


def _hits(seen: dict[str, Any]) -> list[str]:
    found = FORBIDDEN.findall(seen.get("text") or "")
    for attr in seen.get("attrs") or []:
        found += FORBIDDEN.findall(attr)
    return found


def test_mobile_desktop_copy_computer_has_no_phone_words(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report = _run(tmp_path, monkeypatch, ["computer"])
    check(report["pageErrors"] == [], f"no page errors: {report['pageErrors']}")
    run = report["runs"]["computer"]
    screens, fails = run["screens"], run["fails"]
    env = screens["env"]
    check(env["touch"] == 0 and env["fine"] is True, "computer context has no touch points and a fine pointer")
    check(fails == {}, f"every computer screen was reached: {sorted(fails)}")
    for name in COMPUTER_SCREENS:
        check(screens.get(name) is not None, f"computer: {name} collected")
        hits = _hits(screens[name])
        check(hits == [], f"computer: {name} has no phone-only words: {sorted(set(hits))}")
    start = screens["start"]
    check(start["hero"]["phone"] is False and start["hero"]["device"] == "computer", "computer: the start picture draws no phone")
    check("选照片，换配方，下载" in start["text"], "computer: heading says 下载")
    check("这台电脑" in start["text"], "computer: privacy line says 这台电脑")
    check("怎么把存储卡接到电脑" in start["text"], "computer: help link wording")
    help_text = screens["help"]["text"]
    for line in (
        "把存储卡插进电脑的 SD 卡槽，或用读卡器接上",
        "在弹出的窗口里打开存储卡的 DCIM 文件夹",
        "第一次用时，再选存储卡最外层的 GXRCE.DAT 和 GXRFX.BIN",
        "拔卡前，先确认照片已经下载好",
    ):
        check(line in help_text, f"computer help sheet: {line}")
    check("把存储卡接到这台电脑，在弹出的窗口里选照片。" in screens["remembered"]["text"], "computer: remembered lead text")
    check("怎么把存储卡接到电脑" in screens["gridMore"]["text"], "computer: grid ⋯ help row")
    check("换配方、下载" in screens["pulled"]["text"], "computer: pulled-card sheet says 下载")
    calib_line = "在 GXR Studio 里导出校准数据，再点上面的按钮选这个文件"
    check(calib_line in screens["recipeStatus"]["text"], "computer: calibration line has no AirDrop")
    check("选择 GXRCE.DAT 和 GXRFX.BIN" in screens["askDat"]["text"], "computer: camera-files button has no 「文件」")
    check("双击圆点回到 0" in screens["adjust"]["text"], "computer: adjust hint says 双击")
    check(screens["lut"]["dom"] == "LUT 只存在这台电脑上，直接套在照片上，不写入相机。", "computer: LUT footnote")
    check("在支持 HDR 的屏幕上看" in screens["exportOptions"]["text"], "computer: HDR line")
    check("下载后把 GXROFF.JPG 拷到存储卡最外层" in screens["offImage"]["text"], "computer: off-image note")
    check("标志只保存在这台电脑上" in screens["logo"]["text"], "computer: logo footnote")
    glyphs = screens["exportOptions"]["glyphs"]
    check(any(g["text"] == "下载" for g in glyphs), f"computer: export options has a 下载 button: {glyphs}")
    check(all(g["share"] is False for g in glyphs), f"computer: no share glyph on download buttons: {glyphs}")


def test_mobile_desktop_copy_phone_and_ipad_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report = _run(tmp_path, monkeypatch, ["phone", "ipad"])
    if report.get("webkitMissing"):
        pytest.skip("Playwright WebKit is not installed")
    check(report["pageErrors"] == [], f"no page errors: {report['pageErrors']}")
    got = {label: {name: report["runs"][label]["screens"].get(name) for name in ("env", *COMPARED)} for label in ("phone", "ipad")}
    for label in ("phone", "ipad"):
        check(report["runs"][label]["fails"] == {}, f"{label}: every screen was reached: {report['runs'][label]['fails']}")
    capture = os.environ.get("GXR_DESKTOP_COPY_CAPTURE")
    if capture:
        Path(capture).write_text(json.dumps(got, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        pytest.skip("baseline captured")
    want = json.loads(BASELINE.read_text(encoding="utf-8"))
    check(got["phone"]["env"]["touch"] == 1 and got["ipad"]["env"]["touch"] == 5, "phone has 1 touch point, iPad 5")
    for label in ("phone", "ipad"):
        for name in COMPARED:
            check(got[label][name] == want[label][name], f"{label}: {name} text and attributes equal the 3b5b535d strings")
