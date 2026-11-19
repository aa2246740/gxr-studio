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


def _run_driver(settings: Any, manifest: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    _need(shutil.which("node") is not None, "node is required for desktop browser test")
    _need(RUNTIME.is_file(), f"pinned Playwright runtime missing: {RUNTIME}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(settings.with_(port=port, web_dir=WEB)), log_level="warning"))
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
    assert ui["steps"]["filter"] == {"filteredCount": 3, "restoredCount": 8}, ui["steps"]["filter"]

    # 2. Selection + batch export -> 1 diptych file by default
    sb = ui["steps"]["selectionBatchExport"]
    assert sb["dialog"]["hasHfRow"] is True and sb["dialog"]["pressedMode"] == "双联一张", sb
    assert sb["batchReqs"] and sb["batchReqs"][0]["status"] == 202 and sb["batchReqs"][0]["halfframe"] == "diptych", sb
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
    assert ed["infoTab"] == {"hasFirstLabel": True, "hasSecondLabel": True, "hasBothNames": True}, ed["infoTab"]

    vb = ed["viewbar"]
    assert vb["firstView"]["pressed"] == "第 1 张", vb
    assert vb["firstView"]["cardDisabled"] is True and vb["firstView"]["selectDisabled"] is True, vb
    assert vb["putsDuringSingleViews"] == 0, vb
    assert vb["diffFirstSecond"] > 3.0, vb
    assert vb["backToDiptych"]["pressed"] == "双联" and vb["backToDiptych"]["cardDisabled"] is False, vb
    assert ed["nav"]["landedOnFirst"] is False, ed["nav"]

    # 4. Export dialog
    ex = ui["steps"]["exportDialog"]
    assert ex["exportDefault"]["title"] == "导出 1 项（半格一对）", ex["exportDefault"]
    assert ex["exportDefault"]["hasHfRow"] is True and ex["exportDefault"]["pressedMode"] == "双联一张", ex["exportDefault"]
    assert "1 个文件" in ex["exportDefault"]["sizeText"], ex["exportDefault"]
    assert ex["exportDefault"]["hdrDisabled"] is True and "半格一对暂时做不了 HDR" in ex["exportDefault"]["hdrText"], ex["exportDefault"]
    assert "2 个文件" in ex["exportSingles"]["sizeText"] and "2088" in ex["exportSingles"]["sizeText"], ex["exportSingles"]
    assert "3 个文件" in ex["exportBoth"]["sizeText"], ex["exportBoth"]
    assert ex["singlesExportReqs"] and ex["singlesExportReqs"][0]["status"] == 200 and ex["singlesExportReqs"][0]["fileCount"] == 2, ex
    exported_after_singles = [p for p in settings.export_dir.rglob("*") if p.is_file()]
    assert len(exported_after_singles) == 3, f"expected 1 + 2 = 3 exported files total, got {len(exported_after_singles)}"
    assert ex["exportReopened"]["pressedMode"] == "两张单格", ex["exportReopened"]
    assert ex["exportXpan"]["hasHfRow"] is False, ex["exportXpan"]

    # 5. Crop preset 「取半格」 on half_first
    cr = ui["steps"]["cropHalfFirst"]
    assert cr["xpanHasHalfCrop"] is False, cr
    assert cr["cropPressed"] is True, cr
    rect = cr["crop"]["rect"]
    px_w = round(float(rect[2]) * hf_w)
    px_h = round(float(rect[3]) * hf_h)
    assert px_w == 2088 or px_h == 2088, f"expected 2088px half-frame crop, got {px_w}x{px_h} from {rect}"


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
