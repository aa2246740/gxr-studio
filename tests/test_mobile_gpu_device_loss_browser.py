"""手机端显卡设备丢失回归测试（browser_serial，生产 mobile/dist，Metal Chromium）。

照 Sol 的做法：拿到引擎请求的 GPUDevice 调 destroy()，真的触发 GPUDevice.lost，再看页面落到哪个状态。
对照组是同一份构建、去掉 navigator.gpu 的页面（没有 WebGPU）：控件和提示逐个和它比，不是手写清单。

1. 编辑中丢了：2 秒内落回相机 JPG（data-gpu=0、没有 data-frame），没有显卡的提示出现，控件和对照页一样；
   拖曝光滑块不留下「改过」。停用的控件看得出是停用的。
2. 两张照片之间丢了：回图库开另一张，显示它自己的 JPG，不是黑的（舞台中心 24×24 取色）。
3. 第一次点开之前丢了：第一次打开就是 JPG。
4. 只有 RAW 的照片：丢了以后显示「预览不了」，不是黑画布。
5. 以上都没有页面错误、未处理的 Promise 拒绝和轻提示。
6. 恢复：丢了以后下一次打开照片再建一次引擎，建好了回到显卡；Engine.create() 连着失败 3 次就不再试。

1–5 在丢了以后让 Engine.create() 一直失败，测的是落回 JPG 这条路本身（不靠恢复）。
固定版本的 WebKit 没有 WebGPU；Chromium 拿不到适配器时整组跳过。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from conftest import REAL_HOME
from test_mobile_perf_startup_browser import _lazy_photos
from test_mobile_ui_browser import MODULES, PKG, RUNTIME, _need

pytestmark = [pytest.mark.browser_serial, pytest.mark.browser_alone]

REPO = Path(__file__).resolve().parents[1]
DIST = REPO / "mobile" / "dist"
DRIVER = REPO / "tests" / "support" / "mobile_gpu_device_loss_browser.mjs"
# _lazy_photos 画的两张相机 JPG 的颜色（jpeg、jpeg2）。
JPEG_RGB = (120, 140, 160)
JPEG2_RGB = (160, 120, 90)
FALLBACK_MS = 2000


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need((DIST / "index.html").is_file(), "mobile/dist is missing; run make mobile-build")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")
    tmp = tmp_path_factory.mktemp("gpu-device-loss")
    out = tmp / "report.json"
    manifest = tmp / "manifest.json"
    payload = {"distDir": str(DIST), "photos": _lazy_photos(tmp / "photos"), "out": str(out)}
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(DRIVER), str(RUNTIME), str(manifest)],
        cwd=REPO,
        env={**os.environ, "HOME": str(REAL_HOME)},
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, f"driver failed ({proc.returncode}): {proc.stderr[-1500:]}"
    data: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
    if data.get("skipped"):
        pytest.skip(data["skipped"])
    for name, row in data["scenarios"].items():
        assert "error" not in row, f"{name}: {row['error']}"
    return data


def _scenario(report: dict[str, Any], name: str) -> dict[str, Any]:
    row: dict[str, Any] = report["scenarios"][name]
    return row


def _near(rgb: list[int], want: tuple[int, int, int], tol: int = 14) -> bool:
    return all(abs(int(a) - b) <= tol for a, b in zip(rgb, want, strict=True))


def _shows_jpg(row: dict[str, Any], photo: str, rgb: list[int], want: tuple[int, int, int]) -> None:
    assert row["screen"] == "editor" and row["photo"] == photo, row
    assert row["gpu"] == "0", f"data-gpu should be 0 after the device is lost: {row}"
    assert row["frame"] is None, f"stale data-frame after the device is lost: {row}"
    assert row["stillVisible"] is True and row["gpuVisible"] is False, f"camera JPG is not the visible stage: {row}"
    assert _near(rgb, want), f"stage centre {rgb} is not the camera JPG {want} (black canvas?)"


def test_lost_while_editing_falls_back_to_the_jpg(report: dict[str, Any]) -> None:
    lost = _scenario(report, "lostWhileEditing")
    assert lost["before"]["gpu"] == "1" and lost["before"]["frame"] == "1", lost["before"]
    ms = lost["fallbackMs"]
    assert ms is not None and ms <= FALLBACK_MS, f"no fallback to the camera JPG within {FALLBACK_MS} ms: {lost['after']}"
    _shows_jpg(lost["after"], lost["before"]["photo"], lost["afterRgb"], JPEG_RGB)


def test_lost_while_editing_shows_the_no_gpu_notice(report: dict[str, Any]) -> None:
    ref, lost = _scenario(report, "reference"), _scenario(report, "lostWhileEditing")
    assert ref["adjust"]["notices"], "reference page shows no no-GPU notice in the adjust tool"
    assert lost["adjust"]["notices"] == ref["adjust"]["notices"], (lost["adjust"]["notices"], ref["adjust"]["notices"])
    ref_l, lost_l = _scenario(report, "referenceLandscape"), _scenario(report, "lostLandscape")
    assert lost_l["fallbackMs"] is not None and lost_l["fallbackMs"] <= FALLBACK_MS, lost_l["after"]
    assert "nogpu-notice" in ref_l["opened"]["notices"], ref_l["opened"]
    assert lost_l["after"]["notices"] == ref_l["opened"]["notices"], (lost_l["after"]["notices"], ref_l["opened"]["notices"])


def _diff(want: dict[str, Any], got: dict[str, Any]) -> dict[str, Any]:
    return {key: {"no-webgpu": want.get(key), "lost": got.get(key)} for key in sorted(set(want) | set(got)) if want.get(key) != got.get(key)}


def test_lost_while_editing_controls_match_a_page_without_webgpu(report: dict[str, Any]) -> None:
    ref, lost = _scenario(report, "reference"), _scenario(report, "lostWhileEditing")
    assert ref["controls"], "no controls captured on the reference page"
    assert _diff(ref["controls"], lost["controls"]) == {}
    assert _diff(ref["controlsAfterDrag"], lost["controlsAfterDrag"]) == {}
    ref_l, lost_l = _scenario(report, "referenceLandscape"), _scenario(report, "lostLandscape")
    assert _diff(ref_l["controls"], lost_l["controls"]) == {}


def test_lost_while_editing_exposure_drag_leaves_no_edit(report: dict[str, Any]) -> None:
    ref, lost = _scenario(report, "reference"), _scenario(report, "lostWhileEditing")
    assert lost["afterDrag"] == ref["afterDrag"], (lost["afterDrag"], ref["afterDrag"])
    assert lost["afterDrag"]["edited"] == [] and lost["afterDrag"]["value"] == "0.0", lost["afterDrag"]


def test_disabled_controls_look_disabled_without_webgpu(report: dict[str, Any]) -> None:
    """没有 WebGPU（以及设备丢了，同一个状态）时，看得见的停用控件都要淡下去，旁边有一行原因。"""
    ref = _scenario(report, "reference")
    disabled = {key: row for key, row in ref["controls"].items() if row["disabled"] and row["visible"]}
    assert disabled, "no visible disabled controls in the adjust tool without WebGPU"
    looks_enabled = {key: row["opacity"] for key, row in disabled.items() if float(row["opacity"]) > 0.6}
    assert looks_enabled == {}, f"disabled controls that still look enabled: {looks_enabled}"
    assert "adj-disabled-note" in ref["adjust"]["notices"], ref["adjust"]


def test_lost_between_photos_opens_the_next_jpg(report: dict[str, Any]) -> None:
    between = _scenario(report, "lostBetweenPhotos")
    assert between["grid"]["gpu"] == "0" and between["grid"]["frame"] is None, between["grid"]
    _shows_jpg(between["next"], "r9990003", between["nextRgb"], JPEG2_RGB)
    lost = _scenario(report, "lostWhileEditing")
    _shows_jpg(lost["next"], "r9990003", lost["nextRgb"], JPEG2_RGB)


def test_lost_before_first_tap_opens_the_jpg(report: dict[str, Any]) -> None:
    early = _scenario(report, "lostBeforeFirstTap")
    _shows_jpg(early["first"], "r9990002", early["firstRgb"], JPEG_RGB)


def test_raw_only_photo_after_loss_says_it_cannot_preview(report: dict[str, Any]) -> None:
    ref, lost = _scenario(report, "reference"), _scenario(report, "lostWhileEditing")
    raw = lost["raw"]
    assert raw["screen"] == "editor" and raw["photo"] == "r9990001", raw
    assert raw["gpu"] == "0" and raw["gpuVisible"] is False, f"RAW-only photo opened on the lost GPU canvas: {raw}"
    assert raw["noPreview"], f"no explicit can't-preview state for a RAW-only photo: {raw}"
    assert raw["noPreview"] == ref["raw"]["noPreview"], (raw["noPreview"], ref["raw"]["noPreview"])
    assert raw["label"] == ref["raw"]["label"] == [raw["noPreview"], "导出仍会套用配方"], (raw["label"], ref["raw"]["label"])


def test_device_loss_raises_no_errors(report: dict[str, Any]) -> None:
    for name, row in report["scenarios"].items():
        assert row["pageErrors"] == [], f"{name} page errors: {row['pageErrors']}"
        assert row["rejections"] == [], f"{name} unhandled rejections: {row['rejections']}"
        assert row["toasts"] == [], f"{name} toasts: {row['toasts']}"


def test_recovery_rebuilds_the_engine_on_next_open(report: dict[str, Any]) -> None:
    rec = _scenario(report, "recovery")
    assert rec["fallbackMs"] is not None and rec["fallbackMs"] <= FALLBACK_MS, rec
    nxt = rec["next"]
    assert nxt["adapterSinceLoss"] == 1, f"next open did not try Engine.create() exactly once: {nxt}"
    assert nxt["gpu"] == "1" and nxt["frame"] == "1" and nxt["gpuVisible"] is True, f"recovered engine not used: {nxt}"
    assert _near(rec["nextRgb"], JPEG2_RGB), f"recovered GPU frame {rec['nextRgb']} is not the photo"
    again = rec["again"]
    assert again["adapterSinceLoss"] == 1 and again["gpu"] == "1" and again["gpuVisible"] is True, again
    assert _near(rec["againRgb"], JPEG_RGB), rec["againRgb"]


def test_recovery_stops_after_three_failures(report: dict[str, Any]) -> None:
    lim = _scenario(report, "retryLimit")
    tries = [row["adapterSinceLoss"] for row in lim["opens"]]
    assert tries == [1, 2, 3, 3, 3], f"Engine.create() attempts after each open: {tries}"
    for row in lim["opens"]:
        assert row["gpu"] == "0" and row["stillVisible"] is True and row["gpuVisible"] is False, row
    assert _near(lim["lastRgb"], JPEG2_RGB), lim["lastRgb"]
