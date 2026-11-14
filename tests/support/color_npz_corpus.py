"""Sol's NPZ differential corpus (color review r2), generated at test time.

The 42 archive variants and the 3 runtime ctx variants follow
`sol-review-mobile-color-r2/probes/npz_adversarial.py` name for name: the same base
archives, the same field mutations at the same offsets. Everything is synthetic
(`color_fixtures`); no file from the review is copied into the repo.
"""

from __future__ import annotations

import io
import struct
import warnings
import zipfile
from typing import Any

import numpy as np

from gxr_studio.camdata import calib
from support import color_fixtures as cf

# Variants the duplicate-member and stored-member parity tests name; the corpus test runs all.
DUPLICATES = (
    "duplicate_good_then_nan",
    "duplicate_nan_then_good",
    "duplicate_identical",
    "duplicate_good_then_crc_corrupt",
    "bare_name_overrides_npy",
)


STORED_RULES = (
    "descriptor_stored",
    "np_savez_nonseekable",
    "local_crc",
    "local_uncompressed_size",
    "local_compressed_size",
    "local_method",
    "local_descriptor_flag",
    "central_crc",
    "central_uncompressed_size",
    "central_compressed_size",
    "central_descriptor_flag",
    "unused_local_bad_magic",
    "unused_local_bad_crc",
    "local_zip64_file_huge",
    "local_zip64_compressed_huge",
    "local_zip64_short",
    "local_zip64_missing",
    "central_extra_overflow",
    "zip64_extra_truncated",
    "central_both_sizes_huge",
    "central_zip64_huge",
)


# Variants allowed to differ from Python because of the shared size limit, with the reason.
# The zipfile port refuses every huge declared size the way Python does (overlapped entries,
# zip64 record checks, end of file), so there are none.
SHARED_LIMIT_EXCEPTIONS: dict[str, str] = {}


class _NotSeekable(io.BytesIO):
    """A stream `zipfile` and `np.savez` cannot seek, so they write data descriptors."""

    def seekable(self) -> bool:
        return False

    def seek(self, *args: Any) -> int:
        raise io.UnsupportedOperation("not seekable")


def npy(a: np.ndarray) -> bytes:
    buf = io.BytesIO()
    np.save(buf, a, allow_pickle=False)
    return buf.getvalue()


def archive(items: list[tuple[str, bytes]], *, descriptor: bool = False, extra: bytes = b"") -> bytes:
    """A stored zip of `items` in this order (duplicates allowed)."""
    buf: io.BytesIO = _NotSeekable() if descriptor else io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # duplicate names
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as z:
            for name, data in items:
                info = zipfile.ZipInfo(name)
                info.extra = extra
                z.writestr(info, data)
    return buf.getvalue()


def _mutate(original: bytes, at: int, fmt: str, value: int) -> bytes:
    b = bytearray(original)
    struct.pack_into(fmt, b, at, value)
    return bytes(b)


def _flip_first_value(data: bytes, member: str) -> bytes:
    """Flip the low bit of `member`'s first array byte (the last record of that name), CRC kept."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        info = z.getinfo(member)
    name_len, extra_len = struct.unpack_from("<HH", data, info.header_offset + 26)
    payload = info.header_offset + 30 + name_len + extra_len
    header_len = struct.unpack_from("<H", data, payload + 8)[0]
    b = bytearray(data)
    b[payload + 10 + header_len] ^= 1
    return bytes(b)


def _central_zip64(original: bytes, values: tuple[int, int]) -> bytes:
    """The first central record's sizes moved into a ZIP64 extra field."""
    at = original.index(b"PK\x01\x02")
    end = original.rfind(b"PK\x05\x06")
    n = struct.unpack_from("<H", original, at + 28)[0]
    x = struct.pack("<HHQQ", 1, 16, *values)
    b = bytearray(original[: at + 46 + n] + x + original[at + 46 + n :])
    struct.pack_into("<H", b, at + 30, len(x))
    struct.pack_into("<II", b, at + 20, 0xFFFFFFFF, 0xFFFFFFFF)
    size = struct.unpack_from("<I", b, end + len(x) + 12)[0]
    struct.pack_into("<I", b, end + len(x) + 12, size + len(x))
    return bytes(b)


def source_parts() -> tuple[bytes, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """The stock entry, its words and the calibration arrays every variant is built from."""
    stock = cf.entry()
    words = calib.stock_words_of_entry(stock)
    d0, d1, coef = cf.arrays()
    return stock, words, d0, d1, coef


def variants() -> dict[str, bytes]:
    """The 42 source-npz variants, keyed by Sol's names, in Sol's order."""
    _stock, words, d0, d1, coef = source_parts()
    source = cf.source_npz(words, d0, d1, coef)
    with zipfile.ZipFile(io.BytesIO(source)) as z:
        members = [(i.filename, z.read(i)) for i in z.infolist()]
    base = archive(members)
    central = base.index(b"PK\x01\x02")
    end = base.rfind(b"PK\x05\x06")
    out: dict[str, bytes] = {
        "np_savez": source,
        "stored_standard": base,
        "truncated_before_central": source[: source.index(b"PK\x01\x02")],
        "truncated_tail_12": source[:-12],
        "descriptor_stored": archive(members, descriptor=True),
    }
    stream = _NotSeekable()
    np.savez(stream, stock=words, d0=d0, d1=d1, input_coefficients=coef, S_eff=((words[:, 0] + d0) - 1j * (words[:, 1] + d1)) / 32)
    out["np_savez_nonseekable"] = stream.getvalue()
    out["coefficient_crc_corruption"] = _flip_first_value(base, "input_coefficients.npy")
    for name, offset, fmt, value in [
        ("local_crc", 14, "<I", 0),
        ("local_uncompressed_size", 22, "<I", 0),
        ("local_compressed_size", 18, "<I", 0),
        ("local_method", 8, "<H", 8),
        ("local_descriptor_flag", 6, "<H", 8),
        ("central_crc", central + 16, "<I", 0),
        ("central_uncompressed_size", central + 24, "<I", 0),
        ("central_compressed_size", central + 20, "<I", 0),
        ("central_descriptor_flag", central + 8, "<H", 8),
        ("central_comment_overflow", central + 32, "<H", 65535),
        ("central_name_overflow", central + 28, "<H", 65535),
        ("central_extra_overflow", central + 30, "<H", 65535),
        ("central_header_offset_huge", central + 42, "<I", 0xFFFFFFFE),
        ("central_both_sizes_huge", central + 20, "<Q", 0xFFFFFFFEFFFFFFFE),
        ("eocd_comment_overflow", end + 20, "<H", 65535),
        ("eocd_count_zero", end + 8, "<I", 0),
        ("eocd_size_huge", end + 12, "<I", 0xFFFFFFFF),
        ("eocd_offset_huge", end + 16, "<I", 0xFFFFFFFF),
    ]:
        out[name] = _mutate(base, offset, fmt, value)
    bad = coef.copy()
    bad[0, 0] = np.nan
    out["duplicate_good_then_nan"] = archive(members + [("input_coefficients.npy", npy(bad))])
    out["duplicate_nan_then_good"] = archive([("input_coefficients.npy", npy(bad))] + members)
    out["duplicate_identical"] = archive(members + [("input_coefficients.npy", npy(coef))])
    out["duplicate_good_then_crc_corrupt"] = _flip_first_value(out["duplicate_identical"], "input_coefficients.npy")
    out["bare_name_overrides_npy"] = archive(members + [("input_coefficients", npy(bad))])
    unknown = archive(members + [("ignored.npy", b"not an npy")])
    with zipfile.ZipFile(io.BytesIO(unknown)) as z:
        unknown_at = z.getinfo("ignored.npy").header_offset
    out["unused_local_bad_magic"] = _mutate(unknown, unknown_at, "<I", 0)
    out["unused_local_bad_crc"] = _mutate(unknown, unknown_at + 14, "<I", 0)
    name_len = struct.unpack_from("<H", source, 26)[0]
    extra = 30 + name_len
    assert struct.unpack_from("<HH", source, extra) == (1, 16)
    out["local_zip64_file_huge"] = _mutate(source, extra + 4, "<Q", 1 << 63)
    out["local_zip64_compressed_huge"] = _mutate(source, extra + 12, "<Q", 1 << 63)
    out["local_zip64_short"] = _mutate(source, extra + 2, "<H", 8)
    out["local_zip64_missing"] = _mutate(source, extra, "<H", 42)
    out["zip64_extra_irrelevant_valid"] = archive(members, extra=struct.pack("<HHQ", 1, 8, 123))
    out["zip64_extra_truncated"] = archive(members, extra=struct.pack("<HH", 1, 24) + b"1234")
    size = struct.unpack_from("<I", base, central + 24)[0]
    out["central_zip64_valid"] = _central_zip64(base, (size, size))
    out["central_zip64_huge"] = _central_zip64(base, (1 << 63, 1 << 63))
    cdsize = struct.unpack_from("<I", base, end + 12)[0]
    count = len(members)
    rec64 = struct.pack("<4sQHHIIQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0, count, count, cdsize, central)
    locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, end, 1)
    end32 = bytearray(base[end:])
    struct.pack_into("<HHII", end32, 8, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF)
    out["zip64_eocd_valid"] = base[:end] + rec64 + locator + bytes(end32)
    out["zip64_eocd_huge"] = _mutate(out["zip64_eocd_valid"], end + 40, "<Q", 1 << 63)
    return out


def runtime_variants() -> dict[str, bytes]:
    """Pack ctx npz variants for the bake path: valid, data descriptors, duplicate `d0.npy`."""
    _stock, _words, d0, d1, coef = source_parts()
    with zipfile.ZipFile(io.BytesIO(cf.ctx_npz(d0, d1, coef))) as z:
        runtime = [(i.filename, z.read(i)) for i in z.infolist()]
    return {
        "runtime_valid": archive(runtime),
        "runtime_descriptor": archive(runtime, descriptor=True),
        "runtime_duplicate": archive(runtime + [("d0.npy", npy(d0 + 0.1))]),
    }
