"""Runtime subset of the explicit GXR colour chain: type-28 matrix row, type-26 curve, BT.601 helpers.
Dropped (read the official firmware / not used by the prediction path): read_stock_rows,
read_vividness_table, row2_from_row1, SectorTable, Chain, h3_base and the JLAUNCH path constants.

Source: research/color_table_fit/matrix_model.py
Source SHA-256: 9bfe63aaae0f3f4fd4ef0d785456052fb8c4fff91bf0c9156883800a97dc5564
Verbatim AST segments only (see research/studio_engine_e0/tools/extract_engine.py).
"""
import struct

import numpy as np


Q10 = 1024


COEF_MAX = 2047                      # 11-bit magnitude: +-1.999


CHROMA_MAX = 1279                    # x 1.60 vividness must stay below the sign bit (1279 * 1.6 = 2046.4)


CENTRE = 128.0


ROW_BYTES = 28


# ----------------------------------------------------------------- Q10 sign-magnitude codec

def decode_coef(u):
    """u16 -> signed int. bit 11 = sign, bits 0..10 = magnitude; bits 12..15 must be zero."""
    u = int(u)
    if u & ~0xFFF:
        raise ValueError(f"coefficient word {u:#06x} has bits above 11")
    m = u & 0x7FF
    return -m if u & 0x800 else m


def encode_coef(v):
    v = int(v)
    if abs(v) > COEF_MAX:
        raise ValueError(f"coefficient {v} outside +-{COEF_MAX}")
    return (0x800 | -v) if v < 0 else v


class MatrixRow:
    """One 14 x u16 type-28 row: items 0..8 = [Y; Cb; Cr] x [R, G, B] Q10 sign-magnitude, items 9..13 raw words
    (9..11 = output offsets 0/128/128 in row 1, 12..13 = 0/0)."""

    def __init__(self, coef, tail):
        self.coef = np.asarray(coef, dtype=np.int64).reshape(3, 3)
        self.tail = tuple(int(t) for t in tail)
        if len(self.tail) != 5:
            raise ValueError("tail must be the five words 9..13")

    @classmethod
    def from_words(cls, words):
        words = [int(w) for w in words]
        if len(words) != 14:
            raise ValueError("a matrix row is 14 words")
        return cls([decode_coef(w) for w in words[:9]], words[9:])

    @classmethod
    def from_bytes(cls, b):
        if len(b) != ROW_BYTES:
            raise ValueError("a matrix row is 28 bytes")
        return cls.from_words(struct.unpack(">14H", b))

    def words(self):
        return [encode_coef(v) for v in self.coef.ravel()] + list(self.tail)

    def to_bytes(self):
        return struct.pack(">14H", *self.words())

    def copy(self, coef=None, tail=None):
        return MatrixRow(self.coef.copy() if coef is None else coef, self.tail if tail is None else tail)

    @property
    def offsets(self):
        return np.array(self.tail[:3], float)

    def row_sums(self):
        return self.coef.sum(axis=1)

    def violations(self, stock=None):
        """Reasons a firmware build must reject this row (empty list = acceptable)."""
        out = []
        for i, w in enumerate(self.words()[:9]):
            if w & ~0xFFF:
                out.append(f"item {i}: bits above 11")
        if np.any(np.abs(self.coef) > COEF_MAX):
            out.append(f"|coef| > {COEF_MAX}")
        if np.any(np.abs(self.coef[1:]) > CHROMA_MAX):
            out.append(f"|chroma coef| > {CHROMA_MAX} (overflows at vividness 160 %)")
        if stock is not None and tuple(self.tail) != tuple(stock.tail):
            out.append("offsets / tail words differ from stock")
        return out

    def neutral_preserving(self):
        s = self.row_sums()
        return bool(s[0] == Q10 and s[1] == 0 and s[2] == 0)

    def effective(self, vivid_pct=100.0):
        """Float 3x3 (1.0 = 1024) as applied: chroma rows scaled by vividness (rounding of the firmware's scaling is
        unknown; exact at 100 %)."""
        m = self.coef.astype(float) / Q10
        m[1:] *= float(vivid_pct) / 100.0
        return m

    def apply(self, rgbp, vivid_pct=100.0):
        """RGB' (..., 3) 8-bit -> Y, centred c = (Cb - 128) + i (Cr - 128) with this row's offsets."""
        m = self.effective(vivid_pct)
        ycc = np.asarray(rgbp, float) @ m.T
        off = self.offsets
        y = ycc[..., 0] + off[0]
        c = (ycc[..., 1] + off[1] - CENTRE) + 1j * (ycc[..., 2] + off[2] - CENTRE)
        return y, c

    def invert(self, y, c, vivid_pct=100.0):
        m = self.effective(vivid_pct)
        off = self.offsets
        v = np.stack([np.asarray(y, float) - off[0], np.real(c) - (off[1] - CENTRE), np.imag(c) - (off[2] - CENTRE)],
                     axis=-1)
        return v @ np.linalg.inv(m).T

    def describe(self):
        return {"words_u16": self.words(), "bytes_hex": self.to_bytes().hex(),
                "coef_q10": self.coef.tolist(), "coef_float": (self.coef / Q10).round(6).tolist(),
                "tail_words_9_13": list(self.tail), "row_sums": self.row_sums().tolist(),
                "neutral_preserving": self.neutral_preserving()}


# ----------------------------------------------------------------- curves

class Curve:
    """type-26 curve: 33 nodes (0, 31 inputs, 4095) -> 33 u8 outputs, linear interpolation."""

    def __init__(self, xs, ys, name="curve"):
        self.xs = np.asarray(xs, float)
        self.ys = np.asarray(ys, float)
        self.name = name
        if len(self.xs) != 33 or len(self.ys) != 33 or np.any(np.diff(self.xs) <= 0):
            raise ValueError(f"{name}: need 33 increasing nodes")
        keep = np.concatenate([[True], np.diff(self.ys) > 0])
        self._ix, self._iy = self.xs[keep], self.ys[keep]

    @classmethod
    def from_rows(cls, inputs31, outputs33, name="curve"):
        return cls([0, *inputs31, 4095], outputs33, name)

    def __call__(self, x):
        return np.interp(np.clip(np.asarray(x, float), 0, 4095), self.xs, self.ys)

    def inverse(self, y):
        """On the strictly increasing part (first node of a flat run)."""
        return np.interp(np.clip(np.asarray(y, float), self._iy[0], self._iy[-1]), self._iy, self._ix)


# ----------------------------------------------------------------- JFIF and colour

def ycbcr(rgb):
    rgb = np.asarray(rgb, float)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    y = 0.299 * r + 0.587 * g + 0.114 * b
    c = (-0.168736 * r - 0.331264 * g + 0.5 * b) + 1j * (0.5 * r - 0.418688 * g - 0.081312 * b)
    return y, c


def rgb_from_ycc(y, c):
    y = np.asarray(y, float)
    cb, cr = np.real(c), np.imag(c)
    return np.stack([y + 1.402 * cr, y - 0.344136 * cb - 0.714136 * cr, y + 1.772 * cb], axis=-1)


def hue_deg(c):
    return np.degrees(np.angle(c)) % 360
