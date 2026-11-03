"""150/152/153/154/155 只读导入、文件内配方、完整整数行和面板状态。"""

from __future__ import annotations

import hashlib
import io
import json
import struct
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import PORT
from g152_support import ALL_PACKAGES, LOOK_NAMES, ROOT, UNITS, CameraReference, need_dat
from gxr_studio.app import create_app
from gxr_studio.camdata import calib, knob_table, store
from gxr_studio.camdata import datcodec as dc
from gxr_studio.engine import menu
from gxr_studio.render.params import EditError, RecipeSel, parse_recipe
from gxr_studio.render.recipes import CameraData
from test_api import import_all
from test_datcodec import reseal


@pytest.fixture(scope="module")
def cameras(tmp_path_factory):
    cache = {}

    def get(gen, test=False):
        key = (gen, test)
        if key not in cache:
            raw = need_dat(gen, test)
            home = tmp_path_factory.mktemp(f"camera-{gen}-{test}")
            store.import_from_dat(raw, home)
            cache[key] = CameraData(home)
        return cache[key]

    return get


@pytest.fixture(scope="module")
def references(tmp_path_factory):
    cache = {}

    def get(gen):
        if gen not in cache:
            cache[gen] = CameraReference(gen, tmp_path_factory.mktemp(f"reference-{gen}"))
        return cache[gen]

    return get


@pytest.mark.parametrize(("gen", "test"), ALL_PACKAGES)
def test_new_packages_parse_and_round_trip(gen, test):
    raw = need_dat(gen, test)
    dat = dc.parse(raw)
    assert dat.generation == gen
    assert dc.pack(dat) == raw
    assert [look.name for look in dat.looks] == ([] if gen == 150 else list(LOOK_NAMES))
    assert len(dat.users) == (3 if test else 0)
    assert dat.user_names == (("FCC-001", "FCC*002", "GRN*001") if test else ())
    for unit in range(3):
        for slot in range(1, 9):
            store.check_entry(dat.entry_of(unit, slot))


@pytest.mark.parametrize("gen", (150, 152, 153, 154, 155))
def test_new_generations_require_the_dat_template(tmp_path, gen):
    home = tmp_path / "app-home"
    store.import_from_dat(need_dat(gen), home)
    root = store.camera_data_dir(home)
    manifest = store.read_manifest(home)
    manifest["files"].pop("dat-template/GXRCE.DAT")
    (root / "dat-template/GXRCE.DAT").unlink()
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        CameraData(home)


@pytest.mark.parametrize(("gen", "test"), ALL_PACKAGES)
@pytest.mark.parametrize("damage", ("header_crc", "body_crc", "empty", "short_magic", "short_header", "short_body", "last_byte"))
def test_new_packages_refuse_crc_damage_and_truncation(gen, test, damage):
    raw = need_dat(gen, test)
    variants = {"empty": b"", "short_magic": raw[:7], "short_header": raw[:255], "short_body": raw[:260], "last_byte": raw[:-1]}
    if damage in ("header_crc", "body_crc"):
        changed = bytearray(raw)
        changed[40 if damage == "header_crc" else 5000] ^= 1
        broken = bytes(changed)
    else:
        broken = variants[damage]
    with pytest.raises(dc.DatError):
        dc.parse(broken)


@pytest.mark.parametrize("generation", (0, 126, 139, 149, 151, 157, 999))
def test_unknown_generation_stays_rejected_without_local_data(generation):
    raw = bytearray(256)
    raw[:8] = b"GXRCE001"
    struct.pack_into(">I", raw, 28, generation)
    with pytest.raises(dc.UnsupportedGeneration):
        dc.parse(bytes(raw))


@pytest.mark.parametrize("test", (False, True))
def test_g150_records_and_controls_are_a_subset_of_g152(test):
    earlier, later = dc.parse(need_dat(150, test)), dc.parse(need_dat(152, test))
    assert earlier.users == later.users and earlier.ext_config == later.ext_config
    assert earlier.ids == later.ids
    assert knob_table.read(earlier.kv2_block, menu.load()) == knob_table.read(later.kv2_block, menu.load())
    for unit in range(3):
        assert earlier.modules[unit] == later.modules[unit]
        for slot in range(1, 9):
            assert earlier.entry_of(unit, slot) == later.entry_of(unit, slot)


@pytest.mark.parametrize("gen", (152, 153))
def test_r3_points_and_ranges_equal_the_existing_menu(gen, cameras):
    assert cameras(gen).menu == menu.load()


@pytest.mark.parametrize(("gen", "test"), [(g, t) for g in (152, 153, 154, 155) for t in (False, True)])
@pytest.mark.parametrize("unit", range(3), ids=UNITS)
@pytest.mark.parametrize("index", range(10), ids=LOOK_NAMES)
def test_every_look_default_has_the_camera_552_bytes(gen, test, unit, index, cameras, references):
    raw, cam = need_dat(gen, test), cameras(gen, test)
    info = cam.listing()[unit]["looks"][index]
    assert info["name"] == LOOK_NAMES[index]
    assert info["controls"] == cam.controls(unit, info["slot"])
    assert all(c["default"] == (100 if c["name"] == "amount" else 0) for c in info["controls"])
    sel = parse_recipe({"kind": "look", "id": info["id"], "unit": UNITS[unit], "slot": info["slot"]})
    got = cam.resolve(sel)
    assert got.entry == references(gen).resolve(raw, unit, index, {})
    assert got.entry_sha256 == hashlib.sha256(got.entry).hexdigest()
    assert got.entry == dc.parse(raw).looks[index].entry
    store.check_entry(got.entry)
    for c in info["controls"]:
        for invalid in (c["min"] - 1, c["max"] + 1):
            with pytest.raises(EditError, match="这个档位不能用"):
                cam.resolve(replace(sel, levels=((c["name"], invalid),)))


@pytest.mark.parametrize("gen", (152, 153, 154, 155))
@pytest.mark.parametrize("unit", range(3), ids=UNITS)
@pytest.mark.parametrize("index", range(10), ids=LOOK_NAMES)
def test_every_look_control_point_has_the_camera_552_bytes(gen, unit, index, cameras, references):
    raw, cam = need_dat(gen), cameras(gen)
    info = cam.listing()[unit]["looks"][index]
    sel = RecipeSel(UNITS[unit], info["slot"], kind="look", id=info["id"])
    for control in info["controls"]:
        for level in control["levels"]:
            levels = {control["name"]: level}
            assert cam.resolve(replace(sel, levels=tuple(levels.items()))).entry == references(gen).resolve(raw, unit, index, levels)


@pytest.mark.parametrize("gen", (152, 153, 154, 155))
@pytest.mark.parametrize("unit", range(3), ids=UNITS)
def test_file_user_entries_keep_names_defaults_and_camera_rows(gen, unit, cameras, references):
    raw, cam = need_dat(gen, True), cameras(gen, True)
    users = cam.listing()[unit]["users"]
    assert [u["name"] for u in users] == ["FCC-001", "FCC*002", "GRN*001"]
    for i, info in enumerate(users):
        selection = RecipeSel(UNITS[unit], info["slot"], kind="camera_user", id=info["id"])
        got = cam.resolve(selection)
        assert got.entry == references(gen).user(raw, unit, i)
        assert all(got.levels[c["name"]] == c["default"] for c in info["controls"])


def test_look_identity_and_black_white_controls_are_checked(cameras):
    cam = cameras(152)
    info = cam.listing()[0]["looks"][2]
    sel = RecipeSel("jlaunch", info["slot"], kind="look", id=info["id"])
    with pytest.raises(EditError) as missing:
        cam.resolve(replace(sel, id="0" * 64))
    assert missing.value.code == "recipe_not_found"
    with pytest.raises(EditError) as wrong_base:
        cam.resolve(replace(sel, slot=4))
    assert wrong_base.value.code == "recipe_not_found"
    with pytest.raises(EditError) as saturation:
        cam.resolve(replace(sel, levels=(("saturation", 1),)))
    assert saturation.value.code == "knob_not_available"


def test_effect_crc_is_checked_after_outer_crc_is_valid():
    raw = need_dat(152)
    pool = dc.u32(raw, 192)
    area = pool + dc.u32(raw, pool + 64)
    changed = bytearray(raw)
    changed[area + 32 + 20] ^= 1
    with pytest.raises(dc.DatError, match="效果区"):
        dc.parse(reseal(bytes(changed)))


def test_look_names_and_unit_visibility_come_from_the_file(tmp_path, references):
    raw = need_dat(152)
    dat = dc.parse(raw)
    pool = dc.u32(raw, 192)
    area, size = pool + dc.u32(raw, pool + 64), dc.u32(raw, pool + 68)
    changed = bytearray(raw)
    record = area + 32
    changed[record + 18] = 1
    name = b"Stored Look"
    changed[record + 20 : record + 32] = name + bytes(12 - len(name))
    rows = b"".join(dat.looks[0].entry[off : off + length] for off, length in zip(dc.OFFSETS, dc.ROW_BYTES))
    struct.pack_into(">I", changed, record + 4, dc.crc(changed[record : record + 4] + b"\1" + rows))
    struct.pack_into(">I", changed, area + 24, dc.crc(changed[area + 32 : area + size]))
    struct.pack_into(">I", changed, area + 28, dc.crc(changed[area : area + 28]))
    changed = reseal(bytes(changed))
    home = tmp_path / "app-home"
    store.import_from_dat(changed, home)
    cam = CameraData(home)
    units = cam.listing()
    assert [len(unit["looks"]) for unit in units] == [10, 9, 9]
    assert units[0]["looks"][0]["name"] == "Stored Look"
    assert units[1]["looks"][0]["name"] == "HNCS HiCon"
    item = units[0]["looks"][0]
    selection = RecipeSel("jlaunch", item["slot"], kind="look", id=item["id"])
    assert cam.resolve(selection).entry == references(152).resolve(changed, 0, 0, {})
    with pytest.raises(EditError) as invisible:
        cam.resolve(replace(selection, unit="qlaunch"))
    assert invisible.value.code == "recipe_not_found"


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        yield client


def install_calibration(settings):
    sources = calib.dev_sources(ROOT)
    if not all(p.is_file() for pair in sources.values() for p in pair):
        pytest.skip("本机没有校准数据")
    calib.import_calibration(settings.home_dir, sources)


@pytest.mark.parametrize("state", ("missing", "unsupported", "no_raw", "ready", "calibration_missing"))
def test_panel_api_gives_the_true_reason(client, card, settings, state):
    import_all(client)
    photos = client.get("/api/photos").json()
    photo = next(p for p in photos if p["has_raw"] == (state != "no_raw"))
    if state in ("no_raw", "ready", "calibration_missing"):
        assert client.post("/api/camdata/import-upload", content=need_dat(152, True)).status_code == 200
    if state in ("no_raw", "ready"):
        install_calibration(settings)
    if state == "unsupported":
        changed = bytearray(need_dat(152))
        struct.pack_into(">I", changed, 28, 157)
        response = client.post("/api/camdata/import-upload", content=reseal(bytes(changed)))
        assert response.status_code == 422
        assert response.json()["message"] == "这个相机配方文件需要更新版本的 GXR 工作室。"
        assert response.json()["detail"] == "第 157 代。支持第 127、129、131、135、137、138、150、152、153、154、155、156 代。"
        assert "supported_generations" not in response.json()
        camera = client.get("/api/camdata/status").json()["camera_data"]
        assert camera["state"] == "unsupported" and camera["generation"] is None
        assert camera["detail"] == response.json()["detail"]
    caps = client.get(f"/api/photos/{photo['id']}/capabilities").json()
    expected = {
        "missing": "还没有导入相机配方文件",
        "unsupported": dc.unsupported_message(157),
        "no_raw": "配方需要 RAW 文件",
        "ready": "",
        "calibration_missing": "请先导入校准数据",
    }
    assert caps["reason"] == expected[state]
    assert caps["raw"] == (state != "no_raw")
    assert caps["recipe"] == ("full" if state == "ready" else "none")


@pytest.mark.parametrize(("gen", "test"), ALL_PACKAGES)
def test_import_api_lists_file_entries_in_order(client, gen, test):
    raw = need_dat(gen, test)
    assert client.post("/api/camdata/import-upload", content=raw).status_code == 200
    units = client.get("/api/recipes").json()["units"]
    for i, info in enumerate(units):
        assert [r["slot"] for r in info["recipes"]] == list(range(1, 9))
        assert [r["name"] for r in info["looks"]] == ([] if gen == 150 else list(LOOK_NAMES))
        assert [r["name"] for r in info["users"]] == (["FCC-001", "FCC*002", "GRN*001"] if test else [])
        assert info["calibration_note"] == (None if i == 0 else "未校准")


def test_all_file_selections_save_render_and_export(client, real_card, settings):
    import_all(client)
    assert client.post("/api/camdata/import-upload", content=need_dat(152, True)).status_code == 200
    install_calibration(settings)
    photos = client.get("/api/photos").json()
    photo = next(p for p in photos if p["name"] == "R8051144")
    path = f"/api/photos/{photo['id']}"
    unit = client.get(f"{path}/edit").json()["render"]["unit"]
    info = next(u for u in client.get("/api/recipes").json()["units"] if u["id"] == unit)
    outputs = set()
    for item in [*info["looks"], *info["users"]]:
        recipe = {"kind": item["kind"], "id": item["id"], "unit": unit, "slot": item["slot"]}
        saved = client.put(f"{path}/edit", json={"recipe": recipe})
        assert saved.status_code == 200, saved.text
        assert saved.json()["recipe"] == {**recipe, "levels": {}}
        assert client.get(f"{path}/edit").json()["recipe"] == saved.json()["recipe"]
        rendered = client.get(f"{path}/render", params={"max_edge": 160, "stage": "final"})
        assert rendered.status_code == 200, rendered.text[:200]
        assert rendered.headers["X-GXR-Prediction"] == "1"
        image = Image.open(io.BytesIO(rendered.content))
        assert max(image.size) == 160
        outputs.add(hashlib.sha256(rendered.content).hexdigest())
    assert len(outputs) >= 10
    exported = client.post(f"{path}/export", json={"long_edge": 160, "quality": 90})
    assert exported.status_code == 200, exported.text
