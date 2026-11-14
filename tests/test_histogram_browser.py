"""真浏览器里的直方图：在面板顶部、所有标签下都在；曝光加大后右移（显卡预览和服务器出图两条路都测）；
拖滑块时跟得上；J 和三角标出溢出；收起会记住；裁剪页只数框里面；快捷键表里有 J。

起一个真服务器（合成的假卡，只听 127.0.0.1）和无头 Chrome（和显卡一致性测试同一套找法、同一组开关），
由 tests/support/histogram_browser.mjs 用 DevTools 协议操作页面。找不到 Chrome 或 node 时跳过；设了 GXR_REQUIRE_GPU_TEST=1 时
显卡预览那一项不能跳过，只有一个例外：Linux 无头 Chrome 的软件显卡把 WebGPU 画布显示不出来（见 test_viewer_browser.cannot_present）。
最低更新速度（次/秒）用环境变量 GXR_HIST_MIN_HZ 调（默认显卡 2、服务器 1.5）：CI 用软件显卡，比较慢（这台 Mac 上软件显卡 8.6、
服务器 6.3，门槛留约 4 倍余量，CI 不重跑失败的测试），门槛只用来抓"卡住了"，真正的速度在 PR 说明里（用真显卡量）。
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

from conftest import PORT
from gpu_support import require_chrome
from gxr_studio.app import create_app
from support.speed import SpeedBudgets, speed_budgets
from test_api import find, import_all

WEB = Path(__file__).resolve().parents[1] / "web"
DRIVER = Path(__file__).parent / "support" / "histogram_browser.mjs"
# Match the software presentation backend to the software WebGPU adapter.
FLAGS = ["--enable-unsafe-webgpu", "--enable-unsafe-swiftshader", "--use-angle=swiftshader", "--use-webgpu-adapter=swiftshader"]
DEADLINE_S = 420

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过真浏览器测试")


@pytest.fixture
def live(settings, card):
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        import_all(c)
        photo_id = find(c, "R0000102")["id"]
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
    yield {"port": port, "token": settings.token, "photo": photo_id}
    server.should_exit = True
    thread.join(timeout=20)
    sock.close()


def _text(data: str | bytes | None) -> str:
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else (data or "")


def drive(live, tmp_path, mode: str) -> dict:
    chrome = require_chrome()
    flags = list(FLAGS)
    if sys.platform.startswith("linux") and os.environ.get("CI"):
        flags += ["--no-sandbox", "--disable-dev-shm-usage"]
    env = {**os.environ, "CHROME_FLAGS": json.dumps(flags), "VIEWER_DEADLINE_MS": str(DEADLINE_S * 1000)}
    try:
        res = subprocess.run(  # noqa: S603
            ["node", str(DRIVER), chrome, str(live["port"]), live["token"], live["photo"], mode, str(tmp_path / f"chrome-{mode}")],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=DEADLINE_S + 40,
            encoding="utf-8",
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"驱动脚本没有在时限内结束\n进度：\n{_text(exc.stderr)[-3000:]}")
    assert res.stdout.strip(), res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert "error" not in out, f"{json.dumps(out, ensure_ascii=False, indent=1)}\n进度：\n{res.stderr[-3000:]}"
    assert out["ready"] is True
    return out  # type: ignore[no-any-return]


def check(out: dict, mode: str) -> None:
    with speed_budgets() as speed:
        _check(out, mode, speed)


def _check(out: dict, mode: str, speed: SpeedBudgets) -> None:
    """直方图跟手的下限记进 speed，其他断言马上判。"""
    source = "gpu" if mode == "gpu" else "server"
    min_hz = float(os.environ.get("GXR_HIST_MIN_HZ", "2" if mode == "gpu" else "1.5"))
    # 位置：面板最上面、工具标签之上，六个标签下都看得见
    pl = out["placement"]
    assert pl["first"] and pl["aboveTabs"], pl
    assert set(pl["perTab"]) == {"recipe", "adjust", "crop", "frame", "stamp", "info"} and all(pl["perTab"].values()), pl
    # 第一次数出来：来源对，点数够
    assert out["first"]["source"] == source and out["first"]["samples"] > 1000, out["first"]
    # 曝光 +1.5：直方图右移
    ex = out["exposure"]
    assert ex["source"] == source and ex["firstUpdateMs"] is not None, ex
    print(f"[{mode}] 第一次动曝光到直方图第一次更新：{ex['firstUpdateMs']} 毫秒")
    assert ex["after"] - ex["before"] > 8, f"平均亮度档应该明显变大：{ex}"
    # 拖滑块：直方图跟得上
    rate = out["rate"]
    print(f"[{mode}] 拖动时直方图 {rate['perSecond']:.1f} 次/秒（{rate['updates']} 次 / {rate['seconds']:.1f} 秒）")
    print(f"[{mode}] 画面帧间隔 {rate['frameMs']}，两次更新间隔 {rate['gapMs']}")
    frame_work = rate["frameWork"]
    print(f"[{mode}] hidden frame work: {frame_work}")
    assert frame_work == {"cards": 13, "hidden": True, "redraws": 0, "blurBuilds": 0}, frame_work
    speed.at_least(rate["perSecond"], min_hz, f"拖动时直方图每秒更新次数（{rate}）")
    # 三角：曝光 +5 档高光死白，−5 档暗部死黑，各自亮起；提示里写着多少、怎么点、J
    lit = out["lit"]
    assert lit["up"]["highlight"] == "true" and lit["up"]["blown"] >= 0.005, lit
    assert "高光" in lit["up"]["title"] and "死白" in lit["up"]["title"] and "J" in lit["up"]["title"], lit["up"]["title"]
    assert lit["down"]["shadow"] == "true" and lit["down"]["crushed"] >= 0.005, lit
    assert "暗部" in lit["down"]["title"] and "死黑" in lit["down"]["title"] and "J" in lit["down"]["title"], lit["down"]["title"]
    # J：焦点在滑块上也能按；按下标出红色（高光大面积死白），画出来的点数和直方图数出的占比一致；再按关掉
    j = out["keyJ"]
    assert j["focusOnSlider"] is True and j["overlayBefore"] == {"shadow": False, "highlight": False}, j
    on, off = j["on"], j["off"]
    assert on["clip"] == {"shadow": True, "highlight": True} and on["pressed"] == ["true", "true"], on
    assert on["legend"] and "死" in on["legend"], on
    painted = on["paint"]
    assert painted["total"] > 0 and painted["red"] > 0, on
    share = on["blown"] / on["samples"]
    assert abs(painted["red"] / painted["total"] - share) < 0.03, (painted, share)
    assert off["clip"] == {"shadow": False, "highlight": False} and off["pressed"] == ["false", "false"] and off["legend"] is False, off
    assert off["paint"]["red"] == 0 and off["paint"]["blue"] == 0, off
    # 点三角：只标那一边
    click = out["click"]
    assert click["highlightOn"]["clip"] == {"shadow": False, "highlight": True} and click["highlightOn"]["paint"]["red"] > 0, click
    assert click["highlightOn"]["paint"]["blue"] == 0, click
    assert click["highlightOff"]["clip"] == {"shadow": False, "highlight": False} and click["highlightOff"]["paint"]["red"] == 0, click
    assert click["shadowOn"]["clip"] == {"shadow": True, "highlight": False}, click
    assert click["shadowOff"] == {"shadow": False, "highlight": False}, click
    # 收起：只剩一行标题和两个三角，刷新后还是收起的；展开后记住展开
    c = out["collapse"]
    assert c["collapsed"] == "true" and c["expanded"] == "false" and c["stored"] == "1", c
    assert c["plotVisible"] is False and c["trianglesVisible"] is True and 28 <= c["headHeight"] <= 40, c
    assert c["afterReload"] == {"collapsed": "true", "stored": "1"}, c
    assert c["afterExpand"] == {"collapsed": "false", "stored": "0", "plotVisible": True}, c
    # 裁剪页：只数框里面（1:1 的框比整张小），离开裁剪页回到整幅
    crop = out["crop"]
    assert crop["fullRegion"] is None and crop["backRegion"] is None, crop
    assert crop["region"] is not None and 0.2 < crop["area"] < 0.9, crop
    # 快捷键表里有 J
    assert out["help"] and out["help"][0][0] == "J" and "死" in out["help"][0][1], out["help"]
    # 边框和时间戳不算进直方图（两条路径都测）：照片压暗后套白色粗边框，再叠一个大的白色文字戳。边框是浏览器画的底色，
    # 屏幕上整张成片有大片纯白，文字戳在照片上也有一片白；直方图里不该有死白，高光三角不该亮；
    # 溢出提示（服务器路径）只盖照片那一块，不盖边框，图例照常说明颜色
    fr = out.get("frame")
    assert fr, "没有拿到带边框那一步的结果"
    print(f"[{mode}] 带白边框：成片上白色 {fr['white']:.1%}，直方图里死白 {fr['blownShare']:.2%}、死黑 {fr['crushedShare']:.1%}")
    assert fr["source"] == mode and fr["white"] > 0.05, fr
    # 文字戳真的画出来了，而且白得够多：要是数进去，死白会超过 0.5%，高光三角会亮
    assert fr["stampWhite"] is not None and fr["stampWhite"] > 0.01, f"文字戳上的白只占照片的 {fr['stampWhite']}：{fr}"
    assert fr["blownShare"] < 0.005 and fr["highlightWarn"] is False, fr
    assert fr["crushedShare"] > 0.02, f"照片本身是压暗的，死黑应该数得出来：{fr}"
    assert fr["cleanRenders"] == 0, f"不该再另外请求一张无边框的小图：{fr}"
    assert fr["legend"] and "边框" not in fr["legend"] and "死" in fr["legend"], fr
    if mode == "server":
        assert fr["photoBright"] is not None and fr["photoBright"] < 0.01, f"服务器图里不该有边框的白：{fr}"
        ov = fr["overlay"]
        assert ov, f"带边框时服务器路径也要标出位置：{fr}"
        assert all(abs(a - b) <= 1 for a, b in zip(ov["overlay"], ov["photo"], strict=True)), ov  # 和照片一样大、一样的位置
        assert ov["overlay"][2] < ov["stage"][2] - 20 and ov["overlay"][3] < ov["stage"][3] - 20, ov  # 没有盖到边框上
    else:
        assert fr["gpu"] is True and fr["overlay"] is None, fr
    # 页面里没有报错
    bad = [m for m in out["console"] if "Error" in m or "Uncaught" in m]
    assert not bad, out["console"]


@pytest.mark.exclusive  # 拖动时直方图每秒次数有下限（默认 1.5）
def test_histogram_with_the_server_preview(live, tmp_path):
    out = drive(live, tmp_path, "server")
    assert out["gpu"] is False
    check(out, "server")


@pytest.mark.exclusive  # 拖动时直方图每秒次数有下限（显卡默认 2）
def test_histogram_with_the_webgpu_preview(live, tmp_path):
    out = drive(live, tmp_path, "gpu")
    if not out["gpu"]:
        if sys.platform.startswith("linux"):
            pytest.skip("Linux 无头 Chrome 的软件显卡不能把 WebGPU 画布显示出来（这一项由 macOS 强制跑）")
        if os.environ.get("GXR_REQUIRE_GPU_TEST") == "1":
            pytest.fail("GXR_REQUIRE_GPU_TEST=1，但等了 120 秒大图还是没有走显卡预览")
        pytest.skip("这台机器的 Chrome 没有走显卡预览")
    check(out, "gpu")
