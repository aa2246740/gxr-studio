"""用可控的 Promise 和时钟卡住每个异步边界，再从 document 发出真实输入事件。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_gpu_state import ENGINE_STUB

ASSETS = Path(__file__).resolve().parents[1] / "web" / "assets"
SCRIPT = Path(__file__).parent / "support" / "look_card_interrupts.mjs"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")


def scene(tmp_path: Path, phase: str, gesture: str = "pointerdown") -> dict:
    assets = tmp_path / "assets"
    shutil.copytree(ASSETS, assets)
    (assets / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
    (assets / "gpu" / "engine.js").write_text(ENGINE_STUB, encoding="utf-8")
    res = subprocess.run(["node", str(SCRIPT), str(assets), phase, gesture], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


@pytest.mark.parametrize("gesture", ["pointerdown", "keydown", "wheel", "input", "pointer-tap"])
@pytest.mark.parametrize("phase", ["gpu-bake", "gpu-readback", "server-response", "server-blob", "server-bitmap"])
def test_a_gesture_interrupts_an_in_flight_card_and_it_retries_after_settling(tmp_path, phase, gesture):
    out = scene(tmp_path, phase, gesture)
    assert out["reached"] is True, out  # 确实卡在指定边界，而不是根本没开始
    if phase.startswith("server-"):
        assert out["aborted"] is True, out
    assert out["during"] == {"painted": 0, "gpu": 0, "server": 0, "pixelWrites": 0, "failed": False}, out
    if phase == "gpu-bake":
        assert out["thumbsDuring"] == 0, out
    assert out["serverStartsDuring"] == (0 if phase.startswith("gpu-") else 1), out
    assert out["bitmapDrawsDuring"] == 0, out
    assert out["after"]["painted"] == 1 and out["after"]["pixelWrites"] == 1 and out["after"]["failed"] is False, out
    assert out["after"]["gpu"] == int(phase.startswith("gpu-")), out
    assert out["after"]["server"] == int(phase.startswith("server-")), out


def test_cached_cards_follow_aspect_and_gpu_preference_and_clear_on_an_off_on_cycle(tmp_path):
    out = scene(tmp_path, "cache")
    assert out["first"] == {"gpu": 1, "server": 0, "cached": 0}, out
    assert out["same"] == {"gpu": 0, "server": 0, "cached": 1}, out
    assert out["aspect"] == {"gpu": 1, "server": 0, "cached": 0}, out
    assert out["server"] == {"gpu": 0, "server": 1, "cached": 0}, out
    assert out["backToGpu"] == {"gpu": 1, "server": 0, "cached": 0}, out
    assert out["offOn"] == {"gpu": 1, "server": 0, "cached": 0}, out
