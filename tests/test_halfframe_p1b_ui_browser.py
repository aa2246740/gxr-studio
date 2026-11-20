"""无头真浏览器验收：halfframe-p1b-ui（方案 A：桌面把半格一对当作一张照片）+ 非半格图库 DOM 快照回归。"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from conftest import REAL_HOME
from gxr_studio.app import create_app
from gxr_studio.cards.discovery import find_cards
from gxr_studio.cards.importer import ImportOptions, run_import
from gxr_studio.library.store import Library
from support import samples
from test_halfframe_p0 import (
    WEB,
    _clear_desktop_cache,
    _field_folder,
    _setup_g250_calibrated_home,
    _sorted_pairs,
    _stage_volume_pairs,
)
from test_halfframe_p1a_field import _index_shots_by_jpg_sha
from test_mobile_ui_browser import RUNTIME, _need

pytestmark = pytest.mark.browser_serial

DRIVER = Path(__file__).resolve().parent / "support" / "desktop_halfframe_p1b_ui.mjs"
NON_HF_BASELINE = Path(__file__).resolve().parent / "support" / "non_halfframe_gallery_baseline.json"
NON_HF_EDITOR_BASELINE = Path(__file__).resolve().parent / "support" / "non_halfframe_editor_baseline.json"
NON_HF_EDITOR_A12_50_BASELINE = Path(__file__).resolve().parent / "support" / "non_halfframe_editor_a12_50_baseline.json"


def _run_driver(settings: Any, manifest: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    _need(shutil.which("node") is not None, "node is required for desktop browser test")
    _need(RUNTIME.is_file(), f"pinned Playwright runtime missing: {RUNTIME}")

    web_override = os.environ.get("GXR_TEST_WEB_DIR")
    web_dir = Path(web_override) if web_override else WEB
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(settings.with_(port=port, web_dir=web_dir)), log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started, "desktop test server did not start"

        out_path = tmp_path / "p1b_ui_out.json"
        m_path = tmp_path / "manifest.json"
        m_path.write_text(
            json.dumps({**manifest, "base": f"http://127.0.0.1:{port}", "token": settings.token, "out": str(out_path)}),
            encoding="utf-8",
        )
        proc = subprocess.run(
            ["node", str(DRIVER), str(RUNTIME), str(m_path)],
            cwd=str(DRIVER.parent),
            env={**os.environ, "HOME": str(REAL_HOME)},
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert proc.returncode == 0, f"driver failed (rc={proc.returncode}): {proc.stderr[-2000:]}"
        assert out_path.is_file(), proc.stderr[-2000:]
        return json.loads(out_path.read_text(encoding="utf-8"))
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        sock.close()


def _expected_half_start_col(orientation: int, *, raw: bool) -> int:
    sensor_col = 1099 if raw else 1100
    return 4288 - sensor_col - 2088 if orientation in (3, 8) else sensor_col


def test_desktop_halfframe_p1b_ui_card250(
    settings: Any,
    lib: Library,
    volumes: Path,
    tmp_path: Path,
) -> None:
    """真 card250 11 张（3 对半格 + 4 张未成对第 1 张 + 1 张 XPan）完整验收。"""
    _clear_desktop_cache()
    pairs = _sorted_pairs(_field_folder("card250"))
    assert len(pairs) == 11
    _stage_volume_pairs(volumes, pairs)
    _setup_g250_calibrated_home(settings)
    (settings.home_dir / "flags.json").write_text(json.dumps({"gpu_preview": False}), encoding="utf-8")

    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    by_idx = _index_shots_by_jpg_sha(lib, pairs)
    pair_first_ids = [by_idx[i]["shot_id"] for i in (1, 3, 5)]
    pair_second_ids = [by_idx[i]["shot_id"] for i in (2, 4, 6)]
    half_first_id = by_idx[7]["shot_id"]
    xpan_id = by_idx[11]["shot_id"]
    p1_name = str(by_idx[1]["name"])
    p2_name = str(by_idx[2]["name"])
    hf_w = int(by_idx[7].get("width") or 4288)
    hf_h = int(by_idx[7].get("height") or 2848)
    lib.close()

    ss_dir = os.environ.get("GXR_P1B_SCREENSHOT_DIR")
    screenshots = (
        {
            "gallery": str(Path(ss_dir) / "gallery.png"),
            "editor": str(Path(ss_dir) / "editor.png"),
            "export": str(Path(ss_dir) / "export.png"),
            "viewFirst": str(Path(ss_dir) / "view-first.png"),
        }
        if ss_dir
        else None
    )

    ui = _run_driver(
        settings,
        {
            "mode": "card250",
            "pairFirstIds": pair_first_ids,
            "pairSecondIds": pair_second_ids,
            "primaryPairSecondId": pair_second_ids[0],
            "primaryPairFirstName": p1_name,
            "primaryPairSecondName": p2_name,
            "halfFirstId": half_first_id,
            "xpanId": xpan_id,
            "screenshots": screenshots,
        },
        tmp_path,
    )
    assert "error" not in ui, ui
    assert ui["pageErrors"] == [], ui["pageErrors"]

    # 1. Gallery
    g = ui["steps"]["gallery"]
    assert g["totalCells"] == 8, g
    assert len(g["pairCells"]) == 3, g
    assert all(c["objectFit"] == "contain" and "半格" in c["badges"] for c in g["pairCells"]), g["pairCells"]
    assert g["shownFirstIds"] == [], g
    assert len(g["halfFirstCells"]) == 4, g
    assert all(not c["isPair"] for c in g["halfFirstCells"]), g["halfFirstCells"]
    assert g["xpanCell"] is not None and g["xpanCell"]["badges"] == [], g["xpanCell"]
    assert g["libCount"] == "11 张 · 3 对半格", g["libCount"]
    assert "筛选" in g["sidebarGroups"], g["sidebarGroups"]
    assert g["hfFilterText"] == "半格 3 对", g["hfFilterText"]

    # P2-1 + P3-5: Filter prunes hidden selection with undo toast, unified wording
    flt = ui["steps"]["filter"]
    assert flt["mixedSel"]["selbarCount"] == "已选 2 项（含 1 对半格）", flt["mixedSel"]
    assert flt["mixedSel"]["exportBtn"] == "导出选中的 2 项", flt["mixedSel"]
    assert flt["mixedSel"]["pasteBtn"] == "粘贴调整到 2 项", flt["mixedSel"]
    assert flt["filteredCount"] == 3, flt
    assert flt["libTitle"] == "半格" and flt["libCount"] == "· 3 对", flt
    assert flt["hasHiddenXpan"] is False and flt["selSize"] == 2, flt
    assert flt["selbarCount"] == "已选 1 对半格", flt
    assert flt["exportBtn"] == "导出选中的 1 对半格", flt
    assert flt["pasteBtn"] == "粘贴调整到 1 对半格", flt
    assert flt["hasUndoBtn"] is True, flt
    assert flt["toastPos"] is not None and flt["toastPos"]["aboveSelbar"] is True, flt["toastPos"]
    assert flt["afterUndo"]["restoredCount"] == 8 and flt["afterUndo"]["hasXpan"] is True, flt["afterUndo"]

    # 2. Selection + batch export while filtered -> only visible pair (no hidden XPan)
    sb = ui["steps"]["selectionBatchExport"]
    assert sb["dialog"]["hasHfRow"] is True and sb["dialog"]["pressedMode"] == "双联一张", sb
    assert sb["batchReqs"] and sb["batchReqs"][0]["status"] == 202 and sb["batchReqs"][0]["halfframe"] == "diptych", sb
    assert xpan_id not in sb["batchReqs"][0]["sentIds"] and sb["batchReqs"][0]["sentCount"] == 2, sb
    assert sb["resultText"] == "导出 1 张，其中 1 对半格按双联导出", sb

    # 3. Editor for a pair
    ed = ui["steps"]["editor"]
    init = ed["initial"]
    assert init["titleName"] == "半格一对", init
    assert init["hasBothNames"] is True, init
    assert init["pairBadge"] == "RAW+JPG ×2", init
    assert "电脑预测 · 两张 RAW 合成" in init["stageLabels"], init["stageLabels"]
    assert "原图 · 相机双联" in ed["origStageLabels"], ed["origStageLabels"]
    assert init["pressedCards"] == 0 and init["totalCards"] > 0, init
    assert init["loadedThumbs"] > 0, init
    assert "拍摄时：第 1 张" in init["recipeText"] and "· 第 2 张" in init["recipeText"], init["recipeText"]
    assert "改一次，两半一起变" in init["pairHint"], init["pairHint"]
    assert [b["text"] for b in init["viewButtons"]] == ["双联", "第 1 张", "第 2 张"], init["viewButtons"]
    assert init["filmstripCount"] == 8 and init["filmstripHasFirst"] is False, init
    assert init["stripMaskCount"] == 0, init
    assert ed["infoTab"] == {"hasFirstLabel": True, "hasSecondLabel": True, "hasBothNames": True}, ed["infoTab"]

    vb = ed["viewbar"]
    assert vb["firstView"]["pressed"] == "第 1 张", vb
    assert vb["firstView"]["cardDisabled"] is True and vb["firstView"]["selectDisabled"] is True, vb
    m1 = vb["firstView"]["stripMask"]
    assert m1["present"] is True and m1["insidePv"] is True and m1["smallerThanPv"] is True, m1
    n1 = vb["firstView"]["roNote"]
    assert n1["present"] is True and n1["text"] == "只看这一张；调整对整张双联生效。", n1
    assert n1["inPanelOrViewbar"] is True and n1["overlapsStage"] is False, n1
    assert vb["secondStripMask"]["present"] is True and vb["secondStripMask"]["insidePv"] is True, vb["secondStripMask"]
    n2 = vb["secondRoNote"]
    assert n2["present"] is True and n2["text"] == "只看这一张；调整对整张双联生效。", n2
    assert vb["putsDuringSingleViews"] == 0, vb
    assert vb["diffFirstSecond"] > 3.0, vb
    assert vb["backToDiptych"]["pressed"] == "双联" and vb["backToDiptych"]["cardDisabled"] is False, vb
    assert vb["backToDiptych"]["stripMask"]["present"] is False, vb["backToDiptych"]["stripMask"]
    assert vb["backToDiptych"]["roNote"]["present"] is False, vb["backToDiptych"]["roNote"]
    assert ed["nav"]["landedOnFirst"] is False, ed["nav"]

    # 4. Export dialog (incl. P3-3 title follows scope)
    ex = ui["steps"]["exportDialog"]
    assert ex["exportDefault"]["title"] == "导出 1 项（半格一对）", ex["exportDefault"]
    assert ex["exportScopeDateTitle"] == "导出", ex
    assert ex["exportScopeBackTitle"] == "导出 1 项（半格一对）", ex
    assert ex["exportDefault"]["hasHfRow"] is True and ex["exportDefault"]["pressedMode"] == "双联一张", ex["exportDefault"]
    assert "1 个文件" in ex["exportDefault"]["sizeText"], ex["exportDefault"]
    # P1c: both halves have RAW, so the pair offers HDR (off by default) and names the _hdr file while it is on
    assert ex["exportDefault"]["hdrDisabled"] is False and "HDR 需要 RAW 文件" not in ex["exportDefault"]["hdrText"], ex["exportDefault"]
    assert "_hdr" not in ex["exportDefault"]["nameText"], ex["exportDefault"]
    assert "_gxr_hdr.jpg" in ex["exportHdrOn"]["nameText"], ex["exportHdrOn"]
    assert "做不成 HDR 时是" in ex["exportHdrOn"]["nameText"], ex["exportHdrOn"]
    assert "_hdr" not in ex["exportHdrOff"]["nameText"], ex["exportHdrOff"]
    assert "2 个文件" in ex["exportSingles"]["sizeText"] and "2088" in ex["exportSingles"]["sizeText"], ex["exportSingles"]
    assert "3 个文件" in ex["exportBoth"]["sizeText"], ex["exportBoth"]
    assert ex["singlesExportReqs"] and ex["singlesExportReqs"][0]["status"] == 200 and ex["singlesExportReqs"][0]["fileCount"] == 2, ex
    exported_after_singles = [p for p in settings.export_dir.rglob("*") if p.is_file()]
    assert len(exported_after_singles) == 3, f"expected 1 + 2 = 3 exported files total, got {len(exported_after_singles)}"
    assert ex["exportReopened"]["pressedMode"] == "两张单格", ex["exportReopened"]
    assert ex["exportXpan"]["hasHfRow"] is False, ex["exportXpan"]

    # 5. Crop preset 「取半格」 on RAW half_first (P3-4: DNG column 1099/1101)
    cr = ui["steps"]["cropHalfFirst"]
    assert cr["xpanHasHalfCrop"] is False, cr
    assert cr["cropPressed"] is True, cr
    rect = cr["crop"]["rect"]
    px_w = round(float(rect[2]) * hf_w)
    px_h = round(float(rect[3]) * hf_h)
    assert px_w == 2088 or px_h == 2088, f"expected 2088px half-frame crop, got {px_w}x{px_h} from {rect}"
    ori = int(cr.get("orientation") or 1)
    is_raw_base = bool(cr.get("hasRaw")) and cr.get("edLabel") != "camera-jpg"
    assert is_raw_base is True, cr
    exp_start = _expected_half_start_col(ori, raw=True)
    got_start = round(float(rect[1]) * hf_h) if hf_h > hf_w else round(float(rect[0]) * hf_w)
    assert got_start == exp_start, f"expected RAW strip start {exp_start} (ori={ori}), got {got_start} from {rect}"


def test_desktop_halfframe_p1b_jpgonly_photo1_and_half_first_crop(
    settings: Any,
    lib: Library,
    volumes: Path,
    tmp_path: Path,
) -> None:
    """P2-2 + P3-4: 第 1 张只有 JPG 时 `第 1 张` 正常出图且不误导 RAW；JPG-only `取半格` 落在 1100 列。"""
    _clear_desktop_cache()
    pairs = _sorted_pairs(_field_folder("card250"))
    # Stage shot 1 JPG-only, shot 2 RAW+JPG, shot 7 JPG-only
    card_dir = volumes / "CARD_JPGONLY" / "DCIM" / "100RICOH"
    card_dir.mkdir(parents=True)
    shutil.copy2(pairs[0][1], card_dir / pairs[0][1].name)
    shutil.copy2(pairs[1][0], card_dir / pairs[1][0].name)
    shutil.copy2(pairs[1][1], card_dir / pairs[1][1].name)
    shutil.copy2(pairs[6][1], card_dir / pairs[6][1].name)
    _setup_g250_calibrated_home(settings)
    (settings.home_dir / "flags.json").write_text(json.dumps({"gpu_preview": False}), encoding="utf-8")

    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    by_idx = _index_shots_by_jpg_sha(lib, [pairs[0], pairs[1], pairs[6]])
    p2_id = by_idx[2]["shot_id"]
    jpg_hf = by_idx[3]
    hf_w = int(jpg_hf.get("width") or 4288)
    hf_h = int(jpg_hf.get("height") or 2848)
    lib.close()

    ui = _run_driver(
        settings,
        {
            "mode": "jpgonly_p1",
            "pairSecondId": p2_id,
            "jpgHalfFirstId": jpg_hf["shot_id"],
        },
        tmp_path,
    )
    assert "error" not in ui, ui
    assert ui["pageErrors"] == [], ui["pageErrors"]
    j = ui["steps"]["jpgOnlyP1"]
    assert j["initial"]["pairHint"] == "", j["initial"]
    assert "RAW" in j["initial"]["noticeText"], j["initial"]
    assert j["initial"]["stripMaskCount"] == 0, j["initial"]
    assert j["firstReqs"] and all(r["status"] == 200 for r in j["firstReqs"]), j["firstReqs"]
    assert j["firstMask"]["hasMask"] is True, j["firstMask"]
    assert j["firstCenter"] and j["dipCenter"], j
    # Diptych has dark black vertical seam in the center; single camera JPG does not
    assert abs(j["firstCenter"]["centerMean"] - j["dipCenter"]["centerMean"]) > 5.0, j
    assert j["secondReqs"] and all(r["status"] == 200 for r in j["secondReqs"]), j["secondReqs"]
    assert j["exportHdr"]["disabled"] is True and "HDR 需要 RAW 文件" in j["exportHdr"]["text"], j["exportHdr"]

    rect = j["jpgHalfFirstCrop"]["rect"]
    got_start = round(float(rect[1]) * hf_h) if hf_h > hf_w else round(float(rect[0]) * hf_w)
    assert got_start == 1100, f"expected camera-JPG strip start 1100, got {got_start} from {rect}"


def test_desktop_halfframe_p1b_framed_pair_single_views(
    settings: Any,
    lib: Library,
    volumes: Path,
    tmp_path: Path,
) -> None:
    """N1 + UX: 带白色边框和时间戳的半格一对，单张视图不画边框/时间戳且遮罩对齐照片 1 px 内，双联仍带边框。"""
    _clear_desktop_cache()
    pairs = _sorted_pairs(_field_folder("card250"))
    # Stage pair 1 (shots 1 + 2, RAW+JPG)
    _stage_volume_pairs(volumes, pairs[:2])
    _setup_g250_calibrated_home(settings)
    (settings.home_dir / "flags.json").write_text(json.dumps({"gpu_preview": False}), encoding="utf-8")

    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    by_idx = _index_shots_by_jpg_sha(lib, pairs[:2])
    p2_id = by_idx[2]["shot_id"]
    lib.close()

    ss_dir = os.environ.get("GXR_P1B_SCREENSHOT_DIR")
    screenshots = {"framedFirst": str(Path(ss_dir) / "framed-first.png")} if ss_dir else {}
    ui = _run_driver(
        settings,
        {
            "mode": "framed_pair",
            "pairSecondId": p2_id,
            "screenshots": screenshots,
        },
        tmp_path,
    )
    assert "error" not in ui, ui
    assert ui["pageErrors"] == [], ui["pageErrors"]
    fp = ui["steps"]["framedPair"]

    dip = fp["diptych"]
    assert dip["framed"] is True and dip["hasDeco"] is True and dip["pvBiggerThanImg"] is True, dip
    assert dip["stripMaskCount"] == 0 and dip["roNote"]["present"] is False, dip

    for key in ("first", "second"):
        sv = fp[key]
        assert sv["framed"] is False and sv["hasDeco"] is False, (key, sv)
        assert sv["pvMatchesImg"] is True, (key, sv)
        assert sv["stripMaskCount"] == 1 and sv["maskAlign"] is not None, (key, sv)
        assert sv["maskAlign"]["maxErrPx"] <= 1.0, (key, sv["maskAlign"])
        note = sv["roNote"]
        assert note["present"] is True and note["text"] == "只看这一张；调整对整张双联生效。", (key, note)
        assert note["inPanelOrViewbar"] is True and note["overlapsStage"] is False, (key, note)

    back = fp["backToDiptych"]
    assert back["framed"] is True and back["hasDeco"] is True and back["pvBiggerThanImg"] is True, back
    assert back["stripMaskCount"] == 0 and back["roNote"]["present"] is False, back


def test_non_halfframe_gallery_dom_unchanged(
    settings: Any,
    lib: Library,
    card: Any,
    tmp_path: Path,
) -> None:
    """不含半格照片的图库 DOM 结构与 ee33a95 基线逐字段一致。"""
    run_import(lib, find_cards((card.root.parent,))[0], ImportOptions(min_free_bytes=0))
    lib.close()

    ui = _run_driver(settings, {"mode": "snapshot"}, tmp_path)
    assert "error" not in ui, ui
    assert ui["pageErrors"] == [], ui["pageErrors"]
    snap = ui["steps"]["snapshot"]

    if os.environ.get("GXR_UPDATE_NON_HF_BASELINE") == "1":
        NON_HF_BASELINE.write_text(json.dumps(snap, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert NON_HF_BASELINE.is_file(), f"baseline missing: {NON_HF_BASELINE}"
    expected = json.loads(NON_HF_BASELINE.read_text(encoding="utf-8"))
    got_s = json.dumps(snap, ensure_ascii=False)
    exp_s = json.dumps(expected, ensure_ascii=False)
    assert snap == expected, f"non-halfframe DOM changed!\nGOT: {got_s}\nEXP: {exp_s}"


def test_non_halfframe_editor_dom_unchanged(
    settings: Any,
    lib: Library,
    volumes: Path,
    tmp_path: Path,
) -> None:
    """P2-3 + N3: 真实 a12-50 非半格图库的编辑页 DOM 与 ee33a95 基线逐字段一致。"""
    folder = samples.a12_50_samples()
    if not folder.is_dir():
        samples.missing("captures/a12-50 样本不在本机")
    dcim = volumes / "A12_50_CARD" / "DCIM" / "100RICOH"
    dcim.mkdir(parents=True)
    for src in sorted(folder.iterdir()):
        if src.is_file() and src.suffix.upper() in {".DNG", ".JPG", ".JPEG"}:
            shutil.copy2(src, dcim / src.name)
    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    lib.close()

    ui = _run_driver(settings, {"mode": "editor_snapshot"}, tmp_path)
    assert "error" not in ui, ui
    assert ui["pageErrors"] == [], ui["pageErrors"]
    snap = ui["steps"]["editorSnapshot"]

    if os.environ.get("GXR_UPDATE_NON_HF_A12_50_BASELINE") == "1":
        NON_HF_EDITOR_A12_50_BASELINE.write_text(json.dumps(snap, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert NON_HF_EDITOR_A12_50_BASELINE.is_file(), f"baseline missing: {NON_HF_EDITOR_A12_50_BASELINE}"
    expected = json.loads(NON_HF_EDITOR_A12_50_BASELINE.read_text(encoding="utf-8"))
    got_s = json.dumps(snap, ensure_ascii=False)
    exp_s = json.dumps(expected, ensure_ascii=False)
    assert snap == expected, f"non-halfframe a12-50 editor DOM changed!\nGOT: {got_s}\nEXP: {exp_s}"


def test_non_halfframe_editor_dom_unchanged_synthetic(
    settings: Any,
    lib: Library,
    card: Any,
    tmp_path: Path,
) -> None:
    """P2-3: 合成非半格图库的编辑页 DOM 与 ee33a95 基线逐字段一致（无真实样本时也能运行）。"""
    run_import(lib, find_cards((card.root.parent,))[0], ImportOptions(min_free_bytes=0))
    lib.close()

    ui = _run_driver(settings, {"mode": "editor_snapshot"}, tmp_path)
    assert "error" not in ui, ui
    assert ui["pageErrors"] == [], ui["pageErrors"]
    snap = ui["steps"]["editorSnapshot"]

    if os.environ.get("GXR_UPDATE_NON_HF_EDITOR_BASELINE") == "1":
        NON_HF_EDITOR_BASELINE.write_text(json.dumps(snap, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert NON_HF_EDITOR_BASELINE.is_file(), f"baseline missing: {NON_HF_EDITOR_BASELINE}"
    expected = json.loads(NON_HF_EDITOR_BASELINE.read_text(encoding="utf-8"))
    got_s = json.dumps(snap, ensure_ascii=False)
    exp_s = json.dumps(expected, ensure_ascii=False)
    assert snap == expected, f"non-halfframe editor DOM changed!\nGOT: {got_s}\nEXP: {exp_s}"
