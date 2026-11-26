"""电脑上一批下载打成一个 ZIP（2026-11-26，desktop-accept F1 / F2 / F9 / F10 / F11，`browser_serial`）。

- Chromium 电脑（1440×900，没有触摸点）：11 张、14 张一批各点一次「下载」，都只有 1 个 .zip；用 /usr/bin/unzip 解开，
  每个条目的名字和 SHA-256 等于同样选项下单独导出那一张时下载的 JPG。1 张一批仍是 1 个 JPG。WebKit 电脑 14 张一批也是
  1 个 ZIP、14 个条目，和 Chromium 的逐字节相同。
- held：「先下载这 N 张，剩下 M 张接着做」，点一次 1 个 ZIP 并接着做，最后一包是剩下的；拆包时「下载第 1/2 包」。
- WebKit 手机和 iPad（有触摸）：一批仍走共享面板、不下载，只有 held 那句换了。
- not_aligned 在详情里有自己的一句；文件读不了以后按钮是「重新选这张」，打开选照片，选回来接着做成。

只用本地 card250 实拍照片（加 3 张街拍 JPG 凑 14 张）和 g250 相机配方/校准；报告里没有文件名、拍摄时间或 EXIF。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from gxr_studio.camdata import calib_export, store
from support import samples
from support.export_pipeline_cases import check
from support.mobile_ui_calib import pack_files
from test_g250 import _need_g185_or_g250
from test_mobile_camera_recipe_browser import _card250_pair, _prereq
from test_mobile_ui_browser import _drive

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "support" / "mobile_desktop_zip_browser.mjs"
ZIP_NAME = re.compile(r"^GXR-export-\d{8}-\d{4}-(\d+)\.zip$")
NOT_ALIGNED = "RAW 和相机 JPG 画面对不上，这张存普通版本"
UNZIP = Path("/usr/bin/unzip")
SHOTS = Path("/Volumes/Workspace/Scratch/gxr-studio-hillclimb/round-6/studio-desktop-export/shots")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _street_jpgs(n: int) -> list[Path]:
    folder = samples.captures() / "street-g173-20261107"
    if not folder.is_dir():
        samples.missing("captures/street-g173-20261107 不在本机")
    jpgs = sorted(p for p in folder.iterdir() if p.suffix.upper() == ".JPG")
    if len(jpgs) < n:
        samples.missing(f"street-g173 fewer than {n} JPGs")
    return jpgs[:n]


def _run(tmp_path: Path, mode: str, extra: dict[str, Any] | None = None) -> tuple[dict[str, Any], Path]:
    _prereq()
    raw250, lib250, dat250, fx250 = _need_g185_or_g250(250)
    pairs = [_card250_pair(i) for i in range(1, 12)]
    street = _street_jpgs(3)
    _parsed, entries = store.entries_from_dat(raw250, library=lib250)
    all_units = ("jlaunch", "qlaunch", "l08firm")
    pack = pack_files({name: entries[(unit, 4)] for unit, name in enumerate(all_units)})
    pack_zip = tmp_path / "GXR 校准数据 2026-11-26.zip"
    pack_zip.write_bytes(calib_export.zip_bytes(pack, (2026, 11, 26, 9, 0, 0)))
    out_dir = tmp_path / "files"
    out_dir.mkdir()
    report_path = tmp_path / "report.json"
    stems11 = [dng.stem for dng, _jpg in pairs]
    shots = SHOTS if SHOTS.parent.is_dir() else tmp_path / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "mode": mode,
        "pack": str(pack_zip),
        "files": [str(p) for pair in pairs for p in pair] + [str(p) for p in street] + [str(dat250), str(fx250)],
        "count": len(pairs) + len(street),
        "stems11": stems11,
        "stems14": stems11 + [p.stem for p in street],
        "heldStems": stems11[:5],
        "holdAfter": 3,
        "partStems": stems11[5:9],
        "outDir": str(out_dir),
        "shotsDir": str(shots),
        "reportPath": str(report_path),
        **(extra or {}),
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = _drive(DRIVER, manifest_path, tmp_path, "", timeout=3600)
    check(report_path.is_file(), "driver wrote its report")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    check(result.returncode == 0 and report.get("error") is None, f"driver completed without error: {report.get('error')}")
    check(report["pageErrors"] == [], f"no page errors: {len(report['pageErrors'])}")
    return report["steps"], out_dir


def _unzip(zip_path: Path, dest: Path) -> dict[str, str]:
    """用 /usr/bin/unzip 测一遍再解开；返回 {条目名: SHA-256}。"""
    tested = subprocess.run([str(UNZIP), "-t", str(zip_path)], check=False, capture_output=True, text=True)
    check(tested.returncode == 0 and "No errors detected" in tested.stdout, "unzip -t passes")
    dest.mkdir(parents=True)
    done = subprocess.run([str(UNZIP), "-q", str(zip_path), "-d", str(dest)], check=False, capture_output=True)
    check(done.returncode == 0, "unzip extracts")
    return {p.name: _sha(p) for p in dest.iterdir()}


def _one_zip(out_dir: Path, row: dict[str, Any], prefix: str, n: int, what: str) -> dict[str, str]:
    names = row["names"]
    check(len(names) == 1, f"{what}: exactly one download per click (got {len(names)})")
    match = ZIP_NAME.match(names[0])
    check(match is not None, f"{what}: the download is a GXR-export-…zip")
    check(match is not None and int(match.group(1)) == n, f"{what}: the zip name carries the file count")
    entries = _unzip(out_dir / f"{prefix}{names[0]}", out_dir / f"{prefix}unzipped")
    check(len(entries) == n, f"{what}: the zip holds {n} entries (got {len(entries)})")
    return entries


def _backup_zip(row: dict[str, Any], n: int, what: str) -> None:
    backup = row["backup"]
    check(backup is not None, f"{what}: the sheet after the download is open")
    check(backup["text"] == f"已经开始下载 1 个 ZIP，里面有 {n} 张", f"{what}: the sheet says one ZIP with {n} photos")
    check("哪个文件没下下来" not in backup["all"], f"{what}: no 'which file is missing' line for a ZIP")
    check(backup["summary"] == "单张下载" and backup["foldOpen"] is False, f"{what}: per-file links sit in a closed 单张下载 fold")
    check(backup["linksInFold"] == n and len(backup["links"]) == n, f"{what}: the fold lists every file")


@pytest.mark.skipif(not UNZIP.is_file(), reason="/usr/bin/unzip is required")
def test_mobile_desktop_batch_downloads_one_zip(tmp_path: Path) -> None:
    steps, out_dir = _run(tmp_path, "zip")
    pc = steps["chromium"]
    check(pc["env"]["touch"] == 0 and pc["env"]["fine"] is True, "computer context: no touch, fine pointer")
    singles: dict[str, str] = {}
    for row in pc["singles"]:
        check(len(row["names"]) == 1 and row["names"][0].lower().endswith(".jpg"), "a 1-photo batch downloads 1 JPG")
        check(row["dockGone"] is True, "a 1-photo download closes the ready bar")
        singles[row["names"][0]] = _sha(out_dir / f"single-{row['names'][0]}")
    check(len(singles) == 14, f"14 distinct single downloads (got {len(singles)})")
    check(pc["singles"][0]["label"].endswith("做好了 · 下载"), "a single file keeps its label")

    for key, prefix, n in (("b11", "zip11-", 11), ("b14", "zip14-", 14)):
        row = pc[key]
        check(row["label"] == f"下载（{n} 张，ZIP）", f"{n} photos: the ready bar says 下载（{n} 张，ZIP）")
        check(row["aria"].startswith(f"下载（{n} 张，ZIP）"), f"{n} photos: the aria label starts with the visible text")
        entries = _one_zip(out_dir, row, prefix, n, f"Chromium {n} photos")
        for name, digest in entries.items():
            check(singles.get(name) == digest, f"Chromium {n} photos: each entry equals its single download")
        check(row["dockGone"] is True, f"{n} photos: the ready bar closes after the click")
        _backup_zip(row, n, f"Chromium {n} photos")
    b11 = pc["b11"]
    check(b11["linkDownloads"] == 1, "a per-file link still downloads its file")
    check(b11["linkMarked"] is True and b11["linkText"].endswith("✓"), "a clicked per-file link shows ✓")
    check(pc["crc"]["maxSliceMs"] < 50, f"CRC never blocks the main thread for 50 ms ({pc['crc']['maxSliceMs']:.1f})")
    check(pc["share"] == 0, "computer: the share panel never opened")

    if steps.get("webkitMissing"):
        pytest.skip("Playwright WebKit is not installed")
    wk = steps["webkit"]
    check(wk["env"]["touch"] == 0 and wk["env"]["fine"] is True, "WebKit computer context: no touch, fine pointer")
    check(wk["b14"]["label"] == "下载（14 张，ZIP）", "WebKit: the ready bar says 下载（14 张，ZIP）")
    wk_singles: dict[str, str] = {}
    for row in wk["singles"]:
        check(len(row["names"]) == 1 and row["names"][0].lower().endswith(".jpg"), "WebKit: a 1-photo batch downloads 1 JPG")
        wk_singles[row["names"][0]] = _sha(out_dir / f"wksingle-{row['names'][0]}")
    wk_entries = _one_zip(out_dir, wk["b14"], "wk14-", 14, "WebKit 14 photos")
    check(wk_entries == wk_singles, "WebKit: each zip entry equals WebKit's own single download")
    chromium14 = {p.name: _sha(p) for p in (out_dir / "zip14-unzipped").iterdir()}
    check(sorted(wk_entries) == sorted(chromium14), "WebKit and Chromium zips hold the same file names")
    # 相机 JPG 由浏览器解码（export_worker decodeJpeg，createImageBitmap）：两个内核解出来的像素可以不同，单张下载就已经不同，
    # 和 ZIP 无关。ZIP 只是把每个浏览器自己的单张字节原样装进去：WebKit 单张和 Chromium 单张一样的，ZIP 条目也一样。
    for name, digest in wk_singles.items():
        check((digest == singles[name]) == (wk_entries[name] == chromium14[name]), "WebKit vs Chromium: zips differ only where singles differ")
    _backup_zip(wk["b14"], 14, "WebKit 14 photos")


@pytest.mark.skipif(not UNZIP.is_file(), reason="/usr/bin/unzip is required")
def test_mobile_desktop_held_batch_zip(tmp_path: Path) -> None:
    steps, out_dir = _run(tmp_path, "held")
    held = steps["held"]
    check(held["phase"] == "held", "the batch holds at the (forced) memory cap")
    check(held["heldLabel"] == "先下载这 3 张，剩下 2 张接着做", "held: the bar says it is held and how many follow")
    check(held["heldAria"].startswith(held["heldLabel"]), "held: the aria label starts with the visible text")
    first = _one_zip(out_dir, {"names": held["heldNames"]}, "held1-", 3, "held release")
    check(held["phaseAfterTap"] in ("running", "ready"), "held: the queue resumes in the same click")
    check(held["heldBackup"] is not None and held["heldBackup"]["text"] == "已经开始下载 1 个 ZIP，里面有 3 张", "held: the sheet says one ZIP")
    check(held["restLabel"] == "下载（2 张，ZIP）", "held: the rest finish as one more batch")
    rest = _one_zip(out_dir, {"names": held["restNames"]}, "held2-", 2, "held rest")
    check(not set(first) & set(rest) and len(set(first) | set(rest)) == 5, "held: the two ZIPs hold all 5 files once")
    check(held["dockGone"] is True, "held: the bar closes after the last ZIP")
    parts = held["parts"]
    check([p["label"] for p in parts] == ["下载第 1/2 包（共 4 张，ZIP）", "下载第 2/2 包（共 4 张，ZIP）"], "parts: one label per part")
    seen: set[str] = set()
    for k, part in enumerate(parts, start=1):
        names = set(_one_zip(out_dir, part, f"part{k}-", 2, f"part {k}"))
        check(not names & seen, "parts: no file in two parts")
        seen |= names
    check(held["share"] == 0, "computer: the share panel never opened")


def test_mobile_touch_devices_keep_sharing(tmp_path: Path) -> None:
    steps, _out = _run(tmp_path, "touch")
    if steps.get("webkitMissing"):
        pytest.skip("Playwright WebKit is not installed")
    for label, touch in (("webkitPhone", 1), ("webkitIpad", 5)):
        st = steps[label]
        check(st["env"]["touch"] >= touch, f"{label}: has touch points")
        check(st["batch"]["label"] == "2 张做好了 · 存进相册", f"{label}: batch label unchanged")
        check("存进相册，打开系统分享" in st["batch"]["aria"], f"{label}: batch aria unchanged")
        check(st["batch"]["shared"] == 2, f"{label}: both files go to one share call")
        check(st["phase"] == "held", f"{label}: the batch holds at the forced cap")
        check(st["heldLabel"] == "先把这 3 张存进相册，剩下 2 张接着做", f"{label}: held label says it is held")
        check(st["heldShared"] == 3, f"{label}: the held files go to one share call")
        check(st["restLabel"] == "2 张做好了 · 存进相册", f"{label}: the rest finish with the usual label")
        check(st["downloads"] == 0, f"{label}: never downloads")


def test_mobile_desktop_not_aligned_note(tmp_path: Path) -> None:
    steps, _out = _run(tmp_path, "hdr")
    hdr = steps["hdr"]
    reasons = hdr["reasons"]
    check("not_aligned" in reasons, "the card250 HDR batch has at least one not_aligned pair")
    check(hdr["notes"].count(NOT_ALIGNED) == reasons.count("not_aligned"), "every not_aligned photo has its own note in 详情")
    check("这张照片不支持 HDR" not in hdr["notes"] or "not_aligned" not in reasons, "not_aligned no longer shows the generic line")


def test_mobile_desktop_repick_after_read_failure(tmp_path: Path) -> None:
    pair = _card250_pair(3)
    copy_dir = tmp_path / "moved"
    copy_dir.mkdir()
    extra = {
        "copyFrom": [str(p) for p in pair],
        "copyTo": [str(copy_dir / p.name) for p in pair],
        "repickStems": [_card250_pair(1)[0].stem, pair[0].stem, _card250_pair(5)[0].stem],
    }
    steps, _out = _run(tmp_path, "repick", extra)
    st = steps["repick"]
    check(st["failures"] == ["read"], "the deleted copy fails as unreadable")
    check(st["buttons"][0] == "重新选这张", f"details: the button says 重新选这张 ({len(st['buttons'])} buttons)")
    check("重试这张" not in st["buttons"], "details: no retry of the dead file")
    check(st["chooserOpened"] is True, "重新选这张 opens the photo chooser")
    check(st["after"] is not None and st["after"]["failures"] == 0, "after picking the file again the photo exports")
