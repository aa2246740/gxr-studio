"""TrueType 字体里版面要用的数：每个字符的步进宽度、字形外框、上沿高度，单位都是字体单位（em 的 1/upm）。

边框和时间戳的版面（`layout.py`、`web/assets/finish_layout.js`）只用这里读出来的整数和整数算式，
不问 Pillow 的排版引擎：Pillow 默认在有 libraqm 的电脑上走 RAQM（小数步进、GPOS 字距），没有的走 BASIC
（按像素取整的步进，DSEG 这类没有字体指令的字还要被自动对齐器再改一遍），同一个字号量出来的宽度就不一样。
这里不受那些影响：表是从字体文件本身读的，浏览器拿到的是同一份（`/api/finish/assets/<字体>.json`）。

只读 `glyf` 轮廓的 TrueType（包括 Inter SemiBold 在内的六个内置字体都是）。cmap 认格式 4 和 12，别的格式读不了就报错，不猜。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

Box = tuple[int, int, int, int]  # xMin, yMin, xMax, yMax（字体单位，y 向上）
Glyph = tuple[int, Box | None]  # 步进宽度，外框（没有墨迹的字形，比如空格，是 None）


@dataclass(frozen=True)
class FontTable:
    upm: int
    ascender: int  # hhea 的 ascender
    notdef: Glyph
    glyphs: dict[int, Glyph]  # 码点 → 字形；字体里没有的码点用 notdef

    def glyph(self, code: int) -> Glyph:
        return self.glyphs.get(code, self.notdef)

    def to_json(self) -> dict[str, Any]:
        """给浏览器的列式表：cps 升序，adv 逐个对应，box 是每个字形四个数（没有墨迹的写四个 0）。"""
        codes = sorted(self.glyphs)
        box: list[int] = []
        for code in codes:
            box.extend(self.glyphs[code][1] or (0, 0, 0, 0))
        return {
            "upm": self.upm,
            "ascender": self.ascender,
            "notdef": [self.notdef[0], *(self.notdef[1] or (0, 0, 0, 0))],
            "cps": codes,
            "adv": [self.glyphs[code][0] for code in codes],
            "box": box,
        }


def _tables(data: bytes) -> dict[str, tuple[int, int]]:
    if data[:4] == b"ttcf":
        raise ValueError("字体集合（.ttc）不读")
    count = struct.unpack(">H", data[4:6])[0]
    out: dict[str, tuple[int, int]] = {}
    for i in range(count):
        tag, _checksum, offset, length = struct.unpack(">4sIII", data[12 + 16 * i : 28 + 16 * i])
        out[tag.decode("latin-1")] = (offset, length)
    return out


def _cmap(data: bytes, offset: int) -> dict[int, int]:
    count = struct.unpack(">H", data[offset + 2 : offset + 4])[0]
    subtables = []
    for i in range(count):
        platform, encoding, sub = struct.unpack(">HHI", data[offset + 4 + 8 * i : offset + 12 + 8 * i])
        subtables.append((platform, encoding, offset + sub, struct.unpack(">H", data[offset + sub : offset + sub + 2])[0]))
    chosen = next((s for s in subtables if s[3] == 12 and (s[0], s[1]) in {(3, 10), (0, 4), (0, 6)}), None)
    chosen = chosen or next((s for s in subtables if s[3] == 4 and (s[0], s[1]) in {(3, 1), (0, 3), (0, 4)}), None)
    if chosen is None:
        raise ValueError("cmap 里没有格式 4 或 12 的 Unicode 子表")
    _platform, _encoding, at, fmt = chosen
    out: dict[int, int] = {}
    if fmt == 12:
        groups = struct.unpack(">I", data[at + 12 : at + 16])[0]
        for i in range(groups):
            first, last, glyph = struct.unpack(">III", data[at + 16 + 12 * i : at + 28 + 12 * i])
            for code in range(first, last + 1):
                out[code] = glyph + code - first
        return out
    seg2 = struct.unpack(">H", data[at + 6 : at + 8])[0]
    segs = seg2 // 2
    ends = struct.unpack(f">{segs}H", data[at + 14 : at + 14 + seg2])
    starts = struct.unpack(f">{segs}H", data[at + 16 + seg2 : at + 16 + 2 * seg2])
    deltas = struct.unpack(f">{segs}h", data[at + 16 + 2 * seg2 : at + 16 + 3 * seg2])
    ranges_at = at + 16 + 3 * seg2
    ranges = struct.unpack(f">{segs}H", data[ranges_at : ranges_at + seg2])
    for i in range(segs):
        for code in range(starts[i], ends[i] + 1):
            if code == 0xFFFF:
                continue
            if ranges[i] == 0:
                glyph = (code + deltas[i]) & 0xFFFF
            else:
                at_glyph = ranges_at + 2 * i + ranges[i] + 2 * (code - starts[i])
                glyph = struct.unpack(">H", data[at_glyph : at_glyph + 2])[0]
                if glyph:
                    glyph = (glyph + deltas[i]) & 0xFFFF
            out[code] = glyph
    return out


def read_font(path: Path) -> FontTable:
    data = path.read_bytes()
    tables = _tables(data)
    head, hhea, maxp, hmtx, loca, glyf = (tables[t][0] for t in ("head", "hhea", "maxp", "hmtx", "loca", "glyf"))
    upm = struct.unpack(">H", data[head + 18 : head + 20])[0]
    long_loca = struct.unpack(">h", data[head + 50 : head + 52])[0] == 1
    ascender = struct.unpack(">h", data[hhea + 4 : hhea + 6])[0]
    metrics = struct.unpack(">H", data[hhea + 34 : hhea + 36])[0]
    glyph_count = struct.unpack(">H", data[maxp + 4 : maxp + 6])[0]
    advances = [struct.unpack(">H", data[hmtx + 4 * min(g, metrics - 1) : hmtx + 4 * min(g, metrics - 1) + 2])[0] for g in range(glyph_count)]
    offsets = (
        struct.unpack(f">{glyph_count + 1}I", data[loca : loca + 4 * (glyph_count + 1)])
        if long_loca
        else [2 * v for v in struct.unpack(f">{glyph_count + 1}H", data[loca : loca + 2 * (glyph_count + 1)])]
    )

    def box_of(glyph: int) -> Box | None:
        if offsets[glyph + 1] == offsets[glyph]:
            return None  # 没有轮廓（空格等）
        _contours, x0, y0, x1, y1 = struct.unpack(">hhhhh", data[glyf + offsets[glyph] : glyf + offsets[glyph] + 10])
        return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None

    by_glyph = [(advances[g], box_of(g)) for g in range(glyph_count)]
    glyphs = {code: by_glyph[g] for code, g in _cmap(data, tables["cmap"][0]).items() if 0 < g < glyph_count}
    return FontTable(upm, ascender, by_glyph[0], glyphs)


@cache
def load_font(path: Path) -> FontTable:
    return read_font(path)
