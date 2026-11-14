"""Parity for gxr-export::meta: source EXIF, photo display text, JPEG/DNG PhotoMeta, and preview offset.

Python + Pillow 12.2.0 is the oracle. Never log or print EXIF values from real photos.
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
import shutil
import struct
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, TiffImagePlugin

from gxr_studio.export import _DROP_EXIF_TAGS, _clean_text
from gxr_studio.finish.meta_text import photo_meta
from gxr_studio.meta import dng_preview_image, read_dng_meta, read_jpeg_meta
from support.core_cli import CoreCliError, error_of, require_cli, run_json
from support.wasm_check import PKG, run_area

_STUDIO = Path(__file__).resolve().parents[1]
_PARENT = Path(os.environ.get("GXR_STUDIO_TEST_PARENT", str(_STUDIO.parent)))
CAPTURES = _PARENT / "captures"
_CAP = 60


def _real_files(exts: tuple[str, ...], cap: int = _CAP) -> list[Path]:
    if not CAPTURES.is_dir():
        return []
    found = [p for p in CAPTURES.rglob("*") if p.is_file() and p.suffix.upper() in exts and "backup" not in p.as_posix().lower()]
    found.sort(key=lambda p: p.as_posix())
    if len(found) <= cap:
        return found
    return [found[(i * len(found)) // cap] for i in range(cap)]


REAL_DNGS = _real_files((".DNG",))
REAL_JPGS = _real_files((".JPG", ".JPEG"))


def _missing(msg: str) -> None:
    if os.environ.get("GXR_REQUIRE_CORE") == "1":
        pytest.fail(msg)
    pytest.skip(msg)


# --------------------------------------------------------------------------- Python oracles


def _py_val_to_json(val: Any) -> Any:
    if isinstance(val, TiffImagePlugin.IFDRational):
        return {"num": int(val.numerator), "den": int(val.denominator)}
    if isinstance(val, bytes):
        return {"hex": val.hex()}
    if isinstance(val, (tuple, list)):
        return [_py_val_to_json(x) for x in val]
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return int(val)
    if isinstance(val, float):
        return float(val)
    if isinstance(val, str):
        return val
    raise AssertionError(f"unexpected EXIF value type: {type(val)}")


def _py_source_exif(path: Path) -> dict[str, dict[str, Any]]:
    """Exact source-reading half of `src/gxr_studio/export.py:write_with_exif`."""
    base: dict[str, Any] = {}
    sub: dict[str, Any] = {}
    try:
        with Image.open(path) as src:
            exif = src.getexif()
            if 33432 in exif:
                base["33432"] = _py_val_to_json(exif[33432])
            for tag in (271, 272, 306):
                text = _clean_text(exif.get(tag))
                if text:
                    base[str(tag)] = text
            raw_sub = {k: v for k, v in exif.get_ifd(0x8769).items() if k not in _DROP_EXIF_TAGS}
            if not _clean_text(raw_sub.get(42036)):
                lens = _clean_text(exif.get(50708))
                if lens:
                    raw_sub[42036] = lens.removeprefix("RICOH ").strip()
            for k, v in raw_sub.items():
                sub[str(k)] = _py_val_to_json(v)
    except Exception:
        pass
    return {"base": base, "sub": sub}


def _py_photo_meta_json(
    jpg: Path | None,
    dng: Path | None,
    name: str,
    mtime_ms: float | None,
    tz_offset_min: int,
) -> dict[str, Any]:
    """Oracle mirroring `finish/meta_text.py:photo_meta` using explicit `mtime_ms` & `tz_offset_min`."""

    class _StubLib:
        @staticmethod
        def abspath(relpath: str) -> Path:
            return Path(relpath)

    files: dict[str, dict[str, str]] = {}
    if jpg is not None:
        files["jpg"] = {"relpath": str(jpg)}
    if dng is not None:
        files["dng"] = {"relpath": str(dng)}
    shot = {"name": name, "files": files}

    from unittest.mock import patch

    if mtime_ms is not None:
        tz = timezone(timedelta(minutes=tz_offset_min))
        dt = datetime.fromtimestamp(mtime_ms / 1000.0, tz=tz).replace(tzinfo=None)
    else:
        dt = None

    def _fake_exif(path: Path) -> dict[str, Any]:
        try:
            with Image.open(path) as im:
                exif = im.getexif()
                sub = dict(exif.get_ifd(0x8769))
        except (OSError, ValueError, KeyError):
            return {}
        return {"exif": exif, "sub": sub, "mtime": dt}

    with patch("gxr_studio.finish.meta_text._exif", side_effect=_fake_exif):
        frame, taken, file_time = photo_meta(_StubLib(), shot)  # type: ignore[arg-type]

    return {
        "frame": frame,
        "taken": taken.isoformat() if taken is not None else None,
        "file_time": file_time.isoformat() if file_time is not None else None,
    }


def _py_read_meta_json(path: Path, is_dng: bool) -> dict[str, Any]:
    meta = read_dng_meta(path) if is_dng else read_jpeg_meta(path)
    d = dataclasses.asdict(meta)
    d["taken"] = meta.taken.isoformat() if meta.taken is not None else None
    return d


def _assert_exif_equal(got: dict[str, Any], want: dict[str, Any], label: str) -> None:
    assert set(got.keys()) == {"base", "sub"}, label
    assert got["base"] == want["base"], f"{label}: base keys {sorted(got['base'])} vs {sorted(want['base'])}"
    assert got["sub"] == want["sub"], f"{label}: sub keys {sorted(got['sub'])} vs {sorted(want['sub'])}"


def _assert_read_meta_equal(got: dict[str, Any], want: dict[str, Any], label: str) -> None:
    if want["error"] is not None:
        assert got["error"] is not None, label
        got_kind = got["error"].split(":", 1)[0]
        want_kind = want["error"].split(":", 1)[0]
        assert got_kind == want_kind, (label, got_kind, want_kind)
    else:
        assert got["error"] is None, (label, got["error"])
    for key in ("width", "height", "orientation", "taken", "iso", "lens", "model", "copyright", "recipe_tag"):
        assert got[key] == want[key], f"{label}: field {key} mismatch"


# --------------------------------------------------------------------------- Synthetic TIFF/JPEG builders


def _pack_ifd(entries: list[tuple[int, int, int, bytes]], next_ifd: int = 0, endian: str = "<") -> bytes:
    buf = bytearray(struct.pack(endian + "H", len(entries)))
    for tag, typ, cnt, val4 in entries:
        assert len(val4) <= 4
        buf += struct.pack(endian + "HHI", tag, typ, cnt) + val4.ljust(4, b"\x00")
    buf += struct.pack(endian + "I", next_ifd)
    return bytes(buf)


def _make_tiff_doc(
    ifd0_specs: list[tuple[int, int, int, bytes]],
    sub_specs: list[tuple[int, int, int, bytes]] | None = None,
    *,
    endian: str = "<",
    subifd_list: list[list[tuple[int, int, int, bytes]]] | None = None,
) -> bytes:
    """Build a valid TIFF with optional EXIF SubIFD (`0x8769`) and TIFF SubIFDs (`330`)."""
    magic = b"II*\x00" if endian == "<" else b"MM\x00*"
    ifd0_offset = 8
    # Reserve space for IFD0 (+ extra entries for 34665 and 330 if present)
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
        blob.extend(_pack_ifd(final_sub, 0, endian))
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
            blob.extend(_pack_ifd(final_child, 0, endian))
            blob.extend(child_blob)
        offs_bytes = b"".join(struct.pack(endian + "I", o) for o in subifd_offsets)
        final_ifd0.append((330, 4, len(subifd_offsets), alloc_extra(offs_bytes)))

    header = magic + struct.pack(endian + "I", ifd0_offset)
    return header + _pack_ifd(final_ifd0, 0, endian) + bytes(blob)


def _make_jpeg_bytes(w: int = 8, h: int = 6, *, exif_payload: bytes | None = None, xmp: bytes | None = None) -> bytes:
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


def _synthetic_edge_cases() -> dict[str, tuple[bytes, str]]:
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
        (272, 2, 5, b"GXR\x00 "),  # odd length, trailing space after NUL? keep raw NUL test
        (306, 2, 20, b"2026:05:06 07:08:09\x00"),
        (33432, 2, 9, b"\xa9 2026\x00\x00\x00"),  # latin-1 copyright with multiple NULs
        (50708, 2, 26, b"RICOH GR LENS A12 50mm   \x00"),  # fallback lens
        (274, 3, 1, struct.pack("<H", 6)),  # Orientation=6
    ]
    le_sub = [
        (36867, 2, 20, b"2026:05:06 07:08:09\x00"),
        (34855, 3, 1, struct.pack("<H", 400)),
        (33434, 5, 1, struct.pack("<II", 1, 250)),
        (33437, 5, 1, struct.pack("<II", 25, 10)),
        (37386, 5, 1, struct.pack("<II", 5, 0)),  # IFDRational zero denominator!
        (37380, 10, 1, struct.pack("<ii", -3, 0)),  # SRATIONAL negative + zero denom
        (37396, 3, 4, struct.pack("<HHHH", 10, 20, 30, 40)),  # SubjectArea SHORT[4]
        (42034, 5, 4, struct.pack("<IIIIIIII", 50, 1, 50, 1, 25, 10, 22, 1)),  # LensSpecification
        (37500, 7, 6, b"MKNOTE"),  # MakerNote -> must be dropped by export-meta-exif
        (36864, 7, 4, b"0230"),  # ExifVersion UNDEFINED
        (41728, 7, 1, b"\x03"),  # 1-byte UNDEFINED
        (271, 2, 8, b"SUB_TAG\x00"),  # Same tag ID as IFD0!
        (42036, 2, 4, b"   \x00"),  # Whitespace LensModel -> triggers 50708 fallback
    ]
    cases["le_edges.dng"] = (_make_tiff_doc(le_ifd0, le_sub, endian="<"), "dng")

    # 2. Big-endian + count=0 + unknown type + repeated tag (last wins) + float/double + sbyte/slong + 69-char GXR1 recipe tag
    recipe_69 = b"GXR1 6a3a5a3c02 28 06 A8999B999-".ljust(69, b" ") + b"\x00"
    be_ifd0 = base_tiff(">") + [
        (271, 2, 6, b"RICOH\x00"),
        (271, 2, 7, b"PENTAX\x00"),  # repeated tag -> last wins
        (33432, 2, len(recipe_69), recipe_69),
        (50708, 2, 17, b"RICOH A16 24-85\x00\x00"),
        (40001, 3, 0, b"\x00\x00\x00\x00"),  # count == 0 -> skipped
        (40002, 99, 1, b"\x00\x00\x00\x01"),  # unknown typ 99 -> ignored
    ]
    be_sub = [
        (36867, 2, 20, b"2024-02-29 12:34:56\x00"),
        (34855, 3, 2, struct.pack(">HH", 800, 1600)),  # ISO tuple -> _iso takes [0]
        (33434, 5, 1, struct.pack(">II", 2, 5)),  # 0.4 -> 1/2s (round half-to-even of 2.5 is 2!)
        (33437, 5, 1, struct.pack(">II", 0, 0)),  # 0/0 aperture -> ""
        (40003, 9, 1, struct.pack(">i", -42)),  # SLONG
        (40004, 12, 1, struct.pack(">d", 1.25)),  # DOUBLE
    ]
    cases["be_edges.dng"] = (_make_tiff_doc(be_ifd0, be_sub, endian=">"), "dng")

    # 3. SubIFD crop size (50720) + preview SubIFD (largest JPEG wins, skip invalid SOF3 / >32MB)
    small_jpg = _make_jpeg_bytes(4, 4)
    big_jpg = _make_jpeg_bytes(16, 12)
    # 12-bit SOF3 JPEG header (unopenable by Pillow) even larger dimensions:
    sof3_jpg = b"\xff\xd8\xff\xc3\x00\x0b\x0c\x00\x64\x00\x64\x01\x01\x11\x00\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xff\xd9"
    subifds = [
        # Main SubIFD (254=0, 50720=[200, 100])
        [
            (254, 4, 1, struct.pack("<I", 0)),
            (256, 4, 1, struct.pack("<I", 220)),
            (257, 4, 1, struct.pack("<I", 120)),
            (50720, 4, 2, struct.pack("<II", 200, 100)),
        ],
        # Smaller preview
        [
            (254, 4, 1, struct.pack("<I", 1)),
            (256, 4, 1, struct.pack("<I", 4)),
            (257, 4, 1, struct.pack("<I", 4)),
            (259, 3, 1, struct.pack("<H", 7)),
            (273, 4, 1, struct.pack("<I", 8 + 5000)),
            (279, 4, 1, struct.pack("<I", len(small_jpg))),
        ],
        # Out-of-bounds offset
        [
            (254, 4, 1, struct.pack("<I", 1)),
            (256, 4, 1, struct.pack("<I", 16)),
            (257, 4, 1, struct.pack("<I", 12)),
            (259, 3, 1, struct.pack("<H", 7)),
            (273, 4, 1, struct.pack("<I", 999999)),
            (279, 4, 1, struct.pack("<I", len(big_jpg))),
        ],
        # Too-large count (> 32 MiB) -> skipped
        [
            (254, 4, 1, struct.pack("<I", 1)),
            (256, 4, 1, struct.pack("<I", 999)),
            (257, 4, 1, struct.pack("<I", 999)),
            (259, 3, 1, struct.pack("<H", 7)),
            (273, 4, 1, struct.pack("<I", 999999)),
            (279, 4, 1, struct.pack("<I", 33 * 1024 * 1024)),
        ],
    ]
    dng_pv = bytearray(_make_tiff_doc(base_tiff("<") + [(274, 3, 1, struct.pack("<H", 6))], subifd_list=subifds))
    dng_pv.extend(big_jpg)
    cases["subifd_preview_oob.dng"] = (bytes(dng_pv), "dng")

    # Clean preview DNG with exact known offset
    p_hdr = b"II*\x00" + struct.pack("<I", 8)
    # IFD0: 256,257,273,279,330(->off of 1 child)
    # Put big_jpg right after IFD0 + SubIFD0
    ifd0_sz = 2 + 5 * 12 + 4
    sub_off = 8 + ifd0_sz
    sub_sz = 2 + 6 * 12 + 4
    jpg_off = sub_off + sub_sz
    p_ifd0 = _pack_ifd(
        base_tiff("<") + [(330, 4, 1, struct.pack("<I", sub_off))],
        0,
        "<",
    )
    p_sub = _pack_ifd(
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

    # Lossless SOF3 preview (unopenable by Pillow -> must return None / null)
    jpg_off3 = sub_off + sub_sz
    p_sub3 = _pack_ifd(
        [
            (254, 4, 1, struct.pack("<I", 1)),
            (256, 4, 1, struct.pack("<I", 100)),
            (257, 4, 1, struct.pack("<I", 100)),
            (259, 3, 1, struct.pack("<H", 7)),
            (273, 4, 1, struct.pack("<I", jpg_off3)),
            (279, 4, 1, struct.pack("<I", len(sof3_jpg))),
        ],
        0,
        "<",
    )
    cases["sof3_preview.dng"] = (p_hdr + p_ifd0 + p_sub3 + sof3_jpg, "dng")

    # 4. JPEG with multi-APP1 concatenation + XMP orientation fallback + shutter >= 1s
    exif_tiff = _make_tiff_doc(
        [
            (271, 2, 6, b"RICOH\x00"),
            (306, 2, 17, b"2026:01:02 03:04\x00"),
        ],
        [
            (34855, 3, 1, struct.pack("<H", 200)),
            (33434, 5, 1, struct.pack("<II", 25, 10)),  # 2.5s
            (33437, 5, 1, struct.pack("<II", 20, 10)),  # f/2
        ],
    )
    xmp = b'<x:xmpmeta><rdf:RDF><rdf:Description tiff:Orientation="8"/></rdf:RDF></x:xmpmeta>'
    cases["xmp_orient.jpg"] = (_make_jpeg_bytes(10, 6, exif_payload=exif_tiff, xmp=xmp), "jpg")
    # Split exif_tiff across two APP1 Exif\x00\x00 segments + iso=0 + out-of-range taken date (1994)
    split_exif = _make_tiff_doc(
        [(271, 2, 6, b"RICOH\x00"), (306, 2, 20, b"1994:05:06 07:08:09\x00")],
        [(34855, 3, 1, struct.pack("<H", 0))],
    )
    mid = len(split_exif) // 2
    base_jpg = _make_jpeg_bytes(8, 6)
    s1 = b"Exif\x00\x00" + split_exif[:mid]
    s2 = b"Exif\x00\x00" + split_exif[mid:]
    multi_app1 = (
        base_jpg[:2] + b"\xff\xe1" + struct.pack(">H", len(s1) + 2) + s1 + b"\xff\xe1" + struct.pack(">H", len(s2) + 2) + s2 + base_jpg[2:]
    )
    cases["multi_app1_iso0.jpg"] = (multi_app1, "jpg")

    # 5. Damaged / hostile files
    # 5a. OOB tag value mid-IFD0 (271 kept, 272 OOB stops IFD0, 306 skipped)
    oob_ifd0 = base_tiff("<") + [
        (271, 2, 4, b"OK1\x00"),
        (272, 2, 100, struct.pack("<I", 999999)),
        (306, 2, 4, b"SKP\x00"),
    ]
    cases["oob_val.dng"] = (b"II*\x00" + struct.pack("<I", 8) + _pack_ifd(oob_ifd0, 0, "<"), "dng")

    # 5b. 0x8769 self-loop pointing back to IFD0 (offset 8)
    loop_ifd0 = base_tiff("<") + [
        (271, 2, 4, b"LOP\x00"),
        (0x8769, 4, 1, struct.pack("<I", 8)),
    ]
    cases["self_loop_0x8769.dng"] = (b"II*\x00" + struct.pack("<I", 8) + _pack_ifd(loop_ifd0, 0, "<"), "dng")

    # 5c. Huge entry count 65535 in IFD0 -> truncated read -> UnidentifiedImageError in read_dng_meta
    cases["huge_ifd_count.dng"] = (
        b"II*\x00\x08\x00\x00\x00\xff\xff" + struct.pack("<HHI", 256, 4, 1) + struct.pack("<I", 4),
        "dng",
    )

    # 5d. Huge tag count 0x4000_0000 -> u64 multiplication check, no OOM/panic
    huge_val_cnt = base_tiff("<") + [(271, 5, 0x4000_0000, struct.pack("<I", 8))]
    cases["huge_tag_count.dng"] = (b"II*\x00" + struct.pack("<I", 8) + _pack_ifd(huge_val_cnt, 0, "<"), "dng")

    # 5e. Truncated JPEG (no SOS), non-image garbage, empty file
    cases["no_sos.jpg"] = (b"\xff\xd8\xff\xc0\x00\x0b\x08\x00\x02\x00\x03\x01\x01\x11\x00\xff\xd9", "jpg")
    cases["garbage.jpg"] = (b"NOT_AN_IMAGE_1234", "jpg")
    cases["empty.dng"] = (b"", "dng")
    return cases


# --------------------------------------------------------------------------- Tests


def test_real_jpgs_and_dngs_exact_parity() -> None:
    if not REAL_JPGS or not REAL_DNGS:
        _missing("real JPG/DNG captures not present under GXR_STUDIO_TEST_PARENT")
    require_cli()
    assert len(REAL_JPGS) >= 50 and len(REAL_DNGS) >= 50

    now_year = datetime.now().year
    preview_found = 0
    preview_none = 0

    for jpg in REAL_JPGS:
        code, got_exif = run_json(["export-meta-exif", "--src", str(jpg)])
        assert error_of(code, got_exif) is None, jpg.name
        _assert_exif_equal(got_exif, _py_source_exif(jpg), jpg.name)

        code, got_read = run_json(["export-meta-read", "--file", str(jpg), "--now-year", str(now_year)])
        assert error_of(code, got_read) is None, jpg.name
        _assert_read_meta_equal(got_read, _py_read_meta_json(jpg, is_dng=False), jpg.name)

        code, got_photo = run_json(
            [
                "export-meta-photo",
                "--jpg",
                str(jpg),
                "--name",
                jpg.stem,
                "--mtime-ms",
                "1700000000123",
                "--tz-offset-min",
                "480",
                "--now-year",
                str(now_year),
            ]
        )
        assert error_of(code, got_photo) is None, jpg.name
        assert got_photo == _py_photo_meta_json(jpg, None, jpg.stem, 1700000000123.0, 480), jpg.name

    for dng in REAL_DNGS:
        code, got_exif = run_json(["export-meta-exif", "--src", str(dng)])
        assert error_of(code, got_exif) is None, dng.name
        _assert_exif_equal(got_exif, _py_source_exif(dng), dng.name)

        code, got_read = run_json(["export-meta-read", "--file", str(dng), "--now-year", str(now_year)])
        assert error_of(code, got_read) is None, dng.name
        _assert_read_meta_equal(got_read, _py_read_meta_json(dng, is_dng=True), dng.name)

        code, got_photo = run_json(
            [
                "export-meta-photo",
                "--dng",
                str(dng),
                "--name",
                dng.stem,
                "--mtime-ms",
                "1700000000000",
                "--tz-offset-min",
                "-300",
                "--now-year",
                str(now_year),
            ]
        )
        assert error_of(code, got_photo) is None, dng.name
        assert got_photo == _py_photo_meta_json(None, dng, dng.stem, 1700000000000.0, -300), dng.name

        code, got_pv = run_json(["export-meta-preview", "--dng", str(dng)])
        assert error_of(code, got_pv) is None, dng.name
        py_pv = dng_preview_image(dng, embedded_only=True)
        if py_pv is None:
            assert got_pv is None, dng.name
            preview_none += 1
        else:
            assert isinstance(got_pv, dict) and set(got_pv) == {"offset", "length"}, dng.name
            raw = dng.read_bytes()
            off, length = got_pv["offset"], got_pv["length"]
            with Image.open(io.BytesIO(raw[off : off + length])) as rs_pv:
                rs_rgb = rs_pv.convert("RGB")
                assert rs_rgb.size == py_pv.size, dng.name
                assert rs_rgb.tobytes() == py_pv.tobytes(), dng.name
            preview_found += 1

    print(f"Real parity: {len(REAL_JPGS)} JPGs, {len(REAL_DNGS)} DNGs (preview found={preview_found}, none={preview_none})")


def test_synthetic_pillow_edges_and_damaged_files(tmp_path: Path) -> None:
    require_cli()
    now_year = datetime.now().year
    cases = _synthetic_edge_cases()

    for filename, (blob, kind) in cases.items():
        path = tmp_path / filename
        path.write_bytes(blob)
        is_dng = kind == "dng"

        code, got_exif = run_json(["export-meta-exif", "--src", str(path)])
        assert error_of(code, got_exif) is None, filename
        _assert_exif_equal(got_exif, _py_source_exif(path), filename)

        code, got_read = run_json(["export-meta-read", "--file", str(path), "--now-year", str(now_year)])
        assert error_of(code, got_read) is None, filename
        _assert_read_meta_equal(got_read, _py_read_meta_json(path, is_dng=is_dng), filename)

        jpg_arg = path if not is_dng else None
        dng_arg = path if is_dng else None
        args = [
            "export-meta-photo",
            "--name",
            f"R001_{path.stem}",
            "--mtime-ms",
            "1700000000123",
            "--tz-offset-min",
            "480",
            "--now-year",
            str(now_year),
        ]
        args += ["--jpg", str(path)] if not is_dng else ["--dng", str(path)]
        code, got_photo = run_json(args)
        assert error_of(code, got_photo) is None, filename
        assert got_photo == _py_photo_meta_json(jpg_arg, dng_arg, f"R001_{path.stem}", 1700000000123.0, 480), filename

        if is_dng:
            code, got_pv = run_json(["export-meta-preview", "--dng", str(path)])
            assert error_of(code, got_pv) is None, filename
            py_pv = dng_preview_image(path, embedded_only=True)
            if py_pv is None:
                assert got_pv is None, filename
            else:
                assert isinstance(got_pv, dict)
                off, length = got_pv["offset"], got_pv["length"]
                with Image.open(io.BytesIO(blob[off : off + length])) as rs_pv:
                    assert rs_pv.convert("RGB").tobytes() == py_pv.tobytes(), filename


def test_photo_prefers_jpg_when_both_present(tmp_path: Path) -> None:
    require_cli()
    jpg = tmp_path / "pair.jpg"
    dng = tmp_path / "pair.dng"
    cases = _synthetic_edge_cases()
    jpg.write_bytes(cases["xmp_orient.jpg"][0])
    dng.write_bytes(cases["le_edges.dng"][0])
    now_year = datetime.now().year
    code, got = run_json(
        [
            "export-meta-photo",
            "--jpg",
            str(jpg),
            "--dng",
            str(dng),
            "--name",
            "IMGP0456",
            "--mtime-ms",
            "1700000000500",
            "--tz-offset-min",
            "0",
            "--now-year",
            str(now_year),
        ]
    )
    assert error_of(code, got) is None
    assert got == _py_photo_meta_json(jpg, dng, "IMGP0456", 1700000000500.0, 0)
    assert got["frame"]["shutter"] == "2.5s"


def test_errors_match_core_error_kinds(tmp_path: Path) -> None:
    require_cli()
    missing = tmp_path / "does_not_exist.dng"
    code, payload = run_json(["export-meta-exif", "--src", str(missing)])
    err = error_of(code, payload)
    assert isinstance(err, CoreCliError) and err.kind == "raw_decode"

    jpg = tmp_path / "ok.jpg"
    jpg.write_bytes(_make_jpeg_bytes())
    for bad_args, expected_kind in [
        (["export-meta-photo", "--name", "a"], "bad_input"),
        (["export-meta-photo", "--jpg", str(jpg), "--mtime-ms", "NaN"], "bad_meta"),
        (["export-meta-photo", "--jpg", str(jpg), "--mtime-ms", "1000", "--tz-offset-min", "1440"], "bad_meta"),
        (["export-meta-photo", "--jpg", str(jpg), "--mtime-ms", "1000", "--tz-offset-min", "-1440"], "bad_meta"),
    ]:
        code, payload = run_json(bad_args)
        err = error_of(code, payload)
        assert isinstance(err, CoreCliError) and err.kind == expected_kind, (bad_args, err)


def test_wasm_matches_native_export_meta(tmp_path: Path) -> None:
    require_cli()
    cases = _synthetic_edge_cases()
    manifest_entries: list[dict[str, Any]] = []
    null_preview_dngs: list[Path] = []

    for name, (blob, kind) in cases.items():
        p = tmp_path / name
        p.write_bytes(blob)
        manifest_entries.append({"label": f"{name}::exif", "kind": "exif", "src": str(p)})
        manifest_entries.append(
            {
                "label": f"{name}::photo",
                "kind": "photo",
                "jpg": str(p) if kind == "jpg" else None,
                "dng": str(p) if kind == "dng" else None,
                "name": p.stem,
                "mtime_ms": 1700000000123,
                "tz_offset_min": 480,
            }
        )
        if kind == "dng":
            if dng_preview_image(p, embedded_only=True) is not None:
                manifest_entries.append({"label": f"{name}::preview", "kind": "preview", "dng": str(p)})
            else:
                null_preview_dngs.append(p)

    # Include real samples (selected spread) in WASM vs native check
    for jpg in REAL_JPGS[:10]:
        manifest_entries.append({"label": f"{jpg.name}::exif", "kind": "exif", "src": str(jpg)})
        manifest_entries.append(
            {
                "label": f"{jpg.name}::photo",
                "kind": "photo",
                "jpg": str(jpg),
                "dng": None,
                "name": jpg.stem,
                "mtime_ms": 1700000000000,
                "tz_offset_min": 0,
            }
        )
    for dng in REAL_DNGS[:10]:
        manifest_entries.append({"label": f"{dng.name}::exif", "kind": "exif", "src": str(dng)})
        if dng_preview_image(dng, embedded_only=True) is not None:
            manifest_entries.append({"label": f"{dng.name}::preview", "kind": "preview", "dng": str(dng)})
        else:
            null_preview_dngs.append(dng)

    # Error parity cases in WASM vs native
    ok_jpg = tmp_path / "xmp_orient.jpg"
    manifest_entries.extend(
        [
            {"label": "err::missing_both", "kind": "photo", "jpg": None, "dng": None, "name": "a", "mtime_ms": 1000, "tz_offset_min": 0},
            {"label": "err::bad_tz", "kind": "photo", "jpg": str(ok_jpg), "dng": None, "name": "a", "mtime_ms": 1000, "tz_offset_min": 1440},
        ]
    )

    manifest = tmp_path / "export-wasm.json"
    manifest.write_text(json.dumps(manifest_entries), encoding="utf-8")
    proc = run_area("export", [manifest], tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    # Verify null-returning preview DNGs are also byte-identical between native CLI and WASM
    if shutil.which("node") and PKG.is_file() and null_preview_dngs:
        node_script = """
import { readFileSync } from 'node:fs';
const wasm = await import(process.argv[1]);
wasm.initSync({ module: readFileSync(process.argv[2]) });
const results = [];
for (const p of JSON.parse(process.argv[3])) {
  results.push(wasm.export_meta_preview(new Uint8Array(readFileSync(p))));
}
process.stdout.write(JSON.stringify(results));
"""
        res = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                node_script,
                PKG.as_uri(),
                str(PKG.with_name("gxr_wasm_bg.wasm")),
                json.dumps([str(p) for p in null_preview_dngs]),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, res.stderr
        wasm_outputs = json.loads(res.stdout)
        assert len(wasm_outputs) == len(null_preview_dngs)
        for p, got in zip(null_preview_dngs, wasm_outputs, strict=True):
            assert got == "null\n", (p.name, got)
