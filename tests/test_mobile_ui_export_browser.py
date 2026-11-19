"""UI-M3：导出进相册，在无头 Chrome 里走一遍（`browser_serial`）。

- 解码门禁：手机解出来的相机 JPG（`createImageBitmap` + OffscreenCanvas）和桌面 `load_jpeg` 逐像素相同，含一张改写
  Orientation 的拷贝。
- 整条导出：同样的编辑在桌面 `export_finish` 和手机上各做一次，文件逐字节相同（ICC 的 12 字节时间戳按导出管线的办法
  固定），sidecar 的 `parse_edit` 和结果 JSON 相同。
- 流程：停止、文件读不了再重试、Worker 陷阱后重建、清空时停下、批次进行中网格照常滚。

照片只经 `support.samples` 找；比较一律走 `support.export_pipeline_cases` 的脱敏函数，不打印照片数据。这个文件在导出管线
隐私 lint 的范围里。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from support import mobile_ui_export as mx
from support import samples
from support.export_pipeline_cases import check, same_pixels, same_redacted
from test_mobile_ui_browser import MODULES, PKG, RUNTIME, _drive, _need

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "support" / "mobile_ui_export_browser.mjs"


def _prereq() -> None:
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")


def run_driver(tmp_path: Path, manifest: dict[str, Any], *, query: str = "", timeout: int = 600) -> dict[str, Any]:
    """Runs the driver; its report goes to a file in ``tmp_path`` (never to stdout)."""
    out = tmp_path / f"{manifest['mode']}-report.json"
    path = tmp_path / f"{manifest['mode']}-manifest.json"
    path.write_text(json.dumps({**manifest, "out": str(out)}), encoding="utf-8")
    result = _drive(DRIVER, path, tmp_path, query, timeout=timeout)
    debug = os.environ.get("GXR_M3_DEBUG")
    if debug:
        Path(debug, f"{manifest['mode']}-driver.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode != 0 or not out.is_file():
        pytest.fail("export browser driver failed (set GXR_M3_DEBUG=<folder> to keep its log)", pytrace=False)
    report: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
    check(report.get("error") is None, "driver step raised (its first line is in the report file)")
    return report


def test_mobile_ui_export_jpeg_decode_matches_load_jpeg(tmp_path: Path) -> None:
    """UI-M3 item 3: the phone's decode equals Pillow's ``load_jpeg(path, None)`` exactly."""
    pairs = mx.real_pairs(4)
    if not pairs:
        samples.missing(f"need DNG+JPG pairs in {mx.SAMPLE_DIR}")
    _prereq()
    jpgs = [jpg for _dng, jpg in pairs]
    turned = [mx.set_orientation(jpgs[0], tmp_path / f"R999000{v}.JPG", v) for v in (6, 8)]
    check(all(turned), "sample JPG has an Orientation tag to rewrite")
    jpgs += [tmp_path / "R9990006.JPG", tmp_path / "R9990008.JPG"]
    want = [mx.decoded_digest(path) for path in jpgs]
    dump = tmp_path / "dump"
    dump.mkdir()
    report = run_driver(tmp_path, {"mode": "decode", "jpgs": [str(p) for p in jpgs], "want": [w[0] for w in want], "dumpDir": str(dump)})
    check(report["pageErrors"] == [], "no page errors")
    rows = report["steps"]["decode"]
    check(len(rows) == len(jpgs), "one decode per JPG")
    for i, (row, (sha, width, height)) in enumerate(zip(rows, want, strict=True)):
        check(bool(row["ok"]), "phone decoded the JPG")
        check(row["width"] == width and row["height"] == height, "decoded size equals load_jpeg")
        if row["sha"] != sha:
            got = np.frombuffer((dump / f"decoded-{i}.rgb").read_bytes(), dtype=np.uint8).reshape(height, width, 3)
            same_pixels(got, mx.decoded(jpgs[i]), f"decode gate test index {i}")
    same_redacted([r["sha"] for r in rows], [w[0] for w in want], "decoded RGB SHA-256 per test index")


PARITY = REPO / "tests" / "support" / "mobile_ui_export_parity.mjs"
FLOWS = REPO / "tests" / "support" / "mobile_ui_export_flows.mjs"
RESULT_KEYS = ("render", "render_label", "render_kind", "calibrated", "detail")
MIB = 1024 * 1024


@pytest.fixture
def pinned_icc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> bytes:
    """The export-pipeline ICC pin: only header bytes 24..36 (the creation time) may differ."""
    from PIL import Image

    from gxr_studio import export as export_mod
    from gxr_studio.export import srgb_icc
    from support.core_cli import require_cli, run_json
    from support.export_pipeline_cases import cli_ok

    require_cli()
    np.save(tmp_path / "icc.npy", np.zeros((8, 8, 3), dtype=np.uint8))
    code, payload = run_json(["jpeg-encode", "--in", str(tmp_path / "icc.npy"), "--quality", "95", "--out", str(tmp_path / "icc.jpg")])
    cli_ok(code, payload, "jpeg-encode for the ICC pin")
    with Image.open(tmp_path / "icc.jpg") as im:
        rs = bytes(im.info["icc_profile"])
    py = srgb_icc()
    check(len(rs) == len(py) == 588 and rs[:24] == py[:24] and rs[36:] == py[36:], "ICC profiles differ outside bytes 24..36")
    monkeypatch.setattr(export_mod, "_ICC", rs)
    return rs


def _local_dat() -> Path:
    from test_mobile_ui_browser import DATS

    dat = next((path for path in DATS if path.is_file()), None)
    if dat is None:
        samples.missing("no local GXRCE.DAT fixture (GXR_STUDIO_TEST_PARENT)")
    return dat  # type: ignore[return-value]


def _blocks(side: dict[str, Any]) -> dict[str, Any]:
    """The ``recipe`` and ``adjust`` blocks, with ``exposure_ev`` as a float."""
    out = {k: json.loads(json.dumps(side[k])) for k in ("recipe", "adjust") if k in side}
    if "exposure_ev" in out.get("adjust", {}):
        out["adjust"]["exposure_ev"] = float(out["adjust"]["exposure_ev"])
    return out


class Desktop:
    """A temporary desktop home with the same DAT and synthetic calibration pack, and a temporary
    library whose photos are links to the picked files (nothing is written next to the samples)."""

    def __init__(self, tmp: Path, dat: Path) -> None:
        from gxr_studio.camdata import calib, calib_export, store
        from gxr_studio.library.store import Library
        from gxr_studio.render.service import RenderService
        from support.mobile_ui_calib import pack_files, write_pack

        self.home = tmp / "app-home"
        self.home.mkdir()
        raw = dat.read_bytes()
        store.import_from_dat(raw, self.home)
        _dat, entries = store.entries_from_dat(raw)
        pack = pack_files({name: entries[(unit, 4)] for unit, name in enumerate(("jlaunch", "qlaunch", "l08firm"))})
        write_pack(calib.calib_dir(self.home), pack)
        self.zip = tmp / "GXR 校准数据 2026-11-16.zip"
        self.zip.write_bytes(calib_export.zip_bytes(pack, (2026, 11, 16, 9, 0, 0)))
        self.lib = Library(tmp / "library").open()
        self.render = RenderService(self.home, self.lib)
        self.folder = self.lib.root / "2026" / "m3"
        self.folder.mkdir(parents=True)
        self.export_dir = tmp / "desktop-exports"
        self.export_dir.mkdir()

    def binding(self) -> str:
        self.render._refresh()
        return str(self.render.engine_pin())

    def shot(self, stem: str, dng: Path | None, jpg: Path | None) -> dict[str, Any]:
        """The library's shot dict: name, photo_meta width/height (JPG first), lens and ISO."""
        from gxr_studio.meta import PhotoMeta, read_dng_meta, read_jpeg_meta

        files: dict[str, Any] = {}
        for src, key in ((dng, "dng"), (jpg, "jpg")):
            if src is not None:
                (self.folder / src.name).symlink_to(src)
                blob = src.read_bytes()
                files[key] = {"relpath": f"2026/m3/{src.name}", "sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob)}
        pm_dng = read_dng_meta(dng) if dng else PhotoMeta()
        pm = read_jpeg_meta(jpg) if jpg else pm_dng
        if pm.error:
            pm = pm_dng
        return {
            "shot_id": stem,
            "name": stem,
            "width": pm.width or pm_dng.width,
            "height": pm.height or pm_dng.height,
            "files": files,
            "lens": pm.lens or pm_dng.lens,
            "iso": pm.iso or pm_dng.iso,
            "recipe_tag": pm_dng.recipe_tag or pm.recipe_tag,
        }

    def changes(self, shot: dict[str, Any], done: dict[str, Any]) -> dict[str, Any]:
        """What the desktop UI sends for the same edits (recipes.js / adjust.js)."""
        from gxr_studio.render.recipes import unit_of_lens

        unit = unit_of_lens(shot.get("lens")) or "jlaunch"
        out: dict[str, Any] = {}
        if done.get("card"):
            out["recipe"] = {"kind": "builtin", "unit": unit, "slot": int(done["card"].split(":")[1]), "levels": {}}
        if done.get("look"):
            look = done["look"]
            listed = self.render.recipe_menu().listing()[["jlaunch", "qlaunch", "l08firm"].index(unit)]
            info = next(r for r in listed["looks"] if r["id"] == look["id"])
            out["recipe"] = {"kind": "look", "id": look["id"], "unit": unit, "slot": int(info["slot"]), "levels": {look["knob"]: look["level"]}}
        if "recipe" in out:
            # 桌面 look_cards.js recipeTarget：第一次选配方时把切走的默认原厂 04（档位为空）记进 memory，
            # recipeBody({ memory: true }) 带着它交给 PUT /edit（UI-M3-GROUP 第 7 项：手机也这样记）。
            out["recipe"]["memory"] = {f"{unit}/4": {}}
        knobs = dict(done.get("knobs") or {})
        if knobs:
            adjust: dict[str, Any] = {k: v for k, v in knobs.items() if k not in ("warm", "tint")}
            if "warm" in knobs or "tint" in knobs:
                adjust["wb"] = {k: knobs[k] for k in ("warm", "tint") if k in knobs}
            out["adjust"] = adjust
        return out

    def sidecar(self, shot: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
        from gxr_studio.render import edits

        if not changes:
            return {}
        _edit, side = edits.write(self.lib, shot, changes, self.render.recipe_menu())
        return side

    def export(self, shot: dict[str, Any], side: dict[str, Any], quality: int, long_edge: int | None) -> Any:
        from gxr_studio.finish.serve import export_finish
        from gxr_studio.render.camera_recipe import photo_edit

        dest = self.export_dir / f"{shot['name']}-{quality}-{long_edge}"
        edit = photo_edit(side, shot, self.render.recipe_menu())
        return export_finish(self.render, self.lib, shot, edit, side, dest, [], quality=quality, long_edge=long_edge, fmt="jpeg", base="auto")


def test_mobile_ui_export_files_match_desktop(tmp_path: Path, pinned_icc: bytes) -> None:
    """UI-M3 item 9: six photos edited in the UI and exported equal desktop ``export_finish`` byte for byte."""
    from gxr_studio.render.params import parse_edit
    from support.export_pipeline_cases import note, same_file_redacted

    pairs = mx.real_pairs(4)
    if len(pairs) < 4:
        samples.missing(f"need four DNG+JPG pairs in {mx.SAMPLE_DIR}")
    dat = _local_dat()
    _prereq()
    desk = Desktop(tmp_path, dat)
    try:
        jpg_only = mx.copy_as(pairs[1][1], tmp_path / "R9990002.JPG")
        dng_only = mx.copy_as(pairs[2][0], tmp_path / "R9990003.DNG")
        photos: dict[str, tuple[Path | None, Path | None]] = {dng.stem: (dng, jpg) for dng, jpg in pairs}
        photos[jpg_only.stem] = (None, jpg_only)
        photos[dng_only.stem] = (dng_only, None)
        stems = list(photos)
        a, b, c, d = (p[0].stem for p in pairs)
        plans: dict[str, Any] = {
            a: {"card": "builtin:1", "knobs": {"exposure_ev": 0.3, "contrast": 12}},
            b: {"look": True},
            c: {},
            d: {"knobs": {"shadow": 20, "warm": -8}},
            jpg_only.stem: {"knobs": {"exposure_ev": -0.2, "saturation": 15}},
            dng_only.stem: {"card": "builtin:2"},
        }
        files = [str(p) for pair in pairs for p in pair] + [str(jpg_only), str(dng_only), str(dat)]
        out_dir = tmp_path / "phone"
        out_dir.mkdir()
        report = run_driver(
            tmp_path,
            {
                "mode": "parity",
                "parityDriver": str(PARITY),
                "files": files,
                "pack": str(desk.zip),
                "count": len(stems),
                "plans": plans,
                "order": stems,
                "smallOrder": [a, c],
                "binding": desk.binding(),
                "outDir": str(out_dir),
            },
            timeout=1500,
        )
        check(report["pageErrors"] == [], "no page errors")
        steps = report["steps"]
        full = steps["full"]
        check("导出 6 张" in full["sheet"]["text"], "sheet title")
        check("每张照片按各自比例导出，成品尺寸可能不同。" in full["sheet"]["text"], "several photos: the desktop size line")
        check("在系统面板里点「存储 6 张图像」" in full["sheet"]["text"], "the reworded footnote")
        check(full["sheet"]["hdrDisabled"] is False, "HDR switch enabled with RAW selected; explicitly turned off for SDR parity")
        check("6 张做好了 · 存进相册" in full["ready"], "ready bar main line")
        check(
            full["shared"]["labels"] == ["6 张做好了 · 存进相册（第 1 批，共 2 批）", "6 张做好了 · 存进相册（第 2 批，共 2 批）"],
            "two share chunks of at most 4",
        )
        check(full["shared"]["saved"] == "", "ready bar auto-dismisses after final share (no trailing 好)")
        check(steps["small"]["shared"]["saved"] == "", "ready bar auto-dismisses after final share (small)")
        check(full["canShare"] >= 2, "canShare asked before each share")
        check([len(c["files"]) for c in full["files"]] == [4, 2], "chunk sizes")
        check(all(c["active"] is True for c in full["files"] + steps["small"]["files"]), "share() called inside the tap")
        check(all(f["type"] == "image/jpeg" for c in full["files"] for f in c["files"]), "image/jpeg files")

        edits = {e["stem"]: e for e in steps["edits"]}
        shots = {stem: desk.shot(stem, *photos[stem]) for stem in stems}
        sides = {stem: desk.sidecar(shots[stem], desk.changes(shots[stem], edits[stem])) for stem in stems}
        tally: dict[str, int] = {"files": 0, "sides": 0, "results": 0}
        for tag, step, quality, long_edge in (("full-", full, 95, None), ("small-", steps["small"], 70, 1600)):
            facts = step["facts"]["facts"]
            for stem, fact in facts.items():
                shot, side = shots[stem], sides[stem]
                # The sidecar the UI handed to export_prepare parses to the desktop's edit, and its
                # recipe / adjust blocks are the ones render/edits.write stored.
                same_redacted(parse_edit(fact["side"]).to_json(), parse_edit(side).to_json(), "phone side parse_edit equals the desktop's")
                # JS writes exposure 0 as the integer 0 (JSON has no float zero); compare it as a number.
                same_redacted(_blocks(fact["side"]), _blocks(side), "phone side blocks equal the desktop sidecar's")
                check(fact["width"] == shot["width"] and fact["height"] == shot["height"], "shot width/height equal the library's photo_meta")
                check(fact["binding"] == desk.binding(), "dev binding override reached the request")
                res = desk.export(shot, side, quality, long_edge)
                phone = (out_dir / f"{tag}{res.path.name}").read_bytes() if (out_dir / f"{tag}{res.path.name}").is_file() else None
                same_file_redacted(phone, res.path.read_bytes(), "phone file equals desktop export_finish (ICC pinned)")
                got = {k: fact["result"][k] for k in ("file_name", *RESULT_KEYS)}
                want = {"file_name": res.path.name, **{k: getattr(res, k) for k in RESULT_KEYS}}
                same_redacted(got, want, "result JSON equals the desktop ExportResult")
                tally["files"] += 1
                tally["sides"] += 1
                tally["results"] += 1
        check(tally["files"] == 8, "six full-size and two small exports compared")
        mem = full["facts"]["mem"]
        check(0 < mem["wasm"] <= 512 * MIB, "full-size SDR export WASM high-water mark within 512 MiB")
        note("ui_m3_parity", tally)
        note(
            "ui_m3_memory",
            {
                "full": {
                    "wasm_mib": round(mem["wasm"] / MIB, 1),
                    "js_mib": round(mem["js"] / MIB, 1),
                    "staged_mib": round(full["facts"]["stagedHigh"] / MIB, 1),
                },
                "small": {
                    "wasm_mib": round(steps["small"]["facts"]["mem"]["wasm"] / MIB, 1),
                    "js_mib": round(steps["small"]["facts"]["mem"]["js"] / MIB, 1),
                    "staged_mib": round(steps["small"]["facts"]["stagedHigh"] / MIB, 1),
                },
                "ms_per_photo_full": [round(t) for t in full["facts"]["times"]],
                "ms_per_photo_small": [round(t) for t in steps["small"]["facts"]["times"]],
                "workers": mem["workers"],
            },
        )
    finally:
        desk.lib.close()


def test_mobile_ui_export_flows(tmp_path: Path) -> None:
    """UI-M3 item 9 flows: single-photo sheet, stop, Worker trap and recovery, scrolling, 清空, a file
    deleted before its turn then re-picked, and the no-share-sheet fallback."""
    pairs = mx.real_pairs(4)
    if len(pairs) < 4:
        samples.missing(f"need four DNG+JPG pairs in {mx.SAMPLE_DIR}")
    _prereq()
    extra = mx.copy_as(pairs[3][1], tmp_path / "R9990004.JPG")
    victim_source = pairs[2][1]
    victim = mx.copy_as(victim_source, tmp_path / "R9990005.JPG")
    files = [str(p) for pair in pairs for p in pair] + [str(extra)]
    stems = [dng.stem for dng, _jpg in pairs] + [extra.stem]
    out_dir = tmp_path / "phone"
    out_dir.mkdir()
    report = run_driver(
        tmp_path,
        {
            "mode": "flows",
            "flowsDriver": str(FLOWS),
            "files": files,
            "stems": stems,
            "first": str(pairs[0][1]),
            "firstStem": pairs[0][1].stem,
            "victim": str(victim),
            "victimStem": victim.stem,
            "victimSource": str(victim_source),
            "outDir": str(out_dir),
        },
        timeout=1500,
    )
    check(report["pageErrors"] == [], "no page errors")
    steps = report["steps"]

    single = steps["single"]
    check(single["editorButton"] == "存到相册", "editor button is one-tap 存到相册")
    check("导出 0 张" in single["selectBar"] and "导出 0 张 · 下一步做" not in single["selectBar"], "selection bar export without 下一步做")
    check(" × " in single["sizeOriginal"] and single["sizeOriginal"].endswith("像素") or "像素 · " in single["sizeOriginal"], "size line")
    check(single["sizeSmall"].startswith("1600 × ") or " × 1600 像素" in single["sizeSmall"], "small tier is 1600 on the long edge")
    check("未导入相机配方文件时，导出不会带上相机配方效果" in single["sheetText"], "no-recipe-file guide")
    check("导出选项" in single["sheetText"] and "点「存储图像」" in single["sheetText"], "one-photo 导出选项 title and footnote")
    check("这项下一步做" not in single["sheetText"] and "亮处会更亮" in single["sheetText"], "HDR row live (ui-m3-hdr)")

    stop = steps["stop"]
    check(stop["stopped"]["phase"] == "stopped" and stop["stopped"]["staged"] == 2, "stop after 2 of 5 keeps 2 files")
    check(stop["stopped"]["alive"] is False, "stop terminates the export Worker")
    check("已停止。做好的 2 张还没存，点「存进相册」。" in (stop["stopped"]["dock"] or ""), "stopped wording")
    check(stop["after"]["phase"] == "ready" and stop["after"]["staged"] == 1, "a new export works after stop")

    trap = steps["trap"]
    check(trap["staged"] == 2 and len(trap["failures"]) == 1, "trap on photo 2 of 3: two files, one failure")
    check(trap["failures"][0]["kind"] == "trap", "the failure is the trap")
    check("内存不够，这张没做成。可以重试，或改用小一点的尺寸。" in (trap["dock"] or ""), "memory line shown")
    check(trap["workers"] == 2, "a new Worker was created after the trap")
    check("做好 2 张，1 张没做成" in (trap["dock"] or "") and "重试这张" in (trap["dock"] or ""), "06f wording")
    check(steps["trapRetry"]["staged"] == 3 and steps["trapRetry"]["failures"] == [], "retry after the trap succeeds")

    scroll = steps["scroll"]
    check(scroll["ticks"] > 10 and scroll["phase"] == "running", "rAF keeps ticking while the batch runs")
    clear = steps["clear"]
    check(clear["wiped"]["phase"] is None and clear["wiped"]["dock"] is None, "清空 stops the batch and removes the bar")
    check(clear["wiped"]["alive"] is False, "清空 terminates the export Worker")
    check(clear["later"]["shared"] == clear["sharedBefore"], "no late file reaches the share step")

    read = steps["readFail"]
    check(len(read["failed"]["failures"]) == 1 and read["failed"]["failures"][0]["kind"] == "read", "deleted file fails with read")
    check("存储卡上的文件读不了" in (read["failed"]["dock"] or ""), "06f read line")
    check(read["retried"]["staged"] == 2 and read["retried"]["failures"] == [], "re-pick and retry succeeds")
    check(sorted(read["names"]) == sorted([f"{pairs[0][1].stem}_gxr.jpg", f"{victim.stem}_gxr.jpg"]), "retried files shared")

    bare = steps["noShare"]
    check("这个浏览器打不开系统的共享面板。" in bare["text"] and bare["links"] == 1, "no share sheet: one line and a download link")
    from support.export_pipeline_cases import note

    note("ui_m3_flows", {"scroll_ticks": scroll["ticks"], "scroll_max_gap_ms": round(scroll["maxGap"], 1), "trap_workers": trap["workers"]})


STATE = REPO / "tests" / "support" / "mobile_ui_export_state.mjs"


def test_mobile_ui_export_batch_regressions_sol_r1(tmp_path: Path) -> None:
    """Sol r1 P2-01..05, replayed in the browser on the real ExportFlow / ExportBatch / ExportCore with
    Sol's synthetic inputs (no photos)."""
    _prereq()
    report = run_driver(tmp_path, {"mode": "state", "stateDriver": str(STATE)}, timeout=300)
    check(report["pageErrors"] == [], "no page errors")
    steps = report["steps"]
    for name in ("retryKept", "held", "cap", "urls", "workerOom", "workerOomOnce", "warmSetup", "warmBatch"):
        check("error" not in steps[name], "every Sol r1 scenario ran to the end")

    kept = steps["retryKept"]
    check(kept["afterDone"]["phase"] == "ready" and kept["afterDone"]["failures"] == 1, "P2-01: batch stays open with its failure")
    check(kept["afterDone"]["retry"] == 1 and kept["afterDone"]["share"] == 0, "P2-01: retry kept, nothing left to share")
    check(
        "1 张没做成" in (kept["afterDone"]["dock"] or "") and "做好" not in (kept["afterDone"]["dock"] or ""),
        "P2-01: shared photos not counted again",
    )
    check(kept["afterRetry"]["failures"] == 0 and kept["afterRetry"]["staged"] == 1, "P2-01: retry succeeds")
    check(kept["end"]["phase"] == "closed" and kept["end"]["dock"] is None, "P2-01: batch auto-dismisses once nothing is left")

    held = steps["held"]
    check(held["heldAt"]["phase"] == "held" and held["heldAt"]["staged"] == ["1"], "P2-05: the over-cap result is not held")
    check(held["heldAt"]["stagedBytes"] <= 100 and held["maxRetained"] <= 100 and held["stagedHigh"] <= 100, "P2-05: never above the cap")
    check(held["afterRetry"]["phase"] == "held" and held["afterRetry"]["pending"] == 4, "P2-02: retry while held keeps the queue")
    check(held["afterRetry"]["calls"] == 3, "P2-02: retry waits for the hand-off")
    check(held["calls"] == ["0", "1", "2", "0", "2", "3", "4"], "P2-02: queue continues in grid order after the hand-off")
    check(held["end"]["phase"] == "ready" and held["end"]["staged"] == ["0", "2", "3", "4"], "P2-02: every photo made")
    check(held["end"]["failures"] == 0 and held["end"]["waits"] == 0, "P2-02: nothing left waiting")

    cap = steps["cap"]
    check(
        cap["held"]["phase"] == "held" and cap["held"]["stagedBytes"] == 90 and cap["held"]["stagedHigh"] == 90,
        "P2-05: 40+50 held, 30 released",
    )
    check(cap["maxRetained"] <= 100 and cap["stagedHigh"] <= 100, "P2-05: retained bytes never above the cap")
    check(cap["end"]["phase"] == "ready" and cap["end"]["sizes"] == [30, 25], "P2-05: the released photo is redone after the hand-off")
    check(cap["calls"] == ["0", "1", "2", "2", "3"], "P2-05: re-queued photo runs first")

    urls = steps["urls"]
    check(urls["firstPaint"]["liveUrls"] == 1 and urls["secondPaint"]["liveUrls"] == 1, "P2-03: a redraw revokes the old link")
    for key in ("afterDoneClear", "afterClear", "afterEnd"):
        check(urls[key]["liveUrls"] == 0 and urls[key]["flowUrls"] == 0, "P2-03: no object URL left after hand-off, 清空 or batch end")
    check(urls["beforeStart"]["liveUrls"] == 1 and urls["afterStart"]["liveUrls"] == 0, "P2-03: a new export revokes the old links")
    check(urls["created"] == urls["revoked"], "P2-03: every created URL revoked")

    oom = steps["workerOom"]
    check(oom["phase"] == "ready" and oom["kinds"] == ["trap", "trap"], "P2-04: Worker creation RangeError settles as a trap")
    check(oom["messages"] == [True, True] and oom["attempts"] == 2, "P2-04: memory line, a new Worker tried for the next photo")
    check(oom["busy"] is False and oom["alive"] is False, "P2-04: pause released, no Worker left")
    once = steps["workerOomOnce"]
    check(once["phase"] == "ready" and once["attempts"] == 2 and once["kinds"][0] == "trap", "P2-04: next photo runs on a new Worker")
    check("run" not in once["calls"] and "wait" not in once["calls"], "P2-04: no photo left running or waiting")
    warm = steps["warmSetup"]
    check(warm["before"]["alive"] is True and warm["before"]["calls"]["setup"] == 1, "r2 P2-04: the size probe created the Worker first")
    check(
        warm["after"]["phase"] == "ready" and warm["after"]["kinds"] == ["trap"],
        "r2 P2-04: setup send failure on the existing Worker is a trap",
    )
    check(warm["after"]["memoryLine"] is True and warm["after"]["retry"] == 1, "r2 P2-04: memory line and retry shown")
    check(warm["after"]["sheet"] is False and warm["after"]["calls"]["run"] == 0, "r2 P2-04: sheet closed, nothing sent after the failed setup")
    check(warm["after"]["alive"] is False and warm["after"]["calls"]["terminate"] >= 1, "r2 P2-04: the Worker is terminated")
    check(
        warm["retried"]["phase"] == "ready" and warm["retried"]["kinds"] == [] and warm["retried"]["staged"] == 1,
        "r2 P2-04: retry succeeds on a new Worker",
    )
    batch = steps["warmBatch"]
    check(batch["probe"] is True and batch["phase"] == "ready", "r2 P2-04: batch with a pre-created Worker settles")
    check(
        batch["states"] == {"0": "fail", "1": "ok"} and batch["kinds"] == ["trap"] and batch["memoryLine"] is True,
        "r2 P2-04: next photo still made",
    )
    check(
        batch["attempts"] == 2 and batch["setups"] == 3 and batch["terminations"] >= 1 and batch["busy"] is False,
        "r2 P2-04: new Worker for the next photo",
    )
    check("每张 —" in steps["perfEmpty"] and "每张 0 ms" not in steps["perfEmpty"], "perf strip shows em-dash before first finished photo")
    check(steps["paused"] is False, "card thumbnails and bakes resume")
