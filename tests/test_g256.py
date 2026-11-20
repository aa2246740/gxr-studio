"""g253/g256 DAT 读卡、三支单元半格配对合成导出，以及暗景接缝边界与全量误报/桌面手机一致性回归测试。"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageOps

from conftest import PORT
from gxr_studio import halfframe as hf
from gxr_studio import halfframe_compose as hfc
from gxr_studio.app import create_app
from gxr_studio.camdata import calib, intake, knob_table, store
from gxr_studio.camdata import datcodec as dc
from gxr_studio.cards.discovery import find_cards
from gxr_studio.cards.importer import ImportOptions, run_import
from gxr_studio.engine import menu as py_menu
from gxr_studio.library.store import Library
from gxr_studio.render.params import EditState, RecipeSel, parse_recipe
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
from test_halfframe_p0 import (
    BROWSER_DRIVER,
    REPO,
    _clear_desktop_cache,
    _make_synthetic_jpeg,
    _sorted_jpgs,
    _sorted_pairs,
    _stage_volume_pairs,
)
from test_halfframe_p1a_field import _index_shots_by_jpg_sha, _phase_shift_and_corr, _seam_and_halves, _seam_core
from test_mobile_r5_dat import _same_read
from test_mobile_ui_browser import _drive

__all__ = ["client", "reader"]

G253_DAT_SHA256 = "08c54106eef4b7549a22ff67ac7b0b67c8a4c8302864699ab46ce81dace24b98"
G256_DAT_SHA256 = "59f4e5343baaa2cb97db223e4118375394e7aeaf2a79a79e0427b9f9f8df2251"

_DAT_SPECS: dict[int, tuple[str, str, int, int]] = {
    253: ("firmware/package_v310b_zoom_onestep_r1/local/test253-REVIEW_PENDING", G253_DAT_SHA256, 86736, 3932),
    256: ("firmware/package_v310b_hf256_r1/local/test256-REVIEW_PENDING", G256_DAT_SHA256, 87132, 3896),
}


def _need_g253_or_g256(generation: int) -> tuple[bytes, bytes, Path, Path]:
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


def _hf256_field_folder() -> Path:
    folder = samples.captures() / "hf256-field-20261120" / "card"
    if not folder.is_dir():
        samples.missing("captures/hf256-field-20261120/card 不在本机")
    return folder


def _stage_bundle(tmp_path: Path, name: str, raw: bytes, library: bytes) -> tuple[Path, Path]:
    bundle_dir = tmp_path / name
    bundle_dir.mkdir(parents=True, exist_ok=True)
    dat_path = bundle_dir / "GXRCE.DAT"
    fx_path = bundle_dir / "GXRFX.BIN"
    dat_path.write_bytes(raw)
    fx_path.write_bytes(library)
    return dat_path, fx_path


def _setup_g256_calibrated_home(settings: Any) -> None:
    raw, library, _, _ = _need_g253_or_g256(256)
    store.import_from_dat(raw, settings.home_dir, library=library)
    sources = calib.dev_sources(samples.sample_parent())
    if not sources:
        samples.missing("研究目录里的标定文件不在本机")
    calib.import_calibration(settings.home_dir, sources)


def _node_is_diptych_batch(cases: list[tuple[np.ndarray, str]]) -> list[bool]:
    """用 Node 直接调用 mobile/src/lib/halfframe.ts 的 isDiptychRgba 批量判定（灰度展开为 RGBA）。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for mobile halfframe parity test")
    script = """
import { readFileSync } from 'node:fs';
import { isDiptychRgba } from './mobile/src/lib/halfframe.ts';
const buf = readFileSync(0);
let off = 0;
const count = buf.readUInt32LE(off); off += 4;
const out = [];
for (let i = 0; i < count; i += 1) {
  const w = buf.readUInt16LE(off); off += 2;
  const h = buf.readUInt16LE(off); off += 2;
  const axisByte = buf[off]; off += 1;
  const axis = axisByte === 1 ? 'row' : 'col';
  const n = w * h;
  const rgba = new Uint8Array(n * 4);
  for (let p = 0; p < n; p += 1) {
    const v = buf[off + p];
    const q = p * 4;
    rgba[q] = v;
    rgba[q + 1] = v;
    rgba[q + 2] = v;
    rgba[q + 3] = 255;
  }
  off += n;
  out.push(isDiptychRgba(rgba, w, h, axis));
}
process.stdout.write(JSON.stringify(out));
"""
    chunks = [struct.pack("<I", len(cases))]
    for arr, axis in cases:
        h, w = arr.shape
        chunks.append(struct.pack("<HHB", w, h, 1 if axis == "row" else 0))
        chunks.append(np.ascontiguousarray(arr, dtype=np.uint8).tobytes())
    proc = subprocess.run(
        [node, "--experimental-strip-types", "--input-type=module", "-e", script],
        cwd=REPO,
        input=b"".join(chunks),
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    return list(json.loads(proc.stdout.decode("utf-8")))


def _eval_web_gallery_and_recipes(photos: list[dict[str, Any]]) -> dict[str, Any]:
    """用 Node 加载 web/assets/halfframe.js 验证图库折叠与配对拍摄配方标签。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for web halfframe logic check")
    script = """
import { readFileSync } from 'node:fs';
import { foldPhotos, isPairSecond, pairCount, partnerOf, shortRecipeLabel } from './web/assets/halfframe.js';
const photos = JSON.parse(readFileSync(0, 'utf8'));
const visible = foldPhotos(photos);
const hfOnly = foldPhotos(photos, { halfframeOnly: true });
const pairLabels = hfOnly.map((p2) => {
  const p1 = partnerOf(p2, photos);
  return {
    id: p2.id,
    unit: p2.recipe_tag?.unit ?? null,
    label: `拍摄时：第 1 张 ${shortRecipeLabel(p1?.recipe_tag)} · 第 2 张 ${shortRecipeLabel(p2.recipe_tag)}`,
  };
});
process.stdout.write(JSON.stringify({
  visibleCount: visible.length,
  visiblePairCount: visible.filter(isPairSecond).length,
  hfOnlyCount: hfOnly.length,
  pairCount: pairCount(photos),
  pairLabels,
}));
"""
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=REPO,
        input=json.dumps(photos, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    return dict(json.loads(proc.stdout.decode("utf-8")))


# --------------------------------------------------------------------------- 1. g253 / g256 DAT 读卡、绑定与三边对拍


@pytest.mark.parametrize("generation", (253, 256))
def test_g253_and_g256_decode_round_trip_and_intake(generation: int, tmp_path: Path, client: Any) -> None:
    raw, kubik_lib, _, _ = _need_g253_or_g256(generation)
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


@pytest.mark.parametrize("generation", (253, 256))
def test_g253_and_g256_reject_wrong_library_and_envelope_tamper(generation: int, tmp_path: Path) -> None:
    raw, kubik_lib, _, _ = _need_g253_or_g256(generation)
    _, lib185, _, fx185_path = _need_g185_or_g250(185)
    _, lib250_old, _, fx250_old_path = _need_g185_or_g250(250)

    # 缺库与错误库（g185 r5 库、g250 旧 23 条 r6 库）一律拒绝
    for bad_lib in (None, lib185, lib250_old):
        with pytest.raises(intake.IntakeError) as exc_info:
            intake.inspect_dat(raw, bad_lib)
        assert exc_info.value.code != "generation_unsupported"

    # .xdata 篡改 1 字节并重算 CRC 仍拒绝
    xdata_at, xdata_len = struct.unpack_from(">II", raw, 128 + 12 * 2 + 4)
    assert xdata_len > 0
    bad_xdata = bytearray(raw)
    bad_xdata[xdata_at] ^= 0x01
    bad_xdata_bytes = reseal(bytes(bad_xdata))
    with pytest.raises(dc.DatError) as exc_xdata:
        dc.parse(bad_xdata_bytes, kubik_lib)
    assert not isinstance(exc_xdata.value, dc.UnsupportedGeneration)

    # 第 4 区段非空拒绝
    bad_sec3 = bytearray(raw)
    struct.pack_into(">II", bad_sec3, 128 + 12 * 3 + 4, len(raw), 4)
    bad_sec3 += b"\x00\x00\x00\x00"
    for idx in range(4, 7):
        struct.pack_into(">II", bad_sec3, 128 + 12 * idx + 4, len(bad_sec3), 0)
    bad_sec3_bytes = reseal(bytes(bad_sec3))
    with pytest.raises(dc.DatError) as exc_sec3:
        dc.parse(bad_sec3_bytes, kubik_lib)
    assert not isinstance(exc_sec3.value, dc.UnsupportedGeneration)

    # 镜像 + BSS 超出 MAX_RAM 拒绝
    bad_ram = bytearray(raw)
    struct.pack_into(">I", bad_ram, 116, dc.MAX_RAM - len(raw) + 4)
    with pytest.raises(dc.DatError) as exc_ram:
        dc.parse(reseal(bytes(bad_ram)), kubik_lib)
    assert not isinstance(exc_ram.value, dc.UnsupportedGeneration)

    # Rust CLI 对错误库与篡改同样拒绝且错误类型一致
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


@pytest.mark.parametrize("generation", (253, 256))
def test_rust_cli_and_wasm_parity_for_g253_and_g256(generation: int, tmp_path: Path) -> None:
    require_cli()
    raw, library, _, _ = _need_g253_or_g256(generation)
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
def test_mobile_browser_reads_g256_card(tmp_path: Path) -> None:
    pair = mx.real_pairs(1)
    if len(pair) < 1:
        samples.missing("need one DNG+JPG pair")
    _browser_prereq()
    raw, library, dat_path, fx_path = _need_g253_or_g256(256)
    dng, jpg = pair[0]

    home = tmp_path / "home-256-browser"
    store.import_from_dat(raw, home, library=library)
    cam = CameraData(home)
    qlaunch = next(row for row in cam.listing() if row["id"] == "qlaunch")
    kino = next(r for r in qlaunch["recipes"] if r.get("kind") == "record" and r["name"] == "K-KINO")
    extra_recipe_dict = {"kind": "record", "unit": "qlaunch", "slot": kino["slot"], "id": kino["id"], "levels": {}}

    report_path = tmp_path / "g256-report.json"
    manifest: dict[str, Any] = {
        "dng": str(dng),
        "jpg": str(jpg),
        "dat": str(dat_path),
        "bin": str(fx_path),
        "files": [str(dng), str(jpg), str(dat_path), str(fx_path)],
        "extraRecipe": extra_recipe_dict,
        "out": str(report_path),
    }
    mpath = tmp_path / "g256-manifest.json"
    mpath.write_text(json.dumps(manifest), encoding="utf-8")
    result = _drive(R5_DAT_DRIVER, mpath, tmp_path, "", timeout=420)
    if result.returncode != 0 or not report_path.is_file():
        pytest.fail("r6 browser driver failed for g256")
    report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    check(report.get("error") is None, "driver finished cleanly")
    check(report.get("pageErrors") == [], "no page errors")
    assert "缺少 GXRFX.BIN" in report["sheet"]["text"]
    for step_key in ("second", "loaded"):
        phone = report[step_key]
        assert phone["entries"] == 24 * 552
        assert phone["generation"] == 256
        assert phone["library"] == len(library)
        same_redacted(phone["listing"], cam.listing(), "g256 phone listing equals desktop listing")
        sel = parse_recipe({"kind": "builtin", "unit": "jlaunch", "slot": 1, "levels": {}})
        assert sel is not None
        resolved = cam.resolve(sel)
        same_file_redacted(base64.b64decode(phone["entry"]), resolved.entry, "g256 resolve entry")
        same_redacted(phone["levels"], resolved.levels, "g256 resolve levels")
        same_redacted(phone["sha"], resolved.entry_sha256, "g256 resolve sha")
        extra_sel = parse_recipe(extra_recipe_dict)
        assert extra_sel is not None
        extra_resolved = cam.resolve(extra_sel)
        same_file_redacted(base64.b64decode(phone["extra"]["entry"]), extra_resolved.entry, "g256 K-KINO resolve entry")
        same_redacted(phone["extra"]["levels"], extra_resolved.levels, "g256 K-KINO resolve levels")
        same_redacted(phone["extra"]["sha"], extra_resolved.entry_sha256, "g256 K-KINO resolve sha")
    assert _same_read is not None


# --------------------------------------------------------------------------- 2. 三支单元半格配对、合成、导出与图库


def test_hf256_three_units_pairing_including_mount_boundary(lib: Library, volumes: Path) -> None:
    """不依赖 g256 DAT，直接导入现场 10 张，独立验证 28mm / MOUNT（暗景边界例）/ 50mm 三对配对与普通照片保持普通。"""
    _clear_desktop_cache()
    field_pairs = _sorted_pairs(_hf256_field_folder())
    assert len(field_pairs) == 10
    _stage_volume_pairs(volumes, field_pairs)

    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    by_idx = _index_shots_by_jpg_sha(lib, field_pairs)
    assert len(by_idx) == 10

    for label, i1, i2, want_tag_unit in (
        ("28mm", 1, 2, "28"),
        ("MOUNT", 6, 7, "MT"),
        ("50mm", 9, 10, "50"),
    ):
        s1, s2 = by_idx[i1], by_idx[i2]
        assert s2["diptych"] is True, f"{label}: second shot (#{i2}) not detected as diptych"
        assert s1.get("pair") is not None, f"{label}: first shot (#{i1}) missing pair"
        assert s2.get("pair") is not None, f"{label}: second shot (#{i2}) missing pair"
        assert s1["pair"]["role"] == "first" and s1["pair"]["partner_id"] == s2["shot_id"]
        assert s2["pair"]["role"] == "second" and s2["pair"]["partner_id"] == s1["shot_id"]
        assert s2["pair"]["first_half"] == "left" and s2["pair"]["kind"] == "g250"
        assert s2["has_raw"] == 1 and "dng" not in s2["files"]
        assert s1["recipe_tag"]["unit"] == want_tag_unit and s2["recipe_tag"]["unit"] == want_tag_unit

    for ordinary_idx in (3, 4, 5, 8):
        shot = by_idx[ordinary_idx]
        assert shot["diptych"] is False, f"ordinary shot #{ordinary_idx} flagged as diptych"
        assert "pair" not in shot and not shot.get("half_first")


def test_hf256_three_units_pair_compose_export_and_gallery(
    settings: Any,
    lib: Library,
    volumes: Path,
    tmp_path: Path,
) -> None:
    _clear_desktop_cache()
    field_pairs = _sorted_pairs(_hf256_field_folder())
    assert len(field_pairs) == 10
    _stage_volume_pairs(volumes, field_pairs)
    _setup_g256_calibrated_home(settings)

    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    by_idx = _index_shots_by_jpg_sha(lib, field_pairs)
    assert len(by_idx) == 10

    expected_pairs = (
        ("28mm", 1, 2, "qlaunch", "28", "F-CC"),
        ("MOUNT", 6, 7, "l08firm", "MT", "F-CN"),
        ("50mm", 9, 10, "jlaunch", "50", "HC BW"),
    )
    for label, i1, i2, want_unit, want_tag_unit, _want_recipe in expected_pairs:
        s1, s2 = by_idx[i1], by_idx[i2]
        assert s1.get("pair") is not None, f"{label}: first shot ({i1}) missing pair"
        assert s2.get("pair") is not None, f"{label}: second shot ({i2}) missing pair"
        assert s1["pair"]["role"] == "first" and s1["pair"]["partner_id"] == s2["shot_id"]
        assert s2["pair"]["role"] == "second" and s2["pair"]["partner_id"] == s1["shot_id"]
        assert s2["pair"]["first_half"] == "left"
        assert s2["pair"]["kind"] == "g250"
        assert s2["diptych"] is True and s2["has_raw"] == 1
        assert "dng" not in s2["files"]
        assert s1["recipe_tag"]["unit"] == want_tag_unit and s2["recipe_tag"]["unit"] == want_tag_unit

    for ordinary_idx in (3, 4, 5, 8):
        shot = by_idx[ordinary_idx]
        assert shot["diptych"] is False, f"ordinary shot #{ordinary_idx} flagged as diptych"
        assert "pair" not in shot, f"ordinary shot #{ordinary_idx} unexpectedly paired"
        assert not shot.get("half_first"), f"ordinary shot #{ordinary_idx} unexpectedly half_first"

    # 三支单元各自从两张 DNG（列 1099）全分辨率合成并与相机双联对齐
    rsvc = RenderService(settings.home_dir, lib)
    for label, i1, i2, want_unit, _tu, _rn in expected_pairs:
        shot2 = by_idx[i2]
        assert rsvc.photo_unit(shot2) == want_unit, f"{label} photo_unit mismatch"
        cam_dip = np.asarray(ImageOps.exif_transpose(Image.open(field_pairs[i2 - 1][1])).convert("RGB"), dtype=np.uint8)
        cam_p1_raw = np.asarray(Image.open(field_pairs[i1 - 1][1]).convert("RGB"), dtype=np.uint8)
        ori = int(shot2.get("orientation") or 1)
        cam_p1_strip = hfc.orient_half(cam_p1_raw[:, hfc.FULL_STRIP_X0_JPG : hfc.FULL_STRIP_X0_JPG + hfc.FULL_HALF_W], ori)

        for mode_label, edit, min_corr in (
            ("shooting_recipe", EditState(recipe=None), 0.98),
            ("neutral", EditState(recipe="neutral"), 0.90),
        ):
            out_rgb, _labels = hfc.render_pair_rgb(rsvc, lib, shot2, edit, full=True, neutral=(mode_label == "neutral"))
            assert out_rgb.shape == cam_dip.shape, f"{label} {mode_label} shape mismatch"
            out_a, out_seam, out_b = _seam_and_halves(out_rgb)
            cam_a, _cam_seam, cam_b = _seam_and_halves(cam_dip)
            _, cam_seam_core, _ = _seam_core(cam_dip)
            assert int(out_seam.max()) == 0, f"{label} {mode_label} seam not pure black"
            assert int(cam_seam_core.max()) <= 6
            sa, corra = _phase_shift_and_corr(out_a, cam_a)
            sb, corrb = _phase_shift_and_corr(out_b, cam_b)
            sp1, corrp1 = _phase_shift_and_corr(out_a, cam_p1_strip)
            assert sa == (0, 0) and sb == (0, 0) and sp1 == (0, 0), (label, mode_label, sa, sb, sp1)
            assert corra >= min_corr and corrb >= min_corr and corrp1 >= min_corr, (
                label,
                mode_label,
                corra,
                corrb,
                corrp1,
            )

    lib.close()

    # API 图库折叠显示 3 格半格对 + 各自拍摄配方标签 + 双联/两张单格导出
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        rows = client.get("/api/photos?limit=100").json()
        assert len(rows) == 10
        gallery = _eval_web_gallery_and_recipes(rows)
        assert gallery["visibleCount"] == 7
        assert gallery["visiblePairCount"] == 3
        assert gallery["hfOnlyCount"] == 3
        assert gallery["pairCount"] == 3
        labels_by_id = {item["id"]: item["label"] for item in gallery["pairLabels"]}

        for label, _i1, i2, _u, _tu, want_recipe in expected_pairs:
            sid2 = by_idx[i2]["shot_id"]
            assert labels_by_id[sid2] == f"拍摄时：第 1 张 {want_recipe} · 第 2 张 {want_recipe}", (
                label,
                labels_by_id.get(sid2),
            )

            dip_dir = tmp_path / f"exp_dip_{label}"
            dip_dir.mkdir()
            r_dip = client.post(
                f"/api/photos/{sid2}/export",
                json={"halfframe": "diptych", "size": "original", "quality_tier": "high", "dest_dir": str(dip_dir)},
            )
            assert r_dip.status_code == 200, r_dip.text
            dip_path = Path(r_dip.json()["path"])
            assert dip_path.read_bytes() != field_pairs[i2 - 1][1].read_bytes()
            with Image.open(dip_path) as im:
                assert im.size == (4288, 2848)
                arr = np.asarray(im.convert("RGB"), dtype=np.uint8)
                ha, seam_core, hb = _seam_core(arr)
                assert int(seam_core.max()) <= 6
                cam_dip = np.asarray(ImageOps.exif_transpose(Image.open(field_pairs[i2 - 1][1])).convert("RGB"), dtype=np.uint8)
                ca, _, cb = _seam_and_halves(cam_dip)
                sa, corra = _phase_shift_and_corr(ha, ca)
                sb, corrb = _phase_shift_and_corr(hb, cb)
                assert sa == (0, 0) and sb == (0, 0) and corra >= 0.98 and corrb >= 0.98

            sgl_dir = tmp_path / f"exp_singles_{label}"
            sgl_dir.mkdir()
            r_sgl = client.post(
                f"/api/photos/{sid2}/export",
                json={"halfframe": "singles", "size": "original", "quality_tier": "high", "dest_dir": str(sgl_dir)},
            )
            assert r_sgl.status_code == 200, r_sgl.text
            s_files = r_sgl.json()["files"]
            assert len(s_files) == 2
            for item in s_files:
                p = Path(item["path"])
                assert p.name.endswith("_gxr_half.jpg")
                with Image.open(p) as im:
                    assert im.size == (2088, 2848)


# --------------------------------------------------------------------------- 3. 暗景接缝规则、误报全量扫描与桌面/手机一致性


def _make_luma(fill: int = 140) -> np.ndarray:
    return np.full((hf.DRAFT_HEIGHT, hf.DRAFT_WIDTH), fill, dtype=np.uint8)


def test_seam_boundary_rule_synthetic_desktop_and_phone() -> None:
    """合成接缝边界与暗图负对照：g250 允许单侧紧邻暗景，legacy48 保持双侧，桌面与手机一致。"""
    g250_c0, g250_c1 = hf._SEAM_SPEC_COLS[0][1], hf._SEAM_SPEC_COLS[0][2]
    leg_c0, leg_c1 = hf._SEAM_SPEC_COLS[1][1], hf._SEAM_SPEC_COLS[1][2]

    cases: list[tuple[str, np.ndarray, str | None]] = []

    # 1) MOUNT 边界同类：右侧紧邻暗场景（2088..2239 全黑，右半远处仍有 40 灰阶）
    right_dark = _make_luma(140)
    right_dark[:, g250_c0 : g250_c1 + 7] = 0
    right_dark[:, g250_c1 + 7 :] = 40
    cases.append(("g250_dark_right_adjacent", right_dark, hf.SEAM_G250))

    # 2) 对称边界：左侧紧邻暗场景（2040..2199 全黑，左半远处仍有 40 灰阶）
    left_dark = _make_luma(140)
    left_dark[:, g250_c0 - 6 : g250_c1 + 1] = 0
    left_dark[:, : g250_c0 - 6] = 40
    cases.append(("g250_dark_left_adjacent", left_dark, hf.SEAM_G250))

    # 3) legacy48 单侧暗场景仍不放宽（保持双侧要求）
    leg_one_side = _make_luma(140)
    leg_one_side[:, leg_c0 : leg_c1 + 4] = 0
    leg_one_side[:, leg_c1 + 4 :] = 40
    cases.append(("legacy48_dark_right_rejected", leg_one_side, None))

    # 4) 负对照：全黑、暗均匀、整半幅全黑、双侧超宽黑带、无中缝暗景
    cases.append(("all_black", _make_luma(0), None))
    cases.append(("uniform_dark_8", _make_luma(8), None))

    half_black_right = _make_luma(140)
    half_black_right[:, g250_c0:] = 0
    cases.append(("half_black_right", half_black_right, None))

    half_black_left = _make_luma(140)
    half_black_left[:, : g250_c1 + 1] = 0
    cases.append(("half_black_left", half_black_left, None))

    wide_both = _make_luma(140)
    wide_both[:, g250_c0 - 3 : g250_c1 + 4] = 0
    cases.append(("wide_both_sides_dark", wide_both, None))

    dark_scene = _make_luma(8)
    dark_scene[100:120, 10:30] = 90
    dark_scene[100:120, 500:520] = 90
    cases.append(("dark_scene_no_seam_edge", dark_scene, None))

    # 5) F2 近黑负对照：暗侧仅有零星亮点或极小亮斑（< 5%），或中缝附近黑带加微弱噪点，必须拒绝
    one_speck = _make_luma(140)
    one_speck[:, g250_c0:] = 0
    one_speck[180, 450] = 180
    cases.append(("dark_half_single_speck", one_speck, None))

    few_specks = _make_luma(140)
    few_specks[:, g250_c0:] = 0
    for k in range(20):
        few_specks[10 + k * 15, g250_c1 + 5 + k * 10] = 200
    cases.append(("dark_half_few_specks", few_specks, None))

    small_patch = _make_luma(140)
    small_patch[:, g250_c0:] = 0
    small_patch[100:140, 350:390] = 120  # 1600 / 92916 = 1.72% < 5%
    cases.append(("dark_half_small_patch_below_5pct", small_patch, None))

    band_with_speck = _make_luma(4)
    band_with_speck[100:115, g250_c0 - 1] = 80
    band_with_speck[200:205, 400:405] = 90
    cases.append(("black_band_near_center_with_speck", band_with_speck, None))

    # 暗侧真实弱光画面（6.46% >= 5%）：应通过
    dim_real_half = _make_luma(140)
    dim_real_half[:, g250_c0:] = 0
    dim_real_half[100:160, 320:420] = 40  # 6000 / 92916 = 6.46% >= 5%
    cases.append(("g250_dark_right_6pct_accepted", dim_real_half, hf.SEAM_G250))

    node_inputs: list[tuple[np.ndarray, str]] = []
    for _name, luma, _want in cases:
        node_inputs.append((luma, "col"))
        node_inputs.append((luma.T.copy(), "row"))
    phone_verdicts = _node_is_diptych_batch(node_inputs)

    for idx, (name, luma, want_kind) in enumerate(cases):
        got_kind = hf.seam_kind_luma(luma)
        assert got_kind == want_kind, f"desktop mismatch on {name}: got={got_kind} want={want_kind}"
        want_bool = want_kind is not None
        assert phone_verdicts[2 * idx] is want_bool, f"phone landscape mismatch on {name}"
        assert phone_verdicts[2 * idx + 1] is want_bool, f"phone portrait mismatch on {name}"


def test_seam_default_regression_set_desktop_and_phone() -> None:
    """默认回归集：hf256 三对（含 MOUNT 边界）与 4 张普通照，再加 card250 若干普通照，验证桌面与手机 100% 一致。"""
    hf256_jpgs = _sorted_jpgs(_hf256_field_folder())
    assert len(hf256_jpgs) == 10

    regression: list[tuple[str, Path, bool]] = [(f"hf256:{i}", jpg, i in {2, 7, 10}) for i, jpg in enumerate(hf256_jpgs, start=1)]
    card250 = samples.captures() / "r6-field-20261118" / "card250"
    if card250.is_dir():
        c250_jpgs = _sorted_jpgs(card250)
        for idx in (7, 8, 9, 10):
            if idx <= len(c250_jpgs):
                regression.append((f"card250:{idx}", c250_jpgs[idx - 1], False))

    node_inputs: list[tuple[np.ndarray, str]] = []
    meta: list[tuple[str, bool]] = []
    for label, path, expected in regression:
        ok, luma = hf.decode_draft_luma(path)
        assert ok and luma is not None, f"unreadable regression shot {label}"
        node_inputs.append((luma, "col"))
        meta.append((label, expected))

    phone_verdicts = _node_is_diptych_batch(node_inputs)
    for (luma, _axis), (label, expected), phone_v in zip(node_inputs, meta, phone_verdicts, strict=True):
        desk_v = hf.is_diptych_luma(luma)
        assert desk_v is expected, f"desktop mismatch on {label}: got={desk_v} want={expected}"
        assert phone_v is expected, f"phone mismatch on {label}: got={phone_v} want={expected}"


@pytest.mark.browser_serial
def test_hf256_three_units_and_boundary_phone_browser(tmp_path: Path) -> None:
    """真 Chromium 下用 isDiptychBlob 检查 hf256 三支单元 10 张实拍 + 合成暗景边界/负对照，并与桌面 100% 一致。"""
    _browser_prereq()
    _clear_desktop_cache()
    field_jpgs = _sorted_jpgs(_hf256_field_folder())
    assert len(field_jpgs) == 10

    files: list[str] = []
    labels: list[str] = []
    expected: list[bool] = []
    for idx, jpg in enumerate(field_jpgs, start=1):
        files.append(str(jpg))
        labels.append(f"hf256:{idx}")
        expected.append(idx in {2, 7, 10})

    # 合成暗景负对照（含近黑零星亮点）与单侧暗景正对照（包含旋转）
    all_black = _make_synthetic_jpeg(tmp_path / "syn_all_black.jpg", seam_range=None, fill=(0, 0, 0))
    half_black = _make_synthetic_jpeg(tmp_path / "syn_half_black.jpg", seam_range=(2088, 4287), fill=(140, 140, 140))
    dark_specks = tmp_path / "syn_dark_specks.jpg"
    arr_s = np.full((hf.FULL_HEIGHT, hf.FULL_WIDTH, 3), 140, dtype=np.uint8)
    arr_s[:, 2088:] = 0
    for k in range(12):
        arr_s[200 + k * 160 : 216 + k * 160, 2400 + k * 120 : 2416 + k * 120] = 200
    Image.fromarray(arr_s, mode="RGB").save(dark_specks, format="JPEG", quality=92, subsampling=0)
    dark_right = tmp_path / "syn_dark_right.jpg"
    arr = np.full((hf.FULL_HEIGHT, hf.FULL_WIDTH, 3), 140, dtype=np.uint8)
    arr[:, 2088:2240] = 0
    arr[:, 2240:] = 40
    Image.fromarray(arr, mode="RGB").save(dark_right, format="JPEG", quality=92, subsampling=0)
    dark_left_rot6 = tmp_path / "syn_dark_left_rot6.jpg"
    arr_l = np.full((hf.FULL_HEIGHT, hf.FULL_WIDTH, 3), 140, dtype=np.uint8)
    arr_l[:, 2040:2200] = 0
    arr_l[:, :2040] = 40
    exif = Image.Exif()
    exif[274] = 6
    Image.fromarray(arr_l, mode="RGB").save(dark_left_rot6, format="JPEG", quality=92, subsampling=0, exif=exif.tobytes())

    for lbl, path, want in (
        ("syn:all_black", all_black, False),
        ("syn:half_black", half_black, False),
        ("syn:dark_specks", dark_specks, False),
        ("syn:dark_right", dark_right, True),
        ("syn:dark_left_rot6", dark_left_rot6, True),
    ):
        files.append(str(path))
        labels.append(lbl)
        expected.append(want)

    desk_verdicts = [hf.is_diptych(Path(f)) for f in files]
    assert desk_verdicts == expected, [(lbl, w, d) for lbl, w, d in zip(labels, expected, desk_verdicts, strict=True) if w is not d]

    out_path = tmp_path / "hf256-detect-out.json"
    manifest = tmp_path / "hf256-detect-manifest.json"
    manifest.write_text(json.dumps({"mode": "detect", "files": files, "out": str(out_path)}), encoding="utf-8")
    proc = _drive(BROWSER_DRIVER, manifest, tmp_path, "")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report.get("pageErrors") == [], report
    assert "error" not in report, report
    got = report["verdicts"]
    assert got == expected, [(lbl, w, g) for lbl, w, g in zip(labels, expected, got, strict=True) if w is not g]
    assert got == desk_verdicts
