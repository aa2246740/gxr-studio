"""halfframe-p1b-gpu 实拍验收（本地 `GXR_STUDIO_TEST_PARENT`，缺失时自动跳过；不输出照片名/时间/EXIF）。

覆盖：
1. 像素一致性：card250 对 (1,2) 横屏与对 (3,4) 竖屏，在未改动（各自拍摄配方）、显式配方、显示类 LUT、
   曝光+曲线、Log LUT 下，GPU 读回与 Python 生产管线满足 `gpu_gate.py` 门槛，且接缝每像素差 ≤ 2；
   同时校验 `/render?stage=final` 输出同尺寸、同方向、接缝 ≤ 2；
   注：card250 两对的左右两半共用拍摄配方（`sameBake=True`），无实拍异配方对照；异烘焙表路径 `recipeChain2`
   在构建时由 `common.wgsl` 的 `recipeChain` 自动生成，并由 `test_halfframe_p1b_gpu.py` 的合成异配方用例覆盖；
2. 延迟与显存：同一台机器上对 (1,2) 与单张 RAW（第 7 张）交替 A/B 对比滑块到出帧中位延迟（≤ 1.3×）
   及独立源图/总纹理字节（≤ 1.2×）。
"""

from __future__ import annotations

import io
import json
import os
import statistics
import urllib.parse
from pathlib import Path
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
from gxr_studio.cards.discovery import find_cards
from gxr_studio.cards.importer import ImportOptions, run_import
from gxr_studio.finish.compose import finish_array
from gxr_studio.finish.spec import Geometry, factory_frame, factory_stamp
from gxr_studio.library.store import Library
from gxr_studio.lut.photo import _apply
from gxr_studio.render.params import EditState, parse_adjust, parse_curve, parse_recipe
from gxr_studio.render.tones import channel_gains, display_table
from test_halfframe_p0 import (
    _clear_desktop_cache,
    _field_folder,
    _setup_g250_calibrated_home,
    _sorted_pairs,
    _stage_volume_pairs,
)
from test_lut import cube_bytes

pytestmark = pytest.mark.exclusive

PAIR_HARNESS_JS = (
    """
const post = (path, body) => fetch(path, { method: 'POST', body });
const progress = (phase, id = null, info = null) => post('/progress', JSON.stringify({ phase, id, info }));

async function bin(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} ${res.status}`);
  return res.arrayBuffer();
}

try {
  await progress('import');
  const { Engine } = await import('/engine.js');
  const cases = await (await fetch('/cases.json')).json();
  await progress('create');
  const engine = await Engine.create();
  if (!engine) {
    await post('/done', JSON.stringify({ error: 'no WebGPU adapter' }));
  } else {
    await progress('ready', null, engine.info);
    const metrics = {};
    for (const c of cases) {
      await progress('source', c.id, engine.info);
      if (!engine.hasSource(c.source)) {
        if (c.pairRaw) {
          const [d1, d2] = await Promise.all([bin(c.pairRaw.url0), bin(c.pairRaw.url1)]);
          await engine.addPairRaw(c.source, c.pairRaw.config, d1, d2);
        } else if (c.raw) {
          await engine.addRaw(c.source, c.raw.layout, await bin(c.raw.url), c.raw.scene || null);
        }
      }
      await progress('tables', c.id, engine.info);
      if (c.bake && !engine.hasBake(c.bake.key)) engine.addBake(c.bake.key, c.bake.meta, await bin(c.bake.url));
      if (c.bake2 && !engine.hasBake(c.bake2.key)) engine.addBake(c.bake2.key, c.bake2.meta, await bin(c.bake2.url));
      if (c.lut && !engine.hasLut(c.lut.key)) engine.addLut(c.lut.key, c.lut.meta, await bin(c.lut.url));
      engine.show(c.source);
      engine.setLook(c.look);
      engine.setGeometry(c.geometry);
      if (c.measureMemory) {
        const layer = engine.layerSize();
        const stats = engine.stats();
        metrics[c.id] = {
          sourceBytes: stats.sourceBytes,
          layer: layer ? [layer.w, layer.h] : null,
          colorFinalBytes: layer ? layer.w * layer.h * 4 * 2 : 0,
        };
      }
      if (!c.skipReadback) {
        await progress('readback', c.id, engine.info);
        const rb = await engine.readback();
        await post(`/result/${c.id}?w=${rb.w}&h=${rb.h}`, rb.data);
      }
      if (c.benchAB) {
        const ab = c.benchAB;
        const canvas = document.createElement('canvas');
        canvas.style.width = '900px';
        canvas.style.height = '600px';
        document.body.appendChild(canvas);
        engine.attach(canvas);
        const runOne = async (target, ev) => {
          engine.show(target.source);
          engine.setGeometry(target.geometry);
          const t0 = performance.now();
          engine.setLook({ ...target.look, adjust: { ...(target.look.adjust || {}), exposure_ev: ev } });
          const info = engine.frame();
          if (!info) throw new Error('frame returned null during slider bench');
          await engine.device.queue.onSubmittedWorkDone();
          return performance.now() - t0;
        };
        for (let w = 0; w < 3; w++) {
          await runOne(ab.pair, w * 0.05);
          await runOne(ab.single, w * 0.05);
        }
        const pairMs = [];
        const singleMs = [];
        const steps = ab.steps || 15;
        for (let s = 0; s < steps; s++) {
          const ev = -1.0 + (2.0 * s) / Math.max(1, steps - 1);
          if ((s & 1) === 0) {
            pairMs.push(await runOne(ab.pair, ev));
            singleMs.push(await runOne(ab.single, ev));
          } else {
            singleMs.push(await runOne(ab.single, ev));
            pairMs.push(await runOne(ab.pair, ev));
          }
        }
        engine.detach();
        canvas.remove();
        metrics[c.id] = { ...(metrics[c.id] || {}), pairSliderMs: pairMs, singleSliderMs: singleMs };
      }
      await progress('complete', c.id, engine.info);
    }
    await post('/done', JSON.stringify({ info: engine.info, metrics }));
  }
} catch (err) {
  await post('/done', JSON.stringify({ error: String((err && err.stack) || err) }));
}
""".strip()
    + "\n"
)


def _index_shots_by_jpg_sha(lib: Library, pairs: list[tuple[Path, Path]]) -> dict[int, dict[str, Any]]:
    import hashlib

    shots = lib.list_shots(limit=1000)
    by_sha: dict[str, dict[str, Any]] = {}
    for s in shots:
        jpg = s["files"].get("jpg")
        if jpg:
            by_sha[str(jpg["sha256"])] = s
    out: dict[int, dict[str, Any]] = {}
    for idx, (_dng_p, jpg_p) in enumerate(pairs, 1):
        sha = hashlib.sha256(jpg_p.read_bytes()).hexdigest()
        if sha not in by_sha:
            raise AssertionError(f"staged photo #{idx} missing from library (total={len(by_sha)})")
        out[idx] = by_sha[sha]
    return out


def _seam_slice(arr: np.ndarray, *, orientation: int, seam_x0: int, seam_w: int) -> np.ndarray:
    """在已转正数组中取出精确的接缝区域。"""
    if orientation in (1, 3):
        return arr[:, seam_x0 : seam_x0 + seam_w]
    return arr[seam_x0 : seam_x0 + seam_w, :]


def _python_pair_reference(
    render: Any,
    lib: Library,
    shot: dict[str, Any],
    edit: EditState,
    *,
    display_target: Any = None,
    log_target: Any = None,
) -> np.ndarray:
    """用 Python 生产管线计算无损半分辨率参考 RGB（不含 JPEG 压缩噪声）。"""
    strip1, strip2, seam_w, ori, first_half, _ = hfc.pair_sensor_strips(render, shot, max_edge=None)
    if log_target is not None:
        rgb = hfc.render_pair_lut_scene(lib, shot, log_target, _apply, full=False)
        out, _ = finish_array(
            rgb,
            edit,
            stage="final",
            frame=factory_frame(),
            stamp=factory_stamp(),
            crop_rect=None,
            meta={},
            taken=None,
            file_time=None,
            from_raw=False,
        )
        return np.ascontiguousarray(out)
    plan1, plan2, *_ = hfc.pair_plans(render, shot, edit)
    if display_target is not None:
        rgb1, _, _ = hfc._apply_half_codes(render, plan1, strip1, None, None, exact=False)
        rgb2, _, _ = hfc._apply_half_codes(render, plan2, strip2, None, None, exact=False)
        composed = hfc.compose_strips(rgb1, rgb2, first_half=first_half, seam_w=seam_w, orientation=ori)
        looked = _apply(display_target, composed)
        out, _ = finish_array(
            looked,
            edit,
            stage="final",
            frame=factory_frame(),
            stamp=factory_stamp(),
            crop_rect=None,
            meta={},
            taken=None,
            file_time=None,
            from_raw=False,
        )
        return np.ascontiguousarray(out)
    gains = channel_gains(edit.adjust)
    table = display_table(edit.adjust, edit.curve)
    rgb1, _, _ = hfc._apply_half_codes(render, plan1, strip1, gains, table, exact=False)
    rgb2, _, _ = hfc._apply_half_codes(render, plan2, strip2, gains, table, exact=False)
    composed = hfc.compose_strips(rgb1, rgb2, first_half=first_half, seam_w=seam_w, orientation=ori)
    out, _ = finish_array(
        composed,
        edit,
        stage="final",
        frame=factory_frame(),
        stamp=factory_stamp(),
        crop_rect=None,
        meta={},
        taken=None,
        file_time=None,
        from_raw=True,
    )
    return np.ascontiguousarray(out)


def _fetch_bake(client: TestClient, url: str) -> tuple[bytes, dict[str, Any]]:
    resp = client.get(url)
    assert resp.status_code == 200, (url, resp.status_code, resp.text[:200])
    meta = json.loads(urllib.parse.unquote(resp.headers["X-GXR-Bake"]))
    return resp.content, meta


def _fetch_lut_table(client: TestClient, cube_id: str) -> tuple[bytes, dict[str, Any]]:
    resp = client.get(f"/api/luts/{cube_id}/table")
    assert resp.status_code == 200, (cube_id, resp.status_code, resp.text[:200])
    meta = json.loads(urllib.parse.unquote(resp.headers["X-GXR-Lut"]))
    return resp.content, meta


def _enable_gpu_flag(settings: Any) -> None:
    settings.home_dir.mkdir(parents=True, exist_ok=True)
    (settings.home_dir / "flags.json").write_text(json.dumps({"gpu_preview": True}), encoding="utf-8")


def test_card250_pair_gpu_parity_latency_and_memory(
    settings: Any,
    lib: Library,
    volumes: Path,
    tmp_path: Path,
) -> None:
    """验收 1–3：card250 对 (1,2) 横屏与对 (3,4) 竖屏像素一致性 + 滑块延迟 + 显存对比。"""
    _clear_desktop_cache()
    pairs = _sorted_pairs(_field_folder("card250"))
    # 1,2 = 横屏半格对；3,4 = 竖屏半格对；7 = 普通单张 RAW+JPG 对照
    staged = [pairs[0], pairs[1], pairs[2], pairs[3], pairs[6]]
    _stage_volume_pairs(volumes, staged)
    _setup_g250_calibrated_home(settings)
    _enable_gpu_flag(settings)
    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    by_idx = _index_shots_by_jpg_sha(lib, staged)
    shot_p12 = by_idx[2]
    shot_p34 = by_idx[4]
    shot_single = by_idx[5]
    assert shot_p12.get("pair", {}).get("role") == "second"
    assert shot_p34.get("pair", {}).get("role") == "second"
    assert shot_p12.get("orientation") in (1, 3)
    assert shot_p34.get("orientation") in (6, 8)
    lib.close()

    app = create_app(settings)
    harness = tmp_path / "pair_harness.mjs"
    harness.write_text(PAIR_HARNESS_JS, encoding="utf-8")

    files: dict[str, bytes] = {}
    cases: list[dict[str, Any]] = []
    refs: dict[str, np.ndarray] = {}
    case_meta: dict[str, dict[str, Any]] = {}

    adj_exp_curve = {"exposure_ev": 0.45, "contrast": 20, "shadow": 15, "saturation": 10}
    curve_spec = {"master": [[0, 0], [64, 52], [128, 132], [192, 206], [255, 255]]}
    identity_geo = {"straighten": 0, "rotate90": 0, "flip": False, "vertical": 0, "horizontal": 0, "distortion": 0, "box": None}

    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        render = app.state.ctx.render
        lib_live = app.state.ctx.lib

        # 上传显示类 LUT 与 Log LUT
        up_disp = client.post(
            "/api/lut/upload",
            content=cube_bytes(lambda x: np.clip(x * np.array([0.92, 1.02, 1.06]), 0.0, 1.0), size=17),
            headers={"X-GXR-Filename": "pair-disp.cube"},
        )
        assert up_disp.status_code == 200, up_disp.text
        disp_lut_id = up_disp.json()["cube_id"]
        assert client.patch(f"/api/luts/{disp_lut_id}", json={"input_space": "srgb"}).status_code == 200
        disp_lut_bytes, disp_lut_meta = _fetch_lut_table(client, disp_lut_id)
        files["/lut_disp.bin"] = disp_lut_bytes
        disp_target, _ = render.luts.target(disp_lut_id)

        up_log = client.post(
            "/api/lut/upload",
            content=cube_bytes(lambda x: np.clip(x * np.array([1.08, 0.96, 0.88]) + 0.02, 0.0, 1.0), size=17),
            headers={"X-GXR-Filename": "pair-log.cube"},
        )
        assert up_log.status_code == 200, up_log.text
        log_lut_id = up_log.json()["cube_id"]
        assert client.patch(f"/api/luts/{log_lut_id}", json={"input_space": "llog"}).status_code == 200
        log_lut_bytes, log_lut_meta = _fetch_lut_table(client, log_lut_id)
        files["/lut_log.bin"] = log_lut_bytes
        log_target, _ = render.luts.target(log_lut_id)

        # 拉取单张照片的 /gpu 和 RAW + bake（作为延迟和显存基准）
        sid_s = shot_single["shot_id"]
        g_single = client.get(f"/api/photos/{sid_s}/gpu").json()
        assert g_single.get("enabled") is True and g_single.get("raw") is not None, g_single
        raw_s_resp = client.get(g_single["raw"]["url"])
        assert raw_s_resp.status_code == 200
        files["/single.dng"] = raw_s_resp.content
        s_unit = g_single["unit"] or "jlaunch"
        explicit_recipe = {"kind": "builtin", "unit": s_unit, "slot": 1, "levels": {}}
        rec_q = urllib.parse.quote(json.dumps(explicit_recipe))
        b_single_bytes, b_single_meta = _fetch_bake(client, f"/api/photos/{sid_s}/bake?recipe={rec_q}")
        files["/bake_single.bin"] = b_single_bytes

        # 组装两对半格用例
        pair_defs = [("pair_1_2_land", shot_p12), ("pair_3_4_port", shot_p34)]
        for plabel, pshot in pair_defs:
            sid = pshot["shot_id"]
            g = client.get(f"/api/photos/{sid}/gpu").json()
            assert g.get("enabled") is True and g.get("pair") is not None, f"{plabel} /gpu not enabled for pair: {g}"
            pinfo = g["pair"]
            s0, s1 = pinfo["sources"][0], pinfo["sources"][1]
            r0 = client.get(s0["raw"]["url"])
            r1 = client.get(s1["raw"]["url"])
            assert r0.status_code == 200 and r1.status_code == 200
            u0, u1 = f"/{plabel}_0.dng", f"/{plabel}_1.dng"
            files[u0] = r0.content
            files[u1] = r1.content

            pair_cfg = {
                "first_half": pinfo["first_half"],
                "orientation": pinfo["orientation"],
                "window": pinfo["window"],
                "sources": [
                    {"layout": s0["raw"], "scene": s0["scene"]},
                    {"layout": s1["raw"], "scene": s1["scene"]},
                ],
            }

            # Untouched bakes (each half's own shooting recipe)
            ub0_bytes, ub0_meta = _fetch_bake(client, f"/api/photos/{sid}/bake?half=0")
            ub1_bytes, ub1_meta = _fetch_bake(client, f"/api/photos/{sid}/bake?half=1")
            files[f"/{plabel}_ub0.bin"] = ub0_bytes
            files[f"/{plabel}_ub1.bin"] = ub1_bytes

            # Explicit shared recipe bakes
            eb0_bytes, eb0_meta = _fetch_bake(client, f"/api/photos/{sid}/bake?half=0&recipe={rec_q}")
            eb1_bytes, eb1_meta = _fetch_bake(client, f"/api/photos/{sid}/bake?half=1&recipe={rec_q}")
            files[f"/{plabel}_eb0.bin"] = eb0_bytes
            files[f"/{plabel}_eb1.bin"] = eb1_bytes

            def _add_case(
                cid: str,
                *,
                edit_obj: EditState,
                b0_url: str | None,
                b0_meta: dict[str, Any] | None,
                b1_url: str | None,
                b1_meta: dict[str, Any] | None,
                lut_info: dict[str, Any] | None = None,
                adjust_dict: dict[str, Any] | None = None,
                curve_dict: dict[str, Any] | None = None,
                display_t: Any = None,
                log_t: Any = None,
                measure_mem: bool = False,
            ) -> None:
                ref = _python_pair_reference(
                    render,
                    lib_live,
                    pshot,
                    edit_obj,
                    display_target=display_t,
                    log_target=log_t,
                )
                refs[cid] = ref
                case_meta[cid] = {
                    "orientation": pinfo["orientation"],
                    "seam_x0": pinfo["window"]["seam_x0"],
                    "seam_w": pinfo["window"]["seam_w"],
                    "display_lut": display_t is not None,
                    "layer": tuple(pinfo["layer"]),
                    "same_bake": bool(b0_meta and b1_meta and b0_meta["key"] == b1_meta["key"]),
                }
                cases.append(
                    {
                        "id": cid,
                        "source": plabel,
                        "pairRaw": {"url0": u0, "url1": u1, "config": pair_cfg},
                        "bake": {"url": b0_url, "key": b0_meta["key"], "meta": b0_meta} if b0_url and b0_meta else None,
                        "bake2": {"url": b1_url, "key": b1_meta["key"], "meta": b1_meta} if b1_url and b1_meta else None,
                        "lut": lut_info,
                        "look": {
                            "recipe": b0_meta["key"] if b0_meta else None,
                            "recipe2": b1_meta["key"] if b1_meta else None,
                            "lut": lut_info["key"] if lut_info else None,
                            "lutStrength": 1,
                            "adjust": adjust_dict or {},
                            "curve": curve_dict,
                        },
                        "geometry": identity_geo,
                        "measureMemory": measure_mem,
                    }
                )

            _add_case(
                f"{plabel}_untouched",
                edit_obj=EditState(),
                b0_url=f"/{plabel}_ub0.bin",
                b0_meta=ub0_meta,
                b1_url=f"/{plabel}_ub1.bin",
                b1_meta=ub1_meta,
                measure_mem=(plabel == "pair_1_2_land"),
            )
            _add_case(
                f"{plabel}_explicit_recipe",
                edit_obj=EditState(recipe=parse_recipe(explicit_recipe)),
                b0_url=f"/{plabel}_eb0.bin",
                b0_meta=eb0_meta,
                b1_url=f"/{plabel}_eb1.bin",
                b1_meta=eb1_meta,
            )
            _add_case(
                f"{plabel}_display_lut",
                edit_obj=EditState(lut=disp_lut_id),
                b0_url=f"/{plabel}_ub0.bin",
                b0_meta=ub0_meta,
                b1_url=f"/{plabel}_ub1.bin",
                b1_meta=ub1_meta,
                lut_info={"url": "/lut_disp.bin", "key": disp_lut_meta["key"], "meta": disp_lut_meta},
                display_t=disp_target,
            )
            _add_case(
                f"{plabel}_exp_curve",
                edit_obj=EditState(adjust=parse_adjust(adj_exp_curve), curve=parse_curve(curve_spec)),
                b0_url=f"/{plabel}_ub0.bin",
                b0_meta=ub0_meta,
                b1_url=f"/{plabel}_ub1.bin",
                b1_meta=ub1_meta,
                adjust_dict=adj_exp_curve,
                curve_dict=curve_spec,
            )
            _add_case(
                f"{plabel}_log_lut",
                edit_obj=EditState(lut=log_lut_id, adjust=parse_adjust({"exposure_ev": -0.2})),
                b0_url=None,
                b0_meta=None,
                b1_url=None,
                b1_meta=None,
                lut_info={"url": "/lut_log.bin", "key": log_lut_meta["key"], "meta": log_lut_meta},
                adjust_dict={"exposure_ev": -0.2},
                log_t=log_target,
            )

            # 校验服务端 /render?stage=final 与参考尺寸/方向/接缝一致
            r_srv = client.post(
                f"/api/photos/{sid}/render?stage=final&max_edge=2144&quality=95",
                json={"recipe": None, "adjust": {}, "curve": None},
            )
            assert r_srv.status_code == 200, r_srv.text
            with Image.open(io.BytesIO(r_srv.content)) as im_srv:
                srv_rgb = np.asarray(im_srv.convert("RGB"))
            assert srv_rgb.shape == refs[f"{plabel}_untouched"].shape, (plabel, srv_rgb.shape)

        # 单张照片的独立显存对照 + 成对/单张交替 A/B 延迟基准（同一浏览器进程交替测，抵消共享宿主机波动）
        pair_bench_case = next(c for c in cases if c["id"] == "pair_1_2_land_explicit_recipe")
        single_look = {
            "recipe": b_single_meta["key"],
            "lut": None,
            "lutStrength": 1,
            "adjust": {},
            "curve": None,
        }
        single_cases = [
            {
                "id": "single_ref",
                "source": "single",
                "raw": {"url": "/single.dng", "layout": g_single["raw"], "scene": g_single["scene"]},
                "bake": {"url": "/bake_single.bin", "key": b_single_meta["key"], "meta": b_single_meta},
                "look": single_look,
                "geometry": identity_geo,
                "measureMemory": True,
                "skipReadback": True,
            },
            {
                "id": "bench_ab",
                "source": pair_bench_case["source"],
                "pairRaw": pair_bench_case["pairRaw"],
                "bake": pair_bench_case["bake"],
                "bake2": pair_bench_case["bake2"],
                "look": pair_bench_case["look"],
                "geometry": identity_geo,
                "skipReadback": True,
                "benchAB": {
                    "steps": 15,
                    "pair": {
                        "source": pair_bench_case["source"],
                        "look": pair_bench_case["look"],
                        "geometry": identity_geo,
                    },
                    "single": {
                        "source": "single",
                        "look": single_look,
                        "geometry": identity_geo,
                    },
                },
            },
        ]

    # 串行运行两次无头 Chrome：1) 成对一致性 + 独立显存；2) 单张独立显存 + 成对/单张交替 A/B 延迟基准
    run_pair_dir = tmp_path / "run_pair"
    run_single_dir = tmp_path / "run_single"
    run_pair_dir.mkdir()
    run_single_dir.mkdir()
    out_pair, sum_pair = run_engine(run_pair_dir, files, cases, timeout=300.0, harness=harness)
    _out_single, sum_single = run_engine(run_single_dir, files, single_cases, timeout=240.0, harness=harness)

    # 1) 校验每个用例的像素一致性与接缝
    report_rows: dict[str, Any] = {}
    for cid, ref in refs.items():
        assert cid in out_pair, f"missing readback for {cid}"
        meta = case_meta[cid]
        w, h = meta["layer"]
        mask = valid_mask(w, h, Geometry(), None)
        res = evaluate(out_pair[cid], ref, mask=mask, display_lut=meta["display_lut"])
        seam_gpu = _seam_slice(out_pair[cid], orientation=meta["orientation"], seam_x0=meta["seam_x0"], seam_w=meta["seam_w"])
        seam_ref = _seam_slice(ref, orientation=meta["orientation"], seam_x0=meta["seam_x0"], seam_w=meta["seam_w"])
        seam_max_diff = int(np.abs(seam_gpu.astype(np.int16) - seam_ref.astype(np.int16)).max())
        report_rows[cid] = {
            "same_bake": meta["same_bake"],
            "within_1": res["within_1"],
            "mean_de00": res["mean_de00"],
            "max_out_band": res["max_out_band"],
            "over_1": res["over_1"],
            "pixels": res["pixels"],
            "seam_max_diff": seam_max_diff,
            "pass": res["pass"],
            "why": res["why"],
        }
        assert res["pass"], f"{cid}: {res}"
        assert seam_max_diff <= 2, f"{cid}: seam max diff {seam_max_diff} > 2"

    # 2) 延迟对比（pair vs single，交替 A/B 采样）
    ab_metrics = sum_single["metrics"]["bench_ab"]
    pair_lat = ab_metrics["pairSliderMs"]
    single_lat = ab_metrics["singleSliderMs"]
    med_pair = float(statistics.median(pair_lat))
    med_single = float(statistics.median(single_lat))
    lat_ratio = med_pair / med_single

    # 3) 显存对比（pair vs single，独立进程单源采样）
    pair_mem = sum_pair["metrics"]["pair_1_2_land_untouched"]
    single_mem = sum_single["metrics"]["single_ref"]
    pair_src_bytes = int(pair_mem["sourceBytes"])
    single_src_bytes = int(single_mem["sourceBytes"])
    pair_total_tex = pair_src_bytes + int(pair_mem["colorFinalBytes"])
    single_total_tex = single_src_bytes + int(single_mem["colorFinalBytes"])
    src_ratio = pair_src_bytes / single_src_bytes
    total_ratio = pair_total_tex / single_total_tex

    metrics_out = {
        "adapter": sum_pair.get("info"),
        "parity": report_rows,
        "latency": {
            "pair_median_ms": round(med_pair, 3),
            "single_median_ms": round(med_single, 3),
            "ratio": round(lat_ratio, 4),
            "pair_samples_ms": [round(x, 2) for x in pair_lat],
            "single_samples_ms": [round(x, 2) for x in single_lat],
        },
        "memory": {
            "pair_source_bytes": pair_src_bytes,
            "single_source_bytes": single_src_bytes,
            "source_ratio": round(src_ratio, 4),
            "pair_total_tex_bytes": pair_total_tex,
            "single_total_tex_bytes": single_total_tex,
            "total_tex_ratio": round(total_ratio, 4),
        },
    }
    dump_path = os.environ.get("GXR_P1B_GPU_METRICS_OUT")
    if dump_path:
        Path(dump_path).write_text(json.dumps(metrics_out, indent=2) + "\n", encoding="utf-8")

    assert src_ratio <= 1.2, f"pair source texture bytes ratio {src_ratio:.4f} > 1.2"
    assert total_ratio <= 1.2, f"pair total texture bytes ratio {total_ratio:.4f} > 1.2"
    assert lat_ratio <= 1.3, f"pair slider latency ratio {lat_ratio:.4f} > 1.3 ({med_pair:.2f}ms vs {med_single:.2f}ms)"
