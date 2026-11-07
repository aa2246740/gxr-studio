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
FLAGS = ["--enable-unsafe-webgpu", "--enable-unsafe-swiftshader", "--use-webgpu-adapter=swiftshader"]

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
    # 边框卡片：7 张都出来，是浏览器自己画的（没有给每种样式请求 /render）
    assert out["cards"]["cards"] == 7 and out["cards"]["canvases"] == 7
    assert out["cards"]["cardRenders"] <= 1, out["cards"]  # 舞台上没有现成的画面时才请求一张小图
    assert out["cards"]["stageRenders"] <= 1, out["cards"]  # 离开裁切页，服务器出图时大图重出一次；走显卡时一次也没有
    assert out["cards"]["ms"] < 2000, out["cards"]
    assert out["cards"]["distinct"] > 8, out["cards"]  # 底图是真的照片，不是一张空白或纯黑
    # 时间戳文字边输入边变（当帧），保存等 400 ms，输入框不丢光标
    typed = out["live"]["typed"]
    assert typed["keyChanged"] is True and typed["typeMs"] < 100, typed
    assert typed["putsEarly"] == 0 and typed["putsLate"] == 1 and typed["saved"] == "Paris" and typed["focusKept"] is True, typed
    # 100%、换回适合、换到下一张再换回来：边框和日期戳跟着照片；下一张没有边框，叠加层不跟过去
    live_out = out["live"]
    for name in ("zoom100", "fit", "back"):
        stage = live_out[name]
        assert stage and stage["framed"] is True and stage["overlay"] is True, (name, live_out)
        assert stage["deco"] == stage["pv"], (name, stage)  # 叠加层和整张成片一样大
        assert stage["pv"][0] >= stage["media"][0] and stage["pv"][1] > stage["media"][1], (name, stage)  # 徕卡的条在下面：成片比照片高
    assert live_out["next"] and live_out["next"]["framed"] is False and live_out["next"]["overlay"] is False, live_out["next"]
    print_timings(out)


def print_timings(out):
    """一行实测数字（pytest -s 能看到）：卡片出齐、点样式、改颜色、开日期戳、打字、拖滑块每帧，单位毫秒。"""
    live_out = out["live"]
    styles = {k: v for k, v in live_out["styleMs"].items() if ":" not in k}
    print(
        "timings",
        json.dumps(
            {
                "mode": out["mode"],
                "cards_ms": out["cards"]["ms"],
                "style_click_ms_max": max(styles.values()),
                "style_click_ms": styles,
                "color_ms": live_out["colorMs"],
                "stamp_on_ms": live_out["stampMs"],
                "type_ms": live_out["typed"]["typeMs"],
                "slider_frame_ms_worst": live_out["worstFrameMs"],
                "slider_drag_ms_30_inputs": live_out["dragMs"],
            },
            ensure_ascii=False,
        ),
    )


def test_crop_and_frame_cards_with_the_server_preview(live, tmp_path):
    out = drive(live, tmp_path, "server")
    assert out["gpu"] is False
    check(out)
    # 服务器出图时也一样：点样式、改选项、开时间戳都不请求 /render，当帧就画出来
    live_out = out["live"]
    styles = {k: v for k, v in live_out["styleMs"].items() if ":" not in k}
    assert all(live_out["styleMs"][f"{name}:framed"] for name in ("徕卡水印", "哈苏水印", "胶片", "拍立得", "画布", "白边")), live_out
    assert max(styles.values()) < 100, live_out
    assert live_out["stampMs"] < 100 and live_out["stampOn"]["overlay"] is True, live_out
    assert live_out["clickRenders"] == 0, live_out


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
    check(out)
    live_out = out["live"]
    # 加了边框和时间戳以后显卡还在管：还是 .gpu 画面，拖滑块的整个过程里一个 /render 也没有
    assert live_out["gpu"] is True, live_out
    assert live_out["dragRenders"] == 0 and live_out["clickRenders"] == 0, live_out
    assert live_out["frame"]["framed"] is True and live_out["frame"]["overlay"] is True, live_out
    assert live_out["stampOn"]["overlay"] is True, live_out
    # 点样式、改颜色、开时间戳：当帧就画出来（目标 50 毫秒以内；CI 的机器慢，门槛放宽到一倍）
    assert max(v for k, v in live_out["styleMs"].items() if ":" not in k) < 100, live_out
    assert live_out["colorMs"] < 100 and live_out["stampMs"] < 100, live_out


# ---- 预览的版面（真 Chrome 用画布量字）和导出的版面（Pillow 量字）----------------------------------------------


def _layout_cases():
    """横、竖、方、条幅几种大小 × 每种样式和选项 × 每种日期戳字体。中文要系统字体，Pillow 和 Chrome 用的不一定是同一个，这里不带。"""
    import itertools
    from datetime import datetime

    from gxr_studio.finish.fonts import PilFonts
    from gxr_studio.finish.layout import frame_layout, stamp_layout
    from gxr_studio.finish.spec import FrameSpec, StampSpec
    from gxr_studio.finish.stamp import format_stamp
    from test_finish_layout_parity import METAS, Recorder, when_json

    rec = Recorder()
    rec.fonts = PilFonts()
    sizes = [(640, 480), (480, 640), (1600, 1066), (4288, 2848), (2848, 4288), (900, 900), (4288, 1583)]
    frames, want_frames, stamps, want_stamps = [], [], [], []
    for w, h in sizes:
        for style in ("gallery", "leica", "hasselblad", "film", "instant", "canvas"):
            specs = [
                FrameSpec(style=style, text=text, show_date=date, color=color, canvas_ratio=ratio)
                for text, date, color, ratio in itertools.product(("", "Hello 42"), (False, True), ("white", "black"), ("4:5", "9:16"))
            ]
            for spec in specs:
                for meta in METAS[:2] if w < 2000 else METAS[:1]:
                    frames.append({"w": w, "h": h, "spec": spec.to_json(), "meta": meta})
                    want_frames.append(frame_layout(w, h, spec, meta, rec).to_json())
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
                got = stamp_layout(w, h, spec, format_stamp(spec, moment, None), rec)
                want_stamps.append({"text": format_stamp(spec, moment, None), "layout": got.to_json() if got else None})
    return frames, stamps, want_frames, want_stamps, rec


def test_browser_layout_matches_the_export_layout(live, tmp_path):
    """预览用画布量字（同一批字体文件）算出的版面和导出用 Pillow 量字算出的版面：矩形 1 像素以内，字符串和字号相同。"""
    frames, stamps, want_frames, want_stamps, rec = _layout_cases()
    table = []
    want_metric = []
    for key, value in rec.table.items():
        kind, font, px, cjk, text = json.loads(key)
        if not cjk:
            table.append([kind, font, px, text])
            want_metric.append(value)
    cases = tmp_path / "layout-cases.json"
    cases.write_text(json.dumps({"table": table, "frames": frames, "stamps": stamps}, ensure_ascii=False), encoding="utf-8")
    out = drive(live, tmp_path, "layout", {"LAYOUT_CASES": str(cases)})["layout"]
    report = layout_report(out, table, want_metric, want_frames, want_stamps, stamps)
    (tmp_path / "layout-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False), json.dumps(report["where"], ensure_ascii=False))
    assert report["failures"] == [], report["failures"][:6]


def _widen(worst, key, value):
    worst[key] = max(worst.get(key, 0), value)


def _compare_metrics(worst, where, table, want_metric, got_metric):
    for (kind, font, px, text), want, got in zip(table, want_metric, got_metric, strict=True):
        if kind == "a":
            _widen(worst, "advance", abs(want - got))
            continue
        gap = max(abs(a - b) for a, b in zip(want, got, strict=True))
        if gap > worst["bbox"]:
            worst["bbox"], where["bbox"] = gap, [font, px, text, want, got]


def _compare_frame_item(worst, failures, label, want, got):
    if want["k"] != got["k"]:
        failures.append(f"{label}: kinds differ")
    elif want["k"] == "text":
        if (want["text"], want["font"], want["px"]) != (got["text"], got["font"], got["px"]):
            failures.append(f"{label}: text {want['text']!r}/{want['px']}px vs {got['text']!r}/{got['px']}px")
        _widen(worst, "text_x", abs(want["x"] - got["x"]))
        _widen(worst, "text_y", abs(want["y"] - got["y"]))
    elif want["k"] == "line":
        for key in ("x1", "y1", "x2", "y2"):
            _widen(worst, "rect", abs(want[key] - got[key]))
        _widen(worst, "text_x", abs(want["x1"] - got["x1"]))
    else:
        for key in ("x", "y", "w", "h"):
            _widen(worst, "rect", abs(want[key] - got[key]))


def _compare_frames(worst, failures, want_frames, got_frames):
    for i, (want, got) in enumerate(zip(want_frames, got_frames, strict=True)):
        label = f"frame#{i}"
        if got is None:
            failures.append(f"{label}: no layout")
            continue
        for key in ("width", "height"):
            _widen(worst, "rect", abs(want[key] - got[key]))
        for a, b in zip(want["photo"], got["photo"], strict=True):
            _widen(worst, "rect", abs(a - b))
        if len(want["items"]) != len(got["items"]):
            failures.append(f"{label}: {len(want['items'])} items vs {len(got['items'])}")
            continue
        for w_item, g_item in zip(want["items"], got["items"], strict=True):
            _compare_frame_item(worst, failures, label, w_item, g_item)


def _compare_stamps(worst, where, failures, want_stamps, got_stamps, stamps_in):
    for i, (want, got) in enumerate(zip(want_stamps, got_stamps, strict=True)):
        label = f"stamp#{i}"
        if want["text"] != got["text"]:
            failures.append(f"{label}: text {want['text']!r} vs {got['text']!r}")
            continue
        w_layout, g_layout = want["layout"], got["layout"]
        if (w_layout is None) != (g_layout is None):
            failures.append(f"{label}: layout presence")
            continue
        if w_layout is None:
            continue
        if (w_layout["px"], w_layout["font"], len(w_layout["ops"])) != (g_layout["px"], g_layout["font"], len(g_layout["ops"])):
            failures.append(f"{label}: px {w_layout['px']} vs {g_layout['px']}, ops {len(w_layout['ops'])} vs {len(g_layout['ops'])}")
        size = "full" if min(stamps_in[i]["w"], stamps_in[i]["h"]) >= 1000 else "small"
        gap = max(abs(a - b) for a, b in zip(w_layout["rect"], g_layout["rect"], strict=True))
        _widen(worst, f"stamp_rect:{w_layout['font']}:{size}", gap)
        if gap > worst["stamp_rect"]:
            worst["stamp_rect"], where["stamp_rect"] = (
                gap,
                [label, w_layout["font"], w_layout["px"], want["text"], w_layout["rect"], g_layout["rect"]],
            )


def layout_report(out, table, want_metric, want_frames, want_stamps, stamps_in):
    """逐项对：返回最大偏差的汇总和超出门槛的清单。"""
    worst = {"advance": 0.0, "bbox": 0, "rect": 0.0, "text_x": 0.0, "text_y": 0.0, "stamp_rect": 0.0}
    failures: list[str] = []
    where: dict[str, list] = {}
    _compare_metrics(worst, where, table, want_metric, out["metric"])
    _compare_frames(worst, failures, want_frames, out["frames"])
    _compare_stamps(worst, where, failures, want_stamps, out["stamps"], stamps_in)
    # 门槛：边框的矩形、线、标志、文字位置 1 像素以内；日期戳的外框，数码管字体 1 像素以内。
    # 打字机、无衬线两种字体 2 像素：Pillow 的 FreeType 对字形做网格对齐，画布用原始轮廓，量出的字形上下沿最多差 2 像素。
    for key in ("advance", "rect", "text_x", "text_y"):
        if worst[key] > 1:
            failures.append(f"{key} deviates by {worst[key]} px")
    for key, value in worst.items():
        if key.startswith("stamp_rect:") and value > (1 if ":dseg:" in key else 2):
            failures.append(f"{key} deviates by {value} px")
    return {"summary": worst, "where": where, "failures": failures}
