"""电脑上的编辑器舞台（2026-11-26，`browser_serial`）：鼠标和触控板。

- Chromium（Metal，1440×900，没有触摸点）：Ctrl + 滚轮以指针为中心缩放并拦下（浏览器不缩放整页）；1× 时普通滚轮不拦、
  不缩放；放大后滚轮和鼠标拖动平移；双击在适合和 100% 之间切换，不收起面板；+ / = / - / 0 缩放，焦点在滑块上时不动；
  单击照片不收起面板，F 收起和展开，`leaveStageFullView()` 退出；按住 \\ 看原图；舞台有提示。
- 曲线：鼠标双击删中间点，拖出框外松手也删，端点删不掉，模型和预览都跟着回去。
- HDR 开关：读屏看到开关状态，停用时说明原因。
- WebKit 无触摸：Safari 的触控板捏合（gesture 事件）以指针为中心缩放。WebKit 手机和 iPad（有触摸）：单击照片仍收起面板，
  没有新提示，\\ 和 F 不起作用。

只用本地实拍样本；报告里没有文件名、拍摄时间或 EXIF。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from support.export_pipeline_cases import check
from test_mobile_ui_browser import MODULES, PKG, RUNTIME, SAMPLES, _drive, _need, _pairs

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "support" / "mobile_desktop_stage_browser.mjs"
STAGE_TITLE = "按住照片或按住 \\ 键看原图 · 双击放大到 100%"
HDR_NO_RAW = "HDR 需要 RAW 文件"


def _run(tmp: Path, part: str) -> dict[str, Any]:
    files = _pairs()
    _need(len(files) >= 6, f"need three DNG+JPG pairs in {SAMPLES}")
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")
    dng, jpg = files[0], files[1]
    jpg_only = files[5] if files[5].suffix.upper() == ".JPG" else files[4]
    with Image.open(jpg) as im:
        size = list(im.size)
    shot_dir = os.environ.get("GXR_STAGE_SHOTS", "")
    report_path = tmp / "report.json"
    manifest = {
        "part": part,
        "files": [str(dng), str(jpg), str(files[2]), str(files[3]), str(jpg_only)],
        "count": 3,
        "rawId": dng.stem.lower(),
        "jpgOnlyId": jpg_only.stem.lower(),
        "imageSize": size,
        "shotDir": shot_dir,
        "reportPath": str(report_path),
    }
    manifest_path = tmp / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = _drive(DRIVER, manifest_path, tmp, "", timeout=1200)
    check(report_path.is_file(), "driver wrote its report")
    report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    check(result.returncode == 0 and report.get("error") is None, f"driver completed without error: {report.get('error')}")
    check(report["pageErrors"] == [], f"no page errors: {report['pageErrors']}")
    steps: dict[str, Any] = report["steps"]
    return steps


@pytest.fixture(scope="module")
def chromium_steps(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _run(tmp_path_factory.mktemp("stage-chromium"), "chromium")


@pytest.fixture(scope="module")
def webkit_steps(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    steps = _run(tmp_path_factory.mktemp("stage-webkit"), "webkit")
    if steps.get("webkitMissing"):
        pytest.skip("Playwright WebKit is not installed")
    return steps


def test_desktop_stage_wheel_zoom_and_pan(chromium_steps: dict[str, Any]) -> None:
    z = chromium_steps["zoom"]
    check(z["env"]["touch"] == 0 and z["env"]["fine"] is True, f"computer context: {z['env']}")
    check(z["plainAtFit"]["seen"] is True, "plain wheel reached the page")
    check(z["plainAtFit"]["zoom"] == 1, f"plain wheel at fit does not zoom: {z['plainAtFit']}")
    check(z["plainAtFit"]["prevented"] is False, "plain wheel at fit is not cancelled")
    cw = z["ctrlWheel"]
    check(cw["ctrl"] is True, "Ctrl was held during the wheel")
    check(cw["zoom"] > 1.3, f"Ctrl+wheel zooms in: {cw}")
    check(cw["prevented"] is True, "Ctrl+wheel is cancelled so the page does not zoom")
    check(cw["drift"] <= 2, f"image point under the cursor stays within 2 px: {cw}")
    out = z["ctrlWheelOut"]
    check(1 < out["zoom"] < cw["zoom"], f"Ctrl+wheel the other way zooms out: {out}")
    check(out["drift"] <= 2, f"zoom out keeps the point under the cursor: {out}")
    pan = z["wheelPan"]
    check(pan["zoom"] == out["zoom"], f"plain wheel while zoomed does not change the zoom: {pan}")
    check(pan["deltaY"] is not None and pan["deltaY"] > 10, f"the page saw a pixel-mode wheel: {pan}")
    check(abs(pan["dy"] + pan["deltaY"]) <= 2 and abs(pan["dx"]) <= 1, f"plain wheel while zoomed pans by the delta: {pan}")
    check(pan["prevented"] is True, "plain wheel while zoomed is cancelled")
    mp = z["mousePan"]
    check(mp["samePhoto"] is True, "mouse drag while zoomed stays on the photo")
    check(mp["dx"] < -60 and mp["dy"] < -20, f"mouse drag while zoomed pans: {mp}")
    check(z["zeroKey"]["zoom"] == 1, "0 returns to fit")


def test_desktop_stage_double_click_and_keys(chromium_steps: dict[str, Any]) -> None:
    z = chromium_steps["zoom"]
    d = z["dblIn"]
    want = d["wantZoom"]
    check(abs(d["zoom"] - want) <= max(0.02, want * 0.01), f"double-click zooms to 100%: {d}")
    if want < 5:
        check(abs(d["devicePxPerImagePx"] - 1) <= 0.02, f"one image pixel per device pixel: {d}")
    check(d["drift"] <= 2, f"double-click keeps the clicked point: {d}")
    check(d["collapsed"] == "0", "double-click does not hide the panels")
    back = z["dblOut"]
    check(back["zoom"] == 1, f"second double-click returns to fit: {back}")
    check(back["collapsed"] == "0" and back["collapses"] == [], f"double-clicks never touched data-collapsed: {back}")
    k = z["keys"]
    check(k[0] > 1.4 and k[1] > k[0] and 1 < k[2] < k[1] and k[3] == 1, f"+ = - 0 zoom in, in, out, fit: {k}")
    s = z["sliderKeys"]
    check(s["focused"] == "exposure_ev", f"focus was on the exposure slider: {s}")
    check(s["zooms"] == [1, 1, 1], f"zoom keys do nothing while a slider has focus: {s}")


def test_desktop_stage_click_and_full_view(chromium_steps: dict[str, Any]) -> None:
    c = chromium_steps["click"]
    check(c["click"]["collapsed"] == "0" and c["click"]["collapses"] == [], f"single click keeps the panels: {c['click']}")
    check(c["fKey"] == ["1", "0", "1"], f"F toggles full view: {c['fKey']}")
    check(c["leave"] == {"first": True, "collapsed": "0", "second": False}, f"leaveStageFullView leaves full view once: {c['leave']}")


def test_desktop_stage_compare_key(chromium_steps: dict[str, Any]) -> None:
    c = chromium_steps["compare"]
    check(c["title"] == STAGE_TITLE, f"stage title on a computer: {c['title']!r}")
    check(c["held"] == {"holding": True, "sub": "原图"}, f"holding \\ shows the original: {c['held']}")
    check(c["repeat"] is True, "key repeat keeps the original")
    check(c["released"]["holding"] is False and c["released"]["sub"] == c["restSub"], f"releasing \\ returns: {c['released']}")
    check(c["hit"] == "DIV.stage" or "pv" in str(c["hit"]) or "canvas" in str(c["hit"]).lower(), f"nothing covers the stage: {c['hit']}")
    check(c["mouseHold"] is True and c["mouseHoldReleased"] is False, "mouse hold still shows the original")


def test_desktop_curves_delete_with_mouse(chromium_steps: dict[str, Any]) -> None:
    c = chromium_steps["curves"]
    base = c["meanIdentity"]
    check(abs(c["meanWithPoint"] - base) > 2, f"the added point changes the preview: {base} → {c['meanWithPoint']}")
    d = c["dblDelete"]
    check(d["before"] == 3 and d["after"] == 2, f"double-click deletes the point: {d}")
    check(d["modelEmpty"] is True, "the curve model is back to identity")
    check(abs(d["mean"] - base) <= 0.5, f"the preview is back to identity: {base} vs {d['mean']}")
    e = c["endsDbl"]
    check(e["before"] == 3 and e["after"] == 3, f"double-clicking end points deletes nothing: {e}")
    check(e["first"] == [0, 0] and e["last"] == [255, 255], f"end points stay: {e}")
    o = c["dragOff"]
    check(o["offMark"] is True, "the point is marked while it is off the graph")
    check(o["before"] == 3 and o["after"] == 2 and o["modelEmpty"] is True, f"dragging off the graph deletes the point: {o}")
    check(abs(o["mean"] - base) <= 0.5, f"the preview is back to identity after drag-off: {base} vs {o['mean']}")
    check(c["dragBack"] == {"before": 3, "after": 3}, f"a drag that comes back keeps the point: {c['dragBack']}")
    end = c["endDragOff"]
    check(end["after"] == end["before"] and end["lastX"] == 255, f"an end point dragged off stays: {end}")


def test_desktop_hdr_toggle_state(chromium_steps: dict[str, Any]) -> None:
    h = chromium_steps["hdr"]
    for key, want in (("raw", "true"), ("rawOff", "false"), ("rawOn", "true")):
        row = h[key]
        check(row["role"] == "switch" and row["checked"] == want, f"{key}: switch state {row}")
        check(row["ax"] is not None and row["ax"]["role"] == "switch", f"{key}: AX role {row['ax']}")
        check(str(row["ax"]["checked"]).lower() == want, f"{key}: AX checked {row['ax']}")
    j = h["jpgOnly"]
    check(j["disabled"] is True and j["title"] == HDR_NO_RAW, f"disabled toggle gives the reason in title: {j}")
    check(j["ax"] is not None and j["ax"]["description"] == HDR_NO_RAW, f"disabled toggle's accessible description: {j['ax']}")


def test_webkit_gesture_and_touch_unchanged(webkit_steps: dict[str, Any]) -> None:
    d = webkit_steps["webkitDesktop"]
    check(d["env"]["touch"] == 0 and d["env"]["title"] == STAGE_TITLE, f"WebKit desktop is a computer: {d['env']}")
    g = d["gesture"]
    check(g["zoom"] > 1.9 and g["zoom"] < 2.1, f"Safari pinch zooms by the gesture scale: {g}")
    check(all(g["prevented"]), f"gesture events are cancelled: {g}")
    check(g["drift"] <= 2, f"gesture keeps the point under the cursor: {g}")
    check(d["click"]["collapsed"] == "0" and d["click"]["collapses"] == [], f"WebKit desktop click keeps the panels: {d['click']}")
    for key in ("webkitPhone", "webkitIpad"):
        t = webkit_steps[key]
        # Playwright 的 WebKit 手机报 0 个触摸点但指针是粗的；iPad 报 5 个。两者都不算电脑。
        check(t["env"]["touch"] > 0 or t["env"]["fine"] is False, f"{key} is a touch device: {t['env']}")
        check(t["env"]["title"] == "", f"{key}: no new stage title")
        check(t["backslashHolding"] is False, f"{key}: \\ does nothing on touch")
        check(t["fCollapsed"] == t["env"]["collapsed"], f"{key}: F does nothing on touch")
        check(t["tapCollapsed"] == "1" and t["tapAgain"] == "0", f"{key}: tap still collapses and expands: {t}")
