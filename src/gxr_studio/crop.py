"""裁切几何。纯函数，不碰文件。

约定（与 ARCHITECTURE §4.4 一致）：
- `rect = [x, y, w, h]`，归一化到"按方向转正后的画面"（宽 W、高 H 像素）；
- `angle_deg` 为拉直角度，正值 = 画面顺时针转；范围 ±45°；
- 画面绕中心旋转后仍放在 W×H 的画布里，`rect` 定义在这块旋转后的画布上；
  框的四个角必须都落在原画面之内（不允许出现空白角）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

MAX_ANGLE = 45.0
MIN_SIDE_PX = 16
RATIO_TOLERANCE = 0.01  # 用户给的框与预设比例的相对偏差
EDGE_TOLERANCE_PX = 1.0  # 角点超出原画面多少像素以内算在里面（界面取整造成的）

# 预设 -> (长边, 短边)；original / free 没有固定比例
PRESETS: dict[str, tuple[int, int] | None] = {
    "original": None,
    "free": None,
    "3:2": (3, 2),
    "4:3": (4, 3),
    "1:1": (1, 1),
    "4:5": (5, 4),
    "16:9": (16, 9),
    "16:10": (16, 10),  # 电脑壁纸
    "1.91:1": (191, 100),  # Instagram 横图 / 链接预览
    "2.35:1": (235, 100),  # 微信公众号封面
    "xpan-65-24": (65, 24),
}


class CropError(ValueError):
    def __init__(self, code: str, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


@dataclass(frozen=True)
class Crop:
    preset: str
    rect: tuple[float, float, float, float]
    angle_deg: float = 0.0

    def to_json(self) -> dict[Any, Any]:
        return {"preset": self.preset, "rect": list(self.rect), "angle_deg": self.angle_deg}


def round_half_up(v: float) -> int:
    return int(math.floor(v + 0.5))


def target_ratio(preset: str, width: int, height: int, flip: bool = False) -> Fraction | None:
    """框的 宽/高。竖拍的画面把预设转过来（XPan 在竖拍上是又高又窄的条）；flip 再转一次。"""
    if preset not in PRESETS:
        raise CropError("unknown_preset", "无法裁剪。请重新选择比例。", preset)
    if preset == "free":
        return None
    if preset == "original":
        return Fraction(width, height)
    long_side, short_side = PRESETS[preset]  # type: ignore[misc]
    landscape = (width >= height) != flip
    return Fraction(long_side, short_side) if landscape else Fraction(short_side, long_side)


def crop_box(rect: tuple[float, float, float, float], width: int, height: int) -> tuple[int, int, int, int]:
    """归一化矩形 -> 像素 (x0, y0, w, h)；统一用四舍五入（0.5 向上）。"""
    x, y, w, h = rect
    x0, y0 = round_half_up(x * width), round_half_up(y * height)
    w_px, h_px = round_half_up(w * width), round_half_up(h * height)
    return x0, y0, w_px, h_px


def box_inside(box: tuple[float, float, float, float], width: int, height: int, angle_deg: float, tol: float = 1e-6) -> bool:
    """框的四个角（在旋转后的画布上）按反方向转回去，是否都在原画面内。"""
    x0, y0, w, h = box
    th = math.radians(angle_deg)
    c, s = math.cos(th), math.sin(th)
    cx, cy = width / 2, height / 2
    for px, py in ((x0, y0), (x0 + w, y0), (x0, y0 + h), (x0 + w, y0 + h)):
        dx, dy = px - cx, py - cy
        sx = dx * c + dy * s
        sy = -dx * s + dy * c
        if abs(sx) > width / 2 + tol or abs(sy) > height / 2 + tol:
            return False
    return True


def _check_angle(angle_deg: float) -> float:
    if not isinstance(angle_deg, (int, float)) or not math.isfinite(angle_deg):
        raise CropError("bad_angle", "无法裁剪。拉直角度无效。")
    if abs(angle_deg) > MAX_ANGLE + 1e-9:
        raise CropError("bad_angle", f"无法裁剪。拉直角度要在 ±{MAX_ANGLE:g}° 之内。")
    return float(angle_deg)


def default_crop(preset: str, width: int, height: int, angle_deg: float = 0.0, flip: bool = False) -> Crop:
    """某个预设在当前角度下、居中的最大框。XPan 在 4288×2848 上得到 4288×1583。"""
    angle = _check_angle(angle_deg)
    ratio = target_ratio(preset, width, height, flip)
    if ratio is None:
        ratio = Fraction(width, height)
    th = math.radians(abs(angle))
    c, s = math.cos(th), math.sin(th)
    q = float(1 / ratio)  # 高 / 宽
    w_max = min(width / (c + q * s), height / (s + q * c))
    w_px = min(width, int(math.floor(w_max + 1e-9)))
    while w_px >= MIN_SIDE_PX:
        h_px = int(math.floor(w_px / ratio + Fraction(1, 2)))
        h_px = min(h_px, height)
        if box_inside(((width - w_px) / 2, (height - h_px) / 2, w_px, h_px), width, height, angle, 1e-6):
            break
        w_px -= 1
    else:
        raise CropError("too_small", "裁剪框超出画面范围")
    rect = ((width - w_px) / 2 / width, (height - h_px) / 2 / height, w_px / width, h_px / height)
    return Crop(preset=preset, rect=rect, angle_deg=angle)


def fit_target_crop(preset: str, rect: Any, angle_deg: float, width: int, height: int) -> Crop:
    """目标框：不合格也不拒绝，只整理成能交给收框的样子（有限、正尺寸、在画面里）。

    撤销、重做和转 90° 之后的框是界面推出来的，落在新画布的有效区外是常事；这时要的是「离它最近的合格框」，
    不是一个报错。预设和角度仍然照常检查，比例、有效区由调用方的收框（`fitted_crop`）来保证。"""
    angle = _check_angle(angle_deg)
    target_ratio(preset, width, height)  # 不认识的预设照样报错
    try:
        x, y, w, h = (float(v) for v in rect)
    except (TypeError, ValueError):
        raise CropError("bad_rect", "裁剪框超出画面范围") from None
    if not all(math.isfinite(v) for v in (x, y, w, h)) or w <= 0 or h <= 0:
        raise CropError("bad_rect", "裁剪框超出画面范围")
    # 太小的目标（API 能给到，界面的最小框拦着）：以中心为准放大到够收一个合格框——按预设比例收完短边也不能小于 MIN_SIDE_PX。
    # 横竖不变，只补不够的那一边：收框的横竖跟着目标框，把两边都放大到长边那么长，会把窄的目标变成方的、再变成反方向的大框
    tw_px, th_px = w * width, h * height
    long_side, short_side = float(MIN_SIDE_PX), float(MIN_SIDE_PX)
    ratio = target_ratio(preset, width, height)
    if preset == "original":
        # 原始不是整数倍：收的时候用浮点的画布比例（clamp_norm_rect 的 aspect）；分子分母是互质的大整数（例如 660×503），不能拿来算
        aspect = width / height
        long_side = float(math.ceil(MIN_SIDE_PX * max(aspect, 1 / aspect)) + 1)
        short_side = float(MIN_SIDE_PX + 1)
    elif ratio is not None:
        # 定比例的预设收出来的是比例分子分母的整数倍：短边够 MIN_SIDE_PX 的最小一块
        small, big = sorted((ratio.numerator, ratio.denominator))
        k = -(-MIN_SIDE_PX // small)
        long_side, short_side = float(k * big), float(k * small)
    need_w, need_h = (long_side, short_side) if tw_px >= th_px else (short_side, long_side)
    cx, cy = x + w / 2, y + h / 2
    w, h = min(max(w, need_w / width), 1.0), min(max(h, need_h / height), 1.0)
    x, y = min(max(cx - w / 2, 0.0), 1.0 - w), min(max(cy - h / 2, 0.0), 1.0 - h)
    return Crop(preset=preset, rect=(x, y, w, h), angle_deg=angle)


def check_min_size(rect: tuple[float, float, float, float], width: int, height: int) -> None:
    """收完的框，短边不能小于 MIN_SIDE_PX（和 validate_crop 一样）。"""
    _x0, _y0, w_px, h_px = crop_box(rect, width, height)
    if w_px < MIN_SIDE_PX or h_px < MIN_SIDE_PX:
        raise CropError("too_small", "裁剪框超出画面范围")


def validate_crop(
    preset: str, rect: Any, angle_deg: float, width: int, height: int, *, mirrored: bool = False, check_region: bool = True
) -> Crop:
    """校验用户给的框；不合格就抛 CropError，不悄悄修正。

    mirrored：画面左右翻转过。成片先拉直、再转 90°、最后翻转（finish.geometry.warp），翻转把倾斜的方向反过来，
    所以按反过来的角度看框在不在原画面里，和成片真正的有效区一致（PR #39 复审）。存下来的角度不变。

    check_region：要不要按拉直角度检查四个角在不在原画面里。透视、畸变拉开过的画布不是一块拉直后的矩形，这个检查对不上真正的
    有效区，调用方（write_crop）传 False，由后面的收框按真正的有效蒙版处理。
    """
    angle = _check_angle(angle_deg)
    ratio = target_ratio(preset, width, height)
    try:
        x, y, w, h = (float(v) for v in rect)
    except (TypeError, ValueError):
        raise CropError("bad_rect", "裁剪框超出画面范围") from None
    if not all(math.isfinite(v) for v in (x, y, w, h)):
        raise CropError("bad_rect", "裁剪框超出画面范围")
    eps = 1e-9
    if w <= 0 or h <= 0 or x < -eps or y < -eps or x + w > 1 + eps or y + h > 1 + eps:
        raise CropError("bad_rect", "裁剪框超出画面范围")
    x0, y0, w_px, h_px = crop_box((x, y, w, h), width, height)
    if w_px < MIN_SIDE_PX or h_px < MIN_SIDE_PX:
        raise CropError("too_small", "裁剪框超出画面范围")
    if ratio is not None:
        got = w_px / h_px
        # 横竖两个方向都认（界面可以把预设转 90°）
        off = min(abs(got - float(r)) / float(r) for r in (ratio, 1 / ratio))
        if off > RATIO_TOLERANCE:
            raise CropError("ratio_mismatch", "裁剪框超出画面范围")
    box = (x * width, y * height, w * width, h * height)
    if check_region and not box_inside(box, width, height, -angle if mirrored else angle, EDGE_TOLERANCE_PX):
        raise CropError("crop_outside_image", "裁剪框超出画面范围")
    return Crop(preset=preset, rect=(x, y, w, h), angle_deg=angle)


def affine_coeffs(box: tuple[int, int, int, int], width: int, height: int, angle_deg: float) -> tuple[Any, ...]:
    """Pillow `Image.transform(AFFINE)` 的系数：输出像素 (x, y) -> 原图坐标。"""
    x0, y0 = box[0], box[1]
    th = math.radians(angle_deg)
    c, s = math.cos(th), math.sin(th)
    cx, cy = width / 2, height / 2
    return (
        c,
        s,
        cx + c * (x0 - cx) + s * (y0 - cy),
        -s,
        c,
        cy - s * (x0 - cx) + c * (y0 - cy),
    )
