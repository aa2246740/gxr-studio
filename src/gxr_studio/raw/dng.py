"""只读 DNG 读取：GXR 的 CFA 原始平面、黑/白电平、CFA 排列、ActiveArea、DefaultCrop、AsShotNeutral。

来源：research/color_table_fit/dng_scene.py 的 `Dng`（SHA-256 见 PROVENANCE.json）。保留了来源的字段、
取值口径和 `normalised_at`（逐点取值，等价测试用它做对照）；改动只有：

- `Tiff` 用本包自带的那份，不再按路径加载；
- 去掉色彩学（`DngColour`、白点、ROI 映射）和自检，这些不在渲染链里；
- 新增整块读取 `normalised_block`（向量化，与 `normalised_at` 逐点相同的算式）；
- `open_dng` 把各种解析失败统一成 `RawDecodeError`，调用方只需要接一个异常。

只支持 GXR 的写法：未压缩、非分块、2×2 Bayer、3 个颜色平面。其它写法抛 `RawDecodeError`
（ARCHITECTURE U10：降级为"只有配对 JPG 可用"）。文件只以 `rb` 读，不写。
"""

from __future__ import annotations

import hashlib
import math
import struct
from pathlib import Path
from typing import Any, cast

import numpy as np

from .tiff import Tiff


class RawDecodeError(Exception):
    """这份 RAW 读不出来（不是 DNG、写法不支持、文件被截断…）。"""


class Dng:
    """Read-only DNG: raw CFA plane and the tags the render chain needs."""

    def __init__(self, source: bytes | Path | str, name: str | None = None) -> None:
        if isinstance(source, (bytes, bytearray)):
            self.path = Path(name or "<bytes>")
            self.data = bytes(source)
        else:
            self.path = Path(source)
            self.data = self.path.read_bytes()
        self.sha256 = hashlib.sha256(self.data).hexdigest()
        T = Tiff(self.data)
        self.endian = "big" if T.endian == ">" else "little"
        root = T.ifd(T.root)
        subs = [T.ifd(o) for o in root.get(330, {}).get("value", [])]

        def val(d: Any, tag: Any, default: Any = None) -> Any:
            return d[tag]["value"] if tag in d else default

        raw = None
        for d in [root] + subs:
            if val(d, 262, [None])[0] == 32803 and val(d, 254, [0])[0] == 0:
                raw = d
                break
        if raw is None:
            raise ValueError(f"{self.path.name}: no full-resolution CFA IFD")
        self.width, self.height = int(val(raw, 256)[0]), int(val(raw, 257)[0])
        self.bps = int(val(raw, 258)[0])
        if val(raw, 259, [1])[0] != 1:
            raise NotImplementedError(f"compression {val(raw, 259)}")
        if val(raw, 277, [1])[0] != 1:
            raise NotImplementedError("SamplesPerPixel != 1 (not a CFA image)")
        if val(raw, 266, [1])[0] != 1:
            raise NotImplementedError("FillOrder 2")
        if 322 in raw or 324 in raw:
            raise NotImplementedError("tiled raw")
        self._strips = (list(val(raw, 273)), list(val(raw, 279)))
        dim = [int(v) for v in val(raw, 33421, [2, 2])]
        pat = val(raw, 33422)
        pat = list(pat) if isinstance(pat, (bytes, bytearray)) else [int(v) for v in pat]
        self.cfa = np.array(pat, int).reshape(dim)
        plane = val(raw, 50710, [0, 1, 2])
        plane = list(plane)
        if plane != [0, 1, 2] or sorted(set(self.cfa.ravel().tolist())) != [0, 1, 2]:
            raise NotImplementedError(f"CFA colours {plane} / pattern {pat}")
        if val(raw, 50711, [1])[0] != 1:
            raise NotImplementedError("non-rectangular CFALayout")
        self.lin_table = None
        if 50712 in raw:
            # DNG 规定 LinearizationTable 是 SHORT：别的类型（例如有理数，可能是 0/0 → NaN）一律拒绝
            if raw[50712]["type"] != 3:
                raise ValueError("LinearizationTable must be SHORT")
            self.lin_table = np.asarray(val(raw, 50712), dtype=np.float64)
        aa = val(raw, 50829, [0, 0, self.height, self.width])
        self.active = tuple(int(round(v)) for v in aa)  # top, left, bottom, right
        aw, ah = self.active[3] - self.active[1], self.active[2] - self.active[0]
        bdim = [int(v) for v in val(raw, 50713, [1, 1])]
        bl = np.asarray(val(raw, 50714, [0.0]), dtype=np.float64)
        if bl.size == 1:
            bl = np.full(bdim[0] * bdim[1], bl[0])
        if bl.size != bdim[0] * bdim[1]:
            raise NotImplementedError(f"BlackLevel count {bl.size} for repeat {bdim}")
        self.black_pattern = bl.reshape(bdim)
        self.black_dh = np.asarray(val(raw, 50715), dtype=np.float64) if 50715 in raw else None
        self.black_dv = np.asarray(val(raw, 50716), dtype=np.float64) if 50716 in raw else None
        if self.black_dh is not None and self.black_dh.size != aw:
            raise ValueError("BlackLevelDeltaH length")
        if self.black_dv is not None and self.black_dv.size != ah:
            raise ValueError("BlackLevelDeltaV length")
        self.white = float(val(raw, 50717, [(1 << self.bps) - 1])[0])
        self.default_scale = tuple(float(v) for v in val(raw, 50718, [1.0, 1.0]))
        co = [float(v) for v in val(raw, 50719, [0, 0])]
        cs = [float(v) for v in val(raw, 50720, [aw, ah])]
        if any(abs(v - round(v)) > 1e-9 for v in co + cs):
            raise NotImplementedError(f"fractional default crop {co} {cs}")
        self.crop_origin = (int(round(co[0])), int(round(co[1])))  # x, y relative to the ActiveArea
        self.crop_size = (int(round(cs[0])), int(round(cs[1])))  # w, h
        self.orientation = int(val(root, 274, [1])[0])
        self.as_shot_neutral = np.asarray(val(root, 50728), dtype=np.float64) if 50728 in root else None
        self.as_shot_white_xy = np.asarray(val(root, 50729), dtype=np.float64) if 50729 in root else None
        self.analog_balance = np.asarray(val(root, 50727, [1.0, 1.0, 1.0]), dtype=np.float64)
        self.baseline_exposure = float(val(root, 50730, [0.0])[0])
        self.exif = {}
        if 34665 in root:
            try:
                ex = T.ifd(root[34665]["value"][0])
                for tag, name in (
                    (34855, "iso"),
                    (33434, "t"),
                    (33437, "f"),
                    (36867, "when"),
                    (41987, "wb_exif"),
                    (37383, "metering"),
                    (34850, "program"),
                    (37380, "bias_ev"),
                ):
                    if tag in ex:
                        v = ex[tag]["value"]
                        v = v[0] if isinstance(v, list) and len(v) == 1 else v
                        # 分母为 0 的有理数（MOUNT 装手动镜头）是"未知"：存 None，不让 NaN 进 JSON
                        self.exif[name] = None if isinstance(v, float) and not math.isfinite(v) else v
            except Exception as e:  # noqa: BLE001
                self.exif["error"] = str(e)
        self._check_finite()
        self._check_levels()
        self._check_window()
        self._mosaic: np.ndarray | None = None

    def _check_finite(self) -> None:
        # 渲染要用的有理数标签不能是"未知"（分母为 0 读成 NaN），否则像素会悄悄变成 NaN
        for name, v in (
            ("BlackLevel", self.black_pattern),
            ("BlackLevelDeltaH", self.black_dh),
            ("BlackLevelDeltaV", self.black_dv),
            ("WhiteLevel", self.white),
            ("DefaultScale", self.default_scale),
            ("LinearizationTable", self.lin_table),
            ("AnalogBalance", self.analog_balance),
            ("BaselineExposure", self.baseline_exposure),
        ):
            if v is not None and not np.all(np.isfinite(np.asarray(v, dtype=np.float64))):
                raise ValueError(f"{name} has a zero denominator")

    def _check_levels(self) -> None:
        """白电平必须高于任何位置的黑电平（含逐行/逐列偏移），否则归一化时分母 ≤ 0，会算出 NaN 或无穷大。"""
        black = float(np.max(self.black_pattern))
        black += float(np.max(self.black_dh)) if self.black_dh is not None and self.black_dh.size else 0.0
        black += float(np.max(self.black_dv)) if self.black_dv is not None and self.black_dv.size else 0.0
        if not self.white > black:
            raise ValueError(f"WhiteLevel {self.white} must be greater than BlackLevel {black}")

    def _check_window(self) -> None:
        top, left, bottom, right = self.active
        if not (0 <= top < bottom <= self.height and 0 <= left < right <= self.width):
            raise ValueError(f"ActiveArea {self.active} outside the {self.width}x{self.height} raw plane")
        y0, x0, h, w = self.crop_window()
        if h <= 0 or w <= 0 or y0 + h > bottom or x0 + w > right:
            raise ValueError(f"DefaultCrop outside the ActiveArea: {(y0, x0, h, w)}")
        if self.cfa.shape != (2, 2):
            raise NotImplementedError(f"CFA repeat {self.cfa.shape}")

    # --- raw plane
    def mosaic(self) -> np.ndarray:
        """Full raw CFA plane (height x width), uint16, before linearization."""
        if self._mosaic is None:
            try:
                self._mosaic = self._decode_mosaic()
            except (ValueError, IndexError) as exc:
                raise RawDecodeError(f"RAW 数据读不全（{exc}）") from exc
        mosaic = self._mosaic
        if mosaic is None:
            raise RawDecodeError("RAW 数据读不全")
        return mosaic

    def _decode_mosaic(self) -> np.ndarray:
        offs, cnts = self._strips
        buf = np.concatenate([np.frombuffer(self.data, np.uint8, count=int(n), offset=int(o)) for o, n in zip(offs, cnts)])
        h, w, b = self.height, self.width, self.bps
        row = (w * b + 7) // 8
        if buf.size < row * h:
            raise ValueError("raw strips shorter than the image")
        rows = buf[: row * h].reshape(h, row)
        if b == 16:
            m = rows.reshape(-1).view(">u2" if self.endian == "big" else "<u2").reshape(h, w).astype(np.uint16)
        elif b == 8:
            m = rows[:, :w].astype(np.uint16)
        elif b == 12:
            n2 = w // 2
            a = rows[:, : 3 * n2].reshape(h, n2, 3).astype(np.uint16)
            m = np.empty((h, w), np.uint16)
            m[:, 0 : 2 * n2 : 2] = (a[..., 0] << 4) | (a[..., 1] >> 4)
            m[:, 1 : 2 * n2 : 2] = ((a[..., 1] & 15) << 8) | a[..., 2]
            if w % 2:
                t = rows[:, 3 * n2 : 3 * n2 + 2].astype(np.uint16)
                m[:, -1] = (t[:, 0] << 4) | (t[:, 1] >> 4)
        else:
            bits = np.unpackbits(rows, axis=1)[:, : w * b].reshape(h, w, b)
            m = (bits.astype(np.uint32) << np.arange(b - 1, -1, -1, dtype=np.uint32)).sum(-1).astype(np.uint16)
        return m

    def release(self) -> None:
        """丢掉解出来的原始平面（文件字节还在）；大批量处理时用。"""
        self._mosaic = None

    def crop_window(self) -> tuple[Any, ...]:
        """(y0, x0, h, w) of the default crop in raw-plane coordinates."""
        return (
            self.active[0] + self.crop_origin[1],
            self.active[1] + self.crop_origin[0],
            self.crop_size[1],
            self.crop_size[0],
        )

    def crop_mosaic(self) -> Any:
        y0, x0, h, w = self.crop_window()
        return self.mosaic()[y0 : y0 + h, x0 : x0 + w]

    def cfa_at(self, ys: Any, xs: Any) -> Any:
        """CFA colour (0 R, 1 G, 2 B) at raw-plane coordinates; pattern origin = ActiveArea origin."""
        r, c = self.cfa.shape
        return self.cfa[(np.asarray(ys) - self.active[0]) % r, (np.asarray(xs) - self.active[1]) % c]

    def black_at(self, ys: Any, xs: Any) -> Any:
        ry, rx = np.asarray(ys) - self.active[0], np.asarray(xs) - self.active[1]
        bp = self.black_pattern
        b = bp[ry % bp.shape[0], rx % bp.shape[1]].astype(float)
        if self.black_dh is not None:
            b = b + self.black_dh[rx]
        if self.black_dv is not None:
            b = b + self.black_dv[ry]
        return b

    def linearize(self, v: Any) -> Any:
        v = np.asarray(v)
        if self.lin_table is None:
            return v.astype(float)
        return self.lin_table[np.minimum(v.astype(np.int64), len(self.lin_table) - 1)]

    def normalised_at(self, ys: Any, xs: Any) -> Any:
        """(linearized value - black) / (white - black) at raw-plane coordinates (DNG chapter 5)."""
        black = self.black_at(ys, xs)
        return (self.linearize(self.mosaic()[ys, xs]) - black) / (self.white - black)

    def normalised_block(self, y0: int, y1: int, x0: int, x1: int, dtype: Any = np.float64) -> np.ndarray:
        """`normalised_at` over the raw-plane rectangle [y0, y1) x [x0, x1), computed in float64."""
        ys = np.arange(y0, y1)[:, None]
        xs = np.arange(x0, x1)[None, :]
        black = self.black_at(ys, xs)
        v = self.linearize(self.mosaic()[y0:y1, x0:x1])
        with np.errstate(all="ignore"):  # 结果下面整体核对
            out = ((v - black) / (self.white - black)).astype(dtype, copy=False)
        require_finite(out, "归一化码值")
        return cast(np.ndarray, out)

    def crop_cfa_phase(self) -> Any:
        """2x2 colour codes of the crop's first quad (row-major), e.g. [[1, 2], [0, 1]] = G/B/R/G."""
        y0, x0, _, _ = self.crop_window()
        yy, xx = np.meshgrid(np.arange(y0, y0 + 2), np.arange(x0, x0 + 2), indexing="ij")
        return self.cfa_at(yy, xx)

    def summary(self) -> dict[Any, Any]:
        return {
            "file": self.path.name,
            "sha256": self.sha256,
            "endian": self.endian,
            "raw_size_wh": [self.width, self.height],
            "bits": self.bps,
            "cfa_pattern": self.cfa.tolist(),
            "crop_cfa_phase": self.crop_cfa_phase().tolist(),
            "active_area_tlbr": list(self.active),
            "default_crop_origin_xy": list(self.crop_origin),
            "default_crop_size_wh": list(self.crop_size),
            "default_scale": list(self.default_scale),
            "orientation": self.orientation,
            "linearization_table": None if self.lin_table is None else int(self.lin_table.size),
            "black_level_pattern": self.black_pattern.tolist(),
            "black_delta_h": self.black_dh is not None,
            "black_delta_v": self.black_dv is not None,
            "white_level": self.white,
            "as_shot_neutral": None if self.as_shot_neutral is None else self.as_shot_neutral.tolist(),
            "as_shot_white_xy": None if self.as_shot_white_xy is None else self.as_shot_white_xy.tolist(),
            "analog_balance": self.analog_balance.tolist(),
            "baseline_exposure_ev": self.baseline_exposure,
            "exif": self.exif,
        }


def require_finite(a: Any, what: str) -> None:
    """渲染路径上的计算结果必须全部是有限值：有 NaN 或无穷大就明确报错，不能悄悄变成错误的像素。"""
    if not np.all(np.isfinite(a)):
        raise RawDecodeError(f"这张 RAW 的{what}无效，不能显示")


def open_dng(source: bytes | Path | str, name: str | None = None) -> Dng:
    """读 DNG；任何解析失败都变成 `RawDecodeError`（含中文白话原因）。"""
    try:
        return Dng(source, name)
    except RawDecodeError:
        raise
    except OSError as exc:
        raise RawDecodeError(f"读不了这个文件（{exc.strerror or exc}）") from exc
    except NotImplementedError as exc:
        raise RawDecodeError(f"这份 RAW 的写法暂不支持（{exc}）") from exc
    except (ValueError, KeyError, IndexError, TypeError, struct.error, ZeroDivisionError, AttributeError) as exc:
        raise RawDecodeError(f"不是可识别的 GXR DNG（{type(exc).__name__}: {exc}）") from exc
