"""手机端启动性能与 Service Worker 回归测试（browser_serial）。

覆盖 perf-startup 的三项验收要求：
1. 冷启动在 Chromium 和 WebKit 下均只拉取 1 次 WASM；无默认校准且本地 IndexedDB 为空时，纯 IDB 恢复不触发 WASM 拉取。
2. 开始页在编辑器分包与 `Engine.create()`（WebGPU 初始化）之前即可交互，默认校准拉取不阻塞首屏。
3. 生产构建的 Service Worker 在测试环境默认关闭，显式开启后：
   - 二次打开从缓存提供静态产物且支持离线打开；
   - 绝不缓存用户文件或非白名单路径；
   - 发布新版本后下次打开自动使用新代码并清理旧缓存且无弹窗提示。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import zipfile
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
DRIVER_R2 = REPO / "tests" / "support" / "mobile_perf_startup_r2_browser.mjs"


def _prereq() -> None:
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need((DIST / "index.html").is_file(), "mobile/dist is missing; run make mobile-build")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")


def _run_driver(
    mode: str,
    manifest: dict[str, Any],
    tmp_path: Path,
    timeout: int = 180,
    driver: Path = DRIVER,
) -> dict[str, Any]:
    out_path = tmp_path / f"{mode}-report.json"
    manifest_path = tmp_path / f"{mode}-manifest.json"
    payload = {**manifest, "out": str(out_path)}
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(driver), str(RUNTIME), mode, str(manifest_path)],
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


def _build_real_v2_dist(tmp_path: Path) -> Path:
    """在临时目录中真实运行一次 Vite 生产构建，改变 grid 分包哈希、HTML 标记与默认校准包 SHA。"""
    v2_root = tmp_path / "v2_repo"
    v2_mobile = v2_root / "mobile"
    v2_mobile.mkdir(parents=True, exist_ok=True)
    for rel in ("src", "public"):
        shutil.copytree(REPO / "mobile" / rel, v2_mobile / rel)
    for name in ("index.html", "dev.html", "package.json", "tsconfig.json", "vite.config.ts"):
        shutil.copy2(REPO / "mobile" / name, v2_mobile / name)
    (v2_mobile / "node_modules").symlink_to(REPO / "mobile" / "node_modules", target_is_directory=True)
    (v2_root / "frontend").symlink_to(REPO / "frontend", target_is_directory=True)
    (v2_root / "src").symlink_to(REPO / "src", target_is_directory=True)

    grid_ts = v2_mobile / "src" / "app" / "grid.ts"
    grid_ts.write_text(grid_ts.read_text(encoding="utf-8") + "\nvoid 'sw-test-v2-grid';\n", encoding="utf-8")

    index_html = v2_mobile / "index.html"
    index_html.write_text(
        index_html.read_text(encoding="utf-8").replace("<html ", '<html data-sw-test-build="v2" ', 1),
        encoding="utf-8",
    )

    pack = v2_mobile / "public" / "calib" / "gxr-default-calib.zip"
    old_sha = hashlib.sha256(pack.read_bytes()).hexdigest()
    with zipfile.ZipFile(pack, "a") as zf:
        zf.comment = b"sw-test-v2-calib"
    new_sha = hashlib.sha256(pack.read_bytes()).hexdigest()
    assert new_sha != old_sha
    calib_ts = v2_mobile / "src" / "lib" / "default_calib.ts"
    calib_src = calib_ts.read_text(encoding="utf-8")
    assert old_sha in calib_src
    calib_ts.write_text(calib_src.replace(old_sha, new_sha), encoding="utf-8")

    vite_bin = REPO / "mobile" / "node_modules" / "vite" / "bin" / "vite.js"
    proc = subprocess.run(
        ["node", str(vite_bin), "build"],
        cwd=v2_mobile,
        env={**os.environ, "HOME": str(REAL_HOME)},
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"v2 vite build failed: {proc.stdout[-800:]}\n{proc.stderr[-800:]}"
    return v2_mobile / "dist"


def _make_synthetic_photo_pair(dest: Path) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    dng = dest / "R9990001.DNG"
    jpg = dest / "R9990001.JPG"
    bayer = np.full((64, 96), 512, dtype=np.uint16)
    dng.write_bytes(write_dng(bayer))
    Image.new("RGB", (96, 64), (120, 140, 160)).save(jpg, format="JPEG", quality=85)
    return [str(dng), str(jpg)]


def _make_synthetic_lut(dest: Path) -> str:
    lut = dest / "synth_test.cube"
    lut.write_text(
        '# Input: sRGB\nTITLE "Synth"\nLUT_3D_SIZE 2\n0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n',
        encoding="utf-8",
    )
    return str(lut)


def _build_v2_dist(src_dist: Path, dst_dist: Path) -> None:
    """复制 dist 并模拟新构建版本：新 HTML 标记 + 新缓存版本号的 sw.js 与 data-gxr-build。"""
    shutil.copytree(src_dist, dst_dist)
    index_path = dst_dist / "index.html"
    html = index_path.read_text(encoding="utf-8")
    html = html.replace("<html ", '<html data-sw-test-build="v2" ', 1)
    html = re.sub(r"(gxr-studio-v1-[0-9a-f]+)", r"\1-v2", html, count=1)
    index_path.write_text(html, encoding="utf-8")
    sw_path = dst_dist / "sw.js"
    if sw_path.is_file():
        sw_text = sw_path.read_text(encoding="utf-8")
        replaced = re.sub(
            r"(gxr-studio-v1-[0-9a-f]+)",
            r"\1-v2",
            sw_text,
            count=1,
        )
        assert replaced != sw_text, "sw.js missing versioned cache key"
        sw_path.write_text(replaced, encoding="utf-8")


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
    """开始页在编辑器分包与 Engine.create 运行前即可交互，默认校准不阻塞首屏。"""
    _prereq()
    photos = _make_synthetic_photo_pair(tmp_path / "photos")
    report = _run_driver(
        "start-before-editor",
        {"distDir": str(DIST), "photoFiles": photos},
        tmp_path,
    )

    assert report["pageErrors"] == [], f"pageErrors: {report['pageErrors']}"
    assert len(report["editorChunkFiles"]) >= 1, "production build must emit a separate editor chunk"
    trace = report["trace"]
    assert trace["interactiveAt"] is not None, "start screen never became interactive"
    assert trace["gpuBeforeInteractive"] is False, f"Engine.create (requestAdapter) ran before start screen became interactive: {trace}"

    editor_paths = {f"/assets/{name}" for name in report["editorChunkFiles"]}
    before_resources = set(trace["resourcesAtInteractive"] or [])
    assert before_resources.isdisjoint(editor_paths), (
        f"editor chunk loaded before start screen became interactive: {before_resources & editor_paths}"
    )
    assert not any("gxr-default-calib.zip" in p for p in before_resources), (
        f"default calibration fetched before start screen interactive: {before_resources}"
    )

    after_resources = set(report["afterOpen"]["resourcesAfterOpen"]) | set(report["reqsAfterOpen"])
    assert not after_resources.isdisjoint(editor_paths), f"editor chunk did not load on opening photo: {after_resources}"
    assert report["afterOpen"]["screen"] == "editor"
    assert report["afterOpen"]["gpuRequestedAt"] is not None


def test_service_worker_cache_privacy_and_version_upgrade(tmp_path: Path) -> None:
    """SW 测试环境默认关闭；开启后二次打开命中缓存、绝不缓存用户文件、新构建替换旧缓存。"""
    _prereq()
    sw_file = DIST / "sw.js"
    assert sw_file.is_file(), "mobile/dist/sw.js was not generated by build"
    sw_text = sw_file.read_text(encoding="utf-8")
    entry_match = re.search(r"const ENTRY_SCRIPT = '(assets/main-[^']+\.js)';", sw_text)
    assert entry_match is not None, "dist/sw.js missing valid ENTRY_SCRIPT"
    assert (DIST / entry_match.group(1)).is_file(), f"ENTRY_SCRIPT does not exist: {entry_match.group(1)}"
    v1_dir = tmp_path / "dist_v1"
    v2_dir = tmp_path / "dist_v2"
    shutil.copytree(DIST, v1_dir)
    _build_v2_dist(DIST, v2_dir)

    photo_files = _make_synthetic_photo_pair(tmp_path / "user_files")
    lut_file = _make_synthetic_lut(tmp_path / "user_files")

    report = _run_driver(
        "service-worker",
        {
            "distV1": str(v1_dir),
            "distV2": str(v2_dir),
            "userFiles": photo_files,
            "lutFile": lut_file,
        },
        tmp_path,
        timeout=240,
    )

    assert report["pageErrors"] == [], f"pageErrors: {report['pageErrors']}"
    assert report["defaultRegs"] == 0, "SW must stay disabled in automated tests unless opted in"
    assert report["swRegistered"] is True, "SW failed to register when opted in via ?sw=1"
    assert report["load2Controlled"] is True, "second load must be controlled by SW"

    # 二次加载时，已缓存的 /assets/* 与 /calib/gxr-default-calib.zip 不再发往服务器
    load2_paths = report["load2Paths"]
    assert not any(p.startswith("/assets/") for p in load2_paths), f"second load still fetched assets from network: {load2_paths}"
    assert not any(p.startswith("/calib/") for p in load2_paths), f"second load still fetched calib from network: {load2_paths}"
    assert report["offlineStartVisible"] is True, "offline reload failed to render start screen from SW cache"

    # 严格审查缓存条目：绝不能包含用户文件、blob/data 或非白名单路径
    v1_caches = report["cacheAuditV1"]
    assert len(v1_caches["names"]) == 1, f"expected 1 versioned cache, got {v1_caches['names']}"
    v1_name = v1_caches["names"][0]
    cached_urls = v1_caches["entries"][v1_name]
    assert len(cached_urls) >= 3, f"expected cached app assets, got {cached_urls}"
    forbidden_substrings = (".dng", ".jpg", ".dat", ".bin", ".cube", "blob:", "data:", "/other/")
    for url in cached_urls:
        low = url.lower()
        assert not any(bad in low for bad in forbidden_substrings), f"forbidden entry cached by SW: {url}"
        assert low.endswith("/") or "/assets/" in low or "/calib/gxr-default-calib.zip?v=" in low, f"unexpected URL cached by SW: {url}"

    # 新构建版本生效：无弹窗，下次打开即用 v2，且旧缓存被删除
    assert report["dialogs"] == [], f"SW update must not show any prompt: {report['dialogs']}"
    assert report["v2Marker"] == "v2", f"next load did not serve new build HTML: {report['v2Marker']}"
    v2_names = report["cacheAuditV2"]["names"]
    assert v1_name not in v2_names, f"old cache {v1_name} was not deleted on activate: {v2_names}"
    assert len(v2_names) == 1, f"expected 1 new versioned cache after upgrade, got {v2_names}"


def test_sw_offline_first_visit_import_edit_export(tmp_path: Path) -> None:
    """在线访问一次开始页后断网，必须能完整走完导入 -> 编辑 -> 导出；关键预缓存失败时安装必须中止。"""
    _prereq()
    photos = _make_synthetic_photo_pair(tmp_path / "offline_photos")
    report = _run_driver(
        "offline-full-flow",
        {"distDir": str(DIST), "photoFiles": photos},
        tmp_path,
        timeout=240,
        driver=DRIVER_R2,
    )
    assert report["pageErrors"] == [], f"pageErrors: {report['pageErrors']}"
    assert report["failedInstallState"]["hasActive"] is False, (
        f"SW activated despite critical precache 500 error: {report['failedInstallState']}"
    )
    assert report["failedInstallState"]["controlled"] is False
    precached = report["precachedEntries"]
    for prefix in (
        "/assets/grid-",
        "/assets/editor-",
        "/assets/export_ui-",
        "/assets/export_worker-",
        "/assets/thumb-",
        "/assets/worker-",
        "/assets/gxr_wasm_bg-",
    ):
        assert any(entry.startswith(prefix) for entry in precached), f"critical chunk/worker {prefix} missing from precache: {precached}"
    assert report["thumbDrawn"] is True, f"offline first import failed: {report['offlineNetAttempts']}"
    assert report["stageReady"] is True, "offline editor stage failed to render"
    assert report["exportReady"] is True, "offline export failed to complete"


def test_sw_open_old_tab_survives_v2_deploy_with_new_chunk_hashes(tmp_path: Path) -> None:
    """部署真实 V2 后，仍打开的 V1 标签页首次导入与进编辑不能 404；关闭 V1 后再清理旧缓存。"""
    _prereq()
    v2_dist = _build_real_v2_dist(tmp_path)
    photos = _make_synthetic_photo_pair(tmp_path / "old_tab_photos")
    report = _run_driver(
        "open-old-tab",
        {"distV1": str(DIST), "distV2": str(v2_dist), "photoFiles": photos},
        tmp_path,
        timeout=240,
        driver=DRIVER_R2,
    )
    assert report["pageErrors"] == [], f"pageErrors: {report['pageErrors']}"
    assert report["gridA"] != report["gridB"], "real V2 build must produce a different grid chunk hash"
    assert report["cacheA"] != report["cacheB"]
    assert report["newDocBuild"] == "v2"
    assert report["oldDocBuild"] is None
    assert report["oldTabThumb"] is True, f"V1 tab failed to import after V2 deploy: {report}"
    assert report["oldTabEditor"] is True, f"V1 tab failed to open editor after V2 deploy: {report}"
    assert report["oldGrid404"] is False, f"V1 tab hit 404 on old grid chunk: {report}"
    assert report["cacheB"] in report["cachesAfterOldClosed"]
    assert report["cacheA"] not in report["cachesAfterOldClosed"], (
        f"V1 cache lingered after V1 tab closed and V2 reloaded: {report['cachesAfterOldClosed']}"
    )


def test_sw_versioned_default_calib_zip_updates_across_builds(tmp_path: Path) -> None:
    """更新默认校准包与 SHA 后，V2 在 SW 切换前后都必须拿到 V2 ZIP 且通过应用层核对。"""
    _prereq()
    v2_dist = _build_real_v2_dist(tmp_path)
    photos = _make_synthetic_photo_pair(tmp_path / "calib_photos")
    report = _run_driver(
        "calib-update",
        {"distV1": str(DIST), "distV2": str(v2_dist), "photoFiles": photos},
        tmp_path,
        timeout=240,
        driver=DRIVER_R2,
    )
    assert report["pageErrors"] == [], f"pageErrors: {report['pageErrors']}"
    assert report["hashA"] != report["hashB"]
    assert report["v1Sha"] == report["hashA"]
    assert report["shaBeforeSwSwitch"] == report["hashB"], (
        f"V2 received stale calib before SW switch: got {report['shaBeforeSwSwitch'][:12]}, expected {report['hashB'][:12]}"
    )
    assert report["calibFailedBefore"] == 0, "V2 default calibration SHA check failed in UI"
    assert report["shaAfterSwSwitch"] == report["hashB"], (
        f"V2 received stale calib after SW switch: got {report['shaAfterSwSwitch'][:12]}, expected {report['hashB'][:12]}"
    )


def test_sw_index_html_redirect_to_root_online_and_offline(tmp_path: Path) -> None:
    """服务端将 /index.html 跳转到 / 时，在线与离线导航均不得触发 redirected 响应错误。"""
    _prereq()
    report = _run_driver(
        "index-redirect",
        {"distDir": str(DIST)},
        tmp_path,
        timeout=180,
        driver=DRIVER_R2,
    )
    assert report["pageErrors"] == [], f"pageErrors: {report['pageErrors']}"
    assert report["cacheInspect"]["rootCached"] is True, f"scope root HTML not cached: {report['cacheInspect']}"
    assert report["cacheInspect"]["rootRedirected"] is False, f"cached root HTML must not be redirected: {report['cacheInspect']}"
    assert report["cacheInspect"]["indexCachedRedirected"] is not True, (
        f"must never cache a redirected /index.html response: {report['cacheInspect']}"
    )
    assert report["onlineIndexResult"]["ok"] is True, f"online /index.html failed: {report['onlineIndexResult']}"
    assert report["onlineIndexResult"]["pathname"] == "/"
    assert report["offlineRootResult"]["ok"] is True, f"offline / failed: {report['offlineRootResult']}"
    assert report["offlineIndexResult"]["ok"] is True, f"offline /index.html failed: {report['offlineIndexResult']}"


def test_worker_recovers_after_wasm_init_failure_on_same_worker(tmp_path: Path) -> None:
    """单次 WASM 初始化失败后，同一 Worker 收到有效模块必须能恢复成功。"""
    _prereq()
    report = _run_driver(
        "worker-wasm-retry",
        {"distDir": str(DIST)},
        tmp_path,
        timeout=180,
        driver=DRIVER_R2,
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
        driver=DRIVER_R2,
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
