"""Synthetic TIFF/DNG/JPEG fixture builders for `test_core_parity_export_meta.py`."""

from __future__ import annotations

import io
import struct

from PIL import Image


def pack_ifd(entries: list[tuple[int, int, int, bytes]], next_ifd: int = 0, endian: str = "<") -> bytes:
    buf = bytearray(struct.pack(endian + "H", len(entries)))
    for tag, typ, cnt, val4 in entries:
        assert len(val4) <= 4
        buf += struct.pack(endian + "HHI", tag, typ, cnt) + val4.ljust(4, b"\x00")
    buf += struct.pack(endian + "I", next_ifd)
    return bytes(buf)


def make_tiff_doc(
    ifd0_specs: list[tuple[int, int, int, bytes]],
    sub_specs: list[tuple[int, int, int, bytes]] | None = None,
    *,
    endian: str = "<",
    subifd_list: list[list[tuple[int, int, int, bytes]]] | None = None,
) -> bytes:
    """Build a valid TIFF with optional EXIF SubIFD (`0x8769`) and TIFF SubIFDs (`330`)."""
    magic = b"II*\x00" if endian == "<" else b"MM\x00*"
    ifd0_offset = 8
    extra_ifd0_count = int(sub_specs is not None) + int(subifd_list is not None)
    ifd0_total_entries = len(ifd0_specs) + extra_ifd0_count
    ifd0_size = 2 + ifd0_total_entries * 12 + 4
    cursor = ifd0_offset + ifd0_size
    blob = bytearray()

    def alloc_extra(payload: bytes) -> bytes:
        nonlocal cursor
        if len(payload) <= 4:
            return payload.ljust(4, b"\x00")
        off = cursor
        blob.extend(payload)
        cursor += len(payload)
        return struct.pack(endian + "I", off)

    final_ifd0: list[tuple[int, int, int, bytes]] = []
    for tag, typ, cnt, data in ifd0_specs:
        final_ifd0.append((tag, typ, cnt, alloc_extra(data)))

    if sub_specs is not None:
        sub_offset = cursor
        sub_size = 2 + len(sub_specs) * 12 + 4
        cursor += sub_size
        sub_blob = bytearray()
        final_sub: list[tuple[int, int, int, bytes]] = []
        for tag, typ, cnt, data in sub_specs:
            if len(data) <= 4:
                final_sub.append((tag, typ, cnt, data.ljust(4, b"\x00")))
            else:
                off = cursor
                sub_blob.extend(data)
                cursor += len(data)
                final_sub.append((tag, typ, cnt, struct.pack(endian + "I", off)))
        blob.extend(pack_ifd(final_sub, 0, endian))
        blob.extend(sub_blob)
        final_ifd0.append((0x8769, 4, 1, struct.pack(endian + "I", sub_offset)))

    if subifd_list is not None:
        subifd_offsets: list[int] = []
        for child_specs in subifd_list:
            child_off = cursor
            subifd_offsets.append(child_off)
            child_size = 2 + len(child_specs) * 12 + 4
            cursor += child_size
            child_blob = bytearray()
            final_child: list[tuple[int, int, int, bytes]] = []
            for tag, typ, cnt, data in child_specs:
                if len(data) <= 4:
                    final_child.append((tag, typ, cnt, data.ljust(4, b"\x00")))
                else:
                    off = cursor
                    child_blob.extend(data)
                    cursor += len(data)
                    final_child.append((tag, typ, cnt, struct.pack(endian + "I", off)))
            blob.extend(pack_ifd(final_child, 0, endian))
            blob.extend(child_blob)
        offs_bytes = b"".join(struct.pack(endian + "I", o) for o in subifd_offsets)
        final_ifd0.append((330, 4, len(subifd_offsets), alloc_extra(offs_bytes)))

    header = magic + struct.pack(endian + "I", ifd0_offset)
    return header + pack_ifd(final_ifd0, 0, endian) + bytes(blob)


def make_jpeg_bytes(w: int = 8, h: int = 6, *, exif_payload: bytes | None = None, xmp: bytes | None = None) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buf, "JPEG", quality=90)
    raw = buf.getvalue()
    if exif_payload is None and xmp is None:
        return raw
    inserts = bytearray()
    if exif_payload is not None:
        seg = b"Exif\x00\x00" + exif_payload
        inserts += b"\xff\xe1" + struct.pack(">H", len(seg) + 2) + seg
    if xmp is not None:
        seg = b"http://ns.adobe.com/xap/1.0/\x00" + xmp
        inserts += b"\xff\xe1" + struct.pack(">H", len(seg) + 2) + seg
    return raw[:2] + bytes(inserts) + raw[2:]


def synthetic_edge_cases() -> dict[str, tuple[bytes, str]]:
    """Build synthetic TIFF/JPEG buffers covering Pillow edge cases and damaged inputs."""
    cases: dict[str, tuple[bytes, str]] = {}

    def base_tiff(e: str) -> list[tuple[int, int, int, bytes]]:
        return [
            (256, 4, 1, struct.pack(e + "I", 16)),
            (257, 4, 1, struct.pack(e + "I", 12)),
            (273, 4, 1, struct.pack(e + "I", 8)),
            (279, 4, 1, struct.pack(e + "I", 16)),
        ]

    # 1. Little-endian + ASCII NULs + odd length + latin1 + rationals (including 0 denom) + arrays + MakerNote + shared tag IDs
    le_ifd0 = base_tiff("<") + [
        (271, 2, 10, b"  RICOH \x00\x00"),
        (272, 2, 5, b"GXR\x00 "),
        (306, 2, 20, b"2026:05:06 07:08:09\x00"),
        (33432, 2, 9, b"\xa9 2026\x00\x00\x00"),
        (50708, 2, 26, b"RICOH GR LENS A12 50mm   \x00"),
        (274, 3, 1, struct.pack("<H", 6)),
    ]
    le_sub = [
        (36867, 2, 20, b"2026:05:06 07:08:09\x00"),
        (34855, 3, 1, struct.pack("<H", 400)),
        (33434, 5, 1, struct.pack("<II", 1, 250)),
        (33437, 5, 1, struct.pack("<II", 25, 10)),
        (37386, 5, 1, struct.pack("<II", 5, 0)),
        (37380, 10, 1, struct.pack("<ii", -3, 0)),
        (37396, 3, 4, struct.pack("<HHHH", 10, 20, 30, 40)),
        (42034, 5, 4, struct.pack("<IIIIIIII", 50, 1, 50, 1, 25, 10, 22, 1)),
        (37500, 7, 6, b"MKNOTE"),
        (36864, 7, 4, b"0230"),
        (41728, 7, 1, b"\x03"),
        (271, 2, 8, b"SUB_TAG\x00"),
        (42036, 2, 4, b"   \x00"),
    ]
    cases["le_edges.dng"] = (make_tiff_doc(le_ifd0, le_sub, endian="<"), "dng")

    # 2. Big-endian + count=0 + unknown type + repeated tag (last wins) + float/double + sbyte/slong + 69-char GXR1 recipe tag
    recipe_69 = b"GXR1 6a3a5a3c02 28 06 A8999B999-".ljust(69, b" ") + b"\x00"
    be_ifd0 = base_tiff(">") + [
        (271, 2, 6, b"RICOH\x00"),
        (271, 2, 7, b"PENTAX\x00"),
        (33432, 2, len(recipe_69), recipe_69),
        (50708, 2, 17, b"RICOH A16 24-85\x00\x00"),
        (40001, 3, 0, b"\x00\x00\x00\x00"),
        (40002, 99, 1, b"\x00\x00\x00\x01"),
    ]
    be_sub = [
        (36867, 2, 20, b"2024-02-29 12:34:56\x00"),
        (34855, 3, 2, struct.pack(">HH", 800, 1600)),
        (33434, 5, 1, struct.pack(">II", 2, 5)),
        (33437, 5, 1, struct.pack(">II", 0, 0)),
        (40003, 9, 1, struct.pack(">i", -42)),
        (40004, 12, 1, struct.pack(">d", 1.25)),
    ]
    cases["be_edges.dng"] = (make_tiff_doc(be_ifd0, be_sub, endian=">"), "dng")

    # 3. SubIFD crop size (50720) + preview SubIFD (largest JPEG wins, skip invalid SOF3 / >32MB)
    small_jpg = make_jpeg_bytes(4, 4)
    big_jpg = make_jpeg_bytes(16, 12)
    sof3_jpg = b"\xff\xd8\xff\xc3\x00\x0b\x0c\x00\x64\x00\x64\x01\x01\x11\x00\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xff\xd9"
    subifds = [
        [
            (254, 4, 1, struct.pack("<I", 0)),
            (256, 4, 1, struct.pack("<I", 220)),
            (257, 4, 1, struct.pack("<I", 120)),
            (50720, 4, 2, struct.pack("<II", 200, 100)),
        ],
        [
            (254, 4, 1, struct.pack("<I", 1)),
            (256, 4, 1, struct.pack("<I", 4)),
            (257, 4, 1, struct.pack("<I", 4)),
            (259, 3, 1, struct.pack("<H", 7)),
            (273, 4, 1, struct.pack("<I", 8 + 5000)),
            (279, 4, 1, struct.pack("<I", len(small_jpg))),
        ],
        [
            (254, 4, 1, struct.pack("<I", 1)),
            (256, 4, 1, struct.pack("<I", 16)),
            (257, 4, 1, struct.pack("<I", 12)),
            (259, 3, 1, struct.pack("<H", 7)),
            (273, 4, 1, struct.pack("<I", 999999)),
            (279, 4, 1, struct.pack("<I", len(big_jpg))),
        ],
        [
            (254, 4, 1, struct.pack("<I", 1)),
            (256, 4, 1, struct.pack("<I", 999)),
            (257, 4, 1, struct.pack("<I", 999)),
            (259, 3, 1, struct.pack("<H", 7)),
            (273, 4, 1, struct.pack("<I", 999999)),
            (279, 4, 1, struct.pack("<I", 33 * 1024 * 1024)),
        ],
    ]
    dng_pv = bytearray(make_tiff_doc(base_tiff("<") + [(274, 3, 1, struct.pack("<H", 6))], subifd_list=subifds))
    dng_pv.extend(big_jpg)
    cases["subifd_preview_oob.dng"] = (bytes(dng_pv), "dng")

    p_hdr = b"II*\x00" + struct.pack("<I", 8)
    ifd0_sz = 2 + 5 * 12 + 4
    sub_off = 8 + ifd0_sz
    sub_sz = 2 + 6 * 12 + 4
    jpg_off = sub_off + sub_sz
    p_ifd0 = pack_ifd(base_tiff("<") + [(330, 4, 1, struct.pack("<I", sub_off))], 0, "<")
    p_sub = pack_ifd(
        [
            (254, 4, 1, struct.pack("<I", 1)),
            (256, 4, 1, struct.pack("<I", 16)),
            (257, 4, 1, struct.pack("<I", 12)),
            (259, 3, 1, struct.pack("<H", 7)),
            (273, 4, 1, struct.pack("<I", jpg_off)),
            (279, 4, 1, struct.pack("<I", len(big_jpg))),
        ],
        0,
        "<",
    )
    cases["valid_preview.dng"] = (p_hdr + p_ifd0 + p_sub + big_jpg, "dng")

    p_sub3 = pack_ifd(
        [
            (254, 4, 1, struct.pack("<I", 1)),
            (256, 4, 1, struct.pack("<I", 100)),
            (257, 4, 1, struct.pack("<I", 100)),
            (259, 3, 1, struct.pack("<H", 7)),
            (273, 4, 1, struct.pack("<I", jpg_off)),
            (279, 4, 1, struct.pack("<I", len(sof3_jpg))),
        ],
        0,
        "<",
    )
    cases["sof3_preview.dng"] = (p_hdr + p_ifd0 + p_sub3 + sof3_jpg, "dng")

    # 4. JPEG with multi-APP1 concatenation + XMP orientation fallback + shutter >= 1s
    exif_tiff = make_tiff_doc(
        [(271, 2, 6, b"RICOH\x00"), (306, 2, 17, b"2026:01:02 03:04\x00")],
        [
            (34855, 3, 1, struct.pack("<H", 200)),
            (33434, 5, 1, struct.pack("<II", 25, 10)),
            (33437, 5, 1, struct.pack("<II", 20, 10)),
        ],
    )
    xmp = b'<x:xmpmeta><rdf:RDF><rdf:Description tiff:Orientation="8"/></rdf:RDF></x:xmpmeta>'
    cases["xmp_orient.jpg"] = (make_jpeg_bytes(10, 6, exif_payload=exif_tiff, xmp=xmp), "jpg")

    split_exif = make_tiff_doc(
        [(271, 2, 6, b"RICOH\x00"), (306, 2, 20, b"1994:05:06 07:08:09\x00")],
        [(34855, 3, 1, struct.pack("<H", 0))],
    )
    mid = len(split_exif) // 2
    base_jpg = make_jpeg_bytes(8, 6)
    s1 = b"Exif\x00\x00" + split_exif[:mid]
    s2 = b"Exif\x00\x00" + split_exif[mid:]
    multi_app1 = (
        base_jpg[:2] + b"\xff\xe1" + struct.pack(">H", len(s1) + 2) + s1 + b"\xff\xe1" + struct.pack(">H", len(s2) + 2) + s2 + base_jpg[2:]
    )
    cases["multi_app1_iso0.jpg"] = (multi_app1, "jpg")

    # 5. Damaged / hostile files
    oob_ifd0 = base_tiff("<") + [
        (271, 2, 4, b"OK1\x00"),
        (272, 2, 100, struct.pack("<I", 999999)),
        (306, 2, 4, b"SKP\x00"),
    ]
    cases["oob_val.dng"] = (b"II*\x00" + struct.pack("<I", 8) + pack_ifd(oob_ifd0, 0, "<"), "dng")

    loop_ifd0 = base_tiff("<") + [(271, 2, 4, b"LOP\x00"), (0x8769, 4, 1, struct.pack("<I", 8))]
    cases["self_loop_0x8769.dng"] = (b"II*\x00" + struct.pack("<I", 8) + pack_ifd(loop_ifd0, 0, "<"), "dng")

    cases["huge_ifd_count.dng"] = (
        b"II*\x00\x08\x00\x00\x00\xff\xff" + struct.pack("<HHI", 256, 4, 1) + struct.pack("<I", 4),
        "dng",
    )

    huge_val_cnt = base_tiff("<") + [(271, 5, 0x4000_0000, struct.pack("<I", 8))]
    cases["huge_tag_count.dng"] = (b"II*\x00" + struct.pack("<I", 8) + pack_ifd(huge_val_cnt, 0, "<"), "dng")

    cases["no_sos.jpg"] = (b"\xff\xd8\xff\xc0\x00\x0b\x08\x00\x02\x00\x03\x01\x01\x11\x00\xff\xd9", "jpg")
    cases["garbage.jpg"] = (b"NOT_AN_IMAGE_1234", "jpg")
    cases["empty.dng"] = (b"", "dng")

    # 6. Sol r1 preview validation & candidate selection cases
    def build_preview_dng(subifds_and_payloads: list[tuple[int, int, int, bytes, int | None]]) -> bytes:
        hdr_size = 8
        child_specs: list[list[tuple[int, int, int, bytes]]] = []
        ifd0_specs = base_tiff("<")
        ifd0_sz = 2 + (len(ifd0_specs) + 1) * 12 + 4
        offs_area_sz = 4 * len(subifds_and_payloads) if len(subifds_and_payloads) > 1 else 0
        children_start = hdr_size + ifd0_sz + offs_area_sz
        child_sz = 2 + 6 * 12 + 4
        payload_cursor = children_start + len(subifds_and_payloads) * child_sz
        payload_blobs = bytearray()
        for w, h, comp, blob, forced_off in subifds_and_payloads:
            off = payload_cursor if forced_off is None else forced_off
            child_specs.append(
                [
                    (254, 4, 1, struct.pack("<I", 1)),
                    (256, 4, 1, struct.pack("<I", w)),
                    (257, 4, 1, struct.pack("<I", h)),
                    (259, 3, 1, struct.pack("<H", comp)),
                    (273, 4, 1, struct.pack("<I", off)),
                    (279, 4, 1, struct.pack("<I", len(blob))),
                ]
            )
            if forced_off is None:
                payload_blobs.extend(blob)
                payload_cursor += len(blob)
        return make_tiff_doc(ifd0_specs, subifd_list=child_specs) + bytes(payload_blobs)

    tableless = b"\xff\xd8\xff\xc0\x00\x0b\x08\x00\x06\x00\x08\x01\x01\x11\x00\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00\xff\xd9"
    valid_pv = make_jpeg_bytes(8, 6)
    small_pv = make_jpeg_bytes(4, 4)
    prog_buf = io.BytesIO()
    Image.new("RGB", (12, 10), (30, 80, 140)).save(prog_buf, "JPEG", quality=85, progressive=True)
    prog_pv = prog_buf.getvalue()

    cases["preview-no-tables.dng"] = (build_preview_dng([(8, 6, 7, tableless, None)]), "dng")
    cases["preview-valid.dng"] = (build_preview_dng([(8, 6, 7, valid_pv, None)]), "dng")
    cases["preview-no-eoi.dng"] = (build_preview_dng([(8, 6, 7, valid_pv[:-2], None)]), "dng")
    cases["preview-progressive.dng"] = (build_preview_dng([(12, 10, 7, prog_pv, None)]), "dng")
    cases["preview-choice-largest.dng"] = (
        build_preview_dng([(4, 4, 7, small_pv, None), (8, 6, 7, valid_pv, None)]),
        "dng",
    )
    cases["preview-choice-tie-first.dng"] = (
        build_preview_dng([(8, 6, 7, valid_pv, None), (8, 6, 7, small_pv, None)]),
        "dng",
    )
    cases["preview-choice-skip-oob.dng"] = (
        build_preview_dng([(100, 100, 7, valid_pv, 999999), (4, 4, 7, small_pv, None)]),
        "dng",
    )

    # 7. ISO (34855) zero-denominator, NaN/Inf, and ASCII rules
    for e_tag, e in [("le", "<"), ("be", ">")]:
        cases[f"iso-rat-zero-{e_tag}.jpg"] = (
            make_jpeg_bytes(exif_payload=make_tiff_doc([], [(34855, 5, 2, struct.pack(e + "IIII", 1, 0, 2, 3))], endian=e)),
            "jpg",
        )
        cases[f"iso-srat-zero-{e_tag}.jpg"] = (
            make_jpeg_bytes(exif_payload=make_tiff_doc([], [(34855, 10, 2, struct.pack(e + "iiii", -1, 0, 2, 3))], endian=e)),
            "jpg",
        )
        cases[f"iso-short-pair-{e_tag}.jpg"] = (
            make_jpeg_bytes(exif_payload=make_tiff_doc([], [(34855, 3, 2, struct.pack(e + "HH", 2, 8))], endian=e)),
            "jpg",
        )
    cases["numeric-ascii-34855.jpg"] = (
        make_jpeg_bytes(exif_payload=make_tiff_doc([], [(34855, 2, 4, b"800\x00")])),
        "jpg",
    )
    cases["iso-bad-ascii.jpg"] = (
        make_jpeg_bytes(exif_payload=make_tiff_doc([], [(34855, 2, 3, b"8\x1c\x00")])),
        "jpg",
    )

    # 8. Sol r1 numeric ASCII shutter (33434) & aperture (33437)
    cases["numeric-ascii-33434.jpg"] = (
        make_jpeg_bytes(exif_payload=make_tiff_doc([], [(33434, 2, 6, b"0.004\x00")])),
        "jpg",
    )
    cases["numeric-ascii-33437.jpg"] = (
        make_jpeg_bytes(exif_payload=make_tiff_doc([], [(33437, 2, 4, b"2.8\x00")])),
        "jpg",
    )
    cases["ratio-ascii-ws-bytes.jpg"] = (
        make_jpeg_bytes(
            exif_payload=make_tiff_doc(
                [],
                [
                    (33434, 2, 8, b" 1_0e-2\x00"),
                    (33437, 7, 5, b"\x0b2.8\x0c"),
                ],
            )
        ),
        "jpg",
    )
    cases["ratio-ascii-bad-1c.jpg"] = (
        make_jpeg_bytes(exif_payload=make_tiff_doc([], [(33434, 2, 7, b"0.004\x1c\x00"), (33437, 7, 4, b"\xa02.8")])),
        "jpg",
    )
    return cases


def nonfinite_edge_cases() -> dict[str, tuple[bytes, str]]:
    """Fixtures whose raw EXIF map emits Python NaN/Infinity tokens in `gxr_raw::Json`."""
    return {
        "iso-nan.jpg": (
            make_jpeg_bytes(exif_payload=make_tiff_doc([], [(34855, 12, 1, struct.pack("<d", float("nan")))])),
            "jpg",
        ),
        "iso-inf.jpg": (
            make_jpeg_bytes(exif_payload=make_tiff_doc([], [(34855, 12, 1, struct.pack("<d", float("inf")))])),
            "jpg",
        ),
    }


def sol_mtime_tz_cases() -> list[tuple[float, int]]:
    """All 90 broad + 5 modern sub-ms rounding mtime/tz pairs from Sol r1."""
    broad_mtimes = [
        -2208988800000.0,
        -1.0005,
        -0.0005,
        0.0005,
        1.0005,
        999.9995,
        1700000000123.4565,
        1700000000123.0005,
        1700000000123.9995,
        1710053999999.0,
        1710054000000.0,
        1730613599999.0,
        1730613600000.0,
        2147483647123.0,
        253402214399999.0,
    ]
    pairs: list[tuple[float, int]] = [(ms, tz) for ms in broad_mtimes for tz in (-480, -300, -240, 0, 330, 480)]
    modern_sub_ms = [1700000000000.0 + i / 4096 for i in (1, 3, 5, 7, 9)]
    pairs.extend((ms, -300) for ms in modern_sub_ms)
    return pairs
