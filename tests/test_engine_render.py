"""引擎冻结 E0：电脑预测渲染与研究代码一致（同一份 RAW 派生输入、同一个配方加旋钮 → 像素最大差 0）。

- 黄金：`previews.py` 当年（CPython 3.9 / numpy 1.26）留下的 `preview_frames.json`，每 10 张取 1 张（567 张）；
  默认再抽 1/3 跑（`GXR_E0_FULL=1` 跑全部 567 张；完整 5664 张用 research/studio_engine_e0/tools/verify_all_frames.py）。
- 现场对拍：用独立进程运行研究代码本身（`model_adapter.Model` → `closed_loop.Engine` → DEEPTBL0.DAT），与 studio 逐像素比较。
  `GXR_E0_REF_PYTHON=/usr/bin/python3` 可以再用研究当年的解释器（numpy 1.26）多对一遍。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

import numpy as np
import pytest

import e0_support as t
from gxr_studio.camdata import calib, datcodec as dc, store
from gxr_studio.engine import knobs, menu

TOOLS = t.RESEARCH / "studio_engine_e0/tools"


@pytest.fixture(scope="module")
def home(tmp_path_factory):
    raw = t.fixture_dat(127)
    if raw is None:
        pytest.skip("g127 夹具 DAT 不在本机")
    src = calib.dev_sources(t.REPO)
    if not all(p.is_file() for pair in src.values() for p in pair):
        pytest.skip("研究目录里的标定文件不在本机")
    h = tmp_path_factory.mktemp("e0-home")
    store.import_from_dat(raw, h)
    calib.import_calibration(h, src)
    return h


@pytest.fixture(scope="module")
def models(home):
    return {iso: calib.model_for(home, iso)[0] for iso in (200, 1600)}


@pytest.fixture(scope="module")
def inputs():
    out = {}
    for scene in t.SCENES:
        p = t.SCENE_DIR / f"{scene}_input.npz"
        if not p.is_file():
            pytest.skip("RAW 派生输入（knobs_v2_r2/local/*_input.npz）不在本机")
        with np.load(p, allow_pickle=False) as z:
            out[scene] = z["x"].copy()
    return out


@pytest.fixture(scope="module")
def data():
    return menu.load()


def entry_for(home, data, unit, slot, levels=None):
    stock, base = store.load_stock04(home, unit), store.load_entry(home, unit, slot)
    if levels is None:
        return base
    return knobs.apply(stock, base, menu.request(data, unit, slot, levels))[0]


def test_render_matches_the_original_preview_frames(home, models, inputs, data):
    golden = json.loads((t.GOLDEN / "render_frames.json").read_text())["frames"]
    step = 1 if os.environ.get("GXR_E0_FULL") else 3
    frames = golden[::step]
    names = dc.NAMES
    checked = 0
    for f in frames:
        unit, slot = names.index(f["unit"]), f["slot"]
        r = next(r for r in data["recipes"] if r["unit"] == unit and r["slot"] == slot)
        items = data["items"][r["first_item"]:r["first_item"] + r["item_count"]]
        levels = [i["default_level"] for i in items]
        idx = next(i for i, it in enumerate(items) if data["controls"][it["control"]]["name"] == f["param"])
        levels[idx] = f["level"]
        entry = entry_for(home, data, unit, slot, levels)
        assert hashlib.sha256(entry).hexdigest() == f["entry_sha256"], f
        rgb = models[200].predict(entry, inputs[f["scene"]], quantize=True)
        assert hashlib.sha256(rgb.tobytes()).hexdigest() == f["display_RGB_sha256"], f
        checked += 1
    assert checked >= 180


def reference_cases(home, data):
    entries = {f"{u}/{s}": store.load_entry(home, u, s).hex() for u in range(3) for s in range(1, 9)}
    cases = []
    knob_states = {}
    for c in t.knob_cases(data):
        if c["id"].startswith("m/") and int(c["id"].rsplit("/", 1)[1]) < 2:
            knob_states.setdefault((c["unit"], c["slot"]), []).append(c)
    for u in range(3):
        for s in range(1, 9):
            for scene in t.SCENES:
                cases.append({"id": f"b/{u}/{s}/{scene}", "scene": scene, "unit": u, "slot": s, "request": None, "iso": 200})
            for n, c in enumerate(knob_states[(u, s)]):
                req = menu.request(data, u, s, c["levels"])
                r = {"amount": req.amount, "doses": list(req.doses), "wb_method": req.wb_method,
                     "tone_method": req.tone_method}
                scene = t.SCENES[(u + s + n) % 4]
                cases.append({"id": f"k/{u}/{s}/{n}", "scene": scene, "unit": u, "slot": s, "request": r, "iso": 200})
                cases.append({"id": f"i/{u}/{s}/{n}", "scene": scene, "unit": u, "slot": s, "request": r, "iso": 1600})
    return entries, cases


def run_reference(python, entries, cases, tmp_path):
    spec = tmp_path / "cases.json"
    out = tmp_path / "ref.npz"
    spec.write_text(json.dumps({"entries": entries, "cases": cases}))
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)}
    cp = subprocess.run([python, "-B", str(TOOLS / "ref_render.py"), str(spec), str(out), "--repo", str(t.REPO)],
                        capture_output=True, text=True, timeout=900, env=env)
    assert cp.returncode == 0, cp.stderr[-2000:]
    return np.load(out, allow_pickle=False), cp.stdout.strip()


def pythons():
    out = [sys.executable]
    extra = os.environ.get("GXR_E0_REF_PYTHON")
    if extra:
        out.append(extra)
    return out


@pytest.mark.parametrize("python", pythons())
def test_render_equals_the_research_model_pixel_for_pixel(home, models, inputs, data, tmp_path, python):
    if not (t.KNOBS_SRC / "model_adapter.py").is_file() or not (t.RESEARCH / "closed_loop/model.py").is_file():
        pytest.skip("研究代码不在本机")
    entries, cases = reference_cases(home, data)
    ref, banner = run_reference(python, entries, cases, tmp_path)
    worst = 0
    for c in cases:
        stock, base = store.load_stock04(home, c["unit"]), store.load_entry(home, c["unit"], c["slot"])
        r = c["request"]
        entry = base if r is None else knobs.apply(stock, base, knobs.Request(
            r["amount"], tuple(r["doses"]), r["wb_method"], r["tone_method"]))[0]
        assert bytes(ref["entry_" + c["id"]]) == entry, c["id"]
        rgb = models[c["iso"]].predict(entry, inputs[c["scene"]], quantize=True)
        theirs = ref["rgb_" + c["id"]]
        assert rgb.dtype == theirs.dtype == np.uint8 and rgb.shape == theirs.shape
        worst = max(worst, int(np.abs(rgb.astype(int) - theirs.astype(int)).max()))
    assert worst == 0, f"像素最大差 {worst}（{banner}）"
    assert len(cases) >= 190


def test_unclipped_float_output_is_also_identical(home, models, inputs, data, tmp_path):
    """不量化的浮点输出同样相同：量化不会把差异藏起来。"""
    if not (t.KNOBS_SRC / "model_adapter.py").is_file():
        pytest.skip("研究代码不在本机")
    script = tmp_path / "float_ref.py"
    script.write_text(
        "import sys, json, numpy as np\n"
        f"root = {str(t.REPO)!r}\n"
        "sys.path.insert(0, root + '/research/knobs_v2_r2/src')\n"
        "import model_adapter\n"
        "m = model_adapter.Model()\n"
        "spec = json.load(open(sys.argv[1]))\n"
        "x = np.load(root + '/research/knobs_v2_r2/local/' + spec['scene'] + '_input.npz')['x']\n"
        "out = {k: m.predict(bytes.fromhex(v), x) for k, v in spec['entries'].items()}\n"
        "np.savez(sys.argv[2], **out)\n")
    picks = {f"{u}_{s}": store.load_entry(home, u, s).hex() for u, s in ((0, 1), (0, 3), (1, 6), (2, 8), (2, 4))}
    (tmp_path / "f.json").write_text(json.dumps({"scene": "redblue", "entries": picks}))
    cp = subprocess.run([sys.executable, "-B", str(script), str(tmp_path / "f.json"), str(tmp_path / "f.npz")],
                        capture_output=True, text=True, timeout=600, env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)})
    assert cp.returncode == 0, cp.stderr[-2000:]
    ref = np.load(tmp_path / "f.npz")
    for k, hexed in picks.items():
        ours = models[200].predict(bytes.fromhex(hexed), inputs["redblue"])
        assert np.array_equal(ours, ref[k]), k


def test_masks_and_chart_helpers_run_and_are_deterministic(home, models, data):
    from gxr_studio.engine import model as engine_model

    m = models[200]
    base = store.load_entry(home, 0, 1)
    chart = engine_model.recipe_chart(m, base)
    assert chart.shape[1] == 3 and len(chart) == 257 + 729
    assert np.array_equal(chart, engine_model.recipe_chart(m, base))
    for param in ("contrast", "highlight", "shadow", "fade", "saturation", "warmth", "tint", "filter", "tone", "amount"):
        regions = engine_model.masks(m, base, chart, param, chart=True)
        assert all(r.shape == (len(chart),) for r in regions)
    assert len(engine_model.masks(m, base, chart, "split", chart=True)) == 2


def test_model_refuses_unsupported_input(home, models):
    from gxr_studio.engine.model import Model

    m = models[200]
    base = bytearray(store.load_entry(home, 0, 1))
    # 非对称的色表行（研究模型明确拒绝）
    bad = bytearray(base)
    bad[115 + 2] ^= 0x01
    with pytest.raises(ValueError):
        m.predict(bytes(bad), np.array([[100.0, 100.0, 100.0]]))
    with pytest.raises(ValueError):
        Model(np.zeros((24, 2)), np.zeros(24), np.zeros(24), {"schema": "gxr-sector-model/1"})
    with pytest.raises(ValueError):
        Model(np.zeros((23, 2)), np.zeros(24), np.zeros(24), {"schema": "gxr-sector-model/2"})
