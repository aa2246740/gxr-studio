"""电脑浏览器版式回归（2026-11-26，`browser_serial`，生产 mobile/dist）。

L1：没有 WebGPU（WebKit、去掉 navigator.gpu 的 Chromium）时，相机 JPG 的后备画布在 iPad/电脑版式里按 contain 落在舞台里，
和 Chromium Metal 画出来的照片矩形相差不超过 2 px；裁剪框贴着照片。iPad（有触摸）和手机尺寸不动。

只用本地 card250 实拍照片和 g250 相机配方；报告里只有矩形和计数，没有文件名、拍摄时间或 EXIF。
`GXR_DESKTOP_LAYOUT_DIST` 可以指到别的构建（比如旧 head 的 dist），用来证明回归在旧代码上失败。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from conftest import REAL_HOME
from test_g250 import _need_g185_or_g250
from test_mobile_camera_recipe_browser import _card250_pair, _prereq
from test_mobile_ui_browser import RUNTIME, _need

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DIST = Path(os.environ.get("GXR_DESKTOP_LAYOUT_DIST", "") or REPO / "mobile" / "dist")
DRIVER = REPO / "tests" / "support" / "mobile_desktop_layout_browser.mjs"
TOL = 2.0

Rect = dict[str, float]


def check(ok: bool, label: str) -> None:
    """报告里只有矩形和计数（没有照片信息），失败时原样打出来。"""
    assert ok, label


# L2：电脑上这些又宽又矮的窗口要用 iPad/电脑版式，而且什么都不裁、胶片条能用；矮于门槛的、手机模拟尺寸仍是手机版式。
SHORT_SIZES = [(1093, 614), (1280, 600), (1366, 650), (960, 600), (1280, 650), (900, 590)]
CLASSIFY = {
    "1093x614": "ipad-landscape",
    "1280x600": "ipad-landscape",
    "1366x650": "ipad-landscape",
    "960x600": "ipad-landscape",
    "1280x650": "ipad-landscape",
    "900x590": "ipad-landscape",
    "899x600": "phone-landscape",
    "1280x589": "phone-landscape",
    "800x600": "phone-landscape",
    "844x390": "phone-landscape",
    "667x375": "phone-landscape",
    "956x440": "phone-landscape",
    "390x844": "phone-portrait",
    "600x900": "phone-portrait",
    "1024x768": "ipad-landscape",
}
TOUCH_CLASSIFY = {
    **CLASSIFY,
    **{k: "phone-landscape" for k in ("1093x614", "1280x600", "1366x650", "960x600", "1280x650", "900x590", "899x600", "1280x589")},
}


def _run(tmp_path: Path, groups: list[str]) -> dict[str, Any]:
    _prereq()
    _need((DIST / "index.html").is_file(), "mobile/dist is missing; run make mobile-build")
    _raw250, _lib250, dat250, fx250 = _need_g185_or_g250(250)
    pairs = [_card250_pair(i) for i in (1, 2, 3)]
    out = tmp_path / "report.json"
    manifest = {
        "distDir": str(DIST),
        "files": [str(p) for pair in pairs for p in pair] + [str(dat250), str(fx250)],
        "stem": pairs[0][0].stem,
        "shortSizes": [list(s) for s in SHORT_SIZES],
        "shotSizes": ["1093x614", "1366x650", "1280x650"],
        "classifySizes": [[int(n) for n in k.split("x")] for k in CLASSIFY],
        "groups": groups,
        "out": str(out),
        "shotsDir": os.environ.get("GXR_DESKTOP_LAYOUT_SHOTS", ""),
        "shotsPrefix": os.environ.get("GXR_DESKTOP_LAYOUT_SHOTS_PREFIX", "after"),
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(DRIVER), str(RUNTIME), str(manifest_path)],
        cwd=REPO,
        env={**os.environ, "HOME": str(REAL_HOME)},
        check=False,
        capture_output=True,
        text=True,
        timeout=1500,
    )
    assert proc.returncode == 0, f"driver failed ({proc.returncode}): {proc.stderr[-1500:]}"
    report: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
    dump = os.environ.get("GXR_DESKTOP_LAYOUT_REPORT", "")
    if dump:
        Path(dump).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name in groups:
        row = report["groups"][name]
        assert "error" not in row, f"{name}: {row['error']}"
    return report["groups"]


def _inside(inner: Rect, outer: Rect, what: str) -> None:
    check(inner["x"] >= outer["x"] - 1 and inner["y"] >= outer["y"] - 1, f"{what}: photo starts inside the stage ({inner} in {outer})")
    check(
        inner["x"] + inner["w"] <= outer["x"] + outer["w"] + 1 and inner["y"] + inner["h"] <= outer["y"] + outer["h"] + 1,
        f"{what}: photo ends inside the stage ({inner} in {outer})",
    )
    touches_w = abs(inner["w"] - outer["w"]) <= TOL
    touches_h = abs(inner["h"] - outer["h"]) <= TOL
    check(touches_w or touches_h, f"{what}: photo is letterboxed (fills one side of the stage) ({inner} in {outer})")


def _near(a: Rect, b: Rect, what: str) -> None:
    worst = max(abs(a[k] - b[k]) for k in ("x", "y", "w", "h"))
    check(worst <= TOL, f"{what}: rectangles differ by {worst:.1f} px ({a} vs {b})")


def test_mobile_desktop_no_gpu_photo_fit(tmp_path: Path) -> None:
    groups = _run(tmp_path, ["fit"])["fit"]
    metal = groups["metal"]
    check(
        metal.get("adapter") is not None and metal["adapter"]["fallback"] is False, f"Metal Chromium has a real adapter: {metal.get('adapter')}"
    )
    check(metal["errors"] == [], f"Metal: no page errors: {metal['errors']}")
    ref = {row["size"]: row for row in metal["rows"]}
    for row in metal["rows"]:
        check(row["view"]["kind"] == "gpu", f"metal {row['size']}: GPU canvas shows")
        _inside(row["view"]["photo"], row["view"]["pv"], f"metal {row['size']}")
        _near(row["crop"]["cbox"], row["crop"]["photo"], f"metal {row['size']} crop frame")
    for engine in ("nogpu", "webkit"):
        res = groups[engine]
        if res.get("skipped"):
            pytest.skip(res["skipped"])
        check(res["errors"] == [], f"{engine}: no page errors: {res['errors']}")
        check(len(res["rows"]) == 4, f"{engine}: four sizes measured")
        for row in res["rows"]:
            size = row["size"]
            view, crop = row["view"], row["crop"]
            check(view["layout"].startswith("ipad"), f"{engine} {size}: iPad/desktop layout ({view['layout']})")
            check(view["kind"] == "still", f"{engine} {size}: camera-JPG fallback shows")
            _inside(view["photo"], view["pv"], f"{engine} {size}")
            _near(view["photo"], ref[size]["view"]["photo"], f"{engine} {size} vs Metal")
            _inside(crop["photo"], crop["pv"], f"{engine} {size} crop")
            _near(crop["cbox"], crop["photo"], f"{engine} {size} crop frame on the photo")
            _near(crop["cbox"], ref[size]["crop"]["cbox"], f"{engine} {size} crop frame vs Metal")


def test_mobile_desktop_touch_fit_unchanged(tmp_path: Path) -> None:
    """iPad（Mac UA + 触摸）和手机尺寸：照片仍在舞台里按 contain 摆放，裁剪框贴着照片。"""
    groups = _run(tmp_path, ["fitTouch"])["fitTouch"]
    if groups.get("skipped"):
        pytest.skip(groups["skipped"])
    want = {"1180x820": "ipad-landscape", "1024x1366": "ipad-portrait", "390x844": "phone-portrait", "844x390": "phone-landscape"}
    for kind in ("ipad", "phone"):
        res = groups[kind]
        check(res["errors"] == [], f"{kind}: no page errors: {res['errors']}")
        for row in res["rows"]:
            size = row["size"]
            check(row["view"]["layout"] == want[size], f"{kind} {size}: layout {row['view']['layout']}")
            _inside(row["view"]["photo"], row["view"]["pv"], f"{kind} {size}")
            _near(row["crop"]["cbox"], row["crop"]["photo"], f"{kind} {size} crop frame")


def test_mobile_desktop_short_windows(tmp_path: Path) -> None:
    """L2：电脑上 1093×614、1280×600、1366×650、960×600、1280×650 用 iPad/电脑版式，网格和编辑器都不裁，胶片条能用。"""
    groups = _run(tmp_path, ["short"])["short"]
    check(groups["errors"] == [], f"no page errors: {groups['errors']}")
    for row in groups["rows"]:
        size = row["size"]
        for view in ("grid", "recipe", "adjust", "crop", "exportOptions"):
            m = row[view]
            check(m["layout"] == "ipad-landscape", f"{size} {view}: iPad/desktop layout ({m['layout']})")
            check(m["pageScroll"] is False, f"{size} {view}: the page itself does not scroll")
            check(m["clipped"] == [], f"{size} {view}: nothing clipped: {m['clipped']}")
        check(row["grid"]["gridCols"] >= 5, f"{size}: grid has desktop columns ({row['grid']['gridCols']})")
        for view in ("recipe", "adjust", "crop"):
            m = row[view]
            check(m["recipeStrip"] is not None and m["recipeStrip"]["inside"], f"{size} {view}: recipe strip visible: {m['recipeStrip']}")
            check(m["filmstrip"] is not None and m["filmstrip"]["inside"], f"{size} {view}: filmstrip visible: {m['filmstrip']}")
            check(len(m["filmCells"]) >= 2 and min(m["filmCells"]) >= 44, f"{size} {view}: filmstrip cells usable: {m['filmCells']}")
            check(m["stageH"] >= 360, f"{size} {view}: the stage keeps room for the photo ({m['stageH']} px)")
            check(m["panel"] is not None and m["panel"]["h"] >= 180, f"{size} {view}: side panel keeps room ({m['panel']})")
        check(row["recipe"]["panel"]["inView"] >= 3, f"{size}: recipe panel shows 3 controls without scrolling ({row['recipe']['panel']})")
        check(row["adjust"]["panel"]["inView"] >= 9, f"{size}: adjust panel shows its chips and slider ({row['adjust']['panel']})")
        check(row["filmSwitch"] is True, f"{size}: clicking the second filmstrip cell opens it")
    got = groups["computer"]["layouts"]
    check(got == CLASSIFY, f"computer layouts: {got}")
    touch = groups["phone"]["layouts"]
    check(touch == TOUCH_CLASSIFY, f"touch layouts unchanged: {touch}")


HOVER_POINTER = ("tab", "download", "more", "back", "recipeCard", "stripSwitch", "filmCell", "chip")
RESIZE_CURSORS = {"cornerTL": "nwse-resize", "cornerTR": "nesw-resize", "edgeT": "ns-resize", "edgeL": "ew-resize", "ruler": "ew-resize"}


def test_mobile_desktop_hover_and_cursors(tmp_path: Path) -> None:
    """L5：电脑上按钮、配方卡、胶囊和胶片条有手形光标和悬停提示，裁剪手柄和拉直尺有方向光标；iPad（有触摸）不变。"""
    groups = _run(tmp_path, ["hover"])["hover"]
    for engine in ("computer", "webkit"):
        pc = groups[engine]
        if pc.get("skipped"):
            continue
        check(pc["errors"] == [], f"{engine}: no page errors: {pc['errors']}")
        check(pc["media"] == {"hover": True, "fine": True}, f"{engine} context hovers with a fine pointer: {pc['media']}")
        for name in HOVER_POINTER:
            row = pc[name]
            check(row is not None, f"{engine} {name}: target is visible")
            check(row["cursor"] == "pointer", f"{engine} {name}: pointer cursor ({row['cursor']})")
            check(row["hit"] == "self", f"{engine} {name}: the mouse is over it ({row['hit']})")
            check(row["changed"] is True, f"{engine} {name}: hover changes how it looks")
        for name, want in RESIZE_CURSORS.items():
            row = pc[name]
            check(row is not None and row["cursor"] == want, f"{engine} {name}: {want} cursor ({row})")
    pc = groups["computer"]
    pad = groups["ipad"]
    check(pad["errors"] == [], f"ipad: no page errors: {pad['errors']}")
    check(pad["media"] == {"hover": False, "fine": False}, f"touch context has no hover and a coarse pointer: {pad['media']}")
    for name in (*HOVER_POINTER, *RESIZE_CURSORS):
        row = pad[name]
        check(row is not None, f"ipad {name}: target is visible")
        check(row["cursor"] in ("auto", "default"), f"ipad {name}: cursor unchanged ({row['cursor']})")
        check(row["changed"] is False, f"ipad {name}: no hover style on touch")
    # 网格格子的悬停由 desktop-keys 负责：合进来以后电脑上是手型，触屏不变。
    check(pc["gridCellCursor"] == "pointer", f"computer grid cells use the hand cursor (desktop-keys): {pc['gridCellCursor']}")
    check(pad["gridCellCursor"] in ("auto", "default"), f"touch grid cells keep the default cursor: {pad['gridCellCursor']}")


NO_GPU_NOTE = "无显卡预览，暂不能调整"


def test_mobile_desktop_no_gpu_locks_fine_tune_and_curves(tmp_path: Path) -> None:
    """L7 / P2-9：没有显卡预览时配方微调滑块和曲线入口和「调整」一样停用、说同一句话；配方卡照样能选。Metal 下都能用。"""
    groups = _run(tmp_path, ["lock"])["lock"]
    metal = groups["metal"]
    check(metal["errors"] == [], f"metal: no page errors: {metal['errors']}")
    check(metal["recipe"]["knobCount"] >= 3 and metal["recipe"]["knobsDisabled"] == 0, f"metal: fine-tune sliders live: {metal['recipe']}")
    check(metal["recipe"]["knobNote"] is None, "metal: no lock note")
    check(metal["knobMoved"] is True, "metal: clicking a fine-tune track moves it")
    check(
        metal["adjust"]["sliderDisabled"] is False and metal["adjust"]["curvesDisabled"] is False,
        f"metal: adjust and curves live: {metal['adjust']}",
    )
    check(metal["moreCurves"]["disabled"] is False, "metal: 更多 → 曲线 live")
    for name in ("nogpu", "webkit", "webkitPhone"):
        res = groups[name]
        if res.get("skipped"):
            continue
        check(res["errors"] == [], f"{name}: no page errors: {res['errors']}")
        rec = res["recipe"]
        check(rec["knobCount"] >= 1 and rec["knobsDisabled"] == rec["knobCount"], f"{name}: every fine-tune slider is disabled: {rec}")
        check(rec["knobReasons"] == [NO_GPU_NOTE], f"{name}: sliders are described by the note: {rec['knobReasons']}")
        check(rec["knobNote"] == NO_GPU_NOTE, f"{name}: the note is visible on 配方: {rec['knobNote']}")
        check(rec["knobOpacity"] == "0.35", f"{name}: same disabled look as 调整 ({rec['knobOpacity']})")
        check(res["recipeClip"]["clipped"] == [], f"{name}: nothing clipped on 配方: {res['recipeClip']['clipped']}")
        check(res["knobMoved"] is False, f"{name}: clicking a locked fine-tune track does nothing")
        adj = res["adjust"]
        check(adj["sliderDisabled"] is True and adj["sliderReason"] == NO_GPU_NOTE, f"{name}: adjust slider locked as before: {adj}")
        check(adj["curvesDisabled"] is True and adj["curvesReason"] == NO_GPU_NOTE, f"{name}: 曲线 entry locked with the note: {adj}")
        check(
            res["moreCurves"]["disabled"] is True and NO_GPU_NOTE in res["moreCurves"]["text"],
            f"{name}: 更多 → 曲线 locked: {res['moreCurves']}",
        )
    for name in ("nogpu", "webkit"):
        if not groups[name].get("skipped"):
            check(groups[name]["cardSwitched"] is True, f"{name}: recipe cards stay selectable")


def test_mobile_desktop_adjust_slider_names(tmp_path: Path) -> None:
    """L11：调整的每根滑块都叫看得见的名字，aria-valuetext 是看得见的数值（曝光加 EV）。电脑和手机版式都查。"""
    groups = _run(tmp_path, ["names"])["names"]
    for kind in ("computer", "phone"):
        res = groups[kind]
        check(res["errors"] == [], f"{kind}: no page errors: {res['errors']}")
        check(len(res["rows"]) == 10, f"{kind}: all ten adjust sliders checked ({len(res['rows'])})")
        for row in res["rows"]:
            key = row["key"]
            check(bool(row["label"]) and row["ariaLabel"] == row["label"], f"{kind} {key}: name equals the visible label: {row}")
            check(row["roleCount"] == 1, f"{kind} {key}: one slider found by its visible label: {row}")
            want = f"{row['value']} EV" if key == "exposure_ev" else row["value"]
            check(row["valueText"] == want, f"{kind} {key}: aria-valuetext is the shown value: {row}")
            check(row["value"] not in ("0", "0.0"), f"{kind} {key}: ArrowRight moved the value: {row}")


def test_mobile_desktop_crop_ruler_labels_never_cut(tmp_path: Path) -> None:
    """L12：拉直尺上的度数要么整个看得见、要么藏起来；电脑、iPad、手机尺寸，0° 和转过几度后都查。"""
    groups = _run(tmp_path, ["ruler"])["ruler"]
    for kind in ("computer", "ipad", "phone"):
        res = groups[kind]
        check(res["errors"] == [], f"{kind}: no page errors: {res['errors']}")
        for row in res["rows"]:
            for when in ("rest", "turned"):
                m = row[when]
                check(m is not None, f"{kind} {row['size']} {when}: ruler visible")
                check(m["cut"] == [], f"{kind} {row['size']} {when}: no label cut by the ruler edge: {m}")
                # 手机横屏的尺子只有 180 宽：±5° 正好压在边上，只剩中间的 0°。
                want = 3 if m["width"] >= 240 else 1
                check(len(m["shown"]) >= want, f"{kind} {row['size']} {when}: labels still show: {m}")
            check(row["turned"]["angle"] != row["rest"]["angle"], f"{kind} {row['size']}: arrow keys turned the ruler")


def test_mobile_desktop_start_buttons_follow_the_content(tmp_path: Path) -> None:
    """L14：电脑大屏首屏上按钮紧跟内容（不再隔几百像素贴底），矮窗口不出滚动条；iPad（有触摸）和手机照旧贴底。"""
    groups = _run(tmp_path, ["start"])["start"]
    for row in groups["computer"]["rows"]:
        check(row["layout"] == "ipad-landscape", f"computer {row['size']}: desktop layout ({row['layout']})")
        check(row["gap"] <= 40, f"computer {row['size']}: buttons sit right under the content ({row})")
        check(row["scrolls"] is False, f"computer {row['size']}: the start screen fits without scrolling ({row})")
        check(abs(row["topSpace"] - row["bottomSpace"]) <= 40 or row["topSpace"] <= 24, f"computer {row['size']}: the group is centred ({row})")
    for kind in ("ipad", "phone"):
        check(groups[kind]["errors"] == [], f"{kind}: no page errors")
        for row in groups[kind]["rows"]:
            check(row["bottomSpace"] <= 28 and row["gap"] > 100, f"{kind} {row['size']}: buttons still at the bottom ({row})")


def test_mobile_desktop_wheel_scrolls_strips(tmp_path: Path) -> None:
    """P2-6：电脑上竖向滚轮让配方条（和溢出的胶片条）左右滚，滚不动时不拦，露细滚动条；iPad 触摸照旧。"""
    groups = _run(tmp_path, ["wheel"])["wheel"]
    for name in ("computer", "webkit"):
        res = groups[name]
        if res.get("skipped"):
            continue
        check(res["errors"] == [], f"{name}: no page errors: {res['errors']}")
        down = res["recipeDown"]
        check(
            down["overflow"] > 0 and down["prevented"] is True and down["after"] > down["before"],
            f"{name}: wheel scrolls the recipe strip: {down}",
        )
        check(down["scrollbar"] != "none", f"{name}: the recipe strip's scrollbar is no longer hidden: {down}")
        if name == "webkit":  # 无头 Chromium 总是藏滚动条，量不出来
            check(down["bar"] >= 4, f"{name}: a visible scrollbar on the recipe strip: {down}")
        for key in ("recipeUpAtStart", "recipeDownAtEnd", "filmNoOverflow"):
            row = res[key]
            check(row["prevented"] is False and row["after"] == row["before"], f"{name} {key}: wheel left alone when it can't scroll: {row}")
        film = res["filmOverflow"]
        check(
            film["overflow"] > 0 and film["prevented"] is True and film["after"] > 0, f"{name}: wheel scrolls an overflowing filmstrip: {film}"
        )
        check(film["scrollbar"] != "none", f"{name}: the filmstrip's scrollbar is no longer hidden: {film}")
        if name == "webkit":
            check(film["bar"] >= 4, f"{name}: a visible scrollbar on the overflowing filmstrip: {film}")
    check(groups["computer"]["realWheel"] > 0, f"a real mouse wheel scrolls the recipe strip ({groups['computer']['realWheel']})")
    pad = groups["ipad"]
    check(pad["errors"] == [], f"ipad: no page errors: {pad['errors']}")
    for key in ("recipeDown", "filmOverflow"):
        row = pad[key]
        check(row["prevented"] is False and row["after"] == row["before"], f"ipad {key}: touch device unchanged: {row}")
        check(row["scrollbar"] == "none" and row["bar"] == 0, f"ipad {key}: scrollbar still hidden: {row}")


def test_mobile_desktop_side_panel_scroll_cue(tmp_path: Path) -> None:
    """P3-13：1440×900 和 1024×768 上侧边面板放不下时，电脑上有常驻滚动条（WebKit 量得出），
    滚到底「全部重置」和最后一根微调整个露出来；iPad 触摸不变。"""
    groups = _run(tmp_path, ["panel"])["panel"]
    for name in ("metal", "webkit"):
        res = groups[name]
        if res.get("skipped"):
            continue
        check(res["errors"] == [], f"{name}: no page errors: {res['errors']}")
        for row in res["rows"]:
            for view in ("recipe", "adjust"):
                m = row[view]
                check(m is not None and m["lastReachable"] is True, f"{name} {row['size']} {view}: last control reachable: {m}")
                if name == "webkit" and m["overflow"] > 0:
                    check(m["bar"] >= 4, f"{name} {row['size']} {view}: overflowing panel shows a scrollbar: {m}")
        check(any(row["recipe"]["overflow"] > 0 for row in res["rows"]), f"{name}: the recipe panel does overflow at these sizes")
    pad = groups["ipad"]
    if not pad.get("skipped"):
        check(pad["errors"] == [], f"ipad: no page errors: {pad['errors']}")
        for row in pad["rows"]:
            for view in ("recipe", "adjust"):
                check(row[view]["bar"] == 0, f"ipad {row['size']} {view}: touch keeps the overlay scrollbar: {row[view]}")


def test_mobile_desktop_hdr_note_clear_of_frame_caption(tmp_path: Path) -> None:
    """P3-18：套了带说明文字的边框（徕卡水印）后，「这块屏幕显示不了 HDR」挪到舞台上沿，不压边框下沿；没边框时照旧在下沿。"""
    groups = _run(tmp_path, ["note"])["note"]
    for kind in ("computer", "phone"):
        res = groups[kind]
        check(res["errors"] == [], f"{kind}: no page errors: {res['errors']}")
        plain, framed = res["plain"], res["framed"]
        check(plain is not None and plain["decor"] != "1", f"{kind}: note shows without a frame: {plain}")
        check(plain["noteBottom"] >= plain["pvBottom"] - 40, f"{kind}: without a frame the note stays at the bottom: {plain}")
        check(framed is not None and framed["decor"] == "1", f"{kind}: note still shows with the frame: {framed}")
        check(framed["overBottomBand"] is False, f"{kind}: the note is clear of the frame's bottom caption band: {framed}")
