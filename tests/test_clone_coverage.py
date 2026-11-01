"""干净克隆里夹具测试会跳过。这里用运行时合成的字节把解析、预览、拟合和导入的主路径跑一遍。

合成 DAT 不是理光固件：`parse` 必须在已登记的原厂 04 CRC 处拒绝它，文件也不进仓库。
"""

from __future__ import annotations

import hashlib
import json
import struct
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import PORT
from gxr_studio import audit
from gxr_studio.app import create_app
from gxr_studio.camdata import calib, intake, store
from gxr_studio.camdata import datcodec as dc
from gxr_studio.cards.discovery import Card, CardReader
from gxr_studio.crop import Crop
from gxr_studio.engine import knobs, matrix_model
from gxr_studio.engine.model import masks, recipe_chart, synthetic
from gxr_studio.export import ExportOptions, export_shot
from gxr_studio.library import thumbs
from gxr_studio.library.store import FileRecord, Library, ShotRecord, sha256_file
from gxr_studio.lut import colorimetry, worker
from gxr_studio.lut.fit import Cancelled, Fitter, explain, fit_lut, label_for
from gxr_studio.lut.scene import Samples, SceneColour, SceneError, build_samples, scene_colour, target_rgb
from gxr_studio.lut.service import LutService
from gxr_studio.meta import dng_preview_image, parse_exif_datetime, plausible, read_dng_meta, read_jpeg_meta
from gxr_studio.render.params import Adjust, EditState, RecipeSel
from gxr_studio.render.recipes import Menu
from gxr_studio.render.service import NoRaw, RenderService
from raw_support import bayer_scene, build_dng
from test_synthetic_coverage import make_entry


def _tiny_dng(w: int = 32, h: int = 32, **kw: object) -> bytes:
    yy, xx = np.mgrid[0:h, 0:w].astype(float)
    scene = np.stack([0.15 + 0.5 * xx / w, 0.2 + 0.4 * yy / h, 0.55 - 0.3 * xx / w], axis=-1)
    kw.setdefault("neutral", (0.65, 1.0, 0.58))
    return build_dng(bayer_scene(h, w, scene, white=4095), white=4095, active=(0, 0, h, w), **kw)  # type: ignore[arg-type]


def _render_entry() -> bytes:
    """552 B 合成条目，矩阵把 RGB 立方的角投成一块真的色度多边形，烘焙才不会退化。"""
    raw = bytearray(make_entry())
    coefs = (306, 601, 117, -173, -339, 512, 512, -419, -83)
    words = [matrix_model.encode_coef(v) for v in coefs] + [0, 128, 128, 0, 0]
    struct.pack_into(">14H", raw, 216, *words)
    knobs.checksum(raw)
    return bytes(raw)


def _install_camera(home: Path, entry: bytes) -> None:
    root = store.camera_data_dir(home)
    files: dict[str, bytes] = {}
    for name in dc.NAMES:
        for slot in range(1, 9):
            files[f"units/{name}/recipes/{slot:02d}.entry"] = entry
        files[f"units/{name}/stock04.entry"] = entry
    for rel, blob in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
    manifest = {
        "schema": store.SCHEMA,
        "generation": 127,
        "source": {"name": "synthetic", "size": len(entry), "sha256": hashlib.sha256(entry).hexdigest()},
        "imported_at": "2026-10-30T00:00:00",
        "files": {rel: {"size": len(blob), "sha256": hashlib.sha256(blob).hexdigest()} for rel, blob in files.items()},
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    stock = calib.stock_words_of_entry(entry)
    d0 = np.zeros(24)
    d1 = np.zeros(24)
    s_eff = ((stock[:, 0] + d0) - 1j * (stock[:, 1] + d1)) / 32
    ring = {
        "schema": "gxr-sector-model/2",
        "k_coupling": 1.0,
        "nodes_unwrapped_deg": [i * 15.0 for i in range(24)],
        "kernel": {"type": "linear", "blur_deg": 4.0, "blur_kappa": 2.0},
        "gain": 1.0,
    }
    src = home / "calib-src"
    src.mkdir(parents=True, exist_ok=True)
    sources = {}
    for iso in (200, 1600):
        npz = src / f"data_{iso}.npz"
        ring_path = src / f"ring_{iso}.json"
        np.savez(npz, stock=stock, d0=d0, d1=d1, S_eff=s_eff)
        ring_path.write_text(json.dumps(ring), encoding="utf-8")
        sources[iso] = (npz, ring_path)
    calib.import_calibration(home, sources, origin={"kind": "synthetic"})


def _commit_dng(lib: Library, blob: bytes, shot_id: str = "syn1", iso: int = 200) -> dict[str, object]:
    rel = f"2026/2026-10-16/{shot_id}.DNG"
    path = lib.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    st = path.stat()
    lib.commit_shot(
        ShotRecord(
            shot_id,
            shot_id,
            "2026/2026-10-16",
            "2026-10-16",
            "exif",
            "2026-10-16T18:51:53",
            "jlaunch",
            "GR LENS A12 50mm F2.5 MACRO",
            iso,
            1,
            32,
            32,
            "2026-10-30T00:00:00",
            [FileRecord("dng", rel, sha256_file(path), st.st_size, st.st_mtime_ns)],
        )
    )
    shot = lib.get_shot(shot_id)
    assert shot is not None
    return shot


def test_synthetic_dat_packs_then_parse_stops_at_the_factory_crc():
    """pack() 能封完；parse() 读到已登记的原厂 CRC 就必须停。合成字节对不上那个 CRC。"""
    records = []
    for slot in range(1, 17):
        rec = bytearray(64)
        rec[0] = slot
        rec[1] = 1
        struct.pack_into(">I", rec, 60, dc.crc(rec[:60]))
        records.append(bytes(rec))
    entry = bytearray(make_entry())
    entry[0] = 4
    entry[1] = 0
    struct.pack_into(">H", entry, 445, 2)
    struct.pack_into(">I", entry, 4, dc.crc(dc.identity(records[3])))
    ranges = [[(-2, 2, 0)] * 6 + [(0, 0, 0), (0, 0, 0)] for _ in range(16)]
    modules = [dc.Module(name, bytes(152), records, {4: bytes(entry)}, [4] * 16, ranges, bytes(32), 0) for name in dc.NAMES]
    users = [{"base": 1, "levels": [0] * 11}]
    dat = dc.Dat(
        127,
        0,
        256,
        0,
        bytes(32),
        bytes(16),
        bytes(64),
        [],
        modules,
        (1, 2, 3, 4, 5, 6, 0, 0),
        bytes(24 * dc.CONFIG_BYTES),
        users,
        None,
    )
    again = dc.replace_users(dat, [{"base": 2, "levels": [0] * 11}])
    assert again.users[0]["base"] == 2 and dat.users[0]["base"] == 1
    packed = dc.pack(dat)
    assert packed[:8] == b"GXRCE001" and len(packed) > dc.HEADER
    with pytest.raises(dc.DatError, match="原厂 04"):
        dc.parse(packed)


def test_render_preview_on_a_synthetic_dng(tmp_path: Path):
    home = tmp_path / "app"
    entry = _render_entry()
    _install_camera(home, entry)
    lib = Library(tmp_path / "lib").open()
    try:
        shot = _commit_dng(lib, _tiny_dng())
        render = RenderService(home, lib)
        assert render.available()
        edit = EditState(adjust=Adjust(exposure_ev=0.25, contrast=8))
        out = render.render_preview(shot, edit, max_edge=24, quality=80)
        assert out.jpeg[:2] == b"\xff\xd8" and out.cache_hit is False and out.width > 0
        again = render.render_preview(shot, edit, max_edge=24, quality=80)
        assert again.cache_hit is True
        exact = render.render_preview(shot, edit, max_edge=24, exact=True, crop=Crop("free", (0.1, 0.1, 0.8, 0.8)))
        assert exact.jpeg[:2] == b"\xff\xd8" and exact.plan.slot == 4
        other = {**shot, "lens": "GR LENS A12 28mm F2.5", "iso": 800}
        plan = render.plan(other, EditState(recipe=RecipeSel("jlaunch", 1)))
        assert plan.reasons and plan.calibrated is False
        assert render.default_look_label(shot)
        codes = render.proxy_codes(shot, 16)
        looked = render.default_look(shot, codes)
        assert looked is not None and looked.dtype == np.uint8
        assert render.thumb_variant(shot).startswith("r")
        assert thumbs.ensure(lib, shot, 160, render) is not None
        assert thumbs.look_of(shot, render, lib)["prediction"] is True
        made = render.renderer(shot, EditState())
        shown = made.display(codes[:4])
        assert shown.shape[-1] == 3
        exported = export_shot(lib, shot, None, tmp_path / "out", ExportOptions(None, 80), [], made, use_raw=True)
        assert exported.path.suffix == ".jpg" and exported.bytes > 0
        assert render.data_version() and render.engine_pin()
        assert render.replace_data(lambda: "swapped") == "swapped"
        with pytest.raises(NoRaw):
            render.render_preview({"files": {}, "lens": shot["lens"], "iso": 200}, EditState())
        table = render.model(200).engine.table
        assert "coordinate" in table.describe()
    finally:
        lib.close()


def test_fit_scene_and_colour_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    entry = _render_entry()
    home = tmp_path / "app"
    _install_camera(home, entry)
    model = calib.model_for(home, 200)[0]
    n = 48
    x = np.linspace(20, 800, n * 3, dtype=np.float64).reshape(n, 3)
    group = np.array([0] * 32 + [1] * 8 + [2] * 8, np.int8)
    hold = np.arange(n) % 5 == 0
    samples = Samples(x, None, np.ones(n), group, hold, [])
    menu = Menu()

    class Cam:
        stock = [entry]

        def controls(self, unit: int, slot: int) -> list[dict[str, object]]:
            return menu.controls(unit, slot)

        def resolve(self, sel: RecipeSel) -> object:
            names, _levels, req = menu.request_for(sel)
            out, stats = knobs.apply(entry, entry, req)
            return SimpleNamespace(entry=out, levels=dict(zip(names, _levels, strict=False)), stats=stats)

    cam = Cam()
    rgb = np.full((n, 3), 40.0)
    ideal = np.full((n, 3), 90.0)
    fitter = Fitter(cam, model, samples, rgb, ideal, profile="quick", progress=lambda p: None)
    idx = np.arange(8)
    levels, score = fitter.local(
        4,
        fitter.defaults(4),
        idx,
        "n",
        names=("contrast", "highlight", "shadow"),
        rounds=1,
        span=(0.0, 1.0),
        label="syn",
    )
    assert score >= 0 and "contrast" in levels
    assert fitter.grid_starts(4, 1)
    described = fitter.describe(4, fitter.defaults(4))
    assert described["slot"] == 4 and described["entry_sha256"]
    packed = fitter.result([described], 0.1)
    assert packed["candidates"] and label_for(95.0) == "接近" and label_for(None) is None
    notes = explain(
        np.array([[20, 10, 5], [50, -8, 2], [80, 1, 12]] * 20, float),
        np.array([[30, 0, 0], [50, 0, 0], [70, 0, 0]] * 20, float),
        np.array([0, 1, 2] * 20),
    )
    assert notes
    with pytest.raises(Cancelled):
        Fitter(cam, model, samples, rgb, ideal, profile="quick", cancelled=lambda: True).loss(4, fitter.defaults(4), idx, "c")
    monkeypatch.setattr(Fitter, "run", lambda self: {"delegated": True})
    assert fit_lut(cam, model, samples, rgb, ideal, profile="quick")["delegated"] is True
    chart = synthetic()
    assert chart.shape[1] == 3 and recipe_chart(model, entry).shape[1] == 3
    assert masks(model, entry, chart[:32], "contrast")
    dng_path = tmp_path / "a.dng"
    dng_path.write_bytes(_tiny_dng())
    from gxr_studio.raw import open_dng

    handle = open_dng(dng_path)
    try:
        with pytest.raises(SceneError):
            scene_colour(handle)
    finally:
        handle.release()
    colour_of = SceneColour(np.ones(3), np.eye(3))
    photos = [{"codes": x.reshape(6, 8, 3), "colour": colour_of, "label": "a"}]
    built = build_samples(model, entry, "vlog", photos)
    assert built.xyz is not None
    with pytest.raises(SceneError):
        build_samples(model, entry, "vlog", None)
    cube = b"LUT_3D_SIZE 2\n0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n"
    lib = Library(tmp_path / "lib").open()
    try:
        render = RenderService(home, lib)
        svc = LutService(home, lib, render)
        added = svc.add_cube(cube, "camera_vlog.cube")
        target = svc.target(svc.cube(added["cube_id"]), "srgb")
        stock_rgb, ideal_rgb = target_rgb(model, entry, samples, target)
        assert stock_rgb.shape == ideal_rgb.shape
        assert colorimetry.xy_to_cct((0.3457, 0.3585))[0] > 1000
        assert colorimetry.bradford((0.3457, 0.3585), (0.3127, 0.3290)).shape == (3, 3)
    finally:
        lib.close()


class _Proc:
    def __init__(self) -> None:
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def test_lut_jobs_worker_and_recipes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    entry = _render_entry()
    home = tmp_path / "app"
    _install_camera(home, entry)
    lib = Library(tmp_path / "lib").open()
    try:
        _commit_dng(lib, _tiny_dng())
        jpg = tmp_path / "lib" / "2026" / "2026-10-16" / "only.jpg"
        Image.new("RGB", (16, 12), (20, 40, 80)).save(jpg, "JPEG")
        st = jpg.stat()
        lib.commit_shot(
            ShotRecord(
                "jpg1",
                "only",
                "2026/2026-10-16",
                "2026-10-16",
                "mtime",
                None,
                None,
                None,
                None,
                1,
                16,
                12,
                "2026-10-30T00:00:00",
                [FileRecord("jpg", "2026/2026-10-16/only.jpg", sha256_file(jpg), st.st_size, st.st_mtime_ns)],
            )
        )
        render = RenderService(home, lib)
        svc = LutService(home, lib, render)
        procs: list[_Proc] = []
        monkeypatch.setattr("gxr_studio.lut.service.subprocess.Popen", lambda *a, **k: procs.append(_Proc()) or procs[-1])
        cube = ("LUT_3D_SIZE 2\n0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n").encode()
        added = svc.add_cube(cube, "plain.cube")
        cube_id = added["cube_id"]
        shot = lib.get_shot("syn1")
        jpeg, info = svc.preview(cube_id, "lut", {"space": "vlog", "output": "srgb"}, shot, max_edge=32)
        assert jpeg[:2] == b"\xff\xd8" and info["mode"] in ("04-simulation", "neutral")
        jpg_shot = lib.get_shot("jpg1")
        jpeg, info = svc.preview(cube_id, "original", {"space": "srgb", "output": "srgb"}, jpg_shot, max_edge=32)
        assert info["mode"] == "camera-jpg"
        with pytest.raises(Exception):
            svc.preview(cube_id, "nope", {"space": "srgb", "output": "srgb"}, None)
        with pytest.raises(Exception):
            svc.start_fit(cube_id, {"space": "srgb", "output": "srgb"}, unit="qlaunch")
        started = svc.start_fit(cube_id, {"space": "srgb", "output": "srgb"}, profile="quick")
        with pytest.raises(Exception):
            svc.start_fit(cube_id, {"space": "srgb", "output": "srgb"}, profile="quick")
        job_id = started["job_id"]
        assert svc.status(job_id)["state"] == "running"
        menu = Menu()
        levels = {c["name"]: c["default"] for c in menu.controls(0, 4)}
        result = {
            "state": "done",
            "result": {
                "formula": "synthetic",
                "cube": {"title": "plain"},
                "candidates": [
                    {
                        "unit": "jlaunch",
                        "slot": 4,
                        "levels": levels,
                        "similarity": 10.0,
                        "label": "差别明显",
                        "holdout": {"median": 1},
                        "baseline04_holdout": {"median": 2},
                        "differences": ["整体已经很接近"],
                        "entry_sha256": "ab" * 32,
                    }
                ],
            },
        }
        (svc.jobs_dir / job_id / "result.json").write_text(json.dumps(result), encoding="utf-8")
        procs[0].returncode = 0
        saved = svc.save_recipe(job_id, 0, "合成配方")
        assert svc.get_recipe(saved["id"])["name"] == "合成配方"
        assert svc.rename_recipe(saved["id"], "改名")["name"] == "改名"
        assert len(svc.list_recipes()) == 1
        svc.delete_recipe(saved["id"])
        assert svc.list_recipes() == []
        nxt = svc.start_fit(cube_id, {"space": "srgb", "output": "srgb"}, profile="quick")
        assert svc.cancel(nxt["job_id"])["state"] == "cancelled"
        svc.close()
        dng_path = str(lib.abspath(shot["files"]["dng"]["relpath"]))  # type: ignore[index]
        photos, skipped = worker.load_photos(
            [{"dng": str(tmp_path / "missing.dng"), "label": "gone"}, {"dng": dng_path, "label": "ok"}],
            1,
        )
        assert skipped and photos and photos[0]["codes"].ndim == 3
    finally:
        lib.close()


def test_worker_result_files_and_parent_watch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    watchers_before = {thread.ident for thread in threading.enumerate() if thread.name == "parent-watch"}
    real_watch_parent = worker.watch_parent
    handlers = {}
    # run() 的文件结果在当前进程核对；进程监测在下面单独启动并等待结束。
    monkeypatch.setattr(worker, "watch_parent", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker.signal, "signal", lambda signum, handler: handlers.__setitem__(signum, handler))
    entry = _render_entry()
    home = tmp_path / "app"
    _install_camera(home, entry)
    cube_path = tmp_path / "a.cube"
    cube_path.write_text("LUT_3D_SIZE 2\n0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n", encoding="utf-8")
    job = tmp_path / "job-missing"
    job.mkdir()
    spec = {"home": str(home), "cube": str(tmp_path / "nope.cube"), "input_space": "srgb", "output_space": "srgb"}
    (job / "job.json").write_text(json.dumps(spec), encoding="utf-8")
    assert worker.run(job) == 1
    assert json.loads((job / "result.json").read_text())["state"] == "error"

    def boom(*_a: object, **_k: object) -> object:
        raise SceneError("needs_photo", "需要照片")

    monkeypatch.setattr("gxr_studio.lut.scene.build_samples", boom)
    job2 = tmp_path / "job-scene"
    job2.mkdir()
    (job2 / "job.json").write_text(
        json.dumps({"home": str(home), "cube": str(cube_path), "input_space": "srgb", "output_space": "srgb", "photos": []}),
        encoding="utf-8",
    )
    assert worker.run(job2) == 1
    assert json.loads((job2 / "result.json").read_text())["code"] == "needs_photo"
    monkeypatch.setattr("gxr_studio.lut.scene.build_samples", lambda *a, **k: (_ for _ in ()).throw(Cancelled()))
    job3 = tmp_path / "job-cancel"
    job3.mkdir()
    (job3 / "job.json").write_text((job2 / "job.json").read_text(), encoding="utf-8")
    assert worker.run(job3) == 0
    assert json.loads((job3 / "result.json").read_text())["state"] == "cancelled"
    tiny = Samples(np.zeros((4, 3)), None, np.ones(4), np.zeros(4, np.int8), np.array([False, False, False, True]), [])
    monkeypatch.setattr("gxr_studio.lut.scene.build_samples", lambda *a, **k: tiny)
    monkeypatch.setattr("gxr_studio.lut.scene.target_rgb", lambda *a, **k: (np.zeros((4, 3)), np.ones((4, 3))))
    monkeypatch.setattr("gxr_studio.lut.fit.Fitter.run", lambda self: {"samples": {"n": 1}})
    job4 = tmp_path / "job-done"
    job4.mkdir()
    (job4 / "job.json").write_text((job2 / "job.json").read_text(), encoding="utf-8")
    assert worker.run(job4) == 0
    done = json.loads((job4 / "result.json").read_text())
    assert done["state"] == "done" and done["result"]["samples"]["skipped_photos"] == []
    assert set(handlers) == {worker.signal.SIGTERM, worker.signal.SIGINT}

    monkeypatch.setattr(worker.time, "sleep", lambda _s: None)
    monkeypatch.setattr(worker.os, "_exit", lambda _code: None)
    stop: dict[str, object] = {"flag": False}
    thread = real_watch_parent(stop, parent_pid=-1, poll=0, grace=0)
    thread.join(2)
    assert stop["flag"] is True
    assert {thread.ident for thread in threading.enumerate() if thread.name == "parent-watch"} == watchers_before


def test_intake_pack_scan_and_api(tmp_path: Path, settings):  # type: ignore[no-untyped-def]
    entry = _render_entry()
    home = settings.home_dir
    with pytest.raises(intake.IntakeError):
        intake.import_calibration_folder(home, tmp_path)
    _install_camera(home, entry)
    assert intake.existing_source_sha(home)
    assert intake.describe(home)["generation"] == 127
    assert intake.describe_calibration(home)["isos"] == [200, 1600]
    with pytest.raises(intake.IntakeError):
        intake.inspect_dat(b"")
    with pytest.raises(intake.IntakeError):
        intake.inspect_dat(b"not a dat")
    with pytest.raises(intake.IntakeError):
        intake.install_dat(b"GXRCE001" + bytes(64), home, source_name="GXRCE.DAT", origin={"kind": "file"})
    pack = tmp_path / "pack"
    sources = {iso: (home / "calib-src" / f"data_{iso}.npz", home / "calib-src" / f"ring_{iso}.json") for iso in (200, 1600)}
    intake.write_pack(pack, sources)
    manifest, kind = intake.import_calibration_folder(home, pack)
    assert kind == "pack" and manifest["schema"]
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(intake.IntakeError):
        intake.import_calibration_folder(home, empty)
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "calib-pack.json").write_text("{", encoding="utf-8")
    with pytest.raises(intake.IntakeError):
        intake.import_calibration_folder(home, broken)
    vol_root = tmp_path / "volumes"
    card = vol_root / "NO NAME"
    (card / "DCIM").mkdir(parents=True)
    (card / "GXRCE.DAT").write_bytes(b"GXRCE001synthetic")
    (vol_root / ".hidden").mkdir()
    (vol_root / "file-only").write_text("x", encoding="utf-8")
    try:
        scans = intake.scan_cards_for_dat((vol_root,))
        assert any(s.has_dat for s in scans)
        raw, name = intake.read_dat_from_path(card / "GXRCE.DAT")
        assert name == "GXRCE.DAT" and raw.startswith(b"GXRCE001")
        raw, name = intake.read_dat_from_path(card)
        assert name == "GXRCE.DAT"
        with pytest.raises(intake.IntakeError):
            intake.read_dat_from_path(Path("relative.dat"))
        with pytest.raises(intake.IntakeError):
            intake.read_dat_from_card(CardReader(), scans[0].__class__(Card(card, "NO NAME"), True, card / "missing.dat", 1))
    finally:
        audit.unprotect(card)
    lib = Library(settings.library_dir).open()
    try:
        shot = _commit_dng(lib, _tiny_dng())
        jpg = settings.library_dir / "2026" / "2026-10-16" / "plain.jpg"
        jpg.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), (1, 2, 3)).save(jpg)
        st = jpg.stat()
        lib.commit_shot(
            ShotRecord(
                "jpgonly",
                "plain",
                "2026/2026-10-16",
                "2026-10-16",
                "mtime",
                None,
                None,
                None,
                None,
                1,
                8,
                8,
                "2026-10-30T00:00:00",
                [FileRecord("jpg", "2026/2026-10-16/plain.jpg", sha256_file(jpg), st.st_size, st.st_mtime_ns)],
            )
        )
    finally:
        lib.close()
    meta = read_dng_meta(settings.library_dir / "2026" / "2026-10-16" / "syn1.DNG")
    assert meta.iso == 200 and meta.error is None
    assert read_dng_meta(jpg).error
    assert read_jpeg_meta(jpg).width == 8
    preview = dng_preview_image(settings.library_dir / "2026" / "2026-10-16" / "syn1.DNG")
    assert preview is not None and preview.size[0] > 0
    junk = tmp_path / "not-a-dng.bin"
    junk.write_bytes(b"not a dng")
    assert dng_preview_image(junk) is None
    assert parse_exif_datetime("2026:10:16 18:51:53") is not None
    assert parse_exif_datetime("1990:01:01 00:00:00") is None
    assert plausible(datetime(1990, 1, 1)) is False
    assert plausible(datetime(2026, 10, 16)) is True
    app = create_app(settings)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        assert client.get("/api/camdata/status").status_code == 200
        assert client.get("/api/render/status").json()["ready"] is True
        assert client.get("/api/recipes").status_code == 200
        # 卷上那份合成 DAT 过不了校验，导入要拒绝，不能写进应用数据。
        assert client.post("/api/camdata/import-card", json={}).status_code == 422
        bad = tmp_path / "bad.dat"
        bad.write_bytes(b"nope")
        assert client.post("/api/camdata/import-file", json={"path": str(bad)}).status_code == 422
        assert client.post("/api/camdata/import-upload", content=b"GXRCE001xx", headers={"X-GXR-Filename": "GXRCE.DAT"}).status_code == 422
        assert client.post("/api/calibration/import", json={"path": str(pack)}).status_code == 200
        assert client.post("/api/calibration/import", json={"path": "relative"}).status_code == 422
        edit = client.get(f"/api/photos/{shot['shot_id']}/edit")
        assert edit.status_code == 200
        rev = edit.json()["rev"]
        body = {"adjust": {"exposure_ev": 0.2, "contrast": 4}, "expected_rev": rev}
        assert client.put(f"/api/photos/{shot['shot_id']}/edit", json=body).status_code == 200
        rendered = client.get(f"/api/photos/{shot['shot_id']}/render", params={"max_edge": 64, "exact": "true"})
        assert rendered.status_code == 200 and rendered.content[:2] == b"\xff\xd8"
        etag = rendered.headers["etag"]
        cached = client.get(
            f"/api/photos/{shot['shot_id']}/render",
            params={"max_edge": 64, "exact": "true"},
            headers={"If-None-Match": etag},
        )
        assert cached.status_code == 304
        baked = client.get(f"/api/photos/{shot['shot_id']}/render", params={"max_edge": 64})
        assert baked.status_code == 200 and baked.headers["x-gxr-prediction"] == "1"
        assert client.get(f"/api/photos/{shot['shot_id']}/render", params={"max_edge": 8}).status_code == 422
        assert client.get("/api/photos/jpgonly/render").status_code == 200
        assert client.delete(f"/api/photos/{shot['shot_id']}/edit").status_code == 200
        cube_bytes = b"LUT_3D_SIZE 2\n0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n"
        up = client.post("/api/lut/upload", content=cube_bytes, headers={"X-GXR-Filename": "SLog3.cube"})
        assert up.status_code == 200
        cube_id = up.json()["cube_id"]
        preview = client.get(f"/api/lut/{cube_id}/preview", params={"which": "lut", "space": "srgb", "max_edge": 64, "photo": shot["shot_id"]})
        assert preview.status_code == 200 and preview.content[:2] == b"\xff\xd8"
        refused = client.post("/api/lut/fit", json={"cube_id": cube_id, "unit": "qlaunch", "profile": "quick", "space": "srgb"})
        assert refused.status_code == 422


def test_audit_hook_branches(tmp_path: Path):
    watched = tmp_path / "watched"
    watched.mkdir()
    protected = tmp_path / "protected"
    protected.mkdir()
    audit.protect(protected)
    try:
        with audit.AuditRecorder(watch=(str(watched),), record_reads=True) as rec:
            audit._hook("open", (str(watched / "a.txt"), "w"))
            audit._hook("os.rename", (str(watched / "a.txt"), str(watched / "b.txt")))
            audit._hook("os.listdir", (str(watched),))
            audit._hook("sqlite3.connect", (str(watched / "index.sqlite"),))
            audit._hook("open", (object(), "r"))  # type: ignore[arg-type]
            audit._hook("socket.connect", (None, "/tmp/gxr-studio-test.sock"))
            try:
                audit._hook("open", (str(protected / "nope"), "w"))
            except PermissionError:
                pass
            try:
                audit._hook("socket.getaddrinfo", ("example.invalid", 80))
            except PermissionError:
                pass
            assert rec.writes_under(watched)
            assert rec.reads_under(watched)
    finally:
        audit.unprotect(protected)
        audit._network_log.clear()
    assert audit.network_attempts() == []
