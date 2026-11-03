"""颜色门槛本身（gpu_gate.py）：每条规则都有一个刚好通过、一个刚好失败的例子。不需要显卡。"""

from __future__ import annotations

import numpy as np

from gpu_gate import edge_band, evaluate, valid_mask
from gxr_studio.finish.spec import Geometry


def _pair(h=200, w=300, seed=1):
    rng = np.random.default_rng(seed)
    ref = rng.integers(10, 240, (h, w, 3), dtype=np.uint8)
    return ref.copy(), ref


def test_edge_band_follows_rotation_flip_and_crop():
    plain = valid_mask(300, 200, Geometry(), None)
    assert plain.all() and not edge_band(plain).any()  # 没有拉直：没有边缘带（画面四周、裁切边都不豁免）
    tilted = valid_mask(300, 200, Geometry(straighten=5.0), None)
    band = edge_band(tilted)
    assert band.any() and not band[100, 150]  # 中间不在带里
    turned = valid_mask(300, 200, Geometry(straighten=5.0, rotate90=1, flip=True), None)
    assert turned.shape == (300, 200)
    np.testing.assert_array_equal(edge_band(turned), np.fliplr(np.rot90(band, 1)))  # 带跟着旋转、翻转
    cropped = valid_mask(300, 200, Geometry(straighten=5.0), (20, 10, 100, 80))
    np.testing.assert_array_equal(edge_band(cropped), edge_band(tilted[10:90, 20:120]))
    # 带只有边界内外各一像素：离边界两像素以上的无效区不在带里
    assert (~tilted & ~band).any()


def test_base_gate_counts_without_rounding():
    gpu, ref = _pair()
    n = gpu.shape[0] * gpu.shape[1]
    flat = gpu.reshape(-1, 3)
    flat[: n // 10_000, 0] += 2  # 恰好 0.01%：通过
    ok = evaluate(gpu, ref)
    assert ok["over_1"] == n // 10_000 and ok["pass"] is True
    flat[n // 10_000, 0] += 2  # 多一个：失败
    bad = evaluate(gpu, ref)
    assert bad["pass"] is False and "超过 1 码值" in bad["why"][0]


def test_out_of_band_pixels_over_two_need_a_frozen_explained_position():
    gpu, ref = _pair()
    gpu[50, 60, 1] += 3
    assert "未冻结" in " ".join(evaluate(gpu, ref)["why"])
    frozen = {(50, 60): 3}
    assert evaluate(gpu, ref, frozen=frozen, explain=lambda pts: [True] * len(pts))["pass"] is True
    assert evaluate(gpu, ref, frozen=frozen, explain=lambda pts: [False] * len(pts))["pass"] is False  # 复算解释不了
    assert evaluate(gpu, ref, frozen=frozen)["pass"] is False  # 没有复算
    gpu[50, 60, 1] += 1  # 差值变大
    assert evaluate(gpu, ref, frozen=frozen, explain=lambda pts: [True] * len(pts))["pass"] is False
    gpu[50, 60, 1] -= 1
    gpu[70, 80, 2] += 3  # 新位置
    assert evaluate(gpu, ref, frozen=frozen, explain=lambda pts: [True] * len(pts))["pass"] is False


def test_out_of_band_maximum_is_four_and_display_luts_have_no_exemptions():
    gpu, ref = _pair()
    gpu[50, 60, 1] += 5
    r = evaluate(gpu, ref, frozen={(50, 60): 5}, explain=lambda pts: [True] * len(pts))
    assert r["pass"] is False and "带外最大差 5" in " ".join(r["why"])
    gpu[50, 60, 1] -= 2
    r = evaluate(gpu, ref, frozen={(50, 60): 3}, explain=lambda pts: [True] * len(pts), display_lut=True)
    assert r["pass"] is False and "显示类 LUT" in " ".join(r["why"])


def test_in_band_anomalies_are_capped_at_one_per_million():
    h, w = 1000, 1001  # N = 1,001,000 → ceil(N × 10⁻⁶) = 2
    ref = np.full((h, w, 3), 100, np.uint8)
    mask = valid_mask(w, h, Geometry(straighten=3.0), None)
    band = edge_band(mask)
    ys, xs = np.nonzero(band)
    gpu = ref.copy()
    gpu[ys[:2], xs[:2]] = 130  # 带内两个大差：通过
    assert evaluate(gpu, ref, mask=mask)["pass"] is True
    gpu[ys[2], xs[2]] = 130  # 第三个：失败
    r = evaluate(gpu, ref, mask=mask)
    assert r["pass"] is False and r["over_2_in_band"] == 3
