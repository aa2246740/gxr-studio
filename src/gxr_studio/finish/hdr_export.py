"""导出 HDR：照片有编辑时，按成片重新算增益（research/hdr_gainmap/REPORT.md §6）。

底图（转正后的 SDR，相机 JPG 或 RAW 渲染）一定下来，就把同一张 DNG 的 RAW 亮度对齐到底图的全尺寸，作为一个浮点
数据层，跟着成片走同样的几何步骤：

- 裁剪、拉直、透视、畸变、旋转、翻转：对数据层做同样的变换（`geometry.warp_float`、同一个裁剪框）；
- 日期戳和边框：那里不是照片，数据层记为 NaN，增益为 0；
- 曝光、曲线、配方：不需要单独处理。增益按最终成片的"RAW → SDR"关系重新拟合，本来就会适应。

最后在导出文件（转正、方向为 1）上算增益图并封装；主图就是本来要写出的那份 JPEG，字节不变。
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from scipy import ndimage

from ..hdr.container import ContainerError
from ..hdr.gain import (
    HdrUnsupported,
    Params,
    align,
    block_mean,
    check_alignment,
    check_capture,
    compute_gain,
    package,
    raw_luma,
    register,
    sdr_luma_grid,
)
from ..raw import RawDecodeError, open_dng
from .frame import apply_frame
from .geometry import crop_valid, warp_float
from .spec import FrameSpec, Geometry, StampSpec
from .stamp import apply_stamp

NO_RAW = "no_raw"
NOT_JPEG = "not_jpeg"
_SOLIDS = (0, 255)


def _solid(shape: tuple[int, ...], value: int) -> np.ndarray:
    return np.full((shape[0], shape[1], 3), value, np.uint8)


@dataclass
class HdrTrack:
    """跟着成片走的 RAW 亮度层：(H, W, 2) float32，[RAW 亮度, 过曝]；NaN = 这里不是照片。做不了时 `problem` 是原因。"""

    dng: Path | None
    params: Params = Params()
    layer: np.ndarray | None = None
    problem: str | None = None
    jpeg: Path | None = None  # 原始配对 JPG，不能拿编辑后重写的 EXIF 核对拍摄信息

    @property
    def active(self) -> bool:
        return self.layer is not None

    def begin(self, base: np.ndarray) -> None:
        """底图定下来以后：读 DNG，对齐到底图，按底图全尺寸重采样。对不齐、读不出来就记下原因，导出照常（不带 HDR）。"""
        if self.dng is None:
            self.problem = NO_RAW
            return
        try:
            raw = raw_luma(open_dng(self.dng))
            if self.jpeg is not None:
                check_capture(self.jpeg.read_bytes(), raw)
            grid = sdr_luma_grid(base, self.params.map_factor)
            al = align(grid, raw.y)
            check_alignment(al)
            y, clip = register(raw, base.shape[:2], al, grid.shape)
        except HdrUnsupported as exc:
            self.problem = exc.reason
            return
        except RawDecodeError:
            self.problem = "raw_unreadable"
            return
        self.layer = np.dstack([y, clip.astype(np.float32)])

    def warp(self, geom: Geometry) -> None:
        if self.layer is not None:
            self.layer = warp_float(self.layer, geom)

    def box(self, x: int, y: int, w: int, h: int) -> None:
        if self.layer is not None:
            self.layer = self.layer[y : y + h, x : x + w]

    def crop(self, rect: tuple[float, float, float, float]) -> None:
        if self.layer is not None:
            self.layer = crop_valid(self.layer, rect)

    def stamp(self, spec: StampSpec, taken: datetime | None, file_time: datetime | None) -> None:
        """日期戳盖住的地方（含发光和抗锯齿边，再外扩 2 像素）不提亮。两张纯色图上各画一次，变了的像素就是日期戳。"""
        if self.layer is None or not spec.enabled:
            return
        mask = np.zeros(self.layer.shape[:2], bool)
        for value in _SOLIDS:
            solid = _solid(self.layer.shape, value)
            mask |= (apply_stamp(solid, spec, taken, file_time) != solid).any(axis=2)
        if mask.any():
            self.layer = self.layer.copy()
            self.layer[ndimage.binary_dilation(mask, iterations=2)] = np.nan

    def frame(self, spec: FrameSpec, meta: dict[str, str]) -> None:
        """边框把照片原样贴在画布里：两张纯色图各加一次边框，不一样的那块就是照片的位置，其余记为 NaN。"""
        if self.layer is None or spec.style == "none":
            return
        h, w = self.layer.shape[:2]
        outs = [apply_frame(_solid(self.layer.shape, v), spec, meta) for v in _SOLIDS]
        diff = (outs[0] != outs[1]).any(axis=2)
        ys, xs = np.nonzero(diff)
        if ys.size == 0 or (ys.max() - ys.min() + 1, xs.max() - xs.min() + 1) != (h, w):
            self.layer, self.problem = None, "frame_layout"
            return
        canvas = np.full((*outs[0].shape[:2], 2), np.nan, np.float32)
        canvas[ys.min() : ys.min() + h, xs.min() : xs.min() + w] = self.layer
        self.layer = canvas


def _to_grid(layer: np.ndarray, size: tuple[int, int], factor: int) -> tuple[np.ndarray, np.ndarray]:
    """数据层 → 导出尺寸 `size`（宽, 高）→ 增益图网格。块里有一点不是照片，整块算 NaN；有一点过曝就算过曝。"""
    weight = np.isfinite(layer[..., 0]).astype(np.float32)
    planes = [np.where(weight > 0, layer[..., k], 0.0).astype(np.float32) for k in range(2)] + [weight]
    if (layer.shape[1], layer.shape[0]) != size:
        planes = [np.asarray(Image.fromarray(p, "F").resize(size, Image.Resampling.BOX)) for p in planes]
    y, clip, w = (block_mean(p, factor) for p in planes)
    photo = w > 0.999
    raw_y = np.where(photo, y / np.maximum(w, 1e-6), np.nan).astype(np.float32)
    return raw_y, (clip / np.maximum(w, 1e-6)) > 0.01


def finish(payload: bytes, track: HdrTrack, fmt: str) -> tuple[bytes, dict[str, Any]]:
    """导出文件 + 跟到最后的数据层 → (HDR JPEG, 说明)。做不了就原样返回 `payload`，说明里写原因。"""
    if fmt != "jpeg":
        return payload, {"hdr": False, "reason": NOT_JPEG}
    if track.layer is None:
        return payload, {"hdr": False, "reason": track.problem or NO_RAW}
    with Image.open(io.BytesIO(payload)) as im:
        rgb = np.asarray(im.convert("RGB"))
    p = track.params
    raw_y, clip = _to_grid(track.layer, (rgb.shape[1], rgb.shape[0]), p.map_factor)
    try:
        result = compute_gain(sdr_luma_grid(rgb, p.map_factor), raw_y, clip, p)
        out = package(payload, result, p)
    except HdrUnsupported as exc:
        return payload, {"hdr": False, "reason": exc.reason}
    except ContainerError:
        return payload, {"hdr": False, "reason": "container"}
    return out, {"hdr": True, "gain_max_stops": result.stats["gain_max_stops"]}
