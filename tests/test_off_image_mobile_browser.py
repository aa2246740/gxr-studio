"""手机端「导出关机图」真浏览器验收测试与桌面/手机同口径校验（`browser_serial`）。"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from gxr_studio import off_image
from test_mobile_ui_browser import MODULES, PKG, RUNTIME, _drive, _need

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "support" / "mobile_off_image_browser.mjs"
PARENT_ENV = "GXR_STUDIO_TEST_PARENT"

NOTE_CAMERA = "相机会把当天张数叠在关机画面上，下部别放重要内容。"
NOTE_CARD = "把 GXROFF.JPG 拷到存储卡最外层（和 GXRCE.DAT 放在一起）。"
NOTE_FILES = "存的时候选「存储到“文件”」，不要存进相册（相册会改动文件，相机就不认了）。"


def _make_off_image_script() -> Path:
    parent = os.environ.get(PARENT_ENV)
    base = Path(parent) if parent else REPO.parent
    return base / "scripts" / "make_off_image.py"


def _run_ref_check(path: Path) -> str:
    script = _make_off_image_script()
    assert script.is_file(), f"missing reference checker: {script}"
    proc = subprocess.run(
        [sys.executable, "-I", str(script), "--check", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"--check failed: {proc.stdout} {proc.stderr}"
    return proc.stdout.strip()


def _synthetic_photo_jpg(dest: Path) -> None:
    y, x = np.mgrid[0:600, 0:900]
    arr = np.zeros((600, 900, 3), dtype=np.uint8)
    arr[..., 0] = (30 + x * 200 // 900).astype(np.uint8)
    arr[..., 1] = (45 + y * 175 // 600).astype(np.uint8)
    arr[..., 2] = (195 - (x + y) * 110 // 1500).astype(np.uint8)
    Image.fromarray(arr, mode="RGB").save(dest, "JPEG", quality=94)


def _synthetic_alpha_png(dest: Path) -> None:
    arr = np.zeros((480, 360, 4), dtype=np.uint8)
    arr[40:440, 30:330, 0] = 210
    arr[40:440, 30:330, 1] = 135
    arr[40:440, 30:330, 2] = 65
    arr[40:440, 30:330, 3] = 255
    # 边角保持全透明，用于验证两端都铺在纯黑底上
    Image.fromarray(arr, mode="RGBA").save(dest, "PNG")


def _synthetic_rot6_jpg(dest: Path) -> None:
    # 900x600 原始像素，标 Orientation=6（顺时针转 90° 后为 600x900 竖图）
    arr = np.zeros((600, 900, 3), dtype=np.uint8)
    arr[:, :300] = (210, 40, 40)
    arr[:, 300:600] = (40, 180, 60)
    arr[:, 600:] = (45, 70, 210)
    im = Image.fromarray(arr, mode="RGB")
    exif = im.getexif()
    exif[274] = 6
    im.save(dest, "JPEG", quality=95, exif=exif)


def test_mobile_off_image_browser_end_to_end(tmp_path: Path) -> None:
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")

    photo_jpg = tmp_path / "R0001001.JPG"
    alt_png = tmp_path / "alt_alpha.png"
    alt_rot6 = tmp_path / "alt_rot6.jpg"
    _synthetic_photo_jpg(photo_jpg)
    _synthetic_alpha_png(alt_png)
    _synthetic_rot6_jpg(alt_rot6)

    out_path = tmp_path / "mobile-off-image-out.json"
    manifest_path = tmp_path / "mobile-off-image-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "photoJpg": str(photo_jpg),
                "altPng": str(alt_png),
                "altRot6": str(alt_rot6),
                "out": str(out_path),
            }
        ),
        encoding="utf-8",
    )

    result = _drive(DRIVER, manifest_path, tmp_path, "", timeout=300)
    assert result.returncode == 0, f"mobile off-image driver failed: {result.stderr[-1500:]}"
    assert out_path.is_file()
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["error"] is None
    assert report["pageErrors"] == []

    steps = report["steps"]
    assert "做成关机图" in steps["editorMoreLabels"]

    init = steps["initial"]
    assert init["title"] == "导出关机图"
    assert init["naturalWidth"] == 640 and init["naturalHeight"] == 480
    assert init["srcIsBlob"] is True
    assert "640×480" in init["meta"] and "品质" in init["meta"]
    assert NOTE_CAMERA in init["notes"]
    assert NOTE_CARD in init["notes"]
    assert NOTE_FILES in init["notes"]
    assert init["exportDisabled"] is False
    assert init["datState"] == "7"
    assert steps["pdockPresentAfterShare"] is False

    # 验证三次共享结果都满足：手势激活、单文件、文件名 GXROFF.JPG、外部 --check 返回 J7
    shared = report["shared"]
    decoded_files: dict[str, bytes] = {}
    for key in ("photo", "altPng", "altRot6"):
        call = shared[key]
        assert call["active"] is True
        assert len(call["files"]) == 1
        entry = call["files"][0]
        assert entry["name"] == "GXROFF.JPG"
        assert entry["type"] == "image/jpeg"
        raw = base64.b64decode(entry["b64"])
        assert len(raw) == entry["size"] <= 32768
        p = tmp_path / f"shared-{key}.jpg"
        p.write_bytes(raw)
        assert ": J7 ok" in _run_ref_check(p)
        assert off_image.check_off_image(raw).ok is True
        decoded_files[key] = raw

    # 同图端到端对拍：无损 PNG（含黑底合成）在手机 WASM 与桌面 Python 逐字节一致
    desk_png = off_image.encode_off_image_bytes(alt_png.read_bytes()).data
    assert decoded_files["altPng"] == desk_png

    # 旋转 JPEG：允许不同浏览器 JPEG 解码器极小舍入差（若有），逐像素差必须 <= 1 且同品质
    desk_rot6 = off_image.encode_off_image_bytes(alt_rot6.read_bytes()).data
    if decoded_files["altRot6"] != desk_rot6:
        a = np.asarray(Image.open(tmp_path / "shared-altRot6.jpg").convert("RGB"), dtype=np.int16)
        p_desk = tmp_path / "desk-rot6.jpg"
        p_desk.write_bytes(desk_rot6)
        b = np.asarray(Image.open(p_desk).convert("RGB"), dtype=np.int16)
        assert int(np.abs(a - b).max()) <= 1

    fail = steps["forcedFail"]
    assert fail["ready"] == "0"
    assert fail["exportDisabled"] is True
    assert fail["previewHidden"] is True
    assert fail["error"]

    cleanup = steps["cleanup"]
    assert cleanup["workersSpawned"] >= 4
    assert cleanup["workersAlive"] == 0
    assert cleanup["liveUrls"] == 0
