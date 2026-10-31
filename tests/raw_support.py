"""合成 GXR 风格 DNG（大端 TIFF：IFD0 + EXIF 子目录 + 一个带 CFA 的 SubIFD），测试用。

只用来造"结构正确"的小文件：未压缩、单条带、2×2 Bayer；不含任何理光字节。
"""

from __future__ import annotations

import struct

import numpy as np

# TIFF 类型 -> (代码, 单个字节数)
_T = {
    "BYTE": (1, 1),
    "ASCII": (2, 1),
    "SHORT": (3, 2),
    "LONG": (4, 4),
    "RATIONAL": (5, 8),
    "UNDEFINED": (7, 1),
    "SRATIONAL": (10, 8),
}


def _pack_values(kind: str, values) -> bytes:
    if kind == "ASCII":
        return values.encode("ascii") + b"\0"
    if kind in ("BYTE", "UNDEFINED"):
        return bytes(values)
    if kind == "SHORT":
        return struct.pack(">%dH" % len(values), *values)
    if kind == "LONG":
        return struct.pack(">%dI" % len(values), *values)
    if kind in ("RATIONAL", "SRATIONAL"):
        fmt = ">%dI" if kind == "RATIONAL" else ">%di"
        out = []
        for v in values:
            if isinstance(v, tuple):  # (分子, 分母) 原样写入，可以造分母为 0 的值
                out += list(v)
                continue
            den = 1_000_000
            out += [int(round(v * den)), den]
        return struct.pack(fmt % len(out), *out)
    raise ValueError(kind)


class _Buf:
    def __init__(self):
        self.b = bytearray(b"MM\x00*\x00\x00\x00\x00")

    def blob(self, data: bytes) -> int:
        if len(self.b) % 2:
            self.b.append(0)
        off = len(self.b)
        self.b += data
        return off

    def ifd(self, entries) -> int:
        """entries: [(tag, kind, values)]；返回 IFD 偏移。"""
        entries = sorted(entries, key=lambda e: e[0])
        if len(self.b) % 2:
            self.b.append(0)
        off = len(self.b)
        size = 2 + 12 * len(entries) + 4
        self.b += b"\0" * size
        data_pos = len(self.b)
        rows = bytearray(struct.pack(">H", len(entries)))
        extra = bytearray()
        for tag, kind, values in entries:
            code, unit = _T[kind]
            payload = _pack_values(kind, values)
            count = len(payload) // unit if kind not in ("RATIONAL", "SRATIONAL") else len(payload) // 8
            if len(payload) <= 4:
                field = payload.ljust(4, b"\0")
            else:
                if (data_pos + len(extra)) % 2:
                    extra.append(0)
                field = struct.pack(">I", data_pos + len(extra))
                extra += payload
            rows += struct.pack(">HHI", tag, code, count) + field
        rows += b"\0\0\0\0"
        self.b[off : off + size] = rows
        self.b += extra
        return off


def pack_mosaic(m: np.ndarray, bps: int) -> bytes:
    m = np.asarray(m, np.uint16)
    h, w = m.shape
    if bps == 16:
        return m.astype(">u2").tobytes()
    if bps == 12:
        assert w % 2 == 0
        a, b = m[:, 0::2].astype(np.uint16), m[:, 1::2].astype(np.uint16)
        out = np.empty((h, w // 2, 3), np.uint8)
        out[..., 0] = a >> 4
        out[..., 1] = ((a & 15) << 4) | (b >> 8)
        out[..., 2] = b & 255
        return out.tobytes()
    raise ValueError(bps)


def build_dng(
    mosaic: np.ndarray,
    *,
    bps: int = 16,
    cfa=((0, 1), (1, 2)),
    black=(0,),
    black_repeat=(1, 1),
    black_delta_h=None,
    black_delta_v=None,
    white: int | None = None,
    active=None,
    crop_origin=(0, 0),
    crop_size=None,
    neutral=(0.5, 1.0, 0.6),
    orientation: int = 1,
    linearization=None,
    compression: int = 1,
    iso: int = 200,
    when: str = "2026:10:16 18:51:53",
    lens: str = "RICOH GR LENS A12 50mm F2.5 MACRO",
    with_neutral: bool = True,
    exif_entries=(),
    root_extra=(),
) -> bytes:
    """`exif_entries` / `root_extra`：[(tag, kind, values)]，同标签覆盖默认值。"""
    h, w = mosaic.shape
    buf = _Buf()
    strip = buf.blob(pack_mosaic(mosaic, bps))
    strip_len = len(buf.b) - strip
    active = active or (0, 0, h, w)
    crop_size = crop_size or (active[3] - active[1] - crop_origin[0], active[2] - active[0] - crop_origin[1])
    raw = [
        (254, "LONG", [0]),
        (256, "LONG", [w]),
        (257, "LONG", [h]),
        (258, "SHORT", [bps]),
        (259, "SHORT", [compression]),
        (262, "SHORT", [32803]),
        (273, "LONG", [strip]),
        (277, "SHORT", [1]),
        (278, "LONG", [h]),
        (279, "LONG", [strip_len]),
        (33421, "SHORT", [2, 2]),
        (33422, "BYTE", [v for row in cfa for v in row]),
        (50713, "SHORT", list(black_repeat)),
        (50714, "SHORT", list(black)),
        (50717, "LONG", [white if white is not None else (1 << bps) - 1]),
        (50719, "LONG", list(crop_origin)),
        (50720, "LONG", list(crop_size)),
        (50829, "LONG", list(active)),
    ]
    if black_delta_h is not None:
        raw.append((50715, "RATIONAL", list(black_delta_h)))
    if black_delta_v is not None:
        raw.append((50716, "RATIONAL", list(black_delta_v)))
    if linearization is not None:
        raw.append((50712, "SHORT", list(linearization)))
    sub = buf.ifd(raw)
    exif_rows = {
        33434: (33434, "RATIONAL", [0.01]),
        33437: (33437, "RATIONAL", [2.5]),
        34855: (34855, "SHORT", [iso]),
        36867: (36867, "ASCII", when),
        36868: (36868, "ASCII", when),
        37500: (37500, "UNDEFINED", b"Ricoh\0\0\0" + bytes(range(40))),
    }
    exif_rows.update({e[0]: e for e in exif_entries})
    exif = buf.ifd(list(exif_rows.values()))
    thumb = buf.blob(bytes(8 * 8 * 3))
    ifd0 = [
        (256, "SHORT", [8]),
        (257, "SHORT", [8]),
        (258, "SHORT", [8, 8, 8]),
        (259, "SHORT", [1]),
        (262, "SHORT", [2]),
        (271, "ASCII", "RICOH"),
        (272, "ASCII", "GXR A12"),
        (273, "LONG", [thumb]),
        (274, "SHORT", [orientation]),
        (277, "SHORT", [3]),
        (278, "SHORT", [8]),
        (279, "LONG", [192]),
        (306, "ASCII", when),
        (330, "LONG", [sub]),
        (34665, "LONG", [exif]),
        (50708, "ASCII", lens),
        (50730, "SRATIONAL", [0.3]),
    ]
    if with_neutral:
        ifd0.append((50728, "RATIONAL", list(neutral)))
    extra = {e[0]: e for e in root_extra}
    root = buf.ifd([e for e in ifd0 if e[0] not in extra] + list(extra.values()))
    buf.b[4:8] = struct.pack(">I", root)
    return bytes(buf.b)


def bayer_scene(h: int, w: int, rgb_linear, *, cfa=((0, 1), (1, 2)), white: int = 4095, black: int = 0, origin=(0, 0)) -> np.ndarray:
    """给定每个像素的 (h, w, 3) 线性 RGB（0..1），按 CFA 取样成马赛克 uint16。"""
    yy, xx = np.indices((h, w))
    c = np.array(cfa)[(yy + origin[1]) % 2, (xx + origin[0]) % 2]
    v = np.take_along_axis(np.asarray(rgb_linear), c[..., None], axis=2)[..., 0]
    return np.rint(black + v * (white - black)).astype(np.uint16)
