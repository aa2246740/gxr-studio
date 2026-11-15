"""真 Chrome 里的配方卡片：卡片出来、有真像素、点一张就是套这个配方、卡片和舞台一样、拖滑块不发请求、换照片卡片停下。

要真照片和相机数据，所以只在本机有 captures/a12-50 样本和 g127 的相机数据夹具时跑
（GXR_STUDIO_TEST_PARENT 指到放它们的上层目录，和 test_render.py 同一套找法）；
找不到 Chrome、node 时也跳过。显卡那一项和 test_viewer_browser.py 一样：Linux 无头 Chrome 的软件显卡上不了屏时跳过，macOS 强制跑。
驱动脚本：tests/support/look_cards_browser.mjs。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

import e0_support as t
from conftest import PORT
from gpu_support import require_chrome
from gxr_studio.app import create_app
from gxr_studio.camdata import calib, store
from gxr_studio.devtools.synthetic_card import make_card
from support import samples
from test_api import find, import_all
from test_viewer_browser import FLAGS, cannot_present

WEB = Path(__file__).resolve().parents[1] / "web"
DRIVER = Path(__file__).parent / "support" / "look_cards_browser.mjs"
DEADLINE_S = 290
# 卡片和舞台的比对容差（0–255，三个通道逐像素的绝对差）：卡片是引擎在同一张源图上用同一条路径画的，
# 只在「缩小」这一步和舞台不同（舞台的画布更大，这里把显示遍读回的像素缩到卡片大小再比）。视口固定 1440×900、2× 像素密度。
TOLERANCE = {"meanAbs": 3.5, "p99": 32}

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过真浏览器测试")


@pytest.fixture
def live(settings, volumes):
    folder = samples.a12_50_samples()
    if not folder.is_dir():
        samples.missing("captures/a12-50 样本不在本机")
    make_card(volumes, samples=folder)  # 含 4 对真实样本的假卡（只读复制）
    raw = t.fixture_dat(127)
    src = calib.dev_sources(t.REPO)
    if raw is None or not src:
        pytest.skip("相机数据夹具（g127 DAT 和标定文件）不在本机")
    settings.home_dir.mkdir(parents=True, exist_ok=True)
    store.import_from_dat(raw, settings.home_dir)
    calib.import_calibration(settings.home_dir, src)
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        import_all(c)
        first = find(c, "R8051145")["id"]
        second = find(c, "R8051146")["id"]
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(settings.with_(port=port, web_dir=WEB)), log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "服务器没有起来"
    yield {"port": port, "token": settings.token, "photo": first, "next": second}
    server.should_exit = True
    thread.join(timeout=20)
    sock.close()


def drive(live, tmp_path, mode: str) -> dict:
    chrome = require_chrome()
    flags = list(FLAGS)
    if sys.platform.startswith("linux") and os.environ.get("CI"):
        flags += ["--no-sandbox", "--disable-dev-shm-usage"]
    env = {**os.environ, "CHROME_FLAGS": json.dumps(flags), "LOOKS_DEADLINE_MS": str((DEADLINE_S - 10) * 1000)}
    args = [chrome, str(live["port"]), live["token"], live["photo"], live["next"], mode, str(tmp_path / f"chrome-{mode}")]
    try:
        res = subprocess.run(["node", str(DRIVER), *args], capture_output=True, text=True, timeout=DEADLINE_S + 40, encoding="utf-8", env=env)  # noqa: S603, S607
    except subprocess.TimeoutExpired as exc:
        err = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        pytest.fail(f"驱动脚本没有在时限内结束\n进度：\n{err[-3000:]}")
    assert res.stdout.strip(), res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert "error" not in out, f"{json.dumps(out, ensure_ascii=False, indent=1)}\n进度：\n{res.stderr[-3000:]}"
    return out  # type: ignore[no-any-return]


def check(out, *, gpu: bool):
    progress = out["progress"]
    assert "timeout" not in progress, progress
    total = int(progress["looksTotal"])
    assert total >= 8 and int(progress["looksPainted"]) == total  # 内置 8 个至少都画出来了
    # 卡片是真像素：每张都有画布、不是空白或纯黑纯白、各张之间不是同一个颜色
    cards = out["cards"]
    assert cards["canvases"] == total and not cards["blank"], cards
    assert cards["distinct"] >= 4, cards
    assert len(cards["sizes"]) == 1, cards  # 同一张照片，卡片一样大
    # 显卡画（不请服务器出每张卡片的图）；服务器路径一张一张请求
    if gpu:
        assert int(progress["looksGpu"]) == total and int(progress["looksServer"]) == 0, progress
        assert out["cardRequests"]["serverCards"] == 0, out["cardRequests"]
    else:
        assert int(progress["looksServer"]) == total and out["cardRequests"]["serverCards"] == total, (progress, out["cardRequests"])
    # 点一张卡片：当前配方换成它，面板上它被标成当前
    assert out["clicked"]["markedMs"] is not None and out["clicked"]["put"] is not None, out["clicked"]
    # 卡片和舞台一样（缩到同一个大小比）
    stage = out["stage"]
    assert stage["source"] in ("gpu", "img"), stage
    assert stage["meanAbs"] <= TOLERANCE["meanAbs"] and stage["p99"] <= TOLERANCE["p99"], stage
    # 配方标签里拖档位滑块：手按着的时候卡片一张也不画；舞台要这个档位的配方表（和以前一样），但不替别的卡片发请求
    drag = out["drag"]
    assert "error" not in drag, drag
    assert drag["before"]["looksPainted"] == drag["during"]["looksPainted"], drag
    clicked = out["clicked"]["put"]
    foreign = [r for r in out["dragRequests"] if "max_edge=" in r or ("/bake?" in r and f"slot%22%3A{clicked['slot']}%2C" not in r)]
    assert foreign == [], foreign
    # 调整标签里拖曝光：显卡路径上整个拖动期间一个请求都没有，和卡片在不在无关；卡片也不动
    adjust = out["dragAdjust"]
    assert "error" not in adjust, adjust
    assert adjust["before"]["looksPainted"] == adjust["during"]["looksPainted"], adjust
    if gpu:
        polls = ("/api/camdata/status", "/api/cards")  # 页面自己的定时刷新（15 秒、3 秒一次），和拖动无关
        sent = [r for r in out["dragAdjustRequests"] if not r.endswith(polls)]
        assert sent == [], sent
    # 换照片：旧照片的卡片停下（下一张的卡片出到一半时离开，之后不再为它发请求；在路上的那一张最多发完）
    after = out["afterSwitch"]
    assert after["later"] == 0 and after["rightAway"] <= 2, after


def test_look_cards_with_the_server_preview(live, tmp_path):
    out = drive(live, tmp_path, "server")
    assert out["gpu"] is False
    check(out, gpu=False)
    assert out["switched"]["caught"] is True, out["switched"]  # 服务器一张一张画，一定能赶上出到一半


def test_look_cards_with_the_webgpu_preview(live, tmp_path):
    out = drive(live, tmp_path, "gpu")
    if not out["gpu"]:
        detail = json.dumps({k: out.get(k) for k in ("gpuWaitMs", "gpuDiag", "gpuSharedImageError", "gpuChromeLog")}, ensure_ascii=False)
        diag = {"gpuDiag": out.get("gpuDiag"), "gpuSharedImageError": out.get("gpuSharedImageError")}
        if sys.platform.startswith("linux") and cannot_present(diag):
            pytest.skip(f"Linux 无头 Chrome 的软件显卡不能把 WebGPU 画布显示出来：{detail}")
        if os.environ.get("GXR_REQUIRE_GPU_TEST") == "1":
            pytest.fail(f"GXR_REQUIRE_GPU_TEST=1，但等了 120 秒大图还是没有走显卡预览：{detail}")
        pytest.skip(f"这台机器的 Chrome 没有走显卡预览：{detail}")
    check(out, gpu=True)
    print("look-cards timings", json.dumps({k: out["progress"].get(k) for k in ("looksFirstMs", "looksAllMs")}), json.dumps(out["stage"]))
