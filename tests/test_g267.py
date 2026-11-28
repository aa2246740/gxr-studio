"""g267 DAT 读卡、文件与上传导入、错误库与篡改拒收（钉住检查项）、g266/g267 互不冒认、CLI/WASM/手机对拍。

共享 g262 Look+微调标签用例在 g267 与 g266 下逐项一致。
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import struct
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from gxr_studio import audit
from gxr_studio.camdata import datcodec as dc
from gxr_studio.camdata import intake, knob_table, store
from gxr_studio.camdata.engine_bindings import BOUND_ENGINES
from gxr_studio.camdata.recipe_tags import parse_recipe_tag
from gxr_studio.engine import menu as py_menu
from gxr_studio.render.camera_recipe import recipe_tag_payload, recorded_recipe
from gxr_studio.render.params import RecipeSel, parse_recipe
from gxr_studio.render.recipes import CameraData
from support import g262_cases as g262
from support import mobile_ui_export as mx
from support import samples
from support.core_cli import error_of, require_cli, run_json, run_npy
from support.export_pipeline_cases import check, same_file_redacted, same_redacted
from support.wasm_check import run_area
from test_camdata_api import client as client
from test_camdata_api import forge_generation
from test_camdata_api import reader as reader
from test_core_parity_camdata import _assert_parse_matches_python_error, _expected_parse_summary
from test_core_parity_camera_recipe import _cli_actual, _desktop_expected, _normalize_wasm, _run_wasm_cases
from test_datcodec import reseal
from test_g250 import KUBIK_LIB_SHA256, R5_DAT_DRIVER, _browser_prereq, _need_g185_or_g250
from test_g256 import _stage_bundle
from test_g260 import _fresh_client
from test_g266 import G266_DAT_SHA256, _need_g266
from test_mobile_ui_browser import _drive

__all__ = ["client", "reader"]

REPO = Path(__file__).resolve().parents[1]
UI_DRIVER = REPO / "tests" / "support" / "g262_look_finetune_ui.mjs"

G267_DAT_SHA256 = "1008af26ada41c0d5906aabf08056156f2bd4b0730f9b087051bf8e2241151c8"
G267_REL_DIR = "firmware/package_v310b_clut_static_r1/local/test267-REVIEW_PENDING"
G267_ENGINE_SHA256 = "a1ae063be5c25d3b545137ab923add0c8f142996efb746b4d6d58ca56a43da89"
G267_RELOCATIONS = [(93684, 102593), (93704, 71676)]
RESIDENT_BUILD_ID = "18e530aa29a667824828e1b21d1f10b15c9b6eeee4f500d5607436583789457d"
RESIDENT_ABI_ID = "9bad13d535a686a911f246acbac36f34"

# 本单元只新增 267；其余代数与 main 上一致，任何别的新代数都不应出现。
REGISTERED_GENERATIONS = (
    127, 129, 131, 135, 137, 138, 150, 152, 153, 154, 155, 156, 158,
    176, 178, 181, 182, 185, 250, 253, 256, 257, 258, 259, 260, 262, 266, 267,
)  # fmt: skip
BOUND_GENERATIONS = (156, 158, 176, 178, 181, 182, 185, 250, 253, 256, 257, 258, 259, 260, 262, 266, 267)


def _need_g267() -> tuple[bytes, bytes, Path, Path]:
    parent = samples.sample_parent()
    dat_path = parent / G267_REL_DIR / "GXRCE.DAT"
    fx_path = parent / "firmware/package_v310b_kubik_lib_r6/local/test-REVIEW_PENDING/GXRFX.BIN"
    if not dat_path.is_file():
        samples.missing("g267 本地 DAT 不在本机")
    if not fx_path.is_file():
        samples.missing("KUBIK 本地测试库不在本机")
    raw, library = dat_path.read_bytes(), fx_path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == G267_DAT_SHA256, "g267 DAT 摘要已变化"
    assert hashlib.sha256(library).hexdigest() == KUBIK_LIB_SHA256, "KUBIK 库摘要已变化"
    return raw, library, dat_path, fx_path


# --------------------------------------------------------------------------- 1. 登记表：只多 267，g266 与更早版本原样保留


def test_only_g267_is_newly_registered_and_g266_is_unchanged() -> None:
    assert tuple(sorted(dc.PROFILES)) == REGISTERED_GENERATIONS
    assert tuple(sorted(BOUND_ENGINES)) == BOUND_GENERATIONS
    p267, p266 = dc.PROFILES[267], dc.PROFILES[266]
    assert replace(p267, generation=266) == p266
    assert p267.library_sha256 == (KUBIK_LIB_SHA256,)
    (desc,) = BOUND_ENGINES[267]
    assert desc.engine_sha256 == G267_ENGINE_SHA256 and desc.relocations == tuple(G267_RELOCATIONS)
    (desc266,) = BOUND_ENGINES[266]
    assert desc266.engine_sha256 == "8fae9c79c895852d9090831c584255879dd5b6eb48473c0ebc4e1a03391693e9"
    assert desc266.relocations == ((93696, 102789), (93716, 71708))


# --------------------------------------------------------------------------- 2. g267 DAT 解码、往返与导入


def test_g267_decode_round_trip_and_intake(tmp_path: Path, client: Any, volumes: Path, reader: Any) -> None:
    raw, kubik_lib, _, _ = _need_g267()
    raw266, _, _, _ = _need_g266()

    info = intake.inspect_dat(raw, kubik_lib)
    assert info.generation == 267
    assert info.size == 97884 == len(raw)
    assert info.sha256 == G267_DAT_SHA256

    dat = dc.parse(raw, kubik_lib)
    assert dat.generation == 267 and dc.pack(dat) == raw and dat.library == kubik_lib
    assert dat.relocations == G267_RELOCATIONS
    assert dat.entry == 85732 and dat.bss == 9064
    assert len(dat.engine) == 93612 and hashlib.sha256(dat.engine).hexdigest() == G267_ENGINE_SHA256
    assert len(dat.xdata) == 4000
    assert dat.resident_id.hex() == RESIDENT_BUILD_ID and dat.abi_id.hex() == RESIDENT_ABI_ID
    assert 7628 + ((len(raw) + dat.bss + 3) & ~3) == 114576
    assert len(dat.recipes) == 66
    assert all(len(m.entries) == 8 for m in dat.modules)

    dat266 = dc.parse(raw266, kubik_lib)
    assert dat266.generation == 266 and dc.pack(dat266) == raw266
    assert dat.resident_id == dat266.resident_id and dat.abi_id == dat266.abi_id

    dat_copy, _ = _stage_bundle(tmp_path, "bundle-267", raw, kubik_lib)
    assert intake.read_bundle_from_path(dat_copy) == (raw, "GXRCE.DAT", kubik_lib)

    home, home266 = tmp_path / "home-267", tmp_path / "home-266-ref"
    store.import_from_dat(raw, home, library=kubik_lib)
    store.import_from_dat(raw266, home266, library=kubik_lib)
    cam, cam266 = CameraData(home), CameraData(home266)
    assert cam.listing() == cam266.listing()
    for unit_listing in cam.listing():
        recipes = unit_listing["recipes"]
        assert len(recipes) == 26
        assert [r["name"] for r in recipes[-3:]] == ["K-0815", "K-PACIFIC", "K-KINO"]
        for item in recipes:
            sel = RecipeSel(unit_listing["id"], item["slot"], kind=item.get("kind", "builtin"), id=item.get("id"))
            assert cam.resolve(sel).entry == cam266.resolve(sel).entry, (unit_listing["id"], item["position"])

    # 卡导入（只读）
    card_root = volumes / "GXR267"
    (card_root / "DCIM" / "100RICOH").mkdir(parents=True)
    (card_root / "GXRCE.DAT").write_bytes(raw)
    (card_root / "GXRFX.BIN").write_bytes(kubik_lib)
    with audit.AuditRecorder(watch=(str(volumes), str(volumes.resolve()))) as rec:
        card_resp = client.post("/api/camdata/import-card")
    assert card_resp.status_code == 200, card_resp.text
    card_body = card_resp.json()
    assert card_body["imported"] is True and card_body["generation"] == 267 and card_body["sha256"] == G267_DAT_SHA256
    assert "28mm 26 条" in card_body["message"]
    assert rec.writes_under(volumes) == []
    assert set(reader.opened) == {str(card_root / "GXRCE.DAT"), str(card_root / "GXRFX.BIN")}

    # 本地文件与目录导入
    with _fresh_client(tmp_path, "file-267", volumes) as file_client:
        file_resp = file_client.post("/api/camdata/import-file", json={"path": str(dat_copy)})
        assert file_resp.status_code == 200, file_resp.text
        file_body = file_resp.json()
        assert file_body["imported"] is True and file_body["generation"] == 267 and file_body["sha256"] == G267_DAT_SHA256
        assert "28mm 26 条" in file_body["message"]
        dir_resp = file_client.post("/api/camdata/import-file", json={"path": str(dat_copy.parent)})
        assert dir_resp.status_code == 200 and dir_resp.json()["unchanged"] is True and dir_resp.json()["generation"] == 267

    # 浏览器上传导入
    with _fresh_client(tmp_path, "upload-267", volumes) as upload_client:
        resp = upload_client.post("/api/camdata/import-upload", content=raw + kubik_lib, headers={"X-GXR-Dat-Bytes": str(len(raw))})
        assert resp.status_code == 200 and resp.json()["imported"] is True and resp.json()["generation"] == 267
        assert "28mm 26 条" in resp.json()["message"]


# --------------------------------------------------------------------------- 3. 错误库拒收（dat_invalid）


def test_g267_rejects_wrong_library(tmp_path: Path) -> None:
    raw, kubik_lib, _, _ = _need_g267()
    _, lib185, _, fx185_path = _need_g185_or_g250(185)
    _, lib250_old, _, fx250_old_path = _need_g185_or_g250(250)

    for bad_lib in (None, lib185, lib250_old):
        with pytest.raises(intake.IntakeError) as exc_info:
            intake.inspect_dat(raw, bad_lib)
        assert exc_info.value.code == "dat_invalid", exc_info.value.code
    assert intake.inspect_dat(raw, kubik_lib).generation == 267

    require_cli()
    dat_path = tmp_path / "g267.dat"
    dat_path.write_bytes(raw)
    for fx in (None, fx185_path, fx250_old_path):
        _assert_parse_matches_python_error(dat_path, raw, fx)


# --------------------------------------------------------------------------- 4. 篡改拒收：钉住是哪一项检查拒收的


def _tampered(raw: bytes, label: str) -> bytes:
    out = bytearray(raw)
    if label == "bad_xdata":
        xdata_at, xdata_len = struct.unpack_from(">II", raw, 128 + 12 * 2 + 4)
        assert xdata_len == 4000
        out[xdata_at] ^= 0x01
    elif label == "bad_engine":
        out[300] ^= 0x01
    elif label == "bad_sec3":
        struct.pack_into(">II", out, 128 + 12 * 3 + 4, len(raw), 4)
        out += b"\x00\x00\x00\x00"
        struct.pack_into(">I", out, 16, len(out))
        for idx in range(4, 7):
            struct.pack_into(">II", out, 128 + 12 * idx + 4, len(out), 0)
    elif label == "bad_ram":
        struct.pack_into(">I", out, 116, dc.MAX_RAM - len(raw) + 4)
    elif label == "bad_reloc2":
        reloc_at, reloc_len = struct.unpack_from(">II", raw, 128 + 12 + 4)
        assert reloc_len == 16
        out[reloc_at + 15] ^= 0x01
    elif label == "bad_bss":
        struct.pack_into(">I", out, 116, dc.u32(raw, 116) + 4)
    else:  # pragma: no cover - 参数表与这里一一对应
        raise AssertionError(label)
    return reseal(bytes(out))


@pytest.mark.parametrize(
    ("label", "expected_msg"),
    [
        ("bad_xdata", "267 引擎或链接布局未登记"),
        ("bad_engine", "267 引擎或链接布局未登记"),
        ("bad_bss", "267 引擎或链接布局未登记"),
        ("bad_sec3", "r5 执行外壳尺寸不合格"),
        ("bad_ram", "r5 BSS 或重定位不合格"),
        ("bad_reloc2", "r5 重定位与引擎不匹配"),
    ],
)
def test_g267_tamper_is_rejected_by_the_named_check(label: str, expected_msg: str, tmp_path: Path) -> None:
    raw, kubik_lib, _, _ = _need_g267()
    tampered = _tampered(raw, label)
    with pytest.raises(dc.DatError) as exc:
        dc.parse(tampered, kubik_lib)
    assert not isinstance(exc.value, dc.UnsupportedGeneration), label
    assert str(exc.value) == expected_msg, label
    with pytest.raises(intake.IntakeError) as intake_exc:
        intake.inspect_dat(tampered, kubik_lib)
    assert intake_exc.value.code == "dat_invalid"

    require_cli()
    dat_path, fx_path = _stage_bundle(tmp_path, f"tamper-{label}", tampered, kubik_lib)
    _assert_parse_matches_python_error(dat_path, tampered, fx_path)


# --------------------------------------------------------------------------- 5. g266 与 g267 互不冒认


@pytest.mark.parametrize(("source", "relabel"), [(267, 266), (266, 267)], ids=("g267_as_266", "g266_as_267"))
def test_g266_and_g267_files_are_not_decoded_as_each_other(source: int, relabel: int, tmp_path: Path) -> None:
    raw267, kubik_lib, _, _ = _need_g267()
    raw266, _, _, _ = _need_g266()
    assert hashlib.sha256(raw266).hexdigest() == G266_DAT_SHA256
    raw = raw267 if source == 267 else raw266
    assert dc.peek_generation(raw) == source and dc.parse(raw, kubik_lib).generation == source

    forged = forge_generation(raw, relabel)
    assert dc.peek_generation(forged) == relabel
    with pytest.raises(dc.DatError) as exc:
        dc.parse(forged, kubik_lib)
    assert not isinstance(exc.value, dc.UnsupportedGeneration)
    assert str(exc.value) == f"{relabel} 引擎或链接布局未登记"
    with pytest.raises(intake.IntakeError) as intake_exc:
        intake.inspect_dat(forged, kubik_lib)
    assert intake_exc.value.code == "dat_invalid"

    require_cli()
    dat_path, fx_path = _stage_bundle(tmp_path, f"relabel-{source}-{relabel}", forged, kubik_lib)
    _assert_parse_matches_python_error(dat_path, forged, fx_path)


# --------------------------------------------------------------------------- 6. CLI / WASM 对拍


def test_rust_cli_and_wasm_parity_for_g267(tmp_path: Path) -> None:
    require_cli()
    raw, library, _, _ = _need_g267()
    dat_path, fx_path = _stage_bundle(tmp_path, "parity-267", raw, library)

    expected, entries = _expected_parse_summary(raw, library)
    code, payload = run_json(["camdata-parse", "--dat", str(dat_path), "--library", str(fx_path), "--out-dir", str(tmp_path / "entries-267")])
    assert error_of(code, payload) is None and payload == expected and len(payload["recipes"]) == 66
    assert payload["generation"] == 267

    arr = run_npy(["camdata-entries", "--dat", str(dat_path), "--library", str(fx_path)], tmp_path / "entries-267.npy")
    expected_arr = np.frombuffer(b"".join(entries[(u, s)] for u in range(3) for s in store.SLOTS), dtype=np.uint8).reshape(24, 552)
    assert np.array_equal(arr, expected_arr)

    dat = dc.parse(raw, library)
    assert dat.kv2_block is not None
    expected_menu = knob_table.read(dat.kv2_block, py_menu.load(), mount_knobs_from_50mm=dc.PROFILES[267].mount_knobs_from_50mm)
    mcode, mpayload = run_json(["camdata-menu", "--dat", str(dat_path), "--library", str(fx_path)])
    assert error_of(mcode, mpayload) is None and mpayload == expected_menu

    wasm_dir = tmp_path / "wasm-267"
    wasm_dir.mkdir()
    proc = run_area("camdata", [dat_path], wasm_dir)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --------------------------------------------------------------------------- 7. 照片配方标签：共享 g262 用例在 g267 与 g266 下逐项一致


@pytest.fixture(scope="module")
def tag_homes(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    raw267, library, _, _ = _need_g267()
    raw266, _, _, _ = _need_g266()
    root = tmp_path_factory.mktemp("g267-tags")
    dat267_path, fx_path = _stage_bundle(root, "tags-267", raw267, library)
    return {
        "cam267": g262.install_g262_calibrated_home(root / "home-267", raw267, library),
        "cam266": g262.install_g262_calibrated_home(root / "home-266", raw266, library),
        "dat267": dat267_path,
        "fx": fx_path,
    }


@pytest.mark.parametrize("case", g262.TAG_CASES, ids=[c[0] for c in g262.TAG_CASES])
def test_g262_tag_case_decodes_the_same_under_g267_and_g266(case: tuple[str, str, str, str, dict[str, int]], tag_homes: dict[str, Any]) -> None:
    label, tag_text, want_kind, want_name, want_levels = case
    cam267, cam266 = tag_homes["cam267"], tag_homes["cam266"]
    parsed = parse_recipe_tag(tag_text)
    assert parsed is not None, label
    payload267 = recipe_tag_payload(parsed, cam267)
    assert payload267 == recipe_tag_payload(parsed, cam266) and payload267 is not None and payload267["name"] == want_name, label
    sel267, sel266 = recorded_recipe(parsed, cam267), recorded_recipe(parsed, cam266)
    assert sel267 == sel266 and sel267 is not None and sel267.kind == want_kind and sel267.levels_dict() == want_levels, label
    res267, res266 = cam267.resolve(sel267), cam266.resolve(sel266)
    assert res267.entry == res266.entry and res267.entry_sha256 == res266.entry_sha256 and res267.levels == want_levels, label
    expected_changes = {k: v for k, v in want_levels.items() if v != (100 if k == "amount" else 0)}
    zero_res = cam267.resolve(replace(sel267, levels=()))
    assert (res267.entry != zero_res.entry) if expected_changes else (res267.entry == zero_res.entry), label

    desk267 = _desktop_expected(tag_text, cam267)
    same_redacted(desk267, _desktop_expected(tag_text, cam266), f"g267 vs g266 {label} desktop")
    require_cli()
    same_redacted(_cli_actual(tag_text, tag_homes["dat267"], tag_homes["fx"]), desk267, f"g267 {label} cli vs desktop")


def test_g262_tag_cases_wasm_and_ui_match_g266(tmp_path: Path, tag_homes: dict[str, Any]) -> None:
    cam267, cam266 = tag_homes["cam267"], tag_homes["cam266"]
    cases = g262.TAG_CASES
    dat267, fx = str(tag_homes["dat267"]), str(tag_homes["fx"])
    wasm_outputs = _run_wasm_cases(tmp_path, [{"tag": t, "dat": dat267, "library": fx} for _, t, _, _, _ in cases])
    for (label, tag_text, *_rest), wasm_raw in zip(cases, wasm_outputs, strict=True):
        same_redacted(_normalize_wasm(wasm_raw), _desktop_expected(tag_text, cam266), f"g267 {label} wasm vs g266 desktop")

    fx267, fx266 = g262.build_ui_fixture(cam267), g262.build_ui_fixture(cam266)
    assert fx267 == fx266
    if shutil.which("node") is None:
        return
    web_stage = g262.stage_web_assets(REPO, tmp_path / "web_stage")
    outputs = []
    for gen, fixture in ((267, fx267), (266, fx266)):
        path = tmp_path / f"ui-{gen}.json"
        path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
        proc = subprocess.run(
            ["node", "--experimental-strip-types", str(UI_DRIVER), str(web_stage), str(REPO), str(path)],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        outputs.append(json.loads(proc.stdout))
    assert outputs[0] == outputs[1]


# --------------------------------------------------------------------------- 8. 手机浏览器读 g267 卡


@pytest.mark.browser_serial
def test_mobile_browser_reads_g267_card(tmp_path: Path) -> None:
    pair = mx.real_pairs(1)
    if len(pair) < 1:
        samples.missing("need one DNG+JPG pair")
    _browser_prereq()
    raw, library, dat_path, fx_path = _need_g267()
    dng, jpg = pair[0]

    home = tmp_path / "home-267-browser"
    store.import_from_dat(raw, home, library=library)
    cam = CameraData(home)
    qlaunch = next(row for row in cam.listing() if row["id"] == "qlaunch")
    kino = next(r for r in qlaunch["recipes"] if r.get("kind") == "record" and r["name"] == "K-KINO")
    extra_recipe_dict = {"kind": "record", "unit": "qlaunch", "slot": kino["slot"], "id": kino["id"], "levels": g262.KINO_FINETUNE}

    report_path = tmp_path / "g267-report.json"
    mpath = tmp_path / "g267-manifest.json"
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
        pytest.fail("r6 browser driver failed for g267")
    report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    check(report.get("error") is None, "driver finished cleanly")
    check(report.get("pageErrors") == [], "no page errors")
    assert "缺少 GXRFX.BIN" in report["sheet"]["text"]
    for step_key in ("second", "loaded"):
        phone = report[step_key]
        assert phone["entries"] == 24 * 552 and phone["generation"] == 267 and phone["library"] == len(library)
        same_redacted(phone["listing"], cam.listing(), "g267 phone listing equals desktop listing")
        sel = parse_recipe({"kind": "builtin", "unit": "jlaunch", "slot": 1, "levels": {}})
        assert sel is not None
        resolved = cam.resolve(sel)
        same_file_redacted(base64.b64decode(phone["entry"]), resolved.entry, "g267 resolve entry")
        same_redacted(phone["levels"], resolved.levels, "g267 resolve levels")
        same_redacted(phone["sha"], resolved.entry_sha256, "g267 resolve sha")
        extra_sel = parse_recipe(extra_recipe_dict)
        assert extra_sel is not None
        extra_resolved = cam.resolve(extra_sel)
        same_file_redacted(base64.b64decode(phone["extra"]["entry"]), extra_resolved.entry, "g267 K-KINO finetune resolve entry")
        same_redacted(phone["extra"]["levels"], extra_resolved.levels, "g267 K-KINO finetune resolve levels")
        same_redacted(phone["extra"]["sha"], extra_resolved.entry_sha256, "g267 K-KINO finetune resolve sha")
