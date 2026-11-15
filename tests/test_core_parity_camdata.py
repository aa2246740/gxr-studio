"""Parity: gxr-cli and gxr-wasm against Python camdata (datcodec, r5, entries_from_dat, knob_table, menu, recipe_tags)."""

from __future__ import annotations

import hashlib
import os
import re
import struct
import zlib
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from gxr_studio.camdata import datcodec as dc
from gxr_studio.camdata import intake, knob_table, store
from gxr_studio.camdata.recipe_tags import parse_recipe_tag
from gxr_studio.engine import menu as py_menu
from support import samples
from support.core_cli import error_of, require_cli, run_json, run_npy
from support.fake_card import fake_dat, fake_gxrfx
from support.wasm_check import PKG, run_area

_PARENT = samples.sample_parent()


def _reseal(blob: bytes) -> bytes:
    b = bytearray(blob)
    struct.pack_into(">I", b, 24, zlib.crc32(b[256:]) & 0xFFFFFFFF)
    struct.pack_into(">I", b, 20, 0)
    struct.pack_into(">I", b, 20, zlib.crc32(b[:256]) & 0xFFFFFFFF)
    return bytes(b)


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


def _expected_parse_summary(raw: bytes, library: bytes | None = None) -> tuple[dict[str, Any], dict[tuple[int, int], bytes]]:
    dat, entries = store.entries_from_dat(raw, library)
    units_json = []
    for unit_idx, unit_name in enumerate(dc.NAMES):
        slot_items = []
        for slot in store.SLOTS:
            e = entries[(unit_idx, slot)]
            slot_items.append({"slot": slot, "sha256": hashlib.sha256(e).hexdigest()})
        units_json.append(
            {
                "unit": unit_name,
                "unit_index": unit_idx,
                "slots": list(store.SLOTS),
                "entries": slot_items,
            }
        )
    summary = {
        "generation": dat.generation,
        "units": units_json,
        "looks": [
            {"name": lk.name, "base": lk.base, "unit_mask": lk.unit_mask, "sha256": hashlib.sha256(lk.entry).hexdigest()} for lk in dat.looks
        ],
        "recipes": [
            {"name": rec.name, "base": rec.base, "unit_mask": rec.unit_mask, "sha256": hashlib.sha256(rec.entry).hexdigest()}
            for rec in dat.recipes
        ],
        "users": [{"name": dat.user_names[i], "base": u["base"], "levels": list(u["levels"])} for i, u in enumerate(dat.users)],
        "has_kv2": dat.kv2_block is not None,
        "kv2_sha256": hashlib.sha256(dat.kv2_block).hexdigest() if dat.kv2_block is not None else None,
    }
    return summary, entries


def _expected_error_kind(exc: dc.DatError) -> str:
    return "unsupported_generation" if isinstance(exc, dc.UnsupportedGeneration) else "dat_error"


def _assert_parse_matches_python_error(path: Path, raw: bytes, library_path: Path | None = None) -> None:
    lib_bytes = library_path.read_bytes() if library_path is not None else None
    with pytest.raises(dc.DatError) as py_exc:
        dc.parse(raw, lib_bytes)
    args = ["camdata-parse", "--dat", str(path)]
    if library_path is not None:
        args += ["--library", str(library_path)]
    code, payload = run_json(args)
    err = error_of(code, payload)
    assert err is not None, f"Rust unexpectedly succeeded on {path.name}"
    assert err.kind == _expected_error_kind(py_exc.value)
    assert err.message == str(py_exc.value)


# --------------------------------------------------------------------------- Real DATs (when present)


@pytest.mark.parametrize(("label", "dat_path"), SINGLE_DATS, ids=[x[0] for x in SINGLE_DATS])
def test_single_file_dats_match_python(label: str, dat_path: Path, tmp_path: Path) -> None:
    require_cli()
    raw = dat_path.read_bytes()
    expected, entries = _expected_parse_summary(raw)
    out_dir = tmp_path / "entries"
    code, payload = run_json(["camdata-parse", "--dat", str(dat_path), "--out-dir", str(out_dir)])
    assert error_of(code, payload) is None
    assert payload == expected, label

    for unit_idx, unit_name in enumerate(dc.NAMES):
        for slot in store.SLOTS:
            got_bytes = (out_dir / "units" / unit_name / "recipes" / f"{slot:02d}.entry").read_bytes()
            assert got_bytes == entries[(unit_idx, slot)], (label, unit_name, slot)

    arr = run_npy(["camdata-entries", "--dat", str(dat_path)], tmp_path / "entries.npy")
    assert arr.dtype == np.uint8 and arr.shape == (24, 552)
    expected_arr = np.frombuffer(b"".join(entries[(u, s)] for u in range(3) for s in store.SLOTS), dtype=np.uint8).reshape(24, 552)
    assert np.array_equal(arr, expected_arr)


@pytest.mark.parametrize(("label", "dat_path", "fx_path"), R5_BUNDLES, ids=[x[0] for x in R5_BUNDLES])
def test_r5_bundles_match_python(label: str, dat_path: Path, fx_path: Path, tmp_path: Path) -> None:
    require_cli()
    raw = dat_path.read_bytes()
    lib = fx_path.read_bytes()
    expected, entries = _expected_parse_summary(raw, lib)
    out_dir = tmp_path / "entries"
    code, payload = run_json(["camdata-parse", "--dat", str(dat_path), "--library", str(fx_path), "--out-dir", str(out_dir)])
    assert error_of(code, payload) is None
    assert payload == expected, label

    for unit_idx, unit_name in enumerate(dc.NAMES):
        for slot in store.SLOTS:
            got_bytes = (out_dir / "units" / unit_name / "recipes" / f"{slot:02d}.entry").read_bytes()
            assert got_bytes == entries[(unit_idx, slot)], (label, unit_name, slot)

    arr = run_npy(["camdata-entries", "--dat", str(dat_path), "--library", str(fx_path)], tmp_path / "entries.npy")
    assert arr.dtype == np.uint8 and arr.shape == (24, 552)
    expected_arr = np.frombuffer(b"".join(entries[(u, s)] for u in range(3) for s in store.SLOTS), dtype=np.uint8).reshape(24, 552)
    assert np.array_equal(arr, expected_arr)


# --------------------------------------------------------------------------- Damage & edge cases


def _huge_size_synthetic_dats() -> dict[str, bytes]:
    """Synthetic DAT headers with u32::MAX / 2**31 sizes/counts that could wrap or panic."""
    cases: dict[str, bytes] = {}
    for label, start, size, bss in (
        ("huge_4g_x_4g_section", 0xFFFFFFFF, 0xFFFFFFFF, 0),
        ("huge_2g_x_1_section", 256, 0x80000000, 0),
        ("huge_bss_u32max", 256, 4, 0xFFFFFFFF),
        ("huge_nreloc_65535", 256, 4, 0),
    ):
        blob = bytearray(512)
        blob[:8] = b"GXRCE001"
        struct.pack_into(">HHI", blob, 8, 1, 256, 0)
        struct.pack_into(">I", blob, 16, len(blob))
        struct.pack_into(">I", blob, 28, 127)
        struct.pack_into(">II", blob, 112, 256, bss)
        if label == "huge_nreloc_65535":
            struct.pack_into(">H", blob, 120, 0xFFFF)
        struct.pack_into(">HHII", blob, 128, 1, 0, start, size)
        cases[label] = _reseal(bytes(blob))
    return cases


def test_synthetic_malformed_and_oversized_dats_match_python(tmp_path: Path) -> None:
    require_cli()
    bad_cases: dict[str, bytes] = {
        "empty": b"",
        "short_magic": b"GXRCE00",
        "short_header": b"GXRCE001" + b"\0" * 247,
        "wrong_magic": b"GXRCE002" + b"\0" * 248,
        "fake_dat": fake_dat(seed=1, n=512),
        **_huge_size_synthetic_dats(),
    }
    for gen in (0, 123, 126, 130, 151, 157, 177, 999):
        hdr = bytearray(256)
        hdr[:8] = b"GXRCE001"
        struct.pack_into(">I", hdr, 28, gen)
        bad_cases[f"unsupported_g{gen}"] = bytes(hdr)

    for name, blob in bad_cases.items():
        p = tmp_path / f"{name}.dat"
        p.write_bytes(blob)
        _assert_parse_matches_python_error(p, blob)

    # Oversized limit from camdata/intake.py (MAX_DAT_BYTES = 98304)
    oversized = tmp_path / "oversized.dat"
    oversized.write_bytes(b"\0" * (intake.MAX_DAT_BYTES + 1))
    code, payload = run_json(["camdata-parse", "--dat", str(oversized)])
    err = error_of(code, payload)
    assert err is not None
    assert err.kind == "dat_error"
    assert "文件过大" in err.message


def test_real_dat_damage_cases_match_test_datcodec(tmp_path: Path) -> None:
    require_cli()
    if not SINGLE_DATS:
        pytest.skip("no single-file fixture DATs on this machine")
    by_label = dict(SINGLE_DATS)
    for label in ("g127", "g135", "g152", "g156", "g158"):
        if label not in by_label:
            continue
        raw = by_label[label].read_bytes()
        variants: dict[str, bytes] = {
            f"{label}_truncated": raw[:-4],
            f"{label}_extended": raw + b"\0\0\0\0",
            f"{label}_bad_magic": b"GXRCE002" + raw[8:],
            f"{label}_body_crc": raw[:5000] + bytes([raw[5000] ^ 1]) + raw[5001:],
            f"{label}_header_crc": raw[:40] + bytes([raw[40] ^ 1]) + raw[41:],
        }
        # Pool byte flip (pool CRC)
        shared = struct.unpack_from(">II", raw, 128 + 12 * 5 + 4)[0]
        b = bytearray(raw)
        b[shared + 80] ^= 0xFF
        variants[f"{label}_pool_crc"] = _reseal(bytes(b))

        # map128 pin
        map_at = struct.unpack_from(">II", raw, 128 + 12 * 3 + 4)[0]
        b = bytearray(raw)
        b[map_at + 40] ^= 1
        variants[f"{label}_pin"] = _reseal(bytes(b))

        # Reserved header byte 240 -> rejected by pack(dat) != raw
        b = bytearray(raw)
        b[240] = 1
        variants[f"{label}_unmodeled_header"] = _reseal(bytes(b))

        # Wrong bank generation
        directory = dc.u32(raw, 156)
        bank = dc.u32(raw, directory + 16)
        b = bytearray(raw)
        struct.pack_into(">I", b, bank + 16, 999)
        struct.pack_into(">I", b, bank + 28, dc.crc(b[bank : bank + 28]))
        variants[f"{label}_bank_gen"] = _reseal(bytes(b))

        for name, blob in variants.items():
            p = tmp_path / f"{name}.dat"
            p.write_bytes(blob)
            _assert_parse_matches_python_error(p, blob)

    # Single-file DAT given an unwanted library
    g127_path = by_label.get("g127")
    if g127_path is not None:
        fx = tmp_path / "fake.bin"
        fx.write_bytes(fake_gxrfx())
        _assert_parse_matches_python_error(g127_path, g127_path.read_bytes(), fx)


def test_r5_damage_cases_match_python(tmp_path: Path) -> None:
    require_cli()
    if not R5_BUNDLES:
        pytest.skip("no r5 bundles on this machine")
    label, dat_path, fx_path = R5_BUNDLES[0]
    raw = dat_path.read_bytes()
    lib = fx_path.read_bytes()

    # Missing library
    _assert_parse_matches_python_error(dat_path, raw, None)

    # Damaged libraries
    for name, bad_lib in (
        ("truncated", lib[:-1]),
        ("changed", lib[:100] + bytes([lib[100] ^ 1]) + lib[101:]),
        ("trailing", lib + b"x"),
        ("wrong_magic", b"BAD" + lib[3:]),
    ):
        lp = tmp_path / f"{label}_{name}.bin"
        lp.write_bytes(bad_lib)
        _assert_parse_matches_python_error(dat_path, raw, lp)

    # Damaged r5 shell (engine byte flip triggers engine binding failure)
    b = bytearray(raw)
    b[300] ^= 1
    bad_dat = tmp_path / f"{label}_bad_engine.dat"
    bad_dat.write_bytes(_reseal(bytes(b)))
    _assert_parse_matches_python_error(bad_dat, bad_dat.read_bytes(), fx_path)


# --------------------------------------------------------------------------- Menu & knob_table


def test_menu_without_dat_matches_final_json() -> None:
    require_cli()
    code, payload = run_json(["camdata-menu"])
    assert error_of(code, payload) is None
    assert payload == py_menu.load()


@pytest.mark.parametrize(("label", "dat_path"), SINGLE_DATS, ids=[x[0] for x in SINGLE_DATS])
def test_menu_with_single_file_dat_matches_python(label: str, dat_path: Path) -> None:
    require_cli()
    raw = dat_path.read_bytes()
    dat = dc.parse(raw)
    expected = py_menu.load()
    if dat.kv2_block is not None:
        expected = knob_table.read(dat.kv2_block, expected, mount_knobs_from_50mm=dc.PROFILES[dat.generation].mount_knobs_from_50mm)
    code, payload = run_json(["camdata-menu", "--dat", str(dat_path)])
    assert error_of(code, payload) is None
    assert payload == expected, label


@pytest.mark.parametrize(("label", "dat_path", "fx_path"), R5_BUNDLES, ids=[x[0] for x in R5_BUNDLES])
def test_menu_with_r5_bundle_matches_python(label: str, dat_path: Path, fx_path: Path) -> None:
    require_cli()
    raw = dat_path.read_bytes()
    lib = fx_path.read_bytes()
    dat = dc.parse(raw, lib)
    assert dat.kv2_block is not None
    expected = knob_table.read(dat.kv2_block, py_menu.load(), mount_knobs_from_50mm=dc.PROFILES[dat.generation].mount_knobs_from_50mm)
    code, payload = run_json(["camdata-menu", "--dat", str(dat_path), "--library", str(fx_path)])
    assert error_of(code, payload) is None
    assert payload == expected, label


def _synthetic_r3_widen_kv() -> bytes:
    """Synthetic R3 kv block: recipe_count=0, item_count=1, control=1, point_count=2, min_level=32767, doses=(-2, 3)."""
    block = bytearray(64)
    block[:8] = b"GXKV2R3\0"
    struct.pack_into(">HH", block, 22, 0, 1)
    struct.pack_into(">II", block, 36, 44, 44)
    block[44] = 1
    block[45] = 2
    struct.pack_into(">h", block, 46, 32767)
    struct.pack_into(">I", block, 56, 60)
    struct.pack_into(">2h", block, 60, -2, 3)
    return bytes(block)


def test_knob_table_r3_level_widening_matches_python(tmp_path: Path) -> None:
    require_cli()
    block = _synthetic_r3_widen_kv()
    kv_path = tmp_path / "r3_widen.kv"
    kv_path.write_bytes(block)
    expected = knob_table.read(block, py_menu.load())
    assert expected["items"][0]["points"] == [{"level": 32767, "dose": -2}, {"level": 32768, "dose": 3}]

    code, payload = run_json(["camdata-knob-table", "--kv", str(kv_path)])
    assert error_of(code, payload) is None
    assert payload == expected
    # WASM has no standalone knob-table export (spec: parse, entries, menu); `camdata_menu` runs the same code.


# --------------------------------------------------------------------------- Recipe tags


TAG = "GXR1 6a3a5a3c02 28 06 A8999B999-"
SYNTHETIC_TAG = "GXR1 0123456789 50 17 799999999-"


@pytest.mark.parametrize(
    "value",
    [
        TAG,
        TAG + "\0",
        "GXR1 0123456789 MT 99 9999C99999",
        "GXR1 d25d2f0a68 50 01",
        "GXR1 6A3A5A3C02 MT 01 ----------",
        "GXR1 6a3a5a3c02 28 06 F0I999999-",
        "GXR1 6a3a5a3c02 28 06 09A999999-",
        SYNTHETIC_TAG.ljust(69),
        SYNTHETIC_TAG.ljust(69) + "\0",
        "",
        "Alice, 2026",
        "GXR1",
        "GXR1 ",
        "GXR1 6a3a5a3c02 28",
        "GXR1 6a3a5a3c0 28 06",
        "GXR1 6a3a5a3c020 28 06",
        "GXR1 6a3a5a3c0z 28 06",
        "GXR1 6a3a5a3c02 29 06",
        "GXR1 6a3a5a3c02 mt 06",
        "GXR1 6a3a5a3c02 28 00",
        "GXR1 6a3a5a3c02 28 100",
        "GXR1 6a3a5a3c02 28 6",
        "GXR1  6a3a5a3c02 28 06",
        " " + TAG,
        TAG + " ",
        TAG + "\n",
        TAG.replace("28 ", "28\t"),
        TAG + "é",
        TAG + "\x00junk",
        TAG + "\0\0",
        TAG + "*",
        TAG + "\x01",
        b"GXR1 0123456789 28 01 \xff",
        "GXR1 0123456789 28 01 " + "x" * 26,
        "GXR1 6a3a5a3c02 28 06 Classic Neg",
        "GXR1 6a3a5a3c02 28 06 A99999999",
        "GXR1 6a3a5a3c02 28 06 A9999999999",
        "GXR1 6a3a5a3c02 28 06 A9999Z999-",
        "GXR1 6a3a5a3c02 28 06 a99999999-",
        "GXR1 6a3a5a3c02 28 06 J99999999-",
        SYNTHETIC_TAG.ljust(68),
        SYNTHETIC_TAG.ljust(70),
    ],
)
def test_recipe_tag_matches_python(value: str | bytes) -> None:
    require_cli()
    expected = parse_recipe_tag(value)
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    if isinstance(value, str) and "\0" not in value:
        code, payload = run_json(["camdata-recipe-tag", "--tag", value])
        assert error_of(code, payload) is None
        assert payload == {"tag": expected}
    code, payload = run_json(["camdata-recipe-tag", "--tag-hex", raw.hex()])
    assert error_of(code, payload) is None
    assert payload == {"tag": expected}


# --------------------------------------------------------------------------- WASM vs Native


def test_wasm_camdata_matches_native_on_synthetic_and_damaged(tmp_path: Path) -> None:
    paths: list[Path] = []
    synthetic_cases: dict[str, bytes] = {
        "empty": b"",
        "wrong_magic": b"GXRCE002" + b"\0" * 248,
        "fake": fake_dat(seed=7, n=512),
        "oversized": b"\0" * (intake.MAX_DAT_BYTES + 1),
        **_huge_size_synthetic_dats(),
    }
    for name, blob in synthetic_cases.items():
        p = tmp_path / f"{name}.dat"
        p.write_bytes(blob)
        paths.append(p)
    if SINGLE_DATS:
        raw = SINGLE_DATS[0][1].read_bytes()
        for name, blob in (
            ("truncated", raw[:-4]),
            ("bad_crc", raw[:40] + bytes([raw[40] ^ 1]) + raw[41:]),
        ):
            p = tmp_path / f"{name}.dat"
            p.write_bytes(blob)
            paths.append(p)
    proc = run_area("camdata", paths, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_wasm_camdata_matches_native_on_real_dats(tmp_path: Path) -> None:
    all_paths = [p for _, p in SINGLE_DATS] + [dat for _, dat, _ in R5_BUNDLES]
    if not all_paths:
        pytest.skip("no real DAT fixtures on this machine")
    proc = run_area("camdata", all_paths, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_json_nesting_depth_native(tmp_path: Path) -> None:
    require_cli()
    ok_cases = {
        "arr_128_scalar.json": "[" * 128 + "0" + "]" * 128,
        "arr_128_empty.json": "[" * 128 + "]" * 128,
        "obj_128_scalar.json": '{"a":' * 128 + "0" + "}" * 128,
        "obj_128_empty.json": '{"a":' * 127 + "{}" + "}" * 127,
    }
    deep_cases = {
        "arr_129_scalar.json": "[" * 129 + "0" + "]" * 129,
        "arr_129_empty.json": "[" * 129 + "]" * 129,
        "arr_20000.json": "[" * 20_000 + "0" + "]" * 20_000,
        "obj_129_scalar.json": '{"a":' * 129 + "0" + "}" * 129,
        "obj_129_empty.json": '{"a":' * 128 + "{}" + "}" * 128,
        "obj_20000.json": '{"a":' * 20_000 + "0" + "}" * 20_000,
    }
    paths: list[Path] = []
    for name, text in ok_cases.items():
        p = tmp_path / name
        p.write_text(text, encoding="utf-8")
        code, payload = run_json(["camdata-parse-json", "--json", str(p)])
        assert error_of(code, payload) is None, name
        assert payload == {"ok": True}, name
        paths.append(p)
    for name, text in deep_cases.items():
        p = tmp_path / name
        p.write_text(text, encoding="utf-8")
        code, payload = run_json(["camdata-parse-json", "--json", str(p)])
        err = error_of(code, payload)
        assert err is not None, name
        assert err.kind == "json", (name, err)
        assert err.message == "JSON 嵌套太深", (name, err)
        paths.append(p)


def test_json_size_limit_native(tmp_path: Path) -> None:
    """4 MiB is accepted and one byte more is rejected (Sol camdata r2 P2: the limit lives in the shared function)."""
    require_cli()
    limit = 4 * 1024 * 1024
    at_limit = tmp_path / "at_limit.json"
    at_limit.write_bytes(b'"' + b"x" * (limit - 2) + b'"')
    over = tmp_path / "over_limit.json"
    over.write_bytes(b'"' + b"x" * (limit - 1) + b'"')
    assert at_limit.stat().st_size == limit and over.stat().st_size == limit + 1
    code, payload = run_json(["camdata-parse-json", "--json", str(at_limit)])
    assert error_of(code, payload) is None
    assert payload == {"ok": True}
    code, payload = run_json(["camdata-parse-json", "--json", str(over)])
    err = error_of(code, payload)
    assert err is not None
    assert (err.kind, err.message) == ("json", "JSON 文件过大")


def test_wasm_exports_only_the_spec_listed_camdata_functions() -> None:
    """UNIT-COMMON: bind only what the spec lists (Sol camdata r2 P2)."""
    if not PKG.is_file():
        if os.environ.get("GXR_REQUIRE_CORE") == "1":
            pytest.fail(f"WASM package is required but not built at {PKG}")
        pytest.skip("WASM package not built; run make core-wasm")
    dts = PKG.with_suffix(".d.ts").read_text(encoding="utf-8")
    exported = set(re.findall(r"export function (camdata_\w+)\(", dts))
    assert exported == {"camdata_parse", "camdata_entries", "camdata_menu"}
