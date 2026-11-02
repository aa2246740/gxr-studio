"""显卡预览的只读接口和服务器端数据：RAW 布局、烘焙表/LUT 表的字节、成片裁切框（与 finish_array 逐像素相同）。

全部用合成照片和合成数据；需要相机数据的路径用替身，不读理光字节。
"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace
from urllib.parse import quote, unquote

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import PORT
from gxr_studio.app import create_app
from gxr_studio.finish.compose import finish_array
from gxr_studio.finish.geometry import warp
from gxr_studio.finish.spec import Geometry, factory_frame, factory_stamp
from gxr_studio.gpu import source as gsource
from gxr_studio.gpu.box import final_box
from gxr_studio.gpu.source import raw_layout
from gxr_studio.gpu.tables import bake_bytes, lut_bytes
from gxr_studio.lut.cube import Cube
from gxr_studio.lut.spaces import Target
from gxr_studio.raw import open_dng
from gxr_studio.render.chain import ChromaBake
from gxr_studio.render.params import EditState
from raw_support import bayer_scene, build_dng
from test_api import import_all

LENS = "RICOH GR LENS A12 50mm F2.5 MACRO"


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        yield c


def _flag(settings, on: bool = True) -> None:
    settings.home_dir.mkdir(parents=True, exist_ok=True)
    (settings.home_dir / "flags.json").write_text(json.dumps({"gpu_preview": on}))


def _card(volumes, *, raw=True, jpg=True, **dng):
    folder = volumes / "SYNTH" / "DCIM/100RICOH"
    folder.mkdir(parents=True, exist_ok=True)
    image = np.zeros((80, 120, 3), np.uint8)
    image[..., 0] = np.arange(120, dtype=np.uint8)[None, :]
    image[..., 1] = np.arange(80, dtype=np.uint8)[:, None] * 2
    image[..., 2] = 90
    if jpg:
        Image.fromarray(image, "RGB").save(folder / "R0000001.JPG", quality=95)
    if raw:
        (folder / "R0000001.DNG").write_bytes(build_dng(bayer_scene(80, 120, image / 255), white=4095, bps=12, lens=LENS, **dng))
    return folder


def _photo(client):
    import_all(client)
    return client.get("/api/photos").json()[0]


def _synthetic_bake() -> ChromaBake:
    pre = np.repeat(np.linspace(0, 255, 4096, dtype=np.float32)[:, None], 3, axis=1)
    gain = np.zeros((5, 4, 2), np.float32)
    gain[..., 0] = 1
    return ChromaBake(pre, np.eye(3, dtype=np.float32), np.zeros(3, np.float32), gain, (-4.0, -5.0), 2.0)


# ---- RAW 布局 -------------------------------------------------------------------


def test_raw_layout_describes_the_strip_crop_and_orientation():
    data = build_dng(
        bayer_scene(40, 60, np.full((40, 60, 3), 0.5)),
        bps=12,
        crop_origin=(1, 1),
        crop_size=(58, 38),
        orientation=6,
        black=(10, 12, 14, 16),
        black_repeat=(2, 2),
    )
    dng = open_dng(data)
    layout, reason = raw_layout(dng)
    assert reason is None and layout is not None
    assert layout["bps"] == 12 and layout["big_endian"] is True and layout["row_bytes"] == 90
    assert layout["crop"] == [1, 1, 38, 58]
    assert layout["phase"] == [int(v) for v in np.asarray(dng.crop_cfa_phase()).reshape(-1)]
    assert layout["black"] == [10.0, 12.0, 14.0, 16.0]
    assert layout["orientation"] == 6 and layout["layer"] == [19, 29] and layout["full"] == [38, 58]
    off, cnt = layout["strips"][0]
    assert cnt == 40 * 90 and off + cnt <= len(data)


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"linearization": list(range(4096))}, "linearization_table"),
        ({"black_delta_h": [0.0] * 60}, "black_level_pattern"),
        ({"black": (1, 2, 3), "black_repeat": (1, 3)}, "black_level_pattern"),
        ({"orientation": 9}, "orientation"),
    ],
)
def test_raw_layout_refuses_what_the_shader_cannot_read(kwargs, reason):
    dng = open_dng(build_dng(bayer_scene(40, 60, np.full((40, 60, 3), 0.5)), bps=16, **kwargs))
    layout, got = raw_layout(dng)
    assert layout is None and got == reason


def test_scene_affine_is_none_without_colour_matrices():
    dng = open_dng(build_dng(bayer_scene(40, 60, np.full((40, 60, 3), 0.5)), bps=16))
    assert gsource.scene_affine(dng) is None
    assert gsource.describe(dng, with_scene=True).scene is None


def test_scene_affine_expands_scene_colour_exactly():
    dng = open_dng(
        build_dng(
            bayer_scene(40, 60, np.full((40, 60, 3), 0.5)),
            bps=16,
            root_extra=[(50721, "SRATIONAL", [1, 0, 0, 0, 1, 0, 0, 0, 1]), (50778, "SHORT", [21])],
        )
    )
    affine = gsource.scene_affine(dng)
    assert affine is not None
    from gxr_studio.lut.scene import scene_colour

    codes = np.array([[100.0, 2000.0, 4000.0], [0.0, 0.0, 0.0]])
    want = scene_colour(dng).xyz(codes)
    got = codes @ np.asarray(affine["A"]).T + np.asarray(affine["b"])
    np.testing.assert_allclose(got, want, rtol=1e-12, atol=1e-12)


# ---- 表的字节 -------------------------------------------------------------------


def test_bake_bytes_layout_matches_the_shader_offsets():
    bk = _synthetic_bake()
    data, meta = bake_bytes(bk)
    arr = np.frombuffer(data, "<f4")
    assert arr.size == 12288 + 9 + 3 + 5 * 4 * 2
    assert meta == {"nx": 4, "ny": 5, "origin": [-4.0, -5.0], "step": 2.0}
    np.testing.assert_array_equal(arr[12288:12297].reshape(3, 3), np.eye(3))
    assert arr[12300] == 1.0 and arr[12301] == 0.0


def test_bake_bytes_refuses_a_wrong_curve_length():
    bk = _synthetic_bake()
    bad = ChromaBake(bk.pre[:100], bk.matrix, bk.offset, bk.gain, bk.origin, bk.step)
    with pytest.raises(ValueError):
        bake_bytes(bad)


def _cube(*, shaper: bool = False, size: int = 3) -> Cube:
    v = np.linspace(0, 1, size)
    rows = "\n".join(f"{r} {g} {b}" for b in v for g in v for r in v)
    head = f"LUT_1D_SIZE 2\nLUT_3D_SIZE {size}\n0 0 0\n1 1 1\n" if shaper else f"LUT_3D_SIZE {size}\n"
    return Cube.parse(f"{head}{rows}\n")


def test_lut_bytes_for_display_and_log_spaces():
    data, meta, reason = lut_bytes(Target(_cube(), "srgb", "srgb"))
    assert reason is None and data is not None and meta is not None
    assert meta["n"] == 3 and meta["display"] is True and meta["input_space_id"] == 0
    np.testing.assert_allclose(meta["display_to_input"], np.eye(3), atol=1e-12)
    arr = np.frombuffer(data, "<f4").reshape(3, 3, 3, 4)
    assert arr[0, 0, 2, 0] == 1.0 and arr[2, 0, 0, 2] == 1.0  # [b][g][r]，R 最快
    _d, log_meta, _r = lut_bytes(Target(_cube(), "llog", "gamma24"))
    assert log_meta is not None and log_meta["display"] is False and log_meta["input_space_id"] == 3


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        (lambda: Target(_cube(shaper=True), "srgb", "srgb"), "lut_1d_shaper"),
        (lambda: Target(_cube(), "srgb", "vlog"), "lut_output_space"),
    ],
)
def test_lut_bytes_refuses_what_the_shader_cannot_decode(target, reason):
    data, meta, got = lut_bytes(target())
    assert data is None and meta is None and got == reason


# ---- 成片裁切框 = finish_array 裁出来的位置 ----------------------------------------


@pytest.mark.parametrize(
    ("geometry", "crop"),
    [
        ({}, None),
        ({}, {"preset": "free", "rect": (0.1, 0.2, 0.5, 0.6)}),
        ({"rotate90": 1, "flip": True}, {"preset": "free", "rect": (0.05, 0.1, 0.8, 0.7)}),
        ({"straighten": 4.5}, None),
        ({"straighten": -3.0, "rotate90": 3}, {"preset": "3:2", "rect": (0.0, 0.0, 1.0, 1.0)}),
        ({"straighten": 2.0}, {"preset": "free", "rect": (0.2, 0.2, 0.7, 0.7)}),
        ({"rotate90": 2}, {"preset": "4:5", "rect": (0.1, 0.1, 0.8, 0.8)}),
    ],
)
def test_final_box_matches_finish_array(geometry, crop):
    h, w = 90, 130
    rng = np.random.default_rng(3)
    rgb = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)
    geom = Geometry(**geometry)
    rect = crop["rect"] if crop else None
    preset = crop["preset"] if crop else None
    want, _valid = finish_array(
        rgb,
        EditState(geometry=geom),
        stage="final",
        frame=factory_frame(),
        stamp=factory_stamp(),
        crop_rect=rect,
        meta={},
        taken=None,
        file_time=None,
        from_raw=True,
        crop_preset=preset,
    )
    (x, y, bw, bh), valid = final_box(w, h, geom, rect, preset)
    warped = warp(rgb, geom)[0] if not geom.is_identity() else rgb
    np.testing.assert_array_equal(warped[y : y + bh, x : x + bw], want)
    assert all(0.0 <= v <= 1.0 for v in valid)


# ---- 接口 -----------------------------------------------------------------------


def test_gpu_info_raw_and_camera_jpg(client, volumes, settings):
    _flag(settings)
    folder = _card(volumes)
    photo = _photo(client)
    info = client.get(f"/api/photos/{photo['id']}/gpu").json()
    assert info["enabled"] is True and info["raw_reason"] is None and info["unit"] == "jlaunch"
    assert info["raw"]["bps"] == 12 and info["raw"]["layer"] == [60, 40]
    raw = client.get(info["raw"]["url"])
    assert raw.status_code == 200 and raw.content == (folder / "R0000001.DNG").read_bytes()
    assert "immutable" in raw.headers["cache-control"]
    jpg = client.get(info["jpg"]["url"])
    assert jpg.status_code == 200 and jpg.content == (folder / "R0000001.JPG").read_bytes()
    assert client.get(f"/api/photos/{photo['id']}/gpu").json() == info  # 布局按 SHA 缓存


def test_gpu_info_off_by_default(client, volumes):
    _card(volumes)
    photo = _photo(client)
    assert client.get(f"/api/photos/{photo['id']}/gpu").json()["enabled"] is False


def test_jpg_only_and_raw_only_photos(client, volumes, settings):
    _flag(settings)
    _card(volumes, raw=False)
    photo = _photo(client)
    info = client.get(f"/api/photos/{photo['id']}/gpu").json()
    assert info["raw"] is None and info["raw_reason"] == "no_raw" and info["jpg"]
    assert client.get(f"/api/photos/{photo['id']}/raw").status_code == 404
    assert client.get(f"/api/photos/{photo['id']}/bake").status_code == 409


def test_raw_without_white_balance_is_reported(client, volumes, settings):
    _flag(settings)
    _card(volumes, jpg=False, with_neutral=False)
    photo = _photo(client)
    info = client.get(f"/api/photos/{photo['id']}/gpu").json()
    assert info["raw"] is None and info["raw_reason"] == "no_white_balance" and info["jpg"] is None
    assert client.get(f"/api/photos/{photo['id']}/camera-jpg").status_code == 404


def test_unreadable_raw_is_reported(client, volumes, settings, monkeypatch):
    _flag(settings)
    _card(volumes)
    photo = _photo(client)
    from gxr_studio.api import gpu_routes

    gpu_routes._layouts.clear()

    def boom(*_a, **_k):
        raise ValueError("坏文件")

    monkeypatch.setattr(gpu_routes, "open_dng", boom)
    assert client.get(f"/api/photos/{photo['id']}/gpu").json()["raw_reason"] == "unreadable:ValueError"


def test_bake_needs_camera_data(client, volumes):
    _card(volumes)
    photo = _photo(client)
    r = client.get(f"/api/photos/{photo['id']}/bake")
    assert r.status_code == 409 and r.json()["error"] == "camera_data_missing"
    bad = client.get(f"/api/photos/{photo['id']}/bake?recipe={quote('{nope')}")
    assert bad.status_code == 422
    unknown = client.get(f"/api/photos/{photo['id']}/bake?recipe={quote(json.dumps({'unit': 'x', 'slot': 1}))}")
    assert unknown.status_code == 422


def test_bake_serves_the_preview_table_with_etag(client, volumes, monkeypatch):
    _card(volumes)
    photo = _photo(client)
    svc = client.app.state.ctx.render
    plan = SimpleNamespace(entry_sha256="e" * 64, model_iso=200, calibrated=True, label="电脑预测", unit="jlaunch", slot=1, defaulted=False)
    monkeypatch.setattr(svc, "plan", lambda shot, edit: plan)
    monkeypatch.setattr(svc, "preview_bake", lambda p: _synthetic_bake())
    monkeypatch.setattr(svc, "engine_pin", lambda: "pin")
    rec = quote(json.dumps({"kind": "builtin", "unit": "jlaunch", "slot": 1, "levels": {}}))
    r = client.get(f"/api/photos/{photo['id']}/bake?recipe={rec}")
    assert r.status_code == 200, r.text
    meta = json.loads(unquote(r.headers["x-gxr-bake"]))
    assert meta["nx"] == 4 and meta["label"] == "电脑预测" and meta["calibrated"] is True and len(r.content) == (12300 + 40) * 4
    again = client.get(f"/api/photos/{photo['id']}/bake?recipe={rec}", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304

    def odd(p):
        raise ValueError("色表行不对称")

    monkeypatch.setattr(svc, "preview_bake", odd)
    assert client.get(f"/api/photos/{photo['id']}/bake").json()["error"] == "cannot_predict"


def test_lut_table_endpoint(client):
    from test_lut_library import cube_bytes, upload

    item = upload(client, cube_bytes(lambda x: x * [0.5, 0.8, 1.0])).json()
    cid = item["cube_id"]
    assert client.patch(f"/api/luts/{cid}", json={"input_space": "srgb"}).status_code == 200
    r = client.get(f"/api/luts/{cid}/table")
    assert r.status_code == 200, r.text
    meta = json.loads(unquote(r.headers["x-gxr-lut"]))
    assert meta["n"] == 17 and meta["display"] is True and len(r.content) == 17**3 * 16
    assert client.get(f"/api/luts/{cid}/table", headers={"If-None-Match": r.headers["etag"]}).status_code == 304
    assert client.patch(f"/api/luts/{cid}", json={"output_space": "vlog"}).status_code == 200
    assert client.get(f"/api/luts/{cid}/table").json()["error"] == "lut_gpu_unsupported"
    assert client.get(f"/api/luts/{'0' * 64}/table").status_code == 404


def test_box_endpoint(client, volumes):
    _card(volumes)
    photo = _photo(client)
    url = f"/api/photos/{photo['id']}/gpu/box"
    r = client.post(
        url, json={"width": 130, "height": 90, "geometry": {"straighten": 4.5}, "crop": {"preset": "free", "rect": [0.2, 0.2, 0.7, 0.7]}}
    )
    assert r.status_code == 200, r.text
    box, _ = final_box(130, 90, Geometry(straighten=4.5), (0.2, 0.2, 0.7, 0.7), "free")
    assert r.json()["box"] == list(box)
    plain = client.post(url, json={"width": 130, "height": 90}).json()
    assert plain["box"] == [0, 0, 130, 90] and plain["valid"] == [0, 0, 1, 1]
    assert client.post(url, json={"width": 130, "height": 90, "geometry": {"vertical": 10}}).status_code == 422
    assert client.post(url, json={"width": 130, "height": 90, "geometry": {"straighten": "x"}}).status_code == 422
    assert client.post(url, json={"width": 0, "height": 90}).status_code == 422


def test_warm_preview_bakes_runs_once_per_unit_and_iso(client, volumes, monkeypatch):
    _card(volumes)
    photo = _photo(client)
    svc = client.app.state.ctx.render
    shot = client.app.state.ctx.lib.get_shot(photo["id"])
    baked = []
    monkeypatch.setattr(svc, "available", lambda: True)
    monkeypatch.setattr(svc, "plan", lambda s, e: e.recipe.slot)
    monkeypatch.setattr(svc, "_bake", lambda plan, step: baked.append(plan))
    thread = svc.warm_preview_bakes(shot)
    assert thread is not None
    thread.join(10)
    assert baked == list(range(1, 9))
    assert svc.warm_preview_bakes(shot) is None

    def fail(*_a):
        raise RuntimeError("坏了")

    svc.invalidate()
    monkeypatch.setattr(svc, "_bake", fail)
    again = svc.warm_preview_bakes(shot)
    assert again is not None
    again.join(10)
    jpg_only = {**shot, "files": {"jpg": shot["files"].get("jpg")}}
    assert svc.warm_preview_bakes(jpg_only) is None


def test_camera_jpg_is_served_as_is(client, volumes, settings):
    _flag(settings)
    folder = _card(volumes, raw=False)
    photo = _photo(client)
    r = client.get(f"/api/photos/{photo['id']}/camera-jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert np.asarray(Image.open(io.BytesIO(r.content))).shape == np.asarray(Image.open(folder / "R0000001.JPG")).shape
