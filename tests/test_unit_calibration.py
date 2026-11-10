"""逐单元校准的公开回归只用合成条目、合成矩阵和合成色环。"""

from __future__ import annotations

import hashlib
import io
import json
import logging
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import PORT
from gxr_studio.app import create_app
from gxr_studio.camdata import calib, intake, store
from gxr_studio.engine import colour, knobs, matrix_model
from gxr_studio.lut import service as lut_service
from gxr_studio.lut import worker
from gxr_studio.lut.fit import Fitter
from gxr_studio.lut.scene import Samples
from gxr_studio.render import chain, tones
from gxr_studio.render.params import EditState, RecipeSel
from gxr_studio.render.recipes import CameraData
from gxr_studio.render.service import RenderService, RenderUnavailable
from test_clone_coverage import _install_camera, _render_entry
from test_lut import wait
from test_lut_library import cube_bytes, upload


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as api_client:
        yield api_client


def sources(home):
    result = {}
    for index, unit in enumerate(store.UNITS):
        stock = calib.stock_words_of_entry(store.load_stock04(home, unit))
        ring = json.loads((home / "calib-src/ring_200.json").read_text())
        ring["nodes_unwrapped_deg"] = [i * 15.0 + index * 3 for i in range(24)]
        coefficients = np.zeros((10, 3))
        coefficients[1, 0] = index / 50
        coefficients[4, 2] = (index + 1) / 100
        d0, d1 = np.zeros(24), np.zeros(24)
        context = io.BytesIO()
        np.savez(context, stock=stock, d0=d0, d1=d1, S_eff=((stock[:, 0] + d0) - 1j * (stock[:, 1] + d1)) / 32, input_coefficients=coefficients)
        result[unit] = {iso: (("ctx.npz", context.getvalue()), ("ring.json", json.dumps(ring).encode())) for iso in (200, 1600)}
    return result


@pytest.fixture
def installed(settings):
    _install_camera(settings.home_dir, _render_entry())
    data = sources(settings.home_dir)
    calib.import_calibration(settings.home_dir, data)
    return data


@pytest.mark.parametrize("unit", store.UNITS)
def test_own_models_and_only_measured_isos(settings, lib, installed, unit):
    render = RenderService(settings.home_dir, lib)
    lens = {"jlaunch": "50mm", "qlaunch": "28mm", "l08firm": "MOUNT"}[unit]
    for iso in (100, 200, 400, 1600, 3200):
        plan = render.plan({"lens": lens, "iso": iso}, EditState(RecipeSel(unit, 1)))
        assert plan.calibrated == (iso in ((200, 1600) if unit == "jlaunch" else (200,)))
        assert plan.model_iso == (1600 if iso >= 566 else 200)
    status = render.status()["calibration"]["units"][unit]
    assert status["state"] == "ready"
    assert status["isos"] == ([200, 1600] if unit == "jlaunch" else [200])
    assert status["exposure_note"] == (None if unit == "jlaunch" else "曝光沿用 50mm")
    other = render.model(200, "qlaunch")
    assert other is not render.model(200, "jlaunch")
    x = np.array([[123.0, 600.0, 1100.0], [700.0, 800.0, 300.0]])
    assert not np.array_equal(other.predict(_render_entry(), x), render.model(200).predict(_render_entry(), x))


def test_partial_pack_and_tampering_disable_only_the_affected_unit(settings, lib, installed):
    home = settings.home_dir
    calib.import_calibration(home, {u: installed[u] for u in ("jlaunch", "qlaunch")})
    render = RenderService(home, lib)
    assert render.available("jlaunch") and not render.available("l08firm")
    assert render.status()["calibration"]["units"]["l08firm"]["state"] == "missing"
    with pytest.raises(RenderUnavailable):
        render.plan({"lens": "MOUNT", "iso": 200}, EditState())
    render.model(200, "qlaunch")
    (home / "calib/ring_qlaunch_200.json").write_text("{}")
    assert not render.available("qlaunch") and render.available("jlaunch")
    assert render.status()["calibration"]["units"]["qlaunch"]["state"] == "invalid"
    assert next(u for u in render.catalog() if u["id"] == "jlaunch")["calibrated"]
    (home / "calib/ctx_jlaunch_200.npz").unlink()
    assert render.status()["calibration"]["units"]["jlaunch"]["state"] == "invalid"


def test_import_checks_each_units_own_stock_and_preserves_the_old_pack(settings, installed):
    home = settings.home_dir
    before = (home / "calib/manifest.json").read_bytes()
    with np.load(io.BytesIO(installed["qlaunch"][200][0][1]), allow_pickle=False) as z:
        data = dict(z)
    data["stock"][0, 0] += 1
    forged = io.BytesIO()
    np.savez(forged, **data)
    installed["qlaunch"][200] = (("forged.npz", forged.getvalue()), installed["qlaunch"][200][1])
    with pytest.raises(calib.StockMismatch):
        calib.import_calibration(home, installed)
    assert (home / "calib/manifest.json").read_bytes() == before


def test_pack_round_trip_and_old_schema_require_reimport(settings, installed, tmp_path):
    pack = tmp_path / "pack"
    manifest = intake.write_pack(pack, installed)
    assert manifest["schema"] == "gxr-studio/calib-pack/2"
    assert set(manifest["units"]) == set(store.UNITS)
    _, kind = intake.import_calibration_folder(settings.home_dir, pack)
    assert kind == "pack"
    manifest["schema"] = "gxr-studio/calib-pack/1"
    (pack / "calib-pack.json").write_text(json.dumps(manifest))
    with pytest.raises(intake.IntakeError, match="不匹配"):
        intake.import_calibration_folder(settings.home_dir, pack)
    p = settings.home_dir / "calib/manifest.json"
    data = json.loads(p.read_text())
    data["schema"] = "gxr-studio/calib/1"
    p.write_text(json.dumps(data))
    assert calib.unit_status(settings.home_dir, "jlaunch")["state"] == "invalid"


@pytest.mark.parametrize("unit", store.UNITS)
def test_baked_path_keeps_input_mapping_and_factory_lookup(settings, installed, unit):
    model = calib.model_for(settings.home_dir, 200, unit=unit)[0]
    raw = bytearray(_render_entry())
    # Rotate the recipe chroma so looking up by its own hue gives a different answer.
    row = matrix_model.MatrixRow.from_bytes(raw[216:244])
    coefficients = row.effective(100.0) * 1024
    coefficients[[1, 2]] = coefficients[[2, 1]]
    words = [matrix_model.encode_coef(int(v)) for v in coefficients.reshape(-1)] + [0, 128, 128, 0, 0]
    import struct

    struct.pack_into(">14H", raw, 216, *words)
    x = np.random.default_rng(44).uniform(100, 1800, (200, 3))
    entry = bytes(raw)
    exact = model.predict(entry, x)
    baked = chain.bake_chroma(model, entry, step=0.5, workers=1)
    approx = chain.chroma_float(x, baked, None, None, workers=1)
    assert np.percentile(colour.delta_e00(colour.lab_from_codes(approx), colour.lab_from_codes(exact)), 99) <= 0.3
    assert baked.lookup_matrix is not None
    assert not np.array_equal(baked.matrix, baked.lookup_matrix)
    assert baked.input_coefficients.shape == (10, 3)


def test_display_curve_rounds_in_float32_before_8_bit_output():
    table = np.full(tones.TABLE_SIZE, 10.5, np.float32)
    table[0] = np.nextafter(np.float32(10.5), np.float32(0))
    mapped = tones.apply_table(np.full((1, 3), 0.125, np.float32), table)
    assert mapped.dtype == np.float32
    np.testing.assert_array_equal(mapped, [[10.5, 10.5, 10.5]])
    np.testing.assert_array_equal(chain.quantize(mapped), [[11, 11, 11]])


def test_fitter_resolves_the_selected_units_controls_and_stock(settings, installed):
    cam = CameraData(settings.home_dir)
    x = np.repeat(np.arange(100, 200)[:, None], 3, axis=1)
    samples = Samples(x, np.zeros((len(x), 3)), np.ones(len(x)), np.zeros(len(x), np.int8), np.arange(len(x)) % 4 == 0, [])
    for unit in store.UNITS:
        model = calib.model_for(settings.home_dir, unit=unit)[0]
        stock = cam.stock[store.UNITS.index(unit)]
        rgb = model.predict(stock, x)
        fitter = Fitter(cam, model, samples, rgb, rgb, unit=unit, profile="quick")
        assert fitter.entry_of(4, fitter.defaults(4)) == cam.resolve(RecipeSel(unit, 4)).entry
        assert fitter.controls(3) == [
            c
            for name in ("amount", "contrast", "highlight", "shadow", "fade", "saturation", "warmth", "tint", "split", "filter")
            for c in cam.controls(store.UNITS.index(unit), 3)
            if c["name"] == name
        ]


def test_lut_capability_is_the_imported_state(client, settings, installed):
    units = client.get("/api/lut/spaces").json()["units"]
    assert all(u["fit_supported"] for u in units)
    assert all(u["calibration"]["isos"] == ([200, 1600] if u["id"] == "jlaunch" else [200]) for u in units)
    (settings.home_dir / "calib/ring_l08firm_200.json").write_text("{}")
    units = {u["id"]: u for u in client.get("/api/lut/spaces").json()["units"]}
    assert units["jlaunch"]["fit_supported"] and not units["l08firm"]["fit_supported"]


@pytest.mark.parametrize("unit", ("qlaunch", "l08firm"))
@pytest.mark.parametrize("finish_during_poll", [False, True], ids=["normal-poll", "finish-during-poll"])
def test_calibrated_units_finish_a_real_fit_and_save_their_own_recipe(client, settings, installed, unit, finish_during_poll, monkeypatch):
    raw = cube_bytes(lambda x: x * [0.95, 1.0, 0.9], size=5, comments="# Input: sRGB\n")
    uploaded = upload(client, raw)
    assert uploaded.status_code == 200, uploaded.text
    started = client.post("/api/lut/fit", json={"cube_id": uploaded.json()["cube_id"], "unit": unit, "profile": "quick", "space": "srgb"})
    assert started.status_code == 200, started.text
    job = started.json()["job_id"]
    if finish_during_poll:
        service = client.app.state.ctx.lut
        read = service._read
        result_path = service.jobs_dir / job / "result.json"

        def finish_after_read(path):
            doc = read(path)
            if path == result_path and doc is None:
                # Publish the real, exact fit after this read but before status can observe its exit.
                service._procs[job].wait(timeout=240)
            return doc

        monkeypatch.setattr(service, "_read", finish_after_read)
    result = wait(client, job)
    assert result["state"] == "done", json.dumps(result, ensure_ascii=False, indent=2)
    assert result["unit"] == unit
    candidates = result["result"]["candidates"]
    assert candidates and all(candidate["unit"] == unit for candidate in candidates)
    cam = CameraData(settings.home_dir)
    for candidate in candidates:
        selection = RecipeSel(unit, candidate["slot"], tuple(sorted(candidate["levels"].items())))
        assert cam.resolve(selection).entry_sha256 == candidate["entry_sha256"]
    saved = client.post("/api/my-recipes", json={"job_id": job, "candidate": 0, "name": "synthetic per-unit"})
    assert saved.status_code == 200, saved.text
    assert saved.json()["recipe"]["unit"] == unit


def test_refinement_exception_has_a_readable_status_and_one_redacted_alert(client, settings, installed, monkeypatch, caplog):
    process = SimpleNamespace(poll=lambda: 1)
    monkeypatch.setattr(lut_service.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(worker, "watch_parent", lambda *args: None)
    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)
    logger = logging.getLogger("gxr_studio")
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    monkeypatch.setattr(logger, "propagate", False)

    def fail_refinement(self):
        raise ArithmeticError("injected refinement failure token=private-value")

    monkeypatch.setattr(Fitter, "run", fail_refinement)
    item = upload(client, cube_bytes(lambda x: x, size=2)).json()
    started = client.post("/api/lut/fit", json={"cube_id": item["cube_id"], "unit": "qlaunch", "profile": "quick"})
    assert started.status_code == 200, started.text
    job_id = started.json()["job_id"]
    directory = settings.home_dir / "lut_jobs" / job_id
    assert worker.run(directory) == 1
    private_result = json.loads((directory / "result.json").read_text())
    assert "ArithmeticError: injected refinement failure" in private_result["trace"]
    with caplog.at_level(logging.ERROR, logger="gxr_studio"):
        response = client.get(f"/api/lut/fit/{job_id}")
        client.get(f"/api/lut/fit/{job_id}")
    assert response.json()["state"] == "error", response.text
    assert response.json()["error"] == {
        "code": "fit_failed",
        "message": "配方计算失败，未生成可保存的结果。请查看工作室日志。",
    }
    assert "trace" not in response.text and "private-value" not in response.text
    alerts = client.app.state.ctx.alerts.recent()
    assert len(alerts) == 1
    assert alerts[0]["handler"] == "on_job_failed" and alerts[0]["job_id"] == job_id
    events = [json.loads(record.message) for record in caplog.records]
    assert len(events) == 1
    assert "ArithmeticError: injected refinement failure" in events[0]["error"]
    assert "private-value" not in events[0]["error"]


def test_units_with_different_stock_rows_import_without_using_50mm(settings):
    home = settings.home_dir
    _install_camera(home, _render_entry())
    root = store.camera_data_dir(home)
    manifest = json.loads((root / "manifest.json").read_text())
    row = bytearray(store.load_stock04(home, "qlaunch"))
    row[115:119] = colour.encode_word(17, 2)
    knobs.checksum(row)
    name = "units/qlaunch/stock04.entry"
    (root / name).write_bytes(row)
    manifest["files"][name] = {"size": len(row), "sha256": hashlib.sha256(row).hexdigest()}
    (root / "manifest.json").write_text(json.dumps(manifest))
    calib.import_calibration(home, sources(home))
    a, b = (calib.model_for(home, unit=u)[0] for u in ("jlaunch", "qlaunch"))
    assert a.engine.ctx.stock[0, 0] != b.engine.ctx.stock[0, 0]
    np.testing.assert_array_equal(b.engine.ctx.stock[0], [17, 2])


def test_render_cache_separates_units_even_when_recipe_bytes_are_shared(settings, lib, installed):
    render = RenderService(settings.home_dir, lib)
    shot = {"lens": "50mm", "iso": 200, "files": {"dng": {"sha256": "a" * 64}}}
    one = render.plan(shot, EditState(RecipeSel("jlaunch", 1)))
    two = render.plan(shot, EditState(RecipeSel("qlaunch", 1)))
    assert one.entry_sha256 == two.entry_sha256
    assert render.render_key(shot, EditState(), one, max_edge=100, exact=True, crop=None) != render.render_key(
        shot, EditState(), two, max_edge=100, exact=True, crop=None
    )


def test_dev_import_reads_unit_native_nodes_and_the_frozen_input_maps(settings, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from gxr_studio.camdata import datcodec
    from gxr_studio.camdata.dev_calib import FIRMWARE, UNITS

    root = tmp_path / "synthetic-research"
    entry = _render_entry()
    _install_camera(settings.home_dir, entry)

    def put(relative, data):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    put("research/fit_v4_r2/local/adapter/local_batch/workspace.json", {"model_version": "native-C040-v2.0"})
    official = {}
    row = entry[115:211]
    for index, key in enumerate(("50mm", "28mm", "mount")):
        official[key] = {
            "rows": {"colour1": {"file_offset": 1400, "hex": row.hex()}},
            "mask": [(1 << 24) - 1] * 5,
            "d0_levels": np.zeros((17, 24)).tolist(),
            "d1_levels": np.zeros((17, 24)).tolist(),
            "vividness_levels": [100] * 17,
            "sat_levels": [100] * 17,
        }
        firmware = bytearray(1600)
        firmware[1400:1496] = row
        firmware[152:200:2] = bytes(i * 10 + index for i in range(24))
        firmware[153:200:2] = bytes([64]) * 24
        path = root / "artifacts/firmware/gxr-1.51" / FIRMWARE[index]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(firmware)
        terms = 10 if index == 0 else 4
        coefficients = np.zeros((terms, 3))
        coefficients[1, 0] = index / 100
        put(f"research/fit_v4_r2/local/adapter/local_batch/calibration/input-map-{UNITS[index]}.json", {"coefficients": coefficients.tolist()})
    put("research/module_adapt/local/official_parameters.json", {"units": official})
    display = {"vividness": 1}
    for color in ("orange", "green", "blue", "red", "magenta"):
        display[color + "_hue"] = display[color + "_saturation"] = 1
    put("research/stage2e_verification/local/measurements.json", {"frames": {"R8051160": {"meta": {"display": display}}}})
    ring = json.loads((settings.home_dir / "calib-src/ring_200.json").read_text())
    ring["nodes_deg"] = ring["sector_centres_deg"] = ring["nodes_unwrapped_deg"]
    for name in ("ring_model_v2.json", "ring_model_v2_iso1600.json"):
        put("research/hue_ring/local/" + name, ring)
    path = root / "firmware/package_v39_g154_looks18_test/local/SD_ROOT/GXRCE.DAT"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"synthetic-DAT")
    monkeypatch.setattr(datcodec, "parse", lambda _: SimpleNamespace(entry_of=lambda _unit, _slot: entry))
    generated = calib.dev_sources(root)
    calib.import_calibration(settings.home_dir, generated)
    for unit, first_node in zip(store.UNITS, (0.0, 1.40625, 2.8125)):
        model, _ = calib.model_for(settings.home_dir, unit=unit)
        assert model.engine.table.nodes[0] == first_node
        assert model.input_coefficients[1, 0] == store.UNITS.index(unit) / 100
    assert calib.dev_sources(tmp_path / "missing") == {}
    put("research/fit_v4_r2/local/adapter/local_batch/workspace.json", {"model_version": "other"})
    with pytest.raises(ValueError, match="native-C040-v2.0"):
        calib.dev_sources(root)
