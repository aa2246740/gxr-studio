"""手机端拍摄配方采用（验收 #2）与「全部重置」清曲线（验收 #3）的真实浏览器测试（`browser_serial`）。

只通过 `support.samples` 访问本地实拍文件，不打印文件名、拍摄时间、EXIF 或原始标签。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from gxr_studio.camdata import calib_export, datcodec, store
from support import samples
from support.export_pipeline_cases import check, same_file_redacted
from support.mobile_ui_calib import pack_files
from test_g250 import _need_g185_or_g250
from test_mobile_ui_browser import DATS, MODULES, PKG, RUNTIME, _drive, _need

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "support" / "mobile_camera_recipe_browser.mjs"


def _prereq() -> None:
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")


def _card250_pair(index_1based: int) -> tuple[Path, Path]:
    folder = samples.captures() / "r6-field-20261118" / "card250"
    if not folder.is_dir():
        samples.missing("captures/r6-field-20261118/card250 不在本机")
    stems = sorted({p.stem for p in folder.iterdir() if p.suffix.upper() in (".DNG", ".JPG")})
    if len(stems) < index_1based:
        samples.missing(f"card250 fewer than {index_1based} stems")
    stem = stems[index_1based - 1]
    dng = next((p for p in folder.iterdir() if p.stem == stem and p.suffix.upper() == ".DNG"), None)
    jpg = next((p for p in folder.iterdir() if p.stem == stem and p.suffix.upper() == ".JPG"), None)
    if dng is None or jpg is None:
        samples.missing(f"card250 index #{index_1based} is not a DNG+JPG pair")
    return dng, jpg


def _wrong_gen_dat() -> Path:
    for path in DATS:
        if not path.is_file():
            continue
        gen = datcodec.peek_generation(path.read_bytes())
        if datcodec.PROFILES[gen].library_generation != 178:
            return path
    samples.missing("no non-178 local GXRCE.DAT fixture (GXR_STUDIO_TEST_PARENT)")
    raise AssertionError("unreachable")


def test_mobile_camera_recipe_adoption_and_reset_browser(tmp_path: Path) -> None:
    """Acceptance #2 (all 6 bullets) + Acceptance #3 (reset clears curve + knobs) in headless Chromium."""
    _prereq()
    _raw250, _lib250, dat250, fx250 = _need_g185_or_g250(250)
    wrong_dat = _wrong_gen_dat()
    dng1, jpg1 = _card250_pair(1)
    dng2, jpg2 = _card250_pair(2)

    jpg_only = tmp_path / "R9990099.JPG"
    shutil.copyfile(jpg1, jpg_only)

    # Synthetic calibration pack covering all 3 units (stock04 from g250 DAT) so recipe strip + stage rendering run calibrated.
    _parsed, entries = store.entries_from_dat(_raw250, library=_lib250)
    pack = pack_files({name: entries[(unit, 4)] for unit, name in enumerate(("jlaunch", "qlaunch", "l08firm"))})
    pack_zip = tmp_path / "GXR 校准数据 2026-11-18.zip"
    pack_zip.write_bytes(calib_export.zip_bytes(pack, (2026, 11, 18, 9, 0, 0)))

    out_dir = tmp_path / "exported"
    out_dir.mkdir()
    report_path = tmp_path / "camera-recipe-report.json"
    manifest: dict[str, Any] = {
        "pack": str(pack_zip),
        "binding": "gxr-test-binding",
        "file1": dng1.stem,
        "diptych": dng2.stem,
        "jpgOnly": jpg_only.stem,
        "files": [str(dng1), str(jpg1), str(dng2), str(jpg2), str(jpg_only), str(dat250), str(fx250)],
        "wrongGenFiles": [str(dng1), str(jpg1), str(wrong_dat)],
        "missingDatFiles": [str(dng1), str(jpg1)],
        "rawOnlyDng": str(dng1),
        "datFiles": [str(dat250), str(fx250)],
        "outDir": str(out_dir),
        "out": str(report_path),
    }
    manifest_path = tmp_path / "camera-recipe-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = _drive(DRIVER, manifest_path, tmp_path, "", timeout=600)
    if result.returncode != 0 or not report_path.is_file():
        pytest.fail("camera-recipe browser driver failed", pytrace=False)
    report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    check(report.get("error") is None, f"driver raised: {report.get('error')}")
    check(report["pageErrors"] == [], f"pageErrors: {report['pageErrors']}")

    # Bullet 1 & 6: card250 #1 adopts Teal Orange, stage renders it, choice stays null, no grid badge, copy sees recipe.
    adopted = report["steps"]["adopted"]
    check(adopted["badgeBefore"] == 0, f"untouched adopted photo must show no badge before open, got {adopted['badgeBefore']}")
    check(adopted["badgeAfterOpen"] == 0, f"untouched adopted photo must show no badge after open, got {adopted['badgeAfterOpen']}")
    check(adopted["heading"] == "Teal Orange", f"配方 tab heading expected 'Teal Orange', got {adopted['heading']!r}")
    check(adopted["stripOnName"] == "Teal Orange", f"strip active card expected 'Teal Orange', got {adopted['stripOnName']!r}")
    check(adopted["choice"] is None, f"adopted recipe must not be stored as photo.choice, got {adopted['choice']!r}")
    check(adopted["sideHasRecipe"] is True, "export request sidecar must include adopted recipe")
    check(adopted["recipeCopyRowHasNotSet"] is False, "copy sheet on adopted photo must treat recipe as present")

    # Bullet 2: auto export equals exporting with Teal Orange chosen explicitly (byte-for-byte).
    exp = report["steps"]["exportParity"]
    check(exp["explicitChoice"] is not None, "explicit Teal Orange step must set photo.choice")
    check(exp["autoCount"] == 1 and exp["explicitCount"] == 1, "both auto and explicit exports produced 1 file")
    check(exp["autoStageEqualsExplicit"] is True, "stage recipe key of adopted Teal Orange equals explicit Teal Orange")
    auto_paths = sorted(out_dir.glob("auto-*"))
    explicit_paths = sorted(out_dir.glob("explicit-*"))
    check(len(auto_paths) == 1 and len(explicit_paths) == 1, "both auto and explicit exports wrote 1 file")
    same_file_redacted(auto_paths[0].read_bytes(), explicit_paths[0].read_bytes(), "auto export equals explicit Teal Orange export")

    # Bullet 3: choosing 原厂标准 persists and wins.
    wins = report["steps"]["explicitWins"]
    check(wins["stageDiffersFromAdopted"] is True, "choosing 原厂标准 updates stage look")
    check(wins["badgeAfterExplicit"] == 1, "explicit choice shows grid pencil badge")
    check(wins["persistedHeading"] == "原厂标准", f"explicit choice expected '原厂标准', got {wins['persistedHeading']!r}")
    check(
        wins["persistedChoice"] is not None and wins["persistedChoice"].get("id") == "builtin:4",
        f"explicit choice must persist as builtin:4, got {wins['persistedChoice']!r}",
    )

    # Bullet 4 + P3-1 + P3-2: wrong-generation DAT shows full desktop message, falls back to default, and export note warns.
    wrong = report["steps"]["wrongGen"]
    check(wrong["hasMissingCatalogMessage"] is True, "wrong-gen DAT must show '请先导入拍摄时使用的相机配方文件。'")
    check(
        wrong["hasFullEditorMessage"] is True,
        "wrong-gen DAT editor error box must include desktop suffix '照片调整仍可保存，暂不生成拍摄配方预测。'",
    )
    check(wrong["errorCode"] == "camera_recipe_catalog_missing", f"expected camera_recipe_catalog_missing, got {wrong['errorCode']!r}")
    check(wrong["heading"] == "原厂标准", f"wrong-gen DAT must fall back to default '原厂标准', got {wrong['heading']!r}")
    check(wrong["choice"] is None, "wrong-gen fallback must not store photo.choice")
    check(wrong["exportDone"] == 1 and wrong["exportFailed"] == 0, "wrong-gen DAT export must still succeed as camera JPG")
    check(wrong["exportHasNote"] is True, "wrong-gen DAT export must show per-photo note '请先导入拍摄时使用的相机配方文件。'")

    # P3-1: missing camera data on export still exports camera JPG and shows per-photo note.
    missing = report["steps"]["missingDat"]
    check(missing["exportDone"] == 1 and missing["exportFailed"] == 0, "missing-DAT export must succeed as camera JPG")
    check(missing["exportHasNote"] is True, "missing-DAT export must show per-photo note '请先导入拍摄时使用的相机配方文件。'")

    # P1-1: RAW-only photo opened before importing DAT must keep tag and adopt after DAT import.
    raw_only = report["steps"]["rawOnlyBeforeDat"]
    check(raw_only["heading"] == "Teal Orange", f"RAW-only open-before-DAT expected 'Teal Orange', got {raw_only['heading']!r}")
    check(raw_only["sideRecipe"] == "record:6", f"RAW-only open-before-DAT auto export expected 'record:6', got {raw_only['sideRecipe']!r}")

    # Bullet 5: diptych (file 2) and JPG-only do not adopt.
    no_adopt = report["steps"]["noAdopt"]
    check(no_adopt["diptychHasMessage"] is True, "card250 #2 detected as diptych")
    check(no_adopt["diptychChoice"] is None and no_adopt["diptychSideHasRecipe"] is False, "diptych must not adopt recipe")
    check(no_adopt["jpgHasMessage"] is True, "JPG-only photo recognized as JPG-only")
    check(no_adopt["jpgChoice"] is None and no_adopt["jpgSideHasRecipe"] is False, "JPG-only must not adopt recipe")

    # Acceptance #3: Reset clears curve + exposure, bumps rev by 1, clears badge; second press is no-op.
    rst = report["steps"]["reset"]
    check(rst["beforeExposure"] != 0 and rst["beforeHasCurve"] is True, "exposure and curve were set before reset")
    check(rst["badgeBeforeReset"] == 1, "grid badge shown when exposure + curve set")
    check(rst["afterFirstExposure"] == 0, f"reset must clear exposure, got {rst['afterFirstExposure']!r}")
    check(rst["afterFirstCurve"] is None, f"reset must clear curve, got {rst['afterFirstCurve']!r}")
    check(rst["afterFirstRev"] == rst["beforeRev"] + 1, f"reset must bump rev by 1: {rst['beforeRev']} -> {rst['afterFirstRev']}")
    check(rst["afterSecondRev"] == rst["afterFirstRev"], "second reset with nothing set must not bump rev")
    check(rst["badgeAfterReset"] == 0, "grid badge cleared after reset")
