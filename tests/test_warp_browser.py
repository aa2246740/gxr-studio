"""真浏览器里拖 垂直 / 水平 / 畸变：显卡预览不等服务器。

起一个真服务器（合成的假卡，只听 127.0.0.1）和无头 Chrome（和显卡一致性测试同一套找法、同一组开关），
由 tests/support/warp_browser.mjs 用 DevTools 协议拖滑块。判据都不看钟：
- 拖动的整个过程（和松手后的 1.5 秒）一个 /render 也没有，也没有 /gpu/box：服务器出图在等的话，这两项不可能是 0；
- 每一次 input 之后两帧之内，引擎的成片版本变了（舞台在等服务器的话，两帧里变不了）；耗时只打印，不判；
- 松手以后，裁切框和服务器收好的框一样；回到成片页，画布仍是显卡的，形状是裁切后的形状，也没有 /render。
显卡预览关着（走服务器出图）时行为不变：拖滑块照旧请求 /render。
找不到 Chrome 或 node 时跳过；Linux 无头 Chrome 的软件显卡显示不出 WebGPU 画布时，显卡那一项跳过（macOS 强制跑）。
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys

import pytest

from gpu_support import require_chrome
from test_viewer_browser import DEADLINE_S, FLAGS, cannot_present, live  # noqa: F401  live 是夹具

DRIVER = os.path.join(os.path.dirname(__file__), "support", "warp_browser.mjs")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过真浏览器测试")


def drive_warp(live, tmp_path, mode: str) -> dict:  # noqa: F811
    chrome = require_chrome()
    flags = list(FLAGS)
    if sys.platform.startswith("linux") and os.environ.get("CI"):
        flags += ["--no-sandbox", "--disable-dev-shm-usage"]
    env = {**os.environ, "CHROME_FLAGS": json.dumps(flags), "VIEWER_DEADLINE_MS": str(DEADLINE_S * 1000)}
    try:
        res = subprocess.run(  # noqa: S603
            ["node", DRIVER, chrome, str(live["port"]), live["token"], live["photo"], mode, str(tmp_path / f"chrome-{mode}")],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=DEADLINE_S + 40,
            encoding="utf-8",
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        err = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        pytest.fail(f"驱动脚本没有在时限内结束\n进度：\n{err[-3000:]}")
    assert res.stdout.strip(), res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert "error" not in out, f"{json.dumps(out, ensure_ascii=False, indent=1)}\n进度：\n{res.stderr[-3000:]}"
    assert out["ready"] is True
    return out  # type: ignore[no-any-return]


def near(a, b, eps=0.6):
    return all(abs(x - y) < eps for x, y in zip(a, b, strict=True))


def test_dragging_perspective_and_distortion_never_waits_for_the_server(live, tmp_path):  # noqa: F811
    out = drive_warp(live, tmp_path, "gpu")
    if not out["gpu"]:
        detail = json.dumps(out.get("gpuDiag"), ensure_ascii=False)
        if sys.platform.startswith("linux") and cannot_present(out):
            pytest.skip(f"Linux 无头 Chrome 的软件显卡不能把 WebGPU 画布显示出来：{detail}")
        if os.environ.get("GXR_REQUIRE_GPU_TEST") == "1":
            pytest.fail(f"GXR_REQUIRE_GPU_TEST=1，但等了 120 秒大图还是没有走显卡预览：{detail}")
        pytest.skip(f"这台机器的 Chrome 没有走显卡预览：{detail}")
    sliders = out["sliders"]
    assert [s["label"] for s in sliders] == ["垂直", "水平", "畸变"]
    all_ms = []
    for s in sliders:
        name = s["label"]
        assert s["gpu"] is True, (name, s)
        # 硬判据 1：整个过程没有 /render，也没有 /gpu/box（有效区在浏览器里算）
        assert s["renders"] == 0 and s["boxRequests"] == 0, (name, s["requestsWhileDragging"])
        # 硬判据 2：每一次 input 之后两帧之内，引擎的成片版本变了；透视 / 畸变一变画布就换尺寸（从第二步起不为 0）
        steps = s["steps"]
        assert len(steps) == 10 and all(p["changed"] for p in steps), (name, steps)
        assert all(p["frames"] <= 2 for p in steps), (name, steps)
        assert any(p["resized"] for p in steps), (name, steps)
        all_ms += [p["ms"] for p in steps]
        # 松手：几何写进了服务器，框和服务器收好的一样，还是显卡画面
        after = s["afterRelease"]
        assert after["savedGeometry"] is True and after["gpu"] is True, (name, after)
        assert near(after["box"], after["server"]), (name, after)
    assert out["dragRendersTotal"] == 0, out["dragRendersTotal"]
    # 成片页：显卡画布，形状是裁切后的形状（和引擎的成片尺寸一致，也和服务器的框占画布的比例一致），没有 /render
    final = out["final"]
    assert final["gpu"] is True and final["renders"] == 0, final
    shown = final["engineSize"]
    assert abs(final["canvasAspect"] - shown["w"] / shown["h"]) < 0.02, final
    canvas = final["engineCanvas"]
    x, y, w, h = final["serverRect"]
    assert abs(shown["w"] - round(w * canvas["w"])) <= 2 and abs(shown["h"] - round(h * canvas["h"])) <= 2, final
    print("warp-drag", json.dumps({"ms_median": statistics.median(all_ms), "ms_max": max(all_ms), "steps": len(all_ms)}))


def test_dragging_with_the_server_preview_is_unchanged(live, tmp_path):  # noqa: F811
    out = drive_warp(live, tmp_path, "server")
    assert out["gpu"] is False
    for s in out["sliders"]:
        assert s["renders"] >= 1, (s["label"], s["requestsWhileDragging"])  # 服务器出图照旧
        assert s["afterRelease"]["savedGeometry"] is True, s
        # 框跟不跟服务器收好的框，是保存流程（PR #55）的事，这里不判：这一项只管出图走哪条路
