"""手机界面在无头 Chrome 里走一遍。SwiftShader 的帧时间只记下来，不断言。

服务器和浏览器在结束时关掉。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import REAL_HOME
from gpu_support import find_chrome
from support import samples
from test_mobile_shell_browser import core_prerequisite

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
PARENT = samples.sample_parent()
SAMPLES = PARENT / "captures/a12-50/2026-10-19/stage2c-first-run"
PKG = REPO / "mobile" / "src" / "wasm-pkg" / "gxr_wasm.js"
MODULES = REPO / "mobile" / "node_modules"
RUNTIME = Path(os.environ.get("GXR_PLAYWRIGHT_RUNTIME", str(REAL_HOME / ".codex/playwright-runtime/runtime.mjs")))
DRIVER = REPO / "tests" / "support" / "mobile_ui_browser.mjs"


def _need(ok: bool, message: str) -> None:
    action = core_prerequisite(
        ok,
        require_core=os.environ.get("GXR_REQUIRE_CORE") == "1",
        require_gpu=os.environ.get("GXR_REQUIRE_GPU_TEST") == "1",
    )
    if action == "fail":
        pytest.fail(message)
    if action == "skip":
        pytest.skip(message)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _stop(proc: subprocess.Popen[str] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)


def _pairs() -> list[Path]:
    if not SAMPLES.is_dir():
        return []
    found: list[Path] = []
    dngs = sorted(path for path in SAMPLES.iterdir() if path.is_file() and path.suffix.upper() == ".DNG")
    for dng in dngs:
        jpg = next((dng.with_suffix(ext) for ext in (".JPG", ".jpg") if dng.with_suffix(ext).is_file()), None)
        if jpg is None:
            continue
        found.extend((dng, jpg))
        if len(found) >= 12:
            break
    return found


def _drive(driver: Path, manifest: Path, tmp_path: Path, query: str, timeout: int = 240) -> subprocess.CompletedProcess[str]:
    """起 Vite 开发服务器（样本走 GXR_MOBILE_SAMPLES），跑一个驱动脚本，最后按进程组关掉服务器。"""
    port = _free_port()
    env = os.environ.copy()
    env["GXR_MOBILE_SAMPLES"] = str(SAMPLES)
    log = tmp_path / "vite.log"
    vite: subprocess.Popen[str] | None = None
    try:
        with log.open("w", encoding="utf-8") as stream:
            vite_bin = REPO / "mobile" / "node_modules" / "vite" / "bin" / "vite.js"
            vite = subprocess.Popen(
                ["node", str(vite_bin), "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
                cwd=REPO / "mobile",
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if vite.poll() is not None:
                pytest.fail(f"vite exited {vite.returncode}\n{log.read_text(encoding='utf-8', errors='replace')[-2000:]}")
            probe = subprocess.run(
                [
                    "node",
                    "-e",
                    "fetch(process.argv[1]).then((r)=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))",
                    f"http://127.0.0.1:{port}/",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if probe.returncode == 0:
                break
            time.sleep(0.4)
        else:
            pytest.fail("vite did not answer")
        result = subprocess.run(
            ["node", str(driver), str(RUNTIME), f"http://127.0.0.1:{port}/{query}", str(manifest)],
            cwd=REPO,
            env={**os.environ, "HOME": str(REAL_HOME)},
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    finally:
        _stop(vite)
    if result.returncode != 0:
        result.stderr += log.read_text(encoding="utf-8", errors="replace")[-2000:]
    return result


def test_mobile_ui_pick_grid_edit_and_bad_file(tmp_path: Path) -> None:
    files = _pairs()
    # 地基用的那份样本只有 4 对。够 6 对就取 6 对，不够就用现有的。
    samples.need_sample(len(files) >= 2, f"need a DNG+JPG pair in {SAMPLES}")
    _need(len(files) >= 2, f"need a DNG+JPG pair in {SAMPLES}")
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("pnpm") is not None and shutil.which("node") is not None, "pnpm and node are required")
    _need(find_chrome() is not None or RUNTIME.is_file(), "pinned Chrome is missing")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")
    bad = tmp_path / "R9990001.DNG"
    bad.write_bytes(b"not a tiff")
    dat = tmp_path / "GXRCE.DAT"
    dat.write_bytes(b"dat")
    manifest = tmp_path / "files.json"
    manifest.write_text(json.dumps({"pairs": [str(path) for path in [*files, dat]], "bad": str(bad)}), encoding="utf-8")
    result = _drive(DRIVER, manifest, tmp_path, "?perf=1")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["pageErrors"] == []
    assert report["groups"] >= 1
    assert report["orderBefore"]
    assert report["orderAfter"] == list(reversed(report["orderBefore"]))
    assert report["perf"]["samples"] > 0
    assert "原图" in report["held"]
    assert report["swipe"][0] != report["swipe"][1]
    assert "读不了" in report["bad"]
    assert "从这次去掉" in report["bad"]


RECIPES_DRIVER = REPO / "tests" / "support" / "mobile_ui_recipes_browser.mjs"
# 相机配方文件用本机已有的夹具（和 camdata 的对拍一样从上层项目读，不进仓库）：有 Looks 和用户配方的 g158 优先。
DATS = (
    samples.sample_parent() / "firmware/package_v310_g158/local/SD_ROOT/GXRCE.DAT",
    samples.sample_parent() / "firmware/package_v36_g127/local/SD_ROOT/GXRCE.DAT",
)


Picks = dict[str, object]


def _desktop_bakes(
    home: Path, raw: bytes, pack: dict[str, bytes], dng: Path, jpg: Path, picks: Picks, gated: frozenset[str] = frozenset()
) -> tuple[dict[str, dict[str, object]], dict[str, tuple[Any, Any, bytes]]]:
    """桌面那条路：导入同一份相机配方文件和合成校准包，按导出用的 RenderService.plan → preview_bake → bake_bytes 出表。

    `gated` 里的选择另外在本进程用 color-oracle 的 `capture_bake_chroma` 跑一次生产 bake（转 float32 之前抓下
    float64 见证），返回 (CapturedBake, 模型, 条目)，交给 `bake_gate`。"""
    import hashlib

    from gxr_studio.camdata import calib, store
    from gxr_studio.gpu.tables import bake_bytes
    from gxr_studio.library.store import Library
    from gxr_studio.meta import read_dng_meta, read_jpeg_meta
    from gxr_studio.render.params import EditState, RecipeSel
    from gxr_studio.render.service import RenderService
    from support.color_oracle_gates import capture_bake_chroma
    from support.core_cli import CLI as CORE_CLI
    from support.mobile_ui_calib import write_pack

    store.import_from_dat(raw, home)
    write_pack(calib.calib_dir(home), pack)
    jm, dm = read_jpeg_meta(jpg), read_dng_meta(dng)
    shot = {
        "lens": jm.lens or dm.lens,
        "iso": jm.iso or dm.iso,
        "files": {"dng": {"sha256": hashlib.sha256(dng.read_bytes()).hexdigest(), "relpath": dng.name}},
    }
    lib = Library(home.parent / "lib").open()
    try:
        render = RenderService(home, lib)
        unit = render.photo_unit(shot) or "jlaunch"
        listed = render.recipe_menu().listing()[["jlaunch", "qlaunch", "l08firm"].index(unit)]
        out: dict[str, dict[str, object]] = {}
        captures: dict[str, tuple[Any, Any, bytes]] = {}
        for name, pick in picks.items():
            card, levels = pick if isinstance(pick, tuple) else (pick, {})
            if card is None:
                sel = None
            elif card.startswith("builtin:"):
                sel = RecipeSel(unit, int(card.split(":")[1]), levels=tuple(levels.items()))
            else:
                kind, ident = card.split(":", 1)
                info = next(r for r in listed["looks" if kind == "look" else "users"] if r["id"] == ident)
                sel = RecipeSel(unit, int(info["slot"]), levels=tuple(levels.items()), kind=kind, id=ident)
            plan = render.plan(shot, EditState(recipe=sel))
            data, meta = bake_bytes(render.preview_bake(plan))
            if name in gated:
                model = render.model(plan.model_iso, plan.unit)
                captured = capture_bake_chroma(model, plan.entry, float(meta["step"]), workers=1)
                # 同一个生产 bake：和服务路径（preview_bake → bake_bytes）出的字节一样。
                assert captured.data == data and captured.meta == meta, name
                captures[name] = (captured, model, plan.entry)
            # 同一个条目、同一个模型交给原生 gxr-cli color-bake：手机（WASM）必须和它逐字节相同。
            manifest = json.loads((calib.calib_dir(home) / "manifest.json").read_text(encoding="utf-8"))
            branch = manifest["units"][unit]["models"][str(plan.model_iso)]
            work = home.parent / f"native-{name}"
            work.mkdir(exist_ok=True)
            (work / "e.bin").write_bytes(plan.entry)
            (work / "s.bin").write_bytes(store.load_stock04(home, unit))
            (work / "p.json").write_text('{"step": 0.375}', encoding="utf-8")
            native = subprocess.run(
                [str(CORE_CLI), "color-bake", "--entry", str(work / "e.bin"), "--stock", str(work / "s.bin")]
                + ["--ctx", str(calib.calib_dir(home) / branch["ctx"]), "--ring", str(calib.calib_dir(home) / branch["ring"])]
                + ["--params", str(work / "p.json"), "--out", str(work / "o.bin"), "--meta", str(work / "m.json")],
                check=True,
                capture_output=True,
                text=True,
                timeout=300,
            )
            assert native.returncode == 0, native.stderr
            out[name] = {
                "native_sha256": hashlib.sha256((work / "o.bin").read_bytes()).hexdigest(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "meta": meta,
                "model_iso": plan.model_iso,
                "unit": unit,
                "entry_sha256": plan.entry_sha256,
            }
        return out, captures
    finally:
        lib.close()


def test_mobile_ui_recipes_calibration_and_bake_parity(tmp_path: Path) -> None:
    """UI-M2：导入校准数据、06d、补选 DAT、配方卡、点卡换配方、换片保留、06c、06e，以及烘焙表和桌面逐字节相同。"""
    import zipfile

    from gxr_studio.camdata import calib_export, store
    from support.mobile_ui_calib import pack_files

    files = _pairs()
    dat = next((path for path in DATS if path.is_file()), None)
    if len(files) < 4:
        samples.missing(f"need two DNG+JPG pairs in {SAMPLES}")
    if dat is None:
        samples.missing("no local GXRCE.DAT fixture (GXR_STUDIO_TEST_PARENT)")
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")
    assert dat is not None
    raw = dat.read_bytes()
    _dat, entries = store.entries_from_dat(raw)
    # 合成的校准包：只有原厂 04 的指纹跟着这份 DAT 走，数组和色相环都是生成的。
    pack = pack_files({name: entries[(unit, 4)] for unit, name in enumerate(("jlaunch", "qlaunch", "l08firm"))})
    zip_path = tmp_path / "GXR 校准数据 2026-11-15.zip"
    zip_path.write_bytes(calib_export.zip_bytes(pack, (2026, 11, 15, 9, 0, 0)))
    deflated = tmp_path / "squeezed.zip"
    with zipfile.ZipFile(deflated, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, blob in pack.items():
            archive.writestr(name, blob)
    phone_dir = tmp_path / "phone"
    phone_dir.mkdir()
    lone = tmp_path / "R9990002.JPG"
    lone.write_bytes(files[1].read_bytes())
    manifest = tmp_path / "recipes.json"
    manifest.write_text(
        json.dumps(
            {
                "pairs": [str(path) for path in files[:4]],
                "dat": str(dat),
                "pack": str(zip_path),
                "deflated": str(deflated),
                "jpgOnly": str(lone),
                "jpgOnlyStem": lone.stem,
                # 手机交给引擎的 Look 和旋钮 Look 的表写到这里，交给 color-oracle 的 bake_gate。
                "outDir": str(phone_dir),
                # 手动截图时给一个文件夹（不进仓库）；平时不截。
                **({"shots": os.environ["GXR_M2_SHOTS"]} if os.environ.get("GXR_M2_SHOTS") else {}),
            }
        ),
        encoding="utf-8",
    )
    result = _drive(RECIPES_DRIVER, manifest, tmp_path, "", timeout=420)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["pageErrors"] == []
    steps = {row["name"]: row for row in report["steps"]}

    cal = steps["calibration"]
    assert "压缩" in cal["refused"]
    assert "50mm · ISO 200、1600" in cal["card"] and "28mm · ISO 200" in cal["card"] and "MOUNT · ISO 200" in cal["card"]
    assert "替换" in cal["card"] and "删除" in cal["card"]

    nodat = steps["no-recipe-file"]
    assert nodat["state"] == "no-recipe-file" and nodat["locked"] == 8
    assert nodat["names"][:4] == ["GR 正片", "GR 负片", "GR 高对比黑白", "原厂标准"]
    assert nodat["label"] == "中性转换"
    assert "还差相机配方文件" in nodat["sheet"] and "GXRCE.DAT" in nodat["sheet"] and nodat["closed"]

    cards = steps["cards"]
    assert cards["names"][:4] == ["GR 正片", "GR 负片", "GR 高对比黑白", "原厂标准"]
    assert cards["label"] == "电脑预测"
    assert "这项下一步做" not in cards["subs"]
    chosen = steps["chosen"]
    assert chosen["on"] == "builtin:1" and chosen["label"] == "电脑预测" and chosen["changed"]
    assert "GR 正片" in chosen["panel"] and "相机里 · 01" in chosen["panel"]
    assert "全部" in chosen["panel"] and "内置 8" in chosen["panel"] and "我的配方" in chosen["panel"]

    stem = cards["stem"]
    pair = next(i for i in range(0, len(files), 2) if files[i].stem == stem)
    picks: dict[str, object] = {"default": None, "chosen": "builtin:1"}
    if "user" in steps:
        picks["user"] = steps["user"]["id"]
    assert "look" in steps, "this DAT has no Looks for the photo's unit"
    look = steps["look"]
    picks["look"] = look["id"]
    picks["knob"] = (look["id"], {look["knob"]["name"]: look["knob"]["level"]})
    desktop, captures = _desktop_bakes(tmp_path / "app", raw, pack, files[pair], files[pair + 1], picks, frozenset({"look", "knob"}))
    for name, key in (("default", "defaultBake"), ("chosen", "bake")):
        phone = cards[key] if name == "default" else chosen[key]
        assert phone is not None, name
        assert phone["bytes"] == desktop[name]["bytes"], (name, phone, desktop[name])
        assert phone["sha256"] == desktop[name]["sha256"], (name, phone, desktop[name])
        assert phone["meta"] == desktop[name]["meta"], (name, phone["meta"], desktop[name]["meta"])
    if "user" in steps:
        assert steps["user"]["bake"]["sha256"] == desktop["user"]["sha256"], (steps["user"], desktop["user"])
    assert steps["hold"]["held"] == "原图" and steps["hold"]["after"] == "电脑预测"
    swipe = steps["swipe"]
    assert swipe["second"]["stem"] != stem and swipe["second"]["on"] == 0
    assert swipe["backOn"] == "builtin:1" and swipe["backKey"] == chosen["key"]

    cleared = steps["looks-cleared"]
    assert cleared["beforeCalib"]["count"] > 0 and cleared["afterCalib"]["closed"]
    assert cleared["beforeDat"]["count"] > 0 and cleared["afterDat"]["closed"]
    assert cleared["afterDat"]["stats"]["clears"] > cleared["afterCalib"]["stats"]["clears"]

    nocal = steps["no-calibration"]
    assert nocal["label"] == "中性转换" and nocal["key"] == "dev-neutral"
    assert set(nocal["subs"][:8]) == {"中性转换"}
    assert nocal["on"] == "builtin:1"
    assert "还没导入校准数据" in nocal["banner"] and nocal["tag"] == "未校准"

    jpg = steps["jpg-only"]
    assert jpg["label"] == "相机 JPG" and jpg["disabled"] == jpg["cards"] > 0
    assert "配方需要 RAW 文件" in jpg["panel"] and "拍摄时将画质设为 RAW+JPEG" in jpg["panel"]
    assert "HDR 需要 RAW 文件" in jpg["panel"]

    # 第 6 项：选一个 Look、动一个旋钮。
    assert look["drawn"] and "这项下一步做" not in look["panel"]
    knob = look["knob"]
    assert knob["key"] != look["key"] and knob["bake"]["sha256"] != look["bake"]["sha256"]
    # 拖动中（只有 input）不重新烘焙，舞台不换；松手才换。
    # 只看这张 Look 的 resolve：别的卡在后台画小图会装别的表，数表的张数会被它们带上。
    assert knob["duringDrag"]["key"] == look["key"] and knob["duringDrag"]["resolves"] == 0
    assert knob["afterRelease"] >= 1
    # UI 这一边：交给引擎的是和桌面同一个条目（旋钮已经套上）的表，而且和原生 gxr-cli 烘出来的逐字节相同。
    assert look["key"].endswith(":" + desktop["look"]["entry_sha256"]), (look["key"], desktop["look"])
    assert knob["key"].endswith(":" + desktop["knob"]["entry_sha256"]), (knob["key"], desktop["knob"])
    assert look["bake"]["sha256"] == desktop["look"]["native_sha256"], (look["bake"], desktop["look"])
    assert knob["bake"]["sha256"] == desktop["knob"]["native_sha256"], (knob["bake"], desktop["knob"])
    assert knob["bake"]["meta"] == desktop["knob"]["meta"]
    # Looks 用 color-oracle 的门禁：Gate A 手机 == 顺序 oracle 逐位相同；Gate B 手机和桌面 numpy 不同的点由门禁逐点归因。
    from support.color_oracle_gates import bake_gate

    gate_b: dict[str, int] = {}
    for name, saved in (("look", "phone-look"), ("knob", "phone-knob")):
        captured, model, entry_bytes = captures[name]
        phone_bytes = (phone_dir / f"{saved}.bin").read_bytes()
        phone_meta = json.loads((phone_dir / f"{saved}.meta.json").read_text(encoding="utf-8"))
        gap, allowed = bake_gate(captured, (phone_bytes, phone_meta), model, entry_bytes)
        assert gap is None, (name, gap)
        gate_b[name] = len(allowed)
    parity = {"phone": report["steps"], "desktop": desktop, "gate_b_points": gate_b}
    (tmp_path / "bake-parity.json").write_text(json.dumps(parity, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Looks bake_gate Gate B points: {gate_b}")


BOOK_DRIVER = REPO / "tests" / "support" / "mobile_ui_book_races.mjs"


def test_mobile_ui_book_drops_late_results(tmp_path: Path) -> None:
    """Sol r1：清空后晚到的 DAT、旧的比新的晚到、删除后晚到的校准数据，都不能盖掉现在的状态。真的 Book，假的异步边界。"""
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")
    from gxr_studio.camdata import calib_export
    from support.color_fixtures import entry
    from support.mobile_ui_calib import pack_files

    # 合成的校准包（只走清单和 SHA-256 的核对，用不到相机数据）。
    pack = tmp_path / "A.zip"
    pack.write_bytes(calib_export.zip_bytes(pack_files({"jlaunch": entry(words=0)}), (2026, 11, 15, 9, 0, 0)))
    # Sol r3：清单和 SHA-256 都对、但旧版本或单元为空 / 不认得的包。
    bad: dict[str, str] = {}
    for name, change in {
        "old-schema": {"schema": "gxr-studio/calib/1"},
        "empty-units": {"units": {}},
        "unknown-unit": {"units": {"nounit": {}}},
    }.items():
        files = pack_files({"jlaunch": entry(words=0)})
        listed = json.loads(files["manifest.json"])
        listed.update(change)
        files["manifest.json"] = json.dumps(listed).encode()
        path = tmp_path / f"{name}.zip"
        path.write_bytes(calib_export.zip_bytes(files, (2026, 11, 15, 9, 0, 0)))
        bad[name] = str(path)
    # Sol r4：某个 ISO 分支缺 ctx / ring（或整个为空）的包也要在保存前拒收，不能盖掉好包。
    for name, drop in {"empty-both": ("200", "1600"), "no-ctx-200": ("200:ctx",), "no-ring-1600": ("1600:ring",)}.items():
        files = pack_files({"jlaunch": entry(words=0)})
        listed = json.loads(files["manifest.json"])
        models = listed["units"]["jlaunch"]["models"]
        for item in drop:
            branch, _, key = item.partition(":")
            if key:
                del models[branch][key]
            else:
                models[branch] = {}
        files["manifest.json"] = json.dumps(listed).encode()
        path = tmp_path / f"{name}.zip"
        path.write_bytes(calib_export.zip_bytes(files, (2026, 11, 15, 9, 0, 0)))
        bad[name] = str(path)
    manifest = tmp_path / "book.json"
    manifest.write_text(json.dumps({"pack": str(pack), "bad": bad}), encoding="utf-8")
    result = _drive(BOOK_DRIVER, manifest, tmp_path, "", timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["pageErrors"] == []
    cases = report["cases"]
    assert cases["clearThenLateSuccess"] == {"dat": None, "busy": False, "error": None}
    assert cases["clearThenLateFailure"] == {"dat": None, "busy": False, "error": None}
    assert cases["olderLate"] == {"name": "B", "busy": False}
    late = cases["removeThenLateImport"]
    assert late["pack"] is None and late["busy"] is False and late["error"] is None and late["deletes"] == 1
    assert cases["lateRestore"] == {"sha": "NEW"}
    # Sol r1 P6：超过上限的文件零次读取；正常的包整份交给 Worker，主线程不读不复制。
    assert cases["oversized"]["ok"] is False and cases["oversized"]["reads"] == 0
    assert "太大" in cases["oversized"]["error"]
    assert cases["inLimit"] == {"ok": True, "packFile": 1, "reads": 0, "sha": "OK"}
    assert cases["idb"] == {"afterLate": None, "afterNew": "b.zip", "cleared": None}
    # 缓存上限：引擎里我们的表不超过 24 张，JS 不超过 16 张，舞台那张一直在。
    assert cases["caps"] == {"engineKeys": 24, "js": 16, "stageKept": True, "stage": True}
    # Sol r2：删除事务失败时保留校准包、写一行，重开还在；再删一次成功，重开也没了。
    failed = cases["failedDelete"]
    assert failed["imported"] is True
    assert failed["failed"]["pack"] == "A.zip" and "删除没有成功" in failed["failed"]["error"] and failed["failed"]["busy"] is False
    assert failed["failed"]["reopened"] == "A.zip"
    assert failed["retried"] == {"pack": None, "error": None, "busy": False, "reopened": None}
    # Sol r2：版本水位存下来，时钟回拨后新会话的导入仍排在墓碑之后；没存上的导入照实说。
    roll = cases["clockRollback"]
    assert roll["a"] is True and roll["markAfterDelete"] == 1001 and roll["restoredTomb"] is None
    assert roll["b"] == {"ok": True, "error": None, "savedAt": 1002}
    assert roll["reopenedB"] == "B.zip"
    assert roll["c"]["ok"] is False and "没有存进这台 iPhone" in roll["c"]["error"] and roll["c"]["pack"] == "C.zip"
    assert roll["reopenedAfterC"] == "B.zip"
    # Sol r3：核心的包级检查在存之前，坏包不存、不显示已导入，重开还是那份好的。
    invalid = cases["invalidImports"]
    assert invalid["good"] is True and invalid["reopened"] == "good.zip"
    assert invalid["bad"]["old-schema"] == {"accepted": False, "error": "校准数据版本已更新，请重新导入", "pack": "good.zip"}
    assert invalid["bad"]["empty-units"] == {"accepted": False, "error": "校准数据的单元不正确", "pack": "good.zip"}
    assert invalid["bad"]["unknown-unit"] == {"accepted": False, "error": "校准数据的单元不正确", "pack": "good.zip"}
    for name in ("empty-both", "no-ctx-200", "no-ring-1600"):
        assert invalid["bad"][name] == {"accepted": False, "error": "校准数据的文件清单不正确", "pack": "good.zip"}, name
    vis = cases["calibCommitVisibility"]
    assert vis["afterImport"]["idbDuringSupersede"] == "CommittedGood.zip"
    assert vis["afterImport"]["idbNow"] == "CommittedGood.zip"
    assert vis["afterImport"]["memNow"] == "CommittedGood.zip"
    assert "太大" in (vis["afterImport"]["error"] or "") and vis["afterImport"]["busy"] is False
    assert vis["afterDelete"]["idbNow"] is None and vis["afterDelete"]["memNow"] is None
    assert "太大" in (vis["afterDelete"]["error"] or "") and vis["afterDelete"]["busy"] is False
    # Sol r3：同一张打开的照片反复调旋钮，每张卡只留现在那张，旧的关掉；上限任何时候都成立。
    knob_thumbs = cases["knobThumbs"]
    assert knob_thumbs["oneCard"] == {"entries": 1, "bytes": 192 * 128 * 4, "live": 1, "lastLive": True}
    assert knob_thumbs["bytes"] <= knob_thumbs["limit"] == 32 << 20
    assert knob_thumbs["shownKept"] == 11 and knob_thumbs["shownKeys"] == sorted(f"bake:{c}:39" for c in range(11))
    assert knob_thumbs["live"] == knob_thumbs["entries"] == knob_thumbs["bytes"] // (192 * 128 * 4)
    # 配方卡小图：40 张照片之后仍在 32 MiB 以内，打开的那张 11 张都在，挤掉的都关了，清空后一张不剩。
    looks = cases["looks"]
    assert looks["limit"] == 32 << 20 and looks["bytes"] <= looks["limit"], looks
    assert looks["entries"] == looks["bytes"] // (192 * 128 * 4) and looks["evicted"] == 440 - looks["entries"] > 0
    assert looks["openKept"] == 11 and looks["newest"] == 11
    assert looks["evictedClosed"] and not looks["closedButListed"]
    assert looks["afterClear"] == 0 and looks["allClosed"] and looks["mapsEmpty"]


HIST_DRIVER = REPO / "tests" / "support" / "mobile_ui_hist_retry.mjs"


def test_mobile_ui_hist_retry_yields(tmp_path: Path) -> None:
    """Sol r6 P1：换片时上一张的直方图还在读回，histogram() 给 null。重试排到下一帧，页面定时器照常跑，几帧内画上。"""
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")
    manifest = tmp_path / "hist.json"
    manifest.write_text(json.dumps({"repo": str(REPO)}), encoding="utf-8")
    result = _drive(HIST_DRIVER, manifest, tmp_path, "", timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    cases = report["cases"]
    # Sol 的 hist-scheduler 探针：修好以前 5000 次重试里定时器一次都没轮到。
    first = cases["busyThenFree"]
    assert first["guardHit"] is False and first["histDrawn"] == 1, first
    # 定时器和下一帧谁先由浏览器排，所以是「一两次以内」，不是恰好第一次。
    assert first["timerAtCalls"] is not None and first["timerAtCalls"] <= 2, first
    assert first["calls"] <= 4 and first["drawnAtFrame"] is not None and first["drawnAtFrame"] <= 5, first
    # 一直忙：每帧最多问一次，定时器照常跑。
    busy = cases["alwaysBusy"]
    assert busy["guardHit"] is False and busy["maxPerFrame"] <= 1, busy
    assert busy["calls"] <= busy["frames"] + 1 and busy["ticks"] > 0, busy
    # 换片以后排着的那次取消了。
    assert cases["afterDestroy"] == {"more": 0}
    # 下面都是受控的帧队列和定时器，按帧数和毫秒推进，不看页面跑多少 fps（Sol r7）。
    # 经理 r6 裁定：300 次没拿到以后只剩一个 250 ms 定时器，不停；空下来后不用新的调整就自己画上。
    assert cases["threshold"] == {"calls": 300, "frames": 0, "timers": 1, "delay": 250}
    assert cases["before250"] == 300 and cases["at250"] == 301
    assert cases["fourSlowTicks"] == {"calls": 305, "frames": 0, "timers": 1}
    assert cases["freeWithoutInput"] == {"calls": 306, "histDrawn": 1, "frames": 0, "timers": 0}
    # 新的调整取消慢重试，重新按帧试；换片后排着的和晚到的旧回调都不再问。
    assert cases["newAdjustment"] == {"calls": 1, "misses": 1, "frames": 1, "timers": 0}
    assert cases["destroyAndStale"] == {"more": 0, "frames": 0, "timers": 0}
    assert cases["destroySlow"] == {"calls": 300, "frames": 0, "timers": 0}
    # reject 也只是每帧一次；读回晚到不画也不排。
    assert cases["rejected"] == {"calls": 11, "frames": 1, "timers": 0}
    assert cases["lateReadback"] == {"histDrawn": -1, "frames": 0, "timers": 0}
    # Sol r7：显卡丢了、pending 一直 true（真的 Engine 原型）。每次调度只看一次 pending，不去读回；
    # 300 次后每 250 ms 看一次，不排帧；关掉以后什么都不剩。
    assert cases["lostAt300"] == {"isLost": True, "pendingReads": 300, "histogramCalls": 0, "misses": 300, "frames": 0, "timers": 1}
    assert cases["lostSlow"] == {
        "afterTimer": {"pendingReads": 1, "frames": 0, "timers": 1},
        "pendingReads": 4,
        "histogramCalls": 0,
        "frames": 0,
        "timers": 1,
    }
    assert cases["lostDestroyed"] == {"frames": 0, "timers": 0}
    # pending 超过 300 次后这一帧画完了：下一个慢周期自己画上。
    assert cases["pendingThenReady"] == {
        "whilePending": {"calls": 0, "frames": 0, "timers": 1},
        "calls": 1,
        "histDrawn": 1,
        "frames": 0,
        "timers": 0,
    }
    # 放在最后：旧代码的 reject 没人接会进这里，先让上面那些说明问题的断言报出来。
    assert report["pageErrors"] == []
