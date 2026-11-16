"""UI-M4-LUT 第 7 项：在无头 Chrome 里驱动 LUT 导入、选空间、GPU 预览与桌面导出对拍（`browser_serial`）。

全部 LUT 由合成生成器现场构造（绝不带入第三方 LUT）。
照片只经 ``support.samples`` 查找；对比一律走 ``support.export_pipeline_cases`` 的脱敏帮手，本文件受
``test_core_parity_export_pipeline_guard.py`` 隐私 lint 约束（不用裸非标量 assert / print）。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from support import mobile_ui_export as mx
from support import samples
from support.export_pipeline_cases import check, cli_ok, note, same_file_redacted, same_redacted
from support.lut_cases import cube_text
from test_mobile_ui_browser import MODULES, PKG, RUNTIME, _drive, _need
from test_mobile_ui_export_browser import RESULT_KEYS, Desktop, _blocks, _local_dat, pinned_icc
from test_mobile_ui_export_hdr_browser import HDR_FIELDS, _desktop_hdr

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "support" / "mobile_ui_lut_browser.mjs"
MIB = 1024 * 1024

__all__ = ["pinned_icc"]


def _prereq() -> None:
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")


def _build_shaper_cube() -> str:
    """合成 1D + 3D shaper .cube（输入写明 sRGB）。"""
    v1 = np.linspace(0.0, 1.0, 17)
    s1 = np.clip(np.power(v1, 0.94), 0.0, 1.0)
    lut1_lines = [f"{float(x):.17g} {float(x):.17g} {float(x):.17g}" for x in s1]

    v3 = np.linspace(0.0, 1.0, 17)
    b, g, r = np.meshgrid(v3, v3, v3, indexing="ij")
    nodes = np.stack([r, g, b], axis=-1).reshape(-1, 3)
    rows = np.clip(nodes * np.array([1.03, 0.98, 0.94]) + np.array([0.01, 0.0, -0.01]), 0.0, 1.0)
    lut3_lines = [" ".join(f"{float(c):.17g}" for c in row) for row in rows]

    return (
        "# Input: sRGB\n"
        'TITLE "Synth Shaper 1D3D"\n'
        "LUT_1D_SIZE 17\n"
        "LUT_3D_SIZE 17\n" + "\n".join(lut1_lines) + "\n" + "\n".join(lut3_lines) + "\n"
    )


def _write_synthetic_luts(tmp_path: Path, desk: Desktop) -> dict[str, dict[str, Any]]:
    """生成 4 个合成 LUT + 1 个非 .cube 忽略文件，并加入桌面临时 LUT 库。"""
    luts_dir = tmp_path / "luts"
    luts_dir.mkdir()

    disp33_txt = cube_text(
        lambda x: np.clip(x * np.array([1.05, 0.98, 0.91]) + np.array([0.01, 0.0, -0.01]), 0.0, 1.0),
        size=33,
        title="Synth Display 33",
        comments="# Input: sRGB\n",
    )
    shaper_txt = _build_shaper_cube()
    undeclared_txt = cube_text(
        lambda x: np.clip(x * np.array([0.97, 1.02, 1.04]), 0.0, 1.0),
        size=17,
        title="Custom Grade",
        comments="",
    )
    applelog_txt = cube_text(
        lambda x: np.clip(x * np.array([1.06, 0.97, 0.90]) + np.array([0.02, -0.01, 0.01]), 0.0, 1.0),
        size=17,
        title="Synth Apple Log",
        comments="# Input: Apple Log\n",
    )

    specs = {
        "disp33": ("synth_disp_33.cube", disp33_txt),
        "shaper": ("synth_shaper_1d3d.cube", shaper_txt),
        "undeclared": ("custom_grade.cube", undeclared_txt),
        "applelog": ("synth_applelog.cube", applelog_txt),
    }
    out: dict[str, dict[str, Any]] = {}
    for key, (fname, txt) in specs.items():
        path = luts_dir / fname
        raw = txt.encode("utf-8")
        path.write_bytes(raw)
        rec = desk.render.luts.add(raw, fname)
        cube_id = str(rec["cube_id"])
        if key == "undeclared":
            rec = desk.render.luts.patch(cube_id, {"input_space": "srgb"})
        out[key] = {"path": str(path), "cubeId": cube_id, "record": rec}
    ignore = luts_dir / "ignore.txt"
    ignore.write_text("not a cube\n", encoding="utf-8")
    out["ignore"] = {"path": str(ignore)}
    return out


def _verify_display_raw_gate_b(
    tmp_path: Path,
    desk: Desktop,
    shot: dict[str, Any],
    side: dict[str, Any],
    cube_path: Path,
    cube_spec: dict[str, Any],
    phone_bytes: bytes,
    desk_bytes: bytes,
    quality: int,
    long_edge: int | None,
    label: str,
) -> tuple[bool, int]:
    """显示域 LUT 跑在 RAW 上时：逐字节相同，或由 export-source Gate A/B 逐点归因且用 Rust 底图重走成片后逐字节相同。"""
    if phone_bytes == desk_bytes:
        return True, 0

    from gxr_studio.camdata import calib
    from gxr_studio.finish import serve as serve_mod
    from gxr_studio.finish.serve import export_finish
    from gxr_studio.render.params import parse_edit
    from support import export_source_lut as gates
    from support.core_cli import run_json

    gdir = tmp_path / f"gate-{label}"
    gdir.mkdir(exist_ok=True)
    plain_side = {k: v for k, v in side.items() if k != "lut"}
    b_py, _f1, _l1 = serve_mod.color_image(
        desk.render, shot, parse_edit(plain_side), max_edge=None, quality=95, lib=desk.lib, base="auto", full=True
    )
    py_base, from_raw, base_labels = serve_mod.color_image(
        desk.render, shot, parse_edit(side), max_edge=None, quality=95, lib=desk.lib, base="auto", full=True
    )

    shot_json = gdir / "shot.json"
    side_json = gdir / "side.json"
    spec_json = gdir / "spec.json"
    rs_base_npy = gdir / "rsbase.npy"
    shot_json.write_text(json.dumps(shot), encoding="utf-8")
    side_json.write_text(json.dumps({k: v for k, v in side.items() if k in ("recipe", "adjust", "lut")}), encoding="utf-8")
    spec_json.write_text(json.dumps(cube_spec), encoding="utf-8")

    dng_rel = (shot.get("files") or {}).get("dng", {}).get("relpath")
    jpg_rel = (shot.get("files") or {}).get("jpg", {}).get("relpath")
    dng_path = desk.lib.root / dng_rel if dng_rel else None
    jpg_path = desk.lib.root / jpg_rel if jpg_rel else None
    dat_path = desk.home / "camera-data" / "dat-template" / "GXRCE.DAT"
    check(dat_path.is_file(), "desktop DAT path present")
    entry_args: list[str] = []
    if "recipe" in side:
        r = side["recipe"]
        sel_json = gdir / "sel.json"
        sel_json.write_text(json.dumps({k: v for k, v in r.items() if k != "memory"}), encoding="utf-8")
        entry_bin = gdir / "entry.bin"
        c_res, p_res = run_json(["color-resolve", "--dat", str(dat_path), "--recipe", str(sel_json)])
        cli_ok(c_res, p_res, "color-resolve for display LUT gate")
        entry_bin.write_bytes(bytes.fromhex(p_res["entry"]))
        entry_args = ["--entry", str(entry_bin)]

    args = [
        "source-color-image",
        "--shot",
        str(shot_json),
        "--edit",
        str(side_json),
        "--base",
        "auto",
        "--dat",
        str(dat_path),
        "--calib",
        str(calib.calib_dir(desk.home)),
        "--binding",
        desk.binding(),
        "--cube",
        str(cube_path),
        "--space",
        str(spec_json),
        "--out",
        str(rs_base_npy),
        *entry_args,
    ]
    if dng_path is not None:
        args += ["--dng", str(dng_path)]
    if jpg_path is not None:
        jpg_npy = gdir / "jpg.npy"
        np.save(jpg_npy, mx.decoded(jpg_path))
        args += ["--jpg", str(jpg_npy)]
    code, payload = run_json(args, timeout=600)
    cli_ok(code, payload, "source-color-image for display LUT gate")
    rs_base = np.load(rs_base_npy)

    n_a, n_b = gates.display_gate(gdir, cube_path, cube_spec, np.asarray(b_py), np.asarray(py_base), np.asarray(rs_base), label)
    check(n_a >= 0 and n_b >= 0, "Gate A/B completed")

    # 把 Python color_image 替换成 rs_base 再走一次桌面成片，必须和手机文件逐字节相同。
    orig_ci = serve_mod.color_image
    serve_mod.color_image = lambda *a, **kw: (rs_base, from_raw, base_labels)  # type: ignore[assignment]
    try:
        dest2 = desk.export_dir / f"{label}-rsbase"
        res2 = export_finish(
            desk.render, desk.lib, shot, parse_edit(side), side, dest2, [], quality=quality, long_edge=long_edge, fmt="jpeg", base="auto"
        )
    finally:
        serve_mod.color_image = orig_ci
    same_file_redacted(phone_bytes, res2.path.read_bytes(), "display LUT RAW export matches finish of Gate-B-clean Rust base")
    return False, int(n_b)


def test_mobile_ui_lut_end_to_end_parity_and_lifecycle(tmp_path: Path, pinned_icc: bytes) -> None:  # noqa: F811
    """UI-M4-LUT 第 7 项：导入、空间选择、3 槽短命 Worker GPU 预览、A/B/C/D+JPG-only 导出对拍与删除报错。"""
    from gxr_studio.lut.service import SPACE_LABELS
    from gxr_studio.lut.spaces import SPACES as DESKTOP_SPACES
    from gxr_studio.render.params import parse_edit

    pairs = mx.real_pairs(4)
    if len(pairs) < 4:
        samples.missing(f"need four DNG+JPG pairs in {mx.SAMPLE_DIR}")
    dat = _local_dat()
    _prereq()
    desk = Desktop(tmp_path, dat)
    try:
        luts = _write_synthetic_luts(tmp_path, desk)
        jpg_only = mx.copy_as(pairs[1][1], tmp_path / "R9990002.JPG")
        photos: dict[str, tuple[Path | None, Path | None]] = {dng.stem: (dng, jpg) for dng, jpg in pairs}
        photos[jpg_only.stem] = (None, jpg_only)
        a, b, c, d = (p[0].stem for p in pairs)
        stems = [a, b, c, d, jpg_only.stem]

        files = [str(p) for pair in pairs for p in pair] + [str(jpg_only), str(dat)]
        out_dir = tmp_path / "phone"
        out_dir.mkdir()
        manifest_path = tmp_path / "manifest.json"
        report_path = tmp_path / "report.json"
        manifest = {
            "pack": str(desk.zip),
            "files": files,
            "count": len(stems),
            "stems": {"a": a, "b": b, "c": c, "d": d, "jpgOnly": jpg_only.stem},
            "luts": {k: {"path": v["path"], "cubeId": v["cubeId"]} for k, v in luts.items() if k != "ignore"},
            "ignoreFile": luts["ignore"]["path"],
            "binding": desk.binding(),
            "outDir": str(out_dir),
            "out": str(report_path),
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        proc = _drive(DRIVER, manifest_path, tmp_path, "", timeout=1800)
        check(report_path.is_file(), "LUT browser driver wrote report")
        report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
        check(proc.returncode == 0 and report.get("error") is None, "LUT browser driver finished without error")
        check(report["pageErrors"] == [], "no page errors")

        steps = report["steps"]
        imp = steps["importAndSpaces"]
        check(imp["footnote"] == "LUT 只存在这台 iPhone 上，直接套在照片上，不写入相机。", "03g replacement footnote")
        check(
            "已导入 4 个 LUT" in imp["importStatus"] and "1 个文件不是 .cube，没有导入。" in imp["importStatus"],
            "import summary and ignored count",
        )
        check(imp["dupStatus"] == "已经导入过", "duplicate import notice")
        check(imp["undeclaredLocked"] is True and imp["undeclaredSub"] == "请选择色彩空间", "undeclared LUT locked until space chosen")

        sheet = imp["spaceSheet"]
        same_redacted(sheet["groups"], ["普通照片与视频显示", "相机 Log 与高级空间"], "space sheet groups")
        same_redacted(sheet["spaces"], list(SPACE_LABELS), "space sheet lists all 12 spaces in desktop UI order")
        same_redacted(sorted(sheet["spaces"]), sorted(DESKTOP_SPACES), "space sheet lists exact 12 desktop spaces")
        check(sheet["badge"] == "默认", "undeclared sheet badge")
        check(imp["spaceRowAfterPick"] == "sRGB · 已手选", "03g space row updates after manual choice")
        check(imp["pairADot"] == 1 and imp["pairAPill"].startswith("LUT · "), "pair A tab dot and stage pill")

        # 3 张不同 Display LUT 连切：显卡最多 3 槽，且每次短命 table Worker 都退出
        for row in imp["switchStats"]:
            st = row["stats"]
            check(st["alive"] == 0 and st["pending"] == 0, "short-lived lut_table Worker terminated")
            check(st["terminated"] == st["spawned"], "every spawned lut_table Worker terminated")
            check(st["gpuSlots"] <= 3, "GPU LUT slots capped at 3")
        check(imp["switchStats"][-1]["stats"]["spawned"] >= 3, "at least 3 LUT tables built in short-lived Workers")

        pv = steps["preview"]
        check(pv["jpgLogLocked"] is True and pv["jpgLogSub"] == "需要 RAW 文件", "JPG-only locks Log LUT card")
        check(pv["jpgLogRefused"] == "这个 LUT 需要 RAW 文件", "JPG-only refuses Log LUT with desktop wording")
        check(pv["jpgSelectedAfterLog"] is None, "JPG-only keeps no LUT selected after Log refusal")
        check(pv["jpgDispDot"] == 1, "JPG-only allows display LUT")
        check(" · " in pv["pairBPill"] and "LUT · " in pv["pairBPill"], "pair B stacks recipe and display LUT")
        check("LUT · " in pv["pairCPill"], "pair C shows Log LUT pill")

        # 桌面 sidecar 与导出对拍
        shots = {stem: desk.shot(stem, *photos[stem]) for stem in stems}
        plans = {
            a: {"lut": luts["disp33"]["cubeId"]},
            b: {"card": "builtin:2", "lut": luts["shaper"]["cubeId"]},
            c: {"lut": luts["applelog"]["cubeId"]},
            d: {"lut": luts["applelog"]["cubeId"]},
            jpg_only.stem: {"lut": luts["undeclared"]["cubeId"]},
        }
        sides: dict[str, dict[str, Any]] = {}
        for stem, plan in plans.items():
            changes = desk.changes(shots[stem], plan)
            if "lut" in plan:
                changes["lut"] = plan["lut"]
            sides[stem] = desk.sidecar(shots[stem], changes)

        exports = steps["exports"]
        gate_b_points: dict[str, int] = {}
        byte_equal: dict[str, bool] = {}

        sdr_specs = [
            ("full-", exports["sdrFull"], 95, None),
            ("small-", exports["sdrSmall"], 70, 1600),
        ]
        for tag, step, quality, long_edge in sdr_specs:
            for stem, fact in step["facts"]["facts"].items():
                shot, side = shots[stem], sides[stem]
                same_redacted(parse_edit(fact["side"]).to_json(), parse_edit(side).to_json(), "phone side parse_edit matches desktop")
                same_redacted(_blocks(fact["side"]), _blocks(side), "phone side recipe/adjust matches desktop")
                same_redacted(fact["side"].get("lut"), side.get("lut"), "phone side.lut matches desktop")

                res = desk.export(shot, side, quality, long_edge)
                phone_path = out_dir / f"{tag}{res.path.name}"
                phone_bytes = phone_path.read_bytes() if phone_path.is_file() else b""
                desk_bytes = res.path.read_bytes()

                display_keys = {a: "disp33", b: "shaper", jpg_only.stem: "undeclared"}
                case_id = f"{tag}{'A' if stem == a else 'B' if stem == b else 'C' if stem == c else 'JPG'}"
                if stem in display_keys:
                    # Display LUT (Pair A, Pair B, JPG-only)：逐字节相同，或过 Gate A=0 / Gate B 机械归因且 Rust 底图成片逐字节相同
                    rec = luts[display_keys[stem]]["record"]
                    spec = {
                        "input_space": rec["input_space"],
                        "output_space": rec["output_space"],
                        "input_gamut": rec["input_gamut"],
                        "output_gamut": rec["output_gamut"],
                        "input_range": rec["input_range"],
                        "output_range": rec["output_range"],
                    }
                    exact, nb = _verify_display_raw_gate_b(
                        tmp_path,
                        desk,
                        shot,
                        side,
                        Path(luts[display_keys[stem]]["path"]),
                        spec,
                        phone_bytes,
                        desk_bytes,
                        quality,
                        long_edge,
                        case_id,
                    )
                    byte_equal[case_id] = exact
                    gate_b_points[case_id] = nb
                else:
                    same_file_redacted(phone_bytes, desk_bytes, "phone LUT export equals desktop export_finish (ICC pinned)")
                    byte_equal[case_id] = True
                    gate_b_points[case_id] = 0

                got = {k: fact["result"][k] for k in ("file_name", *RESULT_KEYS)}
                want = {"file_name": res.path.name, **{k: getattr(res, k) for k in RESULT_KEYS}}
                same_redacted(got, want, "SDR LUT result JSON matches desktop ExportResult")

        hdr_specs = [
            ("hdr-full-", exports["hdrFull"], 95, None),
            ("hdr-small-", exports["hdrSmall"], 70, 1600),
        ]
        for tag, step, quality, long_edge in hdr_specs:
            for stem, fact in step["facts"]["facts"].items():
                shot, side = shots[stem], sides[stem]
                same_redacted(parse_edit(fact["side"]).to_json(), parse_edit(side).to_json(), "phone HDR side parse_edit matches desktop")
                res = _desktop_hdr(desk, shot, side, quality, long_edge)
                phone_path = out_dir / f"{tag}{res.path.name}"
                phone_bytes = phone_path.read_bytes() if phone_path.is_file() else b""
                same_file_redacted(phone_bytes, res.path.read_bytes(), "phone Log LUT + HDR export equals desktop (ICC pinned)")
                got = {k: fact["result"].get(k) for k in HDR_FIELDS}
                want = {"file_name": res.path.name, "hdr": res.hdr, "hdr_reason": res.hdr_reason, **{k: getattr(res, k) for k in RESULT_KEYS}}
                same_redacted(got, want, "HDR Log LUT result JSON matches desktop ExportResult")
                case_id = f"{tag}D"
                byte_equal[case_id] = True
                gate_b_points[case_id] = 0

        # 重命名与删除使用中的 LUT
        md = steps["manageAndDelete"]
        check(md["renamedName"] == "胶片颗粒 LUT", "long-press rename updates card label")
        check("LUT 已删除" in md["deletedCardText"], "deleted LUT in use shows LUT 已删除 card")
        check("找不到这个 LUT。请重新导入。" in md["deletedBannerText"], "deleted LUT banner shows desktop not-found wording")
        check(md["deletedFailures"]["staged"] == 0, "deleted LUT never exports silently without LUT")
        check(
            md["deletedFailures"]["failures"] == [{"kind": "core", "message": "找不到这个 LUT。请重新导入。"}],
            "deleted LUT export fails with desktop cube_not_found message",
        )

        gpu_stats = pv["previewMem"]
        hdr_mem = exports["hdrFull"]["facts"]["mem"]
        sdr_mem = exports["sdrFull"]["facts"]["mem"]
        check(0 < hdr_mem["wasm"] <= 640 * MIB, "HDR Log LUT WASM HWM within 640 MiB")
        check(gpu_stats["gpuSlots"] <= 3 and gpu_stats["alive"] == 0, "GPU slots <= 3 and short worker cleaned up")

        note("ui_m4_lut_parity", {"byte_equal": byte_equal, "gate_b_points": gate_b_points})
        note(
            "ui_m4_lut_memory",
            {
                "lut_gpu_mib": round(gpu_stats["peakGpuBytes"] / MIB, 2),
                "lut_js_mib": round(gpu_stats["peakJsBytes"] / MIB, 2),
                "lut_table_wasm_mib": round(gpu_stats["peakWorkerWasmBytes"] / MIB, 2),
                "export_sdr_wasm_mib": round(sdr_mem["wasm"] / MIB, 1),
                "export_sdr_js_mib": round(sdr_mem["js"] / MIB, 1),
                "export_hdr_wasm_mib": round(hdr_mem["wasm"] / MIB, 1),
                "export_hdr_js_mib": round(hdr_mem["js"] / MIB, 1),
                "hash_prefix_len": len(hashlib.sha256(b"0").hexdigest()[:8]),
            },
        )
    finally:
        desk.lib.close()
