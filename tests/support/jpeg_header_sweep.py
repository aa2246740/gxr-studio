"""Deterministic synthetic JPEG header sweep generator for Pillow 12.2.0 parity."""

from __future__ import annotations

import struct
from typing import NamedTuple

from support.export_meta_cases import make_tiff_doc


class SweepCase(NamedTuple):
    name: str
    family: str
    blob: bytes


_UNIT_SIZES: dict[int, int] = {
    1: 1,
    2: 1,
    3: 2,
    4: 4,
    5: 8,
    6: 1,
    7: 1,
    8: 2,
    9: 4,
    10: 8,
    11: 4,
    12: 8,
}

_SOF0_192X128 = b"\xff\xc0\x00\x11\x08\x00\x80\x00\xc0\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
_SOF0_576X384 = b"\xff\xc0\x00\x11\x08\x01\x80\x02\x40\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
_SOF0_BOMB = b"\xff\xc0\x00\x11\x08\x27\x10\x4e\x20\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
_SOF0_ZERO_W = b"\xff\xc0\x00\x11\x08\x00\x80\x00\x00\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
_SOF0_ZERO_H = b"\xff\xc0\x00\x11\x08\x00\x00\x00\xc0\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
_SOF0_BAD_BITS = b"\xff\xc0\x00\x11\x0c\x00\x80\x00\xc0\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
_SOF0_BAD_LAYERS = b"\xff\xc0\x00\x0e\x08\x00\x80\x00\xc0\x02\x01\x11\x00\x02\x11\x00"
_SOF0_BAD_TAIL = b"\xff\xc0\x00\x09\x08\x00\x80\x00\xc0\x01\x01"
_SOS_1C = b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00"
_EOI = b"\xff\xd9"


def seg(marker: int, payload: bytes) -> bytes:
    return b"\xff" + bytes([marker & 0xFF]) + struct.pack(">H", len(payload) + 2) + payload


def _synthetic_exif_tiff(endian: str = "<", *, copy_suffix: str = "") -> bytes:
    recipe = f"GXR1 6a3a5a3c02 28 06 A8999B999-{copy_suffix}".encode("ascii") + b"\x00"
    ifd0 = [
        (271, 2, 6, b"RICOH\x00"),
        (272, 2, 4, b"GXR\x00"),
        (274, 3, 1, struct.pack(endian + "H", 1)),
        (306, 2, 20, b"2026:10:31 10:00:00\x00"),
        (33432, 2, len(recipe), recipe),
    ]
    sub = [
        (36867, 2, 20, b"2026:10:31 10:00:00\x00"),
        (33434, 5, 1, struct.pack(endian + "II", 1, 250)),
        (33437, 5, 1, struct.pack(endian + "II", 28, 10)),
        (34855, 3, 1, struct.pack(endian + "H", 200)),
        (42036, 2, 17, b"GR LENS A12 50mm\x00"),
    ]
    return make_tiff_doc(ifd0, sub, endian=endian)


def _base_parts() -> tuple[bytes, bytes, bytes]:
    app0 = seg(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    app1 = seg(0xE1, b"Exif\x00\x00" + _synthetic_exif_tiff("<"))
    return app0 + app1, _SOF0_192X128, _SOS_1C + _EOI


def _pack_classic_mpf(
    entries: list[tuple[int, int, int, bytes]],
    endian: str = "<",
    root_off: int = 8,
    *,
    header_off: int | None = None,
) -> bytes:
    magic = b"II*\x00" if endian == "<" else b"MM\x00*"
    ifd_pos = root_off
    ifd_size = 2 + len(entries) * 12 + 4
    cursor = ifd_pos + ifd_size
    ifd = bytearray(struct.pack(endian + "H", len(entries)))
    extra = bytearray()
    for tag, typ, cnt, val in entries:
        if len(val) <= 4:
            vfield = val.ljust(4, b"\x00")
        else:
            vfield = struct.pack(endian + "I", cursor)
            extra.extend(val)
            cursor += len(val)
        ifd.extend(struct.pack(endian + "HHI", tag, typ, cnt) + vfield)
    ifd.extend(struct.pack(endian + "I", 0))
    pad = b"\x00" * max(0, ifd_pos - 8)
    written_off = root_off if header_off is None else header_off
    return b"MPF\x00" + magic + struct.pack(endian + "I", written_off) + pad + bytes(ifd) + bytes(extra)


def _pack_bigtiff_mpf(
    entries: list[tuple[int, int, int, bytes]],
    endian: str = "<",
    root_off: int = 16,
    *,
    header_off: int | None = None,
) -> bytes:
    magic = b"II+\x00" if endian == "<" else b"MM\x00+"
    written_off = root_off if header_off is None else header_off
    hdr_tail = struct.pack(endian + "HHQ", 8, 0, written_off)
    ifd_size = 8 + len(entries) * 20 + 8
    cursor = root_off + ifd_size
    ifd = bytearray(struct.pack(endian + "Q", len(entries)))
    extra = bytearray()
    for tag, typ, cnt, val in entries:
        if len(val) <= 8:
            vfield = val.ljust(8, b"\x00")
        else:
            vfield = struct.pack(endian + "Q", cursor)
            extra.extend(val)
            cursor += len(val)
        ifd.extend(struct.pack(endian + "HHQ", tag, typ, cnt) + vfield)
    ifd.extend(struct.pack(endian + "Q", 0))
    pad = b"\x00" * max(0, root_off - 16)
    return b"MPF\x00" + magic + hdr_tail + pad + bytes(ifd) + bytes(extra)


def _mp_entries(count: int, *, fmt_bits: int = 0) -> bytes:
    out = bytearray()
    for _ in range(count):
        attr = (fmt_bits & 7) << 24
        out.extend(struct.pack("<LLLHH", attr, 1024, 0, 0, 0))
    return bytes(out)


def _typed_value(typ: int, count: int, endian: str = "<") -> bytes:
    sz = _UNIT_SIZES[typ] * count
    if sz == 0:
        return b""
    if typ == 2:
        return (b"2" * max(0, sz - 1)) + b"\x00"
    if typ == 1 or typ == 7 or typ == 6:
        return bytes([(2 if i == 0 else i) & 0x7F for i in range(sz)])
    if typ in (3, 8):
        return b"".join(struct.pack(endian + ("H" if typ == 3 else "h"), 2) for _ in range(count))
    if typ in (4, 9):
        return b"".join(struct.pack(endian + ("I" if typ == 4 else "i"), 2) for _ in range(count))
    if typ in (5, 10):
        fmt = endian + ("II" if typ == 5 else "ii")
        return b"".join(struct.pack(fmt, 2, 1) for _ in range(count))
    if typ == 11:
        return b"".join(struct.pack(endian + "f", 2.0) for _ in range(count))
    if typ == 12:
        return b"".join(struct.pack(endian + "d", 2.0) for _ in range(count))
    return b"\x00" * sz


def sol_r3_cases() -> list[SweepCase]:
    """All 12 Sol r3 synthetic fixtures plus valid MPF 16/32-byte controls."""
    pre, sof, post = _base_parts()
    minimal_sof = _SOF0_576X384
    minimal_sos = _SOS_1C

    def minimal(body: bytes) -> bytes:
        return b"\xff\xd8" + body

    q2_t1_1 = _pack_classic_mpf([(0xB001, 4, 2, struct.pack("<II", 1, 2)), (0xB002, 1, 1, b"\x00")])
    q2_t1_15 = _pack_classic_mpf([(0xB001, 4, 2, struct.pack("<II", 1, 2)), (0xB002, 1, 15, b"\x00" * 15)])
    q2_t7_1 = _pack_classic_mpf([(0xB001, 4, 2, struct.pack("<II", 1, 2)), (0xB002, 7, 1, b"\x00")])
    q2_t7_15 = _pack_classic_mpf([(0xB001, 4, 2, struct.pack("<II", 1, 2)), (0xB002, 7, 15, b"\x00" * 15)])
    q2_t7_16 = _pack_classic_mpf([(0xB001, 4, 2, struct.pack("<II", 1, 2)), (0xB002, 7, 16, _mp_entries(1))])
    big_short = _pack_bigtiff_mpf([(0xB001, 4, 1, struct.pack("<Q", 2)), (0xB002, 7, 1, b"\x00")])
    classic_short = _pack_classic_mpf([(0xB001, 4, 1, struct.pack("<I", 2)), (0xB002, 7, 1, b"\x00")])

    return [
        SweepCase("sol_r3_marker_01_before", "sol_r3", minimal(b"\xff\x01" + minimal_sof + minimal_sos)),
        SweepCase("sol_r3_marker_01_after", "sol_r3", minimal(minimal_sof + b"\xff\x01" + minimal_sos)),
        SweepCase("sol_r3_marker_d9_before", "sol_r3", minimal(b"\xff\xd9" + minimal_sof + minimal_sos)),
        SweepCase("sol_r3_marker_d9_after", "sol_r3", minimal(minimal_sof + b"\xff\xd9" + minimal_sos)),
        SweepCase("sol_r3_full_marker_01_before_sof", "sol_r3", b"\xff\xd8" + pre + b"\xff\x01" + sof + post),
        SweepCase("sol_r3_full_marker_d9_before_sof", "sol_r3", b"\xff\xd8" + pre + b"\xff\xd9" + sof + post),
        SweepCase("sol_r3_full_marker_01_after_sof", "sol_r3", b"\xff\xd8" + pre + sof + b"\xff\x01" + post),
        SweepCase("sol_r3_full_marker_d9_after_sof", "sol_r3", b"\xff\xd8" + pre + sof + b"\xff\xd9" + post),
        SweepCase("sol_r3_mpf_q2_type1_count1", "sol_r3", minimal(seg(0xE2, q2_t1_1) + minimal_sof + minimal_sos)),
        SweepCase("sol_r3_mpf_q2_type1_count15", "sol_r3", minimal(seg(0xE2, q2_t1_15) + minimal_sof + minimal_sos)),
        SweepCase("sol_r3_mpf_q2_type7_count1", "sol_r3", minimal(seg(0xE2, q2_t7_1) + minimal_sof + minimal_sos)),
        SweepCase("sol_r3_mpf_q2_type7_count15", "sol_r3", minimal(seg(0xE2, q2_t7_15) + minimal_sof + minimal_sos)),
        SweepCase("sol_r3_mpf_q2_type7_count16_ok", "sol_r3", minimal(seg(0xE2, q2_t7_16) + minimal_sof + minimal_sos)),
        SweepCase("sol_r3_mpf_bigtiff_short_entry", "sol_r3", minimal(seg(0xE2, big_short) + minimal_sof + minimal_sos)),
        SweepCase("sol_r3_mpf_short_with_bomb_sof", "sol_r3", minimal(seg(0xE2, classic_short) + _SOF0_BOMB + minimal_sos)),
    ]


def marker_sweep_cases(*, fast: bool = False) -> list[SweepCase]:
    """Every marker 0xFF00..0xFFFF with/without length inserted after SOI, before SOF, and after SOF."""
    pre, sof, post = _base_parts()
    if fast:
        codes = [
            0x00,
            0x01,
            0x02,
            0x7F,
            0xBF,
            *range(0xC0, 0xDF),
            0xDF,
            0xE0,
            0xE1,
            0xE2,
            0xED,
            0xEE,
            0xEF,
            0xF0,
            0xFD,
            0xFE,
            0xFF,
        ]
    else:
        codes = list(range(0x00, 0x100))

    cases: list[SweepCase] = []
    for code in codes:
        for mode, chunk in (("no_len", bytes([0xFF, code])), ("with_len", bytes([0xFF, code, 0x00, 0x04, 0x00, 0x00]))):
            cases.append(SweepCase(f"m_ff{code:02x}_{mode}_after_soi", "markers", b"\xff\xd8" + chunk + pre + sof + post))
            cases.append(SweepCase(f"m_ff{code:02x}_{mode}_before_sof", "markers", b"\xff\xd8" + pre + chunk + sof + post))
            cases.append(SweepCase(f"m_ff{code:02x}_{mode}_after_sof", "markers", b"\xff\xd8" + pre + sof + chunk + post))
    return cases


def mpf_tag_sweep_cases(*, fast: bool = False) -> list[SweepCase]:
    """MPF tags 0xB000..0xB002 with types 1..12, counts 0..3, and entry tables 0..33 bytes."""
    pre, sof, post = _base_parts()
    cases: list[SweepCase] = []

    def wrap(mpf: bytes) -> bytes:
        return b"\xff\xd8" + pre + seg(0xE2, mpf) + sof + post

    types = (1, 2, 3, 4, 5, 7, 11) if fast else tuple(range(1, 13))
    counts = (0, 1, 2) if fast else (0, 1, 2, 3)
    for target in (0xB000, 0xB001, 0xB002):
        for typ in types:
            for cnt in counts:
                val = _typed_value(typ, cnt, "<")
                for ctx_name, entry_len in (("short_ctx", 1), ("ok_ctx", 32)):
                    entries: list[tuple[int, int, int, bytes]] = []
                    if target != 0xB000:
                        entries.append((0xB000, 7, 4, b"0100"))
                    if target != 0xB001:
                        entries.append((0xB001, 4, 1, struct.pack("<I", 2)))
                    if target != 0xB002:
                        entries.append((0xB002, 7, entry_len, _mp_entries(2)[:entry_len] if entry_len >= 32 else b"\x00" * entry_len))
                    entries.append((target, typ, cnt, val))
                    entries.sort(key=lambda e: e[0])
                    mpf = _pack_classic_mpf(entries, "<")
                    cases.append(SweepCase(f"mpf_t{target:04x}_ty{typ}_c{cnt}_{ctx_name}", "mpf_tags", wrap(mpf)))

    lengths = (0, 1, 15, 16, 17, 31, 32, 33) if fast else tuple(range(0, 34))
    n_imgs = (0, 1, 2) if fast else (0, 1, 2, 3)
    for b001 in n_imgs:
        for typ in (1, 7):
            for elen in lengths:
                mpf = _pack_classic_mpf(
                    [(0xB000, 7, 4, b"0100"), (0xB001, 4, 1, struct.pack("<I", b001)), (0xB002, typ, elen, b"\x00" * elen)],
                    "<",
                )
                cases.append(SweepCase(f"mpf_tbl_n{b001}_ty{typ}_l{elen}", "mpf_tags", wrap(mpf)))

    bad_fmt = _pack_classic_mpf(
        [(0xB000, 7, 4, b"0100"), (0xB001, 4, 1, struct.pack("<I", 2)), (0xB002, 7, 32, _mp_entries(2, fmt_bits=1))],
        "<",
    )
    ok_fmt = _pack_classic_mpf(
        [(0xB000, 7, 4, b"0100"), (0xB001, 4, 1, struct.pack("<I", 2)), (0xB002, 7, 32, _mp_entries(2, fmt_bits=0))],
        "<",
    )
    cases.append(SweepCase("mpf_fmt_nonzero_rejected_fallback", "mpf_tags", wrap(bad_fmt)))
    cases.append(SweepCase("mpf_fmt_zero_valid", "mpf_tags", wrap(ok_fmt)))
    return cases


def mpf_header_sweep_cases() -> list[SweepCase]:
    """Classic and BigTIFF MPF headers in LE/BE with root offsets in and out of range."""
    pre, sof, post = _base_parts()
    cases: list[SweepCase] = []

    def wrap(mpf: bytes) -> bytes:
        return b"\xff\xd8" + pre + seg(0xE2, mpf) + sof + post

    for kind in ("classic", "bigtiff"):
        for endian, ename in (("<", "le"), (">", "be")):
            packer = _pack_classic_mpf if kind == "classic" else _pack_bigtiff_mpf
            hdr_off = 8 if kind == "classic" else 16
            e_short = [(0xB001, 4, 1, struct.pack(endian + "I" if kind == "classic" else endian + "Q", 2)), (0xB002, 7, 1, b"\x00")]
            e_valid = [(0xB001, 4, 1, struct.pack(endian + "I" if kind == "classic" else endian + "Q", 2)), (0xB002, 7, 32, _mp_entries(2))]
            e_badfmt = [
                (0xB001, 4, 1, struct.pack(endian + "I" if kind == "classic" else endian + "Q", 2)),
                (0xB002, 7, 32, _mp_entries(2, fmt_bits=2)),
            ]

            cases.append(SweepCase(f"hdr_{kind}_{ename}_inrange_short", "mpf_headers", wrap(packer(e_short, endian, hdr_off))))
            cases.append(SweepCase(f"hdr_{kind}_{ename}_inrange_valid", "mpf_headers", wrap(packer(e_valid, endian, hdr_off))))
            cases.append(SweepCase(f"hdr_{kind}_{ename}_inrange_badfmt", "mpf_headers", wrap(packer(e_badfmt, endian, hdr_off))))

            # Offset shifted further in range, exact EOF, and out-of-range
            shifted = packer(e_short, endian, hdr_off + 12)
            cases.append(SweepCase(f"hdr_{kind}_{ename}_shifted", "mpf_headers", wrap(shifted)))
            full_len = len(packer(e_short, endian, hdr_off)) - 4
            cases.append(SweepCase(f"hdr_{kind}_{ename}_eof_off", "mpf_headers", wrap(packer(e_short, endian, hdr_off, header_off=full_len))))
            cases.append(
                SweepCase(f"hdr_{kind}_{ename}_oob_off", "mpf_headers", wrap(packer(e_short, endian, hdr_off, header_off=0x0010_0000)))
            )
            cases.append(SweepCase(f"hdr_{kind}_{ename}_zero_off", "mpf_headers", wrap(packer(e_short, endian, hdr_off, header_off=0))))

    cases.append(SweepCase("hdr_short_3bytes", "mpf_headers", wrap(b"MPF\x00II*")))
    cases.append(SweepCase("hdr_bad_magic", "mpf_headers", wrap(b"MPF\x00BADMAGIC0000")))
    cases.append(SweepCase("hdr_bigtiff_12bytes", "mpf_headers", wrap(b"MPF\x00II+\x00\x08\x00\x00\x00\x10\x00\x00\x00")))
    return cases


def failure_order_sweep_cases() -> list[SweepCase]:
    """Interaction ordering: MPF vs pixel bomb, zero-size SOF vs pixel bomb, APP checks vs SOF."""
    pre, sof, post = _base_parts()
    mp_short = seg(0xE2, _pack_classic_mpf([(0xB001, 4, 1, struct.pack("<I", 2)), (0xB002, 7, 1, b"\x00")], "<"))
    mp_big = seg(0xE2, _pack_bigtiff_mpf([(0xB001, 4, 1, struct.pack("<Q", 2)), (0xB002, 7, 1, b"\x00")], "<"))
    mp_no_b002 = seg(0xE2, _pack_classic_mpf([(0xB001, 4, 1, struct.pack("<I", 2))], "<"))
    app_bad_jfif = seg(0xE0, b"JFIF\x00\x01")
    app_bad_adobe = seg(0xEE, b"Adobe\x00")
    app_bad_icc = seg(0xE2, b"ICC_PROFILE\x00\x01")
    app_bad_ps = seg(0xED, b"Photoshop 3.0\x008BIM\x03\xed")
    dqt_bad = seg(0xDB, b"\x00\x00")

    cases: list[SweepCase] = []
    # MPF vs SOF variants
    for mname, mp in (("mp_short", mp_short), ("mp_big", mp_big), ("mp_no_b002", mp_no_b002)):
        for sname, s in (
            ("ok_sof", sof),
            ("bomb_sof", _SOF0_BOMB),
            ("zero_w_sof", _SOF0_ZERO_W),
            ("zero_h_sof", _SOF0_ZERO_H),
            ("bad_bits_sof", _SOF0_BAD_BITS),
            ("bad_layers_sof", _SOF0_BAD_LAYERS),
            ("bad_tail_sof", _SOF0_BAD_TAIL),
            ("missing_sof", b""),
        ):
            cases.append(SweepCase(f"ord_{mname}_{sname}", "failure_order", b"\xff\xd8" + pre + mp + s + post))

    # Zero-size SOF vs pixel bomb SOF (both orders) + valid SOF after zero-size SOF
    cases.append(SweepCase("ord_bomb_solo", "failure_order", b"\xff\xd8" + pre + _SOF0_BOMB + post))
    cases.append(SweepCase("ord_zero_w_solo", "failure_order", b"\xff\xd8" + pre + _SOF0_ZERO_W + post))
    cases.append(SweepCase("ord_zero_h_solo", "failure_order", b"\xff\xd8" + pre + _SOF0_ZERO_H + post))
    cases.append(SweepCase("ord_zero_then_bomb", "failure_order", b"\xff\xd8" + pre + _SOF0_ZERO_W + _SOF0_BOMB + post))
    cases.append(SweepCase("ord_bomb_then_zero", "failure_order", b"\xff\xd8" + pre + _SOF0_BOMB + _SOF0_ZERO_W + post))
    cases.append(SweepCase("ord_zero_then_ok", "failure_order", b"\xff\xd8" + pre + _SOF0_ZERO_W + sof + post))
    cases.append(SweepCase("ord_ok_then_zero", "failure_order", b"\xff\xd8" + pre + sof + _SOF0_ZERO_W + post))
    cases.append(SweepCase("ord_bomb_then_ok", "failure_order", b"\xff\xd8" + pre + _SOF0_BOMB + sof + post))
    cases.append(SweepCase("ord_ok_then_bomb", "failure_order", b"\xff\xd8" + pre + sof + _SOF0_BOMB + post))

    # APP checks vs SOF (before/after SOF)
    for aname, app in (
        ("bad_jfif", app_bad_jfif),
        ("bad_adobe", app_bad_adobe),
        ("bad_icc", app_bad_icc),
        ("bad_ps", app_bad_ps),
        ("bad_dqt", dqt_bad),
    ):
        cases.append(SweepCase(f"ord_{aname}_before_ok_sof", "failure_order", b"\xff\xd8" + pre + app + sof + post))
        cases.append(SweepCase(f"ord_{aname}_before_bomb_sof", "failure_order", b"\xff\xd8" + pre + app + _SOF0_BOMB + post))
        cases.append(SweepCase(f"ord_{aname}_after_ok_sof", "failure_order", b"\xff\xd8" + pre + sof + app + post))
        cases.append(SweepCase(f"ord_{aname}_before_bad_bits", "failure_order", b"\xff\xd8" + pre + app + _SOF0_BAD_BITS + post))
        cases.append(SweepCase(f"ord_bad_bits_before_{aname}", "failure_order", b"\xff\xd8" + pre + _SOF0_BAD_BITS + app + post))

    return cases


def _three_truncation_headers() -> list[tuple[str, bytes]]:
    app0 = seg(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    exif_le = seg(0xE1, b"Exif\x00\x00" + _synthetic_exif_tiff("<", copy_suffix="HDR1" + "A" * 80))
    exif_be = seg(0xE1, b"Exif\x00\x00" + _synthetic_exif_tiff(">", copy_suffix="HDR2" + "B" * 80))
    xmp = seg(0xE1, b'http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta><rdf:RDF><rdf:Description tiff:Orientation="1"/></rdf:RDF></x:xmpmeta>')
    icc = seg(0xE2, b"ICC_PROFILE\x00\x01\x01" + b"\x00" * 64)
    mpf_ok = seg(0xE2, _pack_classic_mpf([(0xB000, 7, 4, b"0100"), (0xB001, 4, 1, struct.pack("<I", 2)), (0xB002, 7, 32, _mp_entries(2))], "<"))
    ps = seg(0xED, b"Photoshop 3.0\x008BIM\x03\xed\x00\x00" + struct.pack(">I", 16) + b"\x00" * 16)
    adobe = seg(0xEE, b"Adobe\x00\x64\x00\x00\x00\x00\x01")
    dqt_8 = seg(0xDB, b"\x00" + bytes(range(1, 65)))
    dqt_16 = seg(0xDB, b"\x10" + b"\x00\x02" * 64)
    com = seg(0xFE, b"SYNTHETIC_HEADER_PADDING_" * 4)
    sof2 = b"\xff\xc2\x00\x11\x08\x00\x80\x00\xc0\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"

    # Split APP1 Exif into two segments to exercise multi-APP1 concatenation during truncation
    raw_exif = _synthetic_exif_tiff("<", copy_suffix="HDR3" + "C" * 40)
    app1_a = seg(0xE1, b"Exif\x00\x00" + raw_exif[:60])
    app1_b = seg(0xE1, b"Exif\x00\x00" + raw_exif[60:])

    h1 = b"\xff\xd8" + app0 + exif_le + xmp + dqt_8 + com + _SOF0_192X128 + _SOS_1C + _EOI
    h2 = b"\xff\xd8" + exif_be + icc + mpf_ok + ps + com + dqt_8 + sof2 + _SOS_1C + _EOI
    h3 = b"\xff\xd8" + app0 + app1_a + app1_b + adobe + dqt_16 + _SOF0_192X128 + _SOS_1C + _EOI
    for name, hdr in (("std", h1), ("mpf_ps", h2), ("multi_app", h3)):
        assert len(hdr) >= 512, (name, len(hdr))
    return [("std", h1), ("mpf_ps", h2), ("multi_app", h3)]


def truncation_sweep_cases(*, fast: bool = False) -> list[SweepCase]:
    """Truncation at every byte of the first 512 bytes of three synthetic headers."""
    cases: list[SweepCase] = []
    for hname, hdr in _three_truncation_headers():
        cuts: tuple[int, ...] = (
            (0, 1, 2, 3, 4, 5, 16, 17, 19, 20, 64, 128, 256, 384, len(hdr) - 10, len(hdr) - 3, len(hdr) - 2, 512)
            if fast
            else tuple(range(1, 513))
        )
        for cut in sorted(set(c for c in cuts if c <= len(hdr))):
            cases.append(SweepCase(f"trunc_{hname}_b{cut:03d}", "truncation", hdr[:cut]))
    return cases


def build_sweep_cases(*, fast: bool = False) -> list[SweepCase]:
    """Return all deterministic synthetic JPEG header cases."""
    return [
        *sol_r3_cases(),
        *marker_sweep_cases(fast=fast),
        *mpf_tag_sweep_cases(fast=fast),
        *mpf_header_sweep_cases(),
        *failure_order_sweep_cases(),
        *truncation_sweep_cases(fast=fast),
    ]
