"""S6：配方渲染器、烘焙、缓存、sidecar 编辑、预览/导出 API、DNG-only 缩略图（半分辨率）。

理光字节不在仓库里：相机数据现场从本机的固件打包目录提取到临时目录（找不到就跳过依赖它的测试）。
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import threading
from pathlib import Path
from urllib.parse import unquote

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import e0_support as t
from conftest import PORT
from gxr_studio.app import create_app
from gxr_studio.camdata import calib, store
from gxr_studio.devtools.synthetic_card import make_card
from gxr_studio.engine import colour, knobs, menu
from gxr_studio.library import thumbs
from gxr_studio.render import chain, tones
from gxr_studio.render.params import (
    Curves,
    EditError,
    EditState,
    RecipeSel,
    parse_adjust,
    parse_curve,
    parse_edit,
    parse_recipe,
)
from gxr_studio.render.recipes import CameraData, Menu, unit_of_lens
from gxr_studio.render.service import PREVIEW_STEP, RenderService, RenderUnavailable, shrink_codes
from test_api import find, import_all
from test_engine_render import models as models

ROOT = t.REPO
SAMPLE_DIR = ROOT / "captures/a12-50/2026-10-19/stage2c-first-run"
needs_sample = pytest.mark.skipif(not SAMPLE_DIR.is_dir(), reason="captures/a12-50 样本不在本机")


# ---- 夹具 --------------------------------------------------------------------


@pytest.fixture
def real_card(volumes):
    """盖掉 conftest 的同名夹具，换成本文件的样本目录；找不到样本就跳过，和 conftest 一样。"""
    if not SAMPLE_DIR.is_dir():
        pytest.skip("captures/a12-50 样本不在本机")
    return make_card(volumes, samples=SAMPLE_DIR)


@pytest.fixture(scope="module")
def camhome(tmp_path_factory):
    raw = t.fixture_dat(127)
    src = calib.dev_sources(t.REPO)
    if raw is None or not src:
        pytest.skip("相机数据夹具（g127 DAT 和标定文件）不在本机")
    h = tmp_path_factory.mktemp("s6-home")
    store.import_from_dat(raw, h)
    calib.import_calibration(h, src)
    return h


@pytest.fixture(scope="module")
def cam(camhome):
    return CameraData(camhome)


@pytest.fixture(scope="module")
def model200(camhome):
    return calib.model_for(camhome, 200)[0]


@pytest.fixture(scope="module")
def scene_codes():
    out = []
    for scene in t.SCENES:
        p = t.SCENE_DIR / f"{scene}_input.npz"
        if not p.is_file():
            pytest.skip("RAW 派生输入（knobs_v2_r2/local/*_input.npz）不在本机")
        with np.load(p, allow_pickle=False) as z:
            out.append(z["x"].copy())
    return np.concatenate(out)


@pytest.fixture
def home_with_data(settings, camhome):
    shutil.copytree(camhome, settings.home_dir)
    return settings.home_dir


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        yield c


@pytest.fixture
def data_client(home_with_data, client):
    return client


def sample_photos(client):
    import_all(client)
    return {n: find(client, n) for n in ("R8051144", "R8051145", "R8051146", "R8051147")}


# ---- 参数 --------------------------------------------------------------------


def test_param_validation_and_canonical_form():
    assert parse_adjust(None).is_identity and parse_adjust({}).is_identity
    a = parse_adjust({"exposure_ev": 1.5, "wb": {"warm": 10, "tint": -5}, "contrast": 20})
    assert (a.exposure_ev, a.warm, a.tint, a.contrast) == (1.5, 10, -5, 20)
    for bad in ({"exposure_ev": 9}, {"wb": {"warm": 101}}, {"contrast": 1.5}, {"nope": 1}, {"shadow": True}, [1]):
        with pytest.raises(EditError):
            parse_adjust(bad)
    assert parse_curve({"master": [[0, 0], [255, 255]]}) is None, "恒等直线等于没有曲线"
    assert parse_curve({"master": [[0, 0], [128, 100], [255, 255]]}) == Curves(master=((0, 0), (128, 100), (255, 255)))
    for bad in ({"master": [[0, 0]]}, {"master": [[0, 0], [0, 5]]}, {"master": [[0, 0], [300, 5]]}, {"red": []}):
        with pytest.raises(EditError):
            parse_curve(bad)
    r = parse_recipe({"unit": "jlaunch", "slot": 5, "levels": {"contrast": 2}})
    assert r == RecipeSel("jlaunch", 5, (("contrast", 2),))
    for bad in (
        {"unit": "x", "slot": 1},
        {"unit": "jlaunch", "slot": 9},
        {"unit": "jlaunch", "slot": 1, "levels": {"bogus": 1}},
        {"unit": "jlaunch", "slot": 1, "kind": "user"},
    ):
        with pytest.raises(EditError):
            parse_recipe(bad)
    s1 = EditState(parse_recipe({"unit": "jlaunch", "slot": 1}), a, None)
    assert s1.digest() == EditState(parse_recipe({"unit": "jlaunch", "slot": 1}), a, None).digest()
    assert parse_edit({}) == EditState()


def test_unit_from_lens():
    assert unit_of_lens("GR LENS A12 50mm F2.5 MACRO") == "jlaunch"
    assert unit_of_lens("GR LENS A12 28mm F2.5") == "qlaunch"
    assert unit_of_lens("A12 MOUNT") == "l08firm"
    assert unit_of_lens(None) is None and unit_of_lens("35mm Summicron") is None


# ---- 调子 --------------------------------------------------------------------


def test_tones_identity_and_monotone():
    assert tones.display_table(parse_adjust({}), None) is None
    assert tones.channel_gains(parse_adjust({})) is None
    assert tones.channel_gains(parse_adjust({"contrast": 50})) is None, "显示域调整不改输入增益"
    g = tones.channel_gains(parse_adjust({"exposure_ev": 1.0}))
    assert np.allclose(g, 2.0)
    warm = tones.channel_gains(parse_adjust({"wb": {"warm": 100}}))
    assert warm[0] > 1 > warm[2] and warm[1] == 1
    for c in (-100, 0, 100):
        for h in (-100, 100):
            for s in (-100, 100):
                tab = tones.display_table(parse_adjust({"contrast": c, "highlight": h, "shadow": s}), None)
                if tab is None:
                    continue
                assert np.all(np.diff(tab) >= -1e-4), (c, h, s)
                assert tab[0] <= 0.5 and tab[-1] >= 254.5, "黑白场不动"
    # 主曲线：按点走，端点外取平
    tab = tones.display_table(parse_adjust({}), Curves(master=((0.0, 10.0), (128.0, 128.0), (255.0, 250.0))))
    assert abs(tab[0] - 10) < 1e-3 and abs(tab[-1] - 250) < 1e-3
    x = np.array([0.0, 64.5, 255.0], np.float32)
    assert np.allclose(tones.apply_table(x, tab), np.interp(x, np.linspace(0, 255, tones.TABLE_SIZE), tab), atol=1e-3)


# ---- 配方与旋钮 ----------------------------------------------------------------


def test_menu_lists_recipes_without_camera_data():
    m = Menu()
    units = m.listing()
    assert [u["id"] for u in units] == ["jlaunch", "qlaunch", "l08firm"]
    assert all(len(u["recipes"]) == 8 for u in units)
    assert not any(u["calibrated"] for u in units) and units[1]["calibration_note"]
    bw = units[0]["recipes"][2]
    assert [c["name"] for c in bw["controls"]] == ["contrast", "highlight", "shadow", "fade", "filter"] and not bw["has_amount"]
    std = units[0]["recipes"][3]
    assert std["is_stock"] and not std["has_amount"] and std["entry_sha256"] is None
    r1 = units[0]["recipes"][0]
    assert r1["has_amount"] and r1["controls"][0]["amount_step"] == 25
    assert next(c for c in r1["controls"] if c["name"] == "saturation")["cap_upper"], "到顶标记要给到界面"


def test_resolve_matches_knobs_core_and_rejects_bad_levels(cam):
    data = menu.load()
    for unit_i, unit in enumerate(("jlaunch", "qlaunch", "l08firm")):
        for slot in range(1, 9):
            base = cam.entries[(unit_i, slot)]
            assert cam.resolve(RecipeSel(unit, slot)).entry == base, "默认档位 = 底子本身"
    sel = RecipeSel("jlaunch", 1, (("amount", 75), ("contrast", 3), ("saturation", -2)))
    res = cam.resolve(sel)
    levels = [75 if n == "amount" else 3 if n == "contrast" else -2 if n == "saturation" else 0 for n in res.levels]
    ref, _ = knobs.apply(cam.stock[0], cam.entries[(0, 1)], menu.request(data, 0, 1, levels))
    assert res.entry == ref and res.entry != cam.entries[(0, 1)] and len(res.entry) == 552
    assert res.levels["amount"] == 75 and res.levels["contrast"] == 3
    for bad, code in (
        (RecipeSel("jlaunch", 1, (("saturation", 7),)), "level_not_allowed"),  # 到顶标记：只到 +6
        (RecipeSel("jlaunch", 3, (("saturation", 1),)), "knob_not_available"),  # 03 黑白没有饱和
        (RecipeSel("jlaunch", 4, (("amount", 50),)), "knob_not_available"),  # 04 没有 Amount
        (RecipeSel("jlaunch", 1, (("contrast", 10),)), "level_not_allowed"),
    ):
        with pytest.raises(EditError) as e:
            cam.resolve(bad)
        assert e.value.code == code
    # 不在菜单里但取中性值的旋钮不算"动了"
    assert cam.resolve(RecipeSel("jlaunch", 3, (("saturation", 0), ("amount", 100)))).entry == cam.entries[(0, 3)]


# ---- 与 E0 的等价 -------------------------------------------------------------------


def test_exact_chain_with_default_params_equals_unit_predict(cam, camhome, scene_codes):
    for unit in range(3):
        model = calib.model_for(camhome, unit=store.UNITS[unit])[0]
        for slot in range(1, 9):
            e = cam.entries[(unit, slot)]
            ref = model.predict(e, scene_codes, quantize=True)
            got = chain.exact_u8(model, e, scene_codes, None, None)
            assert got.dtype == np.uint8 and np.array_equal(got, ref), (unit, slot)


def test_frozen_chain_still_reproduces_e0_golden_frames(cam, models):
    golden = json.loads((t.GOLDEN / "render_frames.json").read_text(encoding="utf-8"))["frames"]
    data = menu.load()
    inputs = {}
    for scene in t.SCENES:
        p = t.SCENE_DIR / f"{scene}_input.npz"
        if not p.is_file():
            pytest.skip("RAW 派生输入不在本机")
        with np.load(p, allow_pickle=False) as z:
            inputs[scene] = z["x"].copy()
    names = ("jlaunch", "qlaunch", "l08firm")
    n = 0
    for f in golden[::12]:
        unit, slot = names.index(f["unit"]), f["slot"]
        r = next(r for r in data["recipes"] if r["unit"] == unit and r["slot"] == slot)
        items = data["items"][r["first_item"] : r["first_item"] + r["item_count"]]
        lv = {data["controls"][i["control"]]["name"]: i["default_level"] for i in items}
        lv[f["param"]] = f["level"]
        res = cam.resolve(RecipeSel(f["unit"], slot, tuple(lv.items())))
        assert res.entry_sha256 == f["entry_sha256"]
        rgb = chain.exact_u8(models[200], res.entry, inputs[f["scene"]], None, None)
        assert hashlib.sha256(rgb.tobytes()).hexdigest() == f["display_RGB_sha256"], f
        n += 1
    assert n >= 40


def test_exposure_is_input_gain_before_calibration_and_clamp(cam, model200, scene_codes):
    e = cam.entries[(0, 6)]
    x = scene_codes[::7]
    g = tones.channel_gains(parse_adjust({"exposure_ev": 1.0}))
    got = chain.exact_u8(model200, e, x, g, None)
    ref = model200.predict(e, x * 2.0, quantize=True)
    assert np.array_equal(got, ref)
    # 输入残差有跨通道项：先乘曝光增益，再由模型映射、夹紧，不能提前逐通道截断。
    bright = np.full((4, 3), 4000.0)
    assert np.array_equal(
        chain.exact_u8(model200, e, bright, tones.channel_gains(parse_adjust({"exposure_ev": 3.0})), None),
        model200.predict(e, bright * 8.0, quantize=True),
    )


def test_identity_curve_and_tone_are_byte_identical_to_no_curve(cam, model200, scene_codes):
    e = cam.entries[(0, 2)]
    x = scene_codes[::11]
    plain = chain.exact_u8(model200, e, x, None, None)
    ident = tones.display_table(parse_adjust({}), parse_curve({"master": [[0, 0], [100, 100], [255, 255]]}))
    assert ident is None and np.array_equal(chain.exact_u8(model200, e, x, None, ident), plain)
    curved = tones.display_table(parse_adjust({}), Curves(master=((0, 0), (128, 90), (255, 255))))
    out = chain.exact_u8(model200, e, x, None, curved)
    assert not np.array_equal(out, plain) and out.mean() < plain.mean(), "把中间调压暗的曲线要让图变暗"


# ---- 烘焙 --------------------------------------------------------------------------


def _de(a, b):
    return colour.delta_e00(colour.lab_from_codes(a), colour.lab_from_codes(b))


@pytest.mark.parametrize("iso", (200, 1600))
def test_chroma_bake_error_budget_on_all_24_recipes(cam, camhome, scene_codes, iso):
    """烘焙对精确链：p99 ΔE00 ≤ 0.3（未量化的浮点输出比；8 位输出再加最多 ±1 码的取整翻转）。"""
    x = np.concatenate([scene_codes[::9], t_synthetic()])
    worst_p99 = worst_code = 0.0
    for unit in range(3):
        model = calib.model_for(camhome, iso, unit=store.UNITS[unit])[0]
        for slot in range(1, 9):
            e = cam.entries[(unit, slot)]
            ref = chain.exact_float(model, e, x, None, None)
            got = chain.chroma_float(x, chain.bake_chroma(model, e, PREVIEW_STEP), None, None)
            worst_p99 = max(worst_p99, float(np.percentile(_de(ref, got), 99)))
            worst_code = max(worst_code, float(np.abs(chain.quantize(ref).astype(int) - chain.quantize(got).astype(int)).max()))
    assert worst_p99 <= 0.3, worst_p99
    assert worst_code <= 3, worst_code


def t_synthetic():
    from gxr_studio.engine.model import synthetic

    return synthetic()


def test_chroma_bake_with_gains_table_and_precision_grid(cam, model200, scene_codes):
    e = cam.entries[(0, 1)]
    x = scene_codes[::5]
    g = tones.channel_gains(parse_adjust({"exposure_ev": -0.7, "wb": {"warm": 40, "tint": -20}}))
    tab = tones.display_table(parse_adjust({"contrast": 30, "shadow": 40}), Curves(master=((0, 0), (80, 70), (255, 255))))
    ref = chain.exact_float(model200, e, x, g, tab)
    coarse = chain.chroma_float(x, chain.bake_chroma(model200, e, PREVIEW_STEP), g, tab)
    fine = chain.chroma_float(x, chain.bake_chroma(model200, e, PREVIEW_STEP / 2), g, tab)
    assert np.percentile(_de(ref, coarse), 99) <= 0.3
    assert np.percentile(_de(ref, fine), 99) <= np.percentile(_de(ref, coarse), 99)
    assert np.array_equal(
        chain.chroma_u8(x, chain.bake_chroma(model200, e, PREVIEW_STEP), g, tab),
        chain.quantize(chain.chroma_float(x, chain.bake_chroma(model200, e, PREVIEW_STEP), g, tab)),
    )


def test_neutral_axis_stays_neutral_through_the_bake(cam, model200):
    e = cam.entries[(0, 4)]
    ramp = np.repeat(np.geomspace(5, 4000, 200)[:, None], 3, axis=1)
    got = chain.chroma_u8(ramp, chain.bake_chroma(model200, e, PREVIEW_STEP), None, None).astype(int)
    ref = model200.predict(e, ramp, quantize=True).astype(int)
    assert np.abs(got - ref).max() <= 1


def test_lut3d_comparison_path_runs_and_is_worse_than_chroma(cam, model200, scene_codes):
    e = cam.entries[(0, 1)]
    x = scene_codes[::17]
    ref = chain.exact_float(model200, e, x, None, None)
    b3 = chain.bake_model(model200, e, 33)
    got3 = chain.apply_baked_float(x, b3, b3.folded(None), None)
    got2 = chain.chroma_float(x, chain.bake_chroma(model200, e, PREVIEW_STEP), None, None)
    assert np.percentile(_de(ref, got3), 99) > np.percentile(_de(ref, got2), 99)
    assert chain.apply_baked(x, b3, b3.folded(None), None).shape == x.shape


# ---- 服务：缓存、状态、标签 -------------------------------------------------------------


def fake_shot(lib, rel, sha, iso=200, lens="GR LENS A12 50mm F2.5 MACRO"):
    return {
        "shot_id": "x",
        "name": Path(rel).stem,
        "iso": iso,
        "lens": lens,
        "orientation": 1,
        "files": {"dng": {"relpath": rel, "sha256": sha}},
    }


@needs_sample
def test_service_caches_and_labels(lib, camhome):
    rel = "2026/2026-10-16/R8051144.DNG"
    (lib.root / rel).parent.mkdir(parents=True)
    shutil.copyfile(SAMPLE_DIR / "R8051144.DNG", lib.root / rel)
    sha = hashlib.sha256((lib.root / rel).read_bytes()).hexdigest()
    svc = RenderService(camhome, lib)
    shot = fake_shot(lib, rel, sha, iso=200)
    e0 = EditState()
    a = svc.render_preview(shot, e0)
    assert not a.cache_hit and svc.stats == {"decodes": 1, "bakes": 1, "renders": 1, "result_hits": 0}
    assert a.plan.calibrated and a.plan.label == "电脑预测" and a.plan.defaulted and a.plan.slot == 4
    b = svc.render_preview(shot, e0)
    assert b.cache_hit and b.jpeg == a.jpeg and svc.stats["renders"] == 1 and svc.stats["result_hits"] == 1
    # 只动曝光：不重新解码、不重新烘焙（曲线与增益在链的两端）
    adj = EditState(None, parse_adjust({"exposure_ev": 0.5}), None)
    c = svc.render_preview(shot, adj)
    assert not c.cache_hit and svc.stats["decodes"] == 1 and svc.stats["bakes"] == 1 and c.key != a.key
    # 换配方：重新烘焙，不重新解码
    d = svc.render_preview(shot, EditState(RecipeSel("jlaunch", 5), parse_adjust({}), None))
    assert svc.stats["decodes"] == 1 and svc.stats["bakes"] == 2 and d.key != a.key
    # 更小的目标宽度：另一个键，同一份解码
    e = svc.render_preview(shot, e0, max_edge=400)
    assert max(e.width, e.height) == 400 and svc.stats["decodes"] == 1 and e.key != a.key
    assert (a.width, a.height) == (2144, 1424), "预览是半分辨率"
    # 标签：非 200/1600 的 ISO、28mm 都标"未标定"
    assert svc.plan(fake_shot(lib, rel, sha, iso=400), e0).label == "电脑预测 未校准"
    p28 = svc.plan(fake_shot(lib, rel, sha, iso=1600, lens="GR LENS A12 28mm F2.5"), EditState(RecipeSel("qlaunch", 2)))
    assert not p28.calibrated and p28.model_iso == 1600 and p28.reasons == ("镜头单元、配方单元或 ISO 不在实拍校准范围",)
    assert svc.plan(fake_shot(lib, rel, sha, iso=1600), e0).calibrated
    # 配方与镜头单元不一致也要说
    assert not svc.plan(fake_shot(lib, rel, sha, iso=200), EditState(RecipeSel("qlaunch", 1))).calibrated


@needs_sample
def test_default_params_preview_equals_exact_chain_within_bake_budget(lib, camhome):
    rel = "2026/2026-10-16/R8051144.DNG"
    (lib.root / rel).parent.mkdir(parents=True)
    shutil.copyfile(SAMPLE_DIR / "R8051144.DNG", lib.root / rel)
    sha = hashlib.sha256((lib.root / rel).read_bytes()).hexdigest()
    svc = RenderService(camhome, lib)
    shot = fake_shot(lib, rel, sha)
    fast = np.asarray(Image.open(io.BytesIO(svc.render_preview(shot, EditState(), max_edge=240, quality=100).jpeg)))
    slow = np.asarray(Image.open(io.BytesIO(svc.render_preview(shot, EditState(), max_edge=240, exact=True, quality=100).jpeg)))
    assert fast.shape == slow.shape
    # 与 E0 的 predict 逐字节相同的是精确链本身（见上面的等价测试）；烘焙与它的差在 JPEG 噪声之内
    assert np.abs(fast.astype(int) - slow.astype(int)).mean() < 1.0
    codes = svc._proxy(shot, 240)
    model = svc.model(200)
    ref = model.predict(svc.plan(shot, EditState()).entry, codes.reshape(-1, 3), quantize=True).reshape(codes.shape)
    assert np.array_equal(chain.exact_u8(model, svc.plan(shot, EditState()).entry, codes, None, None), ref)


def test_unavailable_without_camera_data(lib, tmp_path):
    svc = RenderService(tmp_path / "empty-home", lib)
    st = svc.status()
    assert not st["ready"] and st["camera_data"]["state"] == "missing" and st["hint"] == "还没有导入相机配方文件"
    assert svc.catalog()[0]["recipes"][0]["entry_sha256"] is None
    with pytest.raises(RenderUnavailable) as e:
        svc.plan({"files": {}}, EditState())
    assert e.value.code == "camera_data_missing" and "导入" in e.value.message


def test_calibration_missing_is_reported_separately(lib, camhome, tmp_path):
    h = tmp_path / "h"
    shutil.copytree(camhome / "camera-data", h / "camera-data")
    svc = RenderService(h, lib)
    st = svc.status()
    assert not st["ready"] and st["camera_data"]["state"] == "ready" and st["calibration"]["state"] == "missing"
    with pytest.raises(RenderUnavailable) as e:
        svc.plan({"files": {}}, EditState())
    assert e.value.code == "calibration_missing"


def test_tampered_camera_data_is_refused(lib, camhome, tmp_path):
    h = tmp_path / "h"
    shutil.copytree(camhome, h)
    f = h / "camera-data/units/jlaunch/recipes/01.entry"
    b = bytearray(f.read_bytes())
    b[100] ^= 1
    f.write_bytes(bytes(b))
    st = RenderService(h, lib).status()
    assert st["camera_data"]["state"] == "invalid" and not st["ready"]


def test_shrink_codes_keeps_linear_values():
    x = np.full((100, 200, 3), 1234.5, np.float32)
    s = shrink_codes(x, 50)
    assert s.shape == (25, 50, 3) and np.allclose(s, 1234.5, atol=0.01)
    assert shrink_codes(x, None) is x and shrink_codes(x, 500) is x


# ---- API -----------------------------------------------------------------------------


def test_api_without_camera_data_gives_clear_status(client, real_card):
    photos = sample_photos(client)
    st = client.get("/api/render/status").json()
    assert not st["ready"] and st["camera_data"]["state"] == "missing"
    rec = client.get("/api/recipes").json()
    assert len(rec["units"]) == 3 and not rec["status"]["ready"]
    p = photos["R8051144"]
    r = client.get(f"/api/photos/{p['id']}/render")
    assert r.status_code == 409 and r.json()["error"] == "camera_data_missing"
    assert r.json()["message"] == "无法生成电脑预测。请先导入相机配方文件。" and r.json()["status"]["hint"]
    # 编辑仍能存取（档位按菜单表校验），只是没法预览
    put = client.put(f"/api/photos/{p['id']}/edit", json={"adjust": {"exposure_ev": 0.3}})
    assert put.status_code == 200 and put.json()["render"]["available"] is False
    # DNG 导出退回中性转换，和 S4 一样
    out = client.post(f"/api/photos/{p['id']}/export", json={"size": "small", "source": "neutral"}).json()
    assert out["render"] == "neutral-preview" and out["prediction"] is False


@needs_sample
def test_api_edit_roundtrip_and_validation(data_client, real_card):
    c = data_client
    p = sample_photos(c)["R8051144"]
    base = c.get(f"/api/photos/{p['id']}/edit").json()
    assert base["rev"] == 0 and base["recipe"] is None and base["render"]["defaulted"] and base["render"]["slot"] == 4
    assert base["scope"]["computer_only"] == ["adjust", "curve", "crop", "lut"]
    body = {
        "recipe": {"kind": "builtin", "unit": "jlaunch", "slot": 1, "levels": {"amount": 75, "contrast": 3}},
        "adjust": {"exposure_ev": 0.5, "wb": {"warm": 10}},
        "curve": {"master": [[0, 0], [128, 120], [255, 255]]},
    }
    r = c.put(f"/api/photos/{p['id']}/edit", json={**body, "expected_rev": 0})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["rev"] == 1 and got["recipe"]["levels"] == {"amount": 75, "contrast": 3}
    assert got["render"]["effective_levels"]["saturation"] == 0 and got["render"]["available"]
    assert c.get(f"/api/photos/{p['id']}/edit").json()["adjust"]["wb"]["warm"] == 10
    # 版本冲突
    assert c.put(f"/api/photos/{p['id']}/edit", json={"adjust": None, "expected_rev": 0}).status_code == 409
    # 只改 adjust：recipe 和 curve 保留
    r2 = c.put(f"/api/photos/{p['id']}/edit", json={"adjust": {"contrast": 10}}).json()
    assert r2["recipe"]["slot"] == 1 and r2["curve"] and r2["adjust"]["exposure_ev"] == 0 and r2["rev"] == 2
    # 裁切写入不丢编辑，编辑写入不丢裁切
    c.put(f"/api/photos/{p['id']}/crop", json={"preset": "xpan-65-24"})
    side = json.loads(next((Path(c.get("/api/settings").json()["library_dir"])).rglob("R8051144.gxrs.json")).read_text(encoding="utf-8"))
    assert side["recipe"]["slot"] == 1 and side["crop"]["preset"] == "xpan-65-24" and side["adjust"]["contrast"] == 10
    # 校验：不合法的档位、没有的旋钮、单元不一致
    for bad, code in (
        ({"recipe": {"unit": "jlaunch", "slot": 1, "levels": {"saturation": 7}}}, "level_not_allowed"),
        ({"recipe": {"unit": "jlaunch", "slot": 3, "levels": {"saturation": 1}}}, "knob_not_available"),
        ({"recipe": {"unit": "qlaunch", "slot": 1}}, "unit_mismatch"),
        ({"adjust": {"exposure_ev": 99}}, "bad_adjust"),
        ({"curve": {"master": [[5, 5]]}}, "bad_curve"),
    ):
        r = c.put(f"/api/photos/{p['id']}/edit", json=bad)
        assert r.status_code == 422 and r.json()["error"] == code, (bad, r.text)
    assert c.get(f"/api/photos/{p['id']}/edit").json()["rev"] == 3, "被拒绝的写入不能改动 sidecar"
    # 还原
    gone = c.delete(f"/api/photos/{p['id']}/edit").json()
    assert gone["recipe"] is None and gone["curve"] is None and gone["adjust"]["contrast"] == 0
    assert find(c, "R8051144")["crop"]["preset"] == "xpan-65-24"


@needs_sample
def test_api_concurrent_writers_conflict_on_rev(data_client, real_card):
    c = data_client
    p = sample_photos(c)["R8051145"]
    results = []

    def put(ev):
        results.append(c.put(f"/api/photos/{p['id']}/edit", json={"adjust": {"exposure_ev": ev}, "expected_rev": 0}).status_code)

    ts = [threading.Thread(target=put, args=(v,)) for v in (0.5, 1.0)]
    [x.start() for x in ts]
    [x.join() for x in ts]
    assert sorted(results) == [200, 409]


@needs_sample
def test_api_render_preview_cache_headers_and_live_override(data_client, real_card):
    c = data_client
    p = sample_photos(c)["R8051144"]
    r = c.get(f"/api/photos/{p['id']}/render")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    im = Image.open(io.BytesIO(r.content))
    assert im.size == (2144, 1424) and im.info.get("icc_profile")
    assert r.headers["X-GXR-Prediction"] == "1" and r.headers["X-GXR-Calibrated"] == "0"  # 样本 ISO 400
    assert unquote(r.headers["X-GXR-Label"]) == "prediction" and r.headers["X-GXR-Cache"] == "miss"
    again = c.get(f"/api/photos/{p['id']}/render")
    assert again.headers["X-GXR-Cache"] == "hit" and again.content == r.content
    assert c.get(f"/api/photos/{p['id']}/render", headers={"If-None-Match": r.headers["ETag"]}).status_code == 304
    # 实时预览：不落盘
    live = c.post(f"/api/photos/{p['id']}/render", json={"adjust": {"exposure_ev": 1.0}}, params={"max_edge": 400})
    assert live.status_code == 200 and max(Image.open(io.BytesIO(live.content)).size) == 400
    assert c.get(f"/api/photos/{p['id']}/edit").json()["rev"] == 0
    live_recipe = c.post(f"/api/photos/{p['id']}/render", json={"recipe": {"unit": "jlaunch", "slot": 6}}, params={"max_edge": 400})
    assert live_recipe.headers["X-GXR-Render-Key"] != live.headers["X-GXR-Render-Key"]
    # 带裁切的预览
    c.put(f"/api/photos/{p['id']}/crop", json={"preset": "xpan-65-24"})
    cr = c.get(f"/api/photos/{p['id']}/render", params={"crop": True})
    assert Image.open(io.BytesIO(cr.content)).size[1] < 1424 and cr.headers["X-GXR-Render-Key"] != r.headers["X-GXR-Render-Key"]
    # 错误
    assert c.get(f"/api/photos/{p['id']}/render", params={"max_edge": 10}).json()["error"] == "bad_size"
    assert c.post(f"/api/photos/{p['id']}/render", json={"adjust": {"contrast": 500}}).json()["error"] == "bad_adjust"
    assert c.get("/api/photos/nope/render").status_code == 404


@needs_sample
def test_api_render_requires_raw_and_recipes_listing(data_client, real_card):
    c = data_client
    import_all(c)
    rec = c.get("/api/recipes").json()
    assert rec["status"]["ready"] and rec["units"][0]["recipes"][0]["entry_sha256"]
    # 三个单元里有些底子的条目字节相同（例如 04 之外的相同槽位），所以不是 24 个都不同
    assert len({r["entry_sha256"] for u in rec["units"] for r in u["recipes"]}) >= 10
    # 只有 JPG 的照片没有 RAW：不能套配方
    jpg_only = [p for p in c.get("/api/photos").json() if not p["has_raw"]]
    assert jpg_only, "假卡里有只有 JPG 的照片"
    # 合同修订：只有 JPEG 时，不点配方也能出图；点了内置配方则是近似，或明确拒绝。
    plain = c.get(f"/api/photos/{jpg_only[0]['id']}/render")
    assert plain.status_code == 200 and plain.content[:2] == b"\xff\xd8"
    cap = c.get(f"/api/photos/{jpg_only[0]['id']}/capabilities").json()
    assert cap["raw"] is False and cap["recipe"] in {"approx", "none"}
    if cap["recipe"] == "none":
        assert cap["reason"] == "配方需要 RAW 文件"
    asked = c.post(f"/api/photos/{jpg_only[0]['id']}/render", json={"recipe": {"unit": "jlaunch", "slot": 8}})
    if cap["recipe"] == "none":
        assert asked.status_code == 409 and asked.json()["error"] == "no_raw"
    else:
        assert asked.status_code == 200 and asked.headers.get("X-GXR-Look") == "approx"


@needs_sample
def test_api_export_prediction_names_labels_and_sources(data_client, real_card):
    c = data_client
    p = sample_photos(c)["R8051146"]
    # 没有编辑、有相机 JPG：照旧导出相机 JPG
    plain = c.post(f"/api/photos/{p['id']}/export", json={"size": "small"}).json()
    assert plain["render"] == "camera-jpg" and plain["prediction"] is False
    # 有编辑：从 RAW 渲染，文件名和标签写明电脑预测
    c.put(f"/api/photos/{p['id']}/edit", json={"recipe": {"unit": "jlaunch", "slot": 5}, "adjust": {"exposure_ev": 0.3}})
    c.put(f"/api/photos/{p['id']}/crop", json={"preset": "xpan-65-24"})
    out = c.post(f"/api/photos/{p['id']}/export", json={"size": "small"}).json()
    assert out["prediction"] is True and out["render"] == "recipe-prediction-uncalibrated" and out["calibrated"] is False
    name = Path(out["path"]).name
    assert name.startswith("R8051146_gxr") and name.endswith(".jpg")
    assert "电脑预测" not in name and "未校准" not in name and "近似" not in name
    assert out["render_label"] == "prediction" and out["render_text"].startswith("电脑预测")
    assert (out["width"], out["height"]) == (1600, round(1600 * 1583 / 4288))
    ex = Image.open(out["path"]).getexif()
    desc = ex[270].encode("latin-1").decode("utf-8")
    assert "computer prediction" in desc and "NOT calibrated" in desc and "未校准" in desc and ex[274] == 1
    assert Image.open(out["path"]).info.get("icc_profile")
    # 明确要相机 JPG / 中性转换
    assert c.post(f"/api/photos/{p['id']}/export", json={"size": "small", "source": "camera-jpg"}).json()["render"] == "camera-jpg"
    assert c.post(f"/api/photos/{p['id']}/export", json={"size": "small", "source": "neutral"}).json()["render"] == "neutral-preview"
    assert c.post(f"/api/photos/{p['id']}/export", json={"source": "bogus"}).json()["error"] == "bad_source"
    # 导出的是配方效果：同一张图的颜色和相机 JPG 明显不同，也不等于中性转换
    rec_im = np.asarray(Image.open(out["path"]).convert("RGB")).astype(float)
    neu = c.post(f"/api/photos/{p['id']}/export", json={"size": "small", "source": "neutral"}).json()
    neu_im = np.asarray(Image.open(neu["path"]).convert("RGB")).astype(float)
    assert rec_im.shape == neu_im.shape and np.abs(rec_im - neu_im).mean() > 5


@needs_sample
def test_api_dng_only_exports_default_look_and_full_res_is_fast(data_client, real_card):
    c = data_client
    (real_card.root / "DCIM" / "100RICOH" / "R8051145.JPG").unlink()
    p = sample_photos(c)["R8051145"]
    assert p["has_raw"] and not p["has_jpg"]
    import time

    t0 = time.time()
    out = c.post(f"/api/photos/{p['id']}/export", json={"size": "original"}).json()
    dt = time.time() - t0
    assert out["prediction"] is True and out["render"].startswith("recipe-prediction") and (out["width"], out["height"]) == (4288, 2848)
    assert dt < 15, f"全分辨率导出 {dt:.1f}s"
    assert "stock 04 simulation" in Image.open(out["path"]).getexif()[270]


@needs_sample
def test_dng_only_thumbnail_and_preview_use_half_resolution(data_client, real_card, settings):
    c = data_client
    (real_card.root / "DCIM" / "100RICOH" / "R8051145.JPG").unlink()
    p = sample_photos(c)["R8051145"]
    th = Image.open(io.BytesIO(c.get(p["thumb_url"]).content))
    pv = Image.open(io.BytesIO(c.get(p["preview_url"]).content))
    assert max(th.size) == 256 and abs(th.width / th.height - 3 / 2) < 0.02, "不再是 DNG 里 160×120 的 4:3 小图"
    assert max(pv.size) == 1600 and pv.size[0] > 1000
    cached = sorted(x.name for x in (Path(settings.library_dir) / ".studio/thumbs").rglob("*.jpg"))
    assert any("-256-r" in n for n in cached), cached  # 带相机数据的 04 模拟变体
    # 相机数据变了（这里：删掉）→ 缩略图变体换了，退回中性转换
    shutil.rmtree(settings.home_dir / "camera-data")
    th2 = Image.open(io.BytesIO(c.get(p["thumb_url"]).content))
    assert max(th2.size) == 256
    assert any(n.endswith("-256-n.jpg") for n in (x.name for x in (Path(settings.library_dir) / ".studio/thumbs").rglob("*.jpg")))


def test_dng_only_with_unreadable_raw_still_falls_back_to_embedded_preview(client, card):
    import_all(client)
    p = find(client, "R0000109", "2026-10-19")
    assert p["preview_kind"] == "dng-preview" and client.get(p["thumb_url"]).status_code == 200
    # 退回的内嵌小预览不是电脑预测：响应头如实标成 embedded，没有预测标记
    r = client.get(p["thumb_url"])
    assert r.headers["X-GXR-Look"] == "embedded" and "X-GXR-Prediction" not in r.headers
    assert "内嵌" in unquote(r.headers["X-GXR-Label"])
    # 照片列表也认得出来（导入时就生成过缩略图）：不会标成"电脑预测"
    assert p["preview_look"] == {"kind": "embedded", "label": thumbs.EMBEDDED_LABEL, "prediction": False}


@needs_sample
def test_dng_only_picture_is_labelled_as_a_computer_prediction(data_client, real_card, settings):
    """P1-2：只有 RAW 的照片，导入相机数据后缩略图/大图是原厂 04 的电脑模拟——必须标"电脑预测"，不能当成照片本身。"""
    c = data_client
    (real_card.root / "DCIM" / "100RICOH" / "R8051145.JPG").unlink()
    photos = sample_photos(c)
    p, jpg = photos["R8051145"], photos["R8051144"]
    assert p["preview_kind"] == "dng-preview"
    look = p["preview_look"]
    assert look["kind"] == "stock04-prediction" and look["prediction"] is True and look["label"] in {"电脑预测", "电脑预测 未校准"}
    for url in (p["thumb_url"], p["preview_url"]):
        r = c.get(url)
        assert r.status_code == 200 and r.headers["X-GXR-Prediction"] == "1" and r.headers["X-GXR-Look"] == "stock04-prediction"
        assert unquote(r.headers["X-GXR-Label"]) == look["label"]
    # 配对了相机 JPG 的照片：画面就是相机 JPG，不标预测
    assert jpg["preview_look"] == {"kind": "camera-jpg", "label": None, "prediction": False}
    r = c.get(jpg["thumb_url"])
    assert "X-GXR-Prediction" not in r.headers and "X-GXR-Look" not in r.headers
    # 相机数据没了 → 中性转换，如实标"不是相机的颜色"，也不算预测
    shutil.rmtree(settings.home_dir / "camera-data")
    n = find(c, "R8051145")
    assert n["preview_look"]["kind"] == "neutral" and n["preview_look"]["prediction"] is False
    assert n["preview_look"]["label"] == "中性转换" and n["thumb_url"] != p["thumb_url"]
    r = c.get(n["thumb_url"])
    assert r.headers["X-GXR-Look"] == "neutral" and "X-GXR-Prediction" not in r.headers


def test_precise_export_uses_the_exact_chain_and_matches_baked_closely(lib, camhome, tmp_path):
    from gxr_studio.export import ExportOptions, export_shot
    from gxr_studio.raw import demosaic_full, open_dng, render_full
    from test_raw_export import scene_dng

    rel = "2026/2026-10-16/R0000301.DNG"
    path = lib.root / rel
    path.parent.mkdir(parents=True)
    path.write_bytes(scene_dng(w=96, h=64))
    shot = {
        "shot_id": "s301",
        "name": "R0000301",
        "iso": 200,
        "lens": "GR LENS A12 50mm F2.5 MACRO",
        "orientation": 1,
        "files": {"dng": {"relpath": rel, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}},
    }
    svc = RenderService(camhome, lib)
    edit = EditState(RecipeSel("jlaunch", 6, (("contrast", 2),)), parse_adjust({"exposure_ev": 0.4}), None)
    exact_r, baked_r = svc.renderer(shot, edit, precise=True), svc.renderer(shot, edit)
    assert exact_r.calibrated and exact_r.filename_tag == "" and "exact chain" in exact_r.description
    dng = open_dng(path)
    a, b = render_full(dng, exact_r), render_full(dng, baked_r)
    ref = chain.exact_u8(svc.model(200), svc.plan(shot, edit).entry, demosaic_full(dng), tones.channel_gains(edit.adjust), None)
    assert np.array_equal(a, ref) and np.abs(a.astype(int) - b.astype(int)).max() <= 2
    res = export_shot(lib, shot, None, tmp_path / "o", ExportOptions(None, 90), [], exact_r, True)
    assert res.path.name == "R0000301_gxr.jpg" and res.render == "recipe-prediction"
    assert "computer prediction" in Image.open(res.path).getexif()[270]
