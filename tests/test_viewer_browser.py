"""真浏览器里的大图：裁切里点比例 / 横竖 / 参考线马上有反应，保存挂着也一样；加了边框和时间戳以后显卡还在管，点样式当帧就画出来。

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
import statistics
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
from test_api import find, import_all

WEB = Path(__file__).resolve().parents[1] / "web"
DRIVER = Path(__file__).parent / "support" / "viewer_browser.mjs"
# Match the software presentation backend to the software WebGPU adapter.
FLAGS = ["--enable-unsafe-webgpu", "--enable-unsafe-swiftshader", "--use-angle=swiftshader", "--use-webgpu-adapter=swiftshader"]

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过真浏览器测试")


@pytest.fixture
def live(settings, card):
    """真服务器（合成的假卡，只听 127.0.0.1）。"""
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


DEADLINE_S = 300  # 脚本自己的总时限：到点交出卡在哪一步；进程超时再多留 40 秒收尾


def _text(data: str | bytes | None) -> str:
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else (data or "")


def drive(live, tmp_path, mode: str, extra_env: dict | None = None) -> dict:
    chrome = require_chrome()
    flags = list(FLAGS)
    if sys.platform.startswith("linux") and os.environ.get("CI"):
        flags += ["--no-sandbox", "--disable-dev-shm-usage"]
    env = {**os.environ, "CHROME_FLAGS": json.dumps(flags), "VIEWER_DEADLINE_MS": str(DEADLINE_S * 1000), **(extra_env or {})}
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


def assert_painted_at_once(live_out):
    """点样式、改颜色、开日期戳、打字：点下之后的下一帧，叠加层的画法就变了（「当帧画出来」）。
    这是硬的：舞台要是又等服务器出图，一帧里不可能变，和机器忙不忙无关；/render 为 0 的断言（clickRenders、dragRenders）是另一半。
    耗时只当参考：每次操作拿它前后各三帧里最长的一帧当尺子（机器忙时帧本身就拖长，整台机器卡一下尺子也跟着变长），
    几次操作的中位数要在 max(100 毫秒, 4 倍尺子) 以内，偶尔一次卡顿不会让测试失败。"""
    clicks = live_out["clicks"]
    assert len(clicks) == 10, clicks  # 七次点样式、颜色、日期戳开关、打字
    assert all(c["changed"] for c in clicks), [c for c in clicks if not c["changed"]]
    median_ms = statistics.median(c["ms"] for c in clicks)
    median_frame = statistics.median(c["frameMs"] for c in clicks)
    assert median_ms <= max(100, 4 * median_frame), (median_ms, median_frame, clicks)


def near(a, b, eps=0.3):
    return all(abs(x - y) < eps for x, y in zip(a, b, strict=True))


def check(out):
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
    # 无边框和五种样式都出来，是浏览器自己画的（没有给每种样式请求 /render）
    assert out["cards"]["cards"] == 13 and out["cards"]["canvases"] == 13
    assert out["cards"]["cardRenders"] <= 1, out["cards"]  # 舞台上没有现成的画面时才请求一张小图
    assert out["cards"]["stageRenders"] <= 1, out["cards"]  # 离开裁切页，服务器出图时大图重出一次；走显卡时一次也没有
    assert out["cards"]["distinct"] > 8, out["cards"]  # 底图是真的照片，不是一张空白或纯黑
    # 时间戳文字边输入边变（当帧），保存等 400 ms，输入框不丢光标
    typed = out["live"]["typed"]
    assert typed["keyChanged"] is True, typed
    assert typed["putsEarly"] == 0 or typed["earlyValid"] is False, typed  # 400 毫秒之内没有保存（机器忙得拖过了就不判）
    assert typed["putsLate"] == 1 and typed["saved"] == "Paris" and typed["focusKept"] is True, typed
    # 100%、换回适合、换到下一张再换回来：边框和日期戳跟着照片；下一张没有边框，叠加层不跟过去
    live_out = out["live"]
    for name in ("zoom100", "fit", "back"):
        stage = live_out[name]
        assert stage and stage["framed"] is True and stage["overlay"] is True, (name, live_out)
        assert stage["deco"] == stage["pv"], (name, stage)  # 叠加层和整张成片一样大
        assert stage["pv"][0] >= stage["media"][0] and stage["pv"][1] > stage["media"][1], (name, stage)  # 徕卡的条在下面：成片比照片高
    assert live_out["next"] and live_out["next"]["framed"] is False and live_out["next"]["overlay"] is False, live_out["next"]
    assert live_out["quiet"] is True, "点样式之前舞台一直没有安静下来（30 秒内一直有出图在路上或刚发出）：没法判断点击有没有引起出图"
    assert_painted_at_once(live_out)
    print_timings(out)


def print_timings(out):
    """一行实测数字（pytest -s 能看到）：卡片出齐、点样式、改颜色、开日期戳、打字、拖滑块每帧，单位毫秒。"""
    live_out = out["live"]
    print(
        "timings",
        json.dumps(
            {
                "mode": out["mode"],
                "cards_ms": out["cards"]["ms"],
                "ms": {f"{i}:{c['name']}": c["ms"] for i, c in enumerate(live_out["clicks"])},
                "frame_ms": max(c["frameMs"] for c in live_out["clicks"]),
                "slider_frame_ms_worst": live_out["worstFrameMs"],
                "slider_drag_ms_30_inputs": live_out["dragMs"],
            },
            ensure_ascii=False,
        ),
    )


@pytest.mark.exclusive  # 比例框、横竖、失败提示要在 1000 毫秒内出现，点击中位数也有墙钟上限
def test_crop_and_frame_cards_with_the_server_preview(live, tmp_path):
    out = drive(live, tmp_path, "server")
    assert out["gpu"] is False
    check(out)
    # 服务器出图时也一样：点样式、改选项、开时间戳都不请求 /render，当帧就画出来
    live_out = out["live"]
    assert all(live_out["styleFramed"].values()) and len(live_out["styleFramed"]) == 5, live_out
    assert live_out["stampOn"]["overlay"] is True, live_out
    assert live_out["clickRenders"] == 0, f"点样式、改选项时发出了 /render：{live_out['renderUrls']}"


def test_rotate_after_auto_and_one_press_undo_in_a_real_browser(live, tmp_path):
    """2026-11-07 的两个问题，真服务器 + 真 Chrome：
    1) 小红书 3:4 → 缩放 → 拉直 → 自动（带透视）→ 向左旋转：没有被拒的写入、底栏不是「有修改没有保存」、面板不是骨架，
       画面上的框和服务器的一致；「自动」分析时按钮锁住、写「正在分析…」，结束有提示。
    2) 调整页曲线上点一下，保存还挂着时按一次撤销，点就没了（原来要等保存回来才记一步，要按三次）。"""
    out = drive(live, tmp_path, "server", {"VIEWER_SCENARIO": "edit-fixes"})
    fx = out["fx"]
    rot = fx["rotate"]
    assert rot["skelRightAfter"] == 0 and rot["skel"] == 0, rot
    assert "没有保存" not in rot["save"], rot
    assert near(rot["box"], rot["server"]), rot
    bad = [line for line in out["failedRequests"] if "/crop" in line or "/edit" in line]
    assert not bad, bad
    assert fx["curveBefore"] == "2 / 16" and fx["curveAfterClick"] == "3 / 16"
    assert fx["heldWrites"] >= 1, "曲线的保存应该还挂着"
    assert fx["curveAfterUndo"] == "2 / 16"
    assert fx["autoDuring"] == {"text": "正在分析…", "busy": "true", "disabled": "true"}
    assert out["autoPaused"] == 1, "分析中再点一下不该再发请求"
    assert any("已自动拉直 +4.0°、垂直 +60（可以撤销）" in t for t in fx["autoAfter"]["toasts"]), fx["autoAfter"]
    assert fx["autoAfter"]["restored"] is True


def test_frame_clicks_send_no_render_even_when_the_stage_render_is_slow(live, tmp_path):  # noqa: F811
    """点样式、改颜色、开日期戳、打字，一个 /render 也不发：数的是点下去之后「发出」的，不是「回来」的。
    离开裁剪页时舞台会发一个出图（stage=final&size=full）；服务器慢的时候（CI 上一帧要 100 多毫秒）它在点击期间才回来，
    原来数「回来」的 resource timing，就把它算到点击头上，测试偶尔得到「1 == 0」。VIEWER_DELAY_RENDER_MS 让舞台的出图慢 1 秒才回来，
    每次都能重现；现在先等舞台安静再开始数，数发出的，不受影响。点击也照样当帧画出来（图已经在了，边框当场叠上去）。"""
    out = drive(live, tmp_path, "server", {"VIEWER_DELAY_RENDER_MS": "1000"})
    live_out = out["live"]
    assert live_out["quiet"] is True, live_out
    assert live_out["clickRenders"] == 0, f"点样式、改选项时发出了 /render：{live_out['renderUrls']}"
    assert all(c["changed"] for c in live_out["clicks"]), [c for c in live_out["clicks"] if not c["changed"]]


def cannot_present(out: dict) -> bool:
    """页面的引擎没了，而且 Chrome 日志里是画布的共享图像建不起来（不是别的原因退回服务器）。"""
    diag = out.get("gpuDiag") or {}
    return diag.get("pageEngine", "missing") is None and diag.get("engine") == "created" and out.get("gpuSharedImageError") is True


@pytest.mark.exclusive  # 和服务器预览同一组墙钟断言（visibleMs < 1000，点击中位数）
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
    check(out)
    live_out = out["live"]
    # 加了边框和时间戳以后显卡还在管：还是 .gpu 画面，拖滑块的整个过程里一个 /render 也没有
    assert live_out["gpu"] is True, live_out
    assert live_out["dragRenders"] == 0 and live_out["clickRenders"] == 0, f"显卡路径上发出了 /render：{live_out['renderUrls']}"
    assert live_out["frame"]["framed"] is True and live_out["frame"]["overlay"] is True, live_out
    assert live_out["stampOn"]["overlay"] is True, live_out


# ---- 预览的版面（真 Chrome 里用字体表算）和导出的版面（Python 用同一份表算）----------------------------------------


def _layout_cases():
    """横、竖、方、条幅几种大小 × 每种样式和选项 × 每种日期戳字体。中文要系统字体，Chrome 和 Pillow 用的不一定是同一个，这里不带。"""
    import itertools
    from datetime import datetime

    from gxr_studio.finish.fonts import FontMetrics
    from gxr_studio.finish.layout import frame_layout, stamp_layout
    from gxr_studio.finish.spec import OUTPUT_RATIOS, FrameSpec, StampSpec
    from gxr_studio.finish.stamp import format_stamp
    from test_finish_layout_parity import METAS, when_json

    metrics = FontMetrics()
    sizes = [(640, 480), (480, 640), (1600, 1066), (4288, 2848), (2848, 4288), (900, 900), (4288, 1583)]
    frames, want_frames, stamps, want_stamps = [], [], [], []
    for w, h in sizes:
        for style in ("gallery", "leica", "hasselblad", "film", "instant"):
            specs = [
                FrameSpec(style=style, text=text, show_date=date, color=color, output_ratio=ratio)
                for text, date, color, ratio in itertools.product(
                    ("", "Hello 42"), (False, True), ("white", "black"), (*OUTPUT_RATIOS, "original")
                )
            ]
            for spec in specs:
                for meta in METAS[:2] if w < 2000 else METAS[:1]:
                    frames.append({"w": w, "h": h, "spec": spec.to_json(), "meta": meta})
                    want_frames.append(frame_layout(w, h, spec, meta, metrics).to_json())
        moment = datetime(2026, 10, 23, 17, 41)
        for font, size, corner in itertools.product(("seg7", "typewriter", "sans"), ("small", "medium", "large"), ("tl", "br")):
            for content, text, fmt in (
                ("date", "", "'yy mm dd"),
                ("date", "", "yyyy.mm.dd"),
                ("date+text", "Paris", "mm dd 'yy"),
                ("text", "W" * 24, "yy mm dd"),
            ):
                spec = StampSpec(
                    enabled=True, content=content, text=text, date_format=fmt, font=font, size=size, corner=corner, show_time=bool(text)
                )
                stamps.append({"w": w, "h": h, "spec": spec.to_json(), "when": when_json(moment)})
                got = stamp_layout(w, h, spec, format_stamp(spec, moment, None), metrics)
                want_stamps.append({"text": format_stamp(spec, moment, None), "layout": got.to_json() if got else None})
    return frames, stamps, want_frames, want_stamps


def test_browser_layout_matches_the_export_layout(live, tmp_path):
    """真 Chrome 里用服务器给的字体表算出的版面，和导出（Python，同一份表）算出的版面逐项相同：
    矩形、线、标志、字号、每个字的列、日期戳的外框，都是同样的数（不是「差不多」）；字符串逐字相同。"""
    from test_finish_layout_parity import same

    frames, stamps, want_frames, want_stamps = _layout_cases()
    cases = tmp_path / "layout-cases.json"
    cases.write_text(json.dumps({"frames": frames, "stamps": stamps}, ensure_ascii=False), encoding="utf-8")
    out = drive(live, tmp_path, "layout", {"LAYOUT_CASES": str(cases)})["layout"]
    assert len(out["frames"]) == len(want_frames) > 1000 and len(out["stamps"]) == len(want_stamps) > 400
    for i, (case, want) in enumerate(zip(frames, want_frames, strict=True)):
        same(want, out["frames"][i], f"frame {case['w']}x{case['h']} {case['spec']['style']} #{i}")
    for i, (case, want) in enumerate(zip(stamps, want_stamps, strict=True)):
        same(want, out["stamps"][i], f"stamp {case['w']}x{case['h']} #{i}")
