"""显卡预览前端的状态与版本（P1-02、P2-03、P2-04 的回归）：真正的 gpu_stage.js，接口、fetch 和引擎用替身。

每个场景单独起一个 node 进程；请求什么时候完成由脚本控制，不依赖时间间隔。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "web" / "assets"
SCRIPT = Path(__file__).parent / "support" / "gpu_stage_state.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过前端状态测试")

ENGINE_STUB = """
export class Engine {
  static async create() { return new Engine(); }
  constructor() {
    this.info = { vendor: 'stub' };
    this.sources = new Map(); this.bakes = new Map(); this.luts = new Map(); this.current = null;
  }
  log(...a) { globalThis.engineLog.push(a); }
  hasSource(k) { return this.sources.has(k); }
  async addRaw(k) { this.sources.set(k, { w: 100, h: 80 }); this.log('addRaw', k); }
  addJpg(k) { this.sources.set(k, { w: 120, h: 80 }); this.log('addJpg', k); }
  sourceSize(k) { return this.sources.get(k) || null; }
  hasBake(k) { return this.bakes.has(k); }
  addBake(k) { this.bakes.set(k, true); this.log('addBake', k); }
  hasLut(k) { return this.luts.has(k); }
  addLut(k) { this.luts.set(k, true); this.log('addLut', k); }
  show(k) { this.current = k; this.log('show', k); }
  setLook(l) { this.log('setLook', { ...l }); }
  setGeometry(g) { this.log('setGeometry', { ...g }); }
  currentKey() { return this.current; }
  finalSize() { return { w: 100, h: 80 }; }
  attach() {}
  redraw() {}
}
"""


def run(tmp_path: Path, scenario: str) -> dict[str, object]:
    folder = tmp_path / "modules"
    (folder / "gpu").mkdir(parents=True)
    for name in ("gpu_stage.js", "gpu_pref.js", "gpu_warp.js", "state.js"):
        shutil.copyfile(ASSETS / name, folder / name)
    (folder / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
    (folder / "api.js").write_text("export const api = (...args) => globalThis.apiStub(...args);\n", encoding="utf-8")
    (folder / "gpu" / "engine.js").write_text(ENGINE_STUB, encoding="utf-8")
    res = subprocess.run(["node", str(SCRIPT), str(folder), scenario], capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)  # type: ignore[no-any-return]


def test_lut_changed_to_an_unsupported_space_falls_back_to_the_server(tmp_path):
    out = run(tmp_path, "lut-patched-to-vlog")
    assert out["first"] == "server"  # 表还在算：先用服务器出图
    assert out["afterLoad"] == "gpu" and out["lutKey"] == "L-v1"
    assert out["afterEvent"] == "server" and out["again"] == "server"  # 换代后重新取表，422 → 服务器（沿用原来的提示）
    assert out["tableRequests"] == 2 and out["addedLuts"] == ["L-v1"]


def test_camera_data_change_reloads_the_recipe_table(tmp_path):
    out = run(tmp_path, "camdata-changed")
    assert out["before"] == "gpu" and out["recipeBefore"] == "K1"
    assert out["after"] == "gpu" and out["recipeAfter"] == "K2"
    assert out["bakeUrls"] == ["v1", "v2"]


def test_a_late_success_from_the_old_generation_is_ignored(tmp_path):
    out = run(tmp_path, "late-success")
    assert out["addedAfterStale"] == []  # 旧代的表没有装进引擎
    assert out["final"] == "gpu" and out["recipe"] == "K2" and out["addedBakes"] == ["K2"]


def test_a_late_failure_from_the_old_generation_does_not_mark_the_new_one(tmp_path):
    out = run(tmp_path, "late-failure")
    assert out["addedAfterStale"] == []
    assert out["final"] == "gpu" and out["recipe"] == "K2"  # 旧代失败没有把新代标成失败


def test_prefetch_runs_once_per_photo_not_per_slider_input(tmp_path):
    out = run(tmp_path, "prefetch-dedupe")
    assert out["first"] == "gpu"
    assert out["neighbourEdits"] <= 2 and out["neighbourInfos"] <= 2


def test_flag_off_makes_no_gpu_requests(tmp_path):
    out = run(tmp_path, "flag-off")
    assert out == {"way": "server", "gpuInfoRequests": 0, "engineCreated": False}
