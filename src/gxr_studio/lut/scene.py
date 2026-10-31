"""拟合用的色样：合成网格（灰阶、肤色、常见色块）+ 图库照片抽样，以及 LUT 在这些色样上的"理想输出"（ARCHITECTURE §6.4）。

口径（与 research/lut_convert 一致）：
- 色样是**白平衡后的传感器码值**（0..4095，渲染链的输入域，见 raw/codes.py）；
- 基准是原厂 04 的电脑模拟 `Model.predict(stock04, 码值)`（sRGB 0..255）；
- 目标 = `spaces.Target.render(基准, 场景 XYZ)`：显示域输入（sRGB / Rec.709 / Gamma 2.4）的 LUT 直接套在 04 的输出上；
  Log 输入需要场景光的 XYZ，由 DNG 自带的色彩矩阵、AsShotNeutral 和固定的中灰锚（`ANCHOR`）算出，不从 JPEG 猜。
没有 DNG（图库里没有照片）时，合成色样没有场景 XYZ，Log 输入的 LUT 不能拟合（显示域输入不受影响）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import numpy as np

from ..engine import colour
from ..engine.model import synthetic
from ..raw.codes import CODE_MAX
from ..raw.tiff import Tiff
from . import colorimetry
from .spaces import DISPLAY

# 固定的中灰锚（50mm 的测光标定；research/um_measurement/REPORT.md，lut_convert/camera.py 的 anchor）。
ANCHOR = {"u_m": 0.10250885, "f": -0.0022470695785486096}
CLIP_CODE = 4000  # 照片抽样时丢掉任一通道接近满码的像素
GROUP_NAMES = ("灰阶", "肤色", "常见色块", "照片")
GROUP_WEIGHT = (1.5, 2.0, 1.0, 1.0)


class SceneError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class SceneColour:
    """一个 DNG 的色彩：AsShotNeutral + 相机→XYZ(D65) 矩阵。`xyz` 与 lut_convert 的 `Camera.xyz` 逐式相同。"""

    neutral: np.ndarray
    matrix: np.ndarray

    def xyz(self, codes: np.ndarray) -> np.ndarray:
        u, f = ANCHOR["u_m"], ANCHOR["f"]
        white = self.neutral @ self.matrix.T
        with np.errstate(all="ignore"):  # 结果下面整体核对
            scale = 0.18 / ((u - f) * white[1])
            raw = (np.asarray(codes, float) / CODE_MAX - f) * self.neutral
            out = raw @ self.matrix.T * scale
        if not np.all(np.isfinite(out)):
            raise SceneError("bad_colour", "这张 RAW 的颜色换算出现无效值，不能用来换算 Log 调色文件")
        return cast(np.ndarray, out)


def scene_colour(dng: Any) -> SceneColour:
    """从 `raw.Dng` 读色彩矩阵标签并算出相机→XYZ；缺标签时抛 SceneError。"""
    neutral = None if dng.as_shot_neutral is None else np.asarray(dng.as_shot_neutral, float)
    if neutral is None or not np.all(np.isfinite(neutral)) or np.any(neutral <= 0):
        raise SceneError("no_neutral", "这张 RAW 没有白平衡信息，不能用来换算 Log 调色文件")
    t = Tiff(dng.data)
    root = t.ifd(t.root)

    def val(tag: Any, default: Any = None) -> Any:
        return root[tag]["value"] if tag in root else default

    def mat(tag: Any) -> Any:
        """标签不存在是"没有"；存在但个数不对、或有分母为 0 的值（读成 NaN），是坏的，必须拒绝，不能悄悄当作没有。"""
        v = val(tag)
        if v is None:
            return None
        m = np.asarray(v, float)
        if m.size != 9 or not np.all(np.isfinite(m)):
            raise SceneError("bad_matrix", "这张 RAW 的色彩矩阵无效，不能用来换算 Log 调色文件")
        return m.reshape(3, 3)

    from types import SimpleNamespace

    tags = SimpleNamespace(
        color_matrix=[mat(50721), mat(50722)],
        camera_calibration=[mat(50723), mat(50724)],
        forward_matrix=[mat(50964), mat(50965)],
        illuminant=[int(val(50778, [0])[0]), int(val(50779, [0])[0])],
        analog_balance=np.asarray(val(50727, [1.0, 1.0, 1.0]), float),
    )
    if all(m is None for m in tags.color_matrix):
        raise SceneError("no_matrix", "这张 RAW 没有色彩矩阵，不能用来换算 Log 调色文件")
    # 标签本身合格还不够（复审 A1a R2-P1-05）：换算出来的白点、矩阵、白点亮度也要逐项核对
    bad = SceneError("bad_matrix", "这张 RAW 的色彩矩阵无效，不能用来换算 Log 调色文件")
    try:
        with np.errstate(all="ignore"):
            col = colorimetry.DngColour(tags)
            white, _ = colorimetry.white_from_neutral(col, neutral)
            matrix = np.asarray(col.camera_to_xyz(white), float)
            white_y = float((neutral @ matrix.T)[1])
    except (np.linalg.LinAlgError, ZeroDivisionError, FloatingPointError, ValueError, IndexError) as exc:
        raise bad from exc
    if not (np.all(np.isfinite(white)) and np.all(np.isfinite(matrix)) and matrix.shape == (3, 3)):
        raise bad
    if not (np.isfinite(white_y) and white_y > 0):
        raise bad
    return SceneColour(neutral, matrix)


@dataclass
class Samples:
    x: np.ndarray  # (n, 3) 码值
    xyz: np.ndarray | None  # (n, 3) 场景 XYZ；没有时为 None
    weight: np.ndarray  # (n,)
    group: np.ndarray  # (n,) 0 灰阶 1 肤色 2 常见色块 3 照片
    holdout: np.ndarray  # (n,) bool：只验收、不参与搜索
    photos: list[Any] = field(default_factory=list)  # 抽样用到的照片说明

    def __len__(self) -> int:
        return len(self.x)

    def counts(self) -> dict[Any, Any]:
        return {
            "total": len(self),
            "train": int((~self.holdout).sum()),
            "holdout": int(self.holdout.sum()),
            "groups": {GROUP_NAMES[g]: int((self.group == g).sum()) for g in range(4)},
        }


def _hue_cloud(model: Any, stock04: bytes, n: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """一团确定性的随机码值（对数均匀）和它们在 04 下的 Lab，用来挑肤色、天空、植物这些"常见色"。"""
    rng = np.random.default_rng(seed)
    # 每个像素的三通道不要差得太离谱：让它们围绕一个亮度做对数偏移
    base = np.exp(rng.uniform(np.log(60), np.log(3300), (n, 1)))
    x = np.clip(base * np.exp(rng.normal(0, 0.45, (n, 3))), 12, 4000)
    lab = colour.lab_from_codes(model.predict(stock04, x))
    return x, lab


def _pick(x: Any, lab: Any, lo_h: Any, hi_h: Any, c_lo: Any, c_hi: Any, l_lo: Any, l_hi: Any, count: Any, rng: Any) -> Any:
    h = np.degrees(np.arctan2(lab[:, 2], lab[:, 1])) % 360
    c = np.hypot(lab[:, 1], lab[:, 2])
    ok = np.flatnonzero((h >= lo_h) & (h <= hi_h) & (c >= c_lo) & (c <= c_hi) & (lab[:, 0] >= l_lo) & (lab[:, 0] <= l_hi))
    if len(ok) > count:
        ok = rng.choice(ok, count, replace=False)
    return x[np.sort(ok)]


def synthetic_samples(model: Any, stock04: bytes, seed: int = 20261030) -> Samples:
    """灰阶 + 肤色 + 常见色块（天空、植物、高彩度）+ 对数网格。"""
    syn = synthetic()  # 257 个灰阶 + 9³ 个网格（E0 冻结的确定性色样）
    ramp, grid = syn[:257], syn[257:]
    cx, clab = _hue_cloud(model, stock04, 24000, seed)
    rng = np.random.default_rng(seed + 1)
    skin = _pick(cx, clab, 20, 60, 12, 45, 35, 85, 160, rng)
    sky = _pick(cx, clab, 225, 270, 12, 45, 35, 85, 50, rng)
    leaf = _pick(cx, clab, 95, 150, 12, 55, 25, 80, 50, rng)
    vivid = _pick(cx, clab, 0, 360, 55, 130, 25, 85, 120, rng)
    parts = [(ramp, 0), (skin, 1), (grid, 2), (sky, 2), (leaf, 2), (vivid, 2)]
    x = np.vstack([p for p, _ in parts])
    group = np.concatenate([np.full(len(p), g, np.int8) for p, g in parts])
    # 每组内按顺序隔 4 个取 1 个做留出：灰阶/肤色/色块都有，且与搜索用的样本交错
    holdout = np.zeros(len(x), bool)
    start = 0
    for p, _ in parts:
        holdout[start + 3 : start + len(p) : 4] = True
        start += len(p)
    return Samples(x, None, np.array([GROUP_WEIGHT[g] for g in group]), group, holdout)


def photo_samples(codes: np.ndarray, n: int = 400) -> np.ndarray:
    """(h, w, 3) 码值图 → 均匀网格抽出约 n 个有效像素（丢掉接近满码的）。"""
    h, w = codes.shape[:2]
    step = max(1, int(np.sqrt(h * w / (n * 1.25))))
    px = codes[step // 2 :: step, step // 2 :: step].reshape(-1, 3).astype(np.float64)
    px = px[(px.max(axis=1) < CLIP_CODE) & (px.max(axis=1) > 8)]
    if len(px) > n:
        px = px[np.linspace(0, len(px) - 1, n).astype(int)]
    return cast(np.ndarray, px)


def build_samples(model: Any, stock04: bytes, input_space: str, photos: list[dict[Any, Any]] | None = None, seed: int = 20261030) -> Samples:
    """photos: [{"codes": (h,w,3), "colour": SceneColour|None, "label": str}]，最后一张只做验收（至少两张时）。"""
    s = synthetic_samples(model, stock04, seed)
    photos = photos or []
    ref = next((p["colour"] for p in photos if p.get("colour") is not None), None)
    need_xyz = input_space not in DISPLAY
    if need_xyz and ref is None:
        raise SceneError(
            "needs_photo",
            "这个调色文件是 Log 格式，换算需要图库里至少一张 50mm 的 RAW 照片（用它的色彩矩阵）。"
            "请先导入照片，或者换一个 sRGB / Rec.709 格式的调色文件。",
        )
    xs, groups, hold, labels = [s.x], [s.group], [s.holdout], []
    xyz = [ref.xyz(s.x)] if need_xyz else []  # type: ignore[union-attr]
    for i, p in enumerate(photos):
        px = photo_samples(p["codes"])
        if not len(px):
            continue
        last_only = len(photos) >= 2 and i == len(photos) - 1
        h = np.ones(len(px), bool) if last_only else (np.arange(len(px)) % 4 == 3)
        xs.append(px)
        groups.append(np.full(len(px), 3, np.int8))
        hold.append(h)
        if need_xyz:
            c = p.get("colour") or ref
            xyz.append(c.xyz(px))  # type: ignore[union-attr]
        labels.append({"label": p.get("label"), "samples": int(len(px)), "holdout_only": bool(last_only)})
    x = np.vstack(xs)
    group = np.concatenate(groups)
    return Samples(
        x,
        np.vstack(xyz) if need_xyz else None,
        np.array([GROUP_WEIGHT[g] for g in group]),
        group,
        np.concatenate(hold),
        labels,
    )


def target_rgb(model: Any, stock04: bytes, samples: Samples, target: Any) -> tuple[np.ndarray, np.ndarray]:
    """(04 的电脑模拟, LUT 的理想输出)，都是 (n, 3) 的 sRGB 0..255 浮点。target 要有 `render(stock_rgb, xyz)`。"""
    stock = model.predict(stock04, samples.x)
    ideal = target.render(stock, samples.xyz)
    return stock, np.asarray(ideal, float)
