"""halfframe-p1b-gpu 合成测试：后端 /gpu /raw /bake /gpu/box 接口、gpu_stage.js 路由与状态、
Engine 资源释放与合成双联像素一致性（方向 1/3/6/8、左右先拍、双配方表、显示类/Log LUT、调整/曲线/几何、接缝与显存）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import PORT
from gpu_gate import evaluate, valid_mask
from gpu_support import run_engine
from gxr_studio import halfframe_compose as hfc
from gxr_studio.app import create_app
from gxr_studio.finish.compose import finish_array
from gxr_studio.finish.spec import factory_frame, factory_stamp, parse_geometry
from gxr_studio.gpu.box import final_box
from gxr_studio.gpu.source import affine_of, raw_layout
from gxr_studio.gpu.tables import bake_bytes, lut_bytes
from gxr_studio.lut.cube import Cube
from gxr_studio.lut.photo import _apply
from gxr_studio.lut.scene import SceneColour
from gxr_studio.lut.spaces import Target
from gxr_studio.raw import NEUTRAL, half_res_codes, open_dng
from gxr_studio.render.chain import ChromaBake, chroma_u8
from gxr_studio.render.params import EditState, parse_adjust, parse_curve
from gxr_studio.render.tones import channel_gains, display_table
from raw_support import bayer_scene, build_dng
from test_gpu_frontend import FAKE_GPU, _decide, run_node
from test_halfframe_p1b_gpu_field import PAIR_HARNESS_JS, _seam_slice

ASSETS = Path(__file__).resolve().parents[1] / "web" / "assets"
LENS = "RICOH GR LENS A12 50mm F2.5 MACRO"


def _make_scene(h: int, w: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w] / np.array([h, w])[:, None, None]
    img = np.stack([xx * 0.85, yy * 0.75, (1.0 - 0.5 * (xx + yy)) * 0.8], axis=-1)
    for _ in range(10):
        y0, x0 = int(rng.integers(0, h - 40)), int(rng.integers(0, w - 50))
        img[y0 : y0 + 40, x0 : x0 + 50] = rng.uniform(0.05, 0.95, 3)
    img += rng.normal(0, 0.01, img.shape)
    return np.clip(img, 0.0, 1.1)


def _make_bake(gamma_shift: float, rot: float) -> ChromaBake:
    x = np.arange(4096, dtype=np.float64)[:, None] / 4095.0
    pre = 255.0 * np.clip(x ** (1.0 / (np.array([2.1, 2.2, 2.3]) + gamma_shift)), 0.0, 1.0)
    matrix = np.array([[0.299, 0.587, 0.114], [-0.168736, -0.331264, 0.5], [0.5, -0.418688, -0.081312]])
    step, n = 2.0, 131
    origin = (-130.0, -130.0)
    cb = origin[0] + step * np.arange(n)
    cr = origin[1] + step * np.arange(n)
    c = cb[None, :] + 1j * cr[:, None]
    theta = np.angle(c)
    gain = (1.0 + 0.2 * np.sin(2.0 * theta + rot)) * np.exp(1j * (0.12 * np.cos(theta) + rot))
    g = np.stack([gain.real, gain.imag], axis=-1).astype(np.float32)
    return ChromaBake(pre.astype(np.float32), matrix.astype(np.float32), np.array([1.5, 0.0, 0.0], np.float32), g, origin, step)


def _make_cube(size: int = 17) -> Cube:
    v = np.linspace(0, 1, size)
    nodes = np.array([[r, g, b] for b in v for g in v for r in v])
    mix = np.array([[0.91, 0.09, 0.0], [0.04, 0.86, 0.10], [0.0, 0.12, 0.94]])
    rows = np.clip(nodes @ mix.T, 0, 1) ** 0.92
    text = f'TITLE "pair_synth"\nLUT_3D_SIZE {size}\n' + "\n".join(" ".join(f"{c:.9f}" for c in row) for row in rows) + "\n"
    return Cube.parse(text)


# ---- 1. 后端 API 测试 ------------------------------------------------------------------


def _setup_synthetic_pair_app(settings: Any, tmp_path: Path, *, symlink_second: bool = False) -> tuple[Any, str, str, bytes, bytes]:
    settings.home_dir.mkdir(parents=True, exist_ok=True)
    (settings.home_dir / "flags.json").write_text(json.dumps({"gpu_preview": True}), encoding="utf-8")
    lib_dir = settings.library_dir / "2026" / "2026-01-01"
    lib_dir.mkdir(parents=True, exist_ok=True)
    h, w = 240, 320
    dng1_bytes = build_dng(
        bayer_scene(h, w, np.clip(_make_scene(h, w, 10), 0, 1), white=4095, black=64),
        bps=12,
        black=(64,),
        white=4095,
        orientation=1,
        neutral=(0.55, 1.0, 0.62),
        lens=LENS,
        root_extra=[(50721, "SRATIONAL", [1, 0, 0, 0, 1, 0, 0, 0, 1]), (50778, "SHORT", [21])],
    )
    dng2_bytes = build_dng(
        bayer_scene(h, w, np.clip(_make_scene(h, w, 21 if symlink_second else 20), 0, 1), white=4095, black=64),
        bps=12,
        black=(64,),
        white=4095,
        orientation=1,
        neutral=(0.52, 1.0, 0.66),
        lens=LENS,
        root_extra=[(50721, "SRATIONAL", [1, 0, 0, 0, 1, 0, 0, 0, 1]), (50778, "SHORT", [21])],
    )
    p_dng1 = lib_dir / "S0001.DNG"
    p_dng1.write_bytes(dng1_bytes)
    p_dng2 = lib_dir / "S0002.DNG"
    if symlink_second:
        tmp_path.mkdir(parents=True, exist_ok=True)
        outside = tmp_path / "outside.DNG"
        outside.write_bytes(dng2_bytes)
        p_dng2.unlink(missing_ok=True)
        os.symlink(outside, p_dng2)
    else:
        p_dng2.write_bytes(dng2_bytes)
    p_jpg2 = lib_dir / "S0002.JPG"
    Image.fromarray(np.zeros((h, w, 3), np.uint8), "RGB").save(p_jpg2, quality=90)

    import hashlib

    sha1 = hashlib.sha256(dng1_bytes).hexdigest()
    sha2 = hashlib.sha256(dng2_bytes).hexdigest()
    shaj = hashlib.sha256(p_jpg2.read_bytes()).hexdigest()

    sid1 = "shot-first"
    sid2 = "shot-pair"
    shot1: dict[str, Any] = {
        "shot_id": sid1,
        "name": "S0001",
        "orientation": 1,
        "unit": "jlaunch",
        "lens": LENS,
        "iso": 200,
        "width": w,
        "height": h,
        "files": {"dng": {"relpath": "2026/2026-01-01/S0001.DNG", "sha256": sha1}},
        "pair": {"role": "first", "pair_id": sid2, "first_half": "left"},
    }
    shot2: dict[str, Any] = {
        "shot_id": sid2,
        "name": "S0002",
        "orientation": 1,
        "unit": "jlaunch",
        "lens": LENS,
        "iso": 1600,
        "width": w,
        "height": h,
        "files": {"jpg": {"relpath": "2026/2026-01-01/S0002.JPG", "sha256": shaj}},
        "pair": {"role": "second", "first_id": sid1, "first_half": "left"},
        "pair_sources": {
            "first": {
                "shot_id": sid1,
                "name": "S0001",
                "orientation": 1,
                "unit": "jlaunch",
                "lens": LENS,
                "iso": 200,
                "width": w,
                "height": h,
                "recipe_tag": None,
                "all_files": {"dng": {"relpath": "2026/2026-01-01/S0001.DNG", "sha256": sha1}},
            },
            "second": {
                "shot_id": sid2,
                "name": "S0002",
                "orientation": 1,
                "unit": "jlaunch",
                "lens": LENS,
                "iso": 1600,
                "width": w,
                "height": h,
                "recipe_tag": None,
                "all_files": {"dng": {"relpath": "2026/2026-01-01/S0002.DNG", "sha256": sha2}},
            },
        },
    }
    app = create_app(settings)
    return app, sid1, sid2, dng1_bytes, dng2_bytes, {sid1: shot1, sid2: shot2}  # type: ignore[return-value]


def test_gpu_routes_pair_descriptor_raw_bake_box_and_symlink_refusal(settings: Any, tmp_path: Path, monkeypatch: Any) -> None:
    app, sid1, sid2, dng1_bytes, dng2_bytes, shots = _setup_synthetic_pair_app(settings, tmp_path)  # type: ignore[misc]
    bake1 = _make_bake(0.0, 0.0)
    bake2 = _make_bake(0.25, 0.3)

    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        monkeypatch.setattr(app.state.ctx.lib, "get_shot", lambda sid, **_kw: shots.get(sid))

        def fake_plan(shot: dict[str, Any], edit: EditState) -> Any:
            iso = int(shot.get("iso") or 200)
            slot = edit.recipe.slot if edit.recipe else (1 if iso == 200 else 2)
            return SimpleNamespace(
                unit="jlaunch",
                slot=slot,
                defaulted=edit.recipe is None,
                entry_sha256=f"entry-{iso}-{slot}",
                record_sha256=f"rec-{iso}-{slot}",
                binding_sha256="bind-1",
                selection_id=None,
                quality=None,
                model_iso=1600 if iso >= 800 else 200,
                calibrated=(iso == 200),
                label="已标定" if iso == 200 else "未标定",
            )

        monkeypatch.setattr(app.state.ctx.render, "plan", fake_plan)
        monkeypatch.setattr(app.state.ctx.render, "preview_bake", lambda p: bake1 if p.model_iso == 200 else bake2)
        monkeypatch.setattr(app.state.ctx.render, "available", lambda unit=None: True)
        monkeypatch.setattr(app.state.ctx.render, "warm_preview_bakes", lambda s: None)

        # 1) /gpu on pair returns windowed pair descriptor computed by proxy_geometry
        g = client.get(f"/api/photos/{sid2}/gpu")
        assert g.status_code == 200, g.text
        data = g.json()
        assert data["enabled"] is True
        assert data["raw"] is None
        pair = data["pair"]
        assert pair is not None
        geo = hfc.proxy_geometry(160)
        assert pair["window"] == {
            "width": geo.width,
            "height": 120,
            "half_w": geo.half_w,
            "strip_x0": geo.strip_x0,
            "seam_x0": geo.seam_x0,
            "seam_w": geo.seam_w,
            "right_x0": geo.right_x0,
        }
        assert pair["layer"] == [160, 120] and pair["full"] == [320, 240]
        assert pair["first_half"] == "left" and pair["orientation"] == 1
        assert len(pair["sources"]) == 2
        for idx, s in enumerate(pair["sources"]):
            assert s["half"] == idx
            assert s["raw"]["orientation"] == 1
            assert s["raw"]["crop"] == [0, 2 * geo.strip_x0, 240, 2 * geo.half_w]
            assert s["raw"]["layer"] == [geo.half_w, 120]
            assert f"/api/photos/{sid2}/raw?half={idx}&v=" in s["raw"]["url"]
            assert s["scene"] is not None

        # 2) /raw?half=0|1 serves each DNG; /raw without half stays 404 (P0 rule)
        assert client.get(f"/api/photos/{sid2}/raw").status_code == 404
        assert client.get(pair["sources"][0]["raw"]["url"]).content == dng1_bytes
        assert client.get(pair["sources"][1]["raw"]["url"]).content == dng2_bytes
        assert client.get(f"/api/photos/{sid2}/raw?half=2").status_code == 422
        assert client.get(f"/api/photos/{sid1}/raw?half=0").status_code == 422

        # 3) /bake?half=0|1 returns distinct bakes for each half
        b0 = client.get(f"/api/photos/{sid2}/bake?half=0")
        b1 = client.get(f"/api/photos/{sid2}/bake?half=1")
        assert b0.status_code == 200 and b1.status_code == 200
        m0 = json.loads(urllib.parse.unquote(b0.headers["X-GXR-Bake"]))
        m1 = json.loads(urllib.parse.unquote(b1.headers["X-GXR-Bake"]))
        assert m0["key"] != m1["key"] and m0["model_iso"] == 200 and m1["model_iso"] == 1600
        assert client.get(f"/api/photos/{sid2}/bake").status_code == 409

        # 4) /gpu/box accepts pair layer and full sizes
        bx = client.post(f"/api/photos/{sid2}/gpu/box", json={"width": 320, "height": 240})
        assert bx.status_code == 200, bx.text
        assert bx.json()["box"] == [0, 0, 320, 240]

        # 5) Photo 1 (/gpu) has no pair key (single-photo shape preserved)
        g1 = client.get(f"/api/photos/{sid1}/gpu").json()
        assert g1["enabled"] is True and "pair" not in g1 and g1["raw"] is not None

    # 6) Symlink on second half is refused on both /gpu and /raw?half=1
    app2, _s1, sid2b, _d1, _d2, shots2 = _setup_synthetic_pair_app(settings, tmp_path / "sym", symlink_second=True)  # type: ignore[misc]
    with TestClient(app2, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client2:
        monkeypatch.setattr(app2.state.ctx.lib, "get_shot", lambda sid, **_kw: shots2.get(sid))
        monkeypatch.setattr(app2.state.ctx.render, "available", lambda unit=None: True)
        monkeypatch.setattr(app2.state.ctx.render, "warm_preview_bakes", lambda s: None)
        gs = client2.get(f"/api/photos/{sid2b}/gpu").json()
        assert gs["pair"] is None and gs["jpg"] is None and str(gs["raw_reason"]).startswith("refused:")
        assert client2.get(f"/api/photos/{sid2b}/raw?half=1").status_code == 409


# ---- 2. 前端 decide / gpu_stage 状态 / Engine 资源清理 -----------------------------------


PAIR_INFO = {
    "enabled": True,
    "unit": "jlaunch",
    "raw": None,
    "raw_reason": None,
    "jpg": {"url": "/jpg"},
    "scene": None,
    "pair": {
        "first_half": "left",
        "orientation": 1,
        "layer": [160, 120],
        "full": [320, 240],
        "window": {"width": 160, "height": 120, "half_w": 78, "strip_x0": 41, "seam_x0": 78, "seam_w": 4, "right_x0": 82},
        "sources": [
            {"half": 0, "unit": "jlaunch", "raw": {"url": "/raw0"}, "raw_reason": None, "scene": {"A": [], "b": []}},
            {"half": 1, "unit": "jlaunch", "raw": {"url": "/raw1"}, "raw_reason": None, "scene": {"A": [], "b": []}},
        ],
    },
}
ED = {"v2": True, "lut": None, "geometry": {}, "frame": {"style": "none"}, "stamp": {"enabled": False}}
REC = {"kind": "builtin", "unit": "jlaunch", "slot": 1, "levels": {}}


PAIR_NO_SCENE2 = {
    **PAIR_INFO,
    "pair": {
        **PAIR_INFO["pair"],
        "sources": [
            PAIR_INFO["pair"]["sources"][0],
            {**PAIR_INFO["pair"]["sources"][1], "scene": None},
        ],
    },
}


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node")
@pytest.mark.parametrize(
    ("label", "ed", "info", "stage", "recipe", "lut_space", "expected"),
    [
        (
            "半格一对未改 → pair-raw (recipe=null)",
            ED,
            PAIR_INFO,
            "final",
            None,
            None,
            {"base": "pair-raw", "recipe": None, "lut": None},
        ),
        (
            "半格一对显式配方 → pair-raw",
            ED,
            PAIR_INFO,
            "final",
            REC,
            None,
            {"base": "pair-raw", "recipe": REC, "lut": None},
        ),
        (
            "半格一对显示类 LUT（未改）→ pair-raw",
            {**ED, "lut": "L"},
            PAIR_INFO,
            "final",
            None,
            "srgb",
            {"base": "pair-raw", "recipe": None, "lut": "L"},
        ),
        (
            "半格一对显示类 LUT（显式）→ pair-raw",
            {**ED, "lut": "L"},
            PAIR_INFO,
            "final",
            REC,
            "srgb",
            {"base": "pair-raw", "recipe": REC, "lut": "L"},
        ),
        (
            "半格一对 Log LUT → pair-raw",
            {**ED, "lut": "L"},
            PAIR_INFO,
            "final",
            None,
            "log",
            {"base": "pair-raw", "lut": "L"},
        ),
        ("半格一对 Log LUT 缺第二张 scene → 服务器", {**ED, "lut": "L"}, PAIR_NO_SCENE2, "final", None, "log", None),
        ("半格一对原图 → 服务器", ED, PAIR_INFO, "orig", None, None, None),
        ("半格一对缺 RAW → 服务器（不退回双联 JPG）", ED, {**PAIR_INFO, "pair": None, "jpg": None}, "final", None, None, None),
    ],
)
def test_decide_pair_branches(
    tmp_path: Path,
    label: str,
    ed: Any,
    info: Any,
    stage: str,
    recipe: Any,
    lut_space: str | None,
    expected: Any,
) -> None:
    got = _decide(tmp_path, [{"ed": ed, "info": info, "stage": stage, "recipe": recipe, "lutSpace": lut_space}])[0]
    assert got == expected, label


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node")
def test_engine_pair_frees_both_textures_on_replace_evict_and_dispose(tmp_path: Path) -> None:
    out = run_node(
        tmp_path,
        FAKE_GPU
        + """
device.limits = { maxStorageBufferBindingSize: 1 << 28, maxBufferSize: 1 << 28, maxTextureDimension2D: 8192 };
const origCreateTexture = device.createTexture.bind(device);
device.createTexture = (d) => { const t = origCreateTexture(d); t.createView = () => ({}); return t; };
device.createBindGroup = () => ({});
device.createComputePipeline = () => ({ getBindGroupLayout: () => ({}) });
device.createRenderPipeline = () => ({ getBindGroupLayout: () => ({}) });
device.createCommandEncoder = () => ({
  beginComputePass: () => ({ setPipeline() {}, setBindGroup() {}, dispatchWorkgroups() {}, end() {} }),
  finish: () => ({}),
});
device.queue.onSubmittedWorkDone = async () => {};
import { Engine } from './engine.js';
const e = await Engine.create();
const baseCount = made.length;
const liveAfterInit = () => made.slice(baseCount).filter((r) => r.kind === 'texture' && !r.destroyed);
const layout = {
  bps: 12, big_endian: true, row_bytes: 240, strips: [[0, 240 * 120]],
  crop: [0, 0, 120, 156], active: [0, 0], phase: [0, 1, 1, 2],
  black: [0, 0, 0, 0], white: 4095, wb: [4095, 4095, 4095],
  orientation: 1, width: 160, height: 120,
};
const cfg = {
  first_half: 'left', orientation: 1,
  window: { width: 160, height: 60, half_w: 78, strip_x0: 41, seam_x0: 78, seam_w: 4, right_x0: 82 },
  sources: [{ layout, scene: null }, { layout, scene: null }],
};
const raw = new ArrayBuffer(240 * 120);
await e.addPairRaw('P1', cfg, raw, raw);
const afterAdd = { extraTex: liveAfterInit().length, bytes: e.stats().sourceBytes };
await e.addPairRaw('P1', cfg, raw, raw);
const afterReplace = { extraTex: liveAfterInit().length, sources: e.stats().sources };
const meta = { nx: 2, ny: 2, origin: [0, 0], step: 1 };
e.addBake('K1', meta, new ArrayBuffer(64));
e.addBake('K2', meta, new ArrayBuffer(64));
e.setLook({ recipe: 'K1', recipe2: 'K2' });
for (let i = 0; i < 80; i++) e.addBake('B' + i, meta, new ArrayBuffer(64));
const bakePin = { k1: e.hasBake('K1'), k2: e.hasBake('K2') };
e.dispose();
const afterDispose = { remainingSources: e.stats().sources, extraTex: liveAfterInit().length };
console.log(JSON.stringify({ afterAdd, afterReplace, bakePin, afterDispose }));
""",
    )
    assert out["afterAdd"] == {"extraTex": 2, "bytes": 78 * 60 * 2 * 16}
    assert out["afterReplace"] == {"extraTex": 2, "sources": 1}
    assert out["bakePin"] == {"k1": True, "k2": True}
    assert out["afterDispose"] == {"remainingSources": 0, "extraTex": 0}


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node")
def test_gpu_stage_pair_route_calibrated_generation_and_fallback(tmp_path: Path) -> None:
    folder = tmp_path / "stage_pair"
    (folder / "gpu").mkdir(parents=True)
    for name in ("gpu_stage.js", "gpu_pref.js", "gpu_warp.js", "state.js"):
        shutil.copyfile(ASSETS / name, folder / name)
    (folder / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
    (folder / "api.js").write_text("export const api = (...a) => globalThis.apiStub(...a);\n", encoding="utf-8")
    (folder / "gpu" / "engine.js").write_text(
        """
export const canvasOf = (g, w, h) => ({ minx: 0, miny: 0, w, h, expanded: false });
export const rotatedSize = (c, turns) => (turns % 2 ? { w: c.h, h: c.w } : { w: c.w, h: c.h });
export const validRect = () => [0, 0, 1, 1];
export class Engine {
  static async create() { return new Engine(); }
  constructor() { this.sources = new Map(); this.bakes = new Map(); this.luts = new Map(); this.current = null; }
  hasSource(k) { return this.sources.has(k); }
  async addPairRaw(k, cfg) {
    if (globalThis.failPairAdd) throw new Error('limit_exceeded');
    const swap = cfg.orientation >= 5;
    this.sources.set(k, { w: swap ? cfg.window.height : cfg.window.width, h: swap ? cfg.window.width : cfg.window.height });
    globalThis.log.push(['addPairRaw', k]);
  }
  sourceSize(k) { return this.sources.get(k) || null; }
  hasBake(k) { return this.bakes.has(k); }
  addBake(k) { this.bakes.set(k, true); globalThis.log.push(['addBake', k]); }
  hasLut(k) { return this.luts.has(k); }
  addLut(k) { this.luts.set(k, true); }
  show(k) { this.current = k; globalThis.log.push(['show', k]); }
  setLook(l) { globalThis.log.push(['setLook', { ...l }]); }
  setGeometry() {}
  currentKey() { return this.current; }
  finalSize() { return { w: 160, h: 120 }; }
  canvasSize() { return { w: 160, h: 120 }; }
  setDisplayAspect() {}
  async thumb(o) { globalThis.log.push(['thumb', o.source, o.look.recipe, o.look.recipe2]); return { w: 4, h: 3, data: new Uint8Array(48) }; }
}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    script = folder / "run.mjs"
    script.write_text(
        f"""
Object.defineProperty(globalThis, 'navigator', {{ value: {{ gpu: {{}} }}, configurable: true }});
globalThis.log = [];
globalThis.failPairAdd = false;
const pairInfo = {json.dumps(PAIR_INFO)};
globalThis.apiStub = async (url) => {{
  if (url.endsWith('/gpu')) return pairInfo;
  if (url.endsWith('/gpu/box')) return {{ box: [0, 0, 160, 120], valid: [0, 0, 1, 1] }};
  throw new Error('unexpected api ' + url);
}};
let bakesCalled = [];
globalThis.fetch = async (url) => {{
  if (url.includes('/bake')) {{
    bakesCalled.push(url);
    const half = url.includes('half=1') ? 1 : 0;
    const meta = {{ key: `BK-${{half}}-${{bakesCalled.length}}`, calibrated: half === 0, nx: 2, ny: 2, origin: [0, 0], step: 1 }};
    const headers = new Headers({{ 'X-GXR-Bake': encodeURIComponent(JSON.stringify(meta)) }});
    return {{ ok: true, arrayBuffer: async () => new ArrayBuffer(32), headers }};
  }}
  return {{ ok: true, arrayBuffer: async () => new ArrayBuffer(64), headers: new Headers() }};
}};
import {{ state }} from './state.js';
import {{ route, lookThumb }} from './gpu_stage.js';
state.settings = {{ gpu_preview: true }};
state.camdata = {{ dataVersion: 'v1' }};
const baseEd = () => ({{ v2: true, lut: null, geometry: {{}}, frame: {{ style: 'none' }}, stamp: {{ enabled: false }} }});
const view = {{ photo: {{ id: 'P2', name: 'P2' }}, ed: baseEd() }};
const ctx = {{ stage: 'final', recipe: null, adjust: {{}}, curve: null, geometry: {{}}, crop: null, geometryKey: '' }};
let way;
for (let i = 0; i < 6; i++) {{
  way = route(view, ctx, () => {{}});
  await new Promise((r) => setTimeout(r, 10));
  if (way === 'gpu') break;
}}
const lookCall = globalThis.log.find((x) => x[0] === 'setLook')?.[1];
const thumbRes = await lookThumb(view, ctx, {{ kind: 'builtin', unit: 'jlaunch', slot: 1, levels: {{}} }}, 120);
// Failure fallback when addPairRaw throws on another pair
globalThis.failPairAdd = true;
const view2 = {{ photo: {{ id: 'P3', name: 'P3' }}, ed: baseEd() }};
let way2;
for (let i = 0; i < 6; i++) {{
  way2 = route(view2, ctx, () => {{}});
  await new Promise((r) => setTimeout(r, 10));
  if (way2 === 'server') break;
}}
console.log(JSON.stringify({{ way, calibrated: view.ed.calibrated, lookCall, thumbOk: !!(thumbRes && thumbRes.w === 4), way2 }}));
""".strip()
        + "\n",
        encoding="utf-8",
    )
    res = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["way"] == "gpu"
    assert out["calibrated"] is False  # half 0 calibrated=True, half 1 calibrated=False -> combined False
    assert out["lookCall"]["recipe"].startswith("BK-0-") and out["lookCall"]["recipe2"].startswith("BK-1-")
    assert out["thumbOk"] is True
    assert out["way2"] == "server"


# ---- 3. 合成 GPU 逐像素对拍（方向 1/3/6/8、双配方、双场景矩阵、显示/Log LUT、接缝与显存） ----


def test_synthetic_pair_gpu_parity_all_orientations_and_modes(tmp_path: Path) -> None:
    h, w = 480, 640
    dng1 = build_dng(
        bayer_scene(h, w, np.clip(_make_scene(h, w, 101), 0, 1), white=4095, black=64),
        bps=12,
        black=(64,),
        white=4095,
        crop_origin=(2, 2),
        crop_size=(w - 4, h - 4),
        orientation=1,
        neutral=(0.55, 1.0, 0.62),
    )
    cfa2 = ((1, 0), (2, 1))
    mos2 = (
        bayer_scene(h, w, np.clip(_make_scene(h, w, 202), 0, 1), cfa=cfa2, white=16000, black=0)
        + np.array([[60, 64], [62, 66]], np.uint16)[np.indices((h, w))[0] % 2, np.indices((h, w))[1] % 2]
    )
    dng2 = build_dng(
        mos2,
        bps=16,
        cfa=cfa2,
        black=(60, 64, 62, 66),
        black_repeat=(2, 2),
        white=16066,
        crop_origin=(2, 2),
        crop_size=(w - 4, h - 4),
        orientation=1,
        neutral=(0.60, 1.0, 0.50),
    )

    open1, open2 = open_dng(dng1, "a.dng"), open_dng(dng2, "b.dng")
    lay1, r1 = raw_layout(open1)
    lay2, r2 = raw_layout(open2)
    assert lay1 is not None and lay2 is not None and r1 is None and r2 is None
    codes1 = half_res_codes(open1)
    codes2 = half_res_codes(open2)
    open1.release()
    open2.release()

    h2, w2 = codes1.shape[:2]
    geo = hfc.proxy_geometry(w2)
    strip1 = np.ascontiguousarray(codes1[:, geo.strip_x0 : geo.strip_x0 + geo.half_w], np.float32)
    strip2 = np.ascontiguousarray(codes2[:, geo.strip_x0 : geo.strip_x0 + geo.half_w], np.float32)

    def win_layout(lay: dict[str, Any]) -> dict[str, Any]:
        y0, x0, ch, _cw = lay["crop"]
        hh = ch // 2
        return {
            **lay,
            "crop": [y0, x0 + 2 * geo.strip_x0, 2 * hh, 2 * geo.half_w],
            "orientation": 1,
            "layer": [geo.half_w, hh],
            "full": [2 * geo.half_w, 2 * hh],
        }

    win1, win2 = win_layout(lay1), win_layout(lay2)
    window_dict = {
        "width": geo.width,
        "height": h2,
        "half_w": geo.half_w,
        "strip_x0": geo.strip_x0,
        "seam_x0": geo.seam_x0,
        "seam_w": geo.seam_w,
        "right_x0": geo.right_x0,
    }

    bk1, bk2 = _make_bake(0.0, 0.0), _make_bake(0.18, 0.25)
    bk1_bytes, bk1_meta = bake_bytes(bk1)
    bk2_bytes, bk2_meta = bake_bytes(bk2)
    bk1_meta["key"], bk2_meta["key"] = "bk1", "bk2"

    cube = _make_cube()
    t_disp = Target(cube, "srgb", "srgb")
    t_log = Target(cube, "llog", "gamma24")
    disp_bytes, disp_meta, r_d = lut_bytes(t_disp)
    log_bytes, log_meta, r_l = lut_bytes(t_log)
    assert disp_bytes and disp_meta and r_d is None and log_bytes and log_meta and r_l is None
    disp_meta["key"], log_meta["key"] = "lut_disp", "lut_log"

    col1 = SceneColour(np.array([0.55, 1.0, 0.62]), np.array([[0.60, 0.30, 0.10], [0.25, 0.70, 0.05], [0.02, 0.10, 0.90]]))
    col2 = SceneColour(np.array([0.60, 1.0, 0.50]), np.array([[0.58, 0.32, 0.10], [0.22, 0.72, 0.06], [0.04, 0.08, 0.88]]))
    sc1, sc2 = affine_of(col1), affine_of(col2)

    files = {
        "/d1.dng": dng1,
        "/d2.dng": dng2,
        "/bk1.bin": bk1_bytes,
        "/bk2.bin": bk2_bytes,
        "/lut_disp.bin": disp_bytes,
        "/lut_log.bin": log_bytes,
    }
    cases: list[dict[str, Any]] = []
    refs: dict[str, np.ndarray] = {}
    meta_map: dict[str, dict[str, Any]] = {}

    def add_case(
        cid: str,
        *,
        ori: int,
        first_half: str,
        lut_kind: str | None = None,
        adjust_raw: dict[str, Any] | None = None,
        curve_raw: Any = None,
        geom_raw: dict[str, Any] | None = None,
        crop_raw: dict[str, Any] | None = None,
    ) -> None:
        adjust = parse_adjust(adjust_raw or {})
        curves = parse_curve(curve_raw)
        geom = parse_geometry(geom_raw or {})
        edit = EditState(adjust=adjust, curve=curves, geometry=geom)
        if lut_kind == "log":
            r_s1 = _apply(t_log, NEUTRAL.display(strip1), col1.xyz(strip1))
            r_s2 = _apply(t_log, NEUTRAL.display(strip2), col2.xyz(strip2))
            rgb = hfc.compose_strips(r_s1, r_s2, first_half=first_half, seam_w=geo.seam_w, orientation=ori)
            from_raw = False
        elif lut_kind == "display":
            r_s1 = chroma_u8(strip1, bk1, None, None)
            r_s2 = chroma_u8(strip2, bk2, None, None)
            rgb = _apply(t_disp, hfc.compose_strips(r_s1, r_s2, first_half=first_half, seam_w=geo.seam_w, orientation=ori))
            from_raw = False
        else:
            r_s1 = chroma_u8(strip1, bk1, channel_gains(adjust), display_table(adjust, curves))
            r_s2 = chroma_u8(strip2, bk2, channel_gains(adjust), display_table(adjust, curves))
            rgb = hfc.compose_strips(r_s1, r_s2, first_half=first_half, seam_w=geo.seam_w, orientation=ori)
            from_raw = True
        rect = tuple(crop_raw["rect"]) if crop_raw else None
        ref, _ = finish_array(
            rgb,
            edit,
            stage="final",
            frame=factory_frame(),
            stamp=factory_stamp(),
            crop_rect=rect,
            meta={},
            taken=None,
            file_time=None,
            from_raw=from_raw,
            crop_preset=crop_raw.get("preset") if crop_raw else None,
        )
        lw, lh = (h2, w2) if ori >= 5 else (w2, h2)
        box = None
        if crop_raw or not geom.is_identity():
            box = list(final_box(lw, lh, geom, rect, crop_raw.get("preset") if crop_raw else None)[0])
        refs[cid] = np.ascontiguousarray(ref)
        meta_map[cid] = {
            "ori": ori,
            "mask": valid_mask(lw, lh, geom, tuple(box) if box else None),
            "display_lut": lut_kind == "display",
            "check_seam": geom.is_identity() and crop_raw is None,
        }
        cases.append(
            {
                "id": cid,
                "source": f"p_{ori}_{first_half}",
                "pairRaw": {
                    "url0": "/d1.dng",
                    "url1": "/d2.dng",
                    "config": {
                        "first_half": first_half,
                        "orientation": ori,
                        "window": window_dict,
                        "sources": [{"layout": win1, "scene": sc1}, {"layout": win2, "scene": sc2}],
                    },
                },
                "bake": {"url": "/bk1.bin", "key": "bk1", "meta": bk1_meta} if lut_kind != "log" else None,
                "bake2": {"url": "/bk2.bin", "key": "bk2", "meta": bk2_meta} if lut_kind != "log" else None,
                "lut": (
                    {"url": "/lut_disp.bin", "key": "lut_disp", "meta": disp_meta}
                    if lut_kind == "display"
                    else {"url": "/lut_log.bin", "key": "lut_log", "meta": log_meta}
                    if lut_kind == "log"
                    else None
                ),
                "look": {
                    "recipe": "bk1" if lut_kind != "log" else None,
                    "recipe2": "bk2" if lut_kind != "log" else None,
                    "lut": "lut_disp" if lut_kind == "display" else "lut_log" if lut_kind == "log" else None,
                    "lutStrength": 1,
                    "adjust": adjust_raw or {},
                    "curve": curve_raw,
                },
                "geometry": {
                    "straighten": geom.straighten,
                    "rotate90": geom.rotate90,
                    "flip": geom.flip,
                    "vertical": geom.vertical,
                    "horizontal": geom.horizontal,
                    "distortion": geom.distortion,
                    "box": box,
                },
            }
        )

    add_case("o1_left_dual_bake", ori=1, first_half="left")
    add_case(
        "o3_right_exp_curve_extra",
        ori=3,
        first_half="right",
        adjust_raw={
            "exposure_ev": 0.5,
            "wb": {"warm": 20, "tint": -10},
            "contrast": 30,
            "black_point": -10,
            "saturation": 15,
            "vignette": -25,
        },
        curve_raw={"master": [[0, 0], [64, 50], [192, 210], [255, 255]]},
    )
    add_case("o6_left_display_lut", ori=6, first_half="left", lut_kind="display", adjust_raw={"exposure_ev": 0.25, "contrast": 15})
    add_case("o8_right_log_lut", ori=8, first_half="right", lut_kind="log", adjust_raw={"exposure_ev": -0.3, "shadow": 20, "vignette": 20})
    add_case(
        "o6_left_geom_crop",
        ori=6,
        first_half="left",
        adjust_raw={"highlight": -25},
        geom_raw={"straighten": 3.5, "rotate90": 1, "flip": True},
        crop_raw={"preset": "free", "rect": [0.05, 0.05, 0.9, 0.9]},
    )

    harness = tmp_path / "pair_harness.mjs"
    harness.write_text(PAIR_HARNESS_JS, encoding="utf-8")
    chrome_run = tmp_path / "chrome_run"
    chrome_run.mkdir()
    out, _summary = run_engine(chrome_run, files, cases, timeout=180.0, harness=harness)

    for cid, ref in refs.items():
        m = meta_map[cid]
        res = evaluate(out[cid], ref, mask=m["mask"], display_lut=m["display_lut"])
        assert res["pass"], f"{cid}: {res}"
        if m["check_seam"]:
            sg = _seam_slice(out[cid], orientation=m["ori"], seam_x0=geo.seam_x0, seam_w=geo.seam_w)
            sr = _seam_slice(ref, orientation=m["ori"], seam_x0=geo.seam_x0, seam_w=geo.seam_w)
            assert int(np.abs(sg.astype(np.int16) - sr.astype(np.int16)).max()) <= 2, cid
