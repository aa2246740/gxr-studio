"""直方图测试共用的东西：numpy 参考实现（规则的"标准答案"）和几张已知的合成图。

规则（与 web/assets/histogram_math.js、frontend/src/shaders/hist.wgsl 逐条对应，见 docs/histogram.md）：
- 红、绿、蓝各 256 档；亮度 = (2126R + 7152G + 722B + 5000) // 10000；
- 暗部压死：三个颜色都 ≤ 2；高光溢出：三个颜色都 ≥ 253；
- 取样：矩形里横竖每隔 stride 个像素取一个；stride 是让取到的点不超过 262144 个的最小整数（至少 1）。
这里的实现故意不参考前端代码，只按上面的话写。
"""

from __future__ import annotations

import io
import math

import numpy as np
from PIL import Image

SHADOW_MAX = 2
HIGHLIGHT_MIN = 253
WARN_SHARE = 0.005
MAX_SAMPLES = 262144
SHADOW_RGB = (40, 110, 255)
HIGHLIGHT_RGB = (255, 50, 40)
AT = {"r": 0, "g": 256, "b": 512, "y": 768, "crushed": 1024, "blown": 1025}


def stride_for(width: int, height: int, max_samples: int = MAX_SAMPLES) -> int:
    s = 1
    while math.ceil(width / s) * math.ceil(height / s) > max_samples:
        s += 1
    return s


def pixel_rect(width: int, height: int, region: tuple[float, float, float, float] | None) -> tuple[int, int, int, int]:
    """归一化矩形 (x, y, w, h) → 像素矩形 (x0, y0, x1, y1)；四舍五入（0.5 向上），至少一个像素。"""
    if region is None:
        return 0, 0, width, height

    def rnd(v: float) -> int:
        return math.floor(v + 0.5)

    x, y, w, h = region
    x0 = min(max(rnd(x * width), 0), width - 1)
    y0 = min(max(rnd(y * height), 0), height - 1)
    x1 = min(max(rnd((x + w) * width), x0 + 1), width)
    y1 = min(max(rnd((y + h) * height), y0 + 1), height)
    return x0, y0, x1, y1


def reference(
    rgb: np.ndarray, rect: tuple[int, int, int, int] | None = None, stride: int | None = None, max_samples: int = MAX_SAMPLES
) -> dict[str, object]:
    """按规则数一张 (h, w, 3) uint8 图。返回和显卡一样排列的 1026 个计数、取样点数、取样间隔、像素矩形。"""
    h, w = rgb.shape[:2]
    x0, y0, x1, y1 = rect or (0, 0, w, h)
    step = stride or stride_for(x1 - x0, y1 - y0, max_samples)
    px = rgb[y0:y1:step, x0:x1:step].reshape(-1, 3).astype(np.int64)
    r, g, b = px[:, 0], px[:, 1], px[:, 2]
    luma = (2126 * r + 7152 * g + 722 * b + 5000) // 10000
    counts = np.zeros(1026, np.int64)
    for name, ch in (("r", r), ("g", g), ("b", b), ("y", luma)):
        counts[AT[name] : AT[name] + 256] = np.bincount(ch, minlength=256)
    counts[AT["crushed"]] = int((px.max(axis=1) <= SHADOW_MAX).sum())
    counts[AT["blown"]] = int((px.min(axis=1) >= HIGHLIGHT_MIN).sum())
    return {"counts": counts, "samples": len(px), "stride": step, "rect": (x0, y0, x1, y1)}


def overlay(rgb: np.ndarray, *, shadow: bool, highlight: bool) -> np.ndarray:
    """溢出提示的标准答案：暗部压死的点盖蓝，高光溢出的点盖红（逐个像素）。"""
    out = rgb.copy()
    if shadow:
        out[rgb.max(axis=2) <= SHADOW_MAX] = SHADOW_RGB
    if highlight:
        out[rgb.min(axis=2) >= HIGHLIGHT_MIN] = HIGHLIGHT_RGB
    return out


def png(rgb: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, "PNG")
    return buf.getvalue()


# ---- 已知的合成图 -----------------------------------------------------------------------


def ramp() -> np.ndarray:
    """256 × 64：第 x 列的三个颜色分别是 x、255 − x、(3x) mod 256——每一档都出现，三个通道各不相同。"""
    x = np.arange(256, dtype=np.int64)
    row = np.stack([x, 255 - x, (3 * x) % 256], axis=-1)
    return np.repeat(row[None, :, :], 64, axis=0).astype(np.uint8)


def noise(h: int = 300, w: int = 500, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, (h, w, 3), dtype=np.uint8)


def clipping_blocks() -> np.ndarray:
    """240 × 320 的灰底，放上各种边界情况的块：纯黑、1–2、3（刚好不算）、纯白、253–254、252（刚好不算）、只有一个颜色到顶/到底。"""
    img = np.full((240, 320, 3), 128, np.uint8)
    blocks = [
        ((0, 0, 40, 40), (0, 0, 0)),
        ((0, 40, 40, 80), (1, 2, 2)),
        ((0, 80, 40, 120), (3, 3, 3)),  # 不算：有一个颜色是 3
        ((0, 120, 40, 160), (2, 2, 3)),  # 不算
        ((40, 0, 80, 40), (255, 255, 255)),
        ((40, 40, 80, 80), (253, 254, 255)),
        ((40, 80, 80, 120), (252, 255, 255)),  # 不算：有一个颜色是 252
        ((40, 120, 80, 160), (255, 10, 10)),  # 不算：只有红到顶
        ((80, 0, 120, 40), (0, 200, 200)),  # 不算：只有红到底
        ((80, 40, 120, 80), (255, 255, 253)),
    ]
    for (y0, x0, y1, x1), colour in blocks:
        img[y0:y1, x0:x1] = colour
    return img
