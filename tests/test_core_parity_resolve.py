"""Parity: color-parse-edit, color-listing, color-controls, color-resolve and WASM exports
against render.params and render.recipes.

Gates: entry bytes, entry_sha256, JSON structures/values/canonical/digest, and EditError
code/message/detail must match Python exactly.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from gxr_studio.camdata import datcodec as dc
from gxr_studio.camdata import intake, store
from gxr_studio.render import params as py_params
from gxr_studio.render.recipes import CameraData, Menu
from support import wasm_check
from support.core_cli import error_of, require_cli, run_json
from support.fake_card import fake_dat
from support.wasm_check import run_area

_STUDIO = Path(__file__).resolve().parents[1]
_PARENT = Path(os.environ.get("GXR_STUDIO_TEST_PARENT", str(_STUDIO.parent))).resolve()


def _discover_single_file_dats() -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = [
        ("g127", _PARENT / "firmware/package_v36_g127/local/SD_ROOT/GXRCE.DAT"),
        ("g129", _PARENT / "firmware/package_v36_g129/local/SD_ROOT/GXRCE.DAT"),
        ("g131", _PARENT / "firmware/package_v36_g131/local/SD_ROOT/GXRCE.DAT"),
        ("g135", _PARENT / "firmware/package_v37_g135/local/SD_ROOT/GXRCE.DAT"),
        ("g137", _PARENT / "firmware/package_v38_g137/local/SD_ROOT/GXRCE.DAT"),
        ("g138", _PARENT / "firmware/package_v38_g138/SD_ROOT/GXRCE.DAT"),
        ("g150", _PARENT / "firmware/package_v38_g150/local/SD_ROOT/GXRCE.DAT"),
        ("g150_test", _PARENT / "firmware/package_v38_g150_test/local/SD_ROOT/GXRCE.DAT"),
        ("g152", _PARENT / "firmware/package_v38_g152/local/SD_ROOT/GXRCE.DAT"),
        ("g152_test", _PARENT / "firmware/package_v38_g152_test/local/SD_ROOT/GXRCE.DAT"),
        ("g153", _PARENT / "firmware/package_v39_g153/local/SD_ROOT/GXRCE.DAT"),
        ("g153_test", _PARENT / "firmware/package_v39_g153_test/local/SD_ROOT/GXRCE.DAT"),
        ("g154", _PARENT / "firmware/package_v39_g154/local/SD_ROOT/GXRCE.DAT"),
        ("g154_test", _PARENT / "firmware/package_v39_g154_test/local/SD_ROOT/GXRCE.DAT"),
        ("g155", _PARENT / "firmware/package_v310_g155/GXRCE.DAT"),
        ("g155_test", _PARENT / "firmware/package_v310_g155_test/GXRCE.DAT"),
        ("g155c", _PARENT / "firmware/package_v310_g155c/GXRCE.DAT"),
        ("g155c_test", _PARENT / "firmware/package_v310_g155c_test/GXRCE.DAT"),
        ("g156", _PARENT / "firmware/package_v310_g156/GXRCE.DAT"),
        ("g156_test", _PARENT / "firmware/package_v310_g156_test/GXRCE.DAT"),
        ("g158", _PARENT / "firmware/package_v310_g158/local/SD_ROOT/GXRCE.DAT"),
        ("g158_test", _PARENT / "firmware/package_v310_g158_test/local/SD_ROOT/GXRCE.DAT"),
    ]
    return [(label, p) for label, p in candidates if p.is_file()]


def _discover_r5_bundles() -> list[tuple[str, Path, Path]]:
    candidates = [
        ("g176", _PARENT / "research/body_r5_freeze/local/packages/r5-NOT_FOR_INSTALLATION/SD_ROOT"),
        ("g178", _PARENT / "research/build_g178_r1/local/model"),
        ("g181", _PARENT / "firmware/package_v310b_g181/local/g181-daily"),
        ("g182", _PARENT / "firmware/package_v310b_g182/local/g182-daily-r2"),
    ]
    out: list[tuple[str, Path, Path]] = []
    for label, folder in candidates:
        dat_path = folder / "GXRCE.DAT"
        fx_path = folder / "GXRFX.BIN"
        if dat_path.is_file() and fx_path.is_file():
            out.append((label, dat_path, fx_path))
    return out


SINGLE_DATS = _discover_single_file_dats()
R5_BUNDLES = _discover_r5_bundles()
HEX64 = "a" * 64


def _deep_json(levels: int, kind: str) -> str:
    if kind == "array":
        return "[" * levels + "0" + "]" * levels
    return '{"a":' * levels + "0" + "}" * levels


def _parse_edit_py(raw_text: str) -> py_params.EditState:
    side = json.loads(raw_text)
    if not isinstance(side, dict):
        raise TypeError("edit must be a JSON object")
    return py_params.EditState(
        recipe=py_params.parse_recipe(side.get("recipe")),
        adjust=py_params.parse_adjust(side.get("adjust")),
        curve=py_params.parse_curve(side.get("curve")),
        lut=py_params.parse_lut(side.get("lut")),
    )


def _assert_edit_matches(tmp_path: Path, name: str, raw_text: str) -> None:
    p = tmp_path / f"{name}.json"
    p.write_text(raw_text, encoding="utf-8")
    code, payload = run_json(["color-parse-edit", "--json", str(p)])
    try:
        state = _parse_edit_py(raw_text)
    except py_params.EditError as exc:
        err = error_of(code, payload)
        assert err is not None, (name, payload)
        assert err.kind == exc.code, (name, err, exc)
        assert err.message == exc.message, (name, err, exc)
        assert payload["error"].get("detail", "") == exc.detail, (name, payload, exc)
        return

    assert error_of(code, payload) is None, (name, payload)
    expected = state.to_json()
    assert payload["recipe"] == expected["recipe"], (name, payload, expected)
    assert payload["adjust"] == expected["adjust"], (name, payload, expected)
    assert payload["curve"] == expected["curve"], (name, payload, expected)
    if "lut" in expected:
        assert payload.get("lut") == expected["lut"], name
    else:
        assert "lut" not in payload, name
    assert payload["canonical"] == state.canonical(), (name, payload["canonical"], state.canonical())
    assert payload["digest"] == state.digest(), (name, payload["digest"], state.digest())


def test_edit_parsing_valid_cases_and_edge_numbers(tmp_path: Path) -> None:
    require_cli()
    cases = {
        "empty": "{}",
        "nulls": '{"recipe": null, "adjust": null, "curve": null, "lut": null}',
        "adjust_all": json.dumps(
            {
                "adjust": {
                    "exposure_ev": -2.5,
                    "wb": {"warm": -15, "tint": 22},
                    "contrast": 100,
                    "highlight": -100,
                    "shadow": 50,
                    "black_point": -10,
                    "saturation": 25,
                    "vibrance": -30,
                    "vignette": 40,
                }
            }
        ),
        "adjust_zero_omission": '{"adjust": {"black_point": 0, "saturation": 0.0, "vibrance": -0, "vignette": 0}}',
        "adjust_falsy_wb_and_float_ints": '{"adjust": {"exposure_ev": 5, "wb": [], "contrast": 1.0, "shadow": -2.0}}',
        "adjust_falsy_wb_false": '{"adjust": {"wb": false, "warm": 0}}',
        "adjust_falsy_wb_zero_float": '{"adjust": {"wb": -0.0}}',
        "adjust_round4_edges": '{"adjust": {"exposure_ev": 0.00005}}',
        "adjust_round4_half_to_even": '{"adjust": {"exposure_ev": 0.00025}}',
        "adjust_round4_012345": '{"adjust": {"exposure_ev": 0.12345}}',
        "adjust_neg_zero": '{"adjust": {"exposure_ev": -0.00001}}',
        "curve_identity_removed": '{"curve": {"master": [[0, 0], [255, 255]], "r": [[0, 0.00001], [255, 255]]}}',
        "curve_channels_round4": json.dumps(
            {
                "curve": {
                    "master": [[0, 0], [64.00005, 80.12345], [255, 255]],
                    "r": [[0, 10], [255, 240]],
                    "g": None,
                    "b": [[0, 0], [128, 140], [255, 255]],
                }
            }
        ),
        "recipe_builtin": (
            '{"recipe": {"unit": "jlaunch", "slot": 1, "levels": {"shadow": -1, "amount": 100}, "id": "ignored", "extra_ignored": 42}}'
        ),
        "recipe_falsy_levels": '{"recipe": {"unit": "qlaunch", "slot": 8, "levels": []}}',
        "recipe_extra_and_memory": json.dumps(
            {
                "recipe": {
                    "kind": "camera_user",
                    "id": HEX64,
                    "unit": "l08firm",
                    "slot": 4,
                    "levels": {"contrast": 2},
                    "memory": {"k": 1},
                },
                "lut": HEX64,
            }
        ),
        "recipe_local_accepted_by_parse": json.dumps(
            {"recipe": {"kind": "local", "id": HEX64, "unit": "jlaunch", "slot": 2, "levels": {"contrast": 10**20}}}
        ),
        "duplicate_keys": (
            '{"adjust": {"contrast": 1, "contrast": 7}, '
            '"recipe": {"unit": "jlaunch", "slot": 2, "slot": 3}, '
            '"adjust": {"saturation": 9, "exposure_ev": 0.5}}'
        ),
        "unknown_top_level": '{"adjust": {"contrast": 3}, "geometry": {"straighten": 1.2}, "frame": {"style": "clean"}}',
        "sol_surrogate_recipe_memory": r'{"recipe":{"unit":"jlaunch","slot":1,"memory":{"note":"\ud800"}}}',
        "sol_surrogate_ignored_high": r'{"ignored":"\ud800"}',
        "sol_surrogate_ignored_low": r'{"ignored":"\udfff"}',
        "sol_surrogate_ignored_high_a": r'{"ignored":"\ud800A"}',
        "sol_surrogate_ignored_high_u0041": r'{"ignored":"\ud800\u0041"}',
        "sol_surrogate_key": r'{"\ud800":1}',
        "surrogate_distinct_keys": r'{"\ud800":1,"\ud801":2,"adjust":{"contrast":4}}',
        "surrogate_valid_pair": r'{"recipe":{"unit":"jlaunch","slot":1,"memory":{"note":"\ud83d\ude00"}}}',
    }
    for name, raw_text in cases.items():
        _assert_edit_matches(tmp_path, name, raw_text)


def test_edit_parsing_all_error_branches(tmp_path: Path) -> None:
    require_cli()
    bad_cases = {
        "adj_not_dict": '{"adjust": []}',
        "adj_unknown": '{"adjust": {"unknown": 1}}',
        "adj_ev_bool": '{"adjust": {"exposure_ev": true}}',
        "adj_ev_nan": '{"adjust": {"exposure_ev": NaN}}',
        "adj_ev_inf": '{"adjust": {"exposure_ev": Infinity}}',
        "adj_ev_ninf": '{"adjust": {"exposure_ev": -Infinity}}',
        "adj_ev_range": '{"adjust": {"exposure_ev": 5.0001}}',
        "adj_ev_huge": f'{{"adjust": {{"exposure_ev": {10**20}}}}}',
        "adj_wb_truthy_list": '{"adjust": {"wb": [1]}}',
        "adj_wb_truthy_bool": '{"adjust": {"wb": true}}',
        "adj_wb_unknown": '{"adjust": {"wb": {"foo": 1}}}',
        "adj_int_bool": '{"adjust": {"contrast": true}}',
        "adj_int_frac": '{"adjust": {"highlight": 1.5}}',
        "adj_int_nan": '{"adjust": {"shadow": NaN}}',
        "adj_int_range": '{"adjust": {"warm": 101}}',
        "adj_int_huge": f'{{"adjust": {{"saturation": {10**20}}}}}',
        "adj_int_neg_huge": f'{{"adjust": {{"vignette": -{10**20}}}}}',
        "curve_not_dict": '{"curve": []}',
        "curve_unknown_ch": '{"curve": {"alpha": [[0, 0], [255, 255]]}}',
        "curve_too_few": '{"curve": {"master": [[0, 0]]}}',
        "curve_too_many": json.dumps({"curve": {"master": [[i * 10, i * 10] for i in range(17)]}}),
        "curve_pt_bool": '{"curve": {"r": [[true, 0], [255, 255]]}}',
        "curve_pt_nan": '{"curve": {"g": [[0, NaN], [255, 255]]}}',
        "curve_pt_range": '{"curve": {"b": [[0, 0], [256, 255]]}}',
        "curve_pt_huge": f'{{"curve": {{"master": [[0, 0], [{10**20}, 255]]}}}}',
        "curve_x_not_inc": '{"curve": {"master": [[10, 0], [10, 100]]}}',
        "curve_x_equal_after_round": '{"curve": {"master": [[1.00001, 0], [1.00002, 100]]}}',
        "recipe_not_dict": '{"recipe": 123}',
        "recipe_kind_bad": '{"recipe": {"kind": "other", "unit": "jlaunch", "slot": 1}}',
        "recipe_kind_null": '{"recipe": {"kind": null, "unit": "jlaunch", "slot": 1}}',
        "recipe_id_bad": '{"recipe": {"kind": "look", "id": "short", "unit": "jlaunch", "slot": 1}}',
        "recipe_id_upper": json.dumps({"recipe": {"kind": "record", "id": "A" * 64, "unit": "jlaunch", "slot": 1}}),
        "recipe_unit_bad": '{"recipe": {"unit": "other", "slot": 1}}',
        "recipe_slot_bool": '{"recipe": {"unit": "jlaunch", "slot": true}}',
        "recipe_slot_float": '{"recipe": {"unit": "jlaunch", "slot": 1.0}}',
        "recipe_slot_zero": '{"recipe": {"unit": "jlaunch", "slot": 0}}',
        "recipe_slot_nine": '{"recipe": {"unit": "jlaunch", "slot": 9}}',
        "recipe_slot_huge": f'{{"recipe": {{"unit": "jlaunch", "slot": {10**20}}}}}',
        "recipe_levels_truthy": '{"recipe": {"unit": "jlaunch", "slot": 1, "levels": [1]}}',
        "recipe_knob_bad": '{"recipe": {"unit": "jlaunch", "slot": 1, "levels": {"unknown": 0}}}',
        "recipe_level_bool": '{"recipe": {"unit": "jlaunch", "slot": 1, "levels": {"contrast": true}}}',
        "recipe_level_float": '{"recipe": {"unit": "jlaunch", "slot": 1, "levels": {"contrast": 1.0}}}',
        "recipe_memory_bad": '{"recipe": {"unit": "jlaunch", "slot": 1, "memory": [1]}}',
        "lut_bad": '{"lut": "not-hex"}',
    }
    for name, raw_text in bad_cases.items():
        _assert_edit_matches(tmp_path, name, raw_text)


def test_edit_json_depth_and_size_caps(tmp_path: Path) -> None:
    require_cli()
    for kind in ("array", "object"):
        p_ok = tmp_path / f"ok_{kind}_128.json"
        p_ok.write_text(_deep_json(128, kind), encoding="utf-8")
        code, payload = run_json(["color-parse-edit", "--json", str(p_ok)])
        # Root is either 0 (not object) or {"a": ...}, never json depth error
        err = error_of(code, payload)
        if err is not None:
            assert err.message != "JSON 嵌套太深"

        p_bad = tmp_path / f"bad_{kind}_129.json"
        p_bad.write_text(_deep_json(129, kind), encoding="utf-8")
        code, payload = run_json(["color-parse-edit", "--json", str(p_bad)])
        err = error_of(code, payload)
        assert err is not None and (err.kind, err.message) == ("json", "JSON 嵌套太深")

    huge = tmp_path / "huge.json"
    huge.write_bytes(b" " * (4 * 1024 * 1024 + 1))
    code, payload = run_json(["color-parse-edit", "--json", str(huge)])
    err = error_of(code, payload)
    assert err is not None and (err.kind, err.message) == ("json", "JSON 文件过大")


def test_embedded_menu_listing_and_controls_match_python() -> None:
    require_cli()
    py_menu = Menu()
    code, payload = run_json(["color-listing"])
    assert error_of(code, payload) is None
    assert payload["units"] == py_menu.listing()

    for unit in range(3):
        for slot in range(1, 9):
            code, payload = run_json(["color-controls", "--unit", str(unit), "--slot", str(slot)])
            assert error_of(code, payload) is None
            assert payload["controls"] == py_menu.controls(unit, slot)

    for bad_u, bad_s in ((3, 1), (-1, 1), (0, 0), (0, 9)):
        code, payload = run_json(["color-controls", "--unit", str(bad_u), "--slot", str(bad_s)])
        err = error_of(code, payload)
        assert err is not None and (err.kind, err.message) == ("value", "unit/slot")


def _build_camera_data(tmp_path: Path, label: str, dat_path: Path, fx_path: Path | None) -> CameraData:
    home = tmp_path / f"home_{label}"
    lib = fx_path.read_bytes() if fx_path is not None else None
    store.import_from_dat(dat_path.read_bytes(), home, library=lib)
    return CameraData(home)


def _cli_args_dat(dat_path: Path, fx_path: Path | None) -> list[str]:
    args = ["--dat", str(dat_path)]
    if fx_path is not None:
        args.extend(["--library", str(fx_path)])
    return args


def _assert_resolve_parity(
    tmp_path: Path,
    cam: CameraData,
    dat_path: Path,
    fx_path: Path | None,
    recipe_obj: dict[str, Any],
    case_name: str,
) -> None:
    rpath = tmp_path / f"recipe_{case_name}.json"
    rpath.write_text(json.dumps(recipe_obj), encoding="utf-8")
    code, payload = run_json(["color-resolve", *_cli_args_dat(dat_path, fx_path), "--recipe", str(rpath)])
    try:
        sel = py_params.parse_recipe(recipe_obj)
        assert sel is not None
        res = cam.resolve(sel)
    except py_params.EditError as exc:
        err = error_of(code, payload)
        assert err is not None, (case_name, payload)
        assert err.kind == exc.code, (case_name, err, exc)
        assert err.message == exc.message, (case_name, err, exc)
        assert payload["error"].get("detail", "") == exc.detail, (case_name, payload, exc)
        return

    assert error_of(code, payload) is None, (case_name, payload)
    assert bytes.fromhex(payload["entry"]) == res.entry, case_name
    assert payload["entry_sha256"] == res.entry_sha256, case_name
    assert payload["levels"] == res.levels, case_name
    assert payload["stats"] == res.stats, case_name


def test_real_dats_listing_controls_and_resolve_parity(tmp_path: Path) -> None:
    require_cli()
    bundles: list[tuple[str, Path, Path | None]] = [(lbl, dp, None) for lbl, dp in SINGLE_DATS] + [(lbl, dp, fp) for lbl, dp, fp in R5_BUNDLES]
    if not bundles:
        pytest.skip("no real DAT fixtures on this machine")

    for label, dat_path, fx_path in bundles:
        cam = _build_camera_data(tmp_path, label, dat_path, fx_path)
        dat_args = _cli_args_dat(dat_path, fx_path)

        code, payload = run_json(["color-listing", *dat_args])
        assert error_of(code, payload) is None, label
        py_lst = cam.listing()
        assert payload["units"] == py_lst, label

        for unit in range(3):
            for slot in range(1, 9):
                code, ctrl_payload = run_json(["color-controls", *dat_args, "--unit", str(unit), "--slot", str(slot)])
                assert error_of(code, ctrl_payload) is None, (label, unit, slot)
                assert ctrl_payload["controls"] == cam.controls(unit, slot), (label, unit, slot)

    # Exhaustive resolve parity on representative generations (single-file + all r5)
    by_label = {lbl: (dp, fp) for lbl, dp, fp in bundles}
    pick_labels = [
        lbl for lbl in ("g127", "g138", "g150", "g152_test", "g155_test", "g158_test", "g176", "g178", "g181", "g182") if lbl in by_label
    ]
    if not pick_labels:
        pick_labels = [bundles[0][0]]

    for label in pick_labels:
        dat_path, fx_path = by_label[label]
        cam = _build_camera_data(tmp_path, f"res_{label}", dat_path, fx_path)
        dat_args = _cli_args_dat(dat_path, fx_path)
        py_lst = cam.listing()
        lib_gen = dc.PROFILES[cam.manifest["generation"]].library_generation

        for unit_idx, unit_name in enumerate(py_params.UNITS):
            # Default selection
            code, def_payload = run_json(["color-resolve", *dat_args, "--default", "--unit", unit_name])
            assert error_of(code, def_payload) is None
            def_res = cam.resolve(cam.default_selection(unit_name))
            assert bytes.fromhex(def_payload["entry"]) == def_res.entry
            assert def_payload["entry_sha256"] == def_res.entry_sha256

            # Builtin slots
            for slot in range(1, 9):
                _assert_resolve_parity(tmp_path, cam, dat_path, fx_path, {"unit": unit_name, "slot": slot}, f"{label}_u{unit_idx}_s{slot}")

            if lib_gen != 178:
                # B&W slot (slot 3, entry[0] == 3): valid levels + forbidden saturation
                _assert_resolve_parity(
                    tmp_path,
                    cam,
                    dat_path,
                    fx_path,
                    {"unit": unit_name, "slot": 3, "levels": {"contrast": 2, "filter": 1}},
                    f"{label}_u{unit_idx}_bw_ok",
                )
                _assert_resolve_parity(
                    tmp_path,
                    cam,
                    dat_path,
                    fx_path,
                    {"unit": unit_name, "slot": 3, "levels": {"saturation": 1}},
                    f"{label}_u{unit_idx}_bw_bad_sat",
                )
                # Stock slot 4: neutral amount=100 OK, amount=50 rejected
                _assert_resolve_parity(
                    tmp_path,
                    cam,
                    dat_path,
                    fx_path,
                    {"unit": unit_name, "slot": 4, "levels": {"amount": 100}},
                    f"{label}_u{unit_idx}_s4_neutral",
                )
                _assert_resolve_parity(
                    tmp_path,
                    cam,
                    dat_path,
                    fx_path,
                    {"unit": unit_name, "slot": 4, "levels": {"amount": 50}},
                    f"{label}_u{unit_idx}_s4_amt50",
                )

        # Extras: looks, users, records (unit 0)
        u0 = py_lst[0]
        if u0["looks"]:
            lk = u0["looks"][0]
            _assert_resolve_parity(
                tmp_path,
                cam,
                dat_path,
                fx_path,
                {"kind": "look", "id": lk["id"], "unit": "jlaunch", "slot": lk["slot"]},
                f"{label}_look0",
            )
            _assert_resolve_parity(
                tmp_path,
                cam,
                dat_path,
                fx_path,
                {"kind": "look", "id": lk["id"], "unit": "jlaunch", "slot": lk["slot"], "levels": {"contrast": 999}},
                f"{label}_look0_bad_lv",
            )
        if u0["users"]:
            usr = u0["users"][0]
            _assert_resolve_parity(
                tmp_path,
                cam,
                dat_path,
                fx_path,
                {"kind": "camera_user", "id": usr["id"], "unit": "jlaunch", "slot": usr["slot"]},
                f"{label}_user0",
            )
            _assert_resolve_parity(
                tmp_path,
                cam,
                dat_path,
                fx_path,
                {"kind": "camera_user", "id": usr["id"], "unit": "jlaunch", "slot": 8 if usr["slot"] != 8 else 7},
                f"{label}_user0_wrong_slot",
            )
        records = [r for r in u0["recipes"] if r.get("kind") == "record"]
        if records:
            rec = records[0]
            _assert_resolve_parity(
                tmp_path,
                cam,
                dat_path,
                fx_path,
                {"kind": "record", "id": rec["id"], "unit": "jlaunch", "slot": rec["slot"]},
                f"{label}_record0",
            )
        # local recipe -> recipe_not_found
        _assert_resolve_parity(
            tmp_path,
            cam,
            dat_path,
            fx_path,
            {"kind": "local", "id": HEX64, "unit": "jlaunch", "slot": 1},
            f"{label}_local_missing",
        )


def test_wasm_matches_native_for_resolve_controls_and_listing(tmp_path: Path) -> None:
    require_cli()
    specs: dict[str, dict[str, Any]] = {
        "listing-embedded": {"kind": "listing"},
        "controls-embedded": {"kind": "controls", "unit": 0, "slot": 3},
        "controls-bad-unit": {"kind": "controls", "unit": 3, "slot": 1},
        "controls-bad-slot": {"kind": "controls", "unit": 0, "slot": 9},
        "controls-wrap-unit-pos": {"kind": "controls", "unit": 4294967296, "slot": 1},
        "controls-wrap-unit-pos1": {"kind": "controls", "unit": 4294967297, "slot": 1},
        "controls-wrap-unit-neg": {"kind": "controls", "unit": -4294967296, "slot": 1},
        "controls-wrap-unit-neg1": {"kind": "controls", "unit": -4294967295, "slot": 1},
        "controls-wrap-slot-pos": {"kind": "controls", "unit": 0, "slot": 4294967296},
        "controls-wrap-slot-pos1": {"kind": "controls", "unit": 0, "slot": 4294967297},
        "controls-wrap-slot-neg": {"kind": "controls", "unit": 0, "slot": -4294967296},
        "controls-wrap-slot-neg1": {"kind": "controls", "unit": 0, "slot": -4294967295},
    }

    bad_dat = tmp_path / "fake.dat"
    bad_dat.write_bytes(fake_dat())
    oversized_dat = tmp_path / "oversized.dat"
    oversized_dat.write_bytes(b"GXRCE001" + b"\0" * intake.MAX_DAT_BYTES)
    empty_dat = tmp_path / "empty.dat"
    empty_dat.write_bytes(b"")

    valid_recipe = tmp_path / "r_valid.json"
    valid_recipe.write_text('{"unit": "jlaunch", "slot": 1}', encoding="utf-8")
    specs["listing-bad-dat"] = {"kind": "listing", "dat": str(bad_dat)}
    specs["listing-empty-dat"] = {"kind": "listing", "dat": str(empty_dat)}
    specs["listing-oversized-dat"] = {"kind": "listing", "dat": str(oversized_dat)}
    specs["resolve-oversized-dat"] = {"kind": "resolve", "dat": str(oversized_dat), "recipe": str(valid_recipe)}

    for sname, sbody in (
        ("surrogate-memory-ud800", r'{"unit":"jlaunch","slot":1,"memory":{"note":"\ud800"}}'),
        ("surrogate-ignored-ud800", r'{"ignored":"\ud800"}'),
        ("surrogate-ignored-udfff", r'{"ignored":"\udfff"}'),
        ("surrogate-ignored-ud800a", r'{"ignored":"\ud800A"}'),
        ("surrogate-ignored-ud800u0041", r'{"ignored":"\ud800\u0041"}'),
        ("surrogate-key-ud800", r'{"\ud800":1}'),
    ):
        srp = tmp_path / f"wr_{sname}.json"
        srp.write_text(sbody, encoding="utf-8")
        specs[f"resolve-empty-{sname}"] = {"kind": "resolve", "dat": str(empty_dat), "recipe": str(srp)}

    # Add real single-file and r5 DAT cases if available on this machine
    if SINGLE_DATS:
        by_s = dict(SINGLE_DATS)
        s_label = "g158_test" if "g158_test" in by_s else SINGLE_DATS[-1][0]
        s_dat = by_s[s_label]
        cam = _build_camera_data(tmp_path, f"wasm_{s_label}", s_dat, None)
        lst = cam.listing()
        specs["listing-single"] = {"kind": "listing", "dat": str(s_dat)}
        specs["controls-single"] = {"kind": "controls", "dat": str(s_dat), "unit": 2, "slot": 5}

        def add_r(name: str, body: str) -> None:
            rp = tmp_path / f"wr_{name}.json"
            rp.write_text(body, encoding="utf-8")
            specs[f"resolve-{name}"] = {"kind": "resolve", "dat": str(s_dat), "recipe": str(rp)}

        add_r("bw", '{"unit": "jlaunch", "slot": 3, "levels": {"contrast": 2, "filter": 1}}')
        add_r("bw-bad-sat", '{"unit": "jlaunch", "slot": 3, "levels": {"saturation": 1}}')
        add_r("s4-neutral", '{"unit": "qlaunch", "slot": 4, "levels": {"amount": 100}}')
        add_r("s4-bad-amt", '{"unit": "qlaunch", "slot": 4, "levels": {"amount": 50}}')
        add_r("bad-level", '{"unit": "jlaunch", "slot": 1, "levels": {"contrast": 999}}')
        add_r("huge-level", f'{{"unit": "jlaunch", "slot": 1, "levels": {{"contrast": {10**20}}}}}')
        add_r("bad-slot-float", '{"unit": "jlaunch", "slot": 1.0}')
        add_r("bad-kind", '{"kind": "nope", "unit": "jlaunch", "slot": 1}')
        add_r("local-404", json.dumps({"kind": "local", "id": HEX64, "unit": "jlaunch", "slot": 1}))
        add_r("dup-keys", '{"unit": "qlaunch", "unit": "jlaunch", "slot": 1, "levels": {"contrast": 1, "contrast": 2}}')
        add_r("surrogate-memory", r'{"unit":"jlaunch","slot":1,"memory":{"note":"\ud800"}}')
        if lst[0]["looks"]:
            lk = lst[0]["looks"][0]
            add_r("look", json.dumps({"kind": "look", "id": lk["id"], "unit": "jlaunch", "slot": lk["slot"]}))
        if lst[0]["users"]:
            usr = lst[0]["users"][0]
            add_r("user", json.dumps({"kind": "camera_user", "id": usr["id"], "unit": "jlaunch", "slot": usr["slot"]}))

        # JSON depth 128 vs 129 and oversized recipe JSON (> 4 MiB)
        for levels in (128, 129):
            for kind in ("array", "object"):
                dp = tmp_path / f"wdeep_{kind}_{levels}.json"
                dp.write_text(_deep_json(levels, kind), encoding="utf-8")
                specs[f"resolve-deep-{kind}-{levels}"] = {"kind": "resolve", "dat": str(s_dat), "recipe": str(dp)}
        huge_json = tmp_path / "whuge_recipe.json"
        huge_json.write_bytes(b" " * (4 * 1024 * 1024 + 1))
        specs["resolve-huge-json"] = {"kind": "resolve", "dat": str(s_dat), "recipe": str(huge_json)}

    if R5_BUNDLES:
        by_r5 = {lbl: (dp, fp) for lbl, dp, fp in R5_BUNDLES}
        r5_lbl = "g181" if "g181" in by_r5 else R5_BUNDLES[0][0]
        r5_dat, r5_fx = by_r5[r5_lbl]
        cam_r5 = _build_camera_data(tmp_path, f"wasm_{r5_lbl}", r5_dat, r5_fx)
        specs["listing-r5"] = {"kind": "listing", "dat": str(r5_dat), "library": str(r5_fx)}
        specs["controls-r5"] = {"kind": "controls", "dat": str(r5_dat), "library": str(r5_fx), "unit": 0, "slot": 1}
        recs = [r for r in cam_r5.listing()[0]["recipes"] if r.get("kind") == "record"]
        if recs:
            rp = tmp_path / "wr_r5_rec.json"
            rp.write_text(
                json.dumps({"kind": "record", "id": recs[0]["id"], "unit": "jlaunch", "slot": recs[0]["slot"]}),
                encoding="utf-8",
            )
            specs["resolve-r5-rec"] = {"kind": "resolve", "dat": str(r5_dat), "library": str(r5_fx), "recipe": str(rp)}
        if "g178" in by_r5:
            g178_dat, g178_fx = by_r5["g178"]
            specs["listing-g178"] = {"kind": "listing", "dat": str(g178_dat), "library": str(g178_fx)}
            rp5 = tmp_path / "wr_g178_s5.json"
            rp5.write_text('{"unit": "jlaunch", "slot": 5}', encoding="utf-8")
            specs["resolve-g178-s5"] = {"kind": "resolve", "dat": str(g178_dat), "library": str(g178_fx), "recipe": str(rp5)}

    inputs: list[Path] = []
    for name, spec in specs.items():
        path = tmp_path / f"case-{name}.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        inputs.append(path)
    run_area("color", inputs, tmp_path)


def test_wasm_full_error_json_and_nonfinite_controls_parity(tmp_path: Path) -> None:
    """wasm_vs_native.mjs only checks error.kind/message; verify full JSON including detail
    and non-finite/fractional JS Number bounds in color_controls."""
    require_cli()
    if shutil.which("node") is None:
        wasm_check._missing("node is not on PATH")
    if not wasm_check.PKG.is_file():
        wasm_check._missing(f"need {wasm_check.PKG.name}; run make core-wasm")

    cases: list[dict[str, Any]] = []
    for label, unit, slot in (
        ("unit-4294967296", 4294967296, 1),
        ("unit-4294967297", 4294967297, 1),
        ("unit-neg-4294967296", -4294967296, 1),
        ("unit-neg-4294967295", -4294967295, 1),
        ("slot-4294967296", 0, 4294967296),
        ("slot-4294967297", 0, 4294967297),
        ("slot-neg-4294967296", 0, -4294967296),
        ("slot-neg-4294967295", 0, -4294967295),
        ("unit-3", 3, 1),
        ("slot-9", 0, 9),
    ):
        cases.append({"kind": "controls", "label": label, "unit": unit, "slot": slot})

    if SINGLE_DATS:
        by_s = dict(SINGLE_DATS)
        s_dat = by_s.get("g158_test", SINGLE_DATS[-1][1])
        for rname, body in (
            ("bad-level", '{"unit": "jlaunch", "slot": 1, "levels": {"contrast": 999}}'),
            ("huge-level", f'{{"unit": "jlaunch", "slot": 1, "levels": {{"contrast": {10**20}}}}}'),
            ("bad-slot-float", '{"unit": "jlaunch", "slot": 1.0}'),
            ("bad-kind", '{"kind": "nope", "unit": "jlaunch", "slot": 1}'),
            ("local-404", json.dumps({"kind": "local", "id": HEX64, "unit": "jlaunch", "slot": 1})),
            ("bad-knob-surrogate", r'{"unit": "jlaunch", "slot": 1, "levels": {"\ud800": 1}}'),
            ("deep-129", _deep_json(129, "object")),
        ):
            rp = tmp_path / f"err_{rname}.json"
            rp.write_text(body, encoding="utf-8")
            cases.append({"kind": "resolve", "label": rname, "dat": str(s_dat), "recipe": str(rp)})

    cases_path = tmp_path / "full_error_cases.json"
    cases_path.write_text(json.dumps(cases), encoding="utf-8")

    js = r"""
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const [pkgPath, wasmPath, casesPath] = process.argv.slice(1);
const wasm = await import(pathToFileURL(pkgPath).href);
wasm.initSync({ module: readFileSync(wasmPath) });

const out = [];
for (const c of JSON.parse(readFileSync(casesPath, 'utf8'))) {
  try {
    if (c.kind === 'controls') {
      wasm.color_controls(undefined, c.unit, c.slot, undefined);
    } else if (c.kind === 'resolve') {
      wasm.color_resolve(new Uint8Array(readFileSync(c.dat)), new Uint8Array(readFileSync(c.recipe)), undefined);
    }
    throw new Error(`expected failure for ${c.label}`);
  } catch (err) {
    out.push({ label: c.label, errorText: err instanceof Error ? err.message : String(err) });
  }
}
for (const [label, u, s] of [
  ['nan-unit', NaN, 1],
  ['inf-unit', Infinity, 1],
  ['ninf-unit', -Infinity, 1],
  ['frac-unit', 1.5, 1],
  ['nan-slot', 0, NaN],
  ['inf-slot', 0, Infinity],
  ['ninf-slot', 0, -Infinity],
  ['frac-slot', 0, 1.5],
]) {
  let caught = null;
  try {
    wasm.color_controls(undefined, u, s, undefined);
  } catch (err) {
    caught = err instanceof Error ? err.message : String(err);
  }
  if (caught !== '{"error":{"kind":"value","message":"unit/slot"}}') {
    throw new Error(`unexpected ${label} outcome: ${caught}`);
  }
}
process.stdout.write(JSON.stringify(out));
"""
    proc = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            js,
            str(wasm_check.PKG),
            str(wasm_check.PKG.with_name("gxr_wasm_bg.wasm")),
            str(cases_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    wasm_results = {row["label"]: row["errorText"] for row in json.loads(proc.stdout)}

    for c in cases:
        if c["kind"] == "controls":
            args = ["color-controls", "--unit", str(c["unit"]), "--slot", str(c["slot"])]
        else:
            args = ["color-resolve", "--dat", c["dat"], "--recipe", c["recipe"]]
        cli_proc = subprocess.run(
            [str(wasm_check.CLI), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert cli_proc.returncode == 2, f"{c['label']}: expected exit 2, got {cli_proc.returncode}: {cli_proc.stderr}"
        wasm_text = wasm_results[c["label"]]
        cli_text = cli_proc.stdout.rstrip("\n")
        assert wasm_text == cli_text, f"{c['label']}: {wasm_text!r} != {cli_text!r}"
        assert json.loads(wasm_text) == json.loads(cli_text)
