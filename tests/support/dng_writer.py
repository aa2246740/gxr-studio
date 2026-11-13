"""Build a small TIFF/DNG in memory for parity tests.

Nothing here is a camera file. Tests pass the bytes to Python and to ``gxr-cli``;
they are never written into the repository. Both endians, several strips, and the
tags the raw reader looks at are available as arguments.
"""

from __future__ import annotations

import struct
from typing import Any

import numpy as np

# TIFF type name -> (code, bytes per value)
_TYPES = {
    "BYTE": (1, 1),
    "ASCII": (2, 1),
    "SHORT": (3, 2),
    "LONG": (4, 4),
    "RATIONAL": (5, 8),
    "UNDEFINED": (7, 1),
    "SLONG": (9, 4),
    "SRATIONAL": (10, 8),
}

# A camera-like matrix with exact tenths, including a negative, so SRATIONAL is exercised.
COLOR_MATRIX = (
    (7, 10),
    (-1, 10),
    (2, 5),
    (-1, 5),
    (6, 5),
    (1, 10),
    (0, 1),
    (-3, 10),
    (7, 5),
)

Entry = tuple[int, str, Any]


def row_stride(width: int, bps: int) -> int:
    """Bytes in one packed row: ``(width * bps + 7) // 8``."""
    return (width * bps + 7) // 8


def pack_rows(mosaic: np.ndarray, bps: int, endian: str) -> list[bytes]:
    """Pack each row the way ``Dng._decode_mosaic`` reads it back."""
    image = np.asarray(mosaic, np.uint16)
    height, width = image.shape
    if bps == 16:
        dtype = "<u2" if endian == "<" else ">u2"
        raw = image.astype(dtype).tobytes()
        stride = width * 2
        return [raw[y * stride : (y + 1) * stride] for y in range(height)]
    if bps == 12:
        return [_pack_row_12(image[y]) for y in range(height)]
    return _pack_bit_rows(image, bps)


def _pack_row_12(row: np.ndarray) -> bytes:
    width = int(row.shape[0])
    out = bytearray()
    pairs = width // 2
    for k in range(pairs):
        first = int(row[2 * k]) & 0xFFF
        second = int(row[2 * k + 1]) & 0xFFF
        out.append(first >> 4)
        out.append(((first & 15) << 4) | (second >> 8))
        out.append(second & 255)
    if width % 2:
        last = int(row[-1]) & 0xFFF
        out.append(last >> 4)
        out.append((last & 15) << 4)
    need = row_stride(width, 12)
    if len(out) < need:
        out.extend(b"\0" * (need - len(out)))
    return bytes(out[:need])


def _pack_bit_rows(image: np.ndarray, bps: int) -> list[bytes]:
    """Most-significant bit first, which is what ``np.unpackbits`` reads back."""
    height, width = image.shape
    shifts = np.arange(bps - 1, -1, -1, dtype=np.uint32)
    bits = ((image.astype(np.uint32)[..., None] >> shifts) & 1).astype(np.uint8)
    flat = bits.reshape(height, width * bps)
    pad = row_stride(width, bps) * 8 - width * bps
    if pad:
        flat = np.concatenate([flat, np.zeros((height, pad), np.uint8)], axis=1)
    packed = np.packbits(flat, axis=1)
    return [packed[y].tobytes() for y in range(height)]


class _Tiff:
    """Classic TIFF writer. ``endian`` is ``>`` or ``<``."""

    def __init__(self, endian: str) -> None:
        self.endian = endian
        mark = b"MM" if endian == ">" else b"II"
        self.buf = bytearray(mark + struct.pack(endian + "HI", 42, 0))

    def _align(self) -> None:
        if len(self.buf) % 2:
            self.buf.append(0)

    def blob(self, data: bytes) -> int:
        self._align()
        offset = len(self.buf)
        self.buf += data
        return offset

    def ifd(self, entries: list[Entry]) -> int:
        """Write one IFD and return its offset. Entries are stored sorted by tag."""
        rows = sorted(entries, key=lambda item: item[0])
        self._align()
        offset = len(self.buf)
        slot = 2 + 12 * len(rows) + 4
        self.buf += b"\0" * slot
        data_at = len(self.buf)
        body = bytearray(struct.pack(self.endian + "H", len(rows)))
        extra = bytearray()
        for tag, kind, values in rows:
            code, unit = _TYPES[kind]
            payload = _pack(kind, values, self.endian)
            count = len(payload) // 8 if kind in ("RATIONAL", "SRATIONAL") else len(payload) // unit
            if len(payload) <= 4:
                field = payload.ljust(4, b"\0")
            else:
                if (data_at + len(extra)) % 2:
                    extra.append(0)
                field = struct.pack(self.endian + "I", data_at + len(extra))
                extra += payload
            body += struct.pack(self.endian + "HHI", tag, code, count) + field
        body += b"\0\0\0\0"
        self.buf[offset : offset + slot] = body
        self.buf += extra
        return offset

    def finish(self, root: int) -> bytes:
        self.buf[4:8] = struct.pack(self.endian + "I", root)
        return bytes(self.buf)


def _pack(kind: str, values: Any, endian: str) -> bytes:
    if kind == "ASCII":
        text = values if isinstance(values, str) else str(values)
        return text.encode("ascii") + b"\0"
    if kind in ("BYTE", "UNDEFINED"):
        return bytes(values)
    if kind == "SHORT":
        return struct.pack(endian + f"{len(values)}H", *[int(v) for v in values])
    if kind == "LONG":
        return struct.pack(endian + f"{len(values)}I", *[int(v) for v in values])
    if kind == "SLONG":
        return struct.pack(endian + f"{len(values)}i", *[int(v) for v in values])
    if kind in ("RATIONAL", "SRATIONAL"):
        return _pack_rational(values, signed=kind == "SRATIONAL", endian=endian)
    raise ValueError(kind)


def _pack_rational(values: Any, *, signed: bool, endian: str) -> bytes:
    fmt = endian + ("i" if signed else "I")
    out = bytearray()
    for value in values:
        if isinstance(value, tuple):
            num, den = int(value[0]), int(value[1])
        else:
            den = 1_000_000
            num = int(round(float(value) * den))
        out += struct.pack(fmt, num)
        out += struct.pack(fmt, den)
    return bytes(out)


def _number_entry(tag: int, values: list[float] | tuple[float, ...]) -> Entry:
    """SHORT when every value is a whole number, otherwise RATIONAL."""
    if all(float(v).is_integer() and v >= 0 for v in values):
        return (tag, "SHORT", [int(v) for v in values])
    return (tag, "RATIONAL", list(values))


def write_dng(
    mosaic: np.ndarray,
    *,
    endian: str = ">",
    bps: int = 16,
    rows_per_strip: int | None = None,
    pad_last: int = 0,
    cfa: tuple[tuple[int, ...], ...] = ((0, 1), (1, 2)),
    black: tuple[float, ...] = (0.0,),
    black_repeat: tuple[int, int] = (1, 1),
    black_dh: tuple[float, ...] | None = None,
    black_dv: tuple[float, ...] | None = None,
    white: int | None = None,
    active: tuple[int, int, int, int] | None = None,
    crop_origin: tuple[int, int] = (0, 0),
    crop_size: tuple[int, int] | None = None,
    orientation: int = 1,
    neutral: tuple[float, ...] | None = (0.5, 1.0, 0.7),
    color_matrix: tuple[tuple[int, int], ...] | None = COLOR_MATRIX,
    nan_in_matrix: bool = False,
    linearization: tuple[int, ...] | None = None,
    compression: int = 1,
    samples_per_pixel: int = 1,
    fill_order: int = 1,
    tiled: bool = False,
    strip_offset_past_end: bool = False,
    illuminant: int = 21,
) -> bytes:
    """Return one DNG. ``mosaic`` is ``(height, width)`` uint16 sample values before packing."""
    image = np.asarray(mosaic, np.uint16)
    height, width = int(image.shape[0]), int(image.shape[1])
    rows = pack_rows(image, bps, endian)
    step = height if rows_per_strip is None else rows_per_strip
    strips = [b"".join(rows[start : start + step]) for start in range(0, height, step)]
    if pad_last:
        strips[-1] = strips[-1] + bytes(pad_last)
    if not strips:
        strips = [b""]

    tiff = _Tiff(endian)
    offsets = [tiff.blob(blob) for blob in strips]
    counts = [len(blob) for blob in strips]
    if strip_offset_past_end:
        offsets = [len(tiff.buf) + 64 for _ in offsets]

    area = active if active is not None else (0, 0, height, width)
    aw, ah = area[3] - area[1], area[2] - area[0]
    size = crop_size if crop_size is not None else (aw, ah)
    level = (1 << bps) - 1 if white is None else white
    pattern = [value for row in cfa for value in row]
    raw: list[Entry] = [
        (254, "LONG", [0]),
        (256, "LONG", [width]),
        (257, "LONG", [height]),
        (258, "SHORT", [bps]),
        (259, "SHORT", [compression]),
        (262, "SHORT", [32803]),
        (266, "SHORT", [fill_order]),
        (273, "LONG", offsets),
        (277, "SHORT", [samples_per_pixel]),
        (278, "LONG", [step]),
        (279, "LONG", counts),
        (33421, "SHORT", [len(cfa), len(cfa[0])]),
        (33422, "BYTE", pattern),
        (50713, "SHORT", [black_repeat[0], black_repeat[1]]),
        _number_entry(50714, list(black)),
        (50717, "LONG", [level]),
        (50719, "RATIONAL", list(crop_origin)),
        (50720, "RATIONAL", list(size)),
        (50829, "LONG", list(area)),
    ]
    if black_dh is not None:
        raw.append(_number_entry(50715, list(black_dh)))
    if black_dv is not None:
        raw.append(_number_entry(50716, list(black_dv)))
    if linearization is not None:
        raw.append((50712, "SHORT", list(linearization)))
    if tiled:
        raw.append((322, "LONG", [width]))
    raw_at = tiff.ifd(raw)

    root: list[Entry] = [
        (256, "SHORT", [1]),
        (257, "SHORT", [1]),
        (274, "SHORT", [orientation]),
        (330, "LONG", [raw_at]),
        (50727, "RATIONAL", [1.0, 1.0, 1.0]),
        (50778, "SHORT", [illuminant]),
    ]
    if neutral is not None:
        root.append((50728, "RATIONAL", list(neutral)))
    if color_matrix is not None:
        matrix = list(color_matrix)
        if nan_in_matrix:
            matrix[0] = (0, 0)
        root.append((50721, "SRATIONAL", matrix))
    return tiff.finish(tiff.ifd(root))
