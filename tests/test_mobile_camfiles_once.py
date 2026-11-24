"""相机配方文件只选一次（IndexedDB 持久化、双态起始页、库不匹配提示、RAW+JPG 默认相机 JPG、未校准文案拆分）。

所有样本与 DAT 均经 ``tests/support/samples.py`` 查找，缺样本按规则跳过，绝不打印照片文件名、拍摄时间或 EXIF。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from gxr_studio.camdata import datcodec
from raw_support import bayer_scene, build_dng
from support import g262_cases as g262
from support import mobile_ui_export as mx
from support import samples
from support.dng_writer import COLOR_MATRIX
from support.export_pipeline_cases import check
from test_g250 import _need_g185_or_g250
from test_g266 import _need_g266
from test_mobile_ui_browser import DATS, MODULES, PKG, RUNTIME, _drive, _need

REPO = Path(__file__).resolve().parents[1]
UNIT_DRIVER = REPO / "tests" / "support" / "mobile_camfiles_once_unit.mjs"
BROWSER_DRIVER = REPO / "tests" / "support" / "mobile_camfiles_once_browser.mjs"


def _prereq() -> None:
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")


def _single_file_dat() -> tuple[Path, int]:
    for path in DATS:
        if not path.is_file():
            continue
        gen = datcodec.peek_generation(path.read_bytes())
        if datcodec.PROFILES[gen].library_generation is None:
            return path, gen
    samples.missing("no single-file local GXRCE.DAT fixture")
    raise AssertionError("unreachable")


def _make_iso_out_pair(out_dir: Path, stem: str, *, iso: int = 1250) -> tuple[Path, Path]:
    """构造 50mm 镜头但 ISO 超出实拍校准档位（200 / 1600）的合成 DNG+JPG。"""
    when = "2026:11:24 12:00:00"
    height, width = 64, 96
    yy, xx = np.mgrid[0:height, 0:width]
    scene = np.stack(
        [
            0.12 + 0.55 * (xx / (width - 1)),
            0.18 + 0.42 * (yy / (height - 1)),
            0.22 + 0.30 * (1.0 - xx / (width - 1)),
        ],
        axis=-1,
    )
    neutral = [0.65, 1.0, 0.58]
    dng_bytes = build_dng(
        bayer_scene(height, width, scene * np.array(neutral), white=4095),
        bps=16,
        white=4095,
        neutral=neutral,
        orientation=1,
        iso=iso,
        when=when,
        lens=f"RICOH {g262.LENS_50}",
        exif_entries=[(42036, "ASCII", g262.LENS_50)],
        root_extra=[
            (50721, "SRATIONAL", COLOR_MATRIX),
            (50727, "RATIONAL", [1.0, 1.0, 1.0]),
            (50778, "SHORT", [21]),
        ],
    )
    dng_path, jpg_path = out_dir / f"{stem}.DNG", out_dir / f"{stem}.JPG"
    dng_path.write_bytes(dng_bytes)
    exif = Image.Exif()
    exif[271], exif[272], exif[274], exif[306] = "RICOH", "GXR A12", 1, when
    sub = exif.get_ifd(0x8769)
    sub[34855], sub[36867], sub[36868], sub[42036] = iso, when, when, g262.LENS_50
    rgb8 = np.clip(np.round(np.power(scene, 1.0 / 2.2) * 255.0), 0, 255).astype(np.uint8)
    Image.fromarray(rgb8, "RGB").save(jpg_path, "JPEG", quality=92, exif=exif)
    return dng_path, jpg_path


def _run_browser(tmp_path: Path, manifest: dict[str, Any], *, timeout: int = 600) -> dict[str, Any]:
    report_path = tmp_path / f"{manifest['mode']}-report.json"
    mpath = tmp_path / f"{manifest['mode']}-manifest.json"
    mpath.write_text(json.dumps({**manifest, "out": str(report_path)}), encoding="utf-8")
    result = _drive(BROWSER_DRIVER, mpath, tmp_path, "", timeout=timeout)
    if result.returncode != 0 or not report_path.is_file():
        pytest.fail(f"camfiles-once browser driver failed\n{result.stdout[-1500:]}\n{result.stderr[-1500:]}", pytrace=False)
    data: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    check(data.get("error") is None, f"driver error: {data.get('error')}")
    check(data["pageErrors"] == [], f"pageErrors: {data['pageErrors']}")
    return data


def test_mobile_camfiles_once_unit() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    result = subprocess.run(
        [node, "--experimental-strip-types", "--test", str(UNIT_DRIVER)],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ℹ fail 0" in result.stdout or "# fail 0" in result.stdout, result.stdout


@pytest.mark.browser_serial
def test_mobile_camfiles_once_persist_start_mismatch_and_sooc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """持久化+重载恢复、双态起始页、忘掉/换一份、两类不匹配提示与同库兼容、RAW+JPG 默认相机 JPG。"""
    monkeypatch.delenv("GXR_MOBILE_DEFAULT_CALIB", raising=False)
    _prereq()
    _, _, dat266, fx_kubik = _need_g266()
    _, _, dat262, fx262 = g262.need_g262()
    _, _, dat250, fx250 = _need_g185_or_g250(250)
    cross_dat, cross_gen = _single_file_dat()
    pairs = mx.real_pairs(1)
    if not pairs:
        samples.missing("need at least one real DNG+JPG pair")
    real_dng, real_jpg = pairs[0]
    kubik_tag = g262.encode_tag("50", 68, g262.KINO_FINETUNE)
    kubik_dng, kubik_jpg = g262.synth_tagged_pair(tmp_path, "R9980001", kubik_tag)

    manifest: dict[str, Any] = {
        "mode": "main",
        "g266": [str(dat266), str(fx_kubik)],
        "g262": [str(dat262), str(fx262)],
        "altDat": [str(dat250), str(fx250)],
        "altGen": 250,
        "crossGenDat": str(cross_dat),
        "crossGenNum": cross_gen,
        "nonKubikDat": [str(dat250), str(fx250)],
        "nonKubikGen": 250,
        "kubikPair": [str(kubik_dng), str(kubik_jpg)],
        "kubikStem": kubik_dng.stem,
        "realPair": [str(real_dng), str(real_jpg)],
        "realDngOnly": str(real_dng),
        "realStem": real_dng.stem,
    }
    if os.environ.get("GXR_CAMFILES_SHOTS"):
        manifest["shotsDir"] = os.environ["GXR_CAMFILES_SHOTS"]

    data = _run_browser(tmp_path, manifest, timeout=900)
    steps = data["steps"]

    # 1. Start screen (both states) & IndexedDB persistence across reload + clear photos + corrupt rejection
    p = steps["persist"]
    check(p["uploads"] == [], f"DAT/BIN must never leave the device; saw uploads: {p['uploads']}")
    check(p["beforePick"]["camfilesAttr"] == "none", f"first visit state: {p['beforePick']}")
    check("先选一次相机配方文件" in p["beforePick"]["text"] and "GXRCE.DAT" in p["beforePick"]["text"], "first visit explains step 1")
    check(any("选照片" in b for b in p["beforePick"]["buttons"]), "first visit offers photo pick")
    check(p["afterPick"]["camfilesAttr"] == "remembered", "start updates to remembered after pick")
    check("已记住 · 第 266 代" in (p["afterPick"]["status"] or ""), f"remembered status: {p['afterPick']['status']!r}")
    check(p["afterPick"]["buttons"][0] == "从存储卡选照片", f"remembered primary button: {p['afterPick']['buttons']!r}")
    check(
        p["afterReload"]["camfilesAttr"] == "remembered" and "已记住 · 第 266 代" in (p["afterReload"]["status"] or ""), "restored after reload"
    )
    check(p["adoptedAfterReload"]["heading"] == "K-KINO" and p["adoptedAfterReload"]["hasDat"], "photo-only pick after reload renders recipe")
    check(p["afterClearPhotos"]["camfilesAttr"] == "remembered", "clearing photos keeps remembered camera files")
    check(
        p["afterCorruptReload"]["hasDat"] is False and p["afterCorruptReload"]["camfilesAttr"] == "none",
        "corrupted IDB entry rejected on restore",
    )

    # 2. 更多 → 校准与配方文件: 相机配方文件 card (换一份… / 忘掉)
    fr = steps["forgetReplace"]
    check("相机配方文件" in fr["card266"] and "已记住 · 第 266 代" in fr["card266"], "card shows remembered generation")
    check("换一份…" in fr["card266"] and "忘掉" in fr["card266"], "card offers replace and forget")
    check("已记住 · 第 250 代" in fr["cardReplaced"], "replace updates generation")
    check("已记住 · 第 250 代" in (fr["startAfterReplace"]["status"] or ""), "replacement persists across reload")
    check("未记住" in fr["cardForgotten"], "forget updates card state")
    check(fr["startAfterForget"]["camfilesAttr"] == "none", "forget persists across reload")

    # 3. Mismatch: cross library-generation (g158 remembered vs g266 KUBIK photo)
    cg = steps["mismatchCrossGen"]
    check(cg["before"]["promptOpen"] is True, "cross-gen mismatch prompts once")
    check(
        cg["before"]["recipeKey"] in (None, "", "dev-neutral") and cg["before"]["label"] != "电脑预测",
        f"no wrong recipe rendered: {cg['before']}",
    )
    check(
        cg["after"]["promptOpen"] is False and cg["after"]["heading"] == "K-KINO" and cg["after"]["generation"] == 266,
        "picking g266 resolves mismatch",
    )

    # 4. Mismatch: same library_generation (178) but non-KUBIK g250 vs KUBIK photo
    dl = steps["mismatchSameGenDiffLib"]
    check(dl["before"]["promptOpen"] is True, "non-KUBIK vs KUBIK mismatch prompts once")
    check(
        dl["before"]["recipeKey"] in (None, "", "dev-neutral") and dl["before"]["label"] != "电脑预测",
        f"no wrong recipe rendered: {dl['before']}",
    )
    check(
        dl["after"]["promptOpen"] is False and dl["after"]["heading"] == "K-KINO" and dl["after"]["generation"] == 266,
        "picking g266 resolves diff-lib mismatch",
    )

    # 5. Same library (g262 KUBIK serving g266 KUBIK photo): NOT a mismatch
    sl = steps["sameLibNoMismatch"]
    check(
        sl["promptOpen"] is False and sl["errorTile"] is False and sl["heading"] == "K-KINO", f"g262 serves g266 KUBIK without mismatch: {sl}"
    )

    # 6. RAW+JPG without files opens to camera JPG with no sheet; RAW-only still shows 中性转换
    sooc = steps["rawJpgDefault"]
    check(sooc["rawJpgOpen"]["label"] == "相机 JPG", f"RAW+JPG without files must show 相机 JPG, got {sooc['rawJpgOpen']}")
    check(sooc["rawJpgOpen"]["sheetCount"] == 0, "RAW+JPG open must not show blocking sheet")
    check("还差相机配方文件" in sooc["askSheetText"] and "每张卡选一次就行" not in sooc["askSheetText"], "recipe tap opens updated ask sheet")
    check(sooc["afterDatPicked"]["label"] == "电脑预测", f"picking DAT upgrades open editor to RAW prediction: {sooc['afterDatPicked']}")
    raw_only = steps["rawOnlyDefault"]
    check(raw_only["label"] == "中性转换" and raw_only["sheetCount"] == 0, f"RAW-only without files keeps 中性转换: {raw_only}")


@pytest.mark.browser_serial
def test_mobile_camfiles_once_uncalibrated_split(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """区分「完全无校准」（告警 未校准）与「ISO 超出实拍校准范围」（中性 校准范围外（推算）+ 一点即见原因）。"""
    _prereq()
    _, _, dat266, fx_kubik = _need_g266()
    iso_dng, iso_jpg = _make_iso_out_pair(tmp_path, "R9980002", iso=1250)

    # Case 1: default calibration enabled -> ISO 1250 is outside calibrated range (200 / 1600) -> neutral non-warning tile.
    monkeypatch.delenv("GXR_MOBILE_DEFAULT_CALIB", raising=False)
    sub_on = tmp_path / "on"
    sub_on.mkdir()
    on_data = _run_browser(
        sub_on,
        {
            "mode": "uncalibrated",
            "g266": [str(dat266), str(fx_kubik)],
            "isoOutPair": [str(iso_dng), str(iso_jpg)],
            "isoOutStem": iso_dng.stem,
        },
    )
    iso_tile = on_data["steps"]["uncalibratedSplit"]
    check(iso_tile["state"] == "uncalibrated", f"expected uncalibrated stateAttr, got {iso_tile}")
    check(
        "dim" in (iso_tile["className"] or "").split() and "warn" not in (iso_tile["className"] or "").split(),
        f"must be neutral non-warning style: {iso_tile}",
    )
    check("校准范围外（推算）" in iso_tile["label"] and "未校准" not in iso_tile["label"], f"expected neutral label, got {iso_tile['label']!r}")
    check(
        "ISO 1250" in iso_tile["sheetText"] and "校准范围外（推算）" in iso_tile["sheetText"], f"reason one tap away: {iso_tile['sheetText']!r}"
    )

    # Case 2: no calibration at all -> warning tile 「未校准」
    monkeypatch.setenv("GXR_MOBILE_DEFAULT_CALIB", "off")
    sub_off = tmp_path / "off"
    sub_off.mkdir()
    off_data = _run_browser(
        sub_off,
        {
            "mode": "uncalibrated",
            "g266": [str(dat266), str(fx_kubik)],
            "isoOutPair": [str(iso_dng), str(iso_jpg)],
            "isoOutStem": iso_dng.stem,
        },
    )
    nocal_tile = off_data["steps"]["uncalibratedSplit"]
    check(nocal_tile["state"] == "no-calibration", f"expected no-calibration stateAttr, got {nocal_tile}")
    check("warn" in (nocal_tile["className"] or "").split(), f"no-calibration must keep warn style: {nocal_tile}")
    check(nocal_tile["label"] == "未校准", f"no-calibration must keep label 未校准: {nocal_tile['label']!r}")
    check(nocal_tile["sheetState"] == "no-calibration", f"no-calibration sheet state: {nocal_tile}")
