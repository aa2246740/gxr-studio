"""raw/：DNG 读取、白平衡码值、半分辨率、MHC 去马赛克、中性渲染。（S4）

合成 DNG 的测试随时能跑；真实样本（captures/a12-50，不入库）和研究代码对照的测试找不到文件就跳过。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

from gxr_studio.raw import (
    NEUTRAL,
    RawDecodeError,
    Renderer,
    demosaic_full,
    demosaic_strips,
    half_res_codes,
    open_dng,
    orient_array,
    render_full,
    render_half,
    wb_gains,
)
from gxr_studio.raw.render import srgb_encode
from raw_support import bayer_scene, build_dng

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "captures/a12-50/2026-10-19/stage2c-first-run/R8051144.DNG"
SAMPLE_SHA = "c8b6fd6d2e1cf0705fa187251c0817ca36e2e705efa9411d93216060e7250476"
RESEARCH_DNG = ROOT / "research/color_table_fit/dng_scene.py"


def rng_mosaic(h, w, hi=4095, seed=1):
    return np.random.default_rng(seed).integers(0, hi + 1, (h, w)).astype(np.uint16)


# ---- 结构 -------------------------------------------------------------------


@pytest.mark.parametrize("bps", [16, 12])
def test_synthetic_structure_and_pixels_roundtrip(bps):
    m = rng_mosaic(20, 32, (1 << bps) - 1)
    d = open_dng(
        build_dng(
            m,
            bps=bps,
            black=(64,),
            white=(1 << bps) - 1,
            crop_origin=(2, 1),
            crop_size=(24, 16),
            active=(0, 0, 20, 32),
            neutral=(0.645161, 1.0, 0.574712),
            orientation=1,
        )
    )
    assert d.endian == "big" and d.bps == bps and (d.width, d.height) == (32, 20)
    assert d.cfa.tolist() == [[0, 1], [1, 2]]
    assert d.crop_window() == (1, 2, 16, 24) and d.crop_size == (24, 16)
    assert d.white == (1 << bps) - 1 and d.black_pattern.tolist() == [[64.0]]
    assert d.as_shot_neutral.tolist() == pytest.approx([0.645161, 1.0, 0.574712])
    assert np.array_equal(d.mosaic(), m)
    assert d.crop_cfa_phase().tolist() == [[1, 2], [0, 1]]  # 裁切起点 (x=2, y=1) 落在 CFA 的 G 位置
    assert d.baseline_exposure == pytest.approx(0.3) and d.exif["iso"] == 200


def test_black_level_pattern_deltas_and_linearization_match_pointwise_reader():
    h, w = 16, 24
    m = rng_mosaic(h, w, 4000, seed=3)
    dh = list(range(w))
    dv = [2 * i for i in range(h)]
    d = open_dng(
        build_dng(
            m,
            black=(10, 20, 30, 40),
            black_repeat=(2, 2),
            black_delta_h=dh,
            black_delta_v=dv,
            white=4095,
            active=(0, 0, h, w),
        )
    )
    ys, xs = np.indices((h, w))
    ref = d.normalised_at(ys, xs)  # 来源里的逐点读取
    assert np.array_equal(d.normalised_block(0, h, 0, w), ref)
    assert np.array_equal(d.normalised_block(3, 9, 5, 17), ref[3:9, 5:17])
    # 黑电平图案确实生效：(0,0)=10、(0,1)=20、(1,0)=30、(1,1)=40，再加 deltas
    assert d.black_at(1, 1) == 40 + 1 + 2
    table = np.linspace(0, 4095, 4096) ** 1.0 * 0.5
    d2 = open_dng(build_dng(m, linearization=[int(v) for v in table], black=(0,), white=2047, active=(0, 0, h, w)))
    block = d2.normalised_block(0, h, 0, w)
    assert np.array_equal(block, d2.normalised_at(ys, xs))
    assert block.max() <= 4000 * 0.5 / 2047 + 1e-9


def test_undecodable_files_raise_one_error():
    ok = build_dng(rng_mosaic(8, 8), active=(0, 0, 8, 8))
    for bad in (b"", b"not a tiff at all", ok[:60], ok[:-20000] if len(ok) > 30000 else ok[:200]):
        with pytest.raises(RawDecodeError):
            d = open_dng(bad)
            d.mosaic()
    with pytest.raises(RawDecodeError, match="暂不支持"):
        open_dng(build_dng(rng_mosaic(8, 8), compression=7))
    # 条带被截断：能解析头部、读像素时才发现
    trunc = bytearray(build_dng(rng_mosaic(64, 64), bps=16))
    d = open_dng(bytes(trunc))
    d.data = d.data[:2000]
    d._mosaic = None
    with pytest.raises(RawDecodeError):
        d.mosaic()
    with pytest.raises(RawDecodeError, match="AsShotNeutral"):
        wb_gains(open_dng(build_dng(rng_mosaic(8, 8), with_neutral=False)))


# ---- 码值与半分辨率 ----------------------------------------------------------


def research_half(d, hw=None):
    """按研究代码的算式（model_adapter.raw / camera.scene）在整幅上算：同色取均值 ÷ neutral × 4095，逐点读取。"""
    y0, x0, h, w = d.crop_window()
    h2, w2 = h // 2, w // 2
    acc = [[] for _ in range(3)]
    for dy in (0, 1):
        for dx in (0, 1):
            yy = (y0 + dy + 2 * np.arange(h2))[:, None]
            xx = (x0 + dx + 2 * np.arange(w2))[None, :]
            acc[int(d.cfa_at(y0 + dy, x0 + dx))].append(d.normalised_at(yy, xx))
    rgb = np.stack([np.mean(a, axis=0) for a in acc], axis=-1) / d.as_shot_neutral
    return rgb * 4095


@pytest.mark.parametrize("origin", [(0, 0), (1, 0), (0, 1), (1, 1), (8, 7)])
def test_half_res_equals_research_arithmetic(origin):
    h, w = 40, 52
    m = rng_mosaic(h, w, 4095, seed=7)
    d = open_dng(
        build_dng(
            m,
            black=(16,),
            white=4000,
            crop_origin=origin,
            active=(0, 0, h, w),
            crop_size=(w - origin[0], h - origin[1]),
            neutral=(0.645161, 1.0, 0.574712),
        )
    )
    got = half_res_codes(d, np.float64)
    want = research_half(d)
    assert got.shape == want.shape == (d.crop_size[1] // 2, d.crop_size[0] // 2, 3)
    assert np.max(np.abs(got - want)) < 1e-9
    assert half_res_codes(d).dtype == np.float32
    assert np.max(np.abs(half_res_codes(d) - want)) < 1e-3  # float32 的量化误差


def test_half_res_of_flat_scene_is_exact_white_balanced_gray():
    neutral = (0.5, 1.0, 0.6)
    h, w = 16, 16
    scene = np.empty((h, w, 3))
    scene[..., 0], scene[..., 1], scene[..., 2] = 0.25 * neutral[0], 0.25 * neutral[1], 0.25 * neutral[2]
    d = open_dng(build_dng(bayer_scene(h, w, scene, white=4095), neutral=neutral, white=4095, active=(0, 0, h, w)))
    codes = half_res_codes(d, np.float64)
    assert np.allclose(codes, 0.25 * 4095, atol=0.6)  # 取整到 12 位造成的 ≤0.5 码 / neutral


# ---- 去马赛克 ---------------------------------------------------------------


@pytest.mark.parametrize("origin", [(0, 0), (1, 0), (0, 1), (1, 1)])
def test_demosaic_flat_field_is_exact_including_borders(origin):
    h, w = 24, 30
    neutral = (0.5, 1.0, 0.6)
    scene = np.empty((h, w, 3))
    scene[...] = (0.3 * neutral[0], 0.3 * neutral[1], 0.3 * neutral[2])
    m = bayer_scene(h, w, scene, white=4095)
    d = open_dng(build_dng(m, neutral=neutral, white=4095, crop_origin=origin, active=(0, 0, h, w), crop_size=(w - 2, h - 2)))
    rgb = demosaic_full(d)
    assert rgb.shape == (h - 2, w - 2, 3)
    assert np.allclose(rgb, 0.3 * 4095, atol=0.8)


def test_demosaic_reconstructs_smooth_ramp_in_interior():
    """线性渐变：MHC 在内部应当（几乎）还原每个通道，边界附近不要求。"""
    h, w = 40, 48
    yy, xx = np.mgrid[0:h, 0:w].astype(float)
    scene = np.stack([0.2 + 0.004 * xx, 0.3 + 0.003 * yy, 0.5 - 0.002 * xx], axis=-1)
    m = bayer_scene(h, w, scene, white=4095)
    d = open_dng(build_dng(m, neutral=(1.0, 1.0, 1.0), white=4095, active=(0, 0, h, w)))
    rgb = demosaic_full(d)
    err = np.abs(rgb[4:-4, 4:-4] - scene[4:-4, 4:-4] * 4095)
    assert err.max() < 1.5, err.max()


@pytest.mark.parametrize("origin", [(0, 0), (1, 1)])
def test_strip_demosaic_is_bit_identical_to_full(origin):
    h, w = 50, 46
    m = rng_mosaic(h, w, 4095, seed=11)
    d = open_dng(
        build_dng(
            m,
            black=(30,),
            white=4095,
            crop_origin=origin,
            active=(0, 0, h, w),
            crop_size=(w - 2, h - 2),
            neutral=(0.645161, 1.0, 0.574712),
        )
    )
    full = demosaic_full(d)
    for rows in (4, 6, 16, 256):
        out = np.concatenate([s for _, s in demosaic_strips(d, rows)], axis=0)
        assert np.array_equal(out, full), rows
    # 起始行也对得上
    starts = [y for y, _ in demosaic_strips(d, 8)]
    assert starts == list(range(0, h - 2, 8))
    with pytest.raises(ValueError):
        list(demosaic_strips(d, 5))


def test_demosaic_keeps_sampled_values_at_their_own_sites():
    h, w = 20, 20
    m = rng_mosaic(h, w, 4095, seed=5)
    d = open_dng(build_dng(m, neutral=(0.5, 1.0, 0.6), white=4095, active=(0, 0, h, w)))
    rgb = demosaic_full(d)
    gains = wb_gains(d)
    yy, xx = np.indices((h, w))
    c = d.cfa_at(yy, xx)
    own = np.take_along_axis(rgb, c[..., None], axis=2)[..., 0]
    assert np.allclose(own, m.astype(float) / 4095 * gains[c], rtol=1e-5, atol=1e-3)


# ---- 方向与渲染 -------------------------------------------------------------


def test_orientation_swaps_dimensions_and_matches_rotation():
    h, w = 16, 24
    m = rng_mosaic(h, w, 4095, seed=2)
    base = render_full(open_dng(build_dng(m, active=(0, 0, h, w), orientation=1)))
    assert base.shape == (h, w, 3) and base.dtype == np.uint8
    for o, ref in ((3, base[::-1, ::-1]), (6, np.rot90(base, 3)), (8, np.rot90(base, 1)), (2, base[:, ::-1])):
        got = render_full(open_dng(build_dng(m, active=(0, 0, h, w), orientation=o)))
        assert np.array_equal(got, ref), o
    half6 = render_half(open_dng(build_dng(m, active=(0, 0, h, w), orientation=6)))
    assert half6.shape == (w // 2, h // 2, 3)
    assert np.array_equal(orient_array(np.arange(6).reshape(2, 3), 1), np.arange(6).reshape(2, 3))
    with pytest.raises(RawDecodeError):
        orient_array(np.zeros((2, 2)), 9)


def test_neutral_renderer_is_srgb_of_white_balanced_linear():
    assert NEUTRAL.id == "neutral-preview" and NEUTRAL.label == "中性转换" and not NEUTRAL.calibrated
    codes = np.array([[[0, 4095, 4095 * 0.18], [-50, 9000, 4095 * 0.0031308]]], np.float32)
    out = NEUTRAL.display(codes)
    assert out.dtype == np.uint8 and out.shape == (1, 2, 3)
    assert out[0, 0].tolist() == [0, 255, 118]  # 18% 灰 -> sRGB 118
    assert out[0, 1].tolist() == [0, 255, round(0.0031308 * 12.92 * 255)]  # 夹紧、线性段
    lin = np.linspace(0, 1, 11)
    assert np.allclose(srgb_encode(lin), np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * lin ** (1 / 2.4) - 0.055), atol=1e-6)


def test_renderer_interface_is_pluggable():
    h = w = 16
    d = open_dng(build_dng(rng_mosaic(h, w), active=(0, 0, h, w)))
    seen = []

    def invert(codes):
        seen.append((codes.dtype, codes.shape[2]))
        return (255 - NEUTRAL.display(codes)).astype(np.uint8)

    r = Renderer("test-inverse", "反相", "_inv", False, invert)
    assert np.array_equal(render_full(d, r), 255 - render_full(d, NEUTRAL))
    assert np.array_equal(render_half(d, r), 255 - render_half(d, NEUTRAL))
    assert all(dt == np.float32 and ch == 3 for dt, ch in seen)


# ---- 真实样本 ---------------------------------------------------------------

needs_sample = pytest.mark.skipif(not SAMPLE.is_file(), reason="captures/a12-50 样本不在本机")


@pytest.fixture(scope="module")
def sample():
    return open_dng(SAMPLE)


@needs_sample
def test_real_sample_structure(sample):
    d = sample
    assert d.sha256 == SAMPLE_SHA, "样本被改动过"
    assert (d.width, d.height, d.bps, d.endian) == (4304, 2862, 12, "big")
    assert d.crop_origin == (8, 7) and d.crop_size == (4288, 2848) and d.crop_window() == (7, 8, 2848, 4288)
    assert d.cfa.tolist() == [[0, 1], [1, 2]] and d.crop_cfa_phase().tolist() == [[1, 2], [0, 1]]
    assert d.active == (0, 0, 2862, 4304) and d.white == 4095 and d.black_pattern.tolist() == [[0.0]]
    assert d.lin_table is None and d.orientation == 1 and d.default_scale == (1.0, 1.0)
    assert d.as_shot_neutral.tolist() == pytest.approx([0.645161, 1.0, 0.574712])
    assert d.exif["iso"] == 400 and d.exif["when"] == "2026:10:16 18:51:53"
    assert d.mosaic().shape == (2862, 4304) and d.mosaic().max() <= 4095


@needs_sample
def test_real_sample_half_and_full_sizes(sample):
    half = half_res_codes(sample)
    assert half.shape == (1424, 2144, 3) and half.dtype == np.float32
    assert render_half(sample).shape == (1424, 2144, 3)
    full = render_full(sample)
    assert full.shape == (2848, 4288, 3) and full.dtype == np.uint8


@needs_sample
@pytest.mark.skipif(not RESEARCH_DNG.is_file(), reason="研究代码不在本机")
def test_real_sample_matches_research_reader_and_half_res_arithmetic(sample):
    spec = importlib.util.spec_from_file_location("research_dng_scene", RESEARCH_DNG)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["research_dng_scene"] = mod
    spec.loader.exec_module(mod)
    r = mod.Dng(SAMPLE)
    for field in (
        "width",
        "height",
        "bps",
        "white",
        "orientation",
        "active",
        "crop_origin",
        "crop_size",
        "default_scale",
        "endian",
        "baseline_exposure",
        "sha256",
    ):
        assert getattr(sample, field) == getattr(r, field), field
    assert np.array_equal(sample.cfa, r.cfa) and np.array_equal(sample.as_shot_neutral, r.as_shot_neutral)
    assert np.array_equal(sample.black_pattern, r.black_pattern) and sample.crop_window() == r.crop_window()
    assert np.array_equal(sample.mosaic(), r.mosaic())
    y0, x0, h, w = r.crop_window()
    ys = np.random.default_rng(0).integers(y0, y0 + h, 200000)
    xs = np.random.default_rng(1).integers(x0, x0 + w, 200000)
    assert np.array_equal(sample.normalised_at(ys, xs), r.normalised_at(ys, xs))
    assert np.array_equal(sample.normalised_block(y0, y0 + h, x0, x0 + w)[ys - y0, xs - x0], r.normalised_at(ys, xs))
    got = half_res_codes(sample, np.float64)
    want = research_half(r)
    assert got.shape == want.shape and np.max(np.abs(got - want)) < 1e-9
    assert np.max(np.abs(half_res_codes(sample) - want)) < 5e-4


@needs_sample
def test_real_sample_full_res_agrees_with_half_res_on_average(sample):
    """诊断（U3 的代码域版本）：全分辨率去马赛克后 2×2 平均，对半分辨率合并的相对差。只做宽松断言，数字写进报告。"""
    full = demosaic_full(sample)
    h2, w2 = full.shape[0] // 2, full.shape[1] // 2
    binned = full[: 2 * h2, : 2 * w2].reshape(h2, 2, w2, 2, 3).mean(axis=(1, 3))
    half = half_res_codes(sample)
    mid = (half > 200) & (half < 3500)  # 避开暗部和高光
    rel = np.abs(binned - half)[mid] / half[mid]
    assert np.median(rel) < 0.01 and np.percentile(rel, 99) < 0.25
    # 全局亮度一致
    assert abs(full.mean() / half.mean() - 1) < 0.005
