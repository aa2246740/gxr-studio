"""无头真浏览器验收：在桌面编辑器里编辑 card250 半格一对（第 2 张），覆盖 gpu_preview 开（默认）与关两种模式。

验证：
- 配方卡片全部可点、点选后保存并重新渲染双联合成图；
- Log LUT 与显示类 LUT 均可在编辑器里保存；
- 曝光与曲线调整可保存；
- 刷新页面后，舞台画面、配方状态、以及编辑后缩略图都是带编辑的合成图；
- 导出对话框默认导出即合成图；
- 整个过程中舞台与面板从未提示「没有 RAW / 相机 JPG」或请求 `/camera-jpg`。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import uvicorn
from fastapi.testclient import TestClient
from PIL import Image

from conftest import PORT, REAL_HOME
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
from test_halfframe_p1a_field import _index_shots_by_jpg_sha, _seam_core
from test_lut import cube_bytes
from test_mobile_ui_browser import RUNTIME, _need

pytestmark = pytest.mark.browser_serial

DRIVER = Path(__file__).resolve().parent / "support" / "desktop_halfframe_p1a_editor.mjs"
FORBIDDEN_COPY = ("没有 RAW", "需要 RAW", "没配上第 1 张", "相机 JPG", "no RAW")


def _assert_no_forbidden_copy(panel: dict[str, Any], where: str) -> None:
    texts = [str(panel.get("stageLabels") or ""), *(str(x) for x in panel.get("notices", []))]
    joined = " | ".join(texts)
    for bad in FORBIDDEN_COPY:
        assert bad not in joined, f"{where} showed forbidden copy {bad!r}: {joined!r}"


def _load_rgb(path: Path) -> np.ndarray:
    with Image.open(path) as im:
        return np.asarray(im.convert("RGB"))


def _resize_like(arr: np.ndarray, target: np.ndarray) -> np.ndarray:
    if arr.shape[:2] == target.shape[:2]:
        return arr
    im = Image.fromarray(arr).resize((target.shape[1], target.shape[0]), Image.Resampling.BILINEAR)
    return np.asarray(im)


def _assert_composite_changed(before: np.ndarray, after: np.ndarray, label: str, *, max_seam: int = 5) -> None:
    ha1, core1, hb1 = _seam_core(after)
    assert float(core1.mean()) <= 2.0 and int(core1.max()) <= max_seam, (
        f"{label}: seam core not black (mean={float(core1.mean()):.2f}, max={int(core1.max())})"
    )
    before_fit = _resize_like(before, after)
    ha0, _core0, hb0 = _seam_core(before_fit)
    diff_a = float(np.abs(ha1.astype(np.float32) - ha0.astype(np.float32)).mean())
    diff_b = float(np.abs(hb1.astype(np.float32) - hb0.astype(np.float32)).mean())
    assert diff_a > 1.5 and diff_b > 1.5, f"{label}: composite did not change across both halves ({diff_a:.2f}, {diff_b:.2f})"


@pytest.mark.parametrize("gpu", [True, False], ids=["gpu_on", "gpu_off"])
def test_desktop_editor_halfframe_pair(
    gpu: bool,
    settings: Any,
    lib: Library,
    volumes: Path,
    tmp_path: Path,
) -> None:
    _need(shutil.which("node") is not None, "node is required for desktop editor browser test")
    _need(RUNTIME.is_file(), f"pinned Playwright runtime missing: {RUNTIME}")

    _clear_desktop_cache()
    pairs = _sorted_pairs(_field_folder("card250"))
    _stage_volume_pairs(volumes, pairs[:2])
    _setup_g250_calibrated_home(settings)
    if not gpu:
        (settings.home_dir / "flags.json").write_text(json.dumps({"gpu_preview": False}), encoding="utf-8")

    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    by_idx = _index_shots_by_jpg_sha(lib, pairs[:2])
    p2 = by_idx[2]["shot_id"]
    assert by_idx[2].get("pair", {}).get("role") == "second"
    lib.close()

    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        assert client.get("/api/settings").json()["gpu_preview"] is gpu
        lut_ids: dict[str, str] = {}
        for name, space, fn in (
            ("log", "llog", lambda x: np.clip(x * np.array([1.12, 0.88, 0.78]) + 0.03, 0.0, 1.0)),
            ("disp", "srgb", lambda x: np.clip(x * np.array([0.82, 0.94, 1.12]), 0.0, 1.0)),
        ):
            up = client.post(
                "/api/lut/upload",
                content=cube_bytes(fn, size=9),
                headers={"X-GXR-Filename": f"editor-{name}.cube"},
            )
            assert up.status_code == 200, up.text
            lut_ids[name] = up.json()["cube_id"]
            assert client.patch(f"/api/luts/{lut_ids[name]}", json={"input_space": space}).status_code == 200

        base_render = client.get(f"/api/photos/{p2}/render?stage=final")
        assert base_render.status_code == 200, base_render.text
        with Image.open(io.BytesIO(base_render.content)) as im:
            untouched_composite = np.asarray(im.convert("RGB"))
        _, untouched_core, _ = _seam_core(untouched_composite)
        assert int(untouched_core.max()) <= 3

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

        out_path = tmp_path / "editor_out.json"
        stage_initial_path = tmp_path / "stage_initial.png"
        stage_after_recipe_path = tmp_path / "stage_after_recipe.png"
        stage_after_reload_path = tmp_path / "stage_after_reload.png"
        manifest = tmp_path / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "base": f"http://127.0.0.1:{port}",
                    "token": settings.token,
                    "p2": p2,
                    "logLut": lut_ids["log"],
                    "dispLut": lut_ids["disp"],
                    "gpu": gpu,
                    "out": str(out_path),
                    "stageInitialPath": str(stage_initial_path),
                    "stageAfterRecipePath": str(stage_after_recipe_path),
                    "stageAfterReloadPath": str(stage_after_reload_path),
                }
            ),
            encoding="utf-8",
        )
        proc = subprocess.run(
            ["node", str(DRIVER), str(RUNTIME), str(manifest)],
            cwd=str(DRIVER.parent),
            env={**os.environ, "HOME": str(REAL_HOME)},
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        assert proc.returncode == 0, f"driver failed (rc={proc.returncode}): {proc.stderr[-2000:]}"
        assert out_path.is_file(), proc.stderr[-2000:]
        ui = json.loads(out_path.read_text(encoding="utf-8"))
        assert "error" not in ui, ui
        race = ui["steps"]["startupRace"]
        assert race["cards"] > 0 and race["earlyDisabled"] > 0, f"startup race not reproduced: {race}"
        assert race["enabledAfterStatus"] is True, f"recipe cards stayed disabled after the camera-data status arrived: {race}"
        guard = ui["steps"]["midDragGuard"]
        want = {"kept": True, "deferred": True, "flushed": True, "rebuilt": True}
        assert guard == want, f"camera-data redraw broke a change in progress (W01): {guard}"
        other = ui["steps"]["otherTabGuard"]
        assert other == {"untouched": True, "dirty": True, "rebuiltOnShow": True}, f"camera-data redraw touched another tab: {other}"
        stamp = ui["steps"]["stampTabGuard"]
        assert stamp == {"same": True, "dirty": True}, f"camera-data redraw rebuilt the stamp pane after a change: {stamp}"
        focus = ui["steps"]["focusGuard"]
        assert focus == {"rebuiltOnShow": True}, f"recipe pane stayed stale after focus elsewhere: {focus}"
        assert ui["pageErrors"] == [], ui["pageErrors"]
        assert ui["cameraJpgReqs"] == [], ui["cameraJpgReqs"]
        assert ui["steps"]["settings"]["gpu_preview"] is gpu
        for gr in ui["gpuResponses"]:
            assert gr["status"] == 200 and gr["body"] == {"enabled": False}, gr

        # 初始：配方卡片可点、不灰显、未提示缺 RAW / 相机 JPG、GPU 舞台未激活
        panel0 = ui["steps"]["initialPanel"]
        assert panel0["cards"] > 0 and panel0["disabled"] == 0 and panel0["dimmed"] == 0, panel0
        assert panel0["gpuActive"] is False, panel0
        assert lut_ids["log"] not in panel0["lutDisabled"], panel0
        _assert_no_forbidden_copy(panel0, "initial")
        for r in ui["steps"]["initialRenders"]:
            assert r["label"] != "camera-jpg", r

        # 1) 点选配方卡片 -> PUT 200 + 重新渲染双联合成图
        rp = ui["steps"]["recipePick"]
        assert rp["pick"] is not None and rp["pick"]["disabled"] is False, rp
        assert any(p["status"] == 200 for p in rp["puts"]), rp
        assert rp["stored"] is not None, rp
        assert any(r["status"] == 200 and r["pred"] == "1" and r["label"] != "camera-jpg" for r in rp["renders"]), rp
        _assert_no_forbidden_copy(rp["panel"], "after recipe pick")
        _assert_composite_changed(untouched_composite, _load_rgb(stage_after_recipe_path), "stage after recipe")

        # 2) Log LUT 与显示类 LUT 均成功保存
        for kind, expected_id in (("log", lut_ids["log"]), ("display", lut_ids["disp"])):
            step_lut = ui["steps"][f"lut_{kind}"]
            assert step_lut["res"] == "changed", step_lut
            assert any(p["status"] == 200 for p in step_lut["puts"]), step_lut
            assert step_lut["stored"] == expected_id, step_lut

        # 3) 曝光与曲线保存成功
        exp_step = ui["steps"]["exposure"]
        assert any(p["status"] == 200 for p in exp_step["puts"]), exp_step
        assert exp_step["stored"] is not None and float(exp_step["stored"].get("exposure_ev", 0)) > 0, exp_step

        curve_step = ui["steps"]["curve"]
        assert curve_step["box"] is True, curve_step
        assert any(p["status"] == 200 for p in curve_step["puts"]), curve_step
        assert curve_step["stored"] is not None, curve_step

        # 4) 刷新后：舞台、配方状态、以及编辑后缩略图均为合成图
        ar = ui["steps"]["afterReload"]
        assert ar["edit"]["recipe"] == rp["stored"], ar["edit"]
        assert ar["edit"]["lut"] == lut_ids["disp"], ar["edit"]
        assert ar["panel"]["cards"] > 0 and ar["panel"]["disabled"] == 0, ar["panel"]
        assert rp["pick"]["look"] in ar["panel"]["pressed"], (rp["pick"], ar["panel"])
        assert ar["panel"]["gpuActive"] is False, ar["panel"]
        _assert_no_forbidden_copy(ar["panel"], "after reload")
        assert any(r["status"] == 200 and r["pred"] == "1" and r["label"] != "camera-jpg" for r in ar["renders"]), ar["renders"]
        _assert_composite_changed(untouched_composite, _load_rgb(stage_after_reload_path), "stage after reload")

        # 5) 对话框默认导出即合成图
        ex_step = ui["steps"]["export"]
        assert ex_step["clicked"] is True and len(ex_step["results"]) >= 1, ex_step
        ex_res = ex_step["results"][0]
        assert ex_res["status"] == 200, ex_res
        exported_path = Path(ex_res["body"]["path"])
        assert exported_path.is_file(), ex_res
        _assert_composite_changed(untouched_composite, _load_rgb(exported_path), "default UI export")
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        sock.close()

    # 刷新后编辑缩略图 API 也必须是带编辑的合成图
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        th = client.get(f"/api/photos/{p2}/thumb")
        assert th.status_code == 200, th.text
        with Image.open(io.BytesIO(th.content)) as im:
            thumb_arr = np.asarray(im.convert("RGB"))
        _assert_composite_changed(untouched_composite, thumb_arr, "edited thumbnail after reload", max_seam=10)
