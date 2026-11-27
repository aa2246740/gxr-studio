"""电脑浏览器上「存」就是下载（2026-11-26，`browser_serial`）。

- Chromium，没有触摸点、指针精确：编辑器的按钮写「下载」，一键导出后点就绪条的「下载」得到一个浏览器下载；关机图也是下载；网格里两张
  一起导出，点一次下载 1 个 ZIP（desktop-export：Safari 一次点击只放行 1 个下载），逐个的链接留着兜底。文件名和字节（ZIP 里的条目）
  与「手机」Chromium（一个触摸点）走共享面板的那份逐字节相同。
- HDR 跟着屏幕走：窗口从 SDR 屏挪到 HDR 屏，不重载预览就打开；屏幕显示不了 HDR 时舞台下沿说一句。
- WebKit 手机（390×844）和 iPad（1032×1376，Mac UA + 5 个触摸点），都有触摸：文案不变，仍调共享面板，不下载。

只用本地 card250 实拍照片和 g250 相机配方/校准；报告里没有文件名、拍摄时间或 EXIF。
"""

from __future__ import annotations

import hashlib
import json
import zipfile
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
DRIVER = REPO / "tests" / "support" / "mobile_desktop_download_browser.mjs"
NOTE = "这块屏幕显示不了 HDR，导出的照片仍带 HDR"
FOOT = "「下载」每次都按这里的设置做。做好后点「下载」，文件存到浏览器的下载文件夹。"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _same_files(out: Path, names: list[str], left: str, right: str, what: str) -> None:
    check(len(names) > 0, f"{what}: at least one file")
    for name in names:
        a, b = out / f"{left}{name}", out / f"{right}{name}"
        check(a.is_file() and b.is_file(), f"{what}: both sides wrote the file")
        check(_sha(a) == _sha(b), f"{what}: download bytes equal the shared bytes")


def _unzip_one(out: Path, names: list[str], prefix: str, what: str) -> list[str]:
    """点一次只有 1 个下载，是 ZIP：条目解到 `out/<prefix>unzipped-<名字>`，返回条目名。"""
    check(len(names) == 1 and names[0].endswith(".zip"), f"{what}: one click gives one ZIP")
    with zipfile.ZipFile(out / f"{prefix}{names[0]}") as zf:
        check(zf.testzip() is None, f"{what}: ZIP CRCs check out")
        check(all(info.compress_type == zipfile.ZIP_STORED for info in zf.infolist()), f"{what}: entries are stored")
        entries = zf.namelist()
        for name in entries:
            (out / f"{prefix}unzipped-{name}").write_bytes(zf.read(name))
    return entries


def _touch_unchanged(step: dict[str, Any], label: str) -> None:
    check(step["save"]["text"] == "存到相册", f"{label}: editor button still says 存到相册")
    check(step["save"]["aria"] == "导出当前照片并存到相册", f"{label}: editor button aria unchanged")
    check("做好了 · 存进相册" in step["ready"]["text"], f"{label}: ready bar still says 存进相册")
    check("存进相册，打开系统分享" in step["ready"]["aria"], f"{label}: ready bar aria unchanged")
    check(step["share"]["calls"] >= 1 and step["share"]["canShare"] >= 1, f"{label}: share and canShare were called")
    check(step["downloads"] == 0, f"{label}: no browser download")


def _run(tmp_path: Path, mode: str) -> tuple[dict[str, Any], Path]:
    _prereq()
    raw250, lib250, dat250, fx250 = _need_g185_or_g250(250)
    pair_a = _card250_pair(1)
    pair_b = _card250_pair(3)
    pair_c = _card250_pair(5)

    _parsed, entries = store.entries_from_dat(raw250, library=lib250)
    all_units = ("jlaunch", "qlaunch", "l08firm")
    pack = pack_files({name: entries[(unit, 4)] for unit, name in enumerate(all_units)})
    pack_zip = tmp_path / "GXR 校准数据 2026-11-26.zip"
    pack_zip.write_bytes(calib_export.zip_bytes(pack, (2026, 11, 26, 9, 0, 0)))

    out_dir = tmp_path / "files"
    out_dir.mkdir()
    report_path = tmp_path / "report.json"
    manifest = {
        "mode": mode,
        "pack": str(pack_zip),
        "files": [str(p) for pair in (pair_a, pair_b, pair_c) for p in pair] + [str(dat250), str(fx250)],
        "count": 3,
        "stems": [pair_a[0].stem, pair_b[0].stem],
        "heldStems": [pair_a[0].stem, pair_b[0].stem, pair_c[0].stem],
        "outDir": str(out_dir),
        "reportPath": str(report_path),
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = _drive(DRIVER, manifest_path, tmp_path, "", timeout=1800)
    check(report_path.is_file(), "driver wrote its report")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    check(result.returncode == 0 and report.get("error") is None, f"driver completed without error: {report.get('error')}")
    check(report["pageErrors"] == [], f"no page errors: {report['pageErrors']}")
    return report["steps"], out_dir


def test_mobile_desktop_hdr_follows_the_screen(tmp_path: Path) -> None:
    """页面在 SDR 屏上打开（无头 Chrome 的 media 不匹配），再用 debugForceHdrDisplay「挪到 HDR 屏」，不重载。"""
    steps, _out = _run(tmp_path, "hdr")
    hdr = steps["hdr"]
    sdr = hdr["sdrScreen"]
    check(sdr["exportHdr"] == "true", "RAW photo opens with HDR export on")
    check(sdr["preview"] is False, "SDR screen: no HDR preview")
    check(sdr["note"] == NOTE and sdr["noteVisible"] is True, "SDR screen: the can't-show-HDR line is visible on the stage")
    layouts = [row["layout"] for row in hdr["crop"]]
    check(layouts == ["ipad-landscape", "phone-portrait", "phone-landscape"], f"crop check covers iPad, phone and landscape: {layouts}")
    for row in hdr["crop"]:
        check(row["before"] is True and row["after"] is True, f"{row['layout']}: line shows outside crop mode")
        check(row["cropping"] is False, f"{row['layout']}: line hidden in crop mode, off the bottom handle")
    on = hdr["hdrScreen"]
    check(on["preview"] is True, "moved to the HDR screen: the preview turns on without a reload")
    check(on["note"] is None, "HDR screen: the line is hidden while the preview shows")
    back = hdr["backToSdr"]
    check(back["preview"] is False and back["note"] == NOTE, "moved back: preview off, line back")
    off = hdr["hdrOff"]
    check(off["exportHdr"] == "false" and off["note"] is None, "HDR export off: no line")
    check(hdr["sameDocument"] is True, "no reload happened")


def test_mobile_desktop_download(tmp_path: Path) -> None:
    steps, out_dir = _run(tmp_path, "download")

    pc = steps["computer"]
    check(pc["env"]["touch"] == 0 and pc["env"]["fine"] is True, "computer context has no touch and a fine pointer")
    check(pc["save"]["text"] == "下载" and pc["saveAfter"]["text"] == "下载", "computer: editor button says 下载")
    check(pc["save"]["aria"] == "导出当前照片并下载", "computer: editor button aria contains 下载")
    check(pc["ready"]["text"].endswith("做好了 · 下载"), "computer: ready bar says 下载")
    check("存进相册" not in pc["ready"]["aria"] and "下载" in pc["ready"]["aria"], "computer: ready bar aria says 下载")
    check(len(pc["singleNames"]) == 1, "computer: one tap gives one download")
    check(pc["dockGoneAfterSingle"] is True, "computer: ready bar closes after the download")
    check(FOOT in pc["sheetText"], "computer: export sheet footnote names the download folder")
    check("存到相册" not in pc["sheetText"] and "存储图像" not in pc["sheetText"], "computer: export sheet has no album copy")
    check(pc["batchReady"]["text"] == "下载（2 张，ZIP）", "computer: batch ready bar says 下载（2 张，ZIP）")
    batch_entries = _unzip_one(out_dir, pc["batchNames"], "computerN-", "computer batch")
    check(len(batch_entries) == 2 and len(set(batch_entries)) == 2, "computer: the ZIP holds both files")
    check(pc["dockGoneAfterBatch"] is True, "computer: a multi-file download closes the ready bar like a share")
    check(sorted(pc["backupLinks"]) == sorted(batch_entries), "computer: backup links list the same files")
    check(pc["backupText"].startswith("已经开始下载"), "computer: backup links say the downloads started")
    check(pc["backupClosedByEsc"] is True, "computer: Esc closes the backup links")
    check(pc["heldReady"]["phase"] == "held" and pc["heldReady"]["text"] == "先下载这 2 张，剩下 1 张接着做", "computer: batch held at the cap")
    held_entries = _unzip_one(out_dir, pc.get("heldNames", []), "computerH-", "computer held")
    check(len(held_entries) == 2, "computer: one tap downloads both held files in one ZIP")
    check(pc["phaseAfterHeldTap"] in ("running", "ready"), "computer: the held batch resumes in the same tap")
    check(sorted(pc["backupDuringResume"]) == sorted(held_entries), "computer: backup still lists the held files while it resumes")
    check(pc["restReady"]["text"].endswith("做好了 · 下载"), "computer: the queued photo finished without a second click")
    check(sorted(pc["backupAfterResume"]) == sorted(held_entries), "computer: backup list survives the repaints")
    check(pc["backupClosedByScrim"] is True, "computer: the scrim closes the backup links")
    check(len(pc["restNames"]) == 1 and pc["dockGoneAfterRest"] is True, "computer: the last file downloads and the bar closes")
    check(pc["share"]["calls"] == 0, "computer: share panel never opened")

    ph = steps["phoneChromium"]
    check(ph["env"]["touch"] == 1, "phone Chromium has a touch point")
    check(ph["save"]["text"] == "存到相册" and "存进相册" in ph["ready"]["text"], "phone Chromium keeps album copy")
    check(ph["share"]["files"] == [1, 1, 2] and all(ph["share"]["active"]), "phone Chromium shares inside the tap")
    check(ph["downloads"] == 0, "phone Chromium never downloads")
    phone_single = [f["name"] for f in ph["singleShared"][0]["files"]]
    phone_batch = [f["name"] for call in ph["batchShared"] for f in call["files"]]
    check(phone_single == pc["singleNames"], "single: download file name equals the shared name")
    check(sorted(phone_batch) == sorted(batch_entries), "batch: ZIP entry names equal the shared names")
    _same_files(out_dir, pc["singleNames"], "computer1-", "phone1-", "single")
    _same_files(out_dir, batch_entries, "computerN-unzipped-", "phoneN-", "batch")
    check(pc["offNames"] == ["GXROFF.JPG"], "computer: off-image sheet downloads GXROFF.JPG")
    check([f["name"] for f in ph["offShared"][0]["files"]] == ["GXROFF.JPG"], "phone: off-image sheet shares GXROFF.JPG")
    _same_files(out_dir, pc["offNames"], "computerOff-", "phoneOff-", "off-image")

    if steps.get("webkitMissing"):
        pytest.skip("Playwright WebKit is not installed")
    _touch_unchanged(steps["webkitPhone"], "WebKit phone")
    _touch_unchanged(steps["webkitIpad"], "WebKit iPad")
    check(steps["webkitIpad"]["env"]["touch"] == 5, "WebKit iPad reports 5 touch points")
