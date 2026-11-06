"""真浏览器里的大图：裁切里点比例 / 横竖 / 参考线马上有反应，保存挂着也一样；边框卡片只做一次底图。

起一个真服务器（合成的假卡，只听 127.0.0.1）和无头 Chrome（和显卡一致性测试同一套找法、同一组开关），
由 tests/support/viewer_browser.mjs 用 DevTools 协议点按。保存请求（PUT .../crop）被挂住，模拟后端忙：
界面必须在保存返回之前就变。找不到 Chrome 或 node 时跳过；设了 GXR_REQUIRE_GPU_TEST=1 时显卡预览那一项不能跳过，
只有一个例外：Linux 无头 Chrome 的软件显卡把 WebGPU 画布显示不出来（见 cannot_present），那一项由 macOS 强制跑。
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
from gxr_studio.finish import serve
from test_api import find, import_all

WEB = Path(__file__).resolve().parents[1] / "web"
DRIVER = Path(__file__).parent / "support" / "viewer_browser.mjs"
FLAGS = ["--enable-unsafe-webgpu", "--enable-unsafe-swiftshader", "--use-webgpu-adapter=swiftshader"]

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过真浏览器测试")


@pytest.fixture
def live(settings, card, monkeypatch):
    """真服务器 + 数「选底图」的时间点（边框卡片的底图只做一次）。"""
    try:
        from gxr_studio.finish import cards

        cards.clear()
    except ImportError:  # 拆分之前的版本没有卡片缓存
        pass
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        import_all(c)
        photo_id = find(c, "R0000102")["id"]
    stamps: list[float] = []
    real = serve.color_image

    def spy(*args, **kwargs):
        stamps.append(time.time())
        return real(*args, **kwargs)

    monkeypatch.setattr(serve, "color_image", spy)
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
    yield {"port": port, "token": settings.token, "photo": photo_id, "stamps": stamps}
    server.should_exit = True
    thread.join(timeout=20)
    sock.close()


DEADLINE_S = 300  # 脚本自己的总时限：到点交出卡在哪一步；进程超时再多留 40 秒收尾


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
        log = tmp_path / f"chrome-{mode}.log"
        chrome_log = log.read_text(encoding="utf-8", errors="replace")[-3000:] if log.is_file() else ""
        pytest.fail(f"驱动脚本没有在时限内结束\n进度：\n{_text(exc.stderr)[-3000:]}\nChrome：\n{chrome_log}")
    assert res.stdout.strip(), res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert "error" not in out, f"{json.dumps(out, ensure_ascii=False, indent=1)}\n进度：\n{res.stderr[-3000:]}"
    assert out["ready"] is True
    return out  # type: ignore[no-any-return]


def near(a, b, eps=0.3):
    return all(abs(x - y) < eps for x, y in zip(a, b, strict=True))


def check(out, live, *, cards_max_builds):
    # 保存挂着（后端「忙」）时点 1:1：框马上变成居中的最大 1:1 框，面板的比例名也换了
    assert out["heldDuringRatio"] == 1
    ratio = out["ratio"]
    assert ratio["visibleMs"] is not None and ratio["visibleMs"] < 1000, ratio
    assert ratio["label"] == "1:1"
    left, top, width, height = ratio["box"]
    assert abs(top) < 0.5 and abs(height - 100) < 0.5 and abs(left * 2 + width - 100) < 0.5, ratio["box"]
    assert near(out["ratioStillLocal"], ratio["box"], 0.01)
    # 放行以后换成后端算的准框（取整到像素，差不到 0.3%）
    assert near(out["afterRelease"]["box"], out["afterRelease"]["server"]) and out["afterRelease"]["preset"] == "1:1"
    # 横竖切换：框马上换方向，和后端一致
    assert out["orient"]["visibleMs"] is not None and out["orient"]["visibleMs"] < 1000
    assert near(out["orient"]["box"], out["orient"]["server"])
    # 参考线：菜单里换一种，框里的线马上换
    assert out["guides"]["changed"] is True and out["guides"]["after"] > out["guides"]["before"]
    # 保存失败：框已经变了、留着；提示里有「重试」；重试成功后和后端一致
    assert out["failed"]["visibleMs"] is not None and out["failed"]["visibleMs"] < 1000
    assert out["afterFail"]["hasRetry"] is True
    assert near(out["afterFail"]["boxBefore"], out["failed"]["box"], 0.01)
    assert near(out["afterFail"]["boxAfter"], out["afterFail"]["server"]) and out["afterFail"]["preset"] == "16:9"
    # 撤销：框回到上一步，后端也回去了
    assert out["undo"]["changed"] is True and near(out["undo"]["box"], out["undo"]["server"])
    assert out["undo"]["preset"] != "16:9"
    # 边框卡片：7 张都出来；窗口里「选底图」只跑了不超过 cards_max_builds 次（原来每张一次）
    assert out["cards"]["cards"] == 7 and out["cards"]["imgs"] == 7
    builds = [t for t in live["stamps"] if out["cards"]["from"] <= t <= out["cards"]["to"] + 1]
    assert 1 <= len(builds) <= cards_max_builds, len(builds)


def test_crop_and_frame_cards_with_the_server_preview(live, tmp_path):
    out = drive(live, tmp_path, "server")
    assert out["gpu"] is False
    check(out, live, cards_max_builds=2)  # 离开裁切页会有一次大图的服务器出图


def cannot_present(out: dict) -> bool:
    """页面的引擎没了，而且 Chrome 日志里是画布的共享图像建不起来（不是别的原因退回服务器）。"""
    diag = out.get("gpuDiag") or {}
    return diag.get("pageEngine", "missing") is None and diag.get("engine") == "created" and out.get("gpuSharedImageError") is True


def test_crop_and_frame_cards_with_the_webgpu_preview(live, tmp_path):
    out = drive(live, tmp_path, "gpu")
    if not out["gpu"]:
        keys = ("gpuWaitMs", "gpuDiag", "gpuSharedImageError", "failedBeforeClicks", "consoleBeforeClicks", "gpuChromeLog")
        detail = json.dumps({k: out.get(k) for k in keys}, ensure_ascii=False)
        if sys.platform.startswith("linux") and cannot_present(out):
            # GitHub 的 Linux 机器没有显卡：SwiftShader 能建 WebGPU 设备（显卡一致性测试只读回像素，能过），
            # 但 Chrome 给画布上屏要的共享图像建不起来，页面的引擎随即失效、退回服务器出图（产品该有的退路）。
            # 2026-11-06 在 Chrome 156 上试过 6 组开关（ANGLE/Vulkan SwiftShader、软件合成等）都一样。
            # 目前只做 Mac 版：这一项由 macOS 的 tests 任务强制跑（GXR_REQUIRE_GPU_TEST=1，不能跳过）。
            pytest.skip(f"Linux 无头 Chrome 的软件显卡不能把 WebGPU 画布显示出来：{detail}")
        if os.environ.get("GXR_REQUIRE_GPU_TEST") == "1":
            pytest.fail(f"GXR_REQUIRE_GPU_TEST=1，但等了 120 秒大图还是没有走显卡预览：{detail}")
        pytest.skip(f"这台机器的 Chrome 没有走显卡预览：{detail}")
    check(out, live, cards_max_builds=1)
