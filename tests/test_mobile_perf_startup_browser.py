"""手机端启动性能回归测试（browser_serial，生产 mobile/dist）。

1. 冷启动在 Chromium 和 WebKit 下只拉取 1 次 WASM；无默认校准且 IndexedDB 为空时不拉取。
2. 开始页在编辑器分包和 `Engine.create()` 之前就能点；首屏之后只有网格和编辑器两个懒加载分包，
   分包只有 JS、只依赖主包；样式随首屏一次加载。
3. Worker 单次 WASM 初始化失败后能恢复；池里一个槽坏了只拒绝那个槽，下次新建；WASM 请求先找空闲槽。
4. 第一张缩略图画出来之前不预取编辑器分包；只有 RAW 时落定空闲后才预取。
5. 两个分包加载失败都有「重试」和「刷新页面」，重试重新请求并恢复，恢复后只有一份模块；切屏、换照片、
   清空以后，晚到的分包不会打开旧照片、弹旧面板或旧错误。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
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
LAZY = REPO / "tests" / "support" / "mobile_perf_startup_lazy_browser.mjs"


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
    assert len(report["editorChunkFiles"]) == 1, f"production build must emit one editor chunk: {report['editorChunkFiles']}"
    assert len(report["gridChunkFiles"]) == 1, f"production build must emit one grid chunk: {report['gridChunkFiles']}"
    trace = report["trace"]
    assert trace["interactiveAt"] is not None, "start screen never became interactive"
    assert trace["gpuBeforeInteractive"] is False, f"Engine.create (requestAdapter) ran before start screen became interactive: {trace}"

    editor_paths = {f"/assets/{name}" for name in report["editorChunkFiles"]}
    grid_paths = {f"/assets/{name}" for name in report["gridChunkFiles"]}
    before_resources = set(trace["resourcesAtInteractive"] or [])
    assert before_resources.isdisjoint(editor_paths | grid_paths), (
        f"lazy chunk loaded before start screen became interactive: {before_resources & (editor_paths | grid_paths)}"
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


def test_first_thumb_does_not_prewarm_editor_during_import(tmp_path: Path) -> None:
    """第一张缩略图画出来之前不请求编辑器分包；画出来以后预取一次，进编辑器、开导出选项都不再请求分包。"""
    _prereq()
    photos = _make_synthetic_photo_pair(tmp_path / "thumb_prewarm_photos")
    report = _run_driver("import-before-prewarm", {"distDir": str(DIST), "photoFiles": photos}, tmp_path)
    assert report["pageErrors"] == [], f"pageErrors: {report['pageErrors']}"
    order = report["order"]
    assert order["editor"] is not None, f"editor chunk never prewarmed after first thumb: {order}"
    assert order["editorBegunBeforeDraw"] is False, f"editor request started before first thumb drew: {order}"
    assert order["editorCompletedBeforeDraw"] is False, f"editor completed before first thumb drew: {order}"
    assert order["editorRequests"] == 1, f"editor chunk requested more than once: {order}"
    assert report["exportOptionsOpened"] is True, "export options sheet failed to open from editor more menu"
    lazy = [p for p in report["lazyRequests"] if not p.split("/")[-1].startswith("editor_chunk-")]
    assert [p for p in lazy if not p.split("/")[-1].startswith("grid-")] == [], f"unexpected lazy chunks: {report['lazyRequests']}"


def test_pool_dispatches_wasm_op_to_idle_cold_slot_when_warm_slots_busy(tmp_path: Path) -> None:
    """四槽池中 0/1/2 已预热但忙碌、3 号冷槽空闲时，WASM 请求必须派到 3 号空闲槽而非排在 0 号后。"""
    _prereq()
    report = _run_driver("pool-idle-cold-slot", {"repoDir": str(REPO)}, tmp_path, timeout=60)
    assert report["wasmFetches"] == 1, f"expected single WASM fetch, got {report}"
    assert report["pingSlot"] == 3, f"non-WASM ping did not keep round-robin to slot 3: {report}"
    slots = [row["slot"] for row in report["dispatch"]]
    assert slots == [0, 1, 2, 3], f"resolve queued behind busy warm slot instead of idle slot 3: {report}"
    assert report["dispatch"][-1]["op"] == "resolve"
    assert report["dispatch"][-1]["module"] is True, f"cold idle slot 3 did not receive compiled WASM module: {report}"
    assert report["slot3Held"] == 1


_IMPORT = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*)["'`]\./([^"'`]+?\.js)["'`]""")


def test_build_has_exactly_two_lazy_chunks_that_depend_only_on_main() -> None:
    """生产包：首屏之后只有 grid 和 editor_chunk 两个 JS 分包，分包只静态依赖主包、没有自己的样式；样式与拆包前一致地随首屏加载。"""
    _prereq()
    assets = DIST / "assets"
    index_html = (DIST / "index.html").read_text(encoding="utf-8")
    main = re.findall(r'<script type="module"[^>]*src="/assets/([^"]+\.js)"', index_html)
    css = re.findall(r'<link rel="stylesheet"[^>]*href="/assets/([^"]+\.css)"', index_html)
    assert len(main) == 1 and len(css) == 1, f"index.html should load one main script and one stylesheet: {main} {css}"
    main_code = (assets / main[0]).read_text(encoding="utf-8")
    dynamic = sorted(set(re.findall(r"""import\(\s*["'`]\./([^"'`]+?\.js)["'`]\s*\)""", main_code)))
    assert [name.split("-")[0] for name in dynamic] == ["editor_chunk", "grid"], f"lazy chunks: {dynamic}"
    assert "__GXR_LAZY_CHUNKS__" not in main_code, "lazy chunk names were not written into the main chunk"
    for name in dynamic:
        assert name in main_code.replace(f"import(`./{name}`)", ""), f"retry map is missing {name}"
        code = (assets / name).read_text(encoding="utf-8")
        deps = {dep for dep in _IMPORT.findall(code) if not re.match(r"(worker|thumb|export_worker|off_image_worker|lut_table)-", dep)}
        assert deps <= {main[0]}, f"{name} depends on {deps - {main[0]}}; a retry would not re-request them"
        assert ".css" not in code, f"{name} loads its own stylesheet"
    assert [f.name for f in assets.glob("*.css") if not f.name.startswith(("main-", "dev-"))] == []


def _make_valid_raw_only_dng(dest: Path) -> str:
    """构造带 DNGVersion 标签且无内嵌预览的合法 DNG，用于验证「只有 RAW」路径。"""
    dest.mkdir(parents=True, exist_ok=True)
    data = bytearray(write_dng(np.full((64, 96), 512, dtype=np.uint16)))
    old_ifd = struct.unpack_from(">I", data, 4)[0]
    count = struct.unpack_from(">H", data, old_ifd)[0]
    start = old_ifd + 2
    rows = [bytes(data[at : at + 12]) for at in range(start, start + count * 12, 12)]
    rows.append(struct.pack(">HHI4s", 50706, 1, 4, bytes([1, 4, 0, 0])))
    struct.pack_into(">I", data, 4, len(data))
    dng = dest / "R9990001.DNG"
    dng.write_bytes(data + struct.pack(">H", len(rows)) + b"".join(sorted(rows)) + bytes(4))
    return str(dng)


def _lazy_photos(dest: Path) -> dict[str, str]:
    dest.mkdir(parents=True, exist_ok=True)
    jpeg, jpeg2, bad = dest / "R9990002.JPG", dest / "R9990003.JPG", dest / "R9990004.JPG"
    Image.new("RGB", (96, 64), (120, 140, 160)).save(jpeg, format="JPEG", quality=85)
    Image.new("RGB", (96, 64), (160, 120, 90)).save(jpeg2, format="JPEG", quality=85)
    bad.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00unexpected end")
    raw = _make_valid_raw_only_dng(dest / "raw")
    return {"jpeg": str(jpeg), "jpeg2": str(jpeg2), "bad": str(bad), "raw": raw}


def _run_lazy(modes: list[str], tmp_path: Path, timeout: int = 300) -> dict[str, Any]:
    out = tmp_path / "lazy-report.json"
    manifest = tmp_path / "lazy-manifest.json"
    payload = {"distDir": str(DIST), "photos": _lazy_photos(tmp_path / "photos"), "modes": modes, "out": str(out)}
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(LAZY), str(RUNTIME), str(manifest)],
        cwd=REPO,
        env={**os.environ, "HOME": str(REAL_HOME)},
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert proc.returncode == 0, f"lazy driver failed ({proc.returncode}): {proc.stderr[-1500:]}"
    data: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
    for mode, row in data.items():
        assert "error" not in row, f"{mode}: {row['error']}"
        assert row["pageErrors"] == [], f"{mode} pageErrors: {row['pageErrors']}"
    return data


def test_grid_chunk_failure_shows_error_and_retry_recovers(tmp_path: Path) -> None:
    """网格分包失败时停在开始页并给出「重试」「刷新页面」；重试换地址重新请求并恢复，之后往返不再请求。"""
    _prereq()
    row = _run_lazy(["grid-fail"], tmp_path)["grid-fail"]
    assert row["onError"] == {"screen": "start", "startVisible": True, "retry": True, "reload": True}, row
    assert row["afterRepick"]["screen"] == "start" and row["afterRepick"]["dialogs"] == 1, row
    assert row["ok"] == 1, f"grid chunk must load exactly once: {row}"
    assert row["requests"] == row["atRecovery"], f"navigating after recovery re-requested the grid chunk: {row}"
    assert row["requests"][-1].startswith("?retry="), f"retry did not use a fresh URL: {row}"
    assert row["cells"] == 2, f"photos picked while the grid chunk was failing were lost or duplicated: {row}"


def test_editor_chunk_failure_retry_recovers_with_one_module(tmp_path: Path) -> None:
    """编辑器分包：预取失败不提示；点开照片时失败才提示，重试成功后只有一份模块，导出选项和关机图不再请求分包。"""
    _prereq()
    data = _run_lazy(["editor-fail", "editor-prewarm-fail"], tmp_path)
    row = data["editor-fail"]
    assert row["prewarmRequests"] and row["dialogAfterPrewarm"] == 0, f"failed prewarm must stay silent: {row}"
    assert row["onError"] == {"screen": "grid", "retry": True, "reload": True}, row
    assert row["ok"] == 1 and row["requests"][-1].startswith("?retry="), f"retry did not re-request with a fresh URL: {row}"
    assert len(row["requests"]) == row["atRecovery"], f"export options or off-image re-requested the editor chunk: {row}"
    assert row["navigations"] == 1, "recovery must not reload the page"
    quiet = data["editor-prewarm-fail"]
    assert quiet["dialogs"] == 0 and quiet["ok"] == 1, f"tap after a failed prewarm must load directly: {quiet}"
    assert quiet["requests"][0] == "" and quiet["requests"][-1].startswith("?retry="), quiet


def test_stale_editor_loads_never_open_or_report_on_the_wrong_screen(tmp_path: Path) -> None:
    """编辑器分包在路上：先点 A 再点 B 只开 B；点开或多选导出后清空照片，分包到了（成功或失败）也不开编辑器、不弹面板或错误。"""
    _prereq()
    modes = ["open-other", "open-clear:ok", "open-clear:fail", "export-clear:ok", "export-clear:fail", "export-wait"]
    data = _run_lazy(modes, tmp_path)
    other = data["open-other"]
    assert other["screenWhileLoading"] == "grid" and other["editors"] == 1, other
    assert other["dialogs"] == 0 and other["want"] and other["photo"] == other["want"], f"a stale open showed another photo: {other}"
    for mode in ("open-clear:ok", "open-clear:fail"):
        assert data[mode] == {"screen": "start", "editors": 0, "dialogs": 0, "startVisible": True, "pageErrors": []}, data[mode]
    for mode in ("export-clear:ok", "export-clear:fail"):
        assert data[mode] == {"screen": "start", "exportSheets": 0, "dialogs": 0, "pageErrors": []}, data[mode]
    wait = data["export-wait"]
    assert wait["sheetsWhileLoading"] == 0 and wait["sheets"] == 1 and wait["screen"] == "grid", wait


def test_raw_only_and_unreadable_photos_with_lazy_chunks(tmp_path: Path) -> None:
    """只有 RAW 时导入期间不预取编辑器分包，落定空闲后才预取；网格分包晚到时坏图照常提示一次。"""
    _prereq()
    data = _run_lazy(["raw-only", "unreadable-hold-grid"], tmp_path)
    raw = data["raw-only"]
    assert raw["early"] == {"drawn": 0, "requests": 0}, f"RAW-only import prewarmed the editor early: {raw}"
    assert raw["settled"] == 1, f"settled idle prewarm did not run: {raw}"
    assert len(raw["requests"]) == 1, raw
    bad = data["unreadable-hold-grid"]
    assert bad["beforeGrid"] == {"screen": "start", "thumbs": 0}, f"photos were read before the grid chunk: {bad}"
    assert bad["screen"] == "grid" and bad["notices"] == 1 and bad["drawn"] == 1, bad


def test_ipad_export_options_always_target_the_current_photo(tmp_path: Path) -> None:
    """iPad：导出选项随点随开；换照片后打开导出选项并保存，交给共享面板的是当前这张。"""
    _prereq()
    row = _run_lazy(["ipad-switch-export"], tmp_path)["ipad-switch-export"]
    assert row["sheetAtOnce"] == 1, f"export options must open synchronously: {row}"
    assert row["first"] != row["second"] == row["photo"], row
    assert row["shared"] and all(name.startswith(row["second"]) for name in row["shared"]), row
