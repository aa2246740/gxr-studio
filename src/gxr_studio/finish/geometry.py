"""纠正。正方向按合同修订 1：拉直顺时针，垂直让上边变宽，水平让右边变宽，正畸变把边角往里收。

只有拉直、旋转、翻转时，画布仍是转正后的原尺寸，裁剪框坐标不用迁移。
透视或畸变会扩大画布，空白填 #1b1c1f。
"""

from __future__ import annotations

import math

import numpy as np

from ..crop import MIN_SIDE_PX, PRESETS, RATIO_TOLERANCE, ratio_off, target_ratio
from .spec import Geometry

FILL = (0x1B, 0x1C, 0x1F)


def _slider_s(slider: float) -> float:
    focal = 1.0 / math.tan(math.radians(13.5))
    return math.tan((slider / 100.0) * math.radians(30.0)) / focal


def _rot_flip(rgb: np.ndarray, valid: np.ndarray, geom: Geometry) -> tuple[np.ndarray, np.ndarray]:
    turns = geom.rotate90 % 4
    if turns:
        rgb = np.rot90(rgb, turns)
        valid = np.rot90(valid, turns)
    if geom.flip:
        rgb = np.fliplr(rgb)
        valid = np.fliplr(valid)
    return np.ascontiguousarray(rgb), np.ascontiguousarray(valid)


def _coverage(src_w: int, src_h: int, sx: np.ndarray, sy: np.ndarray) -> np.ndarray:
    return (sx >= -1e-3) & (sy >= -1e-3) & (sx <= src_w - 1 + 1e-3) & (sy <= src_h - 1 + 1e-3)


def _sample(image: np.ndarray, sx: np.ndarray, sy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    h, w = image.shape[:2]
    valid = _coverage(w, h, sx, sy)
    x0 = np.clip(np.floor(sx).astype(np.int32), 0, w - 1)
    y0 = np.clip(np.floor(sy).astype(np.int32), 0, h - 1)
    x1 = np.clip(x0 + 1, 0, w - 1)
    y1 = np.clip(y0 + 1, 0, h - 1)
    tx = (sx - np.floor(sx)).astype(np.float32)
    ty = (sy - np.floor(sy)).astype(np.float32)
    src = image.astype(np.float32)
    sample = (
        src[y0, x0] * ((1 - tx) * (1 - ty))[..., None]
        + src[y0, x1] * (tx * (1 - ty))[..., None]
        + src[y1, x0] * ((1 - tx) * ty)[..., None]
        + src[y1, x1] * (tx * ty)[..., None]
    )
    out = np.full(sample.shape, FILL, np.uint8)
    out[valid] = np.clip(sample[valid] + 0.5, 0, 255).astype(np.uint8)
    return out, valid


def _straighten_coords(width: int, height: int, angle: float) -> tuple[np.ndarray, np.ndarray]:
    """与 crop.affine_coeffs 同一套顺时针旋转，画布尺寸不变：输出像素在原图里的取样位置。"""
    th = math.radians(angle)
    c, s = math.cos(th), math.sin(th)
    yy, xx = np.mgrid[0:height, 0:width]
    cx, cy = (width - 1) / 2.0, (height - 1) / 2.0
    dx, dy = xx - cx, yy - cy
    return c * dx + s * dy + cx, -s * dx + c * dy + cy


def _warp_straighten(image: np.ndarray, angle: float) -> tuple[np.ndarray, np.ndarray]:
    h, w = image.shape[:2]
    if abs(angle) < 1e-9:
        return image, np.ones((h, w), dtype=bool)
    sx, sy = _straighten_coords(w, h, angle)
    return _sample(image, sx, sy)


def _lens(geom: Geometry, width: int, height: int) -> tuple[float, float, float, float, float, float]:
    radial = -(geom.distortion / 100.0) * 0.35
    sv = _slider_s(geom.vertical)
    sh = -_slider_s(geom.horizontal)
    th = math.radians(geom.straighten)
    return radial, sv, sh, math.cos(th), math.sin(th), math.hypot(width, height) / 2.0


def _expanded_bounds(width: int, height: int, geom: Geometry) -> tuple[int, int, int, int]:
    """真实输出画布的原点与宽高。边缘留量是 1.5 像素，不从缩小的试算画布放大。"""
    w, h = width, height
    radial, sv, sh, c, s, half_diag = _lens(geom, w, h)

    def forward(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        cx = x - (w - 1) / 2.0
        cy = y - (h - 1) / 2.0
        radius = np.hypot(cx, cy) / half_diag
        factor = 1.0 + radial * radius * radius
        cx, cy = cx * factor, cy * factor
        nx, ny = cx / (w / 2.0), cy / (h / 2.0)
        denom = 1.0 + sv * ny + sh * nx
        nx, ny = nx / denom, ny / denom
        cx, cy = nx * (w / 2.0), ny * (h / 2.0)
        return c * cx - s * cy, s * cx + c * cy

    edge_x = np.concatenate([np.linspace(0, w - 1, 80), np.linspace(0, w - 1, 80), np.zeros(40), np.full(40, w - 1)])
    edge_y = np.concatenate([np.zeros(80), np.full(80, h - 1), np.linspace(0, h - 1, 40), np.linspace(0, h - 1, 40)])
    fx, fy = forward(edge_x, edge_y)
    minx = math.floor(float(np.min(fx)) - 1.5)
    miny = math.floor(float(np.min(fy)) - 1.5)
    maxx = math.ceil(float(np.max(fx)) + 1.5)
    maxy = math.ceil(float(np.max(fy)) + 1.5)
    return minx, miny, max(1, maxx - minx), max(1, maxy - miny)


def _expanded_coords(width: int, height: int, geom: Geometry) -> tuple[np.ndarray, np.ndarray]:
    w, h = width, height
    radial, sv, sh, c, s, half_diag = _lens(geom, w, h)
    minx, miny, out_w, out_h = _expanded_bounds(w, h, geom)
    cols = minx + np.arange(out_w) + 0.5
    rows = miny + np.arange(out_h) + 0.5
    grid_x = np.broadcast_to(cols, (out_h, out_w)).copy()
    grid_y = np.broadcast_to(rows[:, None], (out_h, out_w)).copy()
    ox, oy = grid_x, grid_y
    rx = c * ox + s * oy
    ry = -s * ox + c * oy
    nx, ny = rx / (w / 2.0), ry / (h / 2.0)
    denom = 1.0 - sh * nx - sv * ny
    nx, ny = nx / denom, ny / denom
    cx, cy = nx * (w / 2.0), ny * (h / 2.0)
    r_out = np.hypot(cx, cy) / half_diag
    r_src = r_out.copy()
    if abs(radial) >= 1e-8:
        for _ in range(8):
            f = radial * r_src**3 + r_src - r_out
            df = 3.0 * radial * r_src**2 + 1.0
            r_src = r_src - f / np.where(np.abs(df) < 1e-6, 1.0, df)
    scale = np.ones_like(r_out)
    np.divide(r_src, r_out, out=scale, where=r_out > 1e-8)
    sx = cx * scale + (w - 1) / 2.0
    sy = cy * scale + (h - 1) / 2.0
    return sx, sy


def _warp_expanded(image: np.ndarray, geom: Geometry) -> tuple[np.ndarray, np.ndarray]:
    h, w = image.shape[:2]
    sx, sy = _expanded_coords(w, h, geom)
    return _sample(image, sx, sy)


def _rot_flip_mask(valid: np.ndarray, geom: Geometry) -> np.ndarray:
    turns = geom.rotate90 % 4
    if turns:
        valid = np.rot90(valid, turns)
    if geom.flip:
        valid = np.fliplr(valid)
    return np.ascontiguousarray(valid)


def warp_valid(width: int, height: int, geom: Geometry) -> np.ndarray:
    """只算有效像素蒙版，不生成 RGB。"""
    if geom.expands():
        sx, sy = _expanded_coords(width, height, geom)
        valid = _coverage(width, height, sx, sy)
    elif abs(geom.straighten) < 1e-9:
        valid = np.ones((height, width), dtype=bool)
    else:
        valid = _coverage(width, height, *_straighten_coords(width, height, geom.straighten))
    return _rot_flip_mask(valid, geom)


def warp(image: np.ndarray, geom: Geometry) -> tuple[np.ndarray, np.ndarray]:
    """返回 (rgb, valid)。valid 为 True 的像素来自原图。"""
    if geom.expands():
        rgb, valid = _warp_expanded(image, geom)
    else:
        rgb, valid = _warp_straighten(image, geom.straighten)
    return _rot_flip(rgb, valid, geom)


def warp_float(layer: np.ndarray, geom: Geometry) -> np.ndarray:
    """和 `warp` 同样的几何变换，用在浮点数据层上（例如导出 HDR 时的 RAW 亮度）：双线性取样，画面外是 NaN。"""
    h, w = layer.shape[:2]
    if geom.expands():
        sx, sy = _expanded_coords(w, h, geom)
    elif abs(geom.straighten) >= 1e-9:
        sx, sy = _straighten_coords(w, h, geom.straighten)
    else:
        return _rot_flip_mask(layer, geom)
    valid = _coverage(w, h, sx, sy)
    x0 = np.clip(np.floor(sx).astype(np.int32), 0, w - 1)
    y0 = np.clip(np.floor(sy).astype(np.int32), 0, h - 1)
    x1, y1 = np.clip(x0 + 1, 0, w - 1), np.clip(y0 + 1, 0, h - 1)
    tx = (sx - np.floor(sx)).astype(np.float32)[..., None]
    ty = (sy - np.floor(sy)).astype(np.float32)[..., None]
    src = layer.reshape(h, w, -1).astype(np.float32, copy=False)
    out = (src[y0, x0] * (1 - tx) + src[y0, x1] * tx) * (1 - ty) + (src[y1, x0] * (1 - tx) + src[y1, x1] * tx) * ty
    out[~valid] = np.nan
    return _rot_flip_mask(out.reshape(*out.shape[:2], *layer.shape[2:]), geom)


def max_area_rect(valid: np.ndarray) -> tuple[int, int, int, int]:
    """完全落在有效像素里、面积最大的轴对齐矩形，返回 (x, y, w, h)。"""
    height, width = valid.shape
    if height == 0 or width == 0 or not np.any(valid):
        return (0, 0, 0, 0)
    heights = np.zeros(width, np.int32)
    best = (0, 0, 0, 0)
    best_area = 0
    for i in range(height):
        heights = np.where(valid[i], heights + 1, 0)
        stack = [-1]
        for j in range(width + 1):
            cur = int(heights[j]) if j < width else 0
            while stack[-1] != -1 and int(heights[stack[-1]]) > cur:
                bar = int(heights[stack.pop()])
                left = stack[-1]
                rect_w = j - left - 1
                area = bar * rect_w
                if area > best_area:
                    best_area = area
                    best = (left + 1, i - bar + 1, rect_w, bar)
            stack.append(j)
    return best


def normalized_valid_rect(valid: np.ndarray) -> tuple[float, float, float, float]:
    height, width = valid.shape
    if height == 0 or width == 0:
        return (0.0, 0.0, 1.0, 1.0)
    if bool(valid.all()):
        return (0.0, 0.0, 1.0, 1.0)
    # 大图先缩到长边 240 再找最大矩形。缩略图上的框映回原图后收进 1 像素，避免边缘填色漏出来。
    long_edge = max(height, width)
    if long_edge > 240:
        scale = 240 / long_edge
        sh = max(2, int(round(height * scale)))
        sw = max(2, int(round(width * scale)))
        ys = np.linspace(0, height - 1, sh).astype(np.int32)
        xs = np.linspace(0, width - 1, sw).astype(np.int32)
        x, y, rw, rh = max_area_rect(valid[ys][:, xs])
        if rw <= 0 or rh <= 0:
            return (0.0, 0.0, 1.0, 1.0)
        x0 = int(x / sw * width) + 1
        y0 = int(y / sh * height) + 1
        x1 = max(x0 + 1, int((x + rw) / sw * width) - 1)
        y1 = max(y0 + 1, int((y + rh) / sh * height) - 1)
        return (x0 / width, y0 / height, (x1 - x0) / width, (y1 - y0) / height)
    x, y, rw, rh = max_area_rect(valid)
    if rw <= 0 or rh <= 0:
        return (0.0, 0.0, 1.0, 1.0)
    return (x / width, y / height, rw / width, rh / height)


def output_size(width: int, height: int, geom: Geometry) -> tuple[int, int]:
    """几何变换之后的画布像素。拉直不改变尺寸；旋转 90° 交换宽高；透视和畸变按真实边缘计算。"""
    if geom.is_identity() or not geom.expands():
        if geom.rotate90 % 2:
            return height, width
        return width, height
    _minx, _miny, ow, oh = _expanded_bounds(width, height, geom)
    if geom.rotate90 % 2:
        return oh, ow
    return ow, oh


def needs_valid_crop(geom: Geometry) -> bool:
    return abs(geom.straighten) > 1e-9 or geom.expands()


def header_value(rect: tuple[float, float, float, float]) -> str:
    return ",".join(f"{v:.4f}" for v in rect)


def _valid_integral(valid: np.ndarray) -> np.ndarray:
    total = np.cumsum(np.cumsum(valid.astype(np.int32), axis=0), axis=1)
    sheet = np.zeros((valid.shape[0] + 1, valid.shape[1] + 1), np.int32)
    sheet[1:, 1:] = total
    return sheet


def _covered(sheet: np.ndarray, x: int, y: int, rw: int, rh: int) -> bool:
    if rw < 1 or rh < 1:
        return False
    got = int(sheet[y + rh, x + rw] - sheet[y, x + rw] - sheet[y + rh, x] + sheet[y, x])
    return got == rw * rh


def _window_mask(sheet: np.ndarray, tw: int, th: int) -> np.ndarray | None:
    """每个左上角位置上，tw×th 的窗口是不是整窗都落在有效像素里（整张格子，一个位置都不漏）。
    积分图四个角相减，切片相减不用逐点取值，12 M 像素的画布也只要几十毫秒。放不下这个大小就返回空。"""
    height, width = sheet.shape[0] - 1, sheet.shape[1] - 1
    if tw < 1 or th < 1 or tw > width or th > height:
        return None
    got = sheet[th:, tw:] - sheet[: height - th + 1, tw:] - sheet[th:, : width - tw + 1] + sheet[: height - th + 1, : width - tw + 1]
    return np.asarray(got == tw * th)


def _has_window(sheet: np.ndarray, tw: int, th: int) -> bool:
    ok = _window_mask(sheet, tw, th)
    return ok is not None and bool(ok.any())


def _nearest_window(sheet: np.ndarray, tw: int, th: int, cx: float, cy: float) -> tuple[int, int, int, int] | None:
    """整窗都落在有效像素里、离 (cx, cy) 最近的 tw×th 窗口；一个都没有就返回空。
    目标的中心落在有效区外时（拉直、透视之后常见）用：把同样大小的框挪到最近的合格位置，而不是放大成整块有效区。
    位置一个不漏地找（不抽样）：16×16 的整窗只在 (1, 1) 一处也找得到。"""
    ok = _window_mask(sheet, tw, th)
    if ok is None:
        return None
    flat = np.flatnonzero(ok)
    if flat.size == 0:
        return None
    ys, xs = np.divmod(flat, ok.shape[1])
    dist = (xs + tw / 2.0 - cx) ** 2 + (ys + th / 2.0 - cy) ** 2
    best = int(np.argmin(dist))
    return int(xs[best]), int(ys[best]), tw, th


def _place_ratio(k: int, numer: int, denom: int, cx: float, cy: float, width: int, height: int) -> tuple[int, int, int, int] | None:
    tw, th = k * numer, k * denom
    if tw < 1 or th < 1 or tw > width or th > height:
        return None
    tx = int(round(cx - tw / 2.0))
    ty = int(round(cy - th / 2.0))
    tx = min(max(0, tx), width - tw)
    ty = min(max(0, ty), height - th)
    return tx, ty, tw, th


def _ratio_in_box(x: int, y: int, box_w: int, box_h: int, numer: int, denom: int, width: int, height: int) -> tuple[int, int, int, int]:
    """放不下一个整数倍时，在这块里按四舍五入取。1:1 仍相等，其余偏差不超过 1 像素。"""
    box_w = max(1, min(box_w, width - x))
    box_h = max(1, min(box_h, height - y))
    if numer == denom:
        side = min(box_w, box_h)
        return x, y, side, side
    tw = box_w
    th = max(1, int(round(tw * denom / numer)))
    if th > box_h:
        th = box_h
        tw = max(1, int(round(th * numer / denom)))
    return x, y, min(tw, box_w), min(th, box_h)


def fit_ratio_in_valid(
    valid: np.ndarray,
    rect: tuple[float, float, float, float],
    numer: int,
    denom: int,
) -> tuple[int, int, int, int]:
    """先取 k×numer、k×denom 的整数框，再缩小到整窗都落在有效像素里。

    1:1 的分子分母都是 1，所以宽高始终相等。其他比例是这个整数倍，偏差是 0。
    """
    height, width = valid.shape
    x, y, rw, rh = rect
    cx = (x + rw / 2.0) * width
    cy = (y + rh / 2.0) * height
    limit_w = min(width, max(1, int(round(rw * width))))
    limit_h = min(height, max(1, int(round(rh * height))))
    max_k = min(limit_w // numer, limit_h // denom) if numer > 0 and denom > 0 else 0
    sheet = _valid_integral(valid)
    centred: tuple[int, int, int, int] | None = None
    centred_k = 0
    for k in range(max(0, max_k), 0, -1):
        box = _place_ratio(k, numer, denom, cx, cy, width, height)
        if box is not None and _covered(sheet, *box):
            centred, centred_k = box, k
            break
    if centred is not None and centred_k == max_k:
        return centred
    # 目标中心处放不下目标那么大的框（中心靠近或落在有效区外）：同样的比例、尽量大的整数倍，挪到最近的放得下的位置。
    # 比中心处放得下的大，就用挪过去的（中心处只剩一个很小的框，比挪一下再保住大小更糟）；不比它大就保持中心。不超过目标的大小
    lo, hi, found_k = centred_k, max_k, 0
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _has_window(sheet, mid * numer, mid * denom):
            found_k, lo = mid, mid
        else:
            hi = mid - 1
    if found_k:
        return _nearest_window(sheet, found_k * numer, found_k * denom, cx, cy)  # type: ignore[return-value]
    if centred is not None:
        return centred
    fx, fy, fw, fh = max_area_rect(valid)
    fw = max(0, min(fw, width - fx))
    fh = max(0, min(fh, height - fy))
    k = min(fw // numer, fh // denom) if numer > 0 and denom > 0 and fw > 0 and fh > 0 else 0
    if k < 1:
        return _ratio_in_box(fx, fy, max(fw, 1), max(fh, 1), numer, denom, width, height)
    tw, th = k * numer, k * denom
    tx = int(round(cx - tw / 2.0))
    ty = int(round(cy - th / 2.0))
    tx = min(max(fx, tx), fx + fw - tw)
    ty = min(max(fy, ty), fy + fh - th)
    return tx, ty, tw, th


def fit_preset_in_valid(
    valid: np.ndarray,
    preset: str | None,
    rect: tuple[float, float, float, float],
) -> tuple[int, int, int, int] | None:
    """固定比例才取整数框。自由、原比例沿用原来的收框。

    横竖跟着传进来的框，不跟画布：界面可以把预设转 90°（横图上的 4:5 竖框），拉直以后写回、预览和导出
    都还是竖框。框是整张画布（没有裁切）时就是画布的方向，和原来一样。
    """
    if preset not in PRESETS or PRESETS[preset] is None:
        return None
    height, width = valid.shape
    ratio = target_ratio(preset, width, height)
    if ratio is None:
        return None
    _x, _y, rw, rh = rect
    if (rw * width >= rh * height) != (ratio >= 1):
        ratio = 1 / ratio
    return fit_ratio_in_valid(valid, rect, ratio.numerator, ratio.denominator)


def window_full(valid: np.ndarray, x: int, y: int, rw: int, rh: int) -> bool:
    if rw < 1 or rh < 1:
        return False
    height, width = valid.shape
    if x < 0 or y < 0 or x + rw > width or y + rh > height:
        return False
    return bool(valid[y : y + rh, x : x + rw].all())


def _snap_to_aspect(th: int, aspect: float, tolerance: float = 0.009, floor: int = 2) -> tuple[int, int]:
    """整数像素的 (宽, 高)，宽高比和 aspect 差不到 tolerance。短边在 16 像素上下时，四舍五入一下比例就差出 1% 以上，
    validate_crop 下一次就拒。先往小里找（不超过 th，不小于 floor），再往大里找；找不到就用四舍五入的。"""

    def fits(t: int) -> tuple[int, int] | None:
        w = int(round(t * aspect))
        return (w, t) if w >= 1 and abs(w / t - aspect) / aspect <= tolerance else None

    for t in range(th, max(floor, th - 40) - 1, -1):
        hit = fits(t)
        if hit:
            return hit
    for t in range(th + 1, th + 40):
        hit = fits(t)
        if hit:
            return hit
    return max(1, int(round(th * aspect))), th


def clamp_norm_rect(
    valid: np.ndarray, rect: tuple[float, float, float, float], aspect: float | None = None
) -> tuple[float, float, float, float]:
    """把归一化框收到有效区域内，保持宽高比，中心尽量不动。已经有效就原样返回。

    aspect：要的宽高比（像素）。给了就先取目标框里最大的、比例对的框（中心不动），再收；整数像素的宽高也按 aspect 对齐
    （差不到 0.9%）、短边不小于 16 像素；不给就沿用目标框自己的比例。"""
    height, width = valid.shape
    x, y, rw, rh = rect
    px, py = int(round(x * width)), int(round(y * height))
    pw, ph = max(1, int(round(rw * width))), max(1, int(round(rh * height)))
    min_h = max(2, math.ceil(16 * max(1.0, 1 / aspect))) if aspect is not None else 2
    if (
        aspect is not None and abs(pw / ph - aspect) / aspect > RATIO_TOLERANCE
    ):  # 比例差在 validate_crop 容许的 1% 以内就不动它（像素也不动）：存下来的框原样再写一遍不变。要改的才按 0.9% 取整
        cx0, cy0 = px + pw / 2.0, py + ph / 2.0
        bw = min(float(pw), ph * aspect)
        ph = max(min_h, int(round(bw / aspect)))
        pw, ph = _snap_to_aspect(ph, aspect, floor=min_h)
        px, py = int(round(cx0 - pw / 2.0)), int(round(cy0 - ph / 2.0))
        rect = (px / width, py / height, pw / width, ph / height)
    if window_full(valid, px, py, pw, ph):
        return rect
    cx = px + pw / 2.0
    cy = py + ph / 2.0
    aspect = pw / ph if aspect is None else aspect
    lo, hi = 0.05, 1.0
    best: tuple[int, int, int, int] | None = None
    for _ in range(18):
        mid = (lo + hi) / 2.0
        th = max(min_h, int(round(ph * mid)))
        tw = max(2, int(round(th * aspect)))
        if min_h > 2:
            tw, th = _snap_to_aspect(th, aspect, floor=min_h)
        tx = int(round(cx - tw / 2.0))
        ty = int(round(cy - th / 2.0))
        tx = min(max(0, tx), max(0, width - tw))
        ty = min(max(0, ty), max(0, height - th))
        if window_full(valid, tx, ty, tw, th):
            best = (tx, ty, tw, th)
            lo = mid
        else:
            hi = mid
    best_scale = lo if best is not None else 0.0
    if best is None or best_scale < 0.999:
        # 中心处缩了很多才放得下（或怎么缩都放不下，中心在有效区外）：同样比例的框，挪到最近的放得下的位置，
        # 比中心处缩出来的大就用挪过去的；不放大成整块有效区，也不超过目标
        sheet = _valid_integral(valid)
        lo2, hi2 = (best_scale if best is not None else 0.05), 1.0
        found_size: tuple[int, int] | None = None
        for _ in range(14):
            mid = (lo2 + hi2) / 2.0
            th = max(min_h, int(round(ph * mid)))
            tw = max(2, int(round(th * aspect)))
            if min_h > 2:
                tw, th = _snap_to_aspect(th, aspect, floor=min_h)
            if _has_window(sheet, tw, th):
                found_size = (tw, th)
                lo2 = mid
            else:
                hi2 = mid
        if found_size is not None:
            best = _nearest_window(sheet, found_size[0], found_size[1], cx, cy)
    if best is None:
        fx, fy, fw, fh = max_area_rect(valid)
        if fw < 1 or fh < 1:
            return (0.0, 0.0, 1.0, 1.0)
        return (fx / width, fy / height, fw / width, fh / height)
    tx, ty, tw, th = best
    return (tx / width, ty / height, tw / width, th / height)


def fitted_crop(
    geom: Geometry,
    rect: tuple[float, float, float, float] | None,
    *,
    preset: str | None = None,
    width: int | None = None,
    height: int | None = None,
    force: bool = False,
) -> tuple[float, float, float, float] | None:
    """做了拉直、透视或畸变时，返回应写回 sidecar 的归一化框。不需要收缩时返回空。
    force：框是别处推出来的目标（撤销、转 90° 之后的框），没有拉直透视也要按预设比例和画布收一遍。

    width、height 是纠正前的画面。给出尺寸时按这次 warp 的真实像素取整数比例，
    写回的归一化框和成片用的是同一块画布。
    """
    if not force and not needs_valid_crop(geom):
        return None
    if width is not None and height is not None and width > 0 and height > 0:
        valid = warp_valid(width, height, geom)
    else:
        valid = warp_valid(120, 90, geom)
    full = (0.0, 0.0, 1.0, 1.0)
    result = _fit_in_valid(valid, preset, full if rect is None else rect, rect)
    if rect is not None and rect != full and not _usable(valid, preset, result):
        # 位置不动地收下来，框小得没法用了（短边不到 16 像素，或者比例差出了 RATIO_TOLERANCE）：
        # 改取有效区里居中的最大框。只有收框收到不能用的时候才丢位置
        again = _fit_in_valid(valid, preset, full, None)
        if _usable(valid, preset, again):
            return again
    return result


def _fit_in_valid(
    valid: np.ndarray,
    preset: str | None,
    target: tuple[float, float, float, float],
    rect: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float]:
    full = (0.0, 0.0, 1.0, 1.0)
    if preset:
        box = fit_preset_in_valid(valid, preset, target)
        if box is not None:
            x, y, bw, bh = box
            vh, vw = valid.shape
            return (x / vw, y / vh, bw / vw, bh / vh)
    if preset == "original":
        # 原始：框的比例就是画布的比例（validate_crop 也这么要求），横竖跟着目标框；
        # 以前整张画布这一支取的是最大内接矩形，比例不对，下一次拖框就被拒（ratio_mismatch）
        vh, vw = valid.shape
        aspect = vw / vh
        if (target[2] * vw >= target[3] * vh) != (aspect >= 1):
            aspect = 1 / aspect
        return clamp_norm_rect(valid, target, aspect)
    if rect is None or rect == full:
        return normalized_valid_rect(valid)
    return clamp_norm_rect(valid, rect)


def _usable(valid: np.ndarray, preset: str | None, rect: tuple[float, float, float, float]) -> bool:
    """收出来的框能不能用：短边不小于 MIN_SIDE_PX，比例在 RATIO_TOLERANCE 以内（validate_crop 同一套）。"""
    vh, vw = valid.shape
    w_px, h_px = max(1, int(round(rect[2] * vw))), max(1, int(round(rect[3] * vh)))
    if min(w_px, h_px) < MIN_SIDE_PX:
        return False
    ratio = target_ratio(preset, vw, vh) if preset in PRESETS else None
    return ratio is None or ratio_off(ratio, w_px, h_px) <= RATIO_TOLERANCE


def crop_box(width: int, height: int, rect: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    """归一化矩形 → 像素框 (x, y, w, h)，取整与 `crop_valid` 相同（显卡预览按它取样）。"""
    x = min(max(0, int(round(rect[0] * width))), width - 1)
    y = min(max(0, int(round(rect[1] * height))), height - 1)
    rw = max(1, int(round(rect[2] * width)))
    rh = max(1, int(round(rect[3] * height)))
    return x, y, min(rw, width - x), min(rh, height - y)


def crop_valid(rgb: np.ndarray, rect: tuple[float, float, float, float]) -> np.ndarray:
    height, width = rgb.shape[:2]
    x, y, rw, rh = crop_box(width, height, rect)
    return rgb[y : y + rh, x : x + rw]
