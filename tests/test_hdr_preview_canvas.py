"""HDR 预览真正写进画布的值，按合成器的方式解码以后：增益为 1 的地方和 SDR 预览一样，高光按增益变亮。

HDR 画布是 rgba16float、colorSpace 'srgb'、toneMapping extended。WebGPU 规范说画布里的值“按 colorSpace 显示”。
'srgb' 是带 sRGB 传递函数、范围扩展到 1 以上的 sRGB：WebKit 把它配成 ExtendedSRGB，Chromium 配成
gfx::ColorSpace::CreateSRGB()。所以合成器先用 sRGB EOTF 解码再显示，和 SDR 画布（bgra8unorm）一样。
写进线性值，整张图就会暗一档 gamma（2026-11-26 机主 iPhone 上的现象）。

页面脚本 tests/support/hdr_canvas_harness.mjs 走引擎真的显示路径：attach 一张 <canvas>，engine.frame()，在同一个任务里
把画布当前纹理拷出来，SDR 一帧、HDR 一帧。这里按 sRGB EOTF 解码再比。无头 Chrome + SwiftShader。
合成数据的门槛：解码后 |HDR − SDR| ≤ 2⁻⁹ · 参考值（float16 存编码值的舍入，经 EOTF 最多放大 2.4 倍，约 2⁻⁹·⁷）。
真照片那条只打印脱敏统计（平均亮度、比例），不打印文件名、拍摄时间或 EXIF。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from gpu_support import run_engine
from support import samples
from support.hdr_parity import real_pairs
from test_mobile_ui_hdr_preview_parity import _cli_grid, _need, _preview_rgb

HARNESS = Path(__file__).resolve().parent / "support" / "hdr_canvas_harness.mjs"
BOUND = 2.0**-9
FLOOR = 2.0**-20
STAGE = 0x2A
SHADOW_RGB = (40, 110, 255)
HIGHLIGHT_RGB = (255, 50, 40)
LUMA = np.array([0.2126, 0.7152, 0.0722])
EXPORT_META = {"gain_min": 0.0, "gain_max": 1.25, "gamma": 1.0, "offset_sdr": 1 / 64, "offset_hdr": 1 / 64, "cap_min": 0.0, "cap_max": 1.25}


def srgb_decode(v: np.ndarray) -> np.ndarray:
    """合成器对 colorSpace 'srgb' 的解码：sRGB EOTF，按 CSS Color 4 扩展到 1 以上和负数。"""
    v = np.asarray(v, np.float64)
    a = np.abs(v)
    return np.sign(v) * np.where(a <= 0.04045, a / 12.92, ((a + 0.055) / 1.055) ** 2.4)


def _ramp() -> np.ndarray:
    """256 × 4，每个通道都有 0–255 的每个码值。第一行是灰。"""
    c = np.arange(256, dtype=np.int64)
    rows = [(c, c, c), (c, 255 - c, (c * 7) % 256), ((c * 3) % 256, c, 255 - c), (255 - c, (c * 11) % 256, c)]
    return np.stack([np.stack(r, axis=-1) for r in rows]).astype(np.uint8)


def _case(
    files: dict[str, bytes],
    cid: str,
    rgb: np.ndarray,
    canvas: tuple[int, int],
    gain: np.ndarray,
    meta: dict[str, float],
    clip: bool = False,
    headroom: float | None = None,
) -> dict[str, Any]:
    h, w = rgb.shape[:2]
    rgba = np.concatenate([rgb, np.full((h, w, 1), 255, np.uint8)], axis=-1)
    files[f"/{cid}.rgba"] = np.ascontiguousarray(rgba).tobytes()
    files[f"/{cid}.gain"] = np.ascontiguousarray(gain, np.uint8).tobytes()
    return {
        "id": cid,
        "image": {"url": f"/{cid}.rgba", "width": w, "height": h},
        "canvas": {"width": canvas[0], "height": canvas[1]},
        "clip": {"shadow": clip, "highlight": clip},
        "gain": {"url": f"/{cid}.gain", "width": int(gain.shape[1]), "height": int(gain.shape[0])},
        "meta": meta,
        "headroom": headroom,
    }


def _run(tmp_path: Path, files: dict[str, bytes], cases: list[dict[str, Any]]) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    images, summary = run_engine(tmp_path, files, cases, timeout=300, harness=HARNESS)
    assert summary.get("dpr") == 1, summary.get("dpr")
    for case in cases:
        got = summary["cases"][case["id"]]
        # 解码方式取决于画布的配置。这里按 'srgb' 解码，配置变了这些测试要跟着改。
        assert got["hdr"]["format"] == "rgba16float", got
        if got["hdr"]["config"] is not None:
            assert got["hdr"]["config"]["colorSpace"] == "srgb", got
        assert images[f"{case['id']}-sdr"].shape[:2] == (case["canvas"]["height"], case["canvas"]["width"])
        assert images[f"{case['id']}-hdr"].shape[:2] == (case["canvas"]["height"], case["canvas"]["width"])
    return images, summary


def _same_light(hdr: np.ndarray, sdr: np.ndarray, what: str) -> None:
    ref = srgb_decode(sdr.astype(np.float64) / 255.0)
    got = srgb_decode(hdr[..., :3])
    err = np.abs(got - ref)
    bad = err > BOUND * ref + FLOOR
    if bad.any():
        at = [tuple(int(i) for i in p) for p in np.argwhere(bad)[:6]]
        rows = [f"{p}: sdr code {int(sdr[p])} → {ref[p]:.5f}, hdr canvas {float(hdr[p]):.5f} → {got[p]:.5f}" for p in at]
        pytest.fail(f"{what}: {int(bad.sum())} of {bad.size} channels differ after decoding\n" + "\n".join(rows))


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory: pytest.TempPathFactory) -> dict[str, np.ndarray]:
    files: dict[str, bytes] = {}
    ramp = _ramp()
    grey = np.array([[[255] * 3, [128] * 3, [64] * 3, [0] * 3]], np.uint8)
    flat = EXPORT_META | {"offset_sdr": 0.0, "offset_hdr": 0.0}
    cases = [
        _case(files, "identity", ramp, (256, 4), np.zeros((2, 8), np.uint8), EXPORT_META),
        _case(files, "stage", ramp, (256, 12), np.zeros((2, 8), np.uint8), EXPORT_META, clip=True),
        _case(files, "gain2", grey, (4, 1), np.full((1, 4), 255, np.uint8), flat | {"gain_max": 1.0, "cap_max": 1.0}),
        _case(files, "gain1p25", grey, (4, 1), np.full((1, 4), 255, np.uint8), flat),
    ]
    images, _summary = _run(tmp_path_factory.mktemp("hdr_canvas"), files, cases)
    return images


def test_hdr_canvas_equals_sdr_below_white(synthetic: dict[str, np.ndarray]) -> None:
    sdr = synthetic["identity-sdr"]
    for ch in range(3):
        assert np.array_equal(np.unique(sdr[..., ch]), np.arange(256)), f"channel {ch} misses codes"
    _same_light(synthetic["identity-hdr"], sdr, "gain 1")


@pytest.mark.parametrize(("cid", "factor"), [("gain2", 2.0), ("gain1p25", 2.0**1.25)])
def test_hdr_canvas_highlights_scale_by_the_gain(synthetic: dict[str, np.ndarray], cid: str, factor: float) -> None:
    hdr = srgb_decode(synthetic[f"{cid}-hdr"][0, :, :3])
    codes = np.array([255, 128, 64, 0], np.float64)
    want = factor * srgb_decode(codes / 255.0)[:, None] * np.ones((1, 3))
    err = np.abs(hdr - want)
    assert np.all(err <= BOUND * np.maximum(1.0, want) + FLOOR), {"decoded": hdr[:, 0].tolist(), "want": want[:, 0].tolist()}
    assert abs(hdr[0, 0] - factor) <= BOUND * factor


def test_hdr_canvas_stage_and_clip_warnings_match_sdr(synthetic: dict[str, np.ndarray]) -> None:
    sdr = synthetic["stage-sdr"]
    # 照片在第 4–7 行，上下各四行是舞台；码值 ≤ 2 的灰盖成蓝，≥ 253 的盖成红。
    assert np.all(sdr[:4] == STAGE) and np.all(sdr[8:] == STAGE)
    assert np.all(sdr[4, :3] == SHADOW_RGB) and np.all(sdr[4, 253:] == HIGHLIGHT_RGB)
    _same_light(synthetic["stage-hdr"], sdr, "stage and clip warnings")


def _luma(lin: np.ndarray) -> np.ndarray:
    return lin @ LUMA


def _gain_one(codes: np.ndarray, w: int, h: int) -> np.ndarray:
    """照片像素里增益确实是 1 的：双线性取样碰到的网格格子（3 × 3 邻域，边缘夹住）全是 0。"""
    zero = np.pad(codes == 0, 1, mode="edge")
    gh, gw = codes.shape
    eroded = np.ones((gh, gw), bool)
    for dy in range(3):
        for dx in range(3):
            eroded &= zero[dy : dy + gh, dx : dx + gw]
    ys = np.minimum(((np.arange(h) + 0.5) * gh / h).astype(int), gh - 1)
    xs = np.minimum(((np.arange(w) + 0.5) * gw / w).astype(int), gw - 1)
    return eroded[ys[:, None], xs[None, :]]


def _pick(tmp_path: Path) -> list[tuple[str, np.ndarray, dict[str, Any]]]:
    """样本里最暗的一张和亮度居中的一张（按预览图的平均线性亮度），各取第一对能算出 HDR 网格的。"""
    pairs = real_pairs(12)
    if len(pairs) < 2:
        samples.missing("fewer than 2 RAW+JPG pairs")
    rows = []
    for jpg, dng in pairs:
        rgb = _preview_rgb(jpg, "plain")
        rows.append((float(_luma(srgb_decode(rgb / 255.0)).mean()), jpg, dng))
    rows.sort(key=lambda r: r[0])
    mid = len(rows) // 2
    picked = []
    for label, order in (("dark", rows), ("mid", rows[mid:] + rows[:mid][::-1])):
        for index, (_mean, jpg, dng) in enumerate(order):
            if any(jpg == p[3] for p in picked):
                continue
            rgb = _preview_rgb(jpg, "plain")
            work = tmp_path / f"{label}{index}"
            work.mkdir()
            grid = _cli_grid(work, rgb, dng, jpg)
            if grid["ok"]:
                picked.append((label, rgb, grid, jpg))
                break
    if len(picked) < 2:
        samples.missing("fewer than 2 RAW+JPG pairs with an HDR grid")
    return [(label, rgb, grid) for label, rgb, grid, _jpg in picked]


def test_hdr_canvas_real_photo_keeps_sdr_brightness_where_gain_is_one(tmp_path: Path) -> None:
    _need()
    picked = _pick(tmp_path)
    files: dict[str, bytes] = {}
    cases = []
    for label, rgb, grid in picked:
        codes = np.asarray(grid["codes"], np.uint8)
        cases.append(_case(files, label, rgb, (rgb.shape[1], rgb.shape[0]), codes, grid["meta"]))
    browser = tmp_path / "browser"
    browser.mkdir()
    images, _summary = _run(browser, files, cases)
    stats = {}
    for label, rgb, grid in picked:
        h, w = rgb.shape[:2]
        mask = _gain_one(np.asarray(grid["codes"], np.uint8), w, h)
        sdr = _luma(srgb_decode(images[f"{label}-sdr"].astype(np.float64) / 255.0))[mask].mean()
        hdr = _luma(srgb_decode(images[f"{label}-hdr"][..., :3]))[mask].mean()
        stats[label] = {
            "gain_one_share": round(float(mask.mean()), 4),
            "sdr_mean_luma": round(float(sdr), 5),
            "hdr_mean_luma": round(float(hdr), 5),
            "relative": round(float(hdr / sdr - 1.0), 5),
        }
    print(json.dumps(stats))
    for label, s in stats.items():
        assert s["gain_one_share"] > 0.25, (label, s)
        assert abs(s["relative"]) <= 0.01, stats
