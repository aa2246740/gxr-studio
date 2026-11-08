"""被服务器说「框本身不对」的裁剪（形状、范围、太小、坏框）：重试、几何写入之后都不能被悄悄改成 fit 存进去。

和 tests/test_edit_route.py 用同一个 node 驱动（tests/support/edit_route.mjs），从那里拆出来是为了守住文件行数上限。
断言先看用户看得到的：发出去的写入、状态、服务器上的框、提示；内部标记最后看。
"""

from __future__ import annotations

import shutil

import pytest

from test_edit_route import run

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过前端状态测试")


@pytest.mark.parametrize("code", ["ratio_mismatch", "crop_outside_image", "too_small", "bad_rect"])
def test_a_box_the_server_called_wrong_stays_rejected_after_a_later_geometry_write(tmp_path, code):
    """评审第八轮 P1：框被服务器说「本身不对」才没写进去，之后几何写成功了。不能把它改成 fit：重试提供明确选择、
    不再重发，界面上的框原样留着；记录还在时的下一次拖动也不带 fit（loadCrop 不再让这个记录把 boxStale 一直开着）。"""
    out = run(tmp_path, f"shape-rejected-then-geometry:{code}", real=("crop.js", "correct.js"))
    mine = [0.2, 0.15, 0.3, 0.6]
    # 先看用户看得到的：发出去的写入、状态、服务器上的框、提示；内部标记最后看
    after = out["afterRetry"]
    assert after["puts"] == [], "被拒的框必须由用户明确选择恢复方式"
    assert out["retry"] is False and after["status"] == "failed" and after["screen"] == mine
    assert after["server"] == [0.12, 0.12, 0.27, 0.54], "服务器上还是几何写入收过的框，被拒的框没有存进去"
    assert any("裁剪框超出画面范围" in t for t in after["toasts"]), after["toasts"]
    assert out["laterDrag"]["puts"] == [["crop", [0.25, 0.2, 0.3, 0.6], False]], "之后的拖动不带 fit"
    assert out["afterGeometry"] == {"status": "failed", "screen": mine, "boxStale": False, "failedFit": False}
    assert out["rejected"] == {"status": "failed", "screen": mine, "shape": True}


@pytest.mark.parametrize("transient", ["500", "409", "offline"])
@pytest.mark.parametrize("code", ["ratio_mismatch", "crop_outside_image", "too_small", "bad_rect"])
def test_a_rejected_box_stays_rejected_after_a_retry_hits_a_transient_error(tmp_path, code, transient):
    """评审第九轮 P1：被拒的框明确选择 fit 时碰上一次 500 / 409 / 断网，之后几何又写成功了。旧代码在那次重试里把「框本身不对」的
    标记清掉，几何写成功时给这个框盖上 fit，下一次重试就把它收成合格框存了进去，还提示「修改已保存」。"""
    out = run(tmp_path, f"shape-rejected-transient-retry:{code}:{transient}", real=("crop.js", "correct.js"))
    mine = [0.2, 0.15, 0.3, 0.6]
    after = out["afterRetry"]
    assert after["puts"] == [], "第二次重试仍只提供选择，不自动重复之前的 fit"
    assert out["transientAttempt"] == [["crop", mine, True]], "第一次 fit 来自用户明确选择，并确实遇到了临时失败"
    assert out["transientCode"] == {"500": "http_500", "409": "rev_conflict", "offline": "offline"}[transient]
    assert out["retry"] is False and after["status"] == "failed" and after["screen"] == mine
    assert after["server"] == [0.12, 0.12, 0.27, 0.54], "被拒的框没有存进去"
    message = {"500": "服务器暂时不可用", "409": "编辑记录在别处改过了", "offline": "无法连接到 GXR Studio"}[transient]
    assert any(message in t and "收进画面" in t and "恢复已保存的框" in t for t in after["toasts"]), after["toasts"]
    assert not any("修改已保存" in t for t in after["toasts"]), after["toasts"]
    assert out["transientRetry"] is False and out["shape"] is True
