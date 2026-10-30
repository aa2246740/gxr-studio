"""S8a：LUT 导入 → 路线 1 旋钮拟合（读取、色样、拟合找回、取消、API、我的配方）。

理光字节不在仓库里：相机数据现场从本机固件打包目录提取到临时目录（找不到就跳过依赖它的测试）。
拟合子进程（`python -m gxr_studio.lut.worker`）在 API 测试里真的会被启动；测试结束时都会被结束。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from scipy.interpolate import LinearNDInterpolator
from scipy.spatial import cKDTree

import e0_support as t
from conftest import PORT
from gxr_studio.app import create_app
from gxr_studio.camdata import calib, store
from gxr_studio.engine import colour
from gxr_studio.lut import fit, scene, spaces
from gxr_studio.lut.cube import Cube, write_cube
from gxr_studio.render.params import RecipeSel
from gxr_studio.render.recipes import CameraData
from test_api import find, import_all

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DIR = ROOT / "captures/a12-50/2026-10-19/stage2c-first-run"
needs_sample = pytest.mark.skipif(not SAMPLE_DIR.is_dir(), reason="captures/a12-50 样本不在本机")
LUT_PKG = Path(__file__).resolve().parents[1] / "src/gxr_studio/lut"


# ---- 夹具 --------------------------------------------------------------------


@pytest.fixture(scope="module")
def camhome(tmp_path_factory):
    raw = t.fixture_dat(127)
    src = calib.dev_sources(t.REPO)
    if raw is None or not all(p.is_file() for pair in src.values() for p in pair):
        pytest.skip("相机数据夹具（g127 DAT 和标定文件）不在本机")
    h = tmp_path_factory.mktemp("s8a-home")
    store.import_from_dat(raw, h)
    calib.import_calibration(h, src)
    return h


@pytest.fixture(scope="module")
def cam(camhome):
    return CameraData(camhome)


@pytest.fixture(scope="module")
def model(camhome):
    return calib.model_for(camhome, 200)[0]


@pytest.fixture(scope="module")
def samples(model, cam):
    return scene.build_samples(model, cam.stock[0], "srgb")


class FnTarget:
    """已知答案的"理想输出"：不经过 .cube，用来证明算法在可表达的目标上是精确的。"""

    def __init__(self, ideal):
        self.ideal = ideal

    def render(self, stock_rgb, xyz):
        return self.ideal


def known(cam, slot, levels):
    full = {c["name"]: c["default"] for c in cam.controls(0, slot)}
    full.update(levels)
    return full, cam.resolve(RecipeSel("jlaunch", slot, tuple(sorted(full.items())))).entry


def cube_from_recipe(model, stock04, entry, path, size=33, seed=7):
    """用"底子 + 旋钮"生成一个真的 .cube：04 的输出（输入）→ 候选的输出（目标）。散点插值到 size³ 格。"""
    rng = np.random.default_rng(seed)
    base = np.exp(rng.uniform(np.log(40), np.log(3800), (60000, 1)))
    x = np.clip(base * np.exp(rng.normal(0, 0.5, (60000, 3))), 8, 4095)
    x = np.vstack([x, np.repeat(np.geomspace(3, 4095, 400)[:, None], 3, axis=1)])
    src = model.predict(stock04, x) / 255.0
    dst = model.predict(entry, x) / 255.0
    grid = np.linspace(0, 1, size)
    b, g, r = np.meshgrid(grid, grid, grid, indexing="ij")
    nodes = np.stack([r, g, b], axis=-1).reshape(-1, 3)
    out = LinearNDInterpolator(src, dst)(nodes)
    miss = np.isnan(out).any(axis=1)
    if miss.any():
        out[miss] = dst[cKDTree(src).query(nodes[miss])[1]]
    write_cube(path, lambda _: out, size, "from-recipe")
    return np.clip(out, 0, 1)


# ---- 读取与色彩空间 ------------------------------------------------------------


def test_provenance_matches_sources_and_files():
    prov = json.loads((LUT_PKG / "PROVENANCE.json").read_text())
    for name in ("cube.py", "spaces.py", "colorimetry.py"):
        assert (LUT_PKG / name).is_file()
        assert len(prov["files"][name]["source_sha256"]) == 64
    src = ROOT / prov["files"]["spaces.py"]["source"]
    if src.is_file():
        assert hashlib.sha256(src.read_bytes()).hexdigest() == prov["files"]["spaces.py"]["source_sha256"]
        assert (LUT_PKG / "spaces.py").read_bytes() == src.read_bytes(), "spaces.py 应与来源逐字节相同"


@pytest.mark.parametrize("size", [17, 33, 65])
def test_identity_3d_cube_sizes(tmp_path, size):
    write_cube(tmp_path / "id.cube", lambda x: x, size)
    c = Cube.read(tmp_path / "id.cube")
    assert c.table3.shape == (size, size, size, 3) and c.table1 is None
    rng = np.random.default_rng(1)
    x = rng.uniform(0, 1, (500, 3))
    assert np.allclose(c.apply(x), x, atol=1e-7)


def test_cube_red_is_fastest_axis(tmp_path):
    p = tmp_path / "r.cube"
    # 2×2×2：第 2 行（R 轴前进一格）的值应当是 R=1,G=0,B=0 的节点
    rows = ["0 0 0", "1 0 0", "0 1 0", "1 1 0", "0 0 1", "1 0 1", "0 1 1", "1 1 1"]
    rows[1] = "0.9 0.9 0.9"
    p.write_text("LUT_3D_SIZE 2\n" + "\n".join(rows) + "\n")
    c = Cube.read(p)
    assert np.allclose(c.apply(np.array([[1.0, 0.0, 0.0]])), [[0.9, 0.9, 0.9]])
    assert np.allclose(c.apply(np.array([[0.0, 1.0, 0.0]])), [[0.0, 1.0, 0.0]])


def test_cube_1d_domain_and_shaper_plus_3d(tmp_path):
    p = tmp_path / "one.cube"
    p.write_text(
        'TITLE "gamma"\nLUT_1D_SIZE 5\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 2 2 2\n' + "\n".join(f"{v} {v} {v}" for v in (0, 0.1, 0.4, 0.8, 1)) + "\n"
    )
    c = Cube.read(p)
    assert c.table1 is not None and c.table3 is None and c.title == "gamma"
    assert np.allclose(c.apply(np.array([[1.0, 1.0, 1.0]])), 0.4)  # 域 0..2 的正中
    both = tmp_path / "both.cube"
    lines = [f"{v} {v} {v}" for v in np.linspace(0, 1, 4)]
    cube3 = [f"{r} {g} {b}" for b in (0, 1) for g in (0, 1) for r in (0, 1)]
    both.write_text("LUT_1D_SIZE 4\nLUT_3D_SIZE 2\n" + "\n".join(lines + cube3) + "\n")
    c2 = Cube.read(both)
    assert c2.table1 is not None and c2.table3 is not None
    assert np.allclose(c2.apply(np.array([[0.3, 0.6, 0.9]])), [[0.3, 0.6, 0.9]], atol=1e-9)


@pytest.mark.parametrize(
    "text",
    [
        "hello\n",
        "LUT_3D_SIZE 2\n0 0 0\n",  # 行数不对
        "LUT_3D_SIZE 1\n0 0 0\n",  # 尺寸太小
        "LUT_3D_SIZE 2\nLUT_3D_SIZE 2\n" + "0 0 0\n" * 8,  # 重复头
        "LUT_3D_SIZE 2\n" + "0 0 nan\n" * 8,  # 非有限数
        "LUT_3D_SIZE 2\nDOMAIN_MIN 1 1 1\nDOMAIN_MAX 0 0 0\n" + "0 0 0\n" * 8,
        "LUT_3D_SIZE 2\nDOMAIN_MIN 0 0 0\nLUT_3D_INPUT_RANGE 0 1\n" + "0 0 0\n" * 8,  # 两种范围写法同时出现
    ],
)
def test_bad_cubes_are_refused(text):
    with pytest.raises(ValueError):
        Cube.parse(text)


@pytest.mark.parametrize("space", spaces.SPACES)
def test_spaces_roundtrip(space):
    x = np.linspace(0.02, 0.9, 40)
    lin = spaces.decode(spaces.encode(x, space), space)
    assert np.allclose(lin, x, atol=1e-6), space


def test_log_target_needs_scene_xyz():
    c = Cube.parse("LUT_3D_SIZE 2\n" + "\n".join(f"{r} {g} {b}" for b in (0, 1) for g in (0, 1) for r in (0, 1)) + "\n")
    with pytest.raises(ValueError):
        spaces.Target(c, "llog").render(np.full((4, 3), 128.0))
    out = spaces.Target(c, "srgb").render(np.full((4, 3), 128.0))
    assert np.allclose(out, 128.0, atol=1e-6)


# ---- 色样 ----------------------------------------------------------------------


def test_synthetic_samples_are_deterministic_and_split(model, cam, samples):
    again = scene.build_samples(model, cam.stock[0], "srgb")
    assert np.array_equal(samples.x, again.x)
    n = samples.counts()
    assert n["groups"]["灰阶"] == 257 and n["groups"]["肤色"] > 50 and n["groups"]["常见色块"] > 500
    assert 0.2 < n["holdout"] / n["total"] < 0.3
    assert samples.xyz is None
    # 肤色色样在 04 下确实是肤色色相
    skin = model.predict(cam.stock[0], samples.x[samples.group == 1])
    lab = colour.lab_from_codes(skin)
    hue = np.degrees(np.arctan2(lab[:, 2], lab[:, 1])) % 360
    assert hue.min() >= 20 and hue.max() <= 60


def test_log_input_without_a_photo_is_refused_in_plain_words(model, cam):
    with pytest.raises(scene.SceneError) as e:
        scene.build_samples(model, cam.stock[0], "llog")
    assert e.value.code == "needs_photo" and "照片" in e.value.message


def test_last_photo_is_holdout_only(model, cam):
    codes = np.full((60, 80, 3), 900.0) * np.linspace(0.5, 2, 80)[None, :, None]
    photos = [{"codes": codes, "colour": None, "label": "a"}, {"codes": codes * 0.9, "colour": None, "label": "b"}]
    s = scene.build_samples(model, cam.stock[0], "srgb", photos)
    last = [p for p in s.photos if p["holdout_only"]]
    assert [p["label"] for p in last] == ["b"]
    assert s.holdout[s.group == 3].sum() > 0 and (~s.holdout[s.group == 3]).sum() > 0


@needs_sample
def test_scene_colour_matches_research_dng_colour():
    import importlib.util

    from gxr_studio.raw import open_dng

    src = ROOT / "research/color_table_fit/dng_scene.py"
    if not src.is_file():
        pytest.skip("研究目录不在本机")
    spec = importlib.util.spec_from_file_location("research_dng_scene", src)
    mod = importlib.util.module_from_spec(spec)
    import sys

    sys.modules["research_dng_scene"] = mod
    spec.loader.exec_module(mod)
    path = SAMPLE_DIR / "R8051144.DNG"
    ref = mod.Dng(path)
    col = mod.DngColour(ref)
    white, _ = mod.as_shot_white(ref, col)
    want = col.camera_to_xyz(white)
    got = scene.scene_colour(open_dng(path))
    assert np.allclose(got.matrix, want, atol=1e-12) and np.allclose(got.neutral, ref.as_shot_neutral)
    # 与 lut_convert/camera.py 的 Camera.xyz 同一个式子
    x = np.array([[1200.0, 1500.0, 900.0]])
    u, f = scene.ANCHOR["u_m"], scene.ANCHOR["f"]
    wh = ref.as_shot_neutral @ want.T
    ref_xyz = ((x / 4095 - f) * ref.as_shot_neutral) @ want.T * (0.18 / ((u - f) * wh[1]))
    assert np.allclose(got.xyz(x), ref_xyz)


# ---- 相似度与说明 ----------------------------------------------------------------


def test_similarity_formula_labels_and_na():
    assert fit.similarity_of(2.0, 0.2) == pytest.approx(90.0)
    assert fit.similarity_of(2.0, 3.0) == pytest.approx(-50.0)  # 比 04 更远：负数，不钳位
    assert fit.similarity_of(0.24, 0.0) is None and fit.similarity_of(0.25, 0.0) == 100.0
    assert [fit.label_for(v) for v in (95.0, 90.0, 89.99, 80.0, 79.9, -3.0, None)] == [
        "接近",
        "接近",
        "大致",
        "大致",
        "差别明显",
        "差别明显",
        None,
    ]


def test_explain_names_the_biggest_differences():
    rng = np.random.default_rng(3)
    L = np.linspace(5, 95, 400)  # noqa: N806  这里的 L 是 Lab 的明度轴
    ideal = np.stack([L, rng.normal(0, 1, 400), rng.normal(0, 1, 400)], axis=1)
    group = np.zeros(400, np.int8)
    pred = ideal.copy()
    pred[L < 35, 1] -= 6.0  # 暗部偏绿
    pred[L >= 70, 0] += 4.0  # 高光偏亮
    notes = fit.explain(pred, ideal, group)
    assert any(n.startswith("暗部") and "偏绿" in n for n in notes), notes
    assert any(n.startswith("高光") and "偏亮" in n for n in notes), notes
    assert fit.explain(ideal, ideal, group) == ["整体已经很接近，没有明显的偏差"]
    chroma = np.stack([np.full(400, 50.0), rng.normal(0, 25, 400), rng.normal(0, 25, 400)], axis=1)
    flat = chroma * np.array([1, 0.6, 0.6])
    assert "整体饱和度偏低" in fit.explain(flat, chroma, np.full(400, 2, np.int8))


# ---- 拟合：找回、取消 -------------------------------------------------------------

TRUTHS = [
    (5, {"contrast": 3, "shadow": -2, "saturation": 2}),
    (1, {"amount": 75, "highlight": -4, "warmth": 2, "saturation": -3}),
]


@pytest.mark.parametrize("slot,levels", TRUTHS, ids=["05-tone-sat", "01-amount-warm"])
def test_representable_target_is_recovered_exactly(cam, model, samples, slot, levels):
    """正确性的底线：目标本来就是"底子 + 旋钮"能做出来的，必须找回同一个底子，相似度 ≥ 99%（沿用 lut_convert 的自检思路）。"""
    full, entry = known(cam, slot, levels)
    stock = model.predict(cam.stock[0], samples.x)
    ideal = model.predict(entry, samples.x)
    r = fit.Fitter(cam, model, samples, stock, ideal, profile="normal").run()
    best = r["best"]
    assert best["slot"] == slot, [(c["slot"], c["similarity"]) for c in r["candidates"]]
    assert best["similarity"] >= 99.0
    assert best["holdout"]["median"] <= 0.05
    assert r["prediction"] is True and r["label"] == "电脑预测"


def test_target_from_a_real_cube_file_is_matched_as_well_as_the_truth(cam, model, tmp_path):
    """目标是一个真的 .cube（用已知配方生成，33³ 格有插值误差）：找回同一个底子，或者 ΔE 不比已知答案差。"""
    slot, levels = 5, {"contrast": 2, "highlight": -3, "saturation": 3}
    full, entry = known(cam, slot, levels)
    cube_from_recipe(model, cam.stock[0], entry, tmp_path / "t.cube")
    cube = Cube.read(tmp_path / "t.cube")
    smp = scene.build_samples(model, cam.stock[0], "srgb")
    stock, ideal = scene.target_rgb(model, cam.stock[0], smp, spaces.Target(cube, "srgb", "srgb"))
    f = fit.Fitter(cam, model, smp, stock, ideal, profile="normal")
    r = f.run()
    truth = f.describe(slot, full)
    best = r["best"]
    assert best["slot"] == slot or best["holdout"]["median"] <= truth["holdout"]["median"] + 0.05
    assert best["holdout"]["median"] <= truth["holdout"]["median"] + 0.05
    assert best["similarity"] >= truth["similarity"] - 0.5, (best["similarity"], truth["similarity"])


def test_cancel_stops_the_search_quickly(cam, model, samples):
    stock = model.predict(cam.stock[0], samples.x)
    ideal = model.predict(known(cam, 6, {"contrast": 2})[1], samples.x)
    calls = {"n": 0}

    def cancelled():
        calls["n"] += 1
        return calls["n"] > 25

    f = fit.Fitter(cam, model, samples, stock, ideal, profile="quick", cancelled=cancelled)
    t0 = time.perf_counter()
    with pytest.raises(fit.Cancelled):
        f.run()
    assert time.perf_counter() - t0 < 5 and f.prog.evaluations <= 26


def test_search_only_uses_menu_legal_levels(cam, model, samples):
    """每个候选的档位都必须是 final.json 菜单里合法的（含到顶标记），不钳位。"""
    stock = model.predict(cam.stock[0], samples.x)
    ideal = np.clip(model.predict(cam.stock[0], samples.x) * np.array([1.12, 0.96, 0.86]), 0, 255)
    r = fit.Fitter(cam, model, samples, stock, ideal, profile="quick").run()
    for c in r["candidates"]:
        controls = {x["name"]: x for x in cam.controls(0, c["slot"])}
        assert set(c["levels"]) == set(controls)
        for name, lv in c["levels"].items():
            assert lv in controls[name]["levels"], (c["slot"], name, lv)
        cam.resolve(RecipeSel("jlaunch", c["slot"], tuple(sorted(c["levels"].items()))))


# ---- API ---------------------------------------------------------------------------


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


def cube_bytes(fun=lambda x: x, size=17, title="t") -> bytes:
    a = np.linspace(0, 1, size)
    b, g, r = np.meshgrid(a, a, a, indexing="ij")
    y = fun(np.stack([r, g, b], axis=-1).reshape(-1, 3))
    return (f'TITLE "{title}"\nLUT_3D_SIZE {size}\n' + "\n".join(" ".join(f"{v:.8f}" for v in row) for row in y) + "\n").encode()


def warm(x):
    return np.clip(x * np.array([1.08, 1.0, 0.9]) + 0.01, 0, 1)


def upload(client, raw, name="warm.cube"):
    return client.post("/api/lut/upload", content=raw, headers={"X-GXR-Filename": name})


def wait(client, job_id, limit=240):
    for _ in range(limit * 4):
        st = client.get(f"/api/lut/fit/{job_id}").json()
        if st["state"] != "running":
            return st
        time.sleep(0.25)
    raise AssertionError("拟合超时")


def test_upload_describes_the_cube_and_guesses_the_space(client):
    r = upload(client, cube_bytes(warm, 17, "warm film"), "Leica_L-Log_warm.cube")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["kind"] == "3D" and d["size_3d"] == 17 and d["title"] == "warm film"
    assert d["input_space_guess"] == "llog" and "L-Log" in d["guess_reason"].replace("l-log", "L-Log")
    assert len(d["cube_id"]) == 64
    r2 = upload(client, cube_bytes(warm, 17), "mystery.cube")
    assert r2.json()["input_space_guess"] is None and "不知道" in r2.json()["guess_note"] or "选" in r2.json()["guess_note"]
    spaces_ = client.get("/api/lut/spaces").json()["spaces"]
    ids = [s["id"] for s in spaces_]
    assert {"srgb", "rec709", "gamma24", "flog", "flog2", "llog", "vlog", "slog3", "applelog"} <= set(ids)
    assert next(s for s in spaces_ if s["id"] == "flog")["kind"] == "log"


@pytest.mark.parametrize(
    "body,code",
    [
        (b"this is not a cube file", "cube_unreadable"),
        (b"LUT_3D_SIZE 2\n0 0 0\n", "cube_unreadable"),
        (b"\xff\xfe\x00\x01", "cube_unreadable"),
    ],
)
def test_upload_rejects_non_cube_with_plain_message(client, body, code):
    r = upload(client, body)
    assert r.status_code == 422 and r.json()["error"] == code
    assert "看不懂" in r.json()["message"] and ".cube" in r.json()["message"]


def test_preview_without_camera_data_uses_the_chart(client):
    cid = upload(client, cube_bytes(warm)).json()["cube_id"]
    a = client.get(f"/api/lut/{cid}/preview", params={"which": "original"})
    b = client.get(f"/api/lut/{cid}/preview", params={"which": "lut"})
    assert a.status_code == 200 and b.status_code == 200 and a.content != b.content
    assert b.headers["x-gxr-preview-mode"] == "chart" and b.headers["x-gxr-prediction"] == "1"
    assert client.get(f"/api/lut/{cid}/preview", params={"which": "nope"}).status_code == 422
    assert client.get(f"/api/lut/{'0' * 64}/preview").status_code == 404
    # Log 输入没有场景光时用"示意"并明说
    c = client.get(f"/api/lut/{cid}/preview", params={"which": "lut", "space": "llog"})
    assert c.status_code == 200 and c.headers["x-gxr-nominal-scene"] == "1"


def test_28mm_and_mount_are_refused_with_a_reason(data_client):
    cid = upload(data_client, cube_bytes(warm)).json()["cube_id"]
    for unit in ("qlaunch", "l08firm"):
        r = data_client.post("/api/lut/fit", json={"cube_id": cid, "unit": unit})
        assert r.status_code == 422 and r.json()["error"] == "unit_not_calibrated"
        assert "没有单独标定" in r.json()["message"]


def test_fit_needs_camera_data(client):
    cid = upload(client, cube_bytes(warm)).json()["cube_id"]
    r = client.post("/api/lut/fit", json={"cube_id": cid})
    assert r.status_code == 409 and r.json()["error"] == "camera_data_missing" and "status" in r.json()
    assert client.post("/api/lut/fit", json={"cube_id": cid, "space": "nope"}).status_code == 422
    assert client.get("/api/lut/fit/0123456789ab").status_code == 404


def test_full_flow_fit_save_apply_and_manage_recipes(data_client, camhome):
    c = data_client
    cid = upload(c, cube_bytes(warm, 17, "warm film"), "warm.cube").json()["cube_id"]
    r = c.post("/api/lut/fit", json={"cube_id": cid, "space": "srgb", "output": "srgb", "profile": "quick"})
    assert r.status_code == 200, r.text
    job = r.json()["job_id"]
    busy = c.post("/api/lut/fit", json={"cube_id": cid, "profile": "quick"})
    if busy.status_code == 409:  # 同时只跑一个
        assert busy.json()["error"] == "fit_busy" and busy.json()["job_id"] == job
    st = wait(c, job)
    assert st["state"] == "done", st.get("error")
    res = st["result"]
    assert res["prediction"] is True and res["label"] == "电脑预测" and "相机实拍通常会低一些" in res["caution"]
    assert len(res["candidates"]) == 2 and res["best"] == res["candidates"][0]
    sims = [x["similarity"] for x in res["candidates"]]
    assert sims == sorted(sims, reverse=True)
    best = res["best"]
    # 数字与结果文件完全一致：界面显示的相似度 = 1 - 候选中位数 / 04 中位数（留出样本）
    assert best["similarity"] == pytest.approx(100 * (1 - best["holdout"]["median"] / best["baseline04_holdout"]["median"]))
    assert best["label"] == fit.label_for(best["similarity"])
    assert res["formula"] == fit.FORMULA and res["thresholds"] == {"close": 90.0, "rough": 80.0}
    job_dir = Path(c.app.state.ctx.settings.home_dir) / "lut_jobs" / job
    on_disk = json.loads((job_dir / "result.json").read_text())
    assert on_disk["result"]["best"]["similarity"] == best["similarity"]
    # 暖色 LUT：差别说明不为空
    assert best["differences"]
    # 存为我的配方
    saved = c.post("/api/my-recipes", json={"job_id": job, "candidate": 0, "name": "  我的暖色  配方 "})
    assert saved.status_code == 200, saved.text
    doc = saved.json()
    assert doc["name"] == "我的暖色 配方" and doc["recipe"]["kind"] == "builtin" and doc["recipe"]["unit"] == "jlaunch"
    assert doc["fit"]["similarity"] == best["similarity"] and doc["camera_write"]["status"] == "not_supported_yet"
    assert len(doc["camera_short_name"]) == 8 and doc["camera_short_name"].isascii()
    assert [x["id"] for x in c.get("/api/my-recipes").json()["recipes"]] == [doc["id"]]
    assert c.put(f"/api/my-recipes/{doc['id']}", json={"name": "改个名"}).json()["name"] == "改个名"
    assert c.put(f"/api/my-recipes/{doc['id']}", json={"name": "  "}).status_code == 422
    assert c.post("/api/my-recipes", json={"job_id": job, "candidate": 9, "name": "x"}).status_code == 422
    assert c.post("/api/my-recipes", json={"job_id": "0123456789ab", "candidate": 0, "name": "x"}).status_code == 404
    # 存下来的配方是内置配方的形状：直接就能当某张照片的配方去预览（走原有的渲染链）
    from gxr_studio.render.params import parse_recipe

    sel = parse_recipe(doc["recipe"])
    assert sel.slot == best["slot"] and dict(sel.levels) == best["levels"]
    assert c.delete(f"/api/my-recipes/{doc['id']}").status_code == 200
    assert c.get("/api/my-recipes").json()["recipes"] == [] and c.get(f"/api/my-recipes/{doc['id']}").status_code == 404


def test_cancel_kills_the_worker_and_allows_a_new_fit(data_client):
    c = data_client
    cid = upload(c, cube_bytes(warm, 17), "warm.cube").json()["cube_id"]
    job = c.post("/api/lut/fit", json={"cube_id": cid, "profile": "normal"}).json()["job_id"]
    svc = c.app.state.ctx.lut
    proc = svc._procs[job]
    assert proc.poll() is None
    st = c.post(f"/api/lut/fit/{job}/cancel").json()
    assert st["state"] == "cancelled" and proc.poll() is not None
    assert "result" not in st
    assert c.get(f"/api/lut/fit/{job}").json()["state"] == "cancelled"
    assert c.post("/api/my-recipes", json={"job_id": job, "candidate": 0, "name": "x"}).status_code == 409
    again = c.post("/api/lut/fit", json={"cube_id": cid, "profile": "quick"})
    assert again.status_code == 200
    c.post(f"/api/lut/fit/{again.json()['job_id']}/cancel")


def test_progress_is_reported_while_running(data_client):
    c = data_client
    cid = upload(c, cube_bytes(warm, 17), "warm.cube").json()["cube_id"]
    job = c.post("/api/lut/fit", json={"cube_id": cid, "profile": "normal"}).json()["job_id"]
    seen = None
    for _ in range(240):
        st = c.get(f"/api/lut/fit/{job}").json()
        if st["progress"] and st["progress"]["evaluations"] > 0:
            seen = st["progress"]
            break
        time.sleep(0.25)
    c.post(f"/api/lut/fit/{job}/cancel")
    assert seen and 0 <= seen["fraction"] <= 1 and seen["phase"] and seen["evaluations"] > 0


@needs_sample
def test_fit_and_preview_with_library_photos(data_client, real_card):
    c = data_client
    import_all(c)
    photo = find(c, "R8051144")
    cid = upload(c, cube_bytes(warm, 17), "warm.cube").json()["cube_id"]
    a = c.get(f"/api/lut/{cid}/preview", params={"which": "original", "photo": photo["id"]})
    b = c.get(f"/api/lut/{cid}/preview", params={"which": "lut", "photo": photo["id"]})
    assert a.status_code == 200 and b.status_code == 200 and a.content != b.content
    assert b.headers["x-gxr-preview-mode"] == "04-simulation"
    job = c.post("/api/lut/fit", json={"cube_id": cid, "profile": "quick"}).json()
    assert job["photos"], "应当有图库里的 DNG 照片可抽样"
    st = wait(c, job["job_id"])
    assert st["state"] == "done", st.get("error")
    assert st["result"]["samples"]["groups"]["照片"] > 0 and any(p["holdout_only"] for p in st["result"]["samples"]["photos"]) == (
        len(st["result"]["samples"]["photos"]) >= 2
    )
