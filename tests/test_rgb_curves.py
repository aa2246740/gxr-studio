"""四通道曲线的参数、顺序、逐通道映射和旧主曲线的字节兼容性。实图只从本机样本读取。"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.interpolate import PchipInterpolator

from gxr_studio.finish.compose import encode_jpeg, load_jpeg
from gxr_studio.finish.pixels import apply_base
from gxr_studio.render.params import CURVE_MAX_POINTS, Adjust, Curves, EditError, parse_adjust, parse_curve, parse_edit
from gxr_studio.render.tones import TABLE_SIZE, apply_table, channel_gains, display_table, tone_function
from support import samples

POINTS = [[0, 10], [128, 110], [255, 245]]
IDENTITY = [[0, 0], [128, 128], [255, 255]]
CHANNELS = ("master", "r", "g", "b")


@pytest.mark.parametrize("channel", CHANNELS)
def test_each_channel_validates_and_round_trips(channel):
    curve = parse_curve({channel: POINTS})
    assert curve is not None
    assert curve.to_json() == {channel: POINTS}
    assert parse_edit({"curve": {channel: POINTS}}).to_json()["curve"] == {channel: POINTS}
    assert parse_curve({channel: IDENTITY}) is None


def test_mixed_null_missing_and_identity_channels_are_absent():
    assert parse_curve({"master": None, "r": None, "g": IDENTITY, "b": None}) is None
    curve = parse_curve({"master": POINTS, "r": None, "g": IDENTITY, "b": POINTS})
    assert curve == Curves(master=tuple(map(tuple, POINTS)), b=tuple(map(tuple, POINTS)))
    assert curve.to_json() == {"master": POINTS, "b": POINTS}
    assert parse_edit({"curve": {"b": POINTS}}).digest() != parse_edit({"curve": {"r": POINTS}}).digest()


@pytest.mark.parametrize("channel", CHANNELS)
@pytest.mark.parametrize(
    "points",
    [
        [],
        [[0, 0]],
        [[i, i + 1] for i in range(CURVE_MAX_POINTS + 1)],
        [[0, 0], [0, 5]],
        [[200, 5], [100, 5]],
        [[0, 0], [256, 5]],
        [[0, 0], [255, -1]],
        [[0, 0], [True, 5]],
        [[0, 0], [255, float("nan")]],
        [[0, 0], [255, float("inf")]],
        [[0], [255, 5]],
        "bad",
        {},
    ],
)
def test_bad_channel_points_raise_a_chinese_edit_error(channel, points):
    with pytest.raises(EditError, match="曲线") as error:
        parse_curve({channel: points})
    assert error.value.code == "bad_curve"


@pytest.mark.parametrize("raw", [[], "curve", {"red": POINTS}, {"master": POINTS, "x": None}])
def test_bad_curve_shape_is_rejected(raw):
    with pytest.raises(EditError, match="主曲线、红、绿、蓝"):
        parse_curve(raw)


def test_channel_then_master_order_with_literal_expected_values():
    # 通道减半与主曲线抬黑不交换；反序会把红色的黑点也减半。
    curve = parse_curve({"master": [[0, 40], [255, 255]], "r": [[0, 0], [255, 127.5]]})
    out = apply_table(np.array([[100, 100, 100]], np.float32), display_table(Adjust(), curve))
    np.testing.assert_allclose(out, [[82.1568627, 124.3137255, 124.3137255]], atol=1e-5)


@pytest.mark.parametrize("channel", ("r", "g", "b"))
def test_one_channel_leaves_other_channels_identical(channel):
    rgb = np.array([[[0, 64, 255], [31, 128, 205]], [[220, 177, 84], [255, 250, 10]]], np.uint8)
    out = apply_base(rgb, Adjust(), parse_curve({channel: POINTS}))
    index = ("r", "g", "b").index(channel)
    assert np.any(out[..., index] != rgb[..., index])
    for other in set(range(3)) - {index}:
        np.testing.assert_array_equal(out[..., other], rgb[..., other])


def _legacy_base(rgb, adjust, master):
    """0d3b08d 的 JPG 调整路径和主曲线算法，保留为兼容性参考。"""
    if adjust.is_identity and master is None:
        return rgb
    x = rgb.astype(np.float32) / np.float32(255.0)
    lin = np.where(x <= 0.04045, x / np.float32(12.92), ((x + 0.055) / 1.055) ** 2.4).astype(np.float32)
    gains = channel_gains(adjust)
    if gains is not None:
        lin = lin * gains.astype(np.float32)
    x = np.clip(lin, 0, None)
    encoded = np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1 / 2.4) - 0.055)
    out = np.clip(encoded * 255 + 0.5, 0, 255).astype(np.uint8)
    if adjust.tone_is_identity and master is None:
        return out
    y = tone_function(np.linspace(0, 255, TABLE_SIZE) / 255, adjust) * 255
    if master is not None:
        xs, ys = np.array(master, np.float64).T
        y = np.clip(PchipInterpolator(xs, ys, extrapolate=False)(np.clip(y, xs[0], xs[-1])), 0, 255)
    table = y.astype(np.float32)
    pos = out.astype(np.float32) * np.float32(4)
    i = np.minimum(pos.astype(np.int32), TABLE_SIZE - 2)
    f = pos - i
    return np.clip(table[i] + (table[i + 1] - table[i]) * f + 0.5, 0, 255).astype(np.uint8)


@pytest.fixture
def real_rgb():
    folder = samples.a12_50_samples()
    if not folder.is_dir():
        samples.missing("真实相机样本只保存在本机")
    source = next(folder.glob("*.JPG"))
    return load_jpeg(source, 640)


def test_red_only_on_a_real_photo(real_rgb):
    out = apply_base(real_rgb, Adjust(), parse_curve({"r": POINTS}))
    assert np.any(out[..., 0] != real_rgb[..., 0])
    np.testing.assert_array_equal(out[..., 1:], real_rgb[..., 1:])


@pytest.mark.parametrize("master", [None, POINTS])
@pytest.mark.parametrize("raw_adjust", [{}, {"contrast": 30, "shadow": 40}, {"exposure_ev": 0.7, "wb": {"warm": 8, "tint": -5}}])
def test_old_main_curve_and_no_curve_export_byte_identically(real_rgb, master, raw_adjust):
    adjust = parse_adjust(raw_adjust)
    before = _legacy_base(real_rgb, adjust, master)
    after = apply_base(real_rgb, adjust, parse_curve({"master": master}))
    np.testing.assert_array_equal(after, before)
    assert encode_jpeg(after, 95) == encode_jpeg(before, 95)
    # 显式恒等通道也不能改变已有主曲线的结果。
    identity_channels = parse_curve({"master": master, "r": IDENTITY, "g": IDENTITY, "b": None})
    np.testing.assert_array_equal(apply_base(real_rgb, adjust, identity_channels), before)
