"""r5 只读双文件编解码。库按 SHA 绑定，未知库不得按相同标签猜读。"""

from __future__ import annotations

import hashlib
import struct

from . import datcodec as dc

LIBRARY_NAME = "GXRFX.BIN"
MAX_LIBRARY_BYTES = 65528
LIBRARY_SHA256 = {
    176: "e81659225e74cc5a59823e921a25092bd02b068f98396ecbd111921a67821c48",
    178: "0af19a0cca12e58d96fbc783491624696dc477db0f6aade0b220df7821b07b50",
}


def _sections(raw: bytes) -> list[tuple[int, int]]:
    cursor = dc.HEADER
    sections = []
    for i in range(7):
        kind, flags, start, size = struct.unpack_from(">HHII", raw, 128 + 12 * i)
        if kind != i + 1 or flags or start != cursor or start % 4 or size % 4 or size > len(raw) - start:
            raise dc.DatError("r5 区段布局不合格")
        sections.append((start, size))
        cursor += size
    if cursor != len(raw):
        raise dc.DatError("r5 区段长度不合格")
    return sections


def _check_envelope(raw: bytes, magic: bytes, flags: int) -> None:
    if len(raw) < dc.HEADER or raw[:8] != magic or raw[8:12] != b"\0\1\1\0" or dc.u32(raw, 12) != flags:
        raise dc.DatError("r5 文件头不合格")
    header = raw[:20] + bytes(4) + raw[24:256]
    if dc.u32(raw, 16) != len(raw) or dc.u32(raw, 20) != dc.crc(header) or dc.u32(raw, 24) != dc.crc(raw[256:]):
        raise dc.DatError("r5 文件长度或 CRC 对不上")


def _catalogue(library: bytes | None, generation: int) -> bytes:
    profile = dc.PROFILES.get(generation)
    if profile is None or profile.library_generation is None:
        raise dc.UnsupportedGeneration(f"代数 {generation} 没有登记 r5 库")
    library_generation = profile.library_generation
    if library is None:
        raise dc.DatError("缺少 GXRFX.BIN。请将 GXRCE.DAT 和 GXRFX.BIN 一起从存储卡复制到同一文件夹，再同时导入。")
    if hashlib.sha256(library).hexdigest() != LIBRARY_SHA256[library_generation]:
        raise dc.DatError(f"GXRFX.BIN 与第 {generation} 代 GXRCE.DAT 不匹配或已损坏，请重新复制同一套两个文件。")
    cat = library[64:]
    if (
        library[:12] != b"GXRLB003\0\3\0@"
        or len(library) > MAX_LIBRARY_BYTES
        or dc.u32(library, 12) != len(library)
        or dc.u32(library, 16) != len(cat)
        or dc.u32(library, 20) != dc.crc(cat)
        or dc.u32(library, 24) != 7
        or dc.u32(library, 28) != 3
        or dc.u32(library, 32) != dc.crc(library[:32] + bytes(4) + library[36:64])
        or dc.u32(library, 36) != library_generation
        or dc.u32(library, 40) != len(cat)
        or any(library[44:64])
    ):
        raise dc.DatError("GXRFX.BIN 库头或 CRC 不合格")
    _check_envelope(cat, b"GXRCL002", 2)
    return cat


def _pools(shared: bytes) -> list[tuple[int, int, int]]:
    return [(dc.u16(shared, 16 + 12 * kind), dc.u16(shared, 18 + 12 * kind), dc.u32(shared, 20 + 12 * kind)) for kind in range(4)]


def _entry(desc: bytes, shared: bytes, pools: list[tuple[int, int, int]], *, wide: bool = False) -> bytes:
    entry = bytearray(dc.ENTRY_BYTES)
    entry[:2], entry[4:8], entry[445:447] = desc[:2], desc[4:8], desc[2:4]
    extra = shared[dc.u32(shared, 64) :] if wide else b""
    extra_at = 48 + 44 * dc.u16(extra, 8) if wide else 0
    starts = []
    for kind, size in enumerate(dc.POOL_LENGTHS):
        starts.append(extra_at)
        extra_at += (dc.u16(extra, 16 + 2 * kind) * size + 3) & ~3 if wide else 0
    for i, (kind, off, size) in enumerate(zip(dc.KINDS, dc.OFFSETS, dc.ROW_BYTES)):
        index = dc.u16(desc, 8 + 2 * i) if wide else desc[8 + i]
        if wide and index & 32768:
            index &= 32767
            if index >= dc.u16(extra, 24 + 2 * kind):
                raise dc.DatError("完整配方行索引越界")
            start, source = starts[kind] + index * size, extra
        else:
            count, _, at = pools[kind]
            if index >= count:
                raise dc.DatError("原生配方行索引越界")
            start, source = at + index * size, shared
        entry[off : off + size] = source[start : start + size]
    for off, size, field in dc.CRC_FIELDS:
        struct.pack_into(">I", entry, field, dc.crc(entry[off : off + size]))
    return bytes(entry)


def _modules(cat: bytes, sections: list[tuple[int, int]], shared: bytes) -> list[dc.Module]:
    modules = []
    pools = _pools(shared)
    for unit, name in enumerate(dc.NAMES):
        directory = sections[2][0] + 64 * unit
        map_at, bank_at, table, ranges_at = (dc.u32(cat, directory + n) for n in (12, 16, 20, 24))
        count = cat[directory + 30]
        records = [cat[bank_at + 32 + 64 * i : bank_at + 96 + 64 * i] for i in range(16)]
        descriptors = [cat[table + 16 + 20 * i : table + 36 + 20 * i] for i in range(count)]
        entries = {d[0]: _entry(d, shared, pools) for d in descriptors}
        owners = [d[0] for d in descriptors]
        refs = [owners[cat[ranges_at + 28 * i + 24]] for i in range(16)]
        ranges = [[struct.unpack_from("bbb", cat, ranges_at + 28 * i + 3 * k) for k in range(8)] for i in range(16)]
        mapping = cat[map_at : map_at + 152]
        stock = entries[4]
        pin = dc.crc(mapping + b"".join(stock[o : o + s] for o, s in zip(dc.OFFSETS, dc.ROW_BYTES)) + stock[445:447])
        if pin != dc.PIN_CRC32[unit] or dc.crc(stock[:448]) != dc.STOCK_ENTRY_CRC[unit]:
            raise dc.DatError("原厂 04 或 map128 钉子不合格")
        modules.append(dc.Module(name, mapping, records, entries, refs, ranges, cat[directory + 32 : directory + 64], pin))
    return modules


def parse(raw: bytes, library: bytes | None) -> dc.Dat:
    _check_envelope(raw, b"GXRCE001", 0)
    sections = _sections(raw)
    generation = dc.peek_generation(raw)
    engine_at, engine_size = sections[0]
    reloc_at, reloc_size = sections[1]
    if len(raw) > 98304 or engine_size > 81920 or reloc_size != 8 or any(n for _, n in sections[2:]):
        raise dc.DatError("r5 执行外壳尺寸不合格")
    dat = dc.Dat(
        generation,
        0,
        dc.u32(raw, 112),
        dc.u32(raw, 116),
        raw[80:112],
        raw[212:228],
        raw[engine_at : engine_at + engine_size],
        [struct.unpack_from(">II", raw, reloc_at)],
        [],
        (),
        b"",
        [],
        None,
        library=library,
        shell_header=raw[:256],
    )
    dc._check_engine_binding(dat, len(raw))
    if len(raw) + dat.bss > dc.MAX_RAM or dc.u16(raw, 120) != 1:
        raise dc.DatError("r5 BSS 或重定位不合格")
    for patch, target in dat.relocations:
        if dc.u32(raw, patch) != target:
            raise dc.DatError("r5 重定位与引擎不匹配")
    cat = _catalogue(library, generation)
    cat_sections = _sections(cat)
    shared_at, shared_size = cat_sections[5]
    shared = cat[shared_at : shared_at + shared_size]
    dat.modules = _modules(cat, cat_sections, shared)
    dat.kv2_block = cat[-dc.KV3_BLOCK_BYTES :]
    if hashlib.sha256(dat.kv2_block).hexdigest() != dc.KV3_BLOCK_SHA256:
        raise dc.DatError("r5 旋钮表未登记")
    area = shared[dc.u32(shared, 64) :]
    records = []
    for i in range(dc.u16(area, 10)):
        desc = area[48 + 44 * i : 92 + 44 * i]
        records.append(
            dc.FullRecipe(desc[32:44].split(b"\0")[0].decode("ascii"), desc[0], desc[28], _entry(desc, shared, _pools(shared), wide=True))
        )
    dat.recipes = tuple(records)
    return dat


def pack(dat: dc.Dat) -> bytes:
    """只重建执行外壳；r5 不提供改库或写卡接口。"""
    header = bytearray(dat.shell_header)
    struct.pack_into(">I", header, 28, dat.generation)
    struct.pack_into(">II", header, 112, dat.entry, dat.bss)
    header[80:112], header[212:228] = dat.resident_id, dat.abi_id
    raw = header + dat.engine + b"".join(struct.pack(">II", *r) for r in dat.relocations)
    dc._check_engine_binding(dat, len(raw))
    struct.pack_into(">I", raw, 24, dc.crc(raw[256:]))
    struct.pack_into(">I", raw, 20, dc.crc(raw[:20] + bytes(4) + raw[24:256]))
    if parse(bytes(raw), dat.library) != dat:
        raise dc.DatError("r5 配方库只读，不支持改写已解码的数据")
    return bytes(raw)
