"""Tiny TrueType bytes for the frame parity tests. Generated here, never a camera file."""

from __future__ import annotations


def _u16(n: int) -> bytes:
    return int(n).to_bytes(2, "big")


def _u32(n: int) -> bytes:
    return int(n).to_bytes(4, "big")


def _i16(n: int) -> bytes:
    return int(n).to_bytes(2, "big", signed=True)


def pack_sfnt(tables: dict[str, bytes]) -> bytes:
    """A classic sfnt directory. Checksums are zero; `sfnt.read_font` does not check them."""
    items = list(tables.items())
    header = b"\x00\x01\x00\x00" + _u16(len(items)) + b"\x00" * 6
    records = bytearray()
    body = bytearray()
    at = 12 + 16 * len(items)
    for tag, blob in items:
        records += tag.encode("latin-1") + b"\x00\x00\x00\x00" + _u32(at) + _u32(len(blob))
        body += blob
        at += len(blob)
    return bytes(header + records + body)


def _minimal_tables(cmap: bytes) -> dict[str, bytes]:
    head = bytearray(54)
    head[18:20] = _u16(1000)
    hhea = bytearray(36)
    hhea[4:6] = _i16(800)
    hhea[34:36] = _u16(1)
    maxp = bytearray(6)
    maxp[4:6] = _u16(1)
    return {
        "head": bytes(head),
        "hhea": bytes(hhea),
        "maxp": bytes(maxp),
        "hmtx": _u16(500) + _i16(0),
        "loca": _u16(0) + _u16(0),
        "glyf": b"",
        "cmap": cmap,
    }


def font_without_unicode_cmap() -> bytes:
    """One cmap subtable, format 0, platform 3 encoding 1. Python rejects it."""
    sub = _u16(0) + b"\x00" * 8
    cmap = _u16(0) + _u16(1) + _u16(3) + _u16(1) + _u32(12) + sub
    return pack_sfnt(_minimal_tables(cmap))


def empty_directory() -> bytes:
    """An sfnt with no tables, so the first lookup (`head`) is a KeyError."""
    return pack_sfnt({})
