"""g262 DAT 读卡、文件与上传导入、错误库/篡改/g261 拒收、CLI/WASM/手机对拍，以及 Look + 微调全链路测试。"""

from __future__ import annotations

import base64
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
from test_g250 import R5_DAT_DRIVER, _browser_prereq, _need_g185_or_g250, _need_kubik_bundle
from test_g256 import _stage_bundle
from test_g260 import _fresh_client
from test_mobile_r5_dat import _R5Desk, pinned_icc
from test_mobile_ui_browser import _drive

__all__ = ["client", "pinned_icc", "reader"]

REPO = Path(__file__).resolve().parents[1]
UI_DRIVER = REPO / "tests" / "support" / "g262_look_finetune_ui.mjs"
MOBILE_LOOK_DRIVER = REPO / "tests" / "support" / "mobile_g262_look_browser.mjs"


# --------------------------------------------------------------------------- 1. g262 DAT 解码、往返与导入


def test_g262_decode_round_trip_and_intake(tmp_path: Path, client: Any, volumes: Path, reader: Any) -> None:
    raw, kubik_lib, _, _ = g262.need_g262()
    raw250, _, _, _ = _need_kubik_bundle()

    info = intake.inspect_dat(raw, kubik_lib)
    assert info.generation == 262
    assert info.size == 93380 and info.size == len(raw)
    assert info.sha256 == g262.G262_DAT_SHA256

    dat = dc.parse(raw, kubik_lib)
    assert dat.generation == 262 and dc.pack(dat) == raw and dat.library == kubik_lib
    assert len(dat.engine) == 89096 and 81920 < len(dat.engine) <= 98304
    assert len(dat.xdata) == 4020
    assert len(dat.recipes) == 66
    assert all(len(m.entries) == 8 for m in dat.modules)

    dat_copy, _ = _stage_bundle(tmp_path, "bundle-262", raw, kubik_lib)
    assert intake.read_bundle_from_path(dat_copy) == (raw, "GXRCE.DAT", kubik_lib)

    home, home250 = tmp_path / "home-262", tmp_path / "home-250-ref"
    store.import_from_dat(raw, home, library=kubik_lib)
    store.import_from_dat(raw250, home250, library=kubik_lib)
    cam, cam250 = CameraData(home), CameraData(home250)

    for unit_idx, unit_listing in enumerate(cam.listing()):
        recipes = unit_listing["recipes"]
        assert len(recipes) == 26
        assert [r["name"] for r in recipes[-3:]] == ["K-0815", "K-PACIFIC", "K-KINO"]
        for item in recipes:
            sel = RecipeSel(unit_listing["id"], item["slot"], kind=item.get("kind", "builtin"), id=item.get("id"))
            assert cam.resolve(sel).entry == cam250.resolve(sel).entry, (unit_idx, item["position"])

    # 卡导入（只读）
    card_root = volumes / "GXR262"
    (card_root / "DCIM" / "100RICOH").mkdir(parents=True)
    (card_root / "GXRCE.DAT").write_bytes(raw)
    (card_root / "GXRFX.BIN").write_bytes(kubik_lib)
    with audit.AuditRecorder(watch=(str(volumes), str(volumes.resolve()))) as rec:
        card_resp = client.post("/api/camdata/import-card")
    assert card_resp.status_code == 200, card_resp.text
    card_body = card_resp.json()
    assert card_body["imported"] is True and card_body["generation"] == 262 and card_body["sha256"] == g262.G262_DAT_SHA256
    assert "28mm 26 条" in card_body["message"]
    assert rec.writes_under(volumes) == []
    assert set(reader.opened) == {str(card_root / "GXRCE.DAT"), str(card_root / "GXRFX.BIN")}

    # 本地文件与目录导入
    with _fresh_client(tmp_path, "file-262", volumes) as file_client:
        file_resp = file_client.post("/api/camdata/import-file", json={"path": str(dat_copy)})
        assert file_resp.status_code == 200, file_resp.text
        file_body = file_resp.json()
        assert file_body["imported"] is True and file_body["generation"] == 262 and file_body["sha256"] == g262.G262_DAT_SHA256
        assert "28mm 26 条" in file_body["message"]
        dir_resp = file_client.post("/api/camdata/import-file", json={"path": str(dat_copy.parent)})
        assert dir_resp.status_code == 200 and dir_resp.json()["unchanged"] is True and dir_resp.json()["generation"] == 262

    # 浏览器上传导入
    with _fresh_client(tmp_path, "upload-262", volumes) as upload_client:
        resp = upload_client.post("/api/camdata/import-upload", content=raw + kubik_lib, headers={"X-GXR-Dat-Bytes": str(len(raw))})
        assert resp.status_code == 200 and resp.json()["imported"] is True and resp.json()["generation"] == 262
        assert "28mm 26 条" in resp.json()["message"]


# --------------------------------------------------------------------------- 2. 错误库、篡改与 g261 拒收


def test_g262_rejects_wrong_library_and_envelope_tamper(tmp_path: Path) -> None:
    raw, kubik_lib, _, _ = g262.need_g262()
    _, lib185, _, fx185_path = _need_g185_or_g250(185)
    _, lib250_old, _, fx250_old_path = _need_g185_or_g250(250)

    for bad_lib in (None, lib185, lib250_old):
        with pytest.raises(intake.IntakeError) as exc_info:
            intake.inspect_dat(raw, bad_lib)
        assert exc_info.value.code == "dat_invalid", exc_info.value.code

    xdata_at, xdata_len = struct.unpack_from(">II", raw, 128 + 12 * 2 + 4)
    assert xdata_len == 4020
    bad_xdata = bytearray(raw)
    bad_xdata[xdata_at] ^= 0x01
    bad_xdata_bytes = reseal(bytes(bad_xdata))

    bad_sec3 = bytearray(raw)
    struct.pack_into(">II", bad_sec3, 128 + 12 * 3 + 4, len(raw), 4)
    bad_sec3 += b"\x00\x00\x00\x00"
    for idx in range(4, 7):
        struct.pack_into(">II", bad_sec3, 128 + 12 * idx + 4, len(bad_sec3), 0)
    bad_sec3_bytes = reseal(bytes(bad_sec3))

    bad_ram = bytearray(raw)
    struct.pack_into(">I", bad_ram, 116, dc.MAX_RAM - len(raw) + 4)
    bad_ram_bytes = reseal(bytes(bad_ram))

    bad_engine = bytearray(raw)
    bad_engine[300] ^= 0x01
    bad_engine_bytes = reseal(bytes(bad_engine))

    for tampered in (bad_xdata_bytes, bad_sec3_bytes, bad_ram_bytes, bad_engine_bytes):
        with pytest.raises(dc.DatError) as exc:
            dc.parse(tampered, kubik_lib)
        assert not isinstance(exc.value, dc.UnsupportedGeneration)

    require_cli()
    _, fx_kubik_copy = _stage_bundle(tmp_path, "neg-262", raw, kubik_lib)
    for path, blob, fx in (
        (tmp_path / "g262_missing_lib.dat", raw, None),
        (tmp_path / "g262_wrong_lib185.dat", raw, fx185_path),
        (tmp_path / "g262_wrong_lib250.dat", raw, fx250_old_path),
        (tmp_path / "g262_bad_xdata.dat", bad_xdata_bytes, fx_kubik_copy),
        (tmp_path / "g262_bad_sec3.dat", bad_sec3_bytes, fx_kubik_copy),
        (tmp_path / "g262_bad_engine.dat", bad_engine_bytes, fx_kubik_copy),
    ):
        path.write_bytes(blob)
        _assert_parse_matches_python_error(path, blob, fx)


def test_g261_is_not_recognised_as_a_known_daily(tmp_path: Path, volumes: Path) -> None:
    raw261, dat261_path = g262.need_g261()
    _, kubik_lib, _, fx_path = g262.need_g262()

    assert 261 not in dc.PROFILES and 261 not in BOUND_ENGINES and dc.peek_generation(raw261) == 261
    with pytest.raises(dc.UnsupportedGeneration):
        dc.parse(raw261, kubik_lib)
    with pytest.raises(intake.IntakeError) as exc_info:
        intake.inspect_dat(raw261, kubik_lib)
    assert exc_info.value.code == "generation_unsupported"
    assert exc_info.value.message == "不支持这个版本的相机配方文件。"

    with _fresh_client(tmp_path, "reject-261", volumes) as upload_client:
        resp = upload_client.post("/api/camdata/import-upload", content=raw261 + kubik_lib, headers={"X-GXR-Dat-Bytes": str(len(raw261))})
        assert resp.status_code == 422
        assert resp.json()["error"] == "generation_unsupported"
        assert resp.json()["message"] == "不支持这个版本的相机配方文件。"

    require_cli()
    _assert_parse_matches_python_error(dat261_path, raw261, fx_path)


# --------------------------------------------------------------------------- 3. CLI / WASM 对拍


def test_rust_cli_and_wasm_parity_for_g262(tmp_path: Path) -> None:
    require_cli()
    raw, library, _, _ = g262.need_g262()
    dat_path, fx_path = _stage_bundle(tmp_path, "parity-262", raw, library)

    expected, entries = _expected_parse_summary(raw, library)
    code, payload = run_json(["camdata-parse", "--dat", str(dat_path), "--library", str(fx_path), "--out-dir", str(tmp_path / "entries-262")])
    assert error_of(code, payload) is None and payload == expected and len(payload["recipes"]) == 66

    arr = run_npy(["camdata-entries", "--dat", str(dat_path), "--library", str(fx_path)], tmp_path / "entries-262.npy")
    expected_arr = np.frombuffer(b"".join(entries[(u, s)] for u in range(3) for s in store.SLOTS), dtype=np.uint8).reshape(24, 552)
    assert np.array_equal(arr, expected_arr)

    dat = dc.parse(raw, library)
    assert dat.kv2_block is not None
    expected_menu = knob_table.read(dat.kv2_block, py_menu.load(), mount_knobs_from_50mm=dc.PROFILES[262].mount_knobs_from_50mm)
    mcode, mpayload = run_json(["camdata-menu", "--dat", str(dat_path), "--library", str(fx_path)])
    assert error_of(mcode, mpayload) is None and mpayload == expected_menu

    wasm_dir = tmp_path / "wasm-262"
    wasm_dir.mkdir()
    proc = run_area("camdata", [dat_path], wasm_dir)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --------------------------------------------------------------------------- 4. Look + 微调：解码、CLI/WASM 对拍、UI 标签与导出


def test_look_and_base_recipe_tags_decode_all_knobs_and_cli_wasm_parity(tmp_path: Path) -> None:
    require_cli()
    raw, library, _, _ = g262.need_g262()
    dat_path, fx_path = _stage_bundle(tmp_path, "tags-262", raw, library)
    home = tmp_path / "home-tags"
    store.import_from_dat(raw, home, library=library)
    cam = CameraData(home)

    cases = g262.TAG_CASES
    wasm_outputs = _run_wasm_cases(tmp_path, [{"tag": t, "dat": str(dat_path), "library": str(fx_path)} for _, t, _, _, _ in cases])

    for (label, tag_text, want_kind, want_name, want_levels), wasm_raw in zip(cases, wasm_outputs, strict=True):
        parsed = parse_recipe_tag(tag_text)
        assert parsed is not None, label
        payload = recipe_tag_payload(parsed, cam)
        assert payload is not None and payload["name"] == want_name and payload["amount_unknown"] is False, label
        expected_changes = {k: v for k, v in want_levels.items() if v != (100 if k == "amount" else 0)}
        assert payload["changes"] == expected_changes, label

        sel = recorded_recipe(parsed, cam)
        assert sel is not None and sel.kind == want_kind and sel.levels_dict() == want_levels, label
        resolved = cam.resolve(sel)
        assert resolved.levels == want_levels, label
        zero_resolved = cam.resolve(replace(sel, levels=()))
        assert (resolved.entry != zero_resolved.entry) if expected_changes else (resolved.entry == zero_resolved.entry), label

        desk = _desktop_expected(tag_text, cam)
        same_redacted(_cli_actual(tag_text, dat_path, fx_path), desk, f"g262 {label} cli vs desktop")
        same_redacted(_normalize_wasm(wasm_raw), desk, f"g262 {label} wasm vs desktop")


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")
def test_desktop_and_mobile_ui_labels_for_look_finetune_and_halfframe(tmp_path: Path) -> None:
    raw, library, _, _ = g262.need_g262()
    home = tmp_path / "home-ui"
    store.import_from_dat(raw, home, library=library)
    cam = CameraData(home)

    web_stage = g262.stage_web_assets(REPO, tmp_path / "web_stage")
    fx_path = tmp_path / "ui-fixture.json"
    fx_path.write_text(json.dumps(g262.build_ui_fixture(cam), ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run(
        ["node", "--experimental-strip-types", str(UI_DRIVER), str(web_stage), str(REPO), str(fx_path)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    res = json.loads(proc.stdout)

    d_kino = res["desktop"]["kino_ft"]
    assert d_kino["shootingText"] == (
        "配方K-KINO（50mm · 68） · 强度 80% · 对比度 -2 · 高光 +3 · 阴影 -1 · 褪色 +2 · 饱和度 +4 · 色温 -3 · 色调 +2 · 分离色调 -1"
    )
    assert "配方K-KINO · 强度 80% · 对比度 -2 · 褪色 +2 · 高光 +3 · 饱和度 +4 · 阴影 -1 · 分离色调 -1 · 色调 +2 · 色温 -3" in d_kino["editText"]
    assert d_kino["curLook"].startswith("当前：K-KINO") and (d_kino["pressedCard"] or "").startswith("record:")
    assert d_kino["sliders"] == {
        "强度": "80%",
        "对比度": "-2",
        "高光": "+3",
        "阴影": "-1",
        "褪色": "+2",
        "饱和度": "+4",
        "色温": "-3",
        "色调": "+2",
        "分离色调": "-1",
    }

    d_amt0 = res["desktop"]["kino_amt0"]
    assert d_amt0["shootingText"] == "配方K-KINO（50mm · 68） · 强度 0% · 对比度 -2"
    assert "配方K-KINO · 强度 0% · 对比度 -2" in d_amt0["editText"]
    assert d_amt0["sliders"]["强度"] == "0%" and d_amt0["sliders"]["对比度"] == "-2"

    d_zero = res["desktop"]["pacific_zero"]
    assert d_zero["shootingText"] == "配方K-PACIFIC（50mm · 65）" and "配方K-PACIFICLUT无" in d_zero["editText"]

    d_acros = res["desktop"]["acros_ft"]
    assert d_acros["shootingText"] == "配方Acros（50mm · 56） · 对比度 -3 · 高光 +8 · 阴影 +2 · 褪色 +4 · 黑白滤镜 -5"
    assert "配方Acros · 对比度 -3 · 褪色 +4 · 黑白滤镜 -5 · 高光 +8 · 阴影 +2" in d_acros["editText"]
    assert d_acros["sliders"] == {"对比度": "-3", "高光": "+8!", "阴影": "+2", "褪色": "+4", "黑白滤镜": "-5"}

    d_gr = res["desktop"]["grpos_ft"]
    assert (
        d_gr["shootingText"]
        == "配方GRPos（50mm · 01） · 强度 75% · 对比度 +2 · 高光 -1 · 阴影 -3 · 褪色 +1 · 饱和度 -4 · 色温 +3 · 色调 -2 · 分离色调 +1"
    )
    assert (
        "配方01 GR 正片 · 强度 75% · 对比度 +2 · 褪色 +1 · 高光 -1 · 饱和度 -4 · 阴影 -3 · 分离色调 +1 · 色调 -2 · 色温 +3" in d_gr["editText"]
    )
    assert d_gr["pressedCard"] == "builtin:1"

    d_hf = res["desktop"]["halfframe"]
    assert (
        "第 1 张K-KINO（50mm · 68） · 强度 80% · 对比度 -2 · 高光 +3 · 阴影 -1 · 褪色 +2 · 饱和度 +4 · 色温 -3 · 色调 +2 · 分离色调 -1"
        in d_hf["shootingText"]
    )
    assert "第 2 张Acros（50mm · 56） · 对比度 -3 · 高光 +8 · 阴影 +2 · 褪色 +4 · 黑白滤镜 -5" in d_hf["shootingText"]
    assert d_hf["curLook"] == "拍摄时：第 1 张 K-KINO · 第 2 张 Acros"

    m_kino = res["mobileAdopted"]["kino_ft"]
    assert m_kino["cardName"] == "K-KINO" and m_kino["adoptedLevels"] == g262.KINO_FINETUNE
    assert m_kino["knobTexts"] == {
        "amount": "80",
        "contrast": "-2",
        "highlight": "+3",
        "shadow": "-1",
        "fade": "+2",
        "saturation": "+4",
        "warmth": "-3",
        "tint": "+2",
        "split": "-1",
    }
    assert m_kino["resolve"]["levels"] == g262.KINO_FINETUNE and m_kino["sideRecipe"]["levels"] == g262.KINO_FINETUNE
    assert res["mobileAdopted"]["acros_ft"]["cardName"] == "Acros"
    assert res["mobileAdopted"]["acros_ft"]["resolve"]["levels"] == g262.ACROS_FINETUNE
    assert res["mobileAdopted"]["grpos_ft"]["cardName"] == "GR 正片"
    assert res["mobileAdopted"]["grpos_ft"]["resolve"]["levels"] == g262.GRPOS_FINETUNE

    m_hf = res["mobileHalfframeTags"]
    assert m_hf["kino_ft"]["withDat"] == (
        "K-KINO（50mm · 68） · 强度 80% · 对比度 -2 · 高光 +3 · 阴影 -1 · 褪色 +2 · 饱和度 +4 · 色温 -3 · 色调 +2 · 分离色调 -1"
    )
    assert m_hf["kino_ft"]["withoutDat"].endswith("· 强度未知")
    assert m_hf["kino_amt0"]["withDat"] == "K-KINO（50mm · 68） · 强度 0% · 对比度 -2"
    assert m_hf["pacific_zero"]["withDat"] == "K-PACIFIC（50mm · 65）"
    assert m_hf["acros_ft"]["withDat"] == "Acros（50mm · 56） · 对比度 -3 · 高光 +8 · 阴影 +2 · 褪色 +4 · 黑白滤镜 -5"


def test_look_finetune_export_exif_and_desktop_halfframe_pair(tmp_path: Path, volumes: Path) -> None:
    raw, library, _, _ = g262.need_g262()
    root = tmp_path / "app-exp"
    cfg = Settings(
        library_dir=root / "library",
        home_dir=root / "app-home",
        export_dir=root / "exports",
        volumes_roots=(volumes,),
        token="test-token-123",
        port=PORT,
        min_free_bytes=0,
    )
    cam = g262.install_g262_calibrated_home(cfg.home_dir, raw, library)

    kino_ft_tag = g262.encode_tag("50", 68, g262.KINO_FINETUNE)
    kino_zero_tag = g262.encode_tag("50", 68, g262.KINO_ZERO)
    acros_ft_tag = g262.encode_tag("50", 56, g262.ACROS_FINETUNE)
    grpos_ft_tag = g262.encode_tag("50", 1, g262.GRPOS_FINETUNE)

    day = cfg.library_dir / "2026" / "2026-11-22"
    day.mkdir(parents=True)
    g262.synth_tagged_pair(day, "R0000201", kino_ft_tag, seed=1)
    g262.synth_tagged_pair(day, "R0000202", kino_zero_tag, seed=1)
    g262.synth_tagged_pair(day, "R0000203", grpos_ft_tag, seed=1)

    lib = Library(cfg.library_dir).open()
    lib.rebuild()
    shots = {s["name"]: s for s in lib.list_shots()}
    render = RenderService(cfg.home_dir, lib)

    exported_rgb: dict[str, np.ndarray] = {}
    for stem, tag_text in (("R0000201", kino_ft_tag), ("R0000202", kino_zero_tag), ("R0000203", grpos_ft_tag)):
        shot = shots[stem]
        edit, side = render_edits.read(lib, shot, render.recipe_menu())
        assert edit.recipe is not None
        res = export_finish(render, lib, shot, edit, side, tmp_path / f"out-{stem}", [], quality=95, long_edge=None, fmt="jpeg", base="auto")
        assert res.render_kind == "prediction"
        with Image.open(res.path) as im:
            ex = im.getexif()
            assert ex.get(33432) == tag_text
            desc = ex.get(270)
            assert cam.resolve(edit.recipe).entry_sha256 in (desc.decode("utf-8") if isinstance(desc, bytes) else str(desc or ""))
            exported_rgb[stem] = np.asarray(im.convert("RGB"))
    assert not np.array_equal(exported_rgb["R0000201"], exported_rgb["R0000202"])
    assert not np.array_equal(exported_rgb["R0000201"], exported_rgb["R0000203"])

    # 半格一对：两半带不同 Look + 微调
    hf_dir = cfg.library_dir / "2026" / "2026-11-23"
    hf_dir.mkdir(parents=True)
    g262.synth_tagged_pair(hf_dir, "R0000301", kino_ft_tag, width=128, height=96, seed=11, when="2026:11:23 10:00:01")
    g262.synth_tagged_pair(hf_dir, "R0000302", acros_ft_tag, width=128, height=96, seed=22, when="2026:11:23 10:00:05")
    lib.rebuild()
    hf_shots = {s["name"]: s for s in lib.list_shots()}
    s1, s2 = hf_shots["R0000301"], hf_shots["R0000302"]
    pair_shot = {
        **s2,
        "pair": {"role": "second", "partner_id": s1["shot_id"], "first_half": "left", "has_raw": True},
        "pair_sources": {"first": {**s1, "all_files": dict(s1["files"])}, "second": {**s2, "all_files": dict(s2["files"])}},
    }
    p1, p2, *_ = pair_plans(render, pair_shot, EditState())
    assert p1.levels == g262.KINO_FINETUNE and p2.levels == g262.ACROS_FINETUNE and p1.entry_sha256 != p2.entry_sha256

    entries = export_pair_finish(
        render, lib, pair_shot, EditState(), {}, tmp_path / "out-hf", [], quality=95, long_edge=None, fmt="jpeg", base="auto", mode="both"
    )
    by_role = {role: res for _sid, role, res in entries}
    assert set(by_role) == {"diptych", "first", "second"}
    with Image.open(by_role["first"].path) as im1, Image.open(by_role["second"].path) as im2, Image.open(by_role["diptych"].path) as imd:
        assert im1.getexif().get(33432) == kino_ft_tag
        assert im2.getexif().get(33432) == acros_ft_tag
        assert imd.getexif().get(33432) == acros_ft_tag
        d1_desc, d2_desc = im1.getexif().get(270), im2.getexif().get(270)
        assert p1.entry_sha256 in (d1_desc.decode("utf-8") if isinstance(d1_desc, bytes) else str(d1_desc or ""))
        assert p2.entry_sha256 in (d2_desc.decode("utf-8") if isinstance(d2_desc, bytes) else str(d2_desc or ""))
    lib.close()


# --------------------------------------------------------------------------- 5. 手机浏览器：读 g262 卡 + Look 微调采用与桌面同图导出


@pytest.mark.browser_serial
def test_mobile_browser_reads_g262_card(tmp_path: Path) -> None:
    pair = mx.real_pairs(1)
    if len(pair) < 1:
        samples.missing("need one DNG+JPG pair")
    _browser_prereq()
    raw, library, dat_path, fx_path = g262.need_g262()
    dng, jpg = pair[0]

    home = tmp_path / "home-262-browser"
    store.import_from_dat(raw, home, library=library)
    cam = CameraData(home)
    qlaunch = next(row for row in cam.listing() if row["id"] == "qlaunch")
    kino = next(r for r in qlaunch["recipes"] if r.get("kind") == "record" and r["name"] == "K-KINO")
    extra_recipe_dict = {"kind": "record", "unit": "qlaunch", "slot": kino["slot"], "id": kino["id"], "levels": g262.KINO_FINETUNE}

    report_path = tmp_path / "g262-report.json"
    mpath = tmp_path / "g262-manifest.json"
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
        pytest.fail("r6 browser driver failed for g262")
    report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    check(report.get("error") is None, "driver finished cleanly")
    check(report.get("pageErrors") == [], "no page errors")
    assert "缺少 GXRFX.BIN" in report["sheet"]["text"]
    for step_key in ("second", "loaded"):
        phone = report[step_key]
        assert phone["entries"] == 24 * 552 and phone["generation"] == 262 and phone["library"] == len(library)
        same_redacted(phone["listing"], cam.listing(), "g262 phone listing equals desktop listing")
        sel = parse_recipe({"kind": "builtin", "unit": "jlaunch", "slot": 1, "levels": {}})
        assert sel is not None
        resolved = cam.resolve(sel)
        same_file_redacted(base64.b64decode(phone["entry"]), resolved.entry, "g262 resolve entry")
        same_redacted(phone["levels"], resolved.levels, "g262 resolve levels")
        same_redacted(phone["sha"], resolved.entry_sha256, "g262 resolve sha")
        extra_sel = parse_recipe(extra_recipe_dict)
        assert extra_sel is not None
        extra_resolved = cam.resolve(extra_sel)
        same_file_redacted(base64.b64decode(phone["extra"]["entry"]), extra_resolved.entry, "g262 K-KINO finetune resolve entry")
        same_redacted(phone["extra"]["levels"], extra_resolved.levels, "g262 K-KINO finetune resolve levels")
        same_redacted(phone["extra"]["sha"], extra_resolved.entry_sha256, "g262 K-KINO finetune resolve sha")


@pytest.mark.browser_serial
def test_mobile_browser_adopts_look_finetune_and_matches_desktop_export(tmp_path: Path, pinned_icc: bytes) -> None:
    _browser_prereq()
    raw, library, dat_path, fx_path = g262.need_g262()
    tag_text = g262.encode_tag("50", 68, g262.KINO_FINETUNE)

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    dng_path, jpg_path = g262.synth_tagged_pair(src_dir, "R0002621", tag_text, width=64, height=48, seed=262)

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
                    "stem": "R0002621",
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
            pytest.fail(f"mobile Look+finetune browser driver failed: {result.stderr[-500:]}")
        report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
        check(report.get("error") is None, f"driver error: {report.get('error')}")
        check(report.get("pageErrors") == [], f"pageErrors: {report.get('pageErrors')}")

        card = report["card"]
        assert card["topRecipe"] == "K-KINO" and card["heading"] == "K-KINO" and card["stripOnName"] == "K-KINO"
        assert card["choice"] is None
        assert card["sideRecipe"]["levels"] == g262.KINO_FINETUNE
        assert {k: v["value"] for k, v in card["knobs"].items()} == {
            "amount": "80",
            "contrast": "-2",
            "highlight": "+3",
            "shadow": "-1",
            "fade": "+2",
            "saturation": "+4",
            "warmth": "-3",
            "tint": "+2",
            "split": "-1",
        }
        assert report["export"]["fileCount"] == 1
        exported = sorted(out_dir.glob("phone-*"))
        assert len(exported) == 1
        phone_bytes = exported[0].read_bytes()
        with Image.open(io.BytesIO(phone_bytes)) as pim, Image.open(io.BytesIO(desk_bytes)) as dim:
            assert pim.getexif().get(33432) == tag_text and pim.getexif().get(270) == dim.getexif().get(270)
        same_file_redacted(phone_bytes, desk_bytes, "phone adopted Look+finetune export matches desktop byte-for-byte")
    finally:
        desk.close()
