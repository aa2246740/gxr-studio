"""手机端启动性能回归测试（browser_serial）。

覆盖 perf-startup 的核心验收要求（Service Worker 已拆至 studio/offline-sw）：
1. 冷启动在 Chromium 和 WebKit 下均只拉取 1 次 WASM；无默认校准且本地 IndexedDB 为空时，纯 IDB 恢复不触发 WASM 拉取。
2. 开始页在编辑器、导出分包与 `Engine.create()`（WebGPU 初始化）之前即可交互，默认校准拉取不阻塞首屏，且生产构建不包含 Service Worker。
3. 同一 Worker 单次 WASM 初始化失败后可恢复，Worker 池单槽崩溃时只拒绝该槽任务并驱逐重建。
4. 首次导入首张缩略图尚未绘制前，不得提前发起 `editor` 或 `export_ui` 分包请求；绘制后再在后台预热，进入编辑器后「导出选项」照常可用。
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

from conftest import REAL_HOME
from support.dng_writer import write_dng
from test_mobile_ui_browser import MODULES, PKG, RUNTIME, _need

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DIST = REPO / "mobile" / "dist"
DRIVER = REPO / "tests" / "support" / "mobile_perf_startup_browser.mjs"


def _prereq() -> None:
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need((DIST / "index.html").is_file(), "mobile/dist is missing; run make mobile-build")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")


def _run_driver(mode: str, manifest: dict[str, Any], tmp_path: Path, timeout: int = 180) -> dict[str, Any]:
    out_path = tmp_path / f"{mode}-report.json"
    manifest_path = tmp_path / f"{mode}-manifest.json"
    payload = {**manifest, "out": str(out_path)}
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(DRIVER), str(RUNTIME), mode, str(manifest_path)],
        cwd=REPO,
        env={**os.environ, "HOME": str(REAL_HOME)},
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert proc.returncode == 0, f"{mode} driver failed ({proc.returncode}): {proc.stderr[-1500:]}"
    assert out_path.is_file(), f"{mode} driver did not write report"
    data: dict[str, Any] = json.loads(out_path.read_text(encoding="utf-8"))
    return data


def _make_synthetic_photo_pair(dest: Path) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    dng = dest / "R9990001.DNG"
    jpg = dest / "R9990001.JPG"
    bayer = np.full((64, 96), 512, dtype=np.uint16)
    dng.write_bytes(write_dng(bayer))
    Image.new("RGB", (96, 64), (120, 140, 160)).save(jpg, format="JPEG", quality=85)
    return [str(dng), str(jpg)]


def test_cold_open_fetches_wasm_once_in_chromium_and_webkit(tmp_path: Path) -> None:
    """冷启动在 Chromium 和 WebKit 下都只拉取 1 次 WASM；无默认校准时 IDB 恢复不拉取 WASM。"""
    _prereq()
    report = _run_driver("wasm-once", {"distDir": str(DIST)}, tmp_path)

    for engine in ("chromium", "webkit"):
        if engine == "webkit" and report.get("webkitSkipped"):
            pytest.skip("Playwright WebKit is not installed")
        row = report[engine]
        assert row["normal"]["pageErrors"] == [], f"{engine} normal pageErrors: {row['normal']['pageErrors']}"
        assert row["normal"]["wasmRequests"] == 1, f"{engine} fetched WASM {row['normal']['wasmRequests']} times: {row['normal']['paths']}"
    for engine in ("chromium", "webkit"):
        row = report[engine]
        assert row["noCalib"]["pageErrors"] == [], f"{engine} noCalib pageErrors: {row['noCalib']['pageErrors']}"
        assert row["noCalib"]["wasmRequests"] == 0, f"{engine} fetched WASM without calib/IDB: {row['noCalib']['paths']}"


def test_start_screen_interactive_before_editor_chunk_and_engine_create(tmp_path: Path) -> None:
    """开始页在编辑器/导出分包与 Engine.create 运行前即可交互，且不包含 SW 构建或注册。"""
    _prereq()
    assert not (DIST / "sw.js").exists(), "mobile/dist/sw.js must not be generated on perf-startup branch"
    index_html = (DIST / "index.html").read_text(encoding="utf-8")
    assert "data-gxr-build" not in index_html, "SW-only data-gxr-build attribute must not be stamped into index.html"

    photos = _make_synthetic_photo_pair(tmp_path / "photos")
    report = _run_driver(
        "start-before-editor",
        {"distDir": str(DIST), "photoFiles": photos},
        tmp_path,
    )

    assert report["pageErrors"] == [], f"pageErrors: {report['pageErrors']}"
    assert report["swRegistrations"] == 0, f"unexpected service worker registrations: {report['swRegistrations']}"
    assert len(report["editorChunkFiles"]) >= 1, "production build must emit a separate editor chunk"
    assert len(report["exportChunkFiles"]) >= 1, "production build must emit a separate export_ui chunk"
    trace = report["trace"]
    assert trace["interactiveAt"] is not None, "start screen never became interactive"
    assert trace["gpuBeforeInteractive"] is False, f"Engine.create (requestAdapter) ran before start screen became interactive: {trace}"

    editor_paths = {f"/assets/{name}" for name in report["editorChunkFiles"]}
    export_paths = {f"/assets/{name}" for name in report["exportChunkFiles"]}
    before_resources = set(trace["resourcesAtInteractive"] or [])
    assert before_resources.isdisjoint(editor_paths), (
        f"editor chunk loaded before start screen became interactive: {before_resources & editor_paths}"
    )
    assert before_resources.isdisjoint(export_paths), (
        f"export_ui chunk loaded before start screen became interactive: {before_resources & export_paths}"
    )
    assert not any("gxr-default-calib.zip" in p for p in before_resources), (
        f"default calibration fetched before start screen interactive: {before_resources}"
    )

    after_resources = set(report["afterOpen"]["resourcesAfterOpen"]) | set(report["reqsAfterOpen"])
    assert not after_resources.isdisjoint(editor_paths), f"editor chunk did not load on opening photo: {after_resources}"
    assert report["afterOpen"]["screen"] == "editor"
    assert report["afterOpen"]["gpuRequestedAt"] is not None


def test_worker_recovers_after_wasm_init_failure_on_same_worker(tmp_path: Path) -> None:
    """单次 WASM 初始化失败后，同一 Worker 收到有效模块必须能恢复成功。"""
    _prereq()
    report = _run_driver(
        "worker-wasm-retry",
        {"distDir": str(DIST)},
        tmp_path,
        timeout=180,
    )
    assert report["firstOk"] is False, f"expected injected first failure, got {report}"
    assert report["secondOk"] is True, f"same worker stayed stuck after first WASM failure: {report}"


def test_pool_replaces_failed_worker_slot_after_onerror(tmp_path: Path) -> None:
    """Worker 槽位永久加载失败后必须拒绝该槽位在途请求并丢弃坏实例，下次请求新建 Worker 恢复。"""
    _prereq()
    report = _run_driver(
        "pool-slot-recovery",
        {"repoDir": str(REPO)},
        tmp_path,
        timeout=60,
    )
    single = report["singleSummary"]
    assert single["firstResult"] == "rejected"
    assert single["retryResult"] == "resolved", f"single-slot pool hung on failed worker: {single}"
    assert single["workersCreated"] == 2
    assert single["messagesToFailedWorker"] == 1
    assert single["failedWorkerTerminated"] is True
    iso = report["isolationSummary"]
    assert iso["slot0Result"] == "rejected"
    assert iso["slot1Result"] == "resolved", f"slot 0 failure broke healthy slot 1: {iso}"


def test_first_thumb_does_not_prewarm_editor_or_export_ui_during_import(tmp_path: Path) -> None:
    """首次导入首张缩略图尚未画出前，不得提前发起 editor 或 export_ui 分包请求，且绘制后可正常预热与打开导出选项。"""
    _prereq()
    photos = _make_synthetic_photo_pair(tmp_path / "thumb_prewarm_photos")
    report = _run_driver(
        "import-before-prewarm",
        {"distDir": str(DIST), "photoFiles": photos},
        tmp_path,
        timeout=180,
    )
    assert report["pageErrors"] == [], f"pageErrors: {report['pageErrors']}"
    order = report["order"]
    assert order["editor"] is not None, f"editor chunk never prewarmed after first thumb: {order}"
    assert order["exporter"] is not None, f"export_ui chunk never prewarmed after first thumb: {order}"
    assert order["editorBegunBeforeDraw"] is False, f"editor request started before first thumb drew: {order}"
    assert order["editorCompletedBeforeDraw"] is False, f"editor completed before first thumb drew: {order}"
    assert order["exporterBegunBeforeDraw"] is False, f"export_ui request started before first thumb drew: {order}"
    assert order["exporterCompletedBeforeDraw"] is False, f"export_ui completed before first thumb drew: {order}"
    if order["offImage"] is not None:
        assert order["offImageBegunBeforeDraw"] is False, f"off_image_sheet started before first thumb drew: {order}"
    assert report["exportOptionsOpened"] is True, "export options sheet failed to open from editor more menu"
