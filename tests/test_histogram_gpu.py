"""显卡上的直方图和溢出提示，和 numpy 参考实现逐个核对（无头 Chrome + SwiftShader，和显卡一致性测试同一套）。

输入是无损 PNG（当"相机 JPG"传上显卡，颜色层原样通过），所以成片纹理的内容就是 PNG 的像素，参考值直接从 PNG 算。
页面还把成片纹理读回来发给测试：显卡数出的直方图必须等于"对读回的像素按规则数"，不依赖颜色链是否正确。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import histogram_support as ref
from gpu_support import run_engine

HARNESS = Path(__file__).parent / "support" / "histogram_harness.mjs"


def _big() -> np.ndarray:
    big = ref.noise(800, 1000, seed=3)
    big[:200, :250] = 0
    big[600:, 700:] = 255
    return big


IMAGES = {
    "ramp": ref.ramp(),
    "noise": ref.noise(),
    "blocks": ref.clipping_blocks(),
    "big": _big(),  # 80 万像素：取样间隔 2
    "odd": ref.noise(777, 1001, seed=11),
}
# id → (图, 归一化矩形, 最多取样点数, 溢出提示 (暗部, 高光) 与读回的显示尺寸)
CASES: dict[str, tuple[str, tuple[float, float, float, float] | None, int | None]] = {
    "ramp": ("ramp", None, None),
    "noise": ("noise", None, None),
    "blocks": ("blocks", None, None),
    "big_stride2": ("big", None, None),
    "odd": ("odd", None, None),
    "noise_region": ("noise", (0.1, 0.2, 0.5, 0.4), None),
    "big_region": ("big", (0.25, 0.25, 0.5, 0.5), None),
    "blocks_corner": ("blocks", (0.0, 0.0, 0.25, 0.3), None),
    "big_stride7": ("big", None, 20000),
    "noise_out_of_range": ("noise", (0.9, 0.9, 0.5, 0.5), None),
}
OVERLAYS = {
    "overlay_both": ("blocks", {"shadow": True, "highlight": True}),
    "overlay_shadow": ("blocks", {"shadow": True, "highlight": False}),
    "overlay_highlight": ("blocks", {"shadow": False, "highlight": True}),
    "overlay_none": ("blocks", {"shadow": False, "highlight": False}),
}


@pytest.fixture(scope="module")
def gpu(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("histgpu")
    files = {f"/{k}.png": ref.png(v) for k, v in IMAGES.items()}
    cases = []
    for cid, (image, region, max_samples) in CASES.items():
        cases.append({"id": cid, "image": f"/{image}.png", "region": list(region) if region else None, "maxSamples": max_samples})
    for cid, (image, overlay) in OVERLAYS.items():
        h, w = IMAGES[image].shape[:2]
        cases.append({"id": cid, "image": f"/{image}.png", "overlay": overlay, "present": [w, h]})
    out, summary = run_engine(tmp, files, cases, harness=HARNESS)
    return out, summary


@pytest.mark.parametrize("cid", list(CASES))
def test_gpu_histogram_matches_numpy(gpu, cid):
    out, summary = gpu
    image, region, max_samples = CASES[cid]
    src = IMAGES[image]
    # 页面读回的成片纹理就是 PNG 的像素（颜色层原样通过）
    np.testing.assert_array_equal(out[cid], src, err_msg="成片纹理不是原图")
    got = summary["hist"][cid]
    rect = ref.pixel_rect(src.shape[1], src.shape[0], region)
    want = ref.reference(src, rect, None, max_samples or ref.MAX_SAMPLES)
    assert tuple(got["rect"]) == want["rect"], cid
    assert got["stride"] == want["stride"], cid
    assert got["samples"] == want["samples"], cid
    np.testing.assert_array_equal(np.asarray(got["counts"]), want["counts"], err_msg=cid)


def test_gpu_histogram_skips_instead_of_queueing(gpu):
    """两次同时来：一次数完，另一次直接返回 null（跳过，不排队）；数完的那次和单独数的一致。"""
    _out, summary = gpu
    for cid in CASES:
        h = summary["hist"][cid]
        assert h["skipped"] is True, cid
        assert h["again"] == h["counts"], cid


@pytest.mark.parametrize("cid", list(OVERLAYS))
def test_gpu_overlay_matches_numpy(gpu, cid):
    out, _summary = gpu
    image, overlay = OVERLAYS[cid]
    src = IMAGES[image]
    want = ref.overlay(src, shadow=overlay["shadow"], highlight=overlay["highlight"])
    got = out[f"{cid}.present"]
    assert got.shape == want.shape
    np.testing.assert_array_equal(got, want, err_msg=cid)
    # 提示确实画出来了（不是两边都没动）
    assert (got != src).any(axis=2).sum() == (want != src).any(axis=2).sum()
    if overlay["shadow"] or overlay["highlight"]:
        assert (got != src).any()
    else:
        np.testing.assert_array_equal(got, src)
