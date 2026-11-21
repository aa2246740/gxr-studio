"""g259/g260 DAT 读卡、文件与上传导入、错误库/篡改拒收、Rust CLI/WASM 对拍及手机端读卡测试。"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import PORT
from gxr_studio import audit
from gxr_studio.app import create_app
from gxr_studio.camdata import datcodec as dc
from gxr_studio.camdata import intake, knob_table, store
from gxr_studio.config import Settings
from gxr_studio.engine import menu as py_menu
from gxr_studio.render.params import RecipeSel, parse_recipe
from gxr_studio.render.recipes import CameraData
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
from test_g256 import _stage_bundle
from test_mobile_ui_browser import _drive

__all__ = ["client", "reader"]

G259_DAT_SHA256 = "9923c805112ee7dc8f2b4723cd6f97ba8c348e2927b6002a156ac7af7a28f23d"
G260_DAT_SHA256 = "93dd9b124da94a1248cc7bf3e284e096fb79c7ef7981bd1c318e39aea9ae872b"

_DAT_SPECS: dict[int, tuple[str, str, int, int]] = {
    259: ("firmware/package_v310b_off_image_r1/local/test259-REVIEW_PENDING", G259_DAT_SHA256, 88044, 4020),
    260: ("firmware/package_v310b_off_image_r2/local/test260-REVIEW_PENDING", G260_DAT_SHA256, 88740, 4020),
}


def _need_g259_or_g260(generation: int) -> tuple[bytes, bytes, Path, Path]:
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


def _fresh_client(tmp_path: Path, tag: str, volumes: Path) -> TestClient:
    root = tmp_path / f"app-{tag}"
    cfg = Settings(
        library_dir=root / "library",
        home_dir=root / "app-home",
        export_dir=root / "exports",
        volumes_roots=(volumes,),
        token="test-token-123",
        port=PORT,
        min_free_bytes=0,
    )
    app = create_app(cfg)
    return TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": cfg.token})


@pytest.mark.parametrize("generation", (259, 260))
def test_g259_and_g260_decode_round_trip_and_intake(
    generation: int,
    tmp_path: Path,
    client: Any,
    volumes: Path,
    reader: Any,
) -> None:
    raw, kubik_lib, _, _ = _need_g259_or_g260(generation)
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

    # 卡导入（只读）
    card_root = volumes / f"GXR{generation}"
    (card_root / "DCIM" / "100RICOH").mkdir(parents=True)
    (card_root / "GXRCE.DAT").write_bytes(raw)
    (card_root / "GXRFX.BIN").write_bytes(kubik_lib)
    with audit.AuditRecorder(watch=(str(volumes), str(volumes.resolve()))) as rec:
        card_resp = client.post("/api/camdata/import-card")
    assert card_resp.status_code == 200, card_resp.text
    card_body = card_resp.json()
    assert card_body["imported"] is True and card_body["generation"] == generation and card_body["sha256"] == want_sha
    assert "28mm 26 条" in card_body["message"]
    assert rec.writes_under(volumes) == []
    assert set(reader.opened) == {str(card_root / "GXRCE.DAT"), str(card_root / "GXRFX.BIN")}

    # 本地文件与目录导入
    with _fresh_client(tmp_path, f"file-{generation}", volumes) as file_client:
        file_resp = file_client.post("/api/camdata/import-file", json={"path": str(dat_copy)})
        assert file_resp.status_code == 200, file_resp.text
        file_body = file_resp.json()
        assert file_body["imported"] is True and file_body["generation"] == generation and file_body["sha256"] == want_sha
        assert "28mm 26 条" in file_body["message"]
        dir_resp = file_client.post("/api/camdata/import-file", json={"path": str(dat_copy.parent)})
        assert dir_resp.status_code == 200, dir_resp.text
        assert dir_resp.json()["unchanged"] is True and dir_resp.json()["generation"] == generation

    # 浏览器上传导入
    with _fresh_client(tmp_path, f"upload-{generation}", volumes) as upload_client:
        resp = upload_client.post("/api/camdata/import-upload", content=raw + kubik_lib, headers={"X-GXR-Dat-Bytes": str(len(raw))})
        assert resp.status_code == 200, resp.text
        assert resp.json()["imported"] is True and resp.json()["generation"] == generation
        assert "28mm 26 条" in resp.json()["message"]


@pytest.mark.parametrize("generation", (259, 260))
def test_g259_and_g260_reject_wrong_library_and_envelope_tamper(generation: int, tmp_path: Path) -> None:
    raw, kubik_lib, _, _ = _need_g259_or_g260(generation)
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

    bad_engine = bytearray(raw)
    bad_engine[300] ^= 0x01
    bad_engine_bytes = reseal(bytes(bad_engine))
    with pytest.raises(dc.DatError) as exc_eng:
        dc.parse(bad_engine_bytes, kubik_lib)
    assert not isinstance(exc_eng.value, dc.UnsupportedGeneration)

    require_cli()
    _, fx_kubik_copy = _stage_bundle(tmp_path, f"neg-{generation}", raw, kubik_lib)
    for path, blob, fx in (
        (tmp_path / f"g{generation}_missing_lib.dat", raw, None),
        (tmp_path / f"g{generation}_wrong_lib185.dat", raw, fx185_path),
        (tmp_path / f"g{generation}_wrong_lib250.dat", raw, fx250_old_path),
        (tmp_path / f"g{generation}_bad_xdata.dat", bad_xdata_bytes, fx_kubik_copy),
        (tmp_path / f"g{generation}_bad_sec3.dat", bad_sec3_bytes, fx_kubik_copy),
        (tmp_path / f"g{generation}_bad_engine.dat", bad_engine_bytes, fx_kubik_copy),
    ):
        path.write_bytes(blob)
        _assert_parse_matches_python_error(path, blob, fx)


@pytest.mark.parametrize("generation", (259, 260))
def test_rust_cli_and_wasm_parity_for_g259_and_g260(generation: int, tmp_path: Path) -> None:
    require_cli()
    raw, library, _, _ = _need_g259_or_g260(generation)
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
@pytest.mark.parametrize("generation", (259, 260))
def test_mobile_browser_reads_g259_and_g260_card(generation: int, tmp_path: Path) -> None:
    pair = mx.real_pairs(1)
    if len(pair) < 1:
        samples.missing("need one DNG+JPG pair")
    _browser_prereq()
    raw, library, dat_path, fx_path = _need_g259_or_g260(generation)
    dng, jpg = pair[0]

    home = tmp_path / f"home-{generation}-browser"
    store.import_from_dat(raw, home, library=library)
    cam = CameraData(home)
    qlaunch = next(row for row in cam.listing() if row["id"] == "qlaunch")
    kino = next(r for r in qlaunch["recipes"] if r.get("kind") == "record" and r["name"] == "K-KINO")
    extra_recipe_dict = {"kind": "record", "unit": "qlaunch", "slot": kino["slot"], "id": kino["id"], "levels": {}}

    report_path = tmp_path / f"g{generation}-report.json"
    manifest: dict[str, Any] = {
        "dng": str(dng),
        "jpg": str(jpg),
        "dat": str(dat_path),
        "bin": str(fx_path),
        "files": [str(dng), str(jpg), str(dat_path), str(fx_path)],
        "extraRecipe": extra_recipe_dict,
        "out": str(report_path),
    }
    mpath = tmp_path / f"g{generation}-manifest.json"
    mpath.write_text(json.dumps(manifest), encoding="utf-8")
    result = _drive(R5_DAT_DRIVER, mpath, tmp_path, "", timeout=420)
    if result.returncode != 0 or not report_path.is_file():
        pytest.fail(f"r6 browser driver failed for g{generation}")
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
        extra_sel = parse_recipe(extra_recipe_dict)
        assert extra_sel is not None
        extra_resolved = cam.resolve(extra_sel)
        same_file_redacted(base64.b64decode(phone["extra"]["entry"]), extra_resolved.entry, f"g{generation} K-KINO resolve entry")
        same_redacted(phone["extra"]["levels"], extra_resolved.levels, f"g{generation} K-KINO resolve levels")
        same_redacted(phone["extra"]["sha"], extra_resolved.entry_sha256, f"g{generation} K-KINO resolve sha")
