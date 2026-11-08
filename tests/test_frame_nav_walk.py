"""导航测试的驱动逻辑（tests/support/frame_nav_walk.mjs 的 walkRounds）：显卡接不了手时怎么交代，走到一半卡住时怎么停。

Linux 无头 Chrome 的软件显卡能建 WebGPU 设备、画布也会短暂出现，但上不了屏（Chrome 日志里是共享图像建不起来、取不到），
页面退回服务器出图。CI 上这条路只有 Ubuntu 能走到，所以把决定的那部分逻辑用假的页面和假的日志在 node 里直接测：
接得住且日志干净才往下走；接不住、没留住、日志里有共享图像的错，都只交出一步 gpu: false，带着判断用到的东西和诊断；
走到一半有一步等不到想要的样子，交出这一步、后面的轮不走、显卡路径上带着诊断；服务器出图的路径不请诊断。
真浏览器里的整套走法在 tests/test_frame_navigation_browser.py。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SUPPORT = Path(__file__).resolve().parent / "support"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过前端纯函数测试")

SCRIPT = """
import { walkRounds } from './frame_nav_walk.mjs';

// 假的 DevTools 调用、假的页面（evalIn 按顺序回答）、假的诊断和日志
async function run({ wantGpu, answers, presentError }) {
  const log = { navigations: 0, diagnostics: 0, evals: 0 };
  const queue = [...answers];
  const out = await walkRounds({
    wantGpu,
    origin: 'http://x',
    pauseMs: 0,
    step: () => {},
    call: async () => { log.navigations += 1; },
    evalIn: async () => { log.evals += 1; return queue.shift(); },
    collectGpuDiagnostics: async () => { log.diagnostics += 1; },
    presentErrors: () => presentError,
  });
  return { ...out, log };
}
const probe = (verdict) => ({ verdict, waitMs: 12, inputs: { engine: 'alive' }, timeline: [{ t: 1 }] });
const ok = (n) => Array.from({ length: n }, (_, i) => ({ label: `step ${i}`, gpu: true }));
const stuck = (gpu) => ({ label: 'stuck', gpu, timedOut: true });
const out = {};

// 1) 画布留住了、日志也干净：往下走两轮，不请诊断
out.holds = await run({ wantGpu: true, presentError: false, answers: [probe('gpu'), ok(3), ok(2)] });
// 2) 画布留住了，但 Chrome 日志里有共享图像的错：上不了屏，只交出一步，带诊断
out.logsError = await run({ wantGpu: true, presentError: true, answers: [probe('gpu')] });
// 3) 画布出现过又没了 / 引擎没了 / 没有适配器 / 等到时限：只交出一步，带诊断和判断用到的东西（日志干净也一样）
for (const verdict of ['gpu-dropped', 'engine-lost', 'no-adapter', 'timeout']) {
  out[verdict] = await run({ wantGpu: true, presentError: false, answers: [probe(verdict)] });
}
// 4) 走到一半有一步显卡没接住（timedOut，gpu: false）：后面的轮不走，带诊断
out.abortsOnGpu = await run({ wantGpu: true, presentError: false, answers: [probe('gpu'), [...ok(2), stuck(false)], ok(5)] });
// 5) 走到一半有一步卡住，但显卡还在（gpu: true）：是舞台的问题，不是显卡的问题，不请显卡诊断，后面的轮也不走
out.abortsOnStage = await run({ wantGpu: true, presentError: false, answers: [probe('gpu'), [...ok(2), stuck(true)], ok(5)] });
// 6) 服务器出图：不探显卡；卡住也不请显卡诊断，后面的轮不走
out.serverAbort = await run({ wantGpu: false, presentError: true, answers: [[...ok(1), stuck(false)], ok(5)] });
out.serverFine = await run({ wantGpu: false, presentError: true, answers: [ok(3), ok(4)] });
console.log(JSON.stringify(out));
"""


def test_the_walk_decides_what_to_hand_back_when_the_gpu_cannot_present(tmp_path: Path) -> None:
    (tmp_path / "frame_nav_walk.mjs").write_text((SUPPORT / "frame_nav_walk.mjs").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
    (tmp_path / "check.mjs").write_text(SCRIPT, encoding="utf-8")
    res = subprocess.run(["node", "check.mjs"], capture_output=True, text=True, cwd=tmp_path, encoding="utf-8")
    assert res.returncode == 0, res.stderr[-2000:]
    out = json.loads(res.stdout)

    holds = out["holds"]
    assert len(holds["steps"]) == 5 and holds["log"]["diagnostics"] == 0 and holds["gpuWaitMs"] == 12, holds

    log = out["logsError"]
    assert [s["verdict"] for s in log["steps"]] == ["cannot-present"], log
    assert log["steps"][0]["gpu"] is False and log["steps"][0]["presentError"] is True, log
    assert log["steps"][0]["inputs"] == {"engine": "alive"} and log["steps"][0]["timeline"] == [{"t": 1}], log  # 判断用到的东西带着
    assert log["log"]["diagnostics"] == 1 and log["log"]["evals"] == 1, log  # 没有再去走那一轮

    for verdict in ("gpu-dropped", "engine-lost", "no-adapter", "timeout"):
        got = out[verdict]
        assert [s["verdict"] for s in got["steps"]] == [verdict] and got["steps"][0]["gpu"] is False, got
        assert got["log"]["diagnostics"] == 1 and got["log"]["evals"] == 1, got

    gpu_abort = out["abortsOnGpu"]
    assert len(gpu_abort["steps"]) == 3 and gpu_abort["steps"][-1]["timedOut"] is True, gpu_abort
    assert gpu_abort["log"]["diagnostics"] == 1 and gpu_abort["log"]["evals"] == 2, gpu_abort  # 探了一次、走了一轮，第二轮没走

    stage_abort = out["abortsOnStage"]
    assert len(stage_abort["steps"]) == 3 and stage_abort["log"]["diagnostics"] == 0 and stage_abort["log"]["evals"] == 2, stage_abort

    server_abort = out["serverAbort"]
    assert len(server_abort["steps"]) == 2 and server_abort["log"] == {"navigations": 1, "diagnostics": 0, "evals": 1}, server_abort
    assert len(out["serverFine"]["steps"]) == 7 and out["serverFine"]["log"]["evals"] == 2, out["serverFine"]
