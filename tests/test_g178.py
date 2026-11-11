"""双文件只读导入及全部 69 份完整记录对实际相机 C 的逐字节验证。"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import replace

import pytest

import e0_support as t
from g178_support import FullRecordReference, need_bundle
from gxr_studio.camdata import datcodec as dc
from gxr_studio.camdata import intake, store
from gxr_studio.render.params import EditError, RecipeSel, parse_recipe
from gxr_studio.render.recipes import CameraData
from test_camdata_api import client as client
from test_camdata_api import reader as reader
from test_camdata_api import tree
from test_datcodec import reseal

NAMES = (
    "F-CC",
    "F-CN",
    "Provia100F",
    "M9 STD",
    "L-Classic",
    "L-Contemp",
    "Leica F1",
    "K64",
    "K25",
    "Portra160",
    "Gold200",
    "Ektachrm100",
    "HNCS HiCon",
    "HNCS LowSat",
    "Teal Orange",
    "Y2K Plady",
    "Agfa LeBox",
    "Acros",
    "Tri-X 400",
)


@pytest.fixture(scope="module", params=(178, 181, 182))
def camera(tmp_path_factory, request):
    raw, library = need_bundle(request.param)
    home = tmp_path_factory.mktemp(f"g{request.param}-camera")
    store.import_from_dat(raw, home, library=library)
    return CameraData(home)


@pytest.fixture(scope="module")
def reference(tmp_path_factory):
    return FullRecordReference(tmp_path_factory.mktemp("g178-c-reference"))


@pytest.mark.parametrize("generation", (176, 178, 181, 182))
def test_two_file_decode_and_round_trip(generation):
    raw, library = need_bundle(generation)
    dat = dc.parse(raw, library)
    assert dat.generation == generation and dc.pack(dat) == raw and dat.library == library
    assert len(dat.recipes) == (29 if generation == 176 else 57)
    assert all(len(m.entries) == 8 for m in dat.modules)
    assert intake.inspect_dat(raw, library).generation == generation


@pytest.mark.parametrize("generation", (176, 178, 181, 182))
@pytest.mark.parametrize("damage", ("missing", "other_generation", "truncated", "changed", "trailing"))
def test_missing_or_mismatched_library_has_a_clear_error(generation, damage):
    raw, library = need_bundle(generation)
    bad = {
        "missing": None,
        "other_generation": need_bundle(178 if generation == 176 else 176)[1],
        "truncated": library[:-1],
        "changed": library[:100] + bytes([library[100] ^ 1]) + library[101:],
        "trailing": library + b"x",
    }[damage]
    with pytest.raises(intake.IntakeError, match="GXRFX.BIN") as rejected:
        intake.inspect_dat(raw, bad)
    assert rejected.value.code == "dat_invalid"


@pytest.mark.parametrize("generation", (176, 178, 181, 182))
@pytest.mark.parametrize("damage", ("engine", "entry", "bss", "relocation", "label"))
def test_every_engine_field_and_label_is_bound(generation, damage):
    raw, library = need_bundle(generation)
    changed = bytearray(raw)
    offsets = {"engine": 300, "entry": 115, "bss": 119, "relocation": dc.u32(raw, 144) + 7, "label": 31}
    changed[offsets[damage]] ^= 1
    with pytest.raises(dc.DatError):
        dc.parse(reseal(bytes(changed)), library)
    dat = dc.parse(raw, library)
    with pytest.raises(dc.DatError, match="只读"):
        dc.pack(replace(dat, recipes=()))


@pytest.mark.parametrize("unit", range(3))
def test_each_unit_lists_23_full_records_in_camera_order(camera, unit):
    listing = camera.listing()[unit]
    assert len(listing["recipes"]) == 23
    assert [r["slot"] for r in listing["recipes"][:4]] == [1, 2, 3, 4]
    assert tuple(r["name"] for r in listing["recipes"][4:]) == NAMES
    assert listing["looks"] == [] and listing["users"] == []
    acros = listing["recipes"][21]
    assert [c["name"] for c in acros["controls"]] == ["contrast", "highlight", "shadow", "fade", "filter"]


@pytest.mark.parametrize("unit", range(3))
@pytest.mark.parametrize("position", range(1, 24))
def test_all_69_records_and_control_endpoints_match_camera_c(camera, reference, unit, position, record_property):
    info = camera.listing()[unit]["recipes"][position - 1]
    identity = info.get("id")
    sel = RecipeSel(dc.NAMES[unit], info["slot"], kind=info.get("kind", "builtin"), id=identity)
    catalogue = store.load_library(camera.home)[64:]
    vectors = [{}]
    for control in info["controls"]:
        vectors.extend({control["name"]: value} for value in {control["min"], control["max"], control["default"]})
    record_property("unit", dc.NAMES[unit])
    record_property("position", position)
    record_property("vectors", len(vectors))
    for levels in vectors:
        ours = camera.resolve(replace(sel, levels=tuple(levels.items()))).entry
        expected = reference.resolve(catalogue, unit, position, levels)
        differences = [i for i, (a, b) in enumerate(zip(ours, expected)) if a != b]
        assert ours == expected, (unit, position, levels, "552 B integer engine", differences[:20], len(differences))


def test_all_69_default_records_equal_the_independent_input_registry(camera):
    path = t.REPO / "research/fit_v4_r2/local/registry/all_full_registry.json"
    if not path.is_file():
        pytest.skip("g178 独立配方登记表不在本机")
    registry = json.loads(path.read_text())["records"]
    assert len(registry) == 69
    for row in registry:
        info = camera.listing()[row["unit_index"]]["recipes"][row["position"] - 1]
        sel = RecipeSel(dc.NAMES[row["unit_index"]], info["slot"], kind=info.get("kind", "builtin"), id=info.get("id"))
        record = (t.REPO / row["record_file"]).read_bytes()
        assert hashlib.sha256(record).hexdigest() == row["record_sha256"]
        assert camera.resolve(sel).entry == record, (row["unit_index"], row["position"])


def test_removed_native_entries_and_foreign_record_identity_are_rejected(camera):
    for slot in range(5, 9):
        with pytest.raises(EditError, match=f"g{camera.manifest['generation']}"):
            camera.resolve(RecipeSel("jlaunch", slot))
    info = camera.listing()[0]["recipes"][4]
    with pytest.raises(EditError):
        camera.resolve(RecipeSel("qlaunch", info["slot"], kind="record", id=info["id"]))


@pytest.mark.parametrize("source", ("file", "directory", "upload", "fake_card"))
@pytest.mark.parametrize("generation", (178, 181, 182))
def test_import_reads_both_files_and_keeps_sources_unchanged(source, generation, client, settings, volumes, tmp_path):
    raw, library = need_bundle(generation)
    folder = (volumes if source == "fake_card" else tmp_path) / "copy"
    folder.mkdir()
    (folder / "GXRCE.DAT").write_bytes(raw)
    (folder / "GXRFX.BIN").write_bytes(library)
    if source == "upload":
        response = client.post("/api/camdata/import-upload", content=raw + library, headers={"X-GXR-Dat-Bytes": str(len(raw))})
    elif source == "fake_card":
        response = client.post("/api/camdata/import-card", json={})
    else:
        response = client.post("/api/camdata/import-file", json={"path": str(folder / "GXRCE.DAT" if source == "file" else folder)})
    assert response.status_code == 200, response.text
    assert response.json()["generation"] == generation and "23" in response.json()["message"]
    status = client.get("/api/render/status").json()
    assert status["camera_data"]["state"] == "ready" and status["camera_data"]["generation"] == generation
    menu = client.get("/api/recipes").json()
    assert [len(unit["recipes"]) for unit in menu["units"]] == [23, 23, 23]
    assert store.load_template(settings.home_dir) == raw and store.load_library(settings.home_dir) == library
    assert (folder / "GXRCE.DAT").read_bytes() == raw and (folder / "GXRFX.BIN").read_bytes() == library
    before = tree(settings.home_dir)
    rejected = client.post("/api/camdata/import-upload", content=raw)
    assert rejected.status_code == 422 and "GXRFX.BIN" in rejected.json()["message"]
    assert tree(settings.home_dir) == before


def test_unregistered_generations_name_the_supported_ones():
    raw = bytearray(256)
    raw[:8] = b"GXRCE001"
    struct.pack_into(">I", raw, 28, 177)
    with pytest.raises(intake.IntakeError) as rejected:
        intake.inspect_dat(bytes(raw))
    assert "不支持" in rejected.value.message
    assert "176" in rejected.value.extra["detail"] and "178" in rejected.value.extra["detail"]


def test_record_selection_keeps_its_content_identity():
    selection = parse_recipe({"kind": "record", "unit": "qlaunch", "slot": 6, "id": "a" * 64, "levels": {"amount": 100}})
    assert selection == RecipeSel("qlaunch", 6, (("amount", 100),), "record", "a" * 64)
    with pytest.raises(EditError):
        parse_recipe({"kind": "record", "unit": "qlaunch", "slot": 6})
