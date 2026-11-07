"""裁剪 / 几何的随机交错测试（tests/support/edit_fuzz.mjs；种子固定，失败时带种子、步骤和请求记录，可以原样重现）。

真正的 viewer / preview / correct / crop / saves 在 node 里跑，接口换成照真服务器规则写的替身（写几何时收框、换画布，
写框要校验、`fit` 收成最近的合格框、`angle_deg` 会改几何的拉直角度，随机延迟、挂住、500、rev_conflict）。
每条序列 6–10 步，从旋转、翻转、垂直 / 水平 / 畸变滑块、拉直、自动、点比例、挪框、撤销、重做、重试、等写完里抽。

每条序列等写完（重试到记录清空）以后必须：
- 服务器的几何、框、画布和界面上的一致；状态不是「还在保存」或「有修改没保存」；
- 服务器自己算的居中框只出现在点比例的时候（别的地方不能让服务器编框，用户摆好的框不能被悄悄换掉）；
- 没有故障、只有延迟时，和「没有故障、每步等写完」的理想情形结果一样；
- 写完以后撤销、重做，只走到用户见过的状态（或见过的框被服务器收小的结果）。

用法：想多跑一些（或换一段种子），设 `GXR_FUZZ_SEEDS=起始:条数`，例如 `GXR_FUZZ_SEEDS=1000:500`。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from test_edit_route import SUPPORT, prepare

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过前端随机测试")

CHUNK = 25  # 一个 node 进程跑的条数：每条序列都开一张新照片，太多会撑大内存
START, COUNT = (int(x) for x in os.environ.get("GXR_FUZZ_SEEDS", "1:300").split(":"))


def _run(tmp_path: Path, folder: Path, start: int, count: int, trace: bool = False) -> dict:
    env = {"PATH": os.environ.get("PATH", ""), "FUZZ_KEEP": "500", **({"FUZZ_TRACE": "1"} if trace else {})}
    res = subprocess.run(  # noqa: S603
        ["node", "--max-old-space-size=900", str(tmp_path / "edit_fuzz.mjs"), str(folder), str(start), str(count)],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=600,
        encoding="utf-8",
        env=env,
    )
    assert res.stdout.strip(), f"node 没有输出（种子 {start}…{start + count - 1}）：{res.stderr[-1500:]}"
    return json.loads(res.stdout.strip().splitlines()[-1])  # type: ignore[no-any-return]


def test_random_crop_and_geometry_interleavings_end_consistent(tmp_path):
    folder = prepare(tmp_path, ("adjust.js", "crop.js", "correct.js"))
    shutil.copy(SUPPORT / "edit_fuzz.mjs", tmp_path / "edit_fuzz.mjs")
    failures: list[dict] = []
    total = 0
    for first in range(START, START + COUNT, CHUNK):
        out = _run(tmp_path, folder, first, min(CHUNK, START + COUNT - first))
        total += out["sequences"]
        failures += out["failures"]
    assert total == COUNT
    if failures:
        worst = failures[0]
        traced = _run(tmp_path, folder, worst["seed"], 1, trace=True)["failures"]
        seeds = [f["seed"] for f in failures][:20]
        lines = [f"{len(failures)} / {total} 条序列不满足不变量。种子（GXR_FUZZ_SEEDS={worst['seed']}:1 可以重现）：{seeds}"]
        for f in failures[:3]:
            history = json.dumps(f.get("history"), ensure_ascii=False)
            lines += [f"seed {f['seed']}", "  步骤：" + " ".join(f["steps"]), *(f"  ! {p}" for p in f["problems"]), "  撤销重做：" + history]
        if traced:
            lines += ["", f"seed {worst['seed']} 的请求记录："] + [f"  {t[:300]}" for t in (traced[0].get("trace") or [])[:120]]
        pytest.fail("\n".join(lines))
