"""换照片、换边框、进出裁剪、100% 之后，大图还在不在（真 Chrome，显卡预览和服务器出图各走一遍）。

起因：给照片加了边框，点「下一张」，大图一片空白，直方图、缩略图、控件都在，刷新才好。显卡画布是整页唯一的一块，
换照片时被搬进新的 .pv；上一张边框写在它身上的 position / left / top 没有去掉，.pv 缩成 0 × 0（web/assets/stage_finish.js 的 clearDecor）。

tests/support/frame_nav_walk.mjs 在页面里走两轮（每轮六张照片各有不同的边框和日期戳：没有、白边、徕卡、哈苏、胶片、画布、拍立得、只有日期戳），
路线里有 没有边框 → 有、有 → 有、有 → 没有、没有 → 没有、原路退回、100% 时换照片、进出裁剪页。每一步之后量：
舞台和 .pv 有尺寸、放得进窗口，照片真的画出来了（取像素，不只看有没有元素），.pv 中心点点得到的是 .pv 里的东西，没有上一张版面留下的行内样式。
等的是「画面成了想要的样子」，不是固定的睡眠；没有一项按时间判。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from test_viewer_browser import drive, live  # noqa: F401  （live 是 pytest 的 fixture）

BG = {
    "gallery": "rgb(255, 255, 255)",
    "leica": "rgb(255, 255, 255)",
    "hasselblad": "rgb(255, 255, 255)",
    "film": "rgb(8, 8, 8)",
    "instant": "rgb(248, 246, 242)",
    "canvas": "rgb(255, 255, 255)",
}
UNSET = {"position": "", "left": "", "top": ""}


def check_step(step: dict, *, gpu: bool) -> None:
    label = step["label"]
    why = f"{label}\n{json.dumps(step, ensure_ascii=False, indent=1)}"
    expect = step["expect"]
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
    if not out["nav"][0].get("gpu"):
        if sys.platform.startswith("linux"):
            pytest.skip("Linux 无头 Chrome 的软件显卡不能把 WebGPU 画布显示出来（这一项由 macOS 强制跑）")
        if os.environ.get("GXR_REQUIRE_GPU_TEST") == "1":
            pytest.fail("GXR_REQUIRE_GPU_TEST=1，但大图没有走显卡预览")
        pytest.skip("这台机器的 Chrome 没有走显卡预览")
    check_walk(out, gpu=True)
