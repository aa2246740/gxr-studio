"""GXRCE.DAT 的解析与封装（按代数分派）。

格式细节来自研究目录里的打包器和校验器（`firmware/engine_v4x_*/candidate/containers/product/pack.py`、
`ui_data.py`、`gate.c`），来源 SHA 见 `engine/PROVENANCE.json` 的 `datcodec_sources`。已有代码各认一个代数
（`ui_data.parse_dat` 写死 127），这里把代数做成参数：

- 支持 127 / 129 / 131 / 135。未登记的代数一律拒绝（`UnsupportedGeneration`），只读也不猜。
- `parse()` 严格：任何没有建模的头部字节、区段尺寸、CRC、钉子（`PIN_CRC32`）对不上都抛 `DatError`。
- `pack(parse(b)) == b` 逐字节成立（每一代都有往返测试）。解析结果里的引擎、重定位、`gxkv2r2` 块都原样保存，
  不重新编译、不改写。
- 理光字节（行池、描述符里的深层行）只在内存里经过这里；本模块不写任何文件。
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from dataclasses import dataclass
from typing import Any, cast

NAMES = ("jlaunch", "qlaunch", "l08firm")
KINDS = (0, 1, 2, 3, 0, 1, 1, 1, 0, 1)
OFFSETS = (20, 82, 115, 216, 284, 346, 379, 412, 456, 518)
ROW_BYTES = (62, 33, 96, 56, 62, 33, 33, 33, 62, 33)
POOL_LENGTHS = (62, 33, 96, 56)
POOL_LIMITS = (144, 240, 48, 48)
CRC_FIELDS = (
    (20, 62, 8),
    (82, 33, 12),
    (115, 96, 16),
    (216, 56, 212),
    (284, 62, 272),
    (346, 33, 276),
    (379, 66, 280),
    (456, 62, 448),
    (518, 33, 452),
)
PIN_CRC32 = (0xF09FE7B4, 0xFB3FBCB2, 0x95F3DCA9)
STOCK_ENTRY_CRC = (0x0D2B0EA9, 0x341DDBEE, 0x4686301B)  # pack_v1.MODULES[*].stock_crc，对 entry[:448]
ILAUNCH3_SHA256 = "da1980e9d6f3996ede4953b8311cf0ce2abeb4bb300b6ee60a38f38e29a3cdf7"
ENTRY_BYTES = 552
HEADER = 256
MAX_RAM = 114688
DESCRIPTOR_BYTES = 20

# GXUI v1（用户条目）
GXUI_MAGIC = b"GXUI124\0"
LEGACY_BYTES = 1408
CONFIG_BYTES = 48
USER_ENTRY_BYTES = 24
USER_CAPACITY = 32
USER_OFFSET = 64 + 24 * CONFIG_BYTES
EXT_BYTES = USER_OFFSET + USER_CAPACITY * USER_ENTRY_BYTES
LEGACY_IDS = (1, 3, 4, 2)
KV2_BLOCK_BYTES = 17384
KV2_BLOCK_SHA256 = "43405f6c5f63050505e958882dcd260f2a9e1f707d3b407860041787964992f9"


class DatError(ValueError):
    """DAT 不合格（损坏、被改过、或不是我们认识的结构）。"""


class UnsupportedGeneration(DatError):
    """代数没有登记。电脑端对这样的 DAT 只能拒绝。"""


@dataclass(frozen=True)
class Profile:
    generation: int
    max_file: int
    kv2_block: bool  # 区段 7 末尾是否带 17384 B 的 gxkv2r2 块（原样搬运）


PROFILES = {
    127: Profile(127, 65536, False),
    129: Profile(129, 98304, True),
    131: Profile(131, 98304, True),
    135: Profile(135, 98304, True),
}


def crc(b: Any) -> int:
    return zlib.crc32(b) & 0xFFFFFFFF


def u16(b: bytes | bytearray, at: int) -> int:
    # typeshed 把 unpack_from 标成 tuple[Any, ...]，这里的格式串固定是一个整数。
    return cast(int, struct.unpack_from(">H", b, at)[0])


def u32(b: bytes | bytearray, at: int) -> int:
    return cast(int, struct.unpack_from(">I", b, at)[0])


def _zero_crc(b: Any, at: Any) -> Any:
    q = bytearray(b)
    q[at : at + 4] = bytes(4)
    return crc(q)


def _align4(n: int) -> int:
    return (n + 3) & ~3


def identity(record: bytes) -> bytes:
    return record[2:3] + record[4:36] + record[3:4] + record[36:51]


@dataclass
class Module:
    name: str
    map: bytes  # 152 B
    records: list[Any]  # 16 × 64 B
    entries: dict[Any, Any]  # 描述符所有者槽位 → 552 B 深层条目
    refs: list[Any]  # 16 个槽位各自指向的所有者槽位
    ranges: list[Any]  # 16 × 8 × (lo, hi, default)
    pin: bytes  # 32 B，前 4 字节 = crc_pin
    crc_pin: int


@dataclass
class Dat:
    generation: int
    flags: int
    entry: int
    bss: int
    resident_id: bytes
    abi_id: bytes
    engine: bytes
    relocations: list[Any]  # [(patch, target)]
    modules: list[Any]
    ids: tuple[Any, ...]  # 8 个参数 ID
    ext_config: bytes  # GXUI 配置表 24×48 B
    users: list[Any]  # [{'base': int, 'levels': [11 ints]}]
    kv2_block: bytes | None

    def entry_of(self, unit: int, slot: int) -> bytes:
        """槽位 1..16 的 552 B 深层条目（别名槽位解引用到所有者）。"""
        m = self.modules[unit]
        return cast(bytes, m.entries[m.refs[slot - 1]])


# --------------------------------------------------------------------------- 解析


def peek_generation(raw: bytes) -> int:
    if len(raw) < HEADER or raw[:8] != b"GXRCE001":
        raise DatError("不是 GXRCE.DAT")
    return u32(raw, 28)


def parse(raw: bytes) -> Dat:  # noqa: C901  按代数读 DAT 的布局，拆开会把已经对过的字节路径拆散
    g = peek_generation(raw)
    prof = PROFILES.get(g)
    if prof is None:
        raise UnsupportedGeneration(f"代数 {g} 未登记（支持 {sorted(PROFILES)}）")
    if len(raw) > prof.max_file or u32(raw, 16) != len(raw):
        raise DatError("DAT 长度不合格")
    if u32(raw, 20) != _zero_crc(raw[:HEADER], 20) or u32(raw, 24) != crc(raw[HEADER:]):
        raise DatError("DAT CRC 对不上")
    flags = u32(raw, 12)
    if raw[8:12] != struct.pack(">HH", 1, 256) or flags != 0:
        raise DatError("头部不是可执行形态（本编解码器不处理占位件）")
    bss = u32(raw, 116)
    if bss > MAX_RAM - len(raw):
        raise DatError("BSS 超限")
    cursor = HEADER
    sections = []
    for i in range(7):
        kind, fl, start, size = struct.unpack_from(">HHII", raw, 128 + 12 * i)
        if kind != i + 1 or fl or start != cursor or start % 4 or size % 4 or size > len(raw) - start:
            raise DatError("区段布局不合格")
        sections.append((start, size))
        cursor += size
    if cursor != len(raw) or sections[2][1] != 192 or sections[3][1] != 456 or sections[4][1] != 3168:
        raise DatError("固定区段尺寸不合格")
    engine_at, engine_len = sections[0]
    entry = u32(raw, 112)
    if not 16 <= engine_len <= 65536 or not 256 <= entry <= 256 + engine_len - 4 or entry % 4:
        raise DatError("引擎入口越界")
    nreloc = u16(raw, 120)
    if nreloc > 256 or sections[1][1] != 8 * nreloc:
        raise DatError("重定位尺寸不合格")
    relocations, previous = [], 0
    for i in range(nreloc):
        patch, target = struct.unpack_from(">II", raw, sections[1][0] + 8 * i)
        if (
            patch <= previous
            or patch % 4
            or not 256 <= patch <= 256 + engine_len - 4
            or not 256 <= target < len(raw) + bss
            or u32(raw, patch) != target
        ):
            raise DatError("重定位或映像绑定不合格")
        relocations.append((patch, target))
        previous = patch

    meta_at, meta_len = sections[6]
    tail = raw[meta_at : meta_at + meta_len]
    want_tail = LEGACY_BYTES + EXT_BYTES + (KV2_BLOCK_BYTES if prof.kv2_block else 0)
    if len(tail) != want_tail:
        raise DatError("区段 7 长度不合格")
    legacy = tail[:LEGACY_BYTES]
    ext = tail[LEGACY_BYTES : LEGACY_BYTES + EXT_BYTES]
    kv2 = tail[LEGACY_BYTES + EXT_BYTES :] if prof.kv2_block else None
    if kv2 is not None and hashlib.sha256(kv2).hexdigest() != KV2_BLOCK_SHA256:
        raise DatError("gxkv2r2 块与已登记的不同")
    if legacy[:32] != struct.pack(">4s6H4I", b"GXTM", 2, 32, 8, 3, 16, 28, 32, 64, 1344, 0):
        raise DatError("范围表布局不合格")
    ids = tuple(u32(legacy, 32 + 4 * i) for i in range(8))
    if ids != (1, 2, 3, 4, 5, 6, 0, 0):
        raise DatError("参数 ID 不合格")
    if (
        ext[:8] != GXUI_MAGIC
        or u16(ext, 8) != 1
        or u16(ext, 10) != 64
        or u32(ext, 12) != EXT_BYTES
        or u32(ext, 16) != _zero_crc(ext, 16)
        or u16(ext, 24) > USER_CAPACITY
        or ext[20:24] != struct.pack(">HH", 24, CONFIG_BYTES)
        or ext[26:32] != struct.pack(">HHH", USER_ENTRY_BYTES, 11, USER_CAPACITY)
        or u32(ext, 32) != 64
        or u32(ext, 36) != USER_OFFSET
        or any(ext[52:64])
    ):
        raise DatError("GXUI 头不合格")

    # 行池
    shared, pool_bytes = sections[5]
    if raw[shared : shared + 4] != b"GXDP" or u32(raw, shared + 8) != pool_bytes:
        raise DatError("行池头不合格")
    pools = []
    for k in range(4):
        count, size, offset, checksum = struct.unpack_from(">HHII", raw, shared + 16 + 12 * k)
        if not 1 <= count <= POOL_LIMITS[k] or size != POOL_LENGTHS[k] or offset + count * size > pool_bytes:
            raise DatError("行池边界不合格")
        body = raw[shared + offset : shared + offset + count * size]
        if crc(body) != checksum:
            raise DatError("行池 CRC 对不上")
        pools.append((count, size, shared + offset))

    directory = sections[2][0]
    modules = []
    for mi, name in enumerate(NAMES):
        d = directory + 64 * mi
        if raw[d : d + 8] != name.encode() + bytes(8 - len(name)) or u16(raw, d + 8) != 0x151 or u16(raw, d + 10) != 0:
            raise DatError("目录项名字或版本不合格")
        map_at, bank_at, table, meta_ranges = (u32(raw, d + 12), u32(raw, d + 16), u32(raw, d + 20), u32(raw, d + 24))
        count = raw[d + 30]
        if map_at != sections[3][0] + 152 * mi or bank_at != sections[4][0] + 1056 * mi:
            raise DatError("映射或配方库地址不合格")
        header = raw[bank_at : bank_at + 32]
        bank = raw[bank_at + 32 : bank_at + 32 + 1024]
        if (
            header[:8] != b"GXRRV201"
            or u32(header, 16) != prof.generation
            or u32(header, 20) != 1024
            or u32(header, 24) != crc(bank)
            or u32(header, 28) != crc(header[:28])
            or header[8:16] != b"\0\2\0 \0@\0\x10"
        ):
            raise DatError("配方库头、CRC 或代数不合格")
        if meta_ranges != meta_at + 64 + 16 * 28 * mi:
            raise DatError("范围目录不合格")
        if not 1 <= count <= 16 or not shared <= table <= shared + pool_bytes - 16 - 20 * count:
            raise DatError("描述符表边界不合格")
        if raw[table : table + 6] != b"GXDT\x06" + bytes([count]) or u32(raw, table + 12) != crc(raw[table + 16 : table + 16 + 20 * count]):
            raise DatError("描述符表 CRC 不合格")
        if u16(raw, d + 28) != 16 + 20 * count or raw[d + 31] != 0:
            raise DatError("描述符表长度不合格")
        records = [bank[64 * i : 64 * (i + 1)] for i in range(16)]
        for slot, rec in enumerate(records, 1):
            if rec[0] != slot or u32(rec, 60) != crc(rec[:60]):
                raise DatError("配方记录 CRC 不合格")
        entries, owners = {}, []
        descriptors = [raw[table + 16 + 20 * i : table + 36 + 20 * i] for i in range(count)]
        for desc in descriptors:
            slot = desc[0]
            if not 1 <= slot <= 16 or slot in entries:
                raise DatError("描述符所有者不合格")
            if u32(desc, 4) != crc(identity(records[slot - 1])):
                raise DatError("描述符与配方身份绑定不合格")
            if desc[18:20] != bytes(2):
                raise DatError("描述符保留字节非零")
            e = bytearray(ENTRY_BYTES)
            e[:2] = desc[:2]
            e[4:8] = desc[4:8]
            e[445:447] = desc[2:4]
            for off, size, kind, index in zip(OFFSETS, ROW_BYTES, KINDS, desc[8:18]):
                if index >= pools[kind][0]:
                    raise DatError("描述符行索引越界")
                at = pools[kind][2] + index * size
                e[off : off + size] = raw[at : at + size]
            for off, size, crc_off in CRC_FIELDS:
                struct.pack_into(">I", e, crc_off, crc(e[off : off + size]))
            entries[slot] = bytes(e)
            owners.append(slot)
        stock = descriptors[0]
        if stock[:4] != b"\x04\0\0\x02":
            raise DatError("第一个描述符不是原厂 04")
        stock_entry = entries[4]
        if crc(stock_entry[:448]) != STOCK_ENTRY_CRC[mi]:
            raise DatError("原厂 04 条目 CRC 与已登记的不同")
        mapping = raw[map_at : map_at + 152]
        raw_stock = b"".join(stock_entry[o : o + s] for o, s in zip(OFFSETS, ROW_BYTES)) + stock_entry[445:447]
        pin = crc(mapping + raw_stock)
        if pin != PIN_CRC32[mi] or u32(raw, d + 32) != pin:
            raise DatError("map128 ∥ stock 钉子对不上")
        refs, ranges = [], []
        for i in range(16):
            row = raw[meta_ranges + 28 * i : meta_ranges + 28 * (i + 1)]
            if row[24] >= len(owners):
                raise DatError("范围表引用越界")
            refs.append(owners[row[24]])
            ranges.append([struct.unpack("bbb", row[3 * j : 3 * j + 3]) for j in range(8)])
        modules.append(Module(name, mapping, records, entries, refs, ranges, raw[d + 32 : d + 64], pin))

    # GXUI：配置表与范围表的绑定、用户条目
    banks = [b"".join(m.records) for m in modules]
    if any(u32(ext, 40 + 4 * i) != crc(b) for i, b in enumerate(banks)):
        raise DatError("GXUI 与配方库的绑定不合格")
    for mi in range(3):
        for slot in range(1, 9):
            c = ext[64 + (mi * 8 + slot - 1) * CONFIG_BYTES :][:CONFIG_BYTES]
            order = list(c[4 : 4 + c[2]])
            expected = [1, 2, 3] if slot == 3 else [1, 2, 3, 4]
            if c[:2] != bytes((mi, slot)) or c[3] or sorted(order) != expected or any(c[4 + c[2] : 15]):
                raise DatError("GXUI 项目顺序不合格")
            for k in range(11):
                domain = struct.unpack_from("bbb", c, 15 + 3 * k)
                if k < 4:
                    if not -8 <= domain[0] <= 0 <= domain[1] <= 8 or domain[2] != 0:
                        raise DatError("GXUI 范围越出旧算法契约")
                    old = struct.unpack_from("bbb", legacy, 64 + (mi * 16 + slot - 1) * 28 + 3 * (LEGACY_IDS[k] - 1))
                    if domain != old:
                        raise DatError("GXUI 与旧范围表不一致")
                elif domain != ((-10, 5, 0) if k == 10 else (-9, 9, 0)):
                    raise DatError("GXUI 保留范围不合格")
    users = []
    for i in range(u16(ext, 24)):
        at = USER_OFFSET + i * USER_ENTRY_BYTES
        users.append({"base": ext[at], "levels": list(struct.unpack_from("11b", ext, at + 10))})
    dat = Dat(
        g,
        flags,
        entry,
        bss,
        raw[80:112],
        raw[212:228],
        raw[engine_at : engine_at + engine_len],
        relocations,
        modules,
        ids,
        bytes(ext[64:USER_OFFSET]),
        users,
        kv2,
    )
    # 最后一道门：用同一套规则重新封装，必须逐字节相同；否则说明还有没建模的字节。
    if pack(dat) != raw:
        raise DatError("重新封装与原文件不同（存在未建模的字节）")
    return dat


# --------------------------------------------------------------------------- 封装


def _metadata(modules: Any, ids: Any) -> bytes:
    n = len(ids)
    stride = _align4(3 * n + 4)
    out = bytearray(32 + 4 * n + 48 * stride)
    struct.pack_into(">4s6H4I", out, 0, b"GXTM", 2, 32, n, 3, 16, stride, 32, 32 + 4 * n, 48 * stride, 0)
    for k, pid in enumerate(ids):
        struct.pack_into(">I", out, 32 + 4 * k, pid)
    for mi, m in enumerate(modules):
        index = {s: i for i, s in enumerate(_owners(m))}
        for s in range(16):
            off = 32 + 4 * n + (mi * 16 + s) * stride
            if m.records[s][1]:
                for k, (lo, hi, default) in enumerate(m.ranges[s]):
                    if not -128 <= lo <= default <= hi <= 127 or (not ids[k] and (lo or hi or default)):
                        raise DatError("范围表不合格")
                    struct.pack_into("bbb", out, off + 3 * k, lo, hi, default)
                owner = m.refs[s]
                if owner not in index:
                    raise DatError("范围表引用了不存在的所有者")
                out[off + 3 * n] = index[owner]
                out[off + 3 * n + 1] = 1 if owner == s + 1 and owner != 4 else 3
            else:
                out[off + 3 * n] = 255
            out[off + 3 * n + 2] = 2
    return bytes(out)


def _owners(m: Module) -> list[int]:
    return [4] + sorted(s for s in m.entries if s != 4)


def _extension(configs: bytes, banks: Any, users: Any) -> bytes:
    if len(configs) != 24 * CONFIG_BYTES or len(users) > USER_CAPACITY:
        raise DatError("GXUI 配置尺寸或用户条目数不合格")
    out = bytearray(EXT_BYTES)
    out[:8] = GXUI_MAGIC
    struct.pack_into(">HHIIHHHHHH", out, 8, 1, 64, EXT_BYTES, 0, 24, CONFIG_BYTES, len(users), USER_ENTRY_BYTES, 11, USER_CAPACITY)
    struct.pack_into(">II", out, 32, 64, USER_OFFSET)
    for i, bank in enumerate(banks):
        struct.pack_into(">I", out, 40 + 4 * i, crc(bank))
    out[64:USER_OFFSET] = configs
    keys = ("Cont", "High", "Shad", "Satu", "Fade", "Warm", "Tint", "Splt", "Filt", "Tone", "Amt")
    groups = ("GRP", "GRN", "HCB", "STD", "FCC", "FCN", "LCH", "LVI")
    ordinals = [0] * 8
    for i, item in enumerate(users):
        slot, levels = item["base"], item["levels"]
        if not isinstance(slot, int) or isinstance(slot, bool) or not 1 <= slot <= 8 or len(levels) != 11:
            raise DatError("用户条目：base 必须是 1..8，levels 必须有 11 项")
        if any(not isinstance(v, int) or isinstance(v, bool) for v in levels):
            raise DatError("用户条目：levels 必须是整数")
        for mi in range(3):
            c = configs[(mi * 8 + slot - 1) * CONFIG_BYTES :][:CONFIG_BYTES]
            visible = set(c[4 : 4 + c[2]])
            for k, value in enumerate(levels):
                lo, hi, default = struct.unpack_from("bbb", c, 15 + 3 * k)
                if not lo <= value <= hi or (k + 1 not in visible and value != default):
                    raise DatError(f"用户条目：{keys[k]}={value} 超出底子 {slot} 的有效范围")
        ordinals[slot - 1] += 1
        ordinal = ordinals[slot - 1]
        name = f"{groups[slot - 1]}{'*' if any(levels) else '-'}{ordinal:03d}"
        at = USER_OFFSET + i * USER_ENTRY_BYTES
        out[at] = slot
        out[at + 2 : at + 10] = name.encode("ascii") + b"\0"
        struct.pack_into("11b", out, at + 10, *levels)
        struct.pack_into(">H", out, at + 22, ordinal)
    struct.pack_into(">I", out, 16, _zero_crc(out, 16))
    return bytes(out)


def _bank_blob(records: Any, build: Any) -> bytes:
    out = bytearray(32)
    struct.pack_into(">8sHHHHII", out, 0, b"GXRRV201", 2, 32, 64, 16, build, 1024)
    body = b"".join(records)
    struct.pack_into(">I", out, 24, crc(body))
    struct.pack_into(">I", out, 28, crc(out[:28]))
    return bytes(out) + body


def pack(dat: Dat) -> bytes:
    prof = PROFILES.get(dat.generation)
    if prof is None:
        raise UnsupportedGeneration(f"代数 {dat.generation} 未登记")
    modules = dat.modules
    if len(modules) != 3 or tuple(m.name for m in modules) != NAMES:
        raise DatError("模块顺序必须是 jlaunch, qlaunch, l08firm")
    buckets: list[list[Any]] = [[] for _ in range(4)]
    indexes: list[dict[Any, Any]] = [{} for _ in range(4)]
    tables = []
    for m in modules:
        owners = _owners(m)
        if not 1 <= len(owners) <= 16:
            raise DatError("描述符数量不合格")
        descriptors = []
        for s in owners:
            e = m.entries[s]
            if len(e) != ENTRY_BYTES or e[0] != s or u32(e, 4) != crc(identity(m.records[s - 1])):
                raise DatError("条目长度、所有者或身份对不上")
            refs = []
            for kind, off, size in zip(KINDS, OFFSETS, ROW_BYTES):
                row = bytes(e[off : off + size])
                if row not in indexes[kind]:
                    indexes[kind][row] = len(buckets[kind])
                    buckets[kind].append(row)
                refs.append(indexes[kind][row])
            descriptors.append(struct.pack(">BBHI10B2x", s, e[1], u16(e, 445), u32(e, 4), *refs))
        body = b"".join(descriptors)
        tables.append(struct.pack(">4sBBHHHI", b"GXDT", 6, len(owners), 0, 20, 0, crc(body)) + body)
    shared = bytearray(64)
    shared += b"".join(tables)
    for kind, rows in enumerate(buckets):
        raw = b"".join(rows)
        struct.pack_into(">HHII", shared, 16 + 12 * kind, len(rows), POOL_LENGTHS[kind], len(shared), crc(raw))
        shared += raw + bytes(_align4(len(raw)) - len(raw))
    struct.pack_into(">4sHHIHH", shared, 0, b"GXDP", 1, 64, len(shared), 16, 4)
    banks = [b"".join(m.records) for m in modules]
    tweaks = _metadata(modules, dat.ids) + _extension(dat.ext_config, banks, dat.users)
    if prof.kv2_block:
        if dat.kv2_block is None:
            raise DatError("这一代需要 gxkv2r2 块")
        tweaks += dat.kv2_block
    reloc = b"".join(struct.pack(">II", *r) for r in dat.relocations)
    parts = [
        bytes(dat.engine),
        reloc,
        bytes(192),
        b"".join(m.map for m in modules),
        b"".join(_bank_blob(m.records, dat.generation) for m in modules),
        bytes(shared),
        tweaks,
    ]
    starts, cursor = [], HEADER
    for p in parts:
        starts.append(cursor)
        cursor += len(p)
    if cursor > prof.max_file or cursor + dat.bss > MAX_RAM:
        raise DatError(f"容量超限 {cursor}/{cursor + dat.bss}")
    d = bytearray(192)
    next_table = starts[5] + 64
    stride = u16(tweaks, 14)
    for i, m in enumerate(modules):
        struct.pack_into(
            ">8sHHIIIIHBB32s",
            d,
            64 * i,
            m.name.encode() + b"\0",
            0x151,
            0,
            starts[3] + 152 * i,
            starts[4] + 1056 * i,
            next_table,
            starts[6] + u32(tweaks, 20) + 16 * stride * i,
            len(tables[i]),
            len(m.entries),
            0,
            m.pin,
        )
        next_table += len(tables[i])
    parts[2] = bytes(d)
    h = bytearray(HEADER)
    h[:8] = b"GXRCE001"
    struct.pack_into(">HHI", h, 8, 1, 256, dat.flags)
    struct.pack_into(">I", h, 28, dat.generation)
    struct.pack_into(">HH4sHHHH", h, 32, 1, 3, b"M32B", 0x151, 0, 7, 3)
    h[48:80] = bytes.fromhex(ILAUNCH3_SHA256)
    h[80:112] = dat.resident_id
    struct.pack_into(">I", h, 112, dat.entry)
    struct.pack_into(">I", h, 116, dat.bss)
    struct.pack_into(">H", h, 120, len(dat.relocations))
    h[212:228] = dat.abi_id
    for i, p in enumerate(parts):
        struct.pack_into(">HHII", h, 128 + 12 * i, i + 1, 0, starts[i], len(p))
    b = bytearray(h + b"".join(parts))
    struct.pack_into(">I", b, 16, len(b))
    struct.pack_into(">I", b, 24, crc(b[HEADER:]))
    struct.pack_into(">I", b, 20, 0)
    struct.pack_into(">I", b, 20, crc(b[:HEADER]))
    return bytes(b)


def replace_users(dat: Dat, users: list[Any]) -> Dat:
    """返回换了用户条目的新 Dat（原对象不动）；条目按 GXUI 规则校验，只改 GXUI 区。"""
    from dataclasses import replace

    new = replace(dat, users=[{"base": u["base"], "levels": list(u["levels"])} for u in users])
    _extension(new.ext_config, [b"".join(m.records) for m in new.modules], new.users)  # 先校验
    return new
