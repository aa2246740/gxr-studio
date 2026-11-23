"""g266 DAT 读卡、文件与上传导入、r5 多重定位外壳校验、g263–g265 拒收、CLI/WASM/手机对拍，以及与 g262 一致的 Look+微调标签校验。"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import struct
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from conftest import PORT
from gxr_studio import audit
from gxr_studio.camdata import datcodec as dc
from gxr_studio.camdata import intake, knob_table, store
from gxr_studio.camdata.engine_bindings import BOUND_ENGINES
from gxr_studio.camdata.recipe_tags import parse_recipe_tag
from gxr_studio.config import Settings
from gxr_studio.engine import menu as py_menu
from gxr_studio.finish.serve import export_finish
from gxr_studio.halfframe_compose import pair_plans
from gxr_studio.halfframe_export import export_pair_finish
from gxr_studio.library.store import Library
from gxr_studio.render import edits as render_edits
from gxr_studio.render.camera_recipe import recipe_tag_payload, recorded_recipe
from gxr_studio.render.params import EditState, RecipeSel, parse_recipe
from gxr_studio.render.recipes import CameraData
from gxr_studio.render.service import RenderService
from support import g262_cases as g262
from support import mobile_ui_export as mx
from support import samples
from support.core_cli import error_of, require_cli, run_json, run_npy
from support.export_pipeline_cases import check, same_file_redacted, same_redacted
from support.wasm_check import run_area
from test_camdata_api import client as client
from test_camdata_api import reader as reader
from test_core_parity_camdata import _assert_parse_matches_python_error, _expected_parse_summary
from test_core_parity_camera_recipe import _cli_actual, _desktop_expected, _normalize_wasm, _run_wasm_cases
from test_datcodec import reseal
from test_g250 import KUBIK_LIB_SHA256, R5_DAT_DRIVER, _browser_prereq, _need_g185_or_g250
from test_g256 import _stage_bundle
from test_g260 import _fresh_client
from test_mobile_r5_dat import _R5Desk, pinned_icc
from test_mobile_ui_browser import _drive

__all__ = ["client", "pinned_icc", "reader"]

REPO = Path(__file__).resolve().parents[1]
UI_DRIVER = REPO / "tests" / "support" / "g262_look_finetune_ui.mjs"
MOBILE_LOOK_DRIVER = REPO / "tests" / "support" / "mobile_g262_look_browser.mjs"

G266_DAT_SHA256 = "17bc8c943624faca18928a0661b07457c99e22c83932b76f2857773591745024"
G266_REL_DIR = "firmware/package_v310b_tweak_store_r4/local/test266-REVIEW_PENDING"
G266_RELOCATIONS = [(93696, 102789), (93716, 71708)]

UNRECOGNISED_DAILIES: dict[int, tuple[str, str]] = {
    263: (
        "firmware/package_v310b_tweak_store_r1/local/test263-REVIEW_PENDING",
        "09c445d20c7bed36d8629cebac0585bcbe175141bfdd1f72a0349307d8c14ab2",
    ),
    264: (
        "firmware/package_v310b_tweak_store_r2/local/test264-REVIEW_PENDING",
        "ce830a12f976ecc1684c163e2e728b6499f43fe939ec027d6b813d00755297f4",
    ),
    265: (
        "firmware/package_v310b_tweak_store_r3/local/test265-REVIEW_PENDING",
        "a2f709e90c8bd58f85eda7068d796d8a994b561c996306c61ff44f9643167b0d",
    ),
}


def _need_g266() -> tuple[bytes, bytes, Path, Path]:
    parent = samples.sample_parent()
    dat_path = parent / G266_REL_DIR / "GXRCE.DAT"
    fx_path = parent / "firmware/package_v310b_kubik_lib_r6/local/test-REVIEW_PENDING/GXRFX.BIN"
    if not dat_path.is_file():
        samples.missing("g266 本地 DAT 不在本机")
    if not fx_path.is_file():
        samples.missing("KUBIK 本地测试库不在本机")
    raw, library = dat_path.read_bytes(), fx_path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == G266_DAT_SHA256, "g266 DAT 摘要已变化"
    assert hashlib.sha256(library).hexdigest() == KUBIK_LIB_SHA256, "KUBIK 库摘要已变化"
    return raw, library, dat_path, fx_path


def _synthetic_r5_shell(
    *,
    generation: int = 266,
    nreloc_header: int,
    relocs: list[tuple[int, int]],
    engine_size: int = 32,
) -> bytes:
    """构造仅含外壳字节的合成 r5/r6 DAT，用于验证重定位计数、区段尺寸与单调性守卫。"""
    engine = bytearray(engine_size)
    for patch, target in relocs:
        if 256 <= patch <= 256 + engine_size - 4 and patch % 4 == 0:
            struct.pack_into(">I", engine, patch - 256, target)
    reloc_bytes = b"".join(struct.pack(">II", p, t) for p, t in relocs)
    raw = bytearray(256) + engine + reloc_bytes
    raw[:12] = b"GXRCE001\0\1\1\0"
    struct.pack_into(">I", raw, 16, len(raw))
    struct.pack_into(">I", raw, 28, generation)
    struct.pack_into(">IIH", raw, 112, 256, 16, nreloc_header)
    cursor = 256
    for idx, size in enumerate((len(engine), len(reloc_bytes), 0, 0, 0, 0, 0)):
        struct.pack_into(">HHII", raw, 128 + 12 * idx, idx + 1, 0, cursor, size)
        cursor += size
    return reseal(bytes(raw))


def _forge_g262_with_two_relocs(raw262: bytes) -> bytes:
    """在真实 g262 上不改引擎字节，追加第 2 条合法且严格递增的重定位并重算区段与 CRC。"""
    eng_at, eng_sz = struct.unpack_from(">II", raw262, 128 + 4)
    rel_at, rel_sz = struct.unpack_from(">II", raw262, 128 + 12 + 4)
    xd_at, xd_sz = struct.unpack_from(">II", raw262, 128 + 24 + 4)
    assert rel_sz == 8
    p0, _t0 = struct.unpack_from(">II", raw262, rel_at)
    bss = dc.u32(raw262, 116)
    new_ram = len(raw262) + 8 + bss
    extra: tuple[int, int] | None = None
    for off in range(p0 + 4, 256 + eng_sz - 4, 4):
        word = dc.u32(raw262, off)
        if 256 <= word < new_ram:
            extra = (off, word)
            break
    assert extra is not None
    header = bytearray(raw262[:256])
    engine = raw262[eng_at : eng_at + eng_sz]
    reloc = raw262[rel_at : rel_at + rel_sz] + struct.pack(">II", *extra)
    xdata = raw262[xd_at : xd_at + xd_sz]
    struct.pack_into(">H", header, 120, 2)
    parts = (engine, reloc, xdata, b"", b"", b"", b"")
    cursor = 256
    for idx, part in enumerate(parts):
        struct.pack_into(">HHII", header, 128 + 12 * idx, idx + 1, 0, cursor, len(part))
        cursor += len(part)
    out = bytearray(header + b"".join(parts))
    struct.pack_into(">I", out, 16, len(out))
    return reseal(bytes(out))


# --------------------------------------------------------------------------- 1. g266 DAT 解码、往返与导入


def test_g266_decode_round_trip_and_intake(tmp_path: Path, client: Any, volumes: Path, reader: Any) -> None:
    raw, kubik_lib, _, _ = _need_g266()
    raw262, _, _, _ = g262.need_g262()

    info = intake.inspect_dat(raw, kubik_lib)
    assert info.generation == 266
    assert info.size == 97980 and info.size == len(raw)
    assert info.sha256 == G266_DAT_SHA256

    dat = dc.parse(raw, kubik_lib)
    assert dat.generation == 266 and dc.pack(dat) == raw and dat.library == kubik_lib
    assert dat.relocations == G266_RELOCATIONS
    assert dat.entry == 85764 and dat.bss == 9164
    assert len(dat.engine) == 93624 and 81920 < len(dat.engine) <= 98304
    assert len(dat.xdata) == 4084
    assert len(dat.recipes) == 66
    assert all(len(m.entries) == 8 for m in dat.modules)

    dat_copy, _ = _stage_bundle(tmp_path, "bundle-266", raw, kubik_lib)
    assert intake.read_bundle_from_path(dat_copy) == (raw, "GXRCE.DAT", kubik_lib)

    home, home262 = tmp_path / "home-266", tmp_path / "home-262-ref"
    store.import_from_dat(raw, home, library=kubik_lib)
    store.import_from_dat(raw262, home262, library=kubik_lib)
    cam, cam262 = CameraData(home), CameraData(home262)

    for unit_idx, unit_listing in enumerate(cam.listing()):
        recipes = unit_listing["recipes"]
        assert len(recipes) == 26
        assert [r["name"] for r in recipes[-3:]] == ["K-0815", "K-PACIFIC", "K-KINO"]
        for item in recipes:
            sel = RecipeSel(unit_listing["id"], item["slot"], kind=item.get("kind", "builtin"), id=item.get("id"))
            assert cam.resolve(sel).entry == cam262.resolve(sel).entry, (unit_idx, item["position"])

    # 卡导入（只读）
    card_root = volumes / "GXR266"
    (card_root / "DCIM" / "100RICOH").mkdir(parents=True)
    (card_root / "GXRCE.DAT").write_bytes(raw)
    (card_root / "GXRFX.BIN").write_bytes(kubik_lib)
    with audit.AuditRecorder(watch=(str(volumes), str(volumes.resolve()))) as rec:
        card_resp = client.post("/api/camdata/import-card")
    assert card_resp.status_code == 200, card_resp.text
    card_body = card_resp.json()
    assert card_body["imported"] is True and card_body["generation"] == 266 and card_body["sha256"] == G266_DAT_SHA256
    assert "28mm 26 条" in card_body["message"]
    assert rec.writes_under(volumes) == []
    assert set(reader.opened) == {str(card_root / "GXRCE.DAT"), str(card_root / "GXRFX.BIN")}

    # 本地文件与目录导入
    with _fresh_client(tmp_path, "file-266", volumes) as file_client:
        file_resp = file_client.post("/api/camdata/import-file", json={"path": str(dat_copy)})
        assert file_resp.status_code == 200, file_resp.text
        file_body = file_resp.json()
        assert file_body["imported"] is True and file_body["generation"] == 266 and file_body["sha256"] == G266_DAT_SHA256
        assert "28mm 26 条" in file_body["message"]
        dir_resp = file_client.post("/api/camdata/import-file", json={"path": str(dat_copy.parent)})
        assert dir_resp.status_code == 200 and dir_resp.json()["unchanged"] is True and dir_resp.json()["generation"] == 266

    # 浏览器上传导入
    with _fresh_client(tmp_path, "upload-266", volumes) as upload_client:
        resp = upload_client.post("/api/camdata/import-upload", content=raw + kubik_lib, headers={"X-GXR-Dat-Bytes": str(len(raw))})
        assert resp.status_code == 200 and resp.json()["imported"] is True and resp.json()["generation"] == 266
        assert "28mm 26 条" in resp.json()["message"]


# --------------------------------------------------------------------------- 2. 错误库、篡改、r5 重定位守卫与 g263–g265 拒收


def test_g266_rejects_wrong_library_and_envelope_tamper(tmp_path: Path) -> None:
    raw, kubik_lib, _, _ = _need_g266()
    _, lib185, _, fx185_path = _need_g185_or_g250(185)
    _, lib250_old, _, fx250_old_path = _need_g185_or_g250(250)

    for bad_lib in (None, lib185, lib250_old):
        with pytest.raises(intake.IntakeError) as exc_info:
            intake.inspect_dat(raw, bad_lib)
        assert exc_info.value.code == "dat_invalid", exc_info.value.code

    xdata_at, xdata_len = struct.unpack_from(">II", raw, 128 + 12 * 2 + 4)
    assert xdata_len == 4084
    bad_xdata = bytearray(raw)
    bad_xdata[xdata_at] ^= 0x01
    bad_xdata_bytes = reseal(bytes(bad_xdata))

    bad_sec3 = bytearray(raw)
    struct.pack_into(">II", bad_sec3, 128 + 12 * 3 + 4, len(raw), 4)
    bad_sec3 += b"\x00\x00\x00\x00"
    struct.pack_into(">I", bad_sec3, 16, len(bad_sec3))
    for idx in range(4, 7):
        struct.pack_into(">II", bad_sec3, 128 + 12 * idx + 4, len(bad_sec3), 0)
    bad_sec3_bytes = reseal(bytes(bad_sec3))

    bad_ram = bytearray(raw)
    struct.pack_into(">I", bad_ram, 116, dc.MAX_RAM - len(raw) + 4)
    bad_ram_bytes = reseal(bytes(bad_ram))

    bad_engine = bytearray(raw)
    bad_engine[300] ^= 0x01
    bad_engine_bytes = reseal(bytes(bad_engine))

    reloc_at, reloc_len = struct.unpack_from(">II", raw, 128 + 12 + 4)
    assert reloc_len == 16
    bad_reloc2 = bytearray(raw)
    bad_reloc2[reloc_at + 15] ^= 0x01
    bad_reloc2_bytes = reseal(bytes(bad_reloc2))

    for label, tampered, expected_msg in (
        ("bad_xdata", bad_xdata_bytes, "266 引擎或链接布局未登记"),
        ("bad_sec3", bad_sec3_bytes, "r5 执行外壳尺寸不合格"),
        ("bad_ram", bad_ram_bytes, "r5 BSS 或重定位不合格"),
        ("bad_engine", bad_engine_bytes, "266 引擎或链接布局未登记"),
        ("bad_reloc2", bad_reloc2_bytes, "r5 重定位与引擎不匹配"),
    ):
        with pytest.raises(dc.DatError) as exc:
            dc.parse(tampered, kubik_lib)
        assert not isinstance(exc.value, dc.UnsupportedGeneration), label
        assert str(exc.value) == expected_msg, label

    require_cli()
    _, fx_kubik_copy = _stage_bundle(tmp_path, "neg-266", raw, kubik_lib)
    for path, blob, fx in (
        (tmp_path / "g266_missing_lib.dat", raw, None),
        (tmp_path / "g266_wrong_lib185.dat", raw, fx185_path),
        (tmp_path / "g266_wrong_lib250.dat", raw, fx250_old_path),
        (tmp_path / "g266_bad_xdata.dat", bad_xdata_bytes, fx_kubik_copy),
        (tmp_path / "g266_bad_sec3.dat", bad_sec3_bytes, fx_kubik_copy),
        (tmp_path / "g266_bad_ram.dat", bad_ram_bytes, fx_kubik_copy),
        (tmp_path / "g266_bad_engine.dat", bad_engine_bytes, fx_kubik_copy),
        (tmp_path / "g266_bad_reloc2.dat", bad_reloc2_bytes, fx_kubik_copy),
    ):
        path.write_bytes(blob)
        _assert_parse_matches_python_error(path, blob, fx)


def test_r5_relocation_guards_and_g262_nreloc2_rejected_in_python_and_rust(tmp_path: Path) -> None:
    require_cli()
    raw262, kubik_lib, _, fx_path = g262.need_g262()

    cases: list[tuple[str, bytes, str]] = [
        ("nreloc_0_empty", _synthetic_r5_shell(nreloc_header=0, relocs=[]), "r5 执行外壳尺寸不合格"),
        ("nreloc_0_with_8b", _synthetic_r5_shell(nreloc_header=0, relocs=[(260, 256)]), "r5 执行外壳尺寸不合格"),
        ("nreloc_257_empty", _synthetic_r5_shell(nreloc_header=257, relocs=[]), "r5 执行外壳尺寸不合格"),
        (
            "nreloc_257_matching_size",
            _synthetic_r5_shell(nreloc_header=257, relocs=[(256 + 4 * i, 256) for i in range(257)], engine_size=1040),
            "r5 执行外壳尺寸不合格",
        ),
        ("reloc_size_mismatch_short", _synthetic_r5_shell(nreloc_header=2, relocs=[(260, 256)]), "r5 执行外壳尺寸不合格"),
        ("reloc_size_mismatch_long", _synthetic_r5_shell(nreloc_header=1, relocs=[(260, 256), (264, 256)]), "r5 执行外壳尺寸不合格"),
        ("non_increasing_equal", _synthetic_r5_shell(nreloc_header=2, relocs=[(260, 256), (260, 256)]), "r5 重定位与引擎不匹配"),
        ("non_increasing_desc", _synthetic_r5_shell(nreloc_header=2, relocs=[(264, 256), (260, 256)]), "r5 重定位与引擎不匹配"),
    ]
    for name, blob, expected_msg in cases:
        with pytest.raises(dc.DatError) as exc_info:
            dc.parse(blob, kubik_lib)
        assert not isinstance(exc_info.value, dc.UnsupportedGeneration), name
        assert str(exc_info.value) == expected_msg, name
        p = tmp_path / f"{name}.dat"
        p.write_bytes(blob)
        _assert_parse_matches_python_error(p, blob, fx_path)

    # g262 改写为 nreloc=2（引擎字节未动、双条重定位均满足外壳结构检查且 CRC 合法）必须被 BOUND_ENGINES 拒绝
    g262_two = _forge_g262_with_two_relocs(raw262)
    with pytest.raises(dc.DatError) as g262_exc:
        dc.parse(g262_two, kubik_lib)
    assert str(g262_exc.value) == "262 引擎或链接布局未登记"
    p262 = tmp_path / "g262_nreloc2.dat"
    p262.write_bytes(g262_two)
    _assert_parse_matches_python_error(p262, g262_two, fx_path)


@pytest.mark.parametrize("generation", (263, 264, 265))
def test_g263_g264_g265_are_not_recognised_as_known_dailies(generation: int, tmp_path: Path, volumes: Path) -> None:
    assert generation not in dc.PROFILES and generation not in BOUND_ENGINES
    rel_dir, expected_sha = UNRECOGNISED_DAILIES[generation]
    _, kubik_lib, _, fx_path = _need_g266()
    dat_path = samples.sample_parent() / rel_dir / "GXRCE.DAT"
    if not dat_path.is_file():
        pytest.skip(f"g{generation} 本地中间构建 DAT 不在本机")
    raw = dat_path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == expected_sha
    assert dc.peek_generation(raw) == generation

    with pytest.raises(dc.UnsupportedGeneration):
        dc.parse(raw, kubik_lib)
    with pytest.raises(intake.IntakeError) as exc_info:
        intake.inspect_dat(raw, kubik_lib)
    assert exc_info.value.code == "generation_unsupported"
    assert exc_info.value.message == "不支持这个版本的相机配方文件。"

    with _fresh_client(tmp_path, f"reject-{generation}", volumes) as upload_client:
        resp = upload_client.post("/api/camdata/import-upload", content=raw + kubik_lib, headers={"X-GXR-Dat-Bytes": str(len(raw))})
        assert resp.status_code == 422
        assert resp.json()["error"] == "generation_unsupported"
        assert resp.json()["message"] == "不支持这个版本的相机配方文件。"

    require_cli()
    _assert_parse_matches_python_error(dat_path, raw, fx_path)


# --------------------------------------------------------------------------- 3. CLI / WASM 对拍


def test_rust_cli_and_wasm_parity_for_g266(tmp_path: Path) -> None:
    require_cli()
    raw, library, _, _ = _need_g266()
    dat_path, fx_path = _stage_bundle(tmp_path, "parity-266", raw, library)

    expected, entries = _expected_parse_summary(raw, library)
    code, payload = run_json(["camdata-parse", "--dat", str(dat_path), "--library", str(fx_path), "--out-dir", str(tmp_path / "entries-266")])
    assert error_of(code, payload) is None and payload == expected and len(payload["recipes"]) == 66

    arr = run_npy(["camdata-entries", "--dat", str(dat_path), "--library", str(fx_path)], tmp_path / "entries-266.npy")
    expected_arr = np.frombuffer(b"".join(entries[(u, s)] for u in range(3) for s in store.SLOTS), dtype=np.uint8).reshape(24, 552)
    assert np.array_equal(arr, expected_arr)

    dat = dc.parse(raw, library)
    assert dat.kv2_block is not None
    expected_menu = knob_table.read(dat.kv2_block, py_menu.load(), mount_knobs_from_50mm=dc.PROFILES[266].mount_knobs_from_50mm)
    mcode, mpayload = run_json(["camdata-menu", "--dat", str(dat_path), "--library", str(fx_path)])
    assert error_of(mcode, mpayload) is None and mpayload == expected_menu

    wasm_dir = tmp_path / "wasm-266"
    wasm_dir.mkdir()
    proc = run_area("camdata", [dat_path], wasm_dir)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --------------------------------------------------------------------------- 4. 照片配方标签：g266 与 g262 逐项一致（含 CLI/WASM/UI/导出）


def test_g266_recipe_tags_ui_and_export_match_g262(tmp_path: Path, volumes: Path) -> None:
    require_cli()
    raw266, library, _, _ = _need_g266()
    raw262, _, _, _ = g262.need_g262()
    dat266_path, fx_path = _stage_bundle(tmp_path, "tags-266", raw266, library)
    cam266 = g262.install_g262_calibrated_home(tmp_path / "home-266", raw266, library)
    cam262 = g262.install_g262_calibrated_home(tmp_path / "home-262", raw262, library)

    cases = g262.TAG_CASES
    wasm_outputs = _run_wasm_cases(tmp_path, [{"tag": t, "dat": str(dat266_path), "library": str(fx_path)} for _, t, _, _, _ in cases])
    for (label, tag_text, want_kind, want_name, want_levels), wasm_raw in zip(cases, wasm_outputs, strict=True):
        parsed = parse_recipe_tag(tag_text)
        assert parsed is not None, label
        payload266 = recipe_tag_payload(parsed, cam266)
        payload262 = recipe_tag_payload(parsed, cam262)
        assert payload266 == payload262 and payload266 is not None and payload266["name"] == want_name, label
        sel266, sel262 = recorded_recipe(parsed, cam266), recorded_recipe(parsed, cam262)
        assert sel266 == sel262 and sel266 is not None and sel266.kind == want_kind and sel266.levels_dict() == want_levels, label
        res266, res262 = cam266.resolve(sel266), cam262.resolve(sel262)
        assert res266.entry == res262.entry and res266.levels == want_levels, label
        expected_changes = {k: v for k, v in want_levels.items() if v != (100 if k == "amount" else 0)}
        zero_res = cam266.resolve(replace(sel266, levels=()))
        assert (res266.entry != zero_res.entry) if expected_changes else (res266.entry == zero_res.entry), label

        desk266, desk262 = _desktop_expected(tag_text, cam266), _desktop_expected(tag_text, cam262)
        same_redacted(desk266, desk262, f"g266 vs g262 {label} desktop")
        same_redacted(_cli_actual(tag_text, dat266_path, fx_path), desk266, f"g266 {label} cli vs desktop")
        same_redacted(_normalize_wasm(wasm_raw), desk266, f"g266 {label} wasm vs desktop")

    # 桌面与手机 UI 夹具和标签文本与 g262 完全一致
    fx266, fx262 = g262.build_ui_fixture(cam266), g262.build_ui_fixture(cam262)
    assert fx266 == fx262
    if shutil.which("node") is not None:
        web_stage = g262.stage_web_assets(REPO, tmp_path / "web_stage")
        p266 = tmp_path / "ui-266.json"
        p262 = tmp_path / "ui-262.json"
        p266.write_text(json.dumps(fx266, ensure_ascii=False), encoding="utf-8")
        p262.write_text(json.dumps(fx262, ensure_ascii=False), encoding="utf-8")
        out266 = subprocess.run(
            ["node", "--experimental-strip-types", str(UI_DRIVER), str(web_stage), str(REPO), str(p266)],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        out262 = subprocess.run(
            ["node", "--experimental-strip-types", str(UI_DRIVER), str(web_stage), str(REPO), str(p262)],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert out266.returncode == 0, out266.stdout + out266.stderr
        assert out262.returncode == 0, out262.stdout + out262.stderr
        assert json.loads(out266.stdout) == json.loads(out262.stdout)

    # 导出 EXIF（Copyright、entry_sha256、record_sha256）与渲染像素（单张 + 半格双联）与 g262 逐像素一致
    kino_ft_tag = g262.encode_tag("50", 68, g262.KINO_FINETUNE)
    acros_ft_tag = g262.encode_tag("50", 56, g262.ACROS_FINETUNE)
    exported: dict[int, dict[str, bytes]] = {}
    for gen, home_dir in ((266, tmp_path / "home-266"), (262, tmp_path / "home-262")):
        lib_dir = tmp_path / f"lib-{gen}"
        day = lib_dir / "2026" / "2026-11-23"
        day.mkdir(parents=True)
        g262.synth_tagged_pair(day, "R0002661", kino_ft_tag, width=128, height=96, seed=11, when="2026:11:23 10:00:01")
        g262.synth_tagged_pair(day, "R0002662", acros_ft_tag, width=128, height=96, seed=22, when="2026:11:23 10:00:05")
        cfg = Settings(
            library_dir=lib_dir,
            home_dir=home_dir,
            export_dir=tmp_path / f"exp-{gen}",
            volumes_roots=(volumes,),
            token="test-token-123",
            port=PORT,
            min_free_bytes=0,
        )
        lib = Library(cfg.library_dir).open()
        try:
            lib.rebuild()
            shots = {s["name"]: s for s in lib.list_shots()}
            render = RenderService(cfg.home_dir, lib)
            s1, s2 = shots["R0002661"], shots["R0002662"]
            edit1, side1 = render_edits.read(lib, s1, render.recipe_menu())
            assert edit1.recipe is not None and edit1.recipe.levels_dict() == g262.KINO_FINETUNE
            single = export_finish(
                render, lib, s1, edit1, side1, cfg.export_dir / "single", [], quality=95, long_edge=None, fmt="jpeg", base="auto"
            )
            with Image.open(single.path) as im:
                assert im.getexif().get(33432) == kino_ft_tag
            pair_shot = {
                **s2,
                "pair": {"role": "second", "partner_id": s1["shot_id"], "first_half": "left", "has_raw": True},
                "pair_sources": {"first": {**s1, "all_files": dict(s1["files"])}, "second": {**s2, "all_files": dict(s2["files"])}},
            }
            p1, p2, *_ = pair_plans(render, pair_shot, EditState())
            assert p1.levels == g262.KINO_FINETUNE and p2.levels == g262.ACROS_FINETUNE
            hf_out = export_pair_finish(
                render,
                lib,
                pair_shot,
                EditState(),
                {},
                cfg.export_dir / "hf",
                [],
                quality=95,
                long_edge=None,
                fmt="jpeg",
                base="auto",
                mode="both",
            )
            by_role = {role: res.path.read_bytes() for _sid, role, res in hf_out}
            exported[gen] = {"single": single.path.read_bytes(), **by_role}
        finally:
            lib.close()
    for role in ("single", "diptych", "first", "second"):
        with Image.open(io.BytesIO(exported[266][role])) as im266, Image.open(io.BytesIO(exported[262][role])) as im262:
            assert im266.getexif().get(33432) == im262.getexif().get(33432), role
            d266_raw, d262_raw = im266.getexif().get(270), im262.getexif().get(270)
            d266 = d266_raw.decode("utf-8") if isinstance(d266_raw, bytes) else str(d266_raw or "")
            d262 = d262_raw.decode("utf-8") if isinstance(d262_raw, bytes) else str(d262_raw or "")
            assert d266.split("; binding ")[0] == d262.split("; binding ")[0], role
            assert np.array_equal(np.asarray(im266.convert("RGB")), np.asarray(im262.convert("RGB"))), role


# --------------------------------------------------------------------------- 5. 手机浏览器：读 g266 卡 + Look 微调采用与桌面同图导出


@pytest.mark.browser_serial
def test_mobile_browser_reads_g266_card(tmp_path: Path) -> None:
    pair = mx.real_pairs(1)
    if len(pair) < 1:
        samples.missing("need one DNG+JPG pair")
    _browser_prereq()
    raw, library, dat_path, fx_path = _need_g266()
    dng, jpg = pair[0]

    home = tmp_path / "home-266-browser"
    store.import_from_dat(raw, home, library=library)
    cam = CameraData(home)
    qlaunch = next(row for row in cam.listing() if row["id"] == "qlaunch")
    kino = next(r for r in qlaunch["recipes"] if r.get("kind") == "record" and r["name"] == "K-KINO")
    extra_recipe_dict = {"kind": "record", "unit": "qlaunch", "slot": kino["slot"], "id": kino["id"], "levels": g262.KINO_FINETUNE}

    report_path = tmp_path / "g266-report.json"
    mpath = tmp_path / "g266-manifest.json"
    mpath.write_text(
        json.dumps(
            {
                "dng": str(dng),
                "jpg": str(jpg),
                "dat": str(dat_path),
                "bin": str(fx_path),
                "files": [str(dng), str(jpg), str(dat_path), str(fx_path)],
                "extraRecipe": extra_recipe_dict,
                "out": str(report_path),
            }
        ),
        encoding="utf-8",
    )
    result = _drive(R5_DAT_DRIVER, mpath, tmp_path, "", timeout=420)
    if result.returncode != 0 or not report_path.is_file():
        pytest.fail("r6 browser driver failed for g266")
    report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    check(report.get("error") is None, "driver finished cleanly")
    check(report.get("pageErrors") == [], "no page errors")
    assert "缺少 GXRFX.BIN" in report["sheet"]["text"]
    for step_key in ("second", "loaded"):
        phone = report[step_key]
        assert phone["entries"] == 24 * 552 and phone["generation"] == 266 and phone["library"] == len(library)
        same_redacted(phone["listing"], cam.listing(), "g266 phone listing equals desktop listing")
        sel = parse_recipe({"kind": "builtin", "unit": "jlaunch", "slot": 1, "levels": {}})
        assert sel is not None
        resolved = cam.resolve(sel)
        same_file_redacted(base64.b64decode(phone["entry"]), resolved.entry, "g266 resolve entry")
        same_redacted(phone["levels"], resolved.levels, "g266 resolve levels")
        same_redacted(phone["sha"], resolved.entry_sha256, "g266 resolve sha")
        extra_sel = parse_recipe(extra_recipe_dict)
        assert extra_sel is not None
        extra_resolved = cam.resolve(extra_sel)
        same_file_redacted(base64.b64decode(phone["extra"]["entry"]), extra_resolved.entry, "g266 K-KINO finetune resolve entry")
        same_redacted(phone["extra"]["levels"], extra_resolved.levels, "g266 K-KINO finetune resolve levels")
        same_redacted(phone["extra"]["sha"], extra_resolved.entry_sha256, "g266 K-KINO finetune resolve sha")


@pytest.mark.browser_serial
def test_mobile_browser_adopts_g266_look_finetune_and_matches_desktop_export(tmp_path: Path, pinned_icc: bytes) -> None:
    _browser_prereq()
    raw, library, dat_path, fx_path = _need_g266()
    tag_text = g262.encode_tag("50", 68, g262.KINO_FINETUNE)

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    dng_path, jpg_path = g262.synth_tagged_pair(src_dir, "R0002669", tag_text, width=64, height=48, seed=266)

    desk_root = tmp_path / "desk"
    desk_root.mkdir()
    desk = _R5Desk(desk_root, raw, library)
    try:
        shutil.copy2(dng_path, desk.folder / dng_path.name)
        shutil.copy2(jpg_path, desk.folder / jpg_path.name)
        desk.lib.rebuild()
        shot = desk.lib.list_shots()[0]
        assert shot["recipe_tag"] is not None
        edit, side = render_edits.read(desk.lib, shot, desk.render.recipe_menu())
        assert edit.recipe is not None and edit.recipe.levels_dict() == g262.KINO_FINETUNE
        desk_res = export_finish(
            desk.render, desk.lib, shot, edit, side, desk.export_dir, [], quality=95, long_edge=None, fmt="jpeg", base="auto"
        )
        desk_bytes = desk_res.path.read_bytes()

        out_dir = tmp_path / "phone-exports"
        out_dir.mkdir()
        report_path = tmp_path / "look-browser-report.json"
        mpath = tmp_path / "look-browser-manifest.json"
        mpath.write_text(
            json.dumps(
                {
                    "pack": str(desk.zip),
                    "binding": desk.binding(),
                    "stem": "R0002669",
                    "expectedName": "K-KINO",
                    "files": [str(dng_path), str(jpg_path), str(dat_path), str(fx_path)],
                    "outDir": str(out_dir),
                    "out": str(report_path),
                }
            ),
            encoding="utf-8",
        )
        result = _drive(MOBILE_LOOK_DRIVER, mpath, tmp_path, "", timeout=420)
        if result.returncode != 0 or not report_path.is_file():
            pytest.fail(f"mobile Look+finetune browser driver failed for g266: {result.stderr[-500:]}")
        report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
        check(report.get("error") is None, f"driver error: {report.get('error')}")
        check(report.get("pageErrors") == [], f"pageErrors: {report.get('pageErrors')}")

        card = report["card"]
        assert card["topRecipe"] == "K-KINO" and card["heading"] == "K-KINO" and card["stripOnName"] == "K-KINO"
        assert card["choice"] is None
        assert card["sideRecipe"]["levels"] == g262.KINO_FINETUNE
        assert report["export"]["fileCount"] == 1
        exported = sorted(out_dir.glob("phone-*"))
        assert len(exported) == 1
        phone_bytes = exported[0].read_bytes()
        with Image.open(io.BytesIO(phone_bytes)) as pim, Image.open(io.BytesIO(desk_bytes)) as dim:
            assert pim.getexif().get(33432) == tag_text and pim.getexif().get(270) == dim.getexif().get(270)
        same_file_redacted(phone_bytes, desk_bytes, "g266 phone adopted Look+finetune export matches desktop byte-for-byte")
    finally:
        desk.close()
