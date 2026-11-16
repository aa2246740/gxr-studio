"""UI-M4 第 5 & 第 8 项：裁剪和拉直在无头 Chrome（SwiftShader WebGPU）里走一遍（``browser_serial``）。

- 第 5 项：真实 DNG 与相机 JPG，在所有比例预设、横竖向及拉直下，核对 ``Engine.finalSize()`` 与
  ``export_output_sizes`` 完全相同，并核对配方卡显示裁剪小图。
- 第 8 项：A（XPan 横向 + 1.5°）、B（1:1）、C（小红书 3:4 竖向）、D（自由拖角 + 拖框移动）、
  只有 JPG 的副本（16:9 + -3°）、E（开 HDR + XPan），导出 原始尺寸 / 高、小 / 低 和 HDR，
  与桌面 ``write_crop`` / ``edits.write`` / ``export_finish`` 逐字节相同；并核对拉直尺拖动进入 03d 布局、
  rAF 帧记录、重选照片保留裁剪、清空后丢弃。

照片只经 ``support.samples`` 找；比较一律走 ``support.export_pipeline_cases`` 的脱敏函数，不打印照片数据。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from support import mobile_ui_export as mx
from support import samples
from support.export_pipeline_cases import check, note, same_file_redacted, same_redacted
from test_mobile_ui_browser import MODULES, PKG, RUNTIME, _drive, _need
from test_mobile_ui_export_browser import RESULT_KEYS, Desktop, _local_dat, pinned_icc

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "support" / "mobile_ui_crop_browser.mjs"

__all__ = ["pinned_icc"]


def _prereq() -> None:
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")


def _run_driver(tmp_path: Path, manifest: dict[str, Any], *, timeout: int = 1500) -> dict[str, Any]:
    out = tmp_path / f"{manifest['mode']}-report.json"
    mpath = tmp_path / f"{manifest['mode']}-manifest.json"
    mpath.write_text(json.dumps({**manifest, "out": str(out)}), encoding="utf-8")
    result = _drive(DRIVER, mpath, tmp_path, "", timeout=timeout)
    debug = os.environ.get("GXR_M4_DEBUG")
    if debug:
        Path(debug, f"{manifest['mode']}-driver.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode != 0 or not out.is_file():
        pytest.fail("crop browser driver failed (set GXR_M4_DEBUG=<folder> to keep its log)", pytrace=False)
    report: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
    check(report.get("error") is None, "crop driver step raised (first error line is in report file)")
    return report


def _crop_floats(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    return {k: ([float(v) for v in val] if k == "rect" else (float(val) if k == "angle_deg" else val)) for k, val in raw.items()}


def test_mobile_ui_crop_gpu_final_size_parity(tmp_path: Path) -> None:
    """UI-M4 第 5 项：真实 DNG 和相机 JPG 在每档比例 / 横竖 / 拉直下的 Engine.finalSize() 对齐 final_box 与 export_output_sizes。"""
    from gxr_studio.finish.sizes import output_sizes as desktop_output_sizes
    from gxr_studio.finish.spec import Geometry
    from gxr_studio.gpu.box import final_box as desktop_final_box
    from gxr_studio.render.params import parse_edit

    pairs = mx.real_pairs(2)
    if len(pairs) < 2:
        samples.missing(f"need two DNG+JPG pairs in {mx.SAMPLE_DIR}")
    dat = _local_dat()
    _prereq()
    desk = Desktop(tmp_path, dat)
    try:
        dng, jpg = pairs[0]
        jpg_only = mx.copy_as(pairs[1][1], tmp_path / "R9990002.JPG")
        shot_dng = desk.shot(dng.stem, dng, jpg)
        shot_jpg = desk.shot(jpg_only.stem, None, jpg_only)
        # output_sizes -> open_media 使用 O_NOFOLLOW；把临时 library 里的链接换成测试目录内副本（样本旁不写文件）。
        for src in (dng, jpg, jpg_only):
            target = desk.folder / src.name
            if target.is_symlink():
                target.unlink()
                shutil.copy2(src, target)
        shots = {dng.stem: shot_dng, jpg_only.stem: shot_jpg}
        files = [str(dng), str(jpg), str(jpg_only), str(dat)]
        report = _run_driver(
            tmp_path,
            {
                "mode": "finalSize",
                "pack": str(desk.zip),
                "files": files,
                "count": 2,
                "stems": [dng.stem, jpg_only.stem],
            },
            timeout=900,
        )
        check(report["pageErrors"] == [], "no page errors")
        rows = report["steps"]["finalSize"]
        check(len(rows) == 28, "14 cases x 2 photos")
        for row in rows:
            shot = shots[row["stem"]]
            side = row["side"] or {}
            edit = parse_edit(side)
            geom = edit.geometry if isinstance(edit.geometry, Geometry) else Geometry()
            crop = side.get("crop")
            rect = tuple(crop["rect"]) if crop else None
            preset = crop["preset"] if crop else None
            sizes = desktop_output_sizes(desk.render, desk.lib, shot, edit, rect, preset)
            want_export = list(sizes["sizes"]["original"])
            fs = row["finalSize"]
            ls = row["layerSize"]
            check(fs is not None and ls is not None, "GPU engine returned finalSize() and layerSize()")
            got = [fs["w"], fs["h"]] if isinstance(fs, dict) else fs
            layer_w, layer_h = int(ls["w"]), int(ls["h"])
            (bx, by, bw, bh), _ = desktop_final_box(layer_w, layer_h, geom, rect, preset)
            same_redacted(got, [bw, bh], f"finalSize == desktop final_box(layerSize) ({row['preset']}, {row['orient']}, {row['angle']})")
            if row["stem"] == jpg_only.stem:
                same_redacted(got, want_export, f"JPG finalSize == export_output_sizes ({row['preset']}, {row['orient']}, {row['angle']})")
            else:
                close = abs(got[0] * 2 - want_export[0]) <= 2 and abs(got[1] * 2 - want_export[1]) <= 2
                check(close, "DNG half-res Engine.finalSize() * 2 tracks export_output_sizes within 2 px")
        check(report["steps"][f"croppedCards_{dng.stem}"] > 0, "recipe strip rendered cropped card thumbnails")
    finally:
        desk.lib.close()


def test_mobile_ui_crop_end_to_end_parity(tmp_path: Path, pinned_icc: bytes) -> None:  # noqa: F811
    """UI-M4 第 8 项：裁剪和拉直端到端导出与桌面逐字节相同，含 HDR、小图、rAF 拖动与重选/清空。"""
    from gxr_studio.finish.serve import export_finish
    from gxr_studio.library import edits as crop_edits
    from gxr_studio.render import edits as render_edits
    from gxr_studio.render.params import parse_edit
    from test_mobile_ui_export_hdr_browser import HDR_FIELDS, _desktop_hdr

    pairs = mx.real_pairs(4)
    if len(pairs) < 4:
        samples.missing(f"need four DNG+JPG pairs in {mx.SAMPLE_DIR}")
    dat = _local_dat()
    _prereq()
    desk = Desktop(tmp_path, dat)
    try:
        jpg_only = mx.copy_as(pairs[1][1], tmp_path / "R9990002.JPG")
        hdr_dng = mx.copy_as(pairs[0][0], tmp_path / "R9990005.DNG")
        hdr_jpg = mx.copy_as(pairs[0][1], tmp_path / "R9990005.JPG")

        photos: dict[str, tuple[Path | None, Path | None]] = {dng.stem: (dng, jpg) for dng, jpg in pairs}
        photos[jpg_only.stem] = (None, jpg_only)
        photos[hdr_dng.stem] = (hdr_dng, hdr_jpg)

        a, b, c, d = (p[0].stem for p in pairs)
        plans = [
            {"stem": a, "preset": "xpan-65-24", "orient": "landscape", "angleDeg": 1.5, "recordRaf": True},
            {"stem": b, "preset": "1:1"},
            {"stem": c, "preset": "4:3", "orient": "portrait"},
            {"stem": d, "preset": "free", "dragBox": True},
            {"stem": jpg_only.stem, "preset": "16:9", "orient": "landscape", "angleDeg": -3.0},
            {"stem": hdr_dng.stem, "preset": "xpan-65-24", "orient": "landscape"},
        ]
        files = [str(p) for pair in pairs for p in pair] + [str(jpg_only), str(hdr_dng), str(hdr_jpg), str(dat)]
        out_dir = tmp_path / "phone"
        out_dir.mkdir()

        report = _run_driver(
            tmp_path,
            {
                "mode": "parity",
                "pack": str(desk.zip),
                "files": files,
                "count": len(photos),
                "plans": plans,
                "sdrOrder": [a, b, c, d, jpg_only.stem],
                "smallOrder": [a, d],
                "hdrStem": hdr_dng.stem,
                "repickPairA": [str(pairs[0][0]), str(pairs[0][1])],
                "binding": desk.binding(),
                "outDir": str(out_dir),
            },
            timeout=1800,
        )
        check(report["pageErrors"] == [], "no page errors")
        steps = report["steps"]

        # rAF 拖动记录 + 03d 收起版式切换 + 自由拖框不翻页 + 重选保留裁剪 + 清空丢掉
        drag = steps["dragStats"]
        check(drag["dragAttr"] == "1" and drag["duringDragAttr"] == "1" and drag["afterDragAttr"] == "0", "03d drag dock state toggled")
        check(drag["rafCount"] >= 1, "straighten drag recorded rAF frames")
        check(steps["freeDrag"]["stayedSamePhoto"] is True, "dragging inside crop box does not swipe photos")
        check(steps["repickCrop"] is not None and steps["repickCrop"]["preset"] == "xpan-65-24", "re-picking photo kept crop")
        check(steps["repickGeom"]["straighten"] == 1.5, "re-picking photo kept straighten")
        check(steps["afterClearCount"] == 0, "clearing photos dropped crops")

        # 桌面照同样的编辑写入 sidecar
        shots = {stem: desk.shot(stem, *photos[stem]) for stem in photos}
        edit_by_stem = {row["stem"]: row for row in steps["edits"]}
        menu = desk.render.recipe_menu()
        sides: dict[str, dict[str, Any]] = {}
        for plan in plans:
            stem = plan["stem"]
            shot = shots[stem]
            phone_edit = edit_by_stem[stem]
            angle = float((phone_edit.get("geometry") or {}).get("straighten") or 0.0)
            if angle:
                render_edits.write(
                    desk.lib,
                    shot,
                    {"geometry": {"straighten": angle, "vertical": 0, "horizontal": 0, "distortion": 0, "rotate90": 0, "flip": False}},
                    menu,
                )
            crop = phone_edit.get("crop")
            if crop is not None:
                # 自由框是真实拖动产生的位置，桌面 write_crop 使用同一个框坐标
                rect = crop["rect"] if plan.get("dragBox") else None
                crop_edits.write_crop(
                    desk.lib,
                    shot,
                    preset=crop["preset"],
                    rect=rect,
                    orientation=plan.get("orient"),
                    fit=False,
                )
            _e, sides[stem] = render_edits.read(desk.lib, shot)

        table_rows: list[dict[str, Any]] = []
        # SDR 原始尺寸 / 高 + 小 / 低
        for tag, step, quality, long_edge in (("full-", steps["full"], 95, None), ("small-", steps["small"], 70, 1600)):
            for stem, fact in step["facts"].items():
                shot, side = shots[stem], sides[stem]
                same_redacted(parse_edit(fact["side"]).to_json(), parse_edit(side).to_json(), f"{tag}{stem} parse_edit")
                same_redacted(_crop_floats(fact["side"].get("crop")), _crop_floats(side.get("crop")), f"{tag}{stem} crop")
                same_redacted(fact["side"].get("frame"), side.get("frame"), f"{tag}{stem} frame")

                dest = desk.export_dir / f"{stem}-{quality}-{long_edge}"
                res = export_finish(
                    desk.render,
                    desk.lib,
                    shot,
                    parse_edit(side),
                    side,
                    dest,
                    [],
                    quality=quality,
                    long_edge=long_edge,
                    fmt="jpeg",
                    base="auto",
                )
                phone_path = out_dir / f"{tag}{res.path.name}"
                phone_bytes = phone_path.read_bytes() if phone_path.is_file() else None
                desk_bytes = res.path.read_bytes()
                same_file_redacted(phone_bytes, desk_bytes, f"{tag} file matches desktop export_finish (ICC pinned)")
                got = {k: fact["result"][k] for k in ("file_name", *RESULT_KEYS)}
                want = {"file_name": res.path.name, **{k: getattr(res, k) for k in RESULT_KEYS}}
                same_redacted(got, want, f"{tag} result JSON matches desktop ExportResult")
                table_rows.append(
                    {
                        "batch": tag.rstrip("-"),
                        "preset": (side.get("crop") or {}).get("preset"),
                        "hdr": False,
                        "bytes": len(desk_bytes),
                        "equal": True,
                    }
                )

        # HDR 原始尺寸 / 高（pair E）
        hdr_facts = steps["hdr"]["facts"]
        for stem, fact in hdr_facts.items():
            shot, side = shots[stem], sides[stem]
            same_redacted(parse_edit(fact["side"]).to_json(), parse_edit(side).to_json(), "hdr parse_edit")
            same_redacted(_crop_floats(fact["side"].get("crop")), _crop_floats(side.get("crop")), "hdr crop")
            same_redacted(fact["side"].get("frame"), side.get("frame"), "hdr frame")
            res = _desktop_hdr(desk, shot, side, 95, None)
            phone_path = out_dir / f"hdr-{res.path.name}"
            phone_bytes = phone_path.read_bytes() if phone_path.is_file() else None
            desk_bytes = res.path.read_bytes()
            same_file_redacted(phone_bytes, desk_bytes, "hdr file matches desktop export_finish(hdr=True) (ICC pinned)")
            got = {k: fact["result"].get(k) for k in HDR_FIELDS}
            want = {"file_name": res.path.name, "hdr": res.hdr, "hdr_reason": res.hdr_reason, **{k: getattr(res, k) for k in RESULT_KEYS}}
            same_redacted(got, want, "hdr result JSON matches desktop ExportResult")
            table_rows.append(
                {
                    "batch": "hdr",
                    "preset": (side.get("crop") or {}).get("preset"),
                    "hdr": bool(res.hdr),
                    "bytes": len(desk_bytes),
                    "equal": True,
                }
            )

        check(len(table_rows) == 8, "5 full SDR + 2 small SDR + 1 HDR = 8 exports verified byte-for-byte")
        note("ui_m4_crop_parity", {"count": len(table_rows), "rows": table_rows})
    finally:
        desk.lib.close()
