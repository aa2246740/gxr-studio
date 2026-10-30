"""极小的只读 TIFF 读取器（经典 TIFF，不含 BigTIFF）。

来源：research/color_calibration/analyze_capture.py 的 `Tiff`（SHA-256 见 PROVENANCE.json）。
与来源的唯一差别：`assert` 换成 `ValueError`（文件来自外部，不能指望 `python -O` 下断言还在）。
取值口径不变：有理数读成 float，ASCII 去掉尾部空白/NUL，其余类型读成 list。
"""

from __future__ import annotations

import struct


class Tiff:
    """Read-only classic TIFF reader."""

    TYPES = {
        1: ("B", 1),
        2: (None, 1),
        3: ("H", 2),
        4: ("I", 4),
        5: ("II", 8),
        6: ("b", 1),
        7: (None, 1),
        8: ("h", 2),
        9: ("i", 4),
        10: ("ii", 8),
        11: ("f", 4),
        12: ("d", 8),
    }

    def __init__(self, data: bytes):
        self.data = data
        if len(data) < 8 or data[:2] not in (b"II", b"MM"):
            raise ValueError("不是 TIFF/DNG 文件")
        self.endian = "<" if data[:2] == b"II" else ">"
        if struct.unpack_from(self.endian + "H", data, 2)[0] != 42:
            raise ValueError("不是经典 TIFF（魔数不是 42）")
        self.root = struct.unpack_from(self.endian + "I", data, 4)[0]

    def ifd(self, offset: int, endian: str | None = None) -> dict[int, dict]:
        e = endian or self.endian
        if not 0 <= offset <= len(self.data) - 2:
            raise ValueError("IFD 偏移越界")
        count = struct.unpack_from(e + "H", self.data, offset)[0]
        if offset + 2 + 12 * count + 4 > len(self.data):
            raise ValueError("IFD 超出文件")
        result: dict[int, dict] = {}
        for i in range(count):
            p = offset + 2 + i * 12
            tag, typ, n = struct.unpack_from(e + "HHI", self.data, p)
            if typ not in self.TYPES:
                continue
            fmt, size = self.TYPES[typ]
            length = n * size
            o = p + 8 if length <= 4 else struct.unpack_from(e + "I", self.data, p + 8)[0]
            if o + length > len(self.data):
                raise ValueError(f"标签 {tag} 的数据超出文件")
            raw = self.data[o : o + length]
            value: object
            if typ == 2:
                value = raw.rstrip(b"\0 ").decode("ascii", errors="replace")
            elif fmt is None:
                value = raw
            elif typ in (5, 10):
                a = struct.unpack(e + fmt * n, raw)
                value = [a[j] / a[j + 1] for j in range(0, len(a), 2)]
            else:
                value = list(struct.unpack(e + fmt * n, raw))
            result[tag] = {"offset": o, "raw": raw, "value": value, "type": typ, "count": n}
        return result
