"""g185/g250 读卡、r6 库身份 6adf847602、Rust/WASM/手机对拍，以及相机 XPan 4288×1584 导入预览导出测试。"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import struct
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from g178_support import need_bundle
from gxr_studio import meta
from gxr_studio.camdata import datcodec as dc
from gxr_studio.camdata import intake, knob_table, r5, store
from gxr_studio.camdata.catalog_names import CATALOG_DESCRIPTORS, CATALOG_GENERATIONS, CATALOG_NAMES, recipe_name, resolve_descriptors
from gxr_studio.camdata.recipe_tags import parse_recipe_tag, recipe_payload
from gxr_studio.cards.discovery import find_cards
from gxr_studio.cards.importer import ImportOptions, run_import
from gxr_studio.crop import default_crop
from gxr_studio.engine import menu as py_menu
from gxr_studio.finish.serve import export_finish, render_stage
from gxr_studio.finish.sizes import output_sizes
from gxr_studio.library.store import Library
from gxr_studio.render.camera_recipe import recipe_tag_payload, recorded_recipe
from gxr_studio.render.params import EditState, RecipeSel, parse_edit, parse_recipe
from gxr_studio.render.recipes import CameraData
from gxr_studio.render.service import RenderService
from support import mobile_ui_export as mx
from support import samples
from support.core_cli import error_of, require_cli, run_json, run_npy
from support.export_pipeline_cases import check, same_file_redacted, same_redacted
from support.wasm_check import run_area
from test_camdata_api import client as client
from test_camdata_api import reader as reader
from test_core_parity_camdata import _expected_error_kind, _expected_parse_summary
from test_datcodec import reseal
from test_mobile_r5_dat import _blocks, _R5Desk, _same_read, pinned_icc
from test_mobile_ui_browser import MODULES, PKG, RUNTIME, _drive, _need

__all__ = ["client", "pinned_icc", "reader"]

REPO = Path(__file__).resolve().parents[1]
R5_DAT_DRIVER = REPO / "tests" / "support" / "mobile_r5_dat_browser.mjs"
XPAN_DRIVER = REPO / "tests" / "support" / "mobile_g250_xpan_browser.mjs"

G185_DAT_SHA256 = "eab6de78d7d9c67906398319b3d8fb0b84eb7ad9c262f7cad7e70d589d9c7ce5"
G250_DAT_SHA256 = "7b566902db1a5354d345185de37f960b01154fcc96027f5707273957db6e9be6"
R6_LIB_SHA256 = "b16465e77931be7f7bd570b3e6105556ac8a0e5f0c2f98c27f0467c56483bb34"


def _bundle_paths(generation: int) -> tuple[Path, Path]:
    parent = samples.sample_parent()
    if generation == 185:
        folder = parent / "firmware/package_v310b_g185/local/g185-daily"
    elif generation == 250:
        folder = parent / "firmware/package_v310b_hf10_r1/local/daily250-REVIEW_PENDING"
    else:
        raise ValueError(f"unexpected generation {generation}")
    dat_path, fx_path = folder / "GXRCE.DAT", folder / "GXRFX.BIN"
    if not dat_path.is_file() or not fx_path.is_file():
        samples.missing(f"g{generation} 本地双文件卡包不在本机")
    return dat_path, fx_path


def _need_g185_or_g250(generation: int) -> tuple[bytes, bytes, Path, Path]:
    dat_path, fx_path = _bundle_paths(generation)
    raw, library = dat_path.read_bytes(), fx_path.read_bytes()
    want_dat = G185_DAT_SHA256 if generation == 185 else G250_DAT_SHA256
    want_lib = r5.LIBRARY_SHA256[178] if generation == 185 else R6_LIB_SHA256
    assert hashlib.sha256(raw).hexdigest() == want_dat, f"g{generation} DAT 摘要已变化"
    assert hashlib.sha256(library).hexdigest() == want_lib, f"g{generation} 库摘要已变化"
    return raw, library, dat_path, fx_path


def _find_xpan_pair() -> tuple[Path, Path]:
    folder = samples.captures() / "r6-field-20261118" / "card250"
    if not folder.is_dir():
        samples.missing("captures/r6-field-20261118/card250 不在本机")
    for dng in sorted(folder.glob("*.DNG")):
        dm = meta.read_dng_meta(dng)
        if dm.error is None and (dm.width, dm.height) == (4288, 1584):
            jpg = dng.with_suffix(".JPG")
            if jpg.is_file():
                return dng, jpg
    samples.missing("card250 里没有 4288×1584 的 XPan DNG+JPG")


def _browser_prereq() -> None:
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")


# --------------------------------------------------------------------------- 1. 读卡与 r6 布局边界


@pytest.mark.parametrize("generation", (185, 250))
def test_g185_and_g250_decode_round_trip_and_intake(generation: int, tmp_path: Path, client: Any) -> None:
    raw, library, dat_path, _ = _need_g185_or_g250(generation)
    info = intake.inspect_dat(raw, library)
    assert info.generation == generation
    dat = dc.parse(raw, library)
    assert dat.generation == generation
    assert dc.pack(dat) == raw
    assert dat.library == library
    assert len(dat.recipes) == 57
    assert all(len(m.entries) == 8 for m in dat.modules)

    if generation == 250:
        assert 81920 < len(dat.engine) <= 98304
        assert len(dat.xdata) == 3916
    else:
        assert len(dat.engine) <= 81920
        assert dat.xdata == b""

    read_raw, read_name, read_lib = intake.read_bundle_from_path(dat_path)
    assert read_name == "GXRCE.DAT" and read_raw == raw and read_lib == library

    home = tmp_path / f"home-{generation}"
    store.import_from_dat(raw, home, library=library)
    cam = CameraData(home)
    _, lib178 = need_bundle(178)
    raw178, _ = need_bundle(178)
    home178 = tmp_path / "home-178"
    store.import_from_dat(raw178, home178, library=lib178)
    cam178 = CameraData(home178)

    for unit_idx, unit_listing in enumerate(cam.listing()):
        assert len(unit_listing["recipes"]) == 23
        for item in unit_listing["recipes"]:
            sel = RecipeSel(unit_listing["id"], item["slot"], kind=item.get("kind", "builtin"), id=item.get("id"))
            assert cam.resolve(sel).entry == cam178.resolve(sel).entry, (generation, unit_idx, item["position"])

    resp = client.post("/api/camdata/import-upload", content=raw + library, headers={"X-GXR-Dat-Bytes": str(len(raw))})
    assert resp.status_code == 200, resp.text
    assert "28mm 23 条" in resp.json()["message"]


def test_g185_and_g250_reject_wrong_library_and_r6_envelope_limits(tmp_path: Path) -> None:
    raw185, lib185, dat185_path, fx185_path = _need_g185_or_g250(185)
    raw250, lib250, dat250_path, fx250_path = _need_g185_or_g250(250)

    # 跨库禁止：g250 不认 g178 库；g185 不认 r6 库；g182 不认 r6 库
    with pytest.raises(intake.IntakeError, match="GXRFX.BIN"):
        intake.inspect_dat(raw250, lib185)
    with pytest.raises(intake.IntakeError, match="GXRFX.BIN"):
        intake.inspect_dat(raw185, lib250)
    raw182, _ = need_bundle(182)
    with pytest.raises(dc.DatError, match="不匹配"):
        dc.parse(raw182, lib250)

    # g250 的 .xdata 哪怕改 1 字节重算 CRC 也必须拒绝
    xdata_at, xdata_len = struct.unpack_from(">II", raw250, 128 + 12 * 2 + 4)
    assert xdata_len == 3916
    bad_xdata = bytearray(raw250)
    bad_xdata[xdata_at + 12] ^= 1
    bad_xdata_bytes = reseal(bytes(bad_xdata))
    with pytest.raises(dc.DatError, match="引擎或链接布局未登记"):
        dc.parse(bad_xdata_bytes, lib250)

    # g250 第 4 区段（sections[3]）非空拒绝
    bad_sec3 = bytearray(raw250)
    bad_sec3.extend(bytes(4))
    struct.pack_into(">I", bad_sec3, 16, len(bad_sec3))
    struct.pack_into(">I", bad_sec3, 128 + 12 * 3 + 8, 4)
    for idx in range(4, 7):
        struct.pack_into(">I", bad_sec3, 128 + 12 * idx + 4, len(bad_sec3))
    with pytest.raises(dc.DatError, match="外壳尺寸"):
        dc.parse(reseal(bytes(bad_sec3)), lib250)

    # g185 第 3 区段（sections[2]）仍不允许非空
    bad_g185_sec2 = bytearray(raw185)
    bad_g185_sec2.extend(bytes(4))
    struct.pack_into(">I", bad_g185_sec2, 16, len(bad_g185_sec2))
    struct.pack_into(">I", bad_g185_sec2, 128 + 12 * 2 + 8, 4)
    for idx in range(3, 7):
        struct.pack_into(">I", bad_g185_sec2, 128 + 12 * idx + 4, len(bad_g185_sec2))
    with pytest.raises(dc.DatError, match="外壳尺寸"):
        dc.parse(reseal(bytes(bad_g185_sec2)), lib185)

    # g250 镜像+BSS 超出 r6 RV_MAX_IMAGE (114688) 时拒绝
    bad_ram = bytearray(raw250)
    struct.pack_into(">I", bad_ram, 116, dc.MAX_RAM - len(raw250) + 4)
    with pytest.raises(dc.DatError):
        dc.parse(reseal(bytes(bad_ram)), lib250)

    # Python 与 Rust CLI 错误一致
    require_cli()
    for p_dat, blob, p_lib in (
        (tmp_path / "g250_wrong_lib.dat", raw250, fx185_path),
        (tmp_path / "g185_wrong_lib.dat", raw185, fx250_path),
        (tmp_path / "g250_bad_xdata.dat", bad_xdata_bytes, fx250_path),
        (tmp_path / "g250_bad_sec3.dat", reseal(bytes(bad_sec3)), fx250_path),
        (tmp_path / "g185_bad_sec2.dat", reseal(bytes(bad_g185_sec2)), fx185_path),
    ):
        p_dat.write_bytes(blob)
        with pytest.raises(dc.DatError) as py_exc:
            dc.parse(blob, p_lib.read_bytes())
        code, payload = run_json(["camdata-parse", "--dat", str(p_dat), "--library", str(p_lib)])
        err = error_of(code, payload)
        assert err is not None
        assert err.kind == _expected_error_kind(py_exc.value)
        assert err.message == str(py_exc.value)
    assert dat185_path.is_file() and dat250_path.is_file()


def test_r6_profile_engine_cap_is_bounded_at_98304_while_r5_stays_81920() -> None:
    assert dc.PROFILES[185].max_engine == 81920 and not dc.PROFILES[185].xdata
    assert dc.PROFILES[250].max_engine == 98304 and dc.PROFILES[250].xdata
    assert dc.PROFILES[250].max_file == 98304


# --------------------------------------------------------------------------- 2. 识别 r6 库 6adf847602 与全身份位置码解码规则


def _read_fx_descriptors(library: bytes) -> list[tuple[str, int]]:
    cat = library[64:]
    secs = r5._sections(cat)
    shared = cat[secs[5][0] : secs[5][0] + secs[5][1]]
    area = shared[dc.u32(shared, 64) :]
    count = dc.u16(area, 10)
    out: list[tuple[str, int]] = []
    for eff in range(1, count + 1):
        desc = area[48 + 44 * (eff - 1) : 92 + 44 * (eff - 1)]
        out.append((desc[32:44].split(b"\0")[0].decode("ascii"), desc[28]))
    return out


def test_r6_catalog_identity_6adf847602_and_position_48_decoding() -> None:
    raw250, lib250, _, _ = _need_g185_or_g250(250)
    assert lib250[84:89].hex() == "6adf847602"
    assert dc.u32(lib250, 36) == 178
    assert CATALOG_GENERATIONS["6adf847602"] == 178

    dat = dc.parse(raw250, lib250)
    for index, code in enumerate(("50", "28", "MT")):
        native = tuple(record[4:36].split(b"\0")[0].decode("ascii")[3:] for record in dat.modules[index].records[:4])
        extras = tuple(record.name for record in dat.recipes if record.unit_mask >> index & 1)
        assert CATALOG_NAMES["6adf847602"][code] == native + extras
    assert tuple(_read_fx_descriptors(lib250)) == CATALOG_DESCRIPTORS["6adf847602"]

    _, lib178 = need_bundle(178)
    _, lib176 = need_bundle(176)
    assert tuple(_read_fx_descriptors(lib178)) == CATALOG_DESCRIPTORS["6a3a5a3c02"]
    assert tuple(_read_fx_descriptors(lib176)) == CATALOG_DESCRIPTORS["d25d2f0a68"]

    # root 规则 + 现场标签（R8052695 28 03, R8052696-698 28 06, R8052699-708 28 48, 11-13 MOUNT 10）
    assert recipe_name("6adf847602", "28", 1) == "GRPos"
    assert recipe_name("6adf847602", "28", 3) == "HC BW"
    assert recipe_name("6adf847602", "28", 6) == "F-CC"
    assert recipe_name("6adf847602", "28", 48) == "Teal Orange"
    assert recipe_name("6adf847602", "MT", 10) == "F-CN"
    assert recipe_payload(parse_recipe_tag("GXR1 6adf847602 28 48 A99999999-"))["catalogued"] is True

    # 用 Studio 自己的 r5 库描述符解码核对位置码 48（r->effect = 48 - 4 = 44，1-based 序号 44 -> 0-based 43）
    cat = lib250[64:]
    cat_secs = r5._sections(cat)
    shared = cat[cat_secs[5][0] : cat_secs[5][0] + cat_secs[5][1]]
    area = shared[dc.u32(shared, 64) :]
    desc44 = area[48 + 44 * 43 : 92 + 44 * 43]
    assert desc44[32:44].split(b"\0")[0].decode("ascii") == "Teal Orange"
    assert desc44[28] == 2  # unit_mask == 2 -> 28mm


@pytest.mark.parametrize(
    ("identity", "unit", "position", "expected"),
    (
        # 6adf847602 (r6 / g250)：现场标签与基底槽/越界回退
        ("6adf847602", "28", 3, "HC BW"),
        ("6adf847602", "28", 6, "F-CC"),
        ("6adf847602", "28", 48, "Teal Orange"),
        ("6adf847602", "MT", 10, "F-CN"),
        ("6adf847602", "50", 6, "P160"),
        ("6adf847602", "28", 5, "K64"),
        ("6adf847602", "50", 48, None),
        ("6adf847602", "28", 25, None),
        # 6a3a5a3c02 (g178 / g181 / g182 r2 / g185)：同套描述符交错规则
        ("6a3a5a3c02", "28", 3, "HC BW"),
        ("6a3a5a3c02", "28", 6, "F-CC"),
        ("6a3a5a3c02", "28", 30, "K25"),
        ("6a3a5a3c02", "28", 48, "Teal Orange"),
        ("6a3a5a3c02", "MT", 10, "F-CN"),
        ("6a3a5a3c02", "50", 6, "P160"),
        ("6a3a5a3c02", "28", 5, "K64"),
        ("6a3a5a3c02", "MT", 48, None),
        # d25d2f0a68 (g176)：29 条共享/独立描述符 + RV 基底槽 1..8
        ("d25d2f0a68", "28", 3, "HC BW"),
        ("d25d2f0a68", "28", 6, "Acros"),
        ("d25d2f0a68", "28", 8, "ARRI 709"),
        ("d25d2f0a68", "28", 29, "F-CC"),
        ("d25d2f0a68", "28", 33, "Teal Orange"),
        ("d25d2f0a68", "MT", 10, None),
        ("d25d2f0a68", "MT", 31, "F-CN"),
        ("d25d2f0a68", "50", 6, "P160"),
        ("d25d2f0a68", "28", 5, "K64"),
        ("d25d2f0a68", "28", 48, None),
    ),
)
def test_root_position_rule_applies_to_all_three_identities(identity: str, unit: str, position: int, expected: str | None) -> None:
    assert recipe_name(identity, unit, position) == expected


@pytest.mark.parametrize("generation", (176, 178, 185, 250))
def test_recorded_recipe_maps_position_via_effect_descriptor_not_menu_index(generation: int, tmp_path: Path) -> None:
    if generation in (185, 250):
        raw, library, _, _ = _need_g185_or_g250(generation)
    else:
        raw, library = need_bundle(generation)
    home = tmp_path / f"home-{generation}"
    store.import_from_dat(raw, home, library=library)
    cam = CameraData(home)
    identity = library[84:89].hex()

    if generation == 176:
        cases = (("28", 5, "builtin", "K64"), ("28", 6, "record", "Acros"), ("28", 29, "record", "F-CC"), ("MT", 31, "record", "F-CN"))
        for unit, pos, kind, name in cases:
            tag = parse_recipe_tag(f"GXR1 {identity} {unit} {pos:02d}")
            sel = recorded_recipe(tag, cam)
            assert sel is not None and sel.kind == kind
            assert recipe_tag_payload(tag, cam)["name"] == name
            assert len(cam.resolve(sel).entry) == 552
    else:
        cases = (("28", 3, "builtin", "HC BW"), ("28", 6, "record", "F-CC"), ("28", 48, "record", "Teal Orange"), ("MT", 10, "record", "F-CN"))
        for unit, pos, kind, name in cases:
            knobs = "-999999999" if kind == "builtin" else "A99999999-"
            tag = parse_recipe_tag(f"GXR1 {identity} {unit} {pos:02d} {knobs}")
            sel = recorded_recipe(tag, cam)
            assert sel is not None and sel.kind == kind
            assert recipe_tag_payload(tag, cam)["name"] == name
            unit_row = next(row for row in cam.listing() if row["id"] == sel.unit)
            if kind == "record":
                expected_info = next(r for r in unit_row["recipes"] if r.get("kind") == "record" and r["name"] == name)
                assert sel.id == expected_info["id"]
            assert len(cam.resolve(sel).entry) == 552
        # 50 06 按规则回退到 RV 基底槽 6 (P160)，178 目录已移除旧 5..8 入口，不得误选成 F-CN
        assert recorded_recipe(parse_recipe_tag(f"GXR1 {identity} 50 06 A99999999-"), cam) is None


def test_local_kubik_test_library_descriptors_follow_same_unit_interleave() -> None:
    fx_path = samples.sample_parent() / "firmware/package_v310b_kubik_lib_r6/local/test-REVIEW_PENDING/GXRFX.BIN"
    if not fx_path.is_file():
        samples.missing("KUBIK 本地测试库不在本机")
    lib_bytes = fx_path.read_bytes()
    ident = lib_bytes[84:89].hex()
    assert ident == "0ccc86c19b"
    # KUBIK 尚未作为日常库登记进全局目录
    assert ident not in CATALOG_GENERATIONS and ident not in CATALOG_NAMES
    descriptors = _read_fx_descriptors(lib_bytes)
    assert len(descriptors) == 66
    expected_kubik = {
        ("50", 62): "K-0815",
        ("50", 65): "K-PACIFIC",
        ("50", 68): "K-KINO",
        ("28", 63): "K-0815",
        ("28", 66): "K-PACIFIC",
        ("28", 69): "K-KINO",
        ("MT", 64): "K-0815",
        ("MT", 67): "K-PACIFIC",
        ("MT", 70): "K-KINO",
        ("28", 6): "F-CC",
        ("28", 48): "Teal Orange",
        ("MT", 10): "F-CN",
        ("50", 63): None,
        ("28", 64): None,
        ("MT", 62): None,
    }
    for (unit, pos), want in expected_kubik.items():
        hit = resolve_descriptors(descriptors, unit, pos)
        assert (None if hit is None else hit.name) == want


@pytest.mark.parametrize("folder_name", ("probe251", "card250"))
def test_r6_field_photos_resolve_catalog_names_including_position_48(folder_name: str, tmp_path: Path) -> None:
    folder = samples.captures() / "r6-field-20261118" / folder_name
    if not folder.is_dir():
        samples.missing(f"captures/r6-field-20261118/{folder_name} 不在本机")
    raw250, lib250, _, _ = _need_g185_or_g250(250)
    home = tmp_path / "home"
    store.import_from_dat(raw250, home, library=lib250)
    cam = CameraData(home)

    dngs = sorted(folder.glob("*.DNG"))
    assert dngs
    seen_positions: list[int] = []
    # root 规则 + 现场标签：01=GRPos, 03=HC BW, 06=F-CC (eff=2), 48=Teal Orange (eff=44)
    expected = {1: ("GRPos", "builtin"), 3: ("HC BW", "builtin"), 6: ("F-CC", "record"), 48: ("Teal Orange", "record")}
    for dng_path in dngs:
        jpg_path = dng_path.with_suffix(".JPG")
        assert jpg_path.is_file()
        dm = meta.read_dng_meta(dng_path)
        jm = meta.read_jpeg_meta(jpg_path)
        assert dm.error is None and jm.error is None
        assert dm.copyright == jm.copyright
        assert dm.recipe_tag == jm.recipe_tag and jm.recipe_tag is not None
        tag = jm.recipe_tag
        assert tag["catalog_identity"] == "6adf847602" and tag["unit"] == "28"
        payload = recipe_tag_payload(tag, cam)
        assert payload is not None
        seen_positions.append(tag["position"])
        want_name, want_kind = expected[tag["position"]]
        assert payload["name"] == want_name
        assert payload["catalogued"] is True and payload["amount_unknown"] is False
        sel = recorded_recipe(tag, cam)
        assert sel is not None and sel.kind == want_kind and len(cam.resolve(sel).entry) == 552

    if folder_name == "probe251":
        assert set(seen_positions) == {1, 3, 6}
    else:
        assert set(seen_positions) == {3, 48}


# --------------------------------------------------------------------------- 3. Rust / WASM / 手机三边读卡对拍


@pytest.mark.parametrize("generation", (185, 250))
def test_rust_cli_and_wasm_parity_for_g185_and_g250(generation: int, tmp_path: Path) -> None:
    require_cli()
    raw, library, dat_path, fx_path = _need_g185_or_g250(generation)
    expected, entries = _expected_parse_summary(raw, library)
    out_dir = tmp_path / f"entries-{generation}"
    code, payload = run_json(["camdata-parse", "--dat", str(dat_path), "--library", str(fx_path), "--out-dir", str(out_dir)])
    assert error_of(code, payload) is None
    assert payload == expected

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
@pytest.mark.parametrize("generation", (185, 250))
def test_mobile_browser_reads_g185_and_g250_cards(generation: int, tmp_path: Path) -> None:
    pair = mx.real_pairs(1)
    if len(pair) < 1:
        samples.missing("need one DNG+JPG pair")
    _browser_prereq()
    raw, library, dat_path, fx_path = _need_g185_or_g250(generation)
    dng, jpg = pair[0]
    home = tmp_path / f"home-{generation}"
    store.import_from_dat(raw, home, library=library)
    cam = CameraData(home)
    report_path = tmp_path / f"g{generation}-report.json"
    manifest = {
        "dng": str(dng),
        "jpg": str(jpg),
        "dat": str(dat_path),
        "bin": str(fx_path),
        "files": [str(dng), str(jpg), str(dat_path), str(fx_path)],
        "out": str(report_path),
    }
    mpath = tmp_path / f"g{generation}-manifest.json"
    mpath.write_text(json.dumps(manifest), encoding="utf-8")
    result = _drive(R5_DAT_DRIVER, mpath, tmp_path, "", timeout=420)
    if result.returncode != 0 or not report_path.is_file():
        pytest.fail(f"r5/r6 browser driver failed for g{generation}")
    report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    check(report.get("error") is None, "driver finished cleanly")
    check(report.get("pageErrors") == [], "no page errors")
    assert "缺少 GXRFX.BIN" in report["sheet"]["text"]
    for step_key in ("second", "loaded"):
        phone = report[step_key]
        assert phone["entries"] == 24 * 552
        assert phone["generation"] == generation
        assert phone["library"] == len(library)
        same_redacted(phone["listing"], cam.listing(), f"g{generation} phone listing equals desktop listing")
        sel = parse_recipe({"kind": "builtin", "unit": "jlaunch", "slot": 1, "levels": {}})
        assert sel is not None
        resolved = cam.resolve(sel)
        same_file_redacted(base64.b64decode(phone["entry"]), resolved.entry, f"g{generation} resolve entry")
        same_redacted(phone["levels"], resolved.levels, f"g{generation} resolve levels")
        same_redacted(phone["sha"], resolved.entry_sha256, f"g{generation} resolve sha")
    assert _same_read is not None


# --------------------------------------------------------------------------- 4. XPan 4288×1584 桌面与手机导入、预览、导出


def test_desktop_xpan_4288x1584_import_preview_and_export_original_ratio(tmp_path: Path, settings: Any) -> None:
    dng_src, jpg_src = _find_xpan_pair()
    raw250, lib250, _, _ = _need_g185_or_g250(250)
    store.import_from_dat(raw250, settings.home_dir, library=lib250)

    # 导入 XPan DNG+JPG 对和 JPG-only（同源 JPG 字节相同，分两张临时卡和图库避免去重跳过）
    cases = [
        ("pair", "R9990709", True),
        ("jpg", "R9990710", False),
    ]
    for kind, stem, with_dng in cases:
        vol_dir = tmp_path / f"vol-{kind}"
        card_dir = vol_dir / "CARD" / "DCIM" / "100RICOH"
        card_dir.mkdir(parents=True)
        mx.copy_as(jpg_src, card_dir / f"{stem}.JPG")
        if with_dng:
            mx.copy_as(dng_src, card_dir / f"{stem}.DNG")

        lib = Library(tmp_path / f"lib-{kind}").open()
        try:
            (card,) = find_cards([vol_dir])
            res = run_import(lib, card, ImportOptions(min_free_bytes=0, make_thumbs=False))
            assert res.imported_shots == 1
            (shot,) = lib.list_shots()
            assert shot["name"] == stem
            assert (shot["width"], shot["height"]) == (4288, 1584)

            render = RenderService(settings.home_dir, lib)
            crop_orig = default_crop("original", 4288, 1584)
            assert crop_orig.rect == (0.0, 0.0, 1.0, 1.0)
            # 若错用 65:24 裁剪比例会少 1 像素高
            crop_6524 = default_crop("xpan-65-24", 4288, 1584)
            assert round(crop_6524.rect[3] * 1584) == 1583

            sizes = output_sizes(render, lib, shot, EditState(), crop_orig.rect, crop_orig.preset)
            assert sizes["photo"] == [4288, 1584]
            assert sizes["sizes"]["original"] == [4288, 1584]

            preview_jpeg, _labels = render_stage(render, lib, shot, EditState(), {}, stage="preview", max_edge=1600, quality=88)
            with Image.open(io.BytesIO(preview_jpeg)) as im:
                assert im.size == (1600, 591)

            exported = export_finish(
                render, lib, shot, EditState(), {"crop": None}, settings.export_dir, [], quality=95, long_edge=None, fmt="jpeg"
            )
            assert (exported.width, exported.height) == (4288, 1584)
            with Image.open(exported.path) as im:
                assert im.size == (4288, 1584)
        finally:
            lib.close()


@pytest.mark.browser_serial
def test_mobile_browser_xpan_4288x1584_import_preview_and_export_parity(tmp_path: Path, pinned_icc: bytes) -> None:
    dng_src, jpg_src = _find_xpan_pair()
    _browser_prereq()
    check(len(pinned_icc) == 588, "ICC pin is 588 bytes")
    raw250, lib250, dat_path, fx_path = _need_g185_or_g250(250)
    desk = _R5Desk(tmp_path, raw250, lib250)
    try:
        x_dng = mx.copy_as(dng_src, tmp_path / "R9990709.DNG")
        x_jpg = mx.copy_as(jpg_src, tmp_path / "R9990709.JPG")
        x_jpg_only = mx.copy_as(jpg_src, tmp_path / "R9990710.JPG")
        shot_pair = desk.shot(x_dng.stem, x_dng, x_jpg)
        shot_jpg = desk.shot(x_jpg_only.stem, None, x_jpg_only)
        assert (shot_pair["width"], shot_pair["height"]) == (4288, 1584)
        assert (shot_jpg["width"], shot_jpg["height"]) == (4288, 1584)

        out_dir = tmp_path / "phone"
        out_dir.mkdir()
        report_path = tmp_path / "xpan-report.json"
        manifest = {
            "pack": str(desk.zip),
            "binding": desk.binding(),
            "files": [str(x_dng), str(x_jpg), str(x_jpg_only), str(dat_path), str(fx_path)],
            "count": 2,
            "stems": [x_dng.stem, x_jpg_only.stem],
            "outDir": str(out_dir),
            "out": str(report_path),
        }
        mpath = tmp_path / "xpan-manifest.json"
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        result = _drive(XPAN_DRIVER, mpath, tmp_path, "", timeout=900)
        if result.returncode != 0 or not report_path.is_file():
            pytest.fail("mobile g250 xpan browser driver failed")
        report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
        check(report.get("error") is None, "xpan browser driver finished")
        check(report.get("pageErrors") == [], "no page errors")

        previews = {row["stem"]: row for row in report["steps"]["previews"]}
        # DNG+JPG 对：半尺寸渲染层 2144×792，原图裁剪徽章与最终尺寸均为 4288×1584，切 65:24 才裁成 4288×1583
        pair_prev = previews[x_dng.stem]
        assert pair_prev["layerSize"] == {"w": 2144, "h": 792}
        assert pair_prev["originalFinalSize"] == {"w": 2144, "h": 792}
        assert pair_prev["originalBadge"] == [4288, 1584]
        assert pair_prev["xpan6524Badge"] == [4288, 1583]
        assert pair_prev["restoredBadge"] == [4288, 1584]

        # 只有 JPG 的 XPan：预览层 4288×1584，原图不额外裁剪
        jpg_prev = previews[x_jpg_only.stem]
        assert jpg_prev["layerSize"] == {"w": 4288, "h": 1584}
        assert jpg_prev["originalFinalSize"] == {"w": 4288, "h": 1584}
        assert jpg_prev["originalBadge"] == [4288, 1584]
        assert jpg_prev["xpan6524Badge"] == [4288, 1583]
        assert jpg_prev["restoredBadge"] == [4288, 1584]

        facts = report["steps"]["export"]["facts"]["facts"]
        for stem, shot in ((x_dng.stem, shot_pair), (x_jpg_only.stem, shot_jpg)):
            fact = facts[stem]
            assert (fact["width"], fact["height"]) == (4288, 1584)
            side = desk.sidecar(shot, {"knobs": {}})
            same_redacted(parse_edit(fact["side"]).to_json(), parse_edit(side).to_json(), "xpan phone side equals desktop")
            same_redacted(_blocks(fact["side"]), _blocks(side), "xpan phone side blocks equal desktop")
            res = desk.export(shot, side, 95, None)
            assert (res.width, res.height) == (4288, 1584)
            phone_out = out_dir / f"xpan-{res.path.name}"
            assert phone_out.is_file()
            with Image.open(phone_out) as im:
                assert im.size == (4288, 1584)
            same_file_redacted(phone_out.read_bytes(), res.path.read_bytes(), f"{stem} phone export equals desktop export")
    finally:
        desk.close()
