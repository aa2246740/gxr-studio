"""独立552B记录的域和CRC检查；不生成相机文件。"""

from __future__ import annotations

import struct
import zlib

from ..camdata.datcodec import CRC_FIELDS, OFFSETS, ROW_BYTES, _valid_look_row
from ..engine.matrix_model import MatrixRow
from .types import RecipeError


def check_record(entry: bytes, *, full: bool = True) -> None:
    if len(entry) != 552:
        raise RecipeError("bad_record", "完整配方记录必须为552字节。")
    if full and entry[1] not in (1, 7):
        raise RecipeError("bad_record", "完整配方必须保留完整彩色或黑白字段。")
    for offset, size, crc_offset in CRC_FIELDS:
        if struct.unpack_from(">I", entry, crc_offset)[0] != zlib.crc32(entry[offset : offset + size]) & 0xFFFFFFFF:
            raise RecipeError("bad_record", "完整配方记录的CRC不符。")
    if not all(_valid_look_row(entry[o : o + n], i, entry[1]) for i, (o, n) in enumerate(zip(OFFSETS, ROW_BYTES))):
        raise RecipeError("bad_record", "完整配方记录的字段超出合法范围。")
    if full and entry[1] == 1:
        if any(entry[115:211]) or any(MatrixRow.from_bytes(entry[o : o + 28]).coef[1:].any() for o in (216, 244)):
            raise RecipeError("bad_record", "完整黑白记录需要物理零色度矩阵和扇区。")
