"""UI-M4 边框、更多选项、时间戳、自定义标志与帮助页（OFL 许可）的 Playwright 浏览器测试。"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from support import samples
from test_mobile_ui_browser import MODULES, PKG, RUNTIME, _drive, _need, _pairs

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "support" / "mobile_ui_frame_browser.mjs"
LIFECYCLE_DRIVER = REPO / "tests" / "support" / "mobile_ui_frame_lifecycle_browser.mjs"


def _make_logo_png(dest: Path) -> Path:
    img = Image.new("RGBA", (128, 128), (255, 255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([28, 28, 99, 99], fill=(22, 24, 28, 255))
    draw.ellipse([48, 48, 79, 79], fill=(214, 40, 31, 255))
    img.save(dest, format="PNG")
    return dest


def test_mobile_ui_frame_browser(tmp_path: Path) -> None:
    files = _pairs()
    samples.need_sample(len(files) >= 2, f"need a DNG+JPG pair in {REPO}")
    _need(len(files) >= 2, "need a DNG+JPG pair")
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")

    logo_png = _make_logo_png(tmp_path / "studio_logo.png")
    too_big = tmp_path / "huge.png"
    too_big.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024 + 64))

    shot_dir = os.environ.get("GXR_M4_SHOT_DIR", "").strip()
    if shot_dir:
        Path(shot_dir).mkdir(parents=True, exist_ok=True)

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "pairs": [str(p) for p in files[:2]],
                "logoPng": str(logo_png),
                "tooBigLogo": str(too_big),
                "shotDir": shot_dir or None,
            }
        ),
        encoding="utf-8",
    )

    result = _drive(DRIVER, manifest, tmp_path, "", timeout=240)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])

    assert report["pageErrors"] == [], report["pageErrors"]
    assert len(report["licences"]) == 6, report["licences"]
    assert all("ricoh" not in item["href"].lower() for item in report["licences"])

    assert report["cardsCount"] == 13
    assert report["cardsNonBlank"] == 13
    assert report["galleryControls"] == {
        "colors": ["white", "black", "cream"],
        "weights": ["thin", "medium", "thick"],
    }
    assert report["slideColors"] == ["cream", "black"]
    assert report["leicaChips"] == ["标志", "参数", "日期", "更多"]

    assert report["decorActive"] is True
    assert report["decoPixels"] > 500
    assert report["blurPixels"] > 500
    assert report["noneCleared"] is True
    assert report["stampPixelsOnly"] > 50

    assert report["cjkFrameErr"] == "无法显示中文。请改用英文或数字。"
    assert report["cjkStampErr"] == "无法显示中文。请改用英文或数字。"
    assert report["instantHasPlacement"] is True

    assert report["logoTooBigErr"] is True
    assert "1 / 12" in (report["logoCountAfterSave"] or "")
    assert report["logoRenamed"] is True
    assert report["logoDeletedFallback"] is True


def test_mobile_ui_frame_lifecycle_browser(tmp_path: Path) -> None:
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")

    logo_png = _make_logo_png(tmp_path / "studio_logo.png")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"logoPng": str(logo_png)}), encoding="utf-8")

    result = _drive(LIFECYCLE_DRIVER, manifest, tmp_path, "", timeout=240)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["pageErrors"] == [], report["pageErrors"]
    assert report["results"] == {
        "lateAsyncOk": True,
        "cancelAndIdbOk": True,
        "resourcesFreedOk": True,
        "boundedRetryOk": True,
        "hostileWorkerOk": True,
        "s1LogoCancelAndRaceOk": True,
        "s1TextRefusalOk": True,
        "s1CardsAndBackdropOk": True,
        "s1FrameNoOk": True,
        "s2DraftOrderAndSheetsOk": True,
        "s3CommitBoundaryMatrixOk": True,
        "s3SameFamilyPersistenceOk": True,
        "idbV2ToV3UpgradeOk": True,
    }, report["results"]
