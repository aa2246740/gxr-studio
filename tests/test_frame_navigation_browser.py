"""换照片、换边框、进出裁剪、100% 之后，大图还在不在（真 Chrome，显卡预览和服务器出图各走一遍）。

起因：给照片加了边框，点「下一张」，大图一片空白，直方图、缩略图、控件都在，刷新才好。显卡画布是整页唯一的一块，
换照片时被搬进新的 .pv；上一张边框写在它身上的 position / left / top 没有去掉，.pv 缩成 0 × 0（web/assets/stage_finish.js 的 clearDecor）。

tests/support/frame_nav_walk.mjs 在页面里走两轮（每轮六张照片各有不同的边框和日期戳：没有、白边、徕卡、哈苏、胶片、拍立得、只有日期戳），
路线里有 没有边框 → 有、有 → 有、有 → 没有、没有 → 没有、原路退回、100% 时换照片、进出裁剪页。每一步之后量：
舞台和 .pv 有尺寸、放得进窗口，照片真的画出来了（取像素，不只看有没有元素），.pv 中心点点得到的是 .pv 里的东西，没有上一张版面留下的行内样式。
等的是「画面成了想要的样子」，不是固定的睡眠；没有一项按时间判。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from support.speed import assert_fast
from test_viewer_browser import drive, live  # noqa: F401  （live 是 pytest 的 fixture）

BG = {
    "gallery": "rgb(255, 255, 255)",
    "leica": "rgb(255, 255, 255)",
    "hasselblad": "rgb(255, 255, 255)",
    "film": "rgb(243, 241, 236)",
    "instant": "rgb(232, 228, 220)",
}
UNSET = {"position": "", "left": "", "top": ""}


def cannot_present_nav(out: dict) -> bool:
    """Chrome 能建 WebGPU 设备（页面外现建一台引擎建得起来），但日志里有画布上屏要的共享图像建不起来 / 取不到的错：
    Linux 无头 Chrome 的软件显卡就是这样。页面自己的引擎这时可能已经没了，也可能还活着、只是画面从没真正出来，所以不再要求它没了
    （test_viewer_browser.py 的 cannot_present 要求）。条件仍然窄：没有那条日志的失败（比如 macOS 上的真回归）不满足它。"""
    diag = out.get("gpuDiag") or {}
    return diag.get("engine") == "created" and out.get("gpuSharedImageError") is True


@pytest.mark.parametrize(
    ("diag", "logged", "want"),
    [
        ({"engine": "created", "pageEngine": None}, True, True),  # 引擎没了 + 日志里有共享图像的错
        ({"engine": "created", "pageEngine": {"lost": False}}, True, True),  # 引擎还活着、画面上不了屏（CI 上的情形）
        ({"engine": "created", "pageEngine": {"lost": False}}, False, False),  # macOS 上的真回归：日志里什么都没有，不能被跳过藏起来
        ({"engine": "null", "pageEngine": None}, True, False),  # 连设备都建不起来：不是「建得了设备、上不了屏」
        ({"navigatorGpu": False, "engine": None}, False, False),  # 页面里没有 WebGPU
        ({}, True, False),
    ],
)
def test_the_linux_skip_condition_stays_narrow(diag, logged, want):
    assert cannot_present_nav({"gpuDiag": diag, "gpuSharedImageError": logged}) is want


def check_step(step: dict, *, gpu: bool) -> None:
    label = step["label"]
    why = f"{label}\n{json.dumps(step, ensure_ascii=False, indent=1)}"
    expect = step["expect"]
    assert not step.get("timedOut"), f"一直等不到想要的样子（等了 {step.get('waitedMs')} 毫秒），后面的步没有走：{why}"
    assert step.get("pv") and step.get("media"), f"大图没了：{why}"
    assert step["hash"].endswith(expect["id"]), f"没有走到想去的那张照片：{why}"
    assert step["pvs"] == 1, why
    area, pv, media = step["area"], step["pv"], step["media"]
    assert area["w"] > 100 and area["h"] > 100, why
    # 舞台和 .pv 有尺寸（原来是 0 × 0），放得进窗口（100% 时画面本来就比窗口大，靠滚动）
    assert pv["w"] >= 60 and pv["h"] >= 60 and media["w"] >= 40 and media["h"] >= 40, why
    if not step["zoom"]:
        assert pv["x"] >= area["x"] - 2 and pv["y"] >= area["y"] - 2, why
        assert pv["x"] + pv["w"] <= area["x"] + area["w"] + 2 and pv["y"] + pv["h"] <= area["y"] + area["h"] + 2, why
    # 照片在 .pv 里面
    assert media["x"] >= pv["x"] - 2 and media["y"] >= pv["y"] - 2, why
    assert media["x"] + media["w"] <= pv["x"] + pv["w"] + 2 and media["y"] + media["h"] <= pv["y"] + pv["h"] + 2, why
    # 看得见：.pv 中心点点到的是 .pv 里的东西（被剪掉的话点到的是舞台）；照片真的画出来了，不是空白或纯色
    assert step["hit"] is True, why
    assert step["pixels"] is not None, why
    assert step["pixels"]["distinct"] >= 12 and step["pixels"]["std"] >= 4, why
    assert step["gpu"] is gpu, why
    if expect["tab"] == "crop":
        # 裁剪页是整张未裁切的照片：没有边框和日期戳，也没有边框版面留下的东西
        assert step["framed"] is False and step["decos"] == 0 and step["pvStyle"] == "" and step["mediaStyle"] == UNSET, why
        return
    frame, stamp = expect.get("frame"), expect.get("stamp")
    assert step["decos"] == (1 if frame or stamp else 0), why
    if frame:
        assert step["framed"] is True and step["bg"] == BG[frame], why
        # 照片在边框里的位置就是版面给的（--fx / --fy），不是别处留下的
        assert step["mediaStyle"] == {"position": "absolute", "left": step["fx"], "top": step["fy"]}, why
    else:
        # 没有边框：.pv 和照片身上没有边框版面写的任何行内样式（上一张的不能留下）
        assert step["framed"] is False and step["bg"] == "rgba(0, 0, 0, 0)", why
        assert step["pvStyle"] == "" and step["mediaStyle"] == UNSET, why
    if frame in ("leica", "hasselblad", "film", "instant") or stamp:
        assert step["ink"] > 0, f"边框上的字或日期戳没画出来：{why}"


def print_steps(steps: list[dict]) -> None:
    """每一步一行（pytest -s 能看到）：量到了什么。"""
    for s in steps:
        pv, px = s.get("pv") or {}, s.get("pixels") or {}
        print(
            f"{s['label']:22} pv {pv.get('w', 0):6.0f}x{pv.get('h', 0):<6.0f} gpu={s.get('gpu')!s:5} framed={s.get('framed')!s:5} "
            f"bg={s.get('bg')} colours={px.get('distinct')} std={px.get('std', 0):.0f} ink={s.get('ink')} style={s.get('mediaStyle')}"
        )


def check_walk(out: dict, *, gpu: bool) -> None:
    steps = out["nav"]
    print_steps(steps)
    labels = [s["label"] for s in steps]
    assert len(steps) >= 30, labels
    assert any("at 100%" in label for label in labels) and any("crop tab" in label for label in labels), labels
    assert any("back from crop" in label for label in labels), labels
    for step in steps:
        check_step(step, gpu=gpu)
    bad = [m for m in out["console"] if "Uncaught" in m]
    assert not bad, out["console"]


def test_the_stage_survives_navigation_with_the_server_preview(live, tmp_path):  # noqa: F811
    out = drive(live, tmp_path, "nav-server")
    check_walk(out, gpu=False)


def test_the_stage_survives_navigation_with_the_webgpu_preview(live, tmp_path):  # noqa: F811
    out = drive(live, tmp_path, "nav-gpu")
    nav = out["nav"]
    # 显卡没接手（探针一步 gpu: false），或者走到一半没接住（某一步等不到、gpu: false）：驱动脚本带着判断用到的东西和诊断回来，
    # 平台怎么处理和 test_viewer_browser.py 的显卡测试一样：真的回归不能被跳过藏起来。显卡还在、只是舞台出了问题的，照常往下走，由断言报。
    culprit = nav[0] if not nav[0].get("gpu") else next((s for s in nav if s.get("timedOut") and not s.get("gpu")), None)
    if culprit is not None:
        keys = ("gpuWaitMs", "gpuDiag", "gpuSharedImageError", "failedBeforeClicks", "consoleBeforeClicks", "gpuChromeLog")
        detail = json.dumps({"step": culprit, **{k: out.get(k) for k in keys}}, ensure_ascii=False)
        if sys.platform.startswith("linux") and cannot_present_nav(out):
            # GitHub 的 Linux 机器没有显卡：软件显卡能建 WebGPU 设备，画布也会短暂出现，但 Chrome 给画布上屏要的共享图像建不起来、取不到，
            # 页面退回服务器出图。这一项由 macOS 的 tests 任务强制跑（GXR_REQUIRE_GPU_TEST=1）。
            pytest.skip(f"Linux 无头 Chrome 的软件显卡不能把 WebGPU 画布显示出来：{detail}")
        if os.environ.get("GXR_REQUIRE_GPU_TEST") == "1":
            pytest.fail(f"GXR_REQUIRE_GPU_TEST=1，但大图没有走显卡预览：{detail}")
        pytest.skip(f"这台机器的 Chrome 没有走显卡预览：{detail}")
    check_walk(out, gpu=True)


@pytest.mark.exclusive  # 没有显卡适配器时要在 30 秒内回来，而不是干等到超时
def test_the_gpu_walk_reports_a_missing_gpu_instead_of_timing_out(live, tmp_path):  # noqa: F811
    """显卡接不了手的机器（Linux 无头 Chrome 的软件显卡能建设备却不能上屏，页面退回服务器出图）上，
    nav-gpu 模式原来在每一步等 20 秒，最后整段 evaluate 超时，轮不到测试去跳过。现在先等一次、马上交出「没走显卡」和诊断。
    这里让页面没有 navigator.gpu，在任何机器上走一遍这条路：很快回来，诊断在。"""
    out = drive(live, tmp_path, "nav-gpu", {"NAV_NO_WEBGPU": "1"})
    assert len(out["nav"]) == 1 and out["nav"][0]["gpu"] is False, out["nav"]
    assert out["nav"][0]["verdict"] == "no-adapter", out["nav"]
    assert out["gpuDiag"]["navigatorGpu"] is False and out["gpuSharedImageError"] is False, out["gpuDiag"]
    assert not cannot_present_nav(out)  # 页面没有 WebGPU 不等于「建得了设备、上不了屏」：Linux 跳过的条件不会被它满足
    assert_fast(out["nav"][0]["waitMs"], 30000, "没有显卡适配器时交出结论（毫秒）")
