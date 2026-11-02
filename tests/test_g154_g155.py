"""154/155 的真实尺寸、MOUNT 控制规则、列表容量和导入状态。"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import replace

import pytest

from g152_support import LOOK_NAMES, UNITS, need_dat
from gxr_studio.camdata import datcodec as dc
from gxr_studio.render.params import EditError, RecipeSel
from test_camdata_api import tree
from test_datcodec import reseal
from test_g152 import cameras as cameras
from test_g152 import client as client
from test_g152 import references as references

PACKAGES = [(g, t) for g in (154, 155) for t in (False, True)]


def effect_location(raw):
    pool = dc.u32(raw, 192)
    return pool + dc.u32(raw, pool + 64), dc.u32(raw, pool + 68)


def seal_effect(changed):
    area, size = effect_location(changed)
    struct.pack_into(">I", changed, area + 24, dc.crc(changed[area + 32 : area + size]))
    struct.pack_into(">I", changed, area + 28, dc.crc(changed[area : area + 28]))
    return reseal(bytes(changed))


@pytest.mark.parametrize(("gen", "test"), PACKAGES)
def test_actual_layout_and_limits(gen, test):
    raw = need_dat(gen, test)
    dat = dc.parse(raw)
    assert dc.pack(dat) == raw
    assert len(raw) == (89624 if gen == 154 else 89508 if test else 86632)
    assert len(dat.engine) == (64728 if gen == 154 else 68544 if test else 65668)
    assert tuple(dat.effect_area[16:20]) == ((8, 28, 32, 32) if gen == 154 else (0, 16, 12, 12))
    assert tuple(dat.effect_area[20:24]) == (0, 10, 9, 10)
    assert len(dat.effect_area) == (6636 if gen == 154 else 2704)
    assert dat.resident_id == raw[80:112] and dat.abi_id == raw[212:228]


@pytest.mark.parametrize("gen", (154, 155))
def test_mount_controls_follow_50mm_except_stock(gen, cameras):
    cam = cameras(gen)
    previous = cameras(153)
    for slot in range(1, 9):
        assert cam.controls(2, slot) == (previous.controls(2, 4) if slot == 4 else cam.controls(0, slot))
    assert cam.controls(2, 1) != previous.controls(2, 1)
    controls = {c["name"]: c for c in cam.controls(2, 1)}
    assert controls["amount"]["levels"] == [0, 25, 50, 75, 100, 125, 150]
    assert controls["saturation"]["max"] == 6
    with pytest.raises(EditError, match="这个档位不能用"):
        cam.resolve(RecipeSel("l08firm", 1, levels=(("saturation", 7),)))


@pytest.mark.parametrize(("gen", "test"), PACKAGES)
@pytest.mark.parametrize("unit", range(3), ids=UNITS)
def test_builtin_and_look_combinations_match_camera(gen, test, unit, cameras, references):
    raw, cam, ref = need_dat(gen, test), cameras(gen, test), references(gen)
    for info in cam.listing()[unit]["recipes"]:
        sel = RecipeSel(UNITS[unit], info["slot"])
        assert cam.resolve(sel).entry == ref.builtin(raw, unit, info["slot"], {})
        for control in info["controls"]:
            for level in control["levels"]:
                levels = {control["name"]: level}
                assert cam.resolve(replace(sel, levels=tuple(levels.items()))).entry == ref.builtin(raw, unit, info["slot"], levels)
    for index, info in enumerate(cam.listing()[unit]["looks"]):
        sel = RecipeSel(UNITS[unit], info["slot"], kind="look", id=info["id"])
        for edge in ("min", "max"):
            levels = {c["name"]: c[edge] for c in info["controls"]}
            assert cam.resolve(replace(sel, levels=tuple(levels.items()))).entry == ref.resolve(raw, unit, index, levels)


@pytest.mark.parametrize("gen", (154, 155))
@pytest.mark.parametrize("count", (0, 30, 31, 32))
def test_flat_list_user_limit_matches_camera(gen, count, references):
    dat = dc.parse(need_dat(gen))
    users = [{"base": 5, "levels": [0] * 11} for _ in range(count)]
    # 直接构造文件，以核对读入时的边界，所有变体只在内存中存在。
    raw = bytearray(need_dat(gen))
    ext_at = dc.u32(raw, 204) + dc.LEGACY_BYTES
    extension = dc._extension(dat.ext_config, [b"".join(m.records) for m in dat.modules], users)
    raw[ext_at : ext_at + dc.EXT_BYTES] = extension
    changed = reseal(bytes(raw))
    accepted = count <= 30
    for unit in range(3):
        assert references(gen).validate(changed, unit) is accepted
    if accepted:
        assert len(dc.parse(changed).users) == count
        assert dc.pack(dc.replace_users(dat, users)) == changed
    else:
        with pytest.raises(dc.DatError, match="总数超限"):
            dc.parse(changed)
        with pytest.raises(dc.DatError, match="总数超限"):
            dc.replace_users(dat, users)


@pytest.mark.parametrize(("gen", "test"), PACKAGES)
@pytest.mark.parametrize(
    "damage",
    ("effect_crc", "effect_capacity", "effect_used", "effect_mask", "effect_index", "effect_padding", "pool_offset", "knob_table", "code_size"),
)
def test_inner_layout_damage_is_rejected_with_valid_outer_crc(gen, test, damage):
    raw = bytearray(need_dat(gen, test))
    area, _ = effect_location(raw)
    if damage == "effect_crc":
        raw[area + 24] ^= 1
    elif damage == "effect_capacity":
        raw[area + 17] = 128
    elif damage == "effect_used":
        raw[area + 21] = raw[area + 17] + 1
    elif damage == "effect_mask":
        raw[area + 32 + 18] = 0
    elif damage == "effect_index":
        raw[area + 32 + 8] = 128
    elif damage == "effect_padding":
        raw[-1 - 7904 - dc.EXT_BYTES - dc.LEGACY_BYTES] ^= 1
    elif damage == "pool_offset":
        pool = dc.u32(raw, 192)
        struct.pack_into(">I", raw, pool + 64, dc.u32(raw, pool + 64) + 1)
    elif damage == "knob_table":
        raw[-1] ^= 1
    elif damage == "code_size":
        struct.pack_into(">I", raw, 136, dc.PROFILES[gen].max_engine + 4)
    changed = seal_effect(raw) if damage.startswith("effect_") and damage != "effect_crc" else reseal(bytes(raw))
    with pytest.raises(dc.DatError):
        dc.parse(changed)


@pytest.mark.parametrize(("gen", "test"), PACKAGES)
def test_failed_upload_keeps_new_generation_data(client, settings, gen, test):
    raw = need_dat(gen, test)
    assert client.post("/api/camdata/import-upload", content=raw).status_code == 200
    before = tree(settings.home_dir)
    catalog = client.get("/api/recipes").json()
    version = client.get("/api/camdata/status").json()["data_version"]
    changed = bytearray(raw)
    struct.pack_into(">I", changed, 28, 156)
    for broken, code in ((raw[:-1], "dat_invalid"), (reseal(bytes(changed)), "generation_unsupported")):
        response = client.post("/api/camdata/import-upload", content=broken)
        assert response.status_code == 422 and response.json()["error"] == code
        assert tree(settings.home_dir) == before
        assert client.get("/api/recipes").json() == catalog
        assert client.get("/api/camdata/status").json()["data_version"] == version


def test_import_versions_refresh_recipe_catalog(client):
    versions = set()
    for gen, test in ((153, False), (154, True), (155, False), (155, True)):
        raw = need_dat(gen, test)
        assert client.post("/api/camdata/import-upload", content=raw).status_code == 200
        state = client.get("/api/camdata/status").json()
        assert state["camera_data"]["generation"] == gen
        assert state["camera_data"]["source"]["sha256"] == hashlib.sha256(raw).hexdigest()
        assert state["data_version"] not in versions
        versions.add(state["data_version"])
        for unit in client.get("/api/recipes").json()["units"]:
            assert [i["name"] for i in unit["looks"]] == list(LOOK_NAMES)
            assert [i["name"] for i in unit["users"]] == (["FCC-001", "FCC*002", "GRN*001"] if test else [])
            if unit["id"] == "l08firm":
                controls = {c["name"]: c for c in unit["recipes"][0]["controls"]}
                assert controls["amount"]["amount_step"] == (20 if gen == 153 else 25)
                assert controls["saturation"]["max"] == (8 if gen == 153 else 6)
