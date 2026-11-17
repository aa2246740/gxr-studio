"""HDR 显示遍和 CPU 上的同一条 ISO 21496-1 公式，差在 float16 的舍入里。

无头 Chrome + SwiftShader，画进离屏 rgba16float。合成像素，不读照片。
门槛：每个通道 |GPU − CPU| ≤ 2⁻⁹ · max(1, |CPU|)。float16 在 1 附近的 1 ulp 是 2⁻¹⁰，
这里留两倍，盖住 GPU pow 和存储成 float16 再读回的舍入。
"""

from __future__ import annotations

import numpy as np

from gpu_support import run_engine

BOUND = 2.0**-9


def _srgb_to_linear(code: np.ndarray) -> np.ndarray:
    v = code.astype(np.float32) / np.float32(255.0)
    low = v / np.float32(12.92)
    high = ((v + np.float32(0.055)) / np.float32(1.055)) ** np.float32(2.4)
    return np.where(v <= np.float32(0.04045), low, high).astype(np.float32)


def _apply(sdr: np.ndarray, gain: np.ndarray, meta: dict[str, float], weight: float) -> np.ndarray:
    lin = _srgb_to_linear(sdr)
    g = gain.astype(np.float32) / np.float32(255.0)
    shaped = np.power(np.maximum(g, np.float32(0)), np.float32(1.0 / meta["gamma"]))
    log2_gain = np.float32(meta["gain_min"]) + (np.float32(meta["gain_max"]) - np.float32(meta["gain_min"])) * shaped
    factor = np.exp2(log2_gain * np.float32(weight))
    hdr = (lin + np.float32(meta["offset_sdr"])) * factor[..., None] - np.float32(meta["offset_hdr"])
    return np.maximum(hdr, np.float32(0))


def test_hdr_shader_matches_the_iso_formula(tmp_path) -> None:
    h = w = 4
    sdr = np.zeros((h, w, 3), np.uint8)
    sdr[..., 0] = np.arange(h * w, dtype=np.uint8).reshape(h, w) * 8
    sdr[..., 1] = 40
    sdr[..., 2] = 200
    gain = ((np.arange(h * w) * 17) % 256).astype(np.uint8).reshape(h, w)
    meta = {"gain_min": 0.0, "gain_max": 1.25, "gamma": 1.0, "offset_sdr": 1 / 64, "offset_hdr": 1 / 64, "cap_min": 0.0, "cap_max": 1.25}
    rgba = np.concatenate([sdr, np.full((h, w, 1), 255, np.uint8)], axis=-1)
    case = {
        "id": "formula",
        "hdr": {
            "sdr": rgba.reshape(-1).astype(int).tolist(),
            "width": w,
            "height": h,
            "gain": gain.reshape(-1).astype(int).tolist(),
            "gainWidth": w,
            "gainHeight": h,
            "meta": meta,
            "headroom": None,
        },
    }
    got, _summary = run_engine(tmp_path, {}, [case])
    image = got["formula"]
    assert image.shape == (h, w, 4)
    ref = _apply(sdr, gain, meta, 1.0)
    err = np.abs(image[..., :3] - ref)
    limit = BOUND * np.maximum(1.0, np.abs(ref))
    assert np.all(err <= limit + 1e-8), float(err.max())
    assert image[..., 3].min() > 0.9
