"""显卡预览的只读接口和服务器端数据：RAW 布局、烘焙表/LUT 表的字节、成片裁切框（与 finish_array 逐像素相同）。

全部用合成照片和合成数据；需要相机数据的路径用替身，不读理光字节。
"""

from __future__ import annotations

import io
import json
import os
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
    (settings.home_dir / "flags.json").write_text(json.dumps({"gpu_preview": on}), encoding="utf-8")


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


def test_display_lut_table_is_the_production_function_on_every_input():
    """复审 P1-03：显示类 LUT 的表 = 全部 256³ 个 8 位输入逐个过 `lut.photo._apply`（一维表、任何输出空间都包括在内）。"""
    from gxr_studio.lut.photo import _apply

    v = np.linspace(0, 1, 17)
    nodes = np.clip(np.array([[r, g, b] for b in v for g in v for r in v]) * [0.86, 0.97, 1.05], 0, 1)  # 复审的反例 LUT
    shaped = "LUT_1D_SIZE 3\nLUT_3D_SIZE 17\n0 0 0\n0.6 0.55 0.5\n1 1 1\n"
    cube = Cube.parse(shaped + "\n".join(" ".join(f"{c:.17g}" for c in row) for row in nodes) + "\n")
    target = Target(cube, "srgb", "vlog")
    data, meta, reason = lut_bytes(target)
    assert reason is None and meta == {"n": 256, "display": True, "table8": True} and data is not None
    table = np.frombuffer(data, np.uint8).reshape(256, 256, 256, 4)  # [b][g][r]，R 最快
    assert (table[..., 3] == 255).all()
    rgb = np.empty((256, 256, 3), np.uint8)
    rgb[..., 0] = np.arange(256, dtype=np.uint8)[None, :]
    rgb[..., 1] = np.arange(256, dtype=np.uint8)[:, None]
    for blue in range(256):
        rgb[..., 2] = blue
        np.testing.assert_array_equal(table[blue, ..., :3], _apply(target, rgb), err_msg=f"blue={blue}")


def test_log_lut_bytes_carry_the_space_parameters():
    data, meta, reason = lut_bytes(Target(_cube(), "llog", "gamma24"))
    assert reason is None and data is not None and meta is not None
    assert meta["n"] == 3 and meta["display"] is False and meta["table8"] is False and meta["input_space_id"] == 3
    assert "display_to_input" not in meta
    arr = np.frombuffer(data, "<f4").reshape(3, 3, 3, 4)
    assert arr[0, 0, 2, 0] == 1.0 and arr[2, 0, 0, 2] == 1.0  # [b][g][r]，R 最快


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        (lambda: Target(_cube(shaper=True), "llog", "srgb"), "lut_1d_shaper"),
        (lambda: Target(_cube(), "llog", "vlog"), "lut_output_space"),
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


def test_gpu_info_off_by_default(client, volumes, monkeypatch):
    """复审 P2-04：开关关着时不解析 DNG、不预热烘焙表，只回 enabled=false；设置里带着开关状态，前端据此不发请求。"""
    from gxr_studio.api import gpu_routes

    _card(volumes)
    photo = _photo(client)
    touched = []
    monkeypatch.setattr(gpu_routes, "open_media", lambda *a, **k: touched.append("open") or 1 / 0)
    monkeypatch.setattr(client.app.state.ctx.render, "warm_preview_bakes", lambda *a: touched.append("warm"))
    assert client.get(f"/api/photos/{photo['id']}/gpu").json() == {"enabled": False}
    assert touched == []
    assert client.get("/api/settings").json()["gpu_preview"] is False


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
    assert meta["n"] == 256 and meta["display"] is True and meta["table8"] is True and len(r.content) == 256**3 * 4
    assert client.get(f"/api/luts/{cid}/table", headers={"If-None-Match": r.headers["etag"]}).status_code == 304
    # 改了输出空间：版本变了，表和 ETag 都换；显示类 LUT 的精确表任何输出空间都能给
    assert client.patch(f"/api/luts/{cid}", json={"output_space": "vlog"}).status_code == 200
    again = client.get(f"/api/luts/{cid}/table")
    assert again.status_code == 200 and again.headers["etag"] != r.headers["etag"]
    # Log 类输入 + 着色器没有解码的输出空间：422，前端退回服务器
    assert client.patch(f"/api/luts/{cid}", json={"input_space": "llog"}).status_code == 200
    refused = client.get(f"/api/luts/{cid}/table")
    assert refused.status_code == 422 and refused.json()["error"] == "lut_gpu_unsupported"
    assert client.get(f"/api/luts/{'0' * 64}/table").status_code == 404


def test_lut_table_does_not_write_and_old_records_upgrade_at_startup(settings):
    """复审 P2-05：GET /table 不写盘；旧格式的 LUT 记录在启动时升级。"""
    from test_lut_library import cube_bytes, upload

    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        cid = upload(c, cube_bytes(comments="# Input: sRGB\n")).json()["cube_id"]
        index = c.app.state.ctx.render.luts.home / "luts" / "library.json"
    doc = json.loads(index.read_text(encoding="utf-8"))
    doc["items"][cid].pop("attribution_version", None)  # 旧格式
    index.write_text(json.dumps(doc), encoding="utf-8")
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        upgraded = index.read_bytes()
        assert json.loads(upgraded)["items"][cid]["attribution_version"] == 2  # 启动时升级
        doc = json.loads(upgraded)
        doc["items"][cid].pop("attribution_version", None)
        index.write_text(json.dumps(doc), encoding="utf-8")  # 运行中又出现旧记录（例如手改）
        before = (index.read_bytes(), index.stat().st_mtime_ns, sorted(p.name for p in index.parent.iterdir()))
        assert c.get(f"/api/luts/{cid}/table").status_code == 200
        assert (index.read_bytes(), index.stat().st_mtime_ns, sorted(p.name for p in index.parent.iterdir())) == before


def test_box_endpoint(client, volumes):
    _card(volumes)
    photo = _photo(client)
    url = f"/api/photos/{photo['id']}/gpu/box"
    r = client.post(
        url, json={"width": 120, "height": 80, "geometry": {"straighten": 4.5}, "crop": {"preset": "free", "rect": [0.2, 0.2, 0.7, 0.7]}}
    )
    assert r.status_code == 200, r.text
    box, _ = final_box(120, 80, Geometry(straighten=4.5), (0.2, 0.2, 0.7, 0.7), "free")
    assert r.json()["box"] == list(box)
    plain = client.post(url, json={"width": 120, "height": 80}).json()
    assert plain["box"] == [0, 0, 120, 80] and plain["valid"] == [0, 0, 1, 1]
    assert client.post(url, json={"width": 120, "height": 80, "geometry": {"vertical": 10}}).status_code == 422


BAD_BOXES = [
    '{"width": 0, "height": 80}',
    '{"width": -5, "height": 80}',
    '{"width": "120", "height": 80}',
    '{"width": 120.5, "height": 80}',
    '{"width": true, "height": 80}',
    '{"width": 100000, "height": 80}',
    '{"width": 120, "height": 81}',  # 超过这张照片的像素数
    '{"width": 120, "height": 80, "geometry": {"straighten": "x"}}',
    '{"width": 120, "height": 80, "geometry": {"straighten": NaN}}',
    '{"width": 120, "height": 80, "geometry": {"straighten": Infinity}}',
    '{"width": 120, "height": 80, "geometry": {"nonsense": 1}}',
    '{"width": 120, "height": 80, "crop": {"rect": [NaN, 0, 1, 1]}}',
    '{"width": 120, "height": 80, "crop": {"rect": [0, 0, 1e309, 1]}}',
    '{"width": 120, "height": 80, "crop": {"rect": [0.5, 0, 0.8, 1]}}',
    '{"width": 120, "height": 80, "crop": {"rect": [0, 0, 0, 1]}}',
    '{"width": 120, "height": 80, "crop": {"rect": [0, 0, 1]}}',
    '{"width": 120, "height": 80, "crop": {"rect": ["0", 0, 1, 1]}}',
    '{"width": 120, "height": 80, "crop": {"rect": [true, 0, 1, 1]}}',
    '{"width": 120, "height": 80, "crop": {"preset": "16:10:2"}}',
    '{"width": 120, "height": 80, "crop": {"preset": 3}}',
    '{"width": 120, "height": 80, "crop": {"extra": 1}}',
    '{"width": 120, "height": 80, "crop": "free"}',
    '{"width": 120, "height": 80, "geometry": [1, 2]}',
]


@pytest.mark.parametrize("body", BAD_BOXES)
def test_box_rejects_bad_input_with_a_json_422(client, volumes, body):
    """复审 P2-01：非有限数、类型错、越界、超出照片像素 → 422 JSON，不会 500。"""
    _card(volumes)
    photo = _photo(client)
    r = client.post(f"/api/photos/{photo['id']}/gpu/box", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 422, (body, r.status_code, r.text)
    assert r.headers["content-type"].startswith("application/json") and "error" in r.json()


# ---- 照片文件：只读、拒绝链接和图库外、按已打开的 fd 发送（复审 P1-01）--------------------


def _replace_with_link(path, target, *, directory=False):
    path.rename(path.with_name(path.name + ".moved"))
    path.symlink_to(target, target_is_directory=directory)


@pytest.mark.parametrize("kind", ["raw", "camera-jpg"])
def test_photo_file_that_became_a_link_is_refused(client, volumes, settings, tmp_path, kind):
    _flag(settings)
    _card(volumes)
    photo = _photo(client)
    shot = client.app.state.ctx.lib.get_shot(photo["id"])
    path = client.app.state.ctx.lib.abspath(shot["files"]["dng" if kind == "raw" else "jpg"]["relpath"])
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"not a library file")
    _replace_with_link(path, outside)
    r = client.get(f"/api/photos/{photo['id']}/{kind}")
    assert r.status_code == 409 and r.json()["error"] == "photo_file_unavailable"
    assert b"not a library file" not in r.content


def test_photo_folder_that_became_a_link_is_refused_for_bytes_and_layout(client, volumes, settings, tmp_path):
    from gxr_studio.api import gpu_routes

    _flag(settings)
    folder = _card(volumes)
    photo = _photo(client)
    shot = client.app.state.ctx.lib.get_shot(photo["id"])
    path = client.app.state.ctx.lib.abspath(shot["files"]["dng"]["relpath"])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for f in folder.iterdir():  # 同名文件放在图库外面
        (elsewhere / f.name).write_bytes(f.read_bytes())
    gpu_routes._layouts.clear()
    _replace_with_link(path.parent, elsewhere, directory=True)
    assert client.get(f"/api/photos/{photo['id']}/raw").status_code == 409
    assert client.get(f"/api/photos/{photo['id']}/camera-jpg").status_code == 409
    info = client.get(f"/api/photos/{photo['id']}/gpu").json()
    assert info["raw"] is None and info["raw_reason"] == "refused:link"  # RAW 布局也不跟着链接去读


def test_open_media_refuses_paths_outside_the_library(tmp_path):
    from gxr_studio.library.media import MediaRefused, open_media

    root = tmp_path / "lib"
    (root / "a").mkdir(parents=True)
    (root / "a" / "x.jpg").write_bytes(b"x")
    lib = SimpleNamespace(root=root, is_local=lambda _p: True)
    for rel in ("../outside", "/etc/passwd", "a/../a/x.jpg", "a//x.jpg", "a\\x.jpg", "", "a/./x.jpg"):
        with pytest.raises(MediaRefused):
            open_media(lib, rel)
    with pytest.raises(MediaRefused):  # 图库根在卡上或受保护目录里
        open_media(SimpleNamespace(root=root, is_local=lambda _p: False), "a/x.jpg")
    with pytest.raises(MediaRefused):  # 不是普通文件
        open_media(lib, "a")


def test_open_media_refuses_a_file_replaced_by_a_link_at_open(tmp_path, monkeypatch):
    """核对之后、打开之前把文件换成链接：最后一级不跟随链接（POSIX 的 O_NOFOLLOW，Windows 拒绝重解析点），打开本身失败。"""
    from gxr_studio.library import media

    root = tmp_path / "lib"
    (root / "a").mkdir(parents=True)
    target = root / "a" / "x.dng"
    target.write_bytes(b"own")
    outside = tmp_path / "outside.dng"
    outside.write_bytes(b"outside")
    lib = SimpleNamespace(root=root, is_local=lambda _p: True)
    from gxr_studio import osplat

    real_open, swapped = osplat.open_file_at, []
    watched = os.fspath(target.resolve())

    def opening(dir_fd, name, **kw):  # 平台原语：两个平台都经过它
        if not swapped and name == "x.dng":
            swapped.append(1)
            os.rename(watched, watched + ".old")
            os.symlink(os.fspath(outside), watched)
        return real_open(dir_fd, name, **kw)

    monkeypatch.setattr(osplat, "open_file_at", opening)
    with pytest.raises(media.MediaRefused):
        media.open_media(lib, "a/x.dng")
    monkeypatch.undo()
    assert swapped


def test_lut_delete_reimport_and_space_change_get_a_new_table(client):
    """复审 r2 P1-02：删除、重新导入相同字节、改输入空间。版本号会从头再数，出图标识不能复用，表必须是新的。"""
    from gxr_studio.lut.photo import _apply
    from test_lut_library import upload

    v = np.linspace(0, 1, 17)
    nodes = np.clip(np.array([[r, g, b] for b in v for g in v for r in v]) * [0.86, 0.97, 1.05], 0, 1)
    cube = ("LUT_3D_SIZE 17\n" + "\n".join(" ".join(f"{c:.17g}" for c in row) for row in nodes) + "\n").encode()
    samples = np.array([[[64, 128, 192], [123, 45, 67], [219, 181, 3], [0, 255, 17]]], np.uint8)
    rows = []
    for space in ("srgb", "rec709", "srgb"):
        cid = upload(client, cube, name="r2.cube").json()["cube_id"]
        assert client.patch(f"/api/luts/{cid}", json={"input_space": space, "output_space": "srgb"}).status_code == 200
        r = client.get(f"/api/luts/{cid}/table")
        assert r.status_code == 200, r.text
        target, _item = client.app.state.ctx.render.luts.target(cid, persist=False)
        table = np.frombuffer(r.content, np.uint8).reshape(256, 256, 256, 4)
        np.testing.assert_array_equal(table[samples[..., 2], samples[..., 1], samples[..., 0], :3], _apply(target, samples))
        rows.append((json.loads(unquote(r.headers["x-gxr-lut"]))["key"], r.headers["etag"], client.app.state.ctx.render.luts.render_token(cid)))
        assert client.delete(f"/api/luts/{cid}").status_code == 200
    assert rows[0][0] != rows[1][0] and rows[0][1] != rows[1][1] and rows[0][2] != rows[1][2]  # 换了空间：标识、ETag 都变
    assert rows[0] == rows[2]  # 同样的内容 + 同样的规范：同一张表（标识按内容和规范算，不按版本号）


def test_render_token_follows_the_colour_spec_not_the_record_version(client):
    from test_lut_library import cube_bytes, upload

    luts = client.app.state.ctx.render.luts
    cid = upload(client, cube_bytes()).json()["cube_id"]
    assert client.patch(f"/api/luts/{cid}", json={"input_space": "srgb"}).status_code == 200
    first = luts.render_token(cid)
    assert client.patch(f"/api/luts/{cid}", json={"output_range": "legal"}).status_code == 200
    assert luts.render_token(cid) != first
    assert client.patch(f"/api/luts/{cid}", json={"output_range": "full"}).status_code == 200
    assert luts.render_token(cid) == first  # 版本号已经加了两次，规范回到原样，标识也回到原样
    target, item, token = luts.target_and_token(cid)
    assert token == luts.render_token(cid) and item["output_range"] == "full"
    assert client.delete(f"/api/luts/{cid}").status_code == 200
    assert luts.render_token(cid) != first  # 删除以后是单独的标识


def test_huge_range_numbers_do_not_crash_or_leak(client, volumes, settings, monkeypatch):
    """复审 r2 P2-01：5,000 位的 Range 起点原来让 int() 抛错，12 次请求 12 个 500、泄漏 12 个 fd。"""
    _flag(settings)
    folder = _card(volumes)
    photo = _photo(client)
    url = f"/api/photos/{photo['id']}/raw"
    client.get(url)  # 先暖一次（线程池等）
    from gxr_studio.api import gpu_routes

    actual_open, opened = gpu_routes.open_media, []

    def opening(*args, **kwargs):
        fd = actual_open(*args, **kwargs)
        st = os.fstat(fd)
        opened.append((fd, st.st_dev, st.st_ino))
        return fd

    monkeypatch.setattr(gpu_routes, "open_media", opening)
    for header in ("bytes=" + "9" * 5000 + "-", "bytes=0-" + "9" * 5000, "bytes=-" + "9" * 5000, "bytes=" + "9" * 21 + "-"):
        for _ in range(3):
            r = client.get(url, headers={"Range": header})
            assert r.status_code == 200 and r.content == (folder / "R0000001.DNG").read_bytes()  # 看不懂的 Range：发整个文件
    big = client.get(url, headers={"Range": "bytes=" + "9" * 20 + "-"})
    assert big.status_code == 416
    assert len(opened) == 13
    for fd, dev, ino in opened:
        try:
            st = os.fstat(fd)
        except OSError:
            continue
        assert (st.st_dev, st.st_ino) != (dev, ino), "响应结束后照片 fd 仍然打开"


def test_fd_is_closed_when_the_response_cannot_be_built(tmp_path, monkeypatch):
    from gxr_studio.library import media

    path = tmp_path / "f.bin"
    path.write_bytes(b"abc")
    fd = os.open(path, os.O_RDONLY)

    def broken(*_a):
        raise ValueError("broken")

    monkeypatch.setattr(media, "parse_range", broken)
    with pytest.raises(ValueError):
        media.FdResponse(fd, media_type="application/octet-stream", headers={}, range_header="bytes=0-1")
    with pytest.raises(OSError):
        os.fstat(fd)  # 已经关了


def test_raw_bytes_support_ranges_and_large_files(client, volumes, settings):
    _flag(settings)
    folder = _card(volumes)
    photo = _photo(client)
    shot = client.app.state.ctx.lib.get_shot(photo["id"])
    path = client.app.state.ctx.lib.abspath(shot["files"]["dng"]["relpath"])
    big = bytes(range(256)) * (5 * 4096 + 7)  # 约 5 MB：跨好几个 1 MB 的块
    path.write_bytes(big)
    url = f"/api/photos/{photo['id']}/raw"
    full = client.get(url)
    assert full.status_code == 200 and full.content == big and full.headers["accept-ranges"] == "bytes"
    assert int(full.headers["content-length"]) == len(big)
    part = client.get(url, headers={"Range": "bytes=1048570-2097160"})
    assert part.status_code == 206 and part.content == big[1048570:2097161]
    assert part.headers["content-range"] == f"bytes 1048570-2097160/{len(big)}"
    tail = client.get(url, headers={"Range": "bytes=-10"})
    assert tail.status_code == 206 and tail.content == big[-10:]
    open_end = client.get(url, headers={"Range": f"bytes={len(big) - 3}-"})
    assert open_end.status_code == 206 and open_end.content == big[-3:]
    bad = client.get(url, headers={"Range": f"bytes={len(big)}-"})
    assert bad.status_code == 416 and bad.headers["content-range"] == f"bytes */{len(big)}"
    assert client.get(url, headers={"Range": "bytes=-0"}).status_code == 416
    assert client.get(url, headers={"Range": "items=0-3"}).content == big  # 看不懂的 Range：发整个文件
    assert (folder / "R0000001.DNG").read_bytes() != big  # 改的是图库里的副本，卡上的文件没动


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
