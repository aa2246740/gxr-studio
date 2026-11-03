"""g156/g155c DATs stay outside this repository and are read only at test time."""

from __future__ import annotations

import ctypes
import hashlib
import shutil
import struct
import sys

import pytest

import e0_support as t
from gxr_studio.camdata import datcodec as dc
from gxr_studio.camdata import store
from test_datcodec import reseal
from test_g152 import client as client

PACKAGES = (
    ("g156", 156, "f42b81a854d86b66ee7f65ac0123153c7e54e59d6e3d2079834490771d0ce1e0"),
    ("g156_test", 156, "0f4f26d4cd3669915b41ce8e38260a61c60c590e2c581cb72e93f52a2ac631f7"),
    ("g155c", 155, "69fa85104fcb449e613950bb58c802b9b64464769ae524fe3f7303e06093c2ad"),
    ("g155c_test", 155, "c3017d3663e9501b43c921e3b46333b74d751eaa170781c4e1a00d90118f0335"),
)


def test_parent_helpers_load_without_writing_bytecode(tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    parent.mkdir()
    path = parent / "parent_helper.py"
    path.write_text("answer = 42\n", encoding="utf-8")
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    original = sys.dont_write_bytecode
    assert t.load_by_path("read_only_parent_helper", path).answer == 42
    assert sys.dont_write_bytecode is original
    assert list(parent.iterdir()) == [path]


@pytest.fixture(params=PACKAGES, ids=[p[0] for p in PACKAGES])
def package(request):
    name, generation, digest = request.param
    path = t.REPO / f"firmware/package_v310_{name}/GXRCE.DAT"
    if not path.is_file():
        pytest.skip(f"{name} DAT is absent from the parent repository")
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == digest, f"{name} fixture changed"
    return name, generation, raw


def bank_offsets(raw):
    directory = dc.u32(raw, 156)
    return [dc.u32(raw, directory + 64 * unit + 16) for unit in range(3)]


@pytest.fixture(scope="module")
def parent_format():
    path = t.REPO / "research/v39_g153/dat_format.py"
    if not path.is_file():
        pytest.skip("The parent's independent DAT reader is absent")
    return t.load_by_path("g156_test_dat_format", path)


@pytest.fixture(scope="module")
def camera_gate(tmp_path_factory, parent_format):
    """Call the parent's parser builder with all build outputs redirected outside both repos."""
    path = t.REPO / "research/v39_g153/format_checks.py"
    product = t.REPO / "firmware/engine_v68_g156/candidate/containers/product"
    if not path.is_file() or not product.is_dir() or shutil.which("clang") is None or sys.platform != "darwin":
        pytest.skip("The parent's g156 C parser builder needs its sources and macOS clang")
    previous_path = sys.path[:]
    try:
        formatter = t.load_by_path("g156_test_format_checks", path, alias={"dat_format": parent_format})
    finally:
        sys.path[:] = previous_path
    output = tmp_path_factory.mktemp("g156-camera-gate")
    (output / "evidence").mkdir()
    formatter.PRODUCT = product
    formatter.TMP = output / "format"
    formatter.HERE = output
    library = formatter.library()

    def validate(raw, unit):
        return library.check_dat(ctypes.create_string_buffer(raw), len(raw), ctypes.create_string_buffer(formatter.trust(raw)), unit)

    return validate


def test_real_dat_round_trip_preserves_banks_pins_and_records(package, parent_format):
    _name, generation, raw = package
    dat = dc.parse(raw)
    assert dat.generation == generation
    assert dc.pack(dat) == raw
    directory = dc.u32(raw, 156)
    for unit, bank in enumerate(bank_offsets(raw)):
        assert dc.u32(raw, bank + 16) == 155
        module = dat.modules[unit]
        assert b"".join(module.records) == raw[bank + 32 : bank + 1056]
        assert module.pin == raw[directory + 64 * unit + 32 : directory + 64 * unit + 64]
        assert module.crc_pin == dc.PIN_CRC32[unit]
        assert dc.crc(dat.entry_of(unit, 4)[:448]) == dc.STOCK_ENTRY_CRC[unit]
        for slot in module.entries:
            assert dat.entry_of(unit, slot) == parent_format.expand(raw, unit, slot)
    for index, look in enumerate(dat.looks):
        assert look.entry == parent_format.expand_effect(raw, index)


@pytest.mark.parametrize("unit", range(3), ids=dc.NAMES)
@pytest.mark.parametrize("wrong_generation", (0, 154, 156, 157))
def test_each_bank_requires_generation155_even_with_valid_crcs(package, unit, wrong_generation):
    _name, _generation, raw = package
    changed = bytearray(raw)
    bank = bank_offsets(raw)[unit]
    struct.pack_into(">I", changed, bank + 16, wrong_generation)
    struct.pack_into(">I", changed, bank + 28, dc.crc(changed[bank : bank + 28]))
    with pytest.raises(dc.DatError, match="配方库头、CRC 或代数不合格"):
        dc.parse(reseal(bytes(changed)))


def test_user_edit_keeps_banks_and_native_records(package):
    _name, generation, raw = package
    dat = dc.parse(raw)
    users = [{"base": 5, "levels": [1] + [0] * 10}]
    changed = dc.pack(dc.replace_users(dat, users))
    assert changed != raw
    assert dc.u32(changed, 28) == generation
    for bank in bank_offsets(raw):
        assert dc.u32(changed, bank + 16) == 155
        assert changed[bank : bank + 1056] == raw[bank : bank + 1056]
    extension = dc.u32(raw, 204) + dc.LEGACY_BYTES
    assert changed[:20] == raw[:20]
    assert changed[28:extension] == raw[28:extension]
    assert changed[extension + dc.EXT_BYTES :] == raw[extension + dc.EXT_BYTES :]
    assert dc.parse(changed).users == users


def test_g156_original_template_and_user_edit_pass_camera_c_gate(package, camera_gate, tmp_path):
    _name, generation, raw = package
    if generation != 156:
        for unit in range(3):
            assert camera_gate(raw, unit) == 106
        return
    home = tmp_path / "camera-data-home"
    store.import_from_dat(raw, home)
    template = store.load_template(home)
    assert template == raw
    changed = dc.pack(dc.replace_users(dc.parse(template), [{"base": 5, "levels": [1] + [0] * 10}]))
    for output in (raw, template, changed):
        assert dc.u32(output, 28) == 156
        assert [dc.u32(output, at + 16) for at in bank_offsets(output)] == [155, 155, 155]
        for unit in range(3):
            assert camera_gate(output, unit) == 0
    for wrong_generation in (155, 157):
        broken = bytearray(changed)
        struct.pack_into(">I", broken, 28, wrong_generation)
        for unit in range(3):
            assert camera_gate(reseal(bytes(broken)), unit) == 106


@pytest.mark.parametrize("method", ("file", "upload"))
def test_g156_file_and_upload_import_preserve_the_template(package, client, settings, tmp_path, method):
    _name, generation, raw = package
    source = tmp_path / "GXRCE.DAT"
    source.write_bytes(raw)
    response = (
        client.post("/api/camdata/import-file", json={"path": str(source)})
        if method == "file"
        else client.post("/api/camdata/import-upload", content=raw)
    )
    assert response.status_code == 200
    assert response.json()["generation"] == generation
    assert store.load_template(settings.home_dir) == raw
    response = client.post("/api/camdata/import-upload", content=raw)
    assert response.status_code == 200 and response.json()["unchanged"] is True
    state = client.get("/api/camdata/status").json()["camera_data"]
    assert state["generation"] == generation
    assert state["source"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert store.load_template(settings.home_dir) == raw
    for unit in client.get("/api/recipes").json()["units"]:
        assert len(unit["recipes"]) == 8 and len(unit["looks"]) == 10
