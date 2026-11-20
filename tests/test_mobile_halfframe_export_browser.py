"""phone-halfframe-export：手机半格导出模式（双联一张 / 两张单格 / 都要）、g258 信息标签与桌面单格对拍。

照片只经 ``support.samples`` 读取；绝不打印照片文件名、拍摄时间或 EXIF。
"""

from __future__ import annotations

import io
import json
import shutil
import struct
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from support import samples
from support.export_pipeline_cases import check, same_file_redacted
from support.lut_cases import cube_text
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


def _patch_exif_orientation(src_jpg: Path, dst_jpg: Path, new_ori: int) -> None:
    """就地改写 JPEG APP1 Exif IFD0 的 Orientation（0x0112），保持其余字节与 APP2 MPF 偏移完全不变。"""
    data = bytearray(src_jpg.read_bytes())
    pos = 2
    patched = False
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        while pos < len(data) and data[pos] == 0xFF:
            pos += 1
        if pos >= len(data):
            break
        marker = data[pos]
        pos += 1
        if marker in (0xDA, 0xD9):
            break
        seg_len = (data[pos] << 8) | data[pos + 1]
        payload_start = pos + 2
        payload_end = pos + seg_len
        if marker == 0xE1 and data[payload_start : payload_start + 6] == b"Exif\x00\x00":
            tiff = payload_start + 6
            endian = "<" if data[tiff : tiff + 2] == b"II" else ">"
            ifd_off = struct.unpack_from(endian + "I", data, tiff + 4)[0]
            ifd_pos = tiff + ifd_off
            count = struct.unpack_from(endian + "H", data, ifd_pos)[0]
            for i in range(count):
                ep = ifd_pos + 2 + i * 12
                tag = struct.unpack_from(endian + "H", data, ep)[0]
                if tag == 0x0112:
                    struct.pack_into(endian + "H", data, ep + 8, new_ori)
                    patched = True
                    break
            break
        pos = payload_end
    check(patched, "patched EXIF Orientation tag in place")
    dst_jpg.write_bytes(data)
    with Image.open(dst_jpg) as im:
        check(int(im.getexif().get(274, 1)) == new_ori, "patched copy has expected EXIF orientation")


def _make_ori3_pair(p1_ori1: Path, p2_ori1: Path, out_dir: Path) -> tuple[Path, Path]:
    """由真实 card250 ori=1 对在临时目录构造合法的 ori=3 对（像素旋转 180°、EXIF=3、第 1 张含标准 APP2 MPF 640×424 预览）。"""
    from gxr_studio.halfframe_pairs import detect_first_like

    out_dir.mkdir(parents=True, exist_ok=True)
    p1_dst = out_dir / "R9900001.JPG"
    p2_dst = out_dir / "R9900002.JPG"

    # 1. Photo 2 (diptych): rotate raw 180° + set EXIF orientation=3
    with Image.open(p2_ori1) as im2:
        exif2 = im2.getexif()
        exif2[274] = 3
        rot2 = im2.transpose(Image.Transpose.ROTATE_180)
        rot2.save(p2_dst, format="JPEG", quality=95, subsampling=0, exif=exif2.tobytes())

    # 2. Photo 1 (first-like): rotate raw 180° + set EXIF orientation=3 + attach valid APP2 MPF 640x424 preview
    with Image.open(p1_ori1) as im1:
        exif1 = im1.getexif()
        exif1[274] = 3
        rot1 = im1.transpose(Image.Transpose.ROTATE_180)
        buf_main = io.BytesIO()
        rot1.save(buf_main, format="JPEG", quality=95, subsampling=0, exif=exif1.tobytes())
        main_jpg = buf_main.getvalue()
        im1.seek(1)
        prev_im = im1.convert("RGB").transpose(Image.Transpose.ROTATE_180)
        buf_prev = io.BytesIO()
        prev_im.save(buf_prev, format="JPEG", quality=90)
        prev_jpg = buf_prev.getvalue()

    # Build APP2 MPF segment (little-endian TIFF): 3 IFD entries (0xB000 Version, 0xB001 Count=2, 0xB002 MPEntry[2])
    # Header: 4 ("MPF\0") + 8 (TIFF hdr) + 2 (count) + 3*12 (entries) + 4 (next_ifd=0) + 32 (2 MPEntries) = 86 B
    ifd_entries_offset = 8
    mp_entry_val_offset = 8 + 2 + 3 * 12 + 4  # 50
    app2_payload_len = 4 + mp_entry_val_offset + 32  # 86
    app2_total_len = 2 + 2 + app2_payload_len  # 90 (FF E2 + 2-byte len + payload)
    mpf_base_in_file = 2 + 2 + 2 + 4  # SOI(2) + FFE2(2) + len(2) + "MPF\0"(4) = 10
    main_with_app2_len = len(main_jpg) + app2_total_len
    prev_offset_from_mpf = main_with_app2_len - mpf_base_in_file

    mpf_tiff = bytearray()
    mpf_tiff += b"II" + struct.pack("<HI", 42, ifd_entries_offset)
    mpf_tiff += struct.pack("<H", 3)
    mpf_tiff += struct.pack("<HHI", 0xB000, 7, 4) + b"0100"
    mpf_tiff += struct.pack("<HHII", 0xB001, 4, 1, 2)
    mpf_tiff += struct.pack("<HHII", 0xB002, 7, 32, mp_entry_val_offset)
    mpf_tiff += struct.pack("<I", 0)
    # Entry 0: primary image; Entry 1: MPF preview
    mpf_tiff += struct.pack("<IIIHH", 0x030000, main_with_app2_len, 0, 0, 0)
    mpf_tiff += struct.pack("<IIIHH", 0x010002, len(prev_jpg), prev_offset_from_mpf, 0, 0)
    app2_payload = b"MPF\x00" + bytes(mpf_tiff)
    app2_seg = b"\xff\xe2" + struct.pack(">H", len(app2_payload) + 2) + app2_payload
    p1_dst.write_bytes(main_jpg[:2] + app2_seg + main_jpg[2:] + prev_jpg)
    check(detect_first_like(p1_dst) is True, "derived ori=3 photo 1 passes desktop detect_first_like")
    return p1_dst, p2_dst


def test_mobile_halfframe_export_modes_and_prefs_browser(tmp_path: Path) -> None:
    """Acceptance 1: 半格行仅在双联出现；两张单格导出 2 张桌面命名无黑缝单格；重开记住并用在一键存到相册；都要导出 3 张。"""
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

    # 3. 两张单格 (unpaired) exports desktop-named <p2>_1_gxr_half.jpg & <p2>_gxr_half.jpg
    sin_sheet = report["diptychSinglesSheet"]
    check("2088 × 2848 像素 · 2 个文件" in sin_sheet["sizeLine"], "singles size line matches desktop")
    check("2 张做好了 · 存进相册" in report["singles"]["ready"], "ready bar counts 2 files for singles")
    singles_meta = [f for c in report["singles"]["files"] for f in c["files"]]
    check(len(singles_meta) == 2, "singles produces 2 shared files")
    singles_names = sorted(f["name"] for f in singles_meta)
    check(
        singles_names == sorted([f"{dip_ori1.stem}_1_gxr_half.jpg", f"{dip_ori1.stem}_gxr_half.jpg"]),
        "unpaired singles filenames match desktop (<p2>_1_gxr_half.jpg & <p2>_gxr_half.jpg)",
    )
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
    check(
        sorted(f["name"] for f in ori8_meta) == sorted([f"{dip_ori8.stem}_1_gxr_half.jpg", f"{dip_ori8.stem}_gxr_half.jpg"]),
        "unpaired ori=8 singles filenames match desktop",
    )
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
    """Acceptance 2 + Items 1/6: hf258 顶栏与照片信息含/不含第 1 张、缺第 1 张剥离 Copyright、方向不匹配同时在信息与导出拒绝。"""
    _prereq()
    hf258_card = _capture_dir("hf258-field-20261120", "card")
    jpgs = _sorted_jpgs(hf258_card)
    check(len(jpgs) >= 2, "hf258 card has pair (0, 1)")
    p1_jpg, p2_jpg = jpgs[0], jpgs[1]
    p1_dng = p1_jpg.with_suffix(".DNG")
    p2_dng = p2_jpg.with_suffix(".DNG")
    with_first = [str(p) for p in (p1_dng, p1_jpg, p2_dng, p2_jpg) if p.is_file()]
    only_second = [str(p) for p in (p2_dng, p2_jpg) if p.is_file()]

    # Patch a copy of photo 1 from ori=1 -> ori=6 (same stem, valid MPF) for orientation-mismatch rejection test.
    mismatch_dir = tmp_path / "mismatch"
    mismatch_dir.mkdir()
    p1_ori6 = mismatch_dir / p1_jpg.name
    _patch_exif_orientation(p1_jpg, p1_ori6, 6)
    mismatch_files = [str(p1_ori6), str(p2_jpg)]

    out_dir = tmp_path / "out"
    out_dir.mkdir()

    report = _run_driver(
        tmp_path,
        {
            "mode": "labels",
            "withFirstCount": 2,
            "withFirstFiles": with_first,
            "onlySecondFiles": only_second,
            "mismatchCount": 2,
            "mismatchFiles": mismatch_files,
            "diptychStem": p2_jpg.stem,
            "outDir": str(out_dir),
        },
    )

    # 1. With matching ori=1 photo 1 present
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

    # 2. Without photo 1 (only diptych imported): omit 第 1 格, export strips Copyright from 第 1 格 while keeping Make/Model & 第 2 格 tag
    os2 = report["onlySecond"]
    check(os2["topLabel"] == "半格双联", "top bar shows 半格双联 when photo 1 absent")
    hf_alone = next((r for r in os2["info"]["rows"] if r["role"] == "halfframe-recipes"), None)
    check(hf_alone is not None, "照片信息 has halfframe shooting recipes row when alone")
    by_half_alone = {v["half"]: v["text"] for v in hf_alone["values"]}
    check("first" not in by_half_alone, "omits 第 1 格 when photo 1 is absent")
    check("second" in by_half_alone and "HC BW" in by_half_alone["second"], "still lists 第 2 格 when photo 1 is absent")

    def _check_unpaired_export(prefix: str, export_step: dict[str, Any]) -> None:
        names = [f["name"] for c in export_step["files"] for f in c["files"]]
        first_name = f"{p2_jpg.stem}_1_gxr_half.jpg"
        second_name = f"{p2_jpg.stem}_gxr_half.jpg"
        check(sorted(names) == sorted([first_name, second_name]), f"{prefix} uses unpaired desktop filenames")
        with Image.open(out_dir / f"{prefix}{first_name}") as im1:
            ex1 = im1.getexif()
            check(ex1.get(33432) is None, f"{prefix} unpaired first single strips EXIF Copyright (0x8298)")
            check(bool(ex1.get(271)) or bool(ex1.get(272)), f"{prefix} unpaired first single keeps Make/Model")
            check(bool(ex1.get(306)), f"{prefix} unpaired first single keeps DateTime")
        with Image.open(out_dir / f"{prefix}{second_name}") as im2:
            ex2 = im2.getexif()
            copy2 = str(ex2.get(33432) or "")
            check(copy2.startswith("GXR1 "), f"{prefix} second single keeps photo 2 GXR1 Copyright tag")

    _check_unpaired_export("alone-", os2["export"])

    # 3. Orientation-mismatched neighbour (ori=6 photo 1 vs ori=1 diptych): rejected in BOTH 照片信息 AND singles export!
    mm = report["oriMismatch"]
    hf_mm = next((r for r in mm["info"]["rows"] if r["role"] == "halfframe-recipes"), None)
    check(hf_mm is not None, "照片信息 has halfframe shooting recipes row on ori mismatch")
    by_half_mm = {v["half"]: v["text"] for v in hf_mm["values"]}
    check("first" not in by_half_mm, "orientation-mismatched photo 1 omitted from 照片信息")
    check("second" in by_half_mm and "HC BW" in by_half_mm["second"], "第 2 格 still shown on ori mismatch")
    _check_unpaired_export("mismatch-", mm["export"])


def _desktop_pair_shot(desk: Desktop, first_jpg: Path, second_jpg: Path) -> dict[str, Any]:
    """构造桌面 halfframe_export 所需的 JPEG-only 半格对 shot 字典（走相机 JPG 单格路径）。"""
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
    """Acceptance 3 + Items 1/2/3/7: 与桌面 export_pair_finish 真实产出逐字节对拍。"""
    from gxr_studio.halfframe_export import export_pair_finish
    from gxr_studio.render.params import parse_edit

    _prereq()
    card250 = _capture_dir("r6-field-20261118", "card250")
    hf258 = _capture_dir("hf258-field-20261120", "card")
    jpgs250 = _sorted_jpgs(card250)
    jpgs258 = _sorted_jpgs(hf258)
    check(len(jpgs250) >= 6 and len(jpgs258) >= 2, "card250 (ori 1/6/8) & hf258 pairs available")
    p1_ori1, p2_ori1 = jpgs250[0], jpgs250[1]
    p1_ori8, p2_ori8 = jpgs250[2], jpgs250[3]
    p1_ori6, p2_ori6 = jpgs250[4], jpgs250[5]
    p1_hf258, p2_hf258 = jpgs258[0], jpgs258[1]
    p1_ori3, p2_ori3 = _make_ori3_pair(p1_ori1, p2_ori1, tmp_path / "ori3")

    dat = _local_dat()
    desk = Desktop(tmp_path, dat)
    out_dir = tmp_path / "phone"
    out_dir.mkdir()

    # Synthetic sRGB 33^3 display-space .cube LUT registered in desktop & imported into mobile UI.
    lut_txt = cube_text(
        lambda x: np.clip(x * np.array([1.04, 0.99, 0.93]) + np.array([0.01, 0.0, -0.01]), 0.0, 1.0),
        size=33,
        title="Halfframe Display 33",
        comments="# Input: sRGB\n",
    )
    lut_raw = lut_txt.encode("utf-8")
    lut_path = tmp_path / "hf_disp33.cube"
    lut_path.write_bytes(lut_raw)
    lut_rec = desk.render.luts.add(lut_raw, lut_path.name)
    lut_cube_id = str(lut_rec["cube_id"])

    try:
        all_files = [
            str(p1_hf258),
            str(p2_hf258),
            str(p1_ori1),
            str(p2_ori1),
            str(p1_ori6),
            str(p2_ori6),
            str(p1_ori8),
            str(p2_ori8),
            str(p1_ori3),
            str(p2_ori3),
        ]
        report = _run_driver(
            tmp_path,
            {
                "mode": "parity",
                "count": len(all_files),
                "files": all_files,
                "hf258Stem": p2_hf258.stem,
                "ori1Stem": p2_ori1.stem,
                "ori6Stem": p2_ori6.stem,
                "ori8Stem": p2_ori8.stem,
                "ori3Stem": p2_ori3.stem,
                "lutPath": str(lut_path),
                "lutCubeId": lut_cube_id,
                "outDir": str(out_dir),
            },
        )

        shot_hf258 = _desktop_pair_shot(desk, p1_hf258, p2_hf258)
        shot_ori1 = _desktop_pair_shot(desk, p1_ori1, p2_ori1)
        shot_ori6 = _desktop_pair_shot(desk, p1_ori6, p2_ori6)
        shot_ori8 = _desktop_pair_shot(desk, p1_ori8, p2_ori8)
        shot_ori3 = _desktop_pair_shot(desk, p1_ori3, p2_ori3)

        def _compare_pair(
            prefix: str,
            phone_step: dict[str, Any],
            shot: dict[str, Any],
            first_jpg: Path,
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

            want_first_name = f"{first_jpg.stem}_gxr_half.jpg"
            want_second_name = f"{second_jpg.stem}_gxr_half.jpg"
            check(want_first_name in phone_files, f"{prefix} first filename matches desktop (<p1>_gxr_half.jpg)")
            check(want_second_name in phone_files, f"{prefix} second filename matches desktop (<p2>_gxr_half.jpg)")
            check(by_role["first"].path.name == want_first_name, f"{prefix} desktop first filename sanity check")
            check(by_role["second"].path.name == want_second_name, f"{prefix} desktop second filename sanity check")

            # Strict byte-for-byte parity on BOTH first & second singles against desktop export_pair_finish!
            same_file_redacted(
                (out_dir / f"{prefix}{want_first_name}").read_bytes(),
                by_role["first"].path.read_bytes(),
                f"{prefix} first single matches desktop export_pair_finish byte-for-byte",
            )
            same_file_redacted(
                (out_dir / f"{prefix}{want_second_name}").read_bytes(),
                by_role["second"].path.read_bytes(),
                f"{prefix} second single matches desktop export_pair_finish byte-for-byte",
            )

            if mode == "both":
                want_dip_name = by_role["diptych"].path.name
                check(want_dip_name in phone_files, f"{prefix} diptych filename matches desktop")
                same_file_redacted(
                    (out_dir / f"{prefix}{want_dip_name}").read_bytes(),
                    by_role["diptych"].path.read_bytes(),
                    f"{prefix} diptych in both mode matches desktop byte-for-byte",
                )

        # 1. Mixed-recipe hf258 pair: byte-equal + verify photo 1 & photo 2 keep distinct EXIF Copyright & capture time!
        _compare_pair("hf258-singles-", report["hf258Singles"], shot_hf258, p1_hf258, p2_hf258, {}, 95, None, "singles")
        with (
            Image.open(p1_hf258) as src1,
            Image.open(p2_hf258) as src2,
            Image.open(out_dir / f"hf258-singles-{p1_hf258.stem}_gxr_half.jpg") as out1,
            Image.open(out_dir / f"hf258-singles-{p2_hf258.stem}_gxr_half.jpg") as out2,
        ):
            c1, c2 = str(out1.getexif().get(33432) or ""), str(out2.getexif().get(33432) or "")
            check(c1 == str(src1.getexif().get(33432) or "") and c1 != c2, "paired 第 1 格 keeps photo 1 Copyright tag, distinct from 第 2 格")
            check(c2 == str(src2.getexif().get(33432) or ""), "paired 第 2 格 keeps photo 2 Copyright tag")
            check(out1.getexif().get(306) == src1.getexif().get(306), "paired 第 1 格 keeps photo 1 capture time")
            check(out2.getexif().get(306) == src2.getexif().get(306), "paired 第 2 格 keeps photo 2 capture time")

        # 2. Unedited pairs across all 4 valid orientations (1, 3, 6, 8)
        _compare_pair("unedited-ori1-", report["uneditedOri1"], shot_ori1, p1_ori1, p2_ori1, {}, 95, None, "singles")
        _compare_pair("unedited-ori6-", report["uneditedOri6"], shot_ori6, p1_ori6, p2_ori6, {}, 95, None, "singles")
        _compare_pair("unedited-ori8-", report["uneditedOri8"], shot_ori8, p1_ori8, p2_ori8, {}, 95, None, "singles")
        _compare_pair("unedited-ori3-", report["uneditedOri3"], shot_ori3, p1_ori3, p2_ori3, {}, 95, None, "singles")

        # 3. Edited card250 ori=1: adjust (exposure + contrast + vignette) + curve + gallery border
        knobs = report["editedKnobs"]
        req_curve = next((it["side"].get("curve") for it in report["editedBoth"]["reqSides"] if it["side"].get("curve")), None)
        check(req_curve is not None, "curve drag produced a non-empty curve in export request")
        edited_side: dict[str, Any] = {
            "adjust": {
                "exposure_ev": float(knobs["exposure_ev"]),
                "contrast": int(knobs["contrast"]),
                "vignette": int(knobs["vignette"]),
            },
            "curve": req_curve,
            "border": {"version": 2, "style": "gallery"},
        }
        _compare_pair("edited-both-", report["editedBoth"], shot_ori1, p1_ori1, p2_ori1, edited_side, 95, None, "both")
        _compare_pair("edited-small-", report["editedSmallSingles"], shot_ori1, p1_ori1, p2_ori1, edited_side, 70, 1600, "singles")

        # 4. Display-space .cube LUT on hf258 pair -> singles (byte-equal or Gate A=0 + Gate B proven 1-LSB tie + byte-equal encode)
        from gxr_studio.export import write_rgb_with_exif
        from gxr_studio.halfframe_compose import camera_jpg_singles
        from gxr_studio.halfframe_pairs import g250_first_half_for_orientation
        from gxr_studio.lut.photo import _apply as lut_apply
        from support import export_source_lut as lut_gates
        from support.core_cli import run_npy

        lut_side = {"lut": lut_cube_id}
        lut_desk_out = export_pair_finish(
            desk.render,
            desk.lib,
            shot_hf258,
            parse_edit(lut_side),
            lut_side,
            desk.export_dir / "lut-singles",
            [],
            quality=95,
            long_edge=None,
            fmt="jpeg",
            base="camera-jpg",
            hdr=False,
            mode="singles",
        )
        lut_by_role = {role: res for _sid, role, res in lut_desk_out}
        lut_phone_files = [f["name"] for c in report["lutSingles"]["files"] for f in c["files"]]
        lut_spec = {"input_space": "srgb", "output_space": "srgb"}
        lut_target, lut_item = desk.render.luts.target(lut_cube_id)
        lut_note = f"GXR Studio computer prediction: LUT {lut_cube_id}, {lut_item['input_space']} to {lut_item['output_space']}"
        base_s1, base_s2 = camera_jpg_singles(
            p1_hf258,
            p2_hf258,
            first_half=g250_first_half_for_orientation(1),
            orientation=1,
        )
        for role, src_jpg, base_rgb in (("first", p1_hf258, base_s1), ("second", p2_hf258, base_s2)):
            want_name = f"{src_jpg.stem}_gxr_half.jpg"
            check(want_name in lut_phone_files, f"lut-singles- {role} filename matches desktop")
            phone_bytes = (out_dir / f"lut-singles-{want_name}").read_bytes()
            desk_bytes = lut_by_role[role].path.read_bytes()
            if phone_bytes != desk_bytes:
                gdir = tmp_path / f"lut-gate-{role}"
                gdir.mkdir()
                py_lut = lut_apply(lut_target, base_rgb)
                base_npy = gdir / "base.npy"
                np.save(base_npy, base_rgb)
                rs_f64 = run_npy(
                    ["lut-apply", "--cube", str(lut_path), "--space", json.dumps(lut_spec), "--rgb", str(base_npy)],
                    gdir / "rs-f64.npy",
                    timeout=600,
                )
                rs_u8 = np.floor(np.clip(rs_f64, 0, 255) + 0.5).astype(np.uint8)
                n_a, n_b = lut_gates.display_gate(gdir, lut_path, lut_spec, base_rgb, py_lut, rs_u8, f"hf-lut-{role}")
                check(n_a == 0 and n_b >= 0, f"lut-singles- {role} Gate A=0 (n_a={n_a}, n_b={n_b})")
                want_rs_bytes = write_rgb_with_exif(rs_u8, src_jpg, lut_note, quality=95, fmt="jpeg")
                same_file_redacted(phone_bytes, want_rs_bytes, f"lut-singles- {role} matches Gate-B-clean desktop encode byte-for-byte")
    finally:
        desk.lib.close()
