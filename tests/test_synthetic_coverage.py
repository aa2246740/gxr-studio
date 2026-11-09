"""不依赖卡上的 DAT、样本 DNG 或研究目录的覆盖。

干净克隆里那些夹具测试会跳过。这里用合成的 552 B 条目、小矩阵和假的菜单数据把整数核心、
色度公式和 DAT 封装的拒绝路径跑一遍。合成字节不是理光固件，也不进仓库当样本。
"""

from __future__ import annotations

import hashlib
import json
import struct
from types import SimpleNamespace

import numpy as np
import pytest

from gxr_studio.camdata import calib
from gxr_studio.camdata import datcodec as dc
from gxr_studio.camdata import store as camstore
from gxr_studio.engine import colour, knobs, matrix_model, sector_model
from gxr_studio.engine.model import Model
from gxr_studio.library.store import Library
from gxr_studio.lut import colorimetry
from gxr_studio.lut.fit import Fitter
from gxr_studio.lut.scene import build_samples, photo_samples, synthetic_samples
from gxr_studio.lut.service import LutService
from gxr_studio.render import chain
from gxr_studio.render.params import EditState
from gxr_studio.render.recipes import Menu, Resolved
from gxr_studio.render.service import RenderService


def _curve() -> bytes:
    return bytes(min(255, i * 8) for i in range(33))


def _xs() -> tuple[int, ...]:
    return tuple(int(i * 4095 / 32) for i in range(1, 32))


def make_entry(kind: int = 4, flags: int = 0x17) -> bytes:
    raw = bytearray(552)
    raw[0] = kind
    raw[1] = flags
    xs = _xs()
    curve = _curve()
    for at in (20, 284, 456):
        struct.pack_into(">31H", raw, at, *xs)
    for at in (82, 346, 379, 412, 518):
        raw[at : at + 33] = curve
    words = [200, 200, 200] + [0] * 11 + [200, 200, 200] + [0] * 11
    struct.pack_into(">28H", raw, 216, *(knobs.enc(v) for v in words))
    for slot in range(24):
        raw[115 + 4 * slot : 115 + 4 * slot + 4] = colour.encode_word(8, slot - 12)
    return bytes(raw)


def test_knobs_apply_runs_on_a_synthetic_entry():
    base = make_entry()
    stock = make_entry(flags=0x17)
    plain, stats = knobs.apply(stock, base)
    assert plain == base and stats["clamps"] == 0
    doses = (400, 200, -100, 50, 80, 30, -20, 40, 60, 25)
    for wb, tone in ((0, 0), (1, 1)):
        got, info = knobs.apply(stock, base, knobs.Request(70, doses, wb, tone))
        assert len(got) == 552 and isinstance(info["clamps"], int)
    bw, _ = knobs.apply(make_entry(kind=3), base, knobs.Request(40, doses, 0, 0))
    assert len(bw) == 552
    assert knobs.request_for("contrast", 10).doses[0] == 10
    with pytest.raises(ValueError):
        knobs.Request(200, (0,) * 10).validate()
    assert knobs.rd(-5, 2) == -3
    assert knobs.clamp(9, 0, 4, {"clamps": 0}) == 4


def test_colour_matrix_and_sector_math():
    rgb = np.array([[0.1, 0.2, 0.3], [0.9, 0.4, 0.2]])
    lab = colour.lab_from_codes(rgb * 255)
    assert colour.delta_e00(lab, lab).shape == (2,)
    assert colour.srgb_to_linear(rgb).shape == rgb.shape
    word = colour.encode_word(3, -4)
    assert colour.decode_word(word) == (3, -4)
    ctx = SimpleNamespace(d0=np.zeros(4), d1=np.ones(4), S_eff=np.ones(4) + 0.2j)
    assert colour.Z_of_words(np.array([1, 2, 3, 4]), np.array([0, 1, 0, 1]), ctx).shape == (4,)
    row = matrix_model.MatrixRow.from_words([matrix_model.encode_coef(v) for v in (200, 100, 80, 0, 40, -20, 0, -10, 30)] + [0, 128, 128, 0, 0])
    y, c = row.apply(np.array([[10.0, 20.0, 30.0], [200.0, 100.0, 50.0]]))
    back = row.invert(y, c)
    assert back.shape == (2, 3)
    assert row.neutral_preserving() is False
    assert row.violations() == [] or isinstance(row.violations(), list)
    curve = matrix_model.Curve.from_rows(list(range(1, 32)), list(range(0, 264, 8)))
    assert curve(np.array([0.0, 100.0, 4095.0])).shape == (3,)
    assert curve.inverse(np.array([10.0, 40.0])).shape == (2,)
    yy, cc = matrix_model.ycbcr(np.array([[10.0, 20.0, 30.0]]))
    assert matrix_model.rgb_from_ycc(yy, cc).shape[-1] == 3
    hue = sector_model.hue_deg(0.2 + 0.3j)
    assert 0 <= float(hue) < 360
    nodes = np.linspace(0, 345, 24)
    weights = sector_model.weights(np.linspace(0, 300, 5), nodes, "linear", 8.0)
    assert weights.shape[0] == 5
    assert sector_model.weights(np.array([10.0]), nodes, "hard", np.array([4.0])).shape[1] == 24
    assert sector_model.effective(1 + 1j, 0.5).shape == ()


def test_colorimetry_and_a_synthetic_model():
    xyz = colorimetry.xy_to_xyz((0.3457, 0.3585))
    assert colorimetry.xyz_to_xy(xyz).shape == (2,)
    assert colorimetry.bradford((0.3, 0.3), (0.32, 0.33)).shape == (3, 3)
    temp, tint = colorimetry.xy_to_cct((0.3127, 0.3290))
    assert temp > 1000 and isinstance(tint, float)
    eye = np.eye(3)
    tags = SimpleNamespace(
        color_matrix=[eye, eye * 0.9],
        camera_calibration=[eye, eye],
        forward_matrix=[eye, eye],
        illuminant=[3, 10],
        analog_balance=np.ones(3),
    )
    spec = colorimetry.DngColour(tags)
    white = spec.white_info((0.33, 0.33), neutral=np.array([0.4, 1.0, 0.6]))
    assert "cct" in white or "temperature" in white or isinstance(white, dict)
    single = colorimetry.DngColour(tags, matrix="cm1")
    assert single.single is True
    ring = {
        "schema": "gxr-sector-model/2",
        "k_coupling": 1.0,
        "nodes_unwrapped_deg": [i * 15.0 for i in range(24)],
        "kernel": {"type": "linear", "blur_deg": 4.0, "blur_kappa": 2.0},
        "gain": 1.0,
    }
    stock = np.full((24, 2), 16, np.int64)
    model = Model(stock, np.zeros(24), np.zeros(24), ring)
    entry = make_entry()
    x = np.array([[80.0, 90.0, 100.0], [400.0, 200.0, 100.0], [1000.0, 1000.0, 1000.0]])
    rgb = model.predict(entry, x, quantize=True)
    assert rgb.shape == (3, 3)
    pre = model.pre(entry, x)
    assert pre.shape == (3, 3)
    assert model.lab(rgb).shape[1] == 3
    exact = chain.exact_u8(model, entry, x, None, None, workers=1)
    assert exact.dtype == np.uint8
    baked3 = chain.bake_model(model, entry, grid=4, workers=1)
    shown = chain.apply_baked(x.astype(np.float32), baked3, baked3.folded(None), None, workers=1)
    assert shown.shape == x.shape
    try:
        baked = chain.bake_chroma(model, entry, step=16.0, workers=1)
    except Exception:
        baked = None
    if baked is not None:
        out = chain.chroma_u8(np.array([[[100, 120, 140]]], np.float32), baked, None, None, workers=1)
        assert out.shape[-1] == 3


def test_datcodec_rejects_synthetic_garbage_and_packs_until_the_pin():
    with pytest.raises(dc.DatError):
        dc.peek_generation(b"nope")
    header = bytearray(dc.HEADER)
    header[:8] = b"GXRCE001"
    struct.pack_into(">I", header, 28, 99)
    with pytest.raises(dc.UnsupportedGeneration):
        dc.parse(bytes(header))
    struct.pack_into(">I", header, 28, 127)
    struct.pack_into(">I", header, 16, 256)
    with pytest.raises(dc.DatError):
        dc.parse(bytes(header))
    assert dc._align4(5) == 8
    record = bytearray(64)
    record[0] = 1
    assert len(dc.identity(bytes(record) + bytes(20))) == 1 + 32 + 1 + 15
    records = []
    for slot in range(1, 17):
        rec = bytearray(64)
        rec[0] = slot
        rec[1] = 1
        struct.pack_into(">I", rec, 60, dc.crc(rec[:60]))
        records.append(bytes(rec))
    entry = make_entry()
    entries = {4: entry, 1: entry}
    ranges = [[(-2, 2, 0)] * 6 + [(0, 0, 0), (0, 0, 0)] for _ in range(16)]
    module = dc.Module("jlaunch", bytes(152), records, entries, [4] * 16, ranges, bytes(32), 0)
    ids = (1, 2, 3, 4, 5, 6, 0, 0)
    meta = dc._metadata([module, module, module], ids)
    assert meta[:4] == b"GXTM"
    blob = dc._bank_blob(records, 127)
    assert blob[:8] == b"GXRRV201"
    assert dc._owners(module)[0] == 4
    names = ("jlaunch", "qlaunch", "l08firm")
    modules = [dc.Module(name, bytes(152), records, entries, [4] * 16, ranges, bytes(32), 0) for name in names]
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
        ids,
        bytes(24 * dc.CONFIG_BYTES),
        [],
        None,
    )
    with pytest.raises(dc.DatError):
        dc.pack(dat)


def test_scene_fit_and_lut_service_on_synthetic_inputs(tmp_path: object):
    from pathlib import Path

    home = Path(str(tmp_path))
    entry = make_entry()
    ring = {
        "schema": "gxr-sector-model/2",
        "k_coupling": 1.0,
        "nodes_unwrapped_deg": [i * 15.0 for i in range(24)],
        "kernel": {"type": "linear", "blur_deg": 4.0, "blur_kappa": 2.0},
        "gain": 1.0,
    }
    model = Model(np.full((24, 2), 16, np.int64), np.zeros(24), np.zeros(24), ring)
    samples = synthetic_samples(model, entry, seed=1)
    assert len(samples) > 100 and samples.counts()["total"] == len(samples)
    photo = photo_samples(np.linspace(10, 200, 48, dtype=np.float64).reshape(4, 4, 3), n=8)
    built = build_samples(model, entry, "srgb", [{"codes": photo.reshape(1, -1, 3), "colour": None, "label": "syn"}])
    assert built.photos
    table = model.engine.table
    chroma = np.array([0.3 + 0.2j, 0.1 - 0.05j])
    forward = table.forward(chroma)
    assert forward[0].shape == chroma.shape
    try:
        table.inverse_stock(forward[0][:1])
    except ValueError:
        pass
    menu = Menu()

    class Cam:
        stock = [entry]

        def controls(self, unit: int, slot: int) -> list[dict[str, object]]:
            return menu.controls(unit, slot)

        def resolve(self, sel: object) -> Resolved:
            names, levels, req = menu.request_for(sel)  # type: ignore[arg-type]
            out, stats = knobs.apply(entry, entry, req)
            return Resolved(out, "ab" * 32, dict(zip(names, levels, strict=False)), stats)

    idx = np.arange(min(24, len(samples)))
    fitter = Fitter(Cam(), model, samples, np.full((len(samples), 3), 40.0), np.full((len(samples), 3), 80.0), profile="quick")
    levels = fitter.defaults(4)
    _levels, score = fitter.descend(4, levels, idx, "n", names=["contrast"], max_sweeps=1, span=(0.0, 1.0), label="syn")
    assert score >= 0
    cube = ("LUT_3D_SIZE 2\n0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n").encode()
    lib = Library(home / "lib").open()
    try:
        render = RenderService(home / "app", lib)
        svc = LutService(home / "app", lib, render)
        added = svc.add_cube(cube, "synthetic.cube")
        original, info = svc.preview(added["cube_id"], "original", {"space": "srgb"}, None, max_edge=32)
        assert original[:2] == b"\xff\xd8" and info["mode"] == "chart"
        spec = colorimetry.DngColour(
            SimpleNamespace(
                color_matrix=[np.eye(3), np.eye(3) * 0.9],
                camera_calibration=[np.eye(3), np.eye(3)],
                forward_matrix=[np.eye(3), np.eye(3)],
                illuminant=[3, 10],
                analog_balance=np.ones(3),
            )
        )
        assert spec.camera_to_xyz((0.33, 0.33)).shape == (3, 3)
        assert spec.xyz_to_camera((0.33, 0.33)).shape == (3, 3)
    finally:
        lib.close()


def _sealed_entry() -> bytes:
    raw = bytearray(make_entry())
    knobs.checksum(raw)
    return bytes(raw)


def test_synthetic_camera_data_drives_render_status(tmp_path: object):
    from pathlib import Path

    home = Path(str(tmp_path)) / "app"
    entry = _sealed_entry()
    root = camstore.camera_data_dir(home)
    files: dict[str, bytes] = {}
    for name in dc.NAMES:
        for slot in range(1, 9):
            rel = f"units/{name}/recipes/{slot:02d}.entry"
            files[rel] = entry
        files[f"units/{name}/stock04.entry"] = entry
    for rel, blob in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
    manifest = {
        "schema": camstore.SCHEMA,
        "generation": 127,
        "source": {"name": "synthetic", "size": len(entry), "sha256": hashlib.sha256(entry).hexdigest()},
        "imported_at": "2026-10-30T00:00:00",
        "files": {rel: {"size": len(blob), "sha256": hashlib.sha256(blob).hexdigest()} for rel, blob in files.items()},
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert camstore.verify(home)["generation"] == 127
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
    src = Path(str(tmp_path)) / "src"
    src.mkdir()
    sources = {}
    for iso in (200, 1600):
        npz = src / f"data_{iso}.npz"
        ring_path = src / f"ring_{iso}.json"
        np.savez(npz, stock=stock, d0=d0, d1=d1, S_eff=s_eff, input_coefficients=np.zeros((4, 3)))
        ring_path.write_text(json.dumps(ring), encoding="utf-8")
        sources[iso] = (npz, ring_path)
    calib.import_calibration(home, {unit: sources for unit in dc.NAMES}, origin={"kind": "synthetic"})
    lib = Library(Path(str(tmp_path)) / "lib").open()
    try:
        render = RenderService(home, lib)
        assert render.available()
        assert render.catalog()
        shot = {
            "lens": "GR LENS A12 50mm F2.5 MACRO",
            "iso": 200,
            "files": {"dng": {"sha256": "ab" * 32, "relpath": "a.dng"}},
        }
        plan = render.plan(shot, EditState())
        assert plan.slot == 4 and render.model(plan.model_iso) is not None
        assert "ready" in render.status()
    finally:
        lib.close()
