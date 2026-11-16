"""De-hinted SFNT rewriter and synthetic TrueType builder for ``ink-text`` parity tests."""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from pathlib import Path

ARGS_ARE_WORDS = 0x0001
ARGS_ARE_XY_VALUES = 0x0002
ROUND_XY_TO_GRID = 0x0004
WE_HAVE_A_SCALE = 0x0008
MORE_COMPONENTS = 0x0020
WE_HAVE_AN_XY_SCALE = 0x0040
WE_HAVE_A_2X2 = 0x0080
WE_HAVE_INSTRUCTIONS = 0x0100
OVERLAP_COMPOUND = 0x0400


def _pad4(blob: bytes) -> bytes:
    rem = len(blob) % 4
    return blob if rem == 0 else blob + b"\x00" * (4 - rem)


def _table_checksum(blob: bytes) -> int:
    padded = _pad4(blob)
    total = 0
    for (word,) in struct.iter_unpack(">I", padded):
        total = (total + word) & 0xFFFFFFFF
    return total


def _assemble_sfnt(sfnt_version: bytes, tables: dict[str, bytes]) -> bytes:
    tags = sorted(tables.keys())
    n = len(tags)
    entry_sel = int(math.floor(math.log2(n))) if n > 0 else 0
    search_range = (1 << entry_sel) * 16 if n > 0 else 0
    range_shift = n * 16 - search_range

    head = bytearray(tables["head"])
    head[8:12] = b"\x00\x00\x00\x00"
    tables = dict(tables)
    tables["head"] = bytes(head)

    offset = 12 + 16 * n
    directory = bytearray()
    body = bytearray()
    head_offset = 0
    for tag in tags:
        raw = tables[tag]
        padded = _pad4(raw)
        csum = _table_checksum(raw)
        directory += struct.pack(">4sIII", tag.encode("latin-1"), csum, offset, len(raw))
        if tag == "head":
            head_offset = offset
        body += padded
        offset += len(padded)

    header = struct.pack(">4sHHHH", sfnt_version, n, search_range, entry_sel, range_shift)
    full = bytearray(header + directory + body)
    adj = (0xB1B0AFBA - _table_checksum(bytes(full))) & 0xFFFFFFFF
    struct.pack_into(">I", full, head_offset + 8, adj)
    return bytes(full)


def _strip_simple_glyph(glyph: bytes) -> bytes:
    n_contours = struct.unpack_from(">h", glyph, 0)[0]
    if n_contours == 0:
        return glyph
    instr_len_off = 10 + 2 * n_contours
    (instr_len,) = struct.unpack_from(">H", glyph, instr_len_off)
    if instr_len == 0:
        return glyph
    return glyph[:instr_len_off] + b"\x00\x00" + glyph[instr_len_off + 2 + instr_len :]


def _strip_composite_glyph(glyph: bytes) -> bytes:
    buf = bytearray(glyph)
    pos = 10
    while True:
        flags, _gid = struct.unpack_from(">HH", buf, pos)
        if flags & WE_HAVE_INSTRUCTIONS:
            struct.pack_into(">H", buf, pos, flags & ~WE_HAVE_INSTRUCTIONS)
        pos += 4
        pos += 4 if (flags & ARGS_ARE_WORDS) else 2
        if flags & WE_HAVE_A_SCALE:
            pos += 2
        elif flags & WE_HAVE_AN_XY_SCALE:
            pos += 4
        elif flags & WE_HAVE_A_2X2:
            pos += 8
        if not (flags & MORE_COMPONENTS):
            break
    return bytes(buf[:pos])


def dehint_ttf(font_bytes: bytes, *, zero_maxp: bool = False) -> bytes:
    """Return a copy of a TrueType font with glyph instructions, ``fpgm``, and ``prep`` emptied.

    When ``zero_maxp`` is False (default), ``maxp.maxSizeOfInstructions`` is kept >= 1 so
    FreeType's ``FT_Load_Glyph`` stays on the native TrueType loader instead of falling back to
    its auto-hinter. When ``zero_maxp`` is True (negative control), ``maxSizeOfInstructions`` is
    set to 0 so FreeType switches to the auto-hinter.
    """
    sfnt_ver = font_bytes[:4]
    (num_tables,) = struct.unpack_from(">H", font_bytes, 4)
    tables: dict[str, bytes] = {}
    for i in range(num_tables):
        tag_b, _csum, off, length = struct.unpack_from(">4sIII", font_bytes, 12 + i * 16)
        tables[tag_b.decode("latin-1")] = font_bytes[off : off + length]

    head = bytearray(tables["head"])
    (loc_fmt,) = struct.unpack_from(">h", head, 50)

    maxp = bytearray(tables["maxp"])
    (num_glyphs,) = struct.unpack_from(">H", maxp, 4)
    if len(maxp) >= 28:
        (orig_max_instr,) = struct.unpack_from(">H", maxp, 26)
        new_max_instr = 0 if zero_maxp else max(1, orig_max_instr)
        struct.pack_into(">H", maxp, 26, new_max_instr)
    tables["maxp"] = bytes(maxp)

    if zero_maxp:
        if "fpgm" in tables:
            tables["fpgm"] = b""
        if "prep" in tables:
            tables["prep"] = b""
    else:
        # FreeType 2.14.3 ftobjs.c:1014 autohints when font_program_size == 0 && cvt_program_size <= 7.
        # A 1-byte dummy `fpgm` keeps it on the native loader while `prep` & glyphs stay empty.
        tables["fpgm"] = b"\x00"
        if "prep" in tables:
            tables["prep"] = b""

    loca = tables["loca"]
    glyf = tables["glyf"]
    if loc_fmt == 0:
        offsets = [v * 2 for (v,) in struct.iter_unpack(">H", loca[: (num_glyphs + 1) * 2])]
    else:
        offsets = [v for (v,) in struct.iter_unpack(">I", loca[: (num_glyphs + 1) * 4])]

    new_glyf = bytearray()
    new_offsets: list[int] = []
    for gid in range(num_glyphs):
        new_offsets.append(len(new_glyf))
        start, end = offsets[gid], offsets[gid + 1]
        if start >= end:
            continue
        raw = glyf[start:end]
        (n_contours,) = struct.unpack_from(">h", raw, 0)
        stripped = _strip_simple_glyph(raw) if n_contours >= 0 else _strip_composite_glyph(raw)
        new_glyf += _pad4(stripped)
    new_offsets.append(len(new_glyf))

    if loc_fmt == 0 and all(off // 2 <= 0xFFFF for off in new_offsets):
        new_loca = b"".join(struct.pack(">H", off // 2) for off in new_offsets)
    else:
        struct.pack_into(">h", head, 50, 1)
        new_loca = b"".join(struct.pack(">I", off) for off in new_offsets)

    tables["head"] = bytes(head)
    tables["glyf"] = bytes(new_glyf)
    tables["loca"] = new_loca
    return _assemble_sfnt(sfnt_ver, tables)


@dataclass(frozen=True)
class SimpleContour:
    points: list[tuple[int, int]]
    on_curve: list[bool]


@dataclass(frozen=True)
class CompositePart:
    glyph_index: int
    flags: int
    arg1: int
    arg2: int


@dataclass(frozen=True)
class SyntheticGlyph:
    codepoint: int | None
    advance: int
    lsb: int = 0
    contours: tuple[SimpleContour, ...] = ()
    components: tuple[CompositePart, ...] = ()


def _encode_simple_glyph(contours: tuple[SimpleContour, ...]) -> bytes:
    if not contours:
        return b""
    all_pts = [pt for c in contours for pt in c.points]
    xs = [p[0] for p in all_pts]
    ys = [p[1] for p in all_pts]
    header = struct.pack(">hhhhh", len(contours), min(xs), min(ys), max(xs), max(ys))
    ends: list[int] = []
    idx = -1
    for c in contours:
        idx += len(c.points)
        ends.append(idx)
    end_bytes = b"".join(struct.pack(">H", e) for e in ends)
    instr_len = b"\x00\x00"
    flags = bytearray()
    dx_bytes = bytearray()
    dy_bytes = bytearray()
    prev_x = 0
    prev_y = 0
    for c in contours:
        for (px, py), on in zip(c.points, c.on_curve, strict=True):
            flags.append(1 if on else 0)
            dx_bytes += struct.pack(">h", px - prev_x)
            dy_bytes += struct.pack(">h", py - prev_y)
            prev_x, prev_y = px, py
    return header + end_bytes + instr_len + bytes(flags) + bytes(dx_bytes) + bytes(dy_bytes)


def _encode_composite_glyph(parts: tuple[CompositePart, ...], bbox: tuple[int, int, int, int]) -> bytes:
    out = bytearray(struct.pack(">hhhhh", -1, *bbox))
    for i, part in enumerate(parts):
        fl = part.flags | ARGS_ARE_WORDS
        if i + 1 < len(parts):
            fl |= MORE_COMPONENTS
        else:
            fl &= ~MORE_COMPONENTS
        out += struct.pack(">HHhh", fl, part.glyph_index, part.arg1, part.arg2)
    return bytes(out)


def _build_cmap_format4(mapping: dict[int, int]) -> bytes:
    codes = sorted(mapping.keys())
    # One segment per contiguous range, plus sentinel 0xFFFF.
    starts: list[int] = []
    ends: list[int] = []
    deltas: list[int] = []
    id_offsets: list[int] = []
    glyph_ids: list[int] = []

    for cp in codes:
        gid = mapping[cp]
        if ends and cp == ends[-1] + 1:
            ends[-1] = cp
            glyph_ids.append(gid)
        else:
            starts.append(cp)
            ends.append(cp)
            deltas.append(0)
            id_offsets.append(0)  # patched below
            glyph_ids.append(gid)

    starts.append(0xFFFF)
    ends.append(0xFFFF)
    deltas.append(1)
    id_offsets.append(0)

    seg_count = len(starts)
    seg_x2 = seg_count * 2
    entry_sel = int(math.floor(math.log2(seg_count)))
    search_range = (1 << entry_sel) * 2
    range_shift = seg_x2 - search_range

    # Compute idRangeOffset values pointing into glyphIdArray right after idRangeOffset[seg_count].
    running_g = 0
    for i in range(seg_count - 1):
        count = ends[i] - starts[i] + 1
        words_after = (seg_count - i) + running_g
        id_offsets[i] = words_after * 2
        running_g += count

    sub = bytearray(struct.pack(">HHHHHHH", 4, 0, 0, seg_x2, search_range, entry_sel, range_shift))
    for arr in (ends, [0], starts, deltas, id_offsets, glyph_ids):
        for val in arr:
            sub += struct.pack(">H", val & 0xFFFF)
    struct.pack_into(">H", sub, 2, len(sub))

    header = struct.pack(">HH", 0, 1) + struct.pack(">HHI", 3, 1, 12)
    return header + bytes(sub)


def build_synthetic_ttf(
    glyphs: list[SyntheticGlyph],
    *,
    upm: int = 1000,
    ascender: int = 800,
    descender: int = -200,
    head_flags: int = 0x0007,
) -> bytes:
    """Assemble a minimal valid TrueType font from simple & composite glyphs."""
    num_glyphs = len(glyphs)
    cmap_map: dict[int, int] = {}
    glyf_buf = bytearray()
    loca_offsets: list[int] = []
    hmtx_buf = bytearray()
    max_pts = 0
    max_contours = 0
    max_cpts = 0
    max_ccontours = 0

    for gid, g in enumerate(glyphs):
        if g.codepoint is not None:
            cmap_map[g.codepoint] = gid
        hmtx_buf += struct.pack(">Hh", g.advance, g.lsb)
        loca_offsets.append(len(glyf_buf))
        if g.components:
            blob = _encode_composite_glyph(g.components, (-upm, descender, upm * 2, ascender))
            max_cpts = max(max_cpts, 64)
            max_ccontours = max(max_ccontours, 16)
        elif g.contours:
            blob = _encode_simple_glyph(g.contours)
            pts_n = sum(len(c.points) for c in g.contours)
            max_pts = max(max_pts, pts_n)
            max_contours = max(max_contours, len(g.contours))
        else:
            blob = b""
        if blob:
            glyf_buf += _pad4(blob)
    loca_offsets.append(len(glyf_buf))

    head = bytearray(54)
    struct.pack_into(">IIHH", head, 0, 0x00010000, 0x00010000, 0, 0)
    struct.pack_into(">IHH", head, 12, 0x5F0F3CF5, head_flags, upm)
    struct.pack_into(">hhhhhh", head, 36, -upm, descender, upm * 2, ascender, 0, 8)
    struct.pack_into(">hh", head, 50, 1, 0)

    hhea = bytearray(36)
    struct.pack_into(">Ihhh", hhea, 0, 0x00010000, ascender, descender, 0)
    struct.pack_into(">H", hhea, 10, upm * 2)
    struct.pack_into(">hH", hhea, 32, 0, num_glyphs)

    maxp = bytearray(32)
    struct.pack_into(
        ">IHHHHHHHHHHHHHH",
        maxp,
        0,
        0x00010000,
        num_glyphs,
        max_pts,
        max_contours,
        max_cpts,
        max_ccontours,
        2,
        0,
        0,
        1,
        0,
        0,
        1,
        8,
        1,
    )

    os2 = bytearray(78)
    struct.pack_into(">Hh", os2, 0, 1, upm // 2)
    struct.pack_into(">H", os2, 4, 400)
    struct.pack_into(">H", os2, 6, 5)
    struct.pack_into(">H", os2, 62, 0)
    struct.pack_into(">hhh", os2, 68, ascender, descender, 0)
    struct.pack_into(">HH", os2, 74, ascender, -descender)

    post = struct.pack(">IiiHIIII", 0x00030000, 0, 0, 0, 0, 0, 0, 0)
    name = struct.pack(">HHH", 0, 0, 6)
    loca = b"".join(struct.pack(">I", off) for off in loca_offsets)

    return _assemble_sfnt(
        b"\x00\x01\x00\x00",
        {
            "OS/2": bytes(os2),
            "cmap": _build_cmap_format4(cmap_map),
            "fpgm": b"\x00",
            "glyf": bytes(glyf_buf),
            "head": bytes(head),
            "hhea": bytes(hhea),
            "hmtx": bytes(hmtx_buf),
            "loca": loca,
            "maxp": bytes(maxp),
            "name": name,
            "post": post,
        },
    )


def make_synthetic_test_font(dest: Path) -> Path:
    """Create a valid TrueType font covering bowtie, sub-pixel, overlap-compound, and huge outlines."""
    notdef = SyntheticGlyph(codepoint=None, advance=500)
    # 'A': self-intersecting bowtie + overlapping square (tests nonzero winding)
    bowtie = SyntheticGlyph(
        codepoint=ord("A"),
        advance=600,
        contours=(
            SimpleContour(
                points=[(100, 100), (500, 700), (500, 100), (100, 700)],
                on_curve=[True, True, True, True],
            ),
            SimpleContour(
                points=[(200, 200), (400, 200), (400, 400), (200, 400)],
                on_curve=[True, True, True, True],
            ),
        ),
    )
    # 'B': sub-pixel tiny triangle at typical px
    subpixel = SyntheticGlyph(
        codepoint=ord("B"),
        advance=400,
        contours=(
            SimpleContour(
                points=[(100, 100), (103, 100), (101, 104)],
                on_curve=[True, True, True],
            ),
        ),
    )
    # 'C': composite referencing 'A' (gid 1) + shifted 'B' (gid 2) with ROUND_XY_TO_GRID & OVERLAP_COMPOUND
    composite = SyntheticGlyph(
        codepoint=ord("C"),
        advance=700,
        components=(
            CompositePart(glyph_index=1, flags=ARGS_ARE_XY_VALUES | ROUND_XY_TO_GRID | OVERLAP_COMPOUND, arg1=40, arg2=20),
            CompositePart(glyph_index=2, flags=ARGS_ARE_XY_VALUES | ROUND_XY_TO_GRID, arg1=150, arg2=150),
        ),
    )
    # 'D': conic quadratic bezier curve
    conic = SyntheticGlyph(
        codepoint=ord("D"),
        advance=600,
        contours=(
            SimpleContour(
                points=[(50, 50), (300, 750), (550, 50)],
                on_curve=[True, False, True],
            ),
        ),
    )
    # 'E': huge outline coordinates (at upm=16, px=256 -> > 10 * x_ppem, triggers raster overflow)
    huge = SyntheticGlyph(
        codepoint=ord("E"),
        advance=500,
        contours=(
            SimpleContour(
                points=[(0, 0), (32000, 0), (32000, 100), (0, 100)],
                on_curve=[True, True, True, True],
            ),
        ),
    )
    dest.write_bytes(build_synthetic_ttf([notdef, bowtie, subpixel, composite, conic, huge]))
    return dest
