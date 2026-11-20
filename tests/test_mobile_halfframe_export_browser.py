"""phone-halfframe-export：手机半格导出模式（双联一张 / 两张单格 / 都要）、g258 信息标签与桌面单格对拍。

照片只经 ``support.samples`` 读取；绝不打印照片文件名、拍摄时间或 EXIF。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image, ImageOps

from support import samples
from support.export_pipeline_cases import check, same_file_redacted
from test_mobile_ui_browser import MODULES, PKG, RUNTIME, _drive, _need
from test_mobile_ui_export_browser import Desktop, _local_dat, pinned_icc  # noqa: F401

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "support" / "mobile_halfframe_export_browser.mjs"
PAIR_EXPORT_NOTE = "双联和相机拼的一样：第 1 张在左（竖拿时在上），中间黑缝 112 像素。"


def _prereq() -> None:
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")


def _sorted_jpgs(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.upper() == ".JPG")


def _capture_dir(*parts: str) -> Path:
    folder = samples.captures().joinpath(*parts)
    if not folder.is_dir():
        samples.missing(f"captures/{'/'.join(parts)} 不在本机")
    return folder


def _run_driver(tmp_path: Path, manifest: dict[str, Any], *, timeout: int = 600) -> dict[str, Any]:
    out = tmp_path / f"{manifest['mode']}-report.json"
    path = tmp_path / f"{manifest['mode']}-manifest.json"
    path.write_text(json.dumps({**manifest, "out": str(out)}), encoding="utf-8")
    result = _drive(DRIVER, path, tmp_path, "", timeout=timeout)
    if result.returncode != 0 or not out.is_file():
        detail = out.read_text(encoding="utf-8") if out.is_file() else (result.stderr or result.stdout)[-400:]
        pytest.fail(f"halfframe export driver failed: {detail}", pytrace=False)
    report: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
    check(report.get("error") is None, f"driver raised: {report.get('error')}")
    check(report.get("pageErrors") == [], f"no page errors: {report.get('pageErrors')}")
    return report


def _has_no_full_black_seam(img: Image.Image) -> bool:
    """确认单格沿原双联接缝轴不含 112px 全黑接缝带（横双联单格 2088×2848 查列，竖双联单格 2848×2088 查行）。"""
    gray = np.asarray(img.convert("L"), dtype=np.uint8)
    w, h = img.size
    seam_axis_max = gray.max(axis=0) if w < h else gray.max(axis=1)
    run = 0
    for v in seam_axis_max:
        run = run + 1 if int(v) <= 12 else 0
        if run >= 80:
            return False
    return True


def test_mobile_halfframe_export_modes_and_prefs_browser(tmp_path: Path) -> None:
    """Acceptance 1: 半格行仅在双联出现；两张单格导出 2 张无黑缝单格；重开记住并用在一键存到相册；都要导出 3 张。"""
    _prereq()
    card250 = _capture_dir("r6-field-20261118", "card250")
    hf256 = _capture_dir("hf256-field-20261120", "card")
    jpgs250 = _sorted_jpgs(card250)
    jpgs256 = _sorted_jpgs(hf256)
    check(len(jpgs250) >= 6 and len(jpgs256) >= 3, "real card250 & hf256 captures available")

    ordinary = jpgs256[2]
    dip_ori1 = jpgs250[1]  # ori=1 diptych (4288x2848 -> 2088x2848 singles)
    dip_ori8 = jpgs250[3]  # ori=8 diptych (2848x4288 -> 2848x2088 singles)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    report = _run_driver(
        tmp_path,
        {
            "mode": "modes",
            "count": 3,
            "files": [str(ordinary), str(dip_ori1), str(dip_ori8)],
            "ordinaryStem": ordinary.stem,
            "diptychOri1Stem": dip_ori1.stem,
            "diptychOri8Stem": dip_ori8.stem,
            "outDir": str(out_dir),
        },
    )

    # 1. Ordinary photo shows NO 半格 row
    check(report["ordinarySheet"]["hasHfRow"] is False, "ordinary photo export options must not show 半格 row")

    # 2. Diptych shows 半格 row with desktop labels & PAIR_EXPORT_NOTE, default diptych, HDR disabled
    def_sheet = report["diptychDefaultSheet"]
    check(def_sheet["hasHfRow"] is True, "diptych export options shows 半格 row")
    check(def_sheet["hfActive"] == "diptych", "default halfframe mode is diptych")
    check(def_sheet["hfLabels"] == ["双联一张", "两张单格", "都要"], "halfframe segment labels match desktop")
    check(def_sheet["hfNote"] == PAIR_EXPORT_NOTE, "PAIR_EXPORT_NOTE matches desktop")
    check(def_sheet["hdrDisabled"] is True, "HDR disabled for JPEG-only diptych")

    # 3. 两张单格 exports 2 portrait 2088x2848 files, _half names, no seam, EXIF preserved
    sin_sheet = report["diptychSinglesSheet"]
    check("2088 × 2848 像素 · 2 个文件" in sin_sheet["sizeLine"], "singles size line matches desktop")
    check("2 张做好了 · 存进相册" in report["singles"]["ready"], "ready bar counts 2 files for singles")
    singles_meta = [f for c in report["singles"]["files"] for f in c["files"]]
    check(len(singles_meta) == 2, "singles produces 2 shared files")
    check(all(f["name"].endswith("_half.jpg") for f in singles_meta), "singles filenames end with _half.jpg")
    check(len({f["name"] for f in singles_meta}) == 2, "singles filenames are distinct")
    for f in singles_meta:
        p = out_dir / f"singles-{f['name']}"
        with Image.open(p) as im:
            check(im.size == (2088, 2848), "ori=1 single is 2088x2848 portrait")
            check(_has_no_full_black_seam(im), "ori=1 single has no black seam")
            exif = im.getexif()
            check(exif.get(274, 1) == 1, "exported single EXIF orientation is 1")
            check(bool(exif.get(271)) or bool(exif.get(272)), "exported single preserves camera EXIF Make/Model")

    # 4. Reload remembers 两张单格, and one-tap 存到相册 on ori=8 diptych exports 2 rotated 2848x2088 files
    check(report["afterReloadSheet"]["hfActive"] == "singles", "halfframe choice remembered across reload")
    check("2 张做好了 · 存进相册" in report["quickOri8"]["ready"], "one-tap uses remembered singles mode (2 files)")
    ori8_meta = [f for c in report["quickOri8"]["files"] for f in c["files"]]
    check(len(ori8_meta) == 2, "one-tap on ori=8 diptych produces 2 files")
    for f in ori8_meta:
        p = out_dir / f"quick-ori8-{f['name']}"
        with Image.open(p) as im:
            check(im.size == (2848, 2088), "ori=8 single is rotated 2848x2088")
            check(_has_no_full_black_seam(im), "ori=8 single has no black seam")

    # 5. 都要 exports 3 files (1 diptych + 2 singles)
    check("3 个文件" in report["diptychBothSheet"]["sizeLine"], "both size line mentions 3 files")
    check("3 张做好了 · 存进相册" in report["both"]["ready"], "ready bar counts 3 files for both")
    both_meta = [f for c in report["both"]["files"] for f in c["files"]]
    check(len(both_meta) == 3, "both mode produces 3 shared files")
    both_sizes = sorted(Image.open(out_dir / f"both-{f['name']}").size for f in both_meta)
    check(both_sizes == [(2088, 2848), (2088, 2848), (4288, 2848)], "both mode outputs 1 diptych + 2 singles")

    # 6. Mixed batch (1 ordinary + 1 diptych in singles mode) -> 3 files
    check(report["mixedSheet"]["hasHfRow"] is True, "mixed selection containing diptych shows 半格 row")
    check("3 张做好了 · 存进相册" in report["mixed"]["ready"], "mixed batch counts 1 + 2 = 3 files")
    mixed_meta = [f for c in report["mixed"]["files"] for f in c["files"]]
    check(len(mixed_meta) == 3, "mixed batch produces 3 files")


def test_mobile_halfframe_g258_labels_browser(tmp_path: Path) -> None:
    """Acceptance 2: hf258 两半不同配方时，顶栏显示「半格双联」，照片信息分别列出第 1 格（在库时）与第 2 格。"""
    _prereq()
    hf258_card = _capture_dir("hf258-field-20261120", "card")
    jpgs = _sorted_jpgs(hf258_card)
    check(len(jpgs) >= 2, "hf258 card has pair (0, 1)")
    p1_jpg, p2_jpg = jpgs[0], jpgs[1]
    p1_dng = p1_jpg.with_suffix(".DNG")
    p2_dng = p2_jpg.with_suffix(".DNG")
    with_first = [str(p) for p in (p1_dng, p1_jpg, p2_dng, p2_jpg) if p.is_file()]
    only_second = [str(p) for p in (p2_dng, p2_jpg) if p.is_file()]

    report = _run_driver(
        tmp_path,
        {
            "mode": "labels",
            "withFirstCount": 2,
            "withFirstFiles": with_first,
            "onlySecondFiles": only_second,
            "diptychStem": p2_jpg.stem,
        },
    )

    # With photo 1 present
    wf = report["withFirst"]
    check(wf["topLabel"] == "半格双联", "top bar shows 半格双联 for diptych")
    check(wf["statusLabel"] == "半格双联", "recipe status pill shows 半格双联")
    rows_wf = wf["info"]["rows"]
    cur_wf = next((r for r in rows_wf if r["label"] == "当前配方"), None)
    check(cur_wf is not None and cur_wf["values"][0]["text"] == "半格双联", "照片信息 当前配方 is 半格双联")
    hf_wf = next((r for r in rows_wf if r["role"] == "halfframe-recipes"), None)
    check(hf_wf is not None, "照片信息 has halfframe shooting recipes row")
    by_half_wf = {v["half"]: v["text"] for v in hf_wf["values"]}
    check("first" in by_half_wf and by_half_wf["first"].startswith("第 1 格："), "lists 第 1 格 when photo 1 is present")
    check("second" in by_half_wf and by_half_wf["second"].startswith("第 2 格："), "lists 第 2 格 from photo 2 tag")
    check("Leica F1" in by_half_wf["first"] and "HC BW" in by_half_wf["second"], "each half shows its own g258 recipe")

    # Without photo 1 (only diptych imported)
    os2 = report["onlySecond"]
    check(os2["topLabel"] == "半格双联", "top bar shows 半格双联 when photo 1 absent")
    hf_alone = next((r for r in os2["info"]["rows"] if r["role"] == "halfframe-recipes"), None)
    check(hf_alone is not None, "照片信息 has halfframe shooting recipes row when alone")
    by_half_alone = {v["half"]: v["text"] for v in hf_alone["values"]}
    check("first" not in by_half_alone, "omits 第 1 格 when photo 1 is absent")
    check("second" in by_half_alone and "HC BW" in by_half_alone["second"], "still lists 第 2 格 when photo 1 is absent")


def _desktop_pair_shot(desk: Desktop, first_jpg: Path, second_jpg: Path) -> dict[str, Any]:
    """构造桌面 halfframe_export 所需的 JPEG-only 半格对 shot 字典（第 1 张只有 JPG，走相机 JPG 单格路径）。"""
    from gxr_studio import halfframe_pairs as hfp

    s1 = desk.shot(first_jpg.stem, None, first_jpg)
    s2 = desk.shot(second_jpg.stem, None, second_jpg)
    with Image.open(second_jpg) as im:
        ori = int(im.getexif().get(274, 1))
    fh = hfp.g250_first_half_for_orientation(ori)
    s1["orientation"] = ori
    s2["orientation"] = ori
    s2["pair"] = {
        "first_half": fh,
        "has_raw": False,
        "first_shot_id": s1["shot_id"],
    }
    s2["pair_sources"] = {
        "first": {**s1, "all_files": dict(s1["files"])},
        "second": {**s2, "all_files": dict(s2["files"])},
    }
    return s2


def test_mobile_halfframe_singles_desktop_parity_browser(tmp_path: Path, pinned_icc: bytes) -> None:  # noqa: F811
    """Acceptance 3: 手机单格与桌面 export_pair_finish 相机 JPG 单格对拍（第 2 格与同源切片第 1 格逐字节一致）。"""
    from gxr_studio.export import write_rgb_with_exif
    from gxr_studio.finish.compose import finish_array
    from gxr_studio.finish.serve import _fit
    from gxr_studio.finish.spec import factory_frame, factory_stamp
    from gxr_studio.halfframe_compose import split_diptych_halves
    from gxr_studio.halfframe_export import export_pair_finish
    from gxr_studio.halfframe_pairs import g250_first_half_for_orientation
    from gxr_studio.render.params import EditState, parse_edit

    _prereq()
    card250 = _capture_dir("r6-field-20261118", "card250")
    jpgs = _sorted_jpgs(card250)
    check(len(jpgs) >= 6, "card250 has ori=1 and ori=6 pairs")
    p1_ori1, p2_ori1 = jpgs[0], jpgs[1]
    p1_ori6, p2_ori6 = jpgs[4], jpgs[5]

    dat = _local_dat()
    desk = Desktop(tmp_path, dat)
    out_dir = tmp_path / "phone"
    out_dir.mkdir()
    try:
        report = _run_driver(
            tmp_path,
            {
                "mode": "parity",
                "count": 2,
                "files": [str(p2_ori1), str(p2_ori6)],
                "ori1Stem": p2_ori1.stem,
                "ori6Stem": p2_ori6.stem,
                "outDir": str(out_dir),
            },
        )

        shot_ori1 = _desktop_pair_shot(desk, p1_ori1, p2_ori1)
        shot_ori6 = _desktop_pair_shot(desk, p1_ori6, p2_ori6)

        def _compare_pair(
            prefix: str,
            phone_step: dict[str, Any],
            shot: dict[str, Any],
            second_jpg: Path,
            side: dict[str, Any],
            quality: int,
            long_edge: int | None,
            mode: str,
        ) -> None:
            edit = parse_edit(side)
            dest = desk.export_dir / f"{prefix.rstrip('-')}"
            desk_out = export_pair_finish(
                desk.render,
                desk.lib,
                shot,
                edit,
                side,
                dest,
                [],
                quality=quality,
                long_edge=long_edge,
                fmt="jpeg",
                base="camera-jpg",
                hdr=False,
                mode=mode,
            )
            by_role = {role: res for _sid, role, res in desk_out}
            phone_files = [f["name"] for c in phone_step["files"] for f in c["files"]]

            # 1) Second half (第 2 格): byte-for-byte identical to desktop export_pair_finish!
            p_second_name = next(n for n in phone_files if n.endswith("_2_gxr_half.jpg"))
            p_second_bytes = (out_dir / f"{prefix}{p_second_name}").read_bytes()
            same_file_redacted(p_second_bytes, by_role["second"].path.read_bytes(), f"{prefix} second single matches desktop byte-for-byte")

            # 2) First half (第 1 格): compare against BOTH desktop pair's first_jpg single (MAD < 1.0)
            #    AND desktop's diptych-sliced single1 (byte-for-byte identical!).
            p_first_name = next(n for n in phone_files if n.endswith("_1_gxr_half.jpg"))
            p_first_bytes = (out_dir / f"{prefix}{p_first_name}").read_bytes()
            with Image.open(out_dir / f"{prefix}{p_first_name}") as im_p, Image.open(by_role["first"].path) as im_d:
                check(im_p.size == im_d.size, f"{prefix} first single dimensions match desktop")
                mad = float(np.mean(np.abs(np.asarray(im_p.convert("RGB"), dtype=np.int16) - np.asarray(im_d.convert("RGB"), dtype=np.int16))))
                check(mad < 1.0, f"{prefix} first single MAD vs desktop first_jpg strip is {mad:.3f} (< 1.0)")

            with Image.open(second_jpg) as im2:
                ori = int(im2.getexif().get(274, 1))
                oriented = np.asarray(ImageOps.exif_transpose(im2).convert("RGB"), dtype=np.uint8)
            s1_dip, _s2_dip = split_diptych_halves(oriented, first_half=g250_first_half_for_orientation(ori), orientation=ori)
            single_edit = EditState(adjust=edit.adjust, curve=edit.curve)
            fin1, _ = finish_array(
                s1_dip,
                single_edit,
                stage="final",
                frame=factory_frame(),
                stamp=factory_stamp(),
                crop_rect=None,
                meta={},
                taken=None,
                file_time=None,
                from_raw=False,
            )
            want_s1_dip = write_rgb_with_exif(_fit(fin1, long_edge), second_jpg, "原图", quality=quality, fmt="jpeg")
            same_file_redacted(p_first_bytes, want_s1_dip, f"{prefix} first single matches desktop diptych-slice pipeline byte-for-byte")

            # 3) In both mode, diptych itself also matches desktop byte-for-byte (with border/adjust applied)
            if mode == "both":
                p_dip_name = next(n for n in phone_files if not n.endswith("_half.jpg"))
                p_dip_bytes = (out_dir / f"{prefix}{p_dip_name}").read_bytes()
                same_file_redacted(
                    p_dip_bytes,
                    by_role["diptych"].path.read_bytes(),
                    f"{prefix} diptych in both mode matches desktop byte-for-byte",
                )

        # Unedited ori=1 & ori=6
        _compare_pair("unedited-ori1-", report["uneditedOri1"], shot_ori1, p2_ori1, {}, 95, None, "singles")
        _compare_pair("unedited-ori6-", report["uneditedOri6"], shot_ori6, p2_ori6, {}, 95, None, "singles")

        # Edited ori=1 (adjust + gallery border): both mode + small/low singles
        knobs = report["editedKnobs"]
        edited_side = {
            "adjust": {"exposure_ev": float(knobs["exposure_ev"]), "contrast": int(knobs["contrast"])},
            "border": {"version": 2, "style": "gallery"},
        }
        _compare_pair("edited-both-", report["editedBoth"], shot_ori1, p2_ori1, edited_side, 95, None, "both")
        _compare_pair("edited-small-", report["editedSmallSingles"], shot_ori1, p2_ori1, edited_side, 70, 1600, "singles")
    finally:
        desk.lib.close()
