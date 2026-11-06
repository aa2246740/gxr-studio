"""g158 fixtures and camera sources stay in the optional, read-only parent checkout."""

from __future__ import annotations

import hashlib
import struct
from dataclasses import replace

import pytest

import e0_support as t
from g152_support import UNITS, CameraReference
from gxr_studio.camdata import datcodec as dc
from gxr_studio.camdata import intake, store
from gxr_studio.render.params import EditError, RecipeSel
from gxr_studio.render.recipes import CameraData
from test_camdata_api import tree
from test_datcodec import reseal
from test_g152 import client as client
from test_g156 import bank_offsets, graft_engine, need_package
from test_g156 import parent_format as parent_format

PACKAGES = (
    ("g158", "756d5840da9d0393f29411a4f8f61c7e2bb88fb546c814b316982ca8c7cb24f6"),
    ("g158_test", "6a40262c3fc5b94e7152012c655f5c7d2dc98bf6186cf3e80d2429b76f45fcee"),
)


def read_package(name):
    path = t.REPO / f"firmware/package_v310_{name}/local/SD_ROOT/GXRCE.DAT"
    if not path.is_file():
        pytest.skip(f"{name} DAT is absent from the parent repository")
    raw = path.read_bytes()
    digest = dict(PACKAGES).get(name)
    if digest is not None:
        assert hashlib.sha256(raw).hexdigest() == digest, f"{name} fixture changed"
    return raw


@pytest.fixture(scope="module", params=PACKAGES, ids=[p[0] for p in PACKAGES])
def package(request):
    name = request.param[0]
    return name, read_package(name)


@pytest.fixture(scope="module")
def camera(package, tmp_path_factory):
    _name, raw = package
    home = tmp_path_factory.mktemp("g158-camera")
    store.import_from_dat(raw, home)
    return CameraData(home)


@pytest.fixture(scope="module")
def reference(tmp_path_factory):
    return CameraReference(158, tmp_path_factory.mktemp("g158-reference"))


def relabel(raw, generation, bank_generation):
    changed = bytearray(raw)
    struct.pack_into(">I", changed, 28, generation)
    for bank in bank_offsets(raw):
        struct.pack_into(">I", changed, bank + 16, bank_generation)
        struct.pack_into(">I", changed, bank + 28, dc.crc(changed[bank : bank + 28]))
    return reseal(bytes(changed))


def engine_fields(raw):
    at, size = dc.u32(raw, 132), dc.u32(raw, 136)
    relocations = [struct.unpack_from(">II", raw, dc.u32(raw, 144) + 8 * i) for i in range(dc.u16(raw, 120))]
    return {"engine": raw[at : at + size], "entry": dc.u32(raw, 112), "bss": dc.u32(raw, 116), "relocations": relocations}


@pytest.fixture(params=("g156_relabelled", "g157_relabelled", "g158_in_g156_layout", "g158_in_g155_layout"))
def mismatch(package, request):
    name, valid = package
    suffix = "_test" if name.endswith("_test") else ""
    new = dc.parse(valid)
    if request.param == "g157_relabelled":
        old_raw = read_package("g157" + suffix)
        invalid = replace(new, **engine_fields(old_raw))
        raw = relabel(old_raw, 158, 158)
    else:
        old = dc.parse(need_package("g156" + suffix)[1])
        invalid = replace(old, generation=158)
        if request.param == "g156_relabelled":
            raw = relabel(need_package("g156" + suffix)[1], 158, 158)
        else:
            generation = 155 if request.param == "g158_in_g155_layout" else 158
            invalid = replace(invalid, generation=generation, **engine_fields(valid))
            raw = relabel(graft_engine(need_package("g156" + suffix)[1], new), generation, 155 if generation == 155 else 158)
    return invalid, raw, valid


def test_engine_mismatches_fail_parse_pack_and_intake(mismatch):
    invalid, raw, _valid = mismatch
    message = f"{invalid.generation} 引擎或链接布局未登记"
    with pytest.raises(dc.DatError, match=message):
        dc.parse(raw)
    with pytest.raises(dc.DatError, match=message):
        dc.pack(invalid)
    with pytest.raises(intake.IntakeError) as rejection:
        intake.inspect_dat(raw)
    assert rejection.value.code == "dat_invalid"


def test_rejected_upload_keeps_the_template_catalog_and_version(mismatch, client, settings):
    _invalid, raw, valid = mismatch
    assert client.post("/api/camdata/import-upload", content=valid).status_code == 200
    before = tree(settings.home_dir)
    catalog = client.get("/api/recipes").json()
    version = client.get("/api/camdata/status").json()["data_version"]
    response = client.post("/api/camdata/import-upload", content=raw)
    assert response.status_code == 422 and response.json()["error"] == "dat_invalid"
    assert tree(settings.home_dir) == before
    assert store.load_template(settings.home_dir) == valid
    assert client.get("/api/recipes").json() == catalog
    assert client.get("/api/camdata/status").json()["data_version"] == version


@pytest.mark.parametrize("damage", ("engine", "entry", "bss", "relocations", "data_end"))
def test_pack_binds_every_engine_field(package, damage):
    _name, raw = package
    dat = dc.parse(raw)
    changes = {
        "engine": {"engine": bytes([dat.engine[0] ^ 1]) + dat.engine[1:]},
        "entry": {"entry": dat.entry + 4},
        "bss": {"bss": dat.bss + 4},
        "relocations": {"relocations": []},
        "data_end": {"effect_area": dat.effect_area + bytes(4)},
    }
    with pytest.raises(dc.DatError, match="158 引擎或链接布局未登记"):
        dc.pack(replace(dat, **changes[damage]))


def test_real_round_trip_layout_and_banks(package):
    name, raw = package
    dat = dc.parse(raw)
    test = name.endswith("_test")
    assert dat.generation == 158 and dc.pack(dat) == raw
    assert (len(raw), len(dat.engine), dat.bss) == ((94952, 69624, 4360) if test else (92484, 67392, 4360))
    assert (len(dat.effect_area), dc.u16(dat.effect_area, 8), dc.u16(dat.effect_area, 10)) == ((5924, 30, 30) if test else (5620, 29, 29))
    assert tuple(dat.effect_area[16:20]) == ((0, 28, 26, 27) if test else (0, 26, 25, 25))
    for unit, bank in enumerate(bank_offsets(raw)):
        assert dc.u32(raw, bank + 16) == 158
        assert b"".join(dat.modules[unit].records) == raw[bank + 32 : bank + 1056]
        assert dat.modules[unit].crc_pin == dc.PIN_CRC32[unit]


def test_all_builtin_and_look_entries_match_parent_reader(package, parent_format):
    name, raw = package
    dat = dc.parse(raw)
    previous = read_package(name.replace("158", "157"))
    for unit, module in enumerate(dat.modules):
        for slot in module.entries:
            assert dat.entry_of(unit, slot) == parent_format.expand(raw, unit, slot)
            assert dat.entry_of(unit, slot) == parent_format.expand(previous, unit, slot)
    for index, look in enumerate(dat.looks):
        assert look.entry == parent_format.expand_effect(raw, index)
        assert look.entry == parent_format.expand_effect(previous, index)


@pytest.mark.parametrize("unit", range(3), ids=UNITS)
@pytest.mark.parametrize("wrong_generation", (0, 155, 156, 157, 159))
def test_each_bank_requires_generation158(package, unit, wrong_generation):
    _name, raw = package
    changed = bytearray(raw)
    bank = bank_offsets(raw)[unit]
    struct.pack_into(">I", changed, bank + 16, wrong_generation)
    struct.pack_into(">I", changed, bank + 28, dc.crc(changed[bank : bank + 28]))
    with pytest.raises(dc.DatError, match="配方库头、CRC 或代数不合格"):
        dc.parse(reseal(bytes(changed)))


@pytest.mark.parametrize("count", (0, 1, 22))
def test_valid_user_edits_preserve_the_rest_of_the_dat_and_pass_c(package, count, reference):
    _name, raw = package
    dat = dc.parse(raw)
    users = [{"base": 5, "levels": [1] + [0] * 10} for _ in range(count)]
    changed = dc.pack(dc.replace_users(dat, users))
    assert dc.parse(changed).users == users
    extension = dc.u32(raw, 204) + dc.LEGACY_BYTES
    assert changed[:20] == raw[:20] and changed[28:extension] == raw[28:extension]
    assert changed[extension + dc.EXT_BYTES :] == raw[extension + dc.EXT_BYTES :]
    for unit in range(3):
        assert reference.validate(raw, unit) and reference.validate(changed, unit)


def test_flat_list_rejects_twenty_three_users(package, reference):
    _name, raw = package
    dat = dc.parse(raw)
    users = [{"base": 5, "levels": [0] * 11} for _ in range(23)]
    with pytest.raises(dc.DatError, match="总数超限"):
        dc.replace_users(dat, users)
    with pytest.raises(dc.DatError, match="总数超限"):
        dc.pack(replace(dat, users=users))
    changed = bytearray(raw)
    at = dc.u32(raw, 204) + dc.LEGACY_BYTES
    changed[at : at + dc.EXT_BYTES] = dc._extension(dat.ext_config, [b"".join(m.records) for m in dat.modules], users)
    changed = reseal(bytes(changed))
    with pytest.raises(dc.DatError, match="总数超限"):
        dc.parse(changed)
    for unit in range(3):
        assert not reference.validate(changed, unit)


@pytest.mark.parametrize("generation", (157, 159, 160, 161))
def test_unregistered_generations_are_rejected_without_fixtures(generation):
    raw = bytearray(256)
    raw[:8] = b"GXRCE001"
    struct.pack_into(">I", raw, 28, generation)
    with pytest.raises(dc.UnsupportedGeneration):
        dc.parse(bytes(raw))
    with pytest.raises(intake.IntakeError) as rejection:
        intake.inspect_dat(bytes(raw))
    assert rejection.value.code == "generation_unsupported"


@pytest.mark.parametrize("generation", (157, 159, 160, 161))
def test_unregistered_generations_fail_pack_and_upload_without_replacing_data(package, generation, client, settings):
    _name, valid = package
    with pytest.raises(dc.UnsupportedGeneration):
        dc.pack(replace(dc.parse(valid), generation=generation))
    assert client.post("/api/camdata/import-upload", content=valid).status_code == 200
    before = tree(settings.home_dir)
    response = client.post("/api/camdata/import-upload", content=relabel(valid, generation, generation))
    assert response.status_code == 422 and response.json()["error"] == "generation_unsupported"
    assert tree(settings.home_dir) == before and store.load_template(settings.home_dir) == valid


@pytest.mark.parametrize("method", ("file", "upload"))
def test_import_preserves_the_template_and_lists_eighteen_looks(package, method, client, settings, tmp_path):
    _name, raw = package
    source = tmp_path / "GXRCE.DAT"
    source.write_bytes(raw)
    response = (
        client.post("/api/camdata/import-file", json={"path": str(source)})
        if method == "file"
        else client.post("/api/camdata/import-upload", content=raw)
    )
    assert response.status_code == 200 and response.json()["generation"] == 158
    assert store.load_template(settings.home_dir) == raw
    assert client.post("/api/camdata/import-upload", content=raw).json()["unchanged"] is True
    for unit in client.get("/api/recipes").json()["units"]:
        assert len(unit["looks"]) == 18


@pytest.mark.parametrize("unit", range(3), ids=UNITS)
def test_looks_resolve_through_their_own_base_including_official_acros(package, camera, reference, unit):
    _name, raw = package
    dat = dc.parse(raw)
    listing = camera.listing()[unit]["looks"]
    for index, look in enumerate(dat.looks):
        if not look.unit_mask >> unit & 1:
            continue
        info = next(item for item in listing if item["name"] == look.name)
        assert info["slot"] == look.base
        assert info["controls"] == camera.controls(unit, look.base)
        assert look.entry[:2] == dat.entry_of(unit, look.base)[:2]
        sel = RecipeSel(UNITS[unit], look.base, kind="look", id=info["id"])
        assert camera.resolve(sel).entry == look.entry == reference.resolve(raw, unit, index, {})
        if look.name == "Acros":
            assert look.base == 3 and look.entry[1] == 1
            assert [c["name"] for c in info["controls"]] == ["contrast", "highlight", "shadow", "fade", "filter"]
            for level in next(c for c in info["controls"] if c["name"] == "filter")["levels"]:
                assert camera.resolve(replace(sel, levels=(("filter", level),))).entry == reference.resolve(raw, unit, index, {"filter": level})
            with pytest.raises(EditError) as rejection:
                camera.resolve(replace(sel, levels=(("saturation", 1),)))
            assert rejection.value.code == "knob_not_available"
    assert sum(item["name"] == "Acros" for item in listing) == 1


@pytest.mark.parametrize("unit", range(3), ids=UNITS)
def test_constructed_colour_look_uses_flags7_base_knobs(package, camera, reference, unit):
    _name, raw = package
    camera = CameraData(camera.home)
    dat = dc.parse(raw)
    # Neither real package contains a colour Look on a flags7 base.
    assert not any(look.base != 3 and look.entry[1] == 7 for look in dat.looks)
    entry = dat.entry_of(unit, 6)
    assert entry[:2] == bytes((6, 7))
    camera._add_extra(unit, "look", 999, "Constructed", 6, entry, {})
    info = camera.listing()[unit]["looks"][-1]
    assert [c["name"] for c in info["controls"]] == [
        "amount",
        "contrast",
        "highlight",
        "shadow",
        "fade",
        "saturation",
        "warmth",
        "tint",
        "split",
    ]
    sel = RecipeSel(UNITS[unit], 6, kind="look", id=info["id"])
    assert camera.resolve(sel).entry == entry
    for control in info["controls"]:
        for level in control["levels"]:
            levels = {control["name"]: level}
            assert camera.resolve(replace(sel, levels=tuple(levels.items()))).entry == reference.builtin(raw, unit, 6, levels)
