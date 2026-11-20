"""g257/g258 DAT 读卡、半格异配方实拍（桌面标签 / 服务端渲染 / GPU 异烘焙 / 导出+HDR / 一起调）与现场配对回归测试。"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import struct
import urllib.parse
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageOps

import hdr_support as hs
from conftest import PORT
from gpu_gate import evaluate, valid_mask
from gpu_support import run_engine
from gxr_studio import halfframe_compose as hfc
from gxr_studio.app import create_app
from gxr_studio.camdata import calib, intake, knob_table, store
from gxr_studio.camdata import datcodec as dc
from gxr_studio.cards.discovery import find_cards
from gxr_studio.cards.importer import ImportOptions, run_import
from gxr_studio.engine import menu as py_menu
from gxr_studio.finish.spec import Geometry
from gxr_studio.library.store import Library
from gxr_studio.raw import Renderer
from gxr_studio.render.params import EditState, RecipeSel, parse_adjust, parse_curve, parse_recipe
from gxr_studio.render.recipes import CameraData
from gxr_studio.render.service import RenderService
from support import mobile_ui_export as mx
from support import samples
from support.core_cli import error_of, require_cli, run_json, run_npy
from support.export_pipeline_cases import check, same_file_redacted, same_redacted
from support.wasm_check import run_area
from test_camdata_api import client as client
from test_camdata_api import reader as reader
from test_core_parity_camdata import _assert_parse_matches_python_error, _expected_parse_summary
from test_datcodec import reseal
from test_g250 import KUBIK_LIB_SHA256, R5_DAT_DRIVER, _browser_prereq, _need_g185_or_g250, _need_kubik_bundle
from test_g256 import _eval_web_gallery_and_recipes, _stage_bundle
from test_halfframe_p0 import _clear_desktop_cache, _sorted_pairs, _stage_volume_pairs
from test_halfframe_p1a_field import _index_shots_by_jpg_sha, _phase_shift_and_corr, _seam_and_halves, _seam_core
from test_halfframe_p1b_gpu_field import (
    PAIR_HARNESS_JS,
    _enable_gpu_flag,
    _fetch_bake,
    _python_pair_reference,
)
from test_halfframe_p1b_gpu_field import (
    _seam_slice as _gpu_seam_slice,
)
from test_halfframe_p1c_field import _seam_slice as _hdr_seam_slice
from test_mobile_ui_browser import _drive

__all__ = ["client", "reader"]

G257_DAT_SHA256 = "7eb47d8930ce18e27da6af01333aa44b021acecc58891c74bb288f50300cee41"
G258_DAT_SHA256 = "19ca420837d6b48d4c8837d71f4cf1414743e08e2da37349b31daf3c027777ac"

_DAT_SPECS: dict[int, tuple[str, str, int, int]] = {
    257: ("firmware/package_v310b_hf_v4_r1/local/test257-REVIEW_PENDING", G257_DAT_SHA256, 87032, 3896),
    258: ("firmware/package_v310b_hf_adj_keep_r1/local/test258-REVIEW_PENDING", G258_DAT_SHA256, 87144, 3896),
}


def _need_g257_or_g258(generation: int) -> tuple[bytes, bytes, Path, Path]:
    rel_dir, want_dat, _eng_len, _xdata_len = _DAT_SPECS[generation]
    parent = samples.sample_parent()
    dat_path = parent / rel_dir / "GXRCE.DAT"
    fx_path = parent / "firmware/package_v310b_kubik_lib_r6/local/test-REVIEW_PENDING/GXRFX.BIN"
    if not dat_path.is_file():
        samples.missing(f"g{generation} 本地 DAT 不在本机")
    if not fx_path.is_file():
        samples.missing("KUBIK 本地测试库不在本机")
    raw = dat_path.read_bytes()
    library = fx_path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == want_dat, f"g{generation} DAT 摘要已变化"
    assert hashlib.sha256(library).hexdigest() == KUBIK_LIB_SHA256, "KUBIK 库摘要已变化"
    return raw, library, dat_path, fx_path


def _hf258_field_folder() -> Path:
    folder = samples.captures() / "hf258-field-20261120" / "card"
    if not folder.is_dir():
        samples.missing("captures/hf258-field-20261120/card 不在本机")
    return folder


def _setup_g258_calibrated_home(settings: Any) -> None:
    raw, library, _, _ = _need_g257_or_g258(258)
    store.import_from_dat(raw, settings.home_dir, library=library)
    sources = calib.dev_sources(samples.sample_parent())
    if not sources:
        samples.missing("研究目录里的标定文件不在本机")
    calib.import_calibration(settings.home_dir, sources)


def _chroma_stats(rgb: np.ndarray) -> tuple[float, float]:
    """返回每像素 max(R,G,B) - min(R,G,B) 的均值与 95 分位，用来区分彩色半格与黑白半格。"""
    arr = rgb.astype(np.int16)
    chroma = arr.max(axis=2) - arr.min(axis=2)
    return float(chroma.mean()), float(np.percentile(chroma, 95))


def _assert_color_half(rgb: np.ndarray, label: str) -> None:
    mean_c, p95_c = _chroma_stats(rgb)
    assert mean_c >= 6.0 and p95_c >= 18.0, f"{label}: expected color half, got chroma mean={mean_c:.2f}, p95={p95_c:.2f}"


def _assert_bw_half(rgb: np.ndarray, label: str) -> None:
    mean_c, p95_c = _chroma_stats(rgb)
    assert mean_c <= 1.5 and p95_c <= 3.0, f"{label}: expected monochrome half, got chroma mean={mean_c:.2f}, p95={p95_c:.2f}"


def _leica_f1_recipe(home_dir: Path) -> dict[str, Any]:
    cam = CameraData(home_dir)
    mount = next(row for row in cam.listing() if row["id"] == "l08firm")
    rec = next(r for r in mount["recipes"] if r.get("kind") == "record" and r["name"] == "Leica F1")
    return {"kind": "record", "unit": "l08firm", "slot": rec["slot"], "id": rec["id"], "levels": {}}


def _exif_desc(raw: bytes) -> str:
    with Image.open(io.BytesIO(raw)) as im:
        val = im.getexif().get(270, "")
    return val.decode("utf-8", errors="replace") if isinstance(val, bytes) else str(val)


# --------------------------------------------------------------------------- 1. g257 / g258 DAT 读卡、绑定与三边对拍


@pytest.mark.parametrize("generation", (257, 258))
def test_g257_and_g258_decode_round_trip_and_intake(generation: int, tmp_path: Path, client: Any) -> None:
    raw, kubik_lib, _, _ = _need_g257_or_g258(generation)
    raw250, _, _, _ = _need_kubik_bundle()
    _, want_sha, want_eng_len, want_xdata_len = _DAT_SPECS[generation]

    info = intake.inspect_dat(raw, kubik_lib)
    assert info.generation == generation
    assert info.size == len(raw)
    assert info.sha256 == want_sha

    dat = dc.parse(raw, kubik_lib)
    assert dat.generation == generation
    assert dc.pack(dat) == raw
    assert dat.library == kubik_lib
    assert len(dat.engine) == want_eng_len and 81920 < len(dat.engine) <= 98304
    assert len(dat.xdata) == want_xdata_len
    assert len(dat.recipes) == 66
    assert all(len(m.entries) == 8 for m in dat.modules)

    dat_copy, _fx_copy = _stage_bundle(tmp_path, f"bundle-{generation}", raw, kubik_lib)
    read_raw, read_name, read_lib = intake.read_bundle_from_path(dat_copy)
    assert read_name == "GXRCE.DAT" and read_raw == raw and read_lib == kubik_lib

    home = tmp_path / f"home-{generation}"
    store.import_from_dat(raw, home, library=kubik_lib)
    cam = CameraData(home)

    home250 = tmp_path / f"home-250-ref-{generation}"
    store.import_from_dat(raw250, home250, library=kubik_lib)
    cam250 = CameraData(home250)

    for unit_idx, unit_listing in enumerate(cam.listing()):
        recipes = unit_listing["recipes"]
        assert len(recipes) == 26
        assert [r["name"] for r in recipes[-3:]] == ["K-0815", "K-PACIFIC", "K-KINO"]
        for item in recipes:
            sel = RecipeSel(unit_listing["id"], item["slot"], kind=item.get("kind", "builtin"), id=item.get("id"))
            assert cam.resolve(sel).entry == cam250.resolve(sel).entry, (generation, unit_idx, item["position"])

    resp = client.post("/api/camdata/import-upload", content=raw + kubik_lib, headers={"X-GXR-Dat-Bytes": str(len(raw))})
    assert resp.status_code == 200, resp.text
    assert "28mm 26 条" in resp.json()["message"]


@pytest.mark.parametrize("generation", (257, 258))
def test_g257_and_g258_reject_wrong_library_and_envelope_tamper(generation: int, tmp_path: Path) -> None:
    raw, kubik_lib, _, _ = _need_g257_or_g258(generation)
    _, lib185, _, fx185_path = _need_g185_or_g250(185)
    _, lib250_old, _, fx250_old_path = _need_g185_or_g250(250)

    for bad_lib in (None, lib185, lib250_old):
        with pytest.raises(intake.IntakeError) as exc_info:
            intake.inspect_dat(raw, bad_lib)
        assert exc_info.value.code != "generation_unsupported"

    xdata_at, xdata_len = struct.unpack_from(">II", raw, 128 + 12 * 2 + 4)
    assert xdata_len > 0
    bad_xdata = bytearray(raw)
    bad_xdata[xdata_at] ^= 0x01
    bad_xdata_bytes = reseal(bytes(bad_xdata))
    with pytest.raises(dc.DatError) as exc_xdata:
        dc.parse(bad_xdata_bytes, kubik_lib)
    assert not isinstance(exc_xdata.value, dc.UnsupportedGeneration)

    bad_sec3 = bytearray(raw)
    struct.pack_into(">II", bad_sec3, 128 + 12 * 3 + 4, len(raw), 4)
    bad_sec3 += b"\x00\x00\x00\x00"
    for idx in range(4, 7):
        struct.pack_into(">II", bad_sec3, 128 + 12 * idx + 4, len(bad_sec3), 0)
    bad_sec3_bytes = reseal(bytes(bad_sec3))
    with pytest.raises(dc.DatError) as exc_sec3:
        dc.parse(bad_sec3_bytes, kubik_lib)
    assert not isinstance(exc_sec3.value, dc.UnsupportedGeneration)

    bad_ram = bytearray(raw)
    struct.pack_into(">I", bad_ram, 116, dc.MAX_RAM - len(raw) + 4)
    with pytest.raises(dc.DatError) as exc_ram:
        dc.parse(reseal(bytes(bad_ram)), kubik_lib)
    assert not isinstance(exc_ram.value, dc.UnsupportedGeneration)

    require_cli()
    _, fx_kubik_copy = _stage_bundle(tmp_path, f"neg-{generation}", raw, kubik_lib)
    for path, blob, fx in (
        (tmp_path / f"g{generation}_wrong_lib185.dat", raw, fx185_path),
        (tmp_path / f"g{generation}_wrong_lib250.dat", raw, fx250_old_path),
        (tmp_path / f"g{generation}_bad_xdata.dat", bad_xdata_bytes, fx_kubik_copy),
        (tmp_path / f"g{generation}_bad_sec3.dat", bad_sec3_bytes, fx_kubik_copy),
    ):
        path.write_bytes(blob)
        _assert_parse_matches_python_error(path, blob, fx)


@pytest.mark.parametrize("generation", (257, 258))
def test_rust_cli_and_wasm_parity_for_g257_and_g258(generation: int, tmp_path: Path) -> None:
    require_cli()
    raw, library, _, _ = _need_g257_or_g258(generation)
    dat_path, fx_path = _stage_bundle(tmp_path, f"parity-{generation}", raw, library)

    expected, entries = _expected_parse_summary(raw, library)
    out_dir = tmp_path / f"entries-{generation}"
    code, payload = run_json(["camdata-parse", "--dat", str(dat_path), "--library", str(fx_path), "--out-dir", str(out_dir)])
    assert error_of(code, payload) is None
    assert payload == expected
    assert len(payload["recipes"]) == 66

    arr = run_npy(["camdata-entries", "--dat", str(dat_path), "--library", str(fx_path)], tmp_path / f"entries-{generation}.npy")
    expected_arr = np.frombuffer(b"".join(entries[(u, s)] for u in range(3) for s in store.SLOTS), dtype=np.uint8).reshape(24, 552)
    assert np.array_equal(arr, expected_arr)

    dat = dc.parse(raw, library)
    assert dat.kv2_block is not None
    expected_menu = knob_table.read(dat.kv2_block, py_menu.load(), mount_knobs_from_50mm=dc.PROFILES[generation].mount_knobs_from_50mm)
    mcode, mpayload = run_json(["camdata-menu", "--dat", str(dat_path), "--library", str(fx_path)])
    assert error_of(mcode, mpayload) is None
    assert mpayload == expected_menu

    wasm_dir = tmp_path / f"wasm-{generation}"
    wasm_dir.mkdir()
    proc = run_area("camdata", [dat_path], wasm_dir)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.browser_serial
def test_mobile_browser_reads_g258_card(tmp_path: Path) -> None:
    pair = mx.real_pairs(1)
    if len(pair) < 1:
        samples.missing("need one DNG+JPG pair")
    _browser_prereq()
    raw, library, dat_path, fx_path = _need_g257_or_g258(258)
    dng, jpg = pair[0]

    home = tmp_path / "home-258-browser"
    store.import_from_dat(raw, home, library=library)
    cam = CameraData(home)
    qlaunch = next(row for row in cam.listing() if row["id"] == "qlaunch")
    kino = next(r for r in qlaunch["recipes"] if r.get("kind") == "record" and r["name"] == "K-KINO")
    extra_recipe_dict = {"kind": "record", "unit": "qlaunch", "slot": kino["slot"], "id": kino["id"], "levels": {}}

    report_path = tmp_path / "g258-report.json"
    manifest: dict[str, Any] = {
        "dng": str(dng),
        "jpg": str(jpg),
        "dat": str(dat_path),
        "bin": str(fx_path),
        "files": [str(dng), str(jpg), str(dat_path), str(fx_path)],
        "extraRecipe": extra_recipe_dict,
        "out": str(report_path),
    }
    mpath = tmp_path / "g258-manifest.json"
    mpath.write_text(json.dumps(manifest), encoding="utf-8")
    result = _drive(R5_DAT_DRIVER, mpath, tmp_path, "", timeout=420)
    if result.returncode != 0 or not report_path.is_file():
        pytest.fail("r6 browser driver failed for g258")
    report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    check(report.get("error") is None, "driver finished cleanly")
    check(report.get("pageErrors") == [], "no page errors")
    assert "缺少 GXRFX.BIN" in report["sheet"]["text"]
    for step_key in ("second", "loaded"):
        phone = report[step_key]
        assert phone["entries"] == 24 * 552
        assert phone["generation"] == 258
        assert phone["library"] == len(library)
        same_redacted(phone["listing"], cam.listing(), "g258 phone listing equals desktop listing")
        sel = parse_recipe({"kind": "builtin", "unit": "jlaunch", "slot": 1, "levels": {}})
        assert sel is not None
        resolved = cam.resolve(sel)
        same_file_redacted(base64.b64decode(phone["entry"]), resolved.entry, "g258 resolve entry")
        same_redacted(phone["levels"], resolved.levels, "g258 resolve levels")
        same_redacted(phone["sha"], resolved.entry_sha256, "g258 resolve sha")
        extra_sel = parse_recipe(extra_recipe_dict)
        assert extra_sel is not None
        extra_resolved = cam.resolve(extra_sel)
        same_file_redacted(base64.b64decode(phone["extra"]["entry"]), extra_resolved.entry, "g258 K-KINO resolve entry")
        same_redacted(phone["extra"]["levels"], extra_resolved.levels, "g258 K-KINO resolve levels")
        same_redacted(phone["extra"]["sha"], extra_resolved.entry_sha256, "g258 K-KINO resolve sha")


# --------------------------------------------------------------------------- 2. hf258 现场配对、未成对第 3 张与图库折叠/标签


def test_hf258_pairing_unpaired_coldstart_and_gallery(
    settings: Any,
    lib: Library,
    volumes: Path,
) -> None:
    """导入 hf258-field-20261120/card 5 张：(#1,#2) MOUNT 异配方与 (#4,#5) 50mm 配对，#3 冷启动独照保持未配对。"""
    _clear_desktop_cache()
    field_pairs = _sorted_pairs(_hf258_field_folder())
    assert len(field_pairs) == 5
    _stage_volume_pairs(volumes, field_pairs)
    _setup_g258_calibrated_home(settings)

    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    by_idx = _index_shots_by_jpg_sha(lib, field_pairs)
    assert len(by_idx) == 5

    # Pair (#1, #2): MOUNT MT 25 (Leica F1) + MT 03 (HC BW)
    # Pair (#4, #5): 50mm 50 01 (GRPos) + 50 01 (GRPos)
    for label, i1, i2, want_unit, p1_pos, p2_pos in (
        ("MOUNT", 1, 2, "MT", 25, 3),
        ("50mm", 4, 5, "50", 1, 1),
    ):
        s1, s2 = by_idx[i1], by_idx[i2]
        assert s2["diptych"] is True, f"{label}: second shot (#{i2}) not detected as diptych"
        assert s1.get("pair") is not None and s2.get("pair") is not None
        assert s1["pair"]["role"] == "first" and s1["pair"]["partner_id"] == s2["shot_id"]
        assert s2["pair"]["role"] == "second" and s2["pair"]["partner_id"] == s1["shot_id"]
        assert s2["pair"]["first_half"] == "left" and s2["pair"]["kind"] == "g250"
        assert s2["has_raw"] == 1 and "dng" not in s2["files"]
        assert s1["recipe_tag"]["unit"] == want_unit and s1["recipe_tag"]["position"] == p1_pos
        assert s2["recipe_tag"]["unit"] == want_unit and s2["recipe_tag"]["position"] == p2_pos

    # #3: 50mm 冷启动拍后切回放，必须保持未配对 half_first
    s3 = by_idx[3]
    assert s3["diptych"] is False
    assert "pair" not in s3
    assert bool(s3.get("half_first")) is True
    assert s3["recipe_tag"]["unit"] == "50" and s3["recipe_tag"]["position"] == 1
    lib.close()

    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as cli:
        rows = cli.get("/api/photos?limit=100").json()
        assert len(rows) == 5
        gallery = _eval_web_gallery_and_recipes(rows)
        assert gallery["visibleCount"] == 3
        assert gallery["visiblePairCount"] == 2
        assert gallery["hfOnlyCount"] == 2
        assert gallery["pairCount"] == 2

        labels_by_id = {item["id"]: item["label"] for item in gallery["pairLabels"]}
        sid2 = by_idx[2]["shot_id"]
        sid5 = by_idx[5]["shot_id"]
        assert labels_by_id[sid2] == "拍摄时：第 1 张 Leica F1 · 第 2 张 HC BW"
        assert labels_by_id[sid5] == "拍摄时：第 1 张 GRPos · 第 2 张 GRPos"

        # 未改动异配方对的 /edit 必须保持 recipe=None，且 render 不得把第 2 张的 unit/slot/hashes 当成整对值（F2）
        ed2 = cli.get(f"/api/photos/{sid2}/edit").json()
        assert ed2["recipe"] is None and ed2["recipe_from_camera"] is False
        r2 = ed2["render"]
        assert r2["available"] is True and r2["prediction"] is True and r2["calibrated"] is True
        for key in ("unit", "slot", "defaulted", "effective_levels", "entry_sha256", "record_sha256", "binding_sha256", "quality", "model_iso"):
            assert r2[key] is None, (key, r2[key])
        assert len(r2["halves"]) == 2
        assert r2["halves"][0]["unit"] == "l08firm" and r2["halves"][1]["unit"] == "l08firm"
        assert r2["halves"][1]["slot"] == 3 and r2["halves"][0]["entry_sha256"] != r2["halves"][1]["entry_sha256"]

        # 同配方对 (#4/#5) 仍保持原有单配方 render 结构不变
        r5 = cli.get(f"/api/photos/{sid5}/edit").json()["render"]
        assert r5["unit"] == "jlaunch" and r5["slot"] == 1 and r5["entry_sha256"] and "halves" not in r5


# --------------------------------------------------------------------------- 3. 异配方实拍：服务端渲染、导出(+HDR)与用户一起调


def test_hf258_different_recipe_pair_server_export_hdr_and_user_pick(
    settings: Any,
    lib: Library,
    volumes: Path,
    tmp_path: Path,
) -> None:
    """实拍 MOUNT 异配方对 (#1 Leica F1 彩 + #2 HC BW 黑白)：服务端渲染、三种导出(含 HDR)保各自配方；选配方后两半一起变。"""
    _clear_desktop_cache()
    field_pairs = _sorted_pairs(_hf258_field_folder())
    assert len(field_pairs) == 5
    _stage_volume_pairs(volumes, field_pairs[:2])
    _setup_g258_calibrated_home(settings)

    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    by_idx = _index_shots_by_jpg_sha(lib, field_pairs[:2])
    shot2 = by_idx[2]
    sid2 = shot2["shot_id"]
    ori = int(shot2.get("orientation") or 1)
    assert shot2["pair"]["role"] == "second" and shot2["pair"]["first_half"] == "left"

    cam_dip = np.asarray(ImageOps.exif_transpose(Image.open(field_pairs[1][1])).convert("RGB"), dtype=np.uint8)
    cam_left, _, cam_right = _seam_and_halves(cam_dip)
    _assert_color_half(cam_left, "camera diptych left")
    _assert_bw_half(cam_right, "camera diptych right")

    # 1) 服务端 RAW 合成：确认两半解析出不同条目，且左彩右黑白与相机双联严格对齐
    rsvc = RenderService(settings.home_dir, lib)
    plan1, plan2, _s1, _s2, _e1, _e2 = hfc.pair_plans(rsvc, shot2, EditState())
    assert plan1.entry_sha256 != plan2.entry_sha256
    assert plan1.unit == "l08firm" and plan2.unit == "l08firm"
    assert plan2.slot == 3

    out_rgb, _labels = hfc.render_pair_rgb(rsvc, lib, shot2, EditState(), full=True)
    assert out_rgb.shape == cam_dip.shape
    out_left, out_seam, out_right = _seam_and_halves(out_rgb)
    assert int(out_seam.max()) == 0
    sl, corrl = _phase_shift_and_corr(out_left, cam_left)
    sr, corrr = _phase_shift_and_corr(out_right, cam_right)
    assert sl == (0, 0) and sr == (0, 0) and corrl >= 0.98 and corrr >= 0.98, (sl, sr, corrl, corrr)
    _assert_color_half(out_left, "server full left")
    _assert_bw_half(out_right, "server full right")
    lib.close()

    leica_f1 = _leica_f1_recipe(settings.home_dir)
    hcbw = {"kind": "builtin", "unit": "l08firm", "slot": 3, "levels": {}}

    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as cli:
        # /render API 预览同样左彩右黑白
        r_prev = cli.post(
            f"/api/photos/{sid2}/render?stage=final&max_edge=2144&quality=95",
            json={"recipe": None, "adjust": {}, "curve": None},
        )
        assert r_prev.status_code == 200, r_prev.text
        with Image.open(io.BytesIO(r_prev.content)) as im_prev:
            prev_rgb = np.asarray(im_prev.convert("RGB"), dtype=np.uint8)
        prev_w = prev_rgb.shape[1]
        _assert_color_half(prev_rgb[:, : prev_w // 2 - 40], "render api preview left")
        _assert_bw_half(prev_rgb[:, prev_w // 2 + 40 :], "render api preview right")

        # 2) 未改动状态下三种导出模式（diptych / singles / both），同时验证 SDR 与 HDR
        for mode in ("diptych", "singles", "both"):
            sdr_dir = tmp_path / f"exp_{mode}_sdr"
            hdr_dir = tmp_path / f"exp_{mode}_hdr"
            sdr_dir.mkdir()
            hdr_dir.mkdir()
            r_sdr = cli.post(
                f"/api/photos/{sid2}/export",
                json={"halfframe": mode, "size": "original", "quality_tier": "high", "hdr": False, "dest_dir": str(sdr_dir)},
            )
            r_hdr = cli.post(
                f"/api/photos/{sid2}/export",
                json={"halfframe": mode, "size": "original", "quality_tier": "high", "hdr": True, "dest_dir": str(hdr_dir)},
            )
            assert r_sdr.status_code == 200, r_sdr.text
            assert r_hdr.status_code == 200, r_hdr.text
            b_sdr = r_sdr.json()
            b_hdr = r_hdr.json()

            if mode == "diptych":
                sdr_items = [{"role": "diptych", **b_sdr}]
                hdr_items = [{"role": "diptych", **b_hdr}]
            else:
                sdr_items = b_sdr["files"]
                hdr_items = b_hdr["files"]
            expected_roles = {"diptych": ["diptych"], "singles": ["first", "second"], "both": ["diptych", "first", "second"]}[mode]
            assert [x["role"] for x in sdr_items] == expected_roles
            assert [x["role"] for x in hdr_items] == expected_roles

            sdr_by_role = {x["role"]: x for x in sdr_items}
            sdr_bytes = {role: Path(x["path"]).read_bytes() for role, x in sdr_by_role.items()}
            hdr_bytes = {x["role"]: Path(x["path"]).read_bytes() for x in hdr_items}

            # 验证像素各半保留各自配方，且双联 EXIF 270 列出左右两半配方并省略单一 recipe_binding（F1）
            if "diptych" in sdr_bytes:
                assert sdr_by_role["diptych"].get("recipe_binding") is None
                dip_desc = _exif_desc(sdr_bytes["diptych"])
                assert dip_desc.startswith("GXR Studio computer prediction (not camera-measured): ")
                assert "left:" in dip_desc and "| right:" in dip_desc
                assert f"entry SHA256 {plan1.entry_sha256}" in dip_desc and f"entry SHA256 {plan2.entry_sha256}" in dip_desc
                assert dip_desc.index(plan1.entry_sha256) < dip_desc.index(plan2.entry_sha256)
                with Image.open(io.BytesIO(sdr_bytes["diptych"])) as im:
                    assert im.size == (4288, 2848)
                    arr = np.asarray(im.convert("RGB"), dtype=np.uint8)
                hl, seam_c, hr = _seam_core(arr)
                assert int(seam_c.max()) <= 6
                assert _phase_shift_and_corr(hl, cam_left)[1] >= 0.98
                assert _phase_shift_and_corr(hr, cam_right)[1] >= 0.98
                _assert_color_half(hl, f"export {mode} diptych left")
                _assert_bw_half(hr, f"export {mode} diptych right")
            if "first" in sdr_bytes:
                d1, d2 = _exif_desc(sdr_bytes["first"]), _exif_desc(sdr_bytes["second"])
                assert plan1.entry_sha256 in d1 and plan2.entry_sha256 not in d1
                assert plan2.entry_sha256 in d2 and plan1.entry_sha256 not in d2
                with Image.open(io.BytesIO(sdr_bytes["first"])) as im1, Image.open(io.BytesIO(sdr_bytes["second"])) as im2:
                    assert im1.size == (2088, 2848) and im2.size == (2088, 2848)
                    a1 = np.asarray(im1.convert("RGB"), dtype=np.uint8)
                    a2 = np.asarray(im2.convert("RGB"), dtype=np.uint8)
                assert _phase_shift_and_corr(a1, cam_left)[1] >= 0.98
                assert _phase_shift_and_corr(a2, cam_right)[1] >= 0.98
                _assert_color_half(a1, f"export {mode} single first")
                _assert_bw_half(a2, f"export {mode} single second")

            # HDR 检查：确认容器有效、接缝增益 0、both 单格增益匹配双联切片
            for item in hdr_items:
                assert item["hdr"] is True, f"{mode} {item['role']} hdr failed: {item.get('hdr_note')}"
                chk = hs.check_file(sdr_bytes[item["role"]], hdr_bytes[item["role"]])
                assert chk["ok"], (mode, item["role"], chk["fail"])
            if "diptych" in hdr_bytes:
                dip_chk = hs.check_file(sdr_bytes["diptych"], hdr_bytes["diptych"])
                seam = _hdr_seam_slice(dip_chk["log_gain"], ori)
                seam_inner = seam[:, 6:-6] if ori in (1, 3) else seam[6:-6, :]
                assert float(seam_inner.max()) == 0.0
                if mode == "both":
                    h1_g, h2_g = hfc.split_diptych_halves(dip_chk["log_gain"], first_half="left", orientation=ori)
                    for role, dip_slice in (("first", h1_g), ("second", h2_g)):
                        s_chk = hs.check_file(sdr_bytes[role], hdr_bytes[role])
                        diff = np.abs(s_chk["log_gain"][2:-2, 2:-2] - dip_slice[2:-2, 2:-2])
                        assert float(np.median(diff)) < 0.08 and float(np.percentile(diff, 95)) < 0.25

        # 3) 用户显式选配方（Q2 一起调）：选 HC BW → 两半全黑白；选 Leica F1 → 两半全彩色
        put_bw = cli.put(f"/api/photos/{sid2}/edit", json={"recipe": hcbw})
        assert put_bw.status_code == 200
        bw_render = put_bw.json()["render"]
        assert bw_render["unit"] == "l08firm" and bw_render["slot"] == 3 and bw_render["entry_sha256"] == plan2.entry_sha256
        assert "halves" not in bw_render
        bw_dir = tmp_path / "exp_picked_hcbw"
        bw_dir.mkdir()
        r_bw = cli.post(
            f"/api/photos/{sid2}/export",
            json={"halfframe": "both", "size": "original", "quality_tier": "high", "hdr": False, "dest_dir": str(bw_dir)},
        )
        assert r_bw.status_code == 200, r_bw.text
        bw_files = {x["role"]: Path(x["path"]) for x in r_bw.json()["files"]}
        bw_dip_desc = _exif_desc(bw_files["diptych"].read_bytes())
        assert "left:" not in bw_dip_desc and plan2.entry_sha256 in bw_dip_desc and plan1.entry_sha256 not in bw_dip_desc
        with Image.open(bw_files["diptych"]) as im:
            bw_l, _, bw_r = _seam_and_halves(np.asarray(im.convert("RGB"), dtype=np.uint8))
        _assert_bw_half(bw_l, "picked HC BW diptych left")
        _assert_bw_half(bw_r, "picked HC BW diptych right")
        with Image.open(bw_files["first"]) as im1, Image.open(bw_files["second"]) as im2:
            _assert_bw_half(np.asarray(im1.convert("RGB"), dtype=np.uint8), "picked HC BW single first")
            _assert_bw_half(np.asarray(im2.convert("RGB"), dtype=np.uint8), "picked HC BW single second")

        assert cli.put(f"/api/photos/{sid2}/edit", json={"recipe": leica_f1}).status_code == 200
        col_dir = tmp_path / "exp_picked_leica_f1"
        col_dir.mkdir()
        r_col = cli.post(
            f"/api/photos/{sid2}/export",
            json={"halfframe": "both", "size": "original", "quality_tier": "high", "hdr": False, "dest_dir": str(col_dir)},
        )
        assert r_col.status_code == 200, r_col.text
        col_files = {x["role"]: Path(x["path"]) for x in r_col.json()["files"]}
        with Image.open(col_files["diptych"]) as im:
            col_l, _, col_r = _seam_and_halves(np.asarray(im.convert("RGB"), dtype=np.uint8))
        _assert_color_half(col_l, "picked Leica F1 diptych left")
        _assert_color_half(col_r, "picked Leica F1 diptych right")
        assert _phase_shift_and_corr(col_l, cam_left)[1] >= 0.98
        with Image.open(col_files["first"]) as im1, Image.open(col_files["second"]) as im2:
            _assert_color_half(np.asarray(im1.convert("RGB"), dtype=np.uint8), "picked Leica F1 single first")
            _assert_color_half(np.asarray(im2.convert("RGB"), dtype=np.uint8), "picked Leica F1 single second")


def test_mixed_pair_labels_exif_order_and_no_single_binding() -> None:
    """F1 单元测试：异配方双联按视觉左右/上下顺序拼合 EXIF 270 并省略单一 recipe_binding；同配方完全不变。"""
    pfx = "GXR Studio computer prediction (not camera-measured): "
    e1, e2 = "1" * 64, "2" * 64

    def _mk(slot: str, ent: str, rec: str, bind: str, sel: str) -> Renderer:
        rb = {
            "record_sha256": rec,
            "entry_sha256": ent,
            "binding_sha256": bind,
            "selection_id": sel,
            "quality_state": "passed",
            "assessed_entry": "1",
        }
        return Renderer("recipe-prediction", "电脑预测", "", True, lambda x: x, f"{pfx}l08firm recipe {slot}. entry SHA256 {ent}.", rb, ent)

    r1 = _mk("06", e1, "a" * 64, "c" * 64, "local:1")
    r2 = _mk("03", e2, "d" * 64, "e" * 64, "local:2")

    same = hfc._labels_from_renderers(r1, r1, neutral=False)
    assert same["description"] == r1.description and same["selection_id"] == "local:1" and same["entry_sha256"] == e1

    for first_half, ori, head, first_tok, second_tok in (
        ("left", 1, "left:", e1, e2),
        ("right", 1, "left:", e2, e1),
        ("right", 3, "left:", e1, e2),
        ("left", 6, "top:", e1, e2),
        ("right", 8, "top:", e1, e2),
    ):
        mixed = hfc._labels_from_renderers(r1, r2, neutral=False, first_half=first_half, orientation=ori)
        assert mixed["description"].startswith(f"{pfx}{head} ")
        assert mixed["description"].index(first_tok) < mixed["description"].index(second_tok)
        for bk in ("selection_id", "record_sha256", "entry_sha256", "binding_sha256", "quality_state", "assessed_entry"):
            assert bk not in mixed


# --------------------------------------------------------------------------- 4. 异配方实拍 WebGPU 真异烘焙（sameBake=False）对拍


@pytest.mark.exclusive
def test_hf258_different_recipe_pair_gpu_parity(
    settings: Any,
    lib: Library,
    volumes: Path,
    tmp_path: Path,
) -> None:
    """首个实拍异配方对 (#1 Leica F1 + #2 HC BW) 的 WebGPU 真 sameBake=False 读回与 Python 逐像素对拍。"""
    _clear_desktop_cache()
    field_pairs = _sorted_pairs(_hf258_field_folder())
    assert len(field_pairs) == 5
    _stage_volume_pairs(volumes, field_pairs[:2])
    _setup_g258_calibrated_home(settings)
    _enable_gpu_flag(settings)

    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    by_idx = _index_shots_by_jpg_sha(lib, field_pairs[:2])
    pshot = by_idx[2]
    sid = pshot["shot_id"]
    assert pshot["pair"]["role"] == "second"
    lib.close()

    leica_f1 = _leica_f1_recipe(settings.home_dir)
    hcbw = {"kind": "builtin", "unit": "l08firm", "slot": 3, "levels": {}}
    adj_exp_curve = {"exposure_ev": 0.35, "contrast": 15, "shadow": 12, "saturation": 8}
    curve_spec = {"master": [[0, 0], [64, 54], [128, 132], [192, 204], [255, 255]]}
    identity_geo = {"straighten": 0, "rotate90": 0, "flip": False, "vertical": 0, "horizontal": 0, "distortion": 0, "box": None}

    harness = tmp_path / "pair_harness.mjs"
    harness.write_text(PAIR_HARNESS_JS, encoding="utf-8")

    files: dict[str, bytes] = {}
    cases: list[dict[str, Any]] = []
    refs: dict[str, np.ndarray] = {}
    case_meta: dict[str, dict[str, Any]] = {}

    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as cli:
        render = cli.app.state.ctx.render  # type: ignore[union-attr]
        lib_live = cli.app.state.ctx.lib  # type: ignore[union-attr]

        g = cli.get(f"/api/photos/{sid}/gpu").json()
        assert g.get("enabled") is True and g.get("pair") is not None, g
        pinfo = g["pair"]
        s0, s1 = pinfo["sources"][0], pinfo["sources"][1]
        r0 = cli.get(s0["raw"]["url"])
        r1 = cli.get(s1["raw"]["url"])
        assert r0.status_code == 200 and r1.status_code == 200
        files["/hf258_0.dng"] = r0.content
        files["/hf258_1.dng"] = r1.content

        pair_cfg = {
            "first_half": pinfo["first_half"],
            "orientation": pinfo["orientation"],
            "window": pinfo["window"],
            "sources": [
                {"layout": s0["raw"], "scene": s0["scene"]},
                {"layout": s1["raw"], "scene": s1["scene"]},
            ],
        }

        # 未改动两半各自烘焙表：必须不同（sameBake=False）
        ub0_bytes, ub0_meta = _fetch_bake(cli, f"/api/photos/{sid}/bake?half=0")
        ub1_bytes, ub1_meta = _fetch_bake(cli, f"/api/photos/{sid}/bake?half=1")
        assert ub0_meta["key"] != ub1_meta["key"], (ub0_meta, ub1_meta)
        files["/ub0.bin"] = ub0_bytes
        files["/ub1.bin"] = ub1_bytes

        # 显式选定共享配方（HC BW 与 Leica F1）：两半同一烘焙表（sameBake=True）
        q_bw = urllib.parse.quote(json.dumps(hcbw))
        bw0_bytes, bw0_meta = _fetch_bake(cli, f"/api/photos/{sid}/bake?half=0&recipe={q_bw}")
        bw1_bytes, bw1_meta = _fetch_bake(cli, f"/api/photos/{sid}/bake?half=1&recipe={q_bw}")
        assert bw0_meta["key"] == bw1_meta["key"]
        files["/bw0.bin"] = bw0_bytes
        files["/bw1.bin"] = bw1_bytes

        q_col = urllib.parse.quote(json.dumps(leica_f1))
        col0_bytes, col0_meta = _fetch_bake(cli, f"/api/photos/{sid}/bake?half=0&recipe={q_col}")
        col1_bytes, col1_meta = _fetch_bake(cli, f"/api/photos/{sid}/bake?half=1&recipe={q_col}")
        assert col0_meta["key"] == col1_meta["key"]
        files["/col0.bin"] = col0_bytes
        files["/col1.bin"] = col1_bytes

        def _add_case(
            cid: str,
            edit_obj: EditState,
            b0_url: str,
            b0_meta: dict[str, Any],
            b1_url: str,
            b1_meta: dict[str, Any],
            adjust_dict: dict[str, Any] | None = None,
            curve_dict: dict[str, Any] | None = None,
        ) -> None:
            refs[cid] = _python_pair_reference(render, lib_live, pshot, edit_obj)
            case_meta[cid] = {
                "orientation": pinfo["orientation"],
                "seam_x0": pinfo["window"]["seam_x0"],
                "seam_w": pinfo["window"]["seam_w"],
                "layer": tuple(pinfo["layer"]),
                "same_bake": bool(b0_meta["key"] == b1_meta["key"]),
            }
            cases.append(
                {
                    "id": cid,
                    "source": "hf258_pair_1_2",
                    "pairRaw": {"url0": "/hf258_0.dng", "url1": "/hf258_1.dng", "config": pair_cfg},
                    "bake": {"url": b0_url, "key": b0_meta["key"], "meta": b0_meta},
                    "bake2": {"url": b1_url, "key": b1_meta["key"], "meta": b1_meta},
                    "lut": None,
                    "look": {
                        "recipe": b0_meta["key"],
                        "recipe2": b1_meta["key"],
                        "lut": None,
                        "lutStrength": 1,
                        "adjust": adjust_dict or {},
                        "curve": curve_dict,
                    },
                    "geometry": identity_geo,
                }
            )

        _add_case("hf258_untouched", EditState(), "/ub0.bin", ub0_meta, "/ub1.bin", ub1_meta)
        _add_case(
            "hf258_untouched_exp_curve",
            EditState(adjust=parse_adjust(adj_exp_curve), curve=parse_curve(curve_spec)),
            "/ub0.bin",
            ub0_meta,
            "/ub1.bin",
            ub1_meta,
            adjust_dict=adj_exp_curve,
            curve_dict=curve_spec,
        )
        _add_case("hf258_picked_hcbw", EditState(recipe=parse_recipe(hcbw)), "/bw0.bin", bw0_meta, "/bw1.bin", bw1_meta)
        _add_case("hf258_picked_leica_f1", EditState(recipe=parse_recipe(leica_f1)), "/col0.bin", col0_meta, "/col1.bin", col1_meta)

    assert case_meta["hf258_untouched"]["same_bake"] is False
    assert case_meta["hf258_untouched_exp_curve"]["same_bake"] is False
    assert case_meta["hf258_picked_hcbw"]["same_bake"] is True
    assert case_meta["hf258_picked_leica_f1"]["same_bake"] is True

    run_dir = tmp_path / "run_hf258_gpu"
    run_dir.mkdir()
    out_gpu, summary = run_engine(run_dir, files, cases, timeout=300.0, harness=harness)

    report_rows: dict[str, Any] = {}
    for cid, ref in refs.items():
        assert cid in out_gpu, f"missing readback for {cid}"
        meta = case_meta[cid]
        w, h = meta["layer"]
        mask = valid_mask(w, h, Geometry(), None)
        res = evaluate(out_gpu[cid], ref, mask=mask, display_lut=False)
        seam_gpu = _gpu_seam_slice(out_gpu[cid], orientation=meta["orientation"], seam_x0=meta["seam_x0"], seam_w=meta["seam_w"])
        seam_ref = _gpu_seam_slice(ref, orientation=meta["orientation"], seam_x0=meta["seam_x0"], seam_w=meta["seam_w"])
        seam_max_diff = int(np.abs(seam_gpu.astype(np.int16) - seam_ref.astype(np.int16)).max())
        report_rows[cid] = {
            "same_bake": meta["same_bake"],
            "within_1": res["within_1"],
            "mean_de00": res["mean_de00"],
            "max_out_band": res["max_out_band"],
            "over_1": res["over_1"],
            "pixels": res["pixels"],
            "seam_max_diff": seam_max_diff,
            "pass": res["pass"],
            "why": res["why"],
        }
        assert res["pass"], f"{cid}: {res}"
        assert seam_max_diff <= 2, f"{cid}: seam max diff {seam_max_diff} > 2"

    # 验证 GPU 读回像素本身确实左彩右黑白 / 一起调后两半同步变
    sx0 = case_meta["hf258_untouched"]["seam_x0"]
    sx1 = sx0 + case_meta["hf258_untouched"]["seam_w"]
    _assert_color_half(out_gpu["hf258_untouched"][:, :sx0], "gpu untouched left")
    _assert_bw_half(out_gpu["hf258_untouched"][:, sx1:], "gpu untouched right")
    _assert_bw_half(out_gpu["hf258_picked_hcbw"][:, :sx0], "gpu picked HC BW left")
    _assert_bw_half(out_gpu["hf258_picked_hcbw"][:, sx1:], "gpu picked HC BW right")
    _assert_color_half(out_gpu["hf258_picked_leica_f1"][:, :sx0], "gpu picked Leica F1 left")
    _assert_color_half(out_gpu["hf258_picked_leica_f1"][:, sx1:], "gpu picked Leica F1 right")

    dump_path = os.environ.get("GXR_G258_GPU_METRICS_OUT")
    if dump_path:
        Path(dump_path).write_text(
            json.dumps({"adapter": summary.get("info"), "parity": report_rows}, indent=2) + "\n",
            encoding="utf-8",
        )
