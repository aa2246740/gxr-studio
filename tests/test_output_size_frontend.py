"""成品数字同步更新，精确尺寸共用；边框和配方旋钮不重复查尺寸。"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from test_edit_route import SUPPORT, prepare

SCRIPT = """
import { pathToFileURL } from 'node:url';
import { installDom, tick } from './mini_dom.mjs';
installDom();
const file = (name) => pathToFileURL(`${process.argv[2]}/${name}`).href;
const calls = [];
const data = { photo: [2136, 2848], meta: {}, crop_preset: '4:3', ratio: [3, 4],
  sizes: { original: [2364, 3152], large: [2250, 3000], medium: [1536, 2048], small: [1200, 1600] } };
let response = data;
globalThis.apiStub = async (url, opts) => { calls.push({ url, body: opts.body }); return response; };
const { state } = await import(file('state.js'));
const { normalizeFrame } = await import(file('finish.js'));
const { photoSize } = await import(file('output_photo_size.js'));
const { decorOf } = await import(file('stage_finish.js'));
const { frameSizeLine, exportSizePreview } = await import(file('output_size.js'));
state.camdata = { dataVersion: 'v1' };
const view = { photo: { id: 'p' }, ed: { v2: true, frame: normalizeFrame({ style: 'gallery' }), stamp: { enabled: false },
  recipe: { kind: 'builtin', unit: 'jlaunch', slot: 2, levels: {} }, lut: null, geometry: {} },
  crop: { preset: '4:3', rect: { x: 0.2, y: 0, w: 0.5, h: 1 }, frame: { width: 4288, height: 2848 } } };
await photoSize(view).promise;
const deco = decorOf(view, 2136, 2846); // 半分辨率预览猜错两个像素，版面仍用导出的精确尺寸
const out = { deco: [deco.ew, deco.eh, deco.width, deco.height], lines: [] };
const liveLine = frameSizeLine(view);
out.lines.push(liveLine.textContent);
view.ed.frame.output_ratio = 'original';
view.ed.frame.weight = 'thick';
liveLine.update();
out.lines.push(liveLine.textContent);
view.ed.frame = normalizeFrame({ style: 'gallery' });
liveLine.update();
out.lines.push(liveLine.textContent);
view.ed.recipe.levels = { Amt: 3 };
view.ed.recipe.memory = { a: 1 };
await photoSize(view).promise;
out.frameAndKnobQueries = calls.length;
view.ed.geometry.rotate90 = 1;
await photoSize(view).promise;
view.crop.rect.w = 0.4;
await photoSize(view).promise;
state.camdata.dataVersion = 'v2';
await photoSize(view).promise;
out.changedQueries = calls.length;
const beforeDialog = calls.length;
let tier = 'original';
let scope = 'ids';
const preview = exportSizePreview({ ids: ['p'], getSize: () => tier, getScope: () => scope });
await tick();
out.tiers = [];
for (tier of ['original', 'large', 'medium', 'small']) { preview.paint(); out.tiers.push(preview.line.textContent); }
out.dialogQueries = calls.length - beforeDialog;
const beforeImport = calls.length;
response = { ...data, sizes: { ...data.sizes, original: [3000, 4000] } };
state.camdata.dataVersion = 'v3';
tier = 'original'; preview.paint(); await tick();
out.afterImport = preview.line.textContent;
out.importQueries = calls.length - beforeImport;
let finishOld;
response = new Promise((resolve) => { finishOld = resolve; });
state.camdata.dataVersion = 'v4'; preview.paint(); await tick();
response = { ...data, sizes: { ...data.sizes, original: [3300, 4400] } };
state.camdata.dataVersion = 'v5'; preview.paint(); await tick();
finishOld({ ...data, sizes: { ...data.sizes, original: [1500, 2000] } });
await tick();
out.afterStaleQuery = preview.line.textContent;
scope = 'date'; preview.paint(); out.batch = preview.line.textContent;
console.log(JSON.stringify(out));
process.exit(0);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node")
def test_exact_size_is_shared_and_frame_choices_update_without_querying(tmp_path):
    folder = prepare(tmp_path)
    shutil.copy(SUPPORT / "mini_dom.mjs", tmp_path / "mini_dom.mjs")
    script = tmp_path / "output_size.mjs"
    script.write_text(SCRIPT, encoding="utf-8")
    result = subprocess.run(["node", str(script), str(folder)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip())
    assert out["deco"] == [2136, 2848, 2364, 3152]
    assert out["lines"] == ["成品 2364 × 3152 像素 · 3:4", "成品 2534 × 3246 像素", "成品 2364 × 3152 像素 · 3:4"]
    assert out["frameAndKnobQueries"] == 1
    assert out["changedQueries"] == 4
    assert out["dialogQueries"] == 1
    assert out["importQueries"] == 1
    assert out["afterImport"] == "3000 × 4000 像素 · 3:4"
    assert out["afterStaleQuery"] == "3300 × 4400 像素 · 3:4"
    assert out["tiers"] == ["2364 × 3152 像素 · 3:4", "2250 × 3000 像素 · 3:4", "1536 × 2048 像素 · 3:4", "1200 × 1600 像素 · 3:4"]
    assert out["batch"] == "每张照片按各自比例导出，成品尺寸可能不同。"
