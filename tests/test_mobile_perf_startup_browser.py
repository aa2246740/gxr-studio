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

import json
import os
import re
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


def _make_synthetic_lut(dest: Path) -> str:
    lut = dest / "synth_test.cube"
    lut.write_text(
        '# Input: sRGB\nTITLE "Synth"\nLUT_3D_SIZE 2\n0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n',
        encoding="utf-8",
    )
    return str(lut)


def _build_v2_dist(src_dist: Path, dst_dist: Path) -> None:
    """复制 dist 并模拟新构建版本：新 HTML 标记 + 新缓存版本号的 sw.js。"""
    shutil.copytree(src_dist, dst_dist)
    index_path = dst_dist / "index.html"
    html = index_path.read_text(encoding="utf-8")
    html = html.replace("<html ", '<html data-sw-test-build="v2" ', 1)
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
        assert low.endswith("/index.html") or "/assets/" in low or "/calib/gxr-default-calib.zip" in low, f"unexpected URL cached by SW: {url}"

    # 新构建版本生效：无弹窗，下次打开即用 v2，且旧缓存被删除
    assert report["dialogs"] == [], f"SW update must not show any prompt: {report['dialogs']}"
    assert report["v2Marker"] == "v2", f"next load did not serve new build HTML: {report['v2Marker']}"
    v2_names = report["cacheAuditV2"]["names"]
    assert v1_name not in v2_names, f"old cache {v1_name} was not deleted on activate: {v2_names}"
    assert len(v2_names) == 1, f"expected 1 new versioned cache after upgrade, got {v2_names}"
