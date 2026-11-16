"""UI-M3 browser-test helpers: real samples (only through ``support.samples``), an in-place EXIF
Orientation rewrite (no re-encode), and the desktop side of the export comparison.

Nothing here prints photo data. Comparisons go through ``support.export_pipeline_cases``'
redacted helpers in the test file.
"""

from __future__ import annotations

import hashlib
import shutil
import struct
from pathlib import Path

import numpy as np

from support import samples

SAMPLE_DIR = samples.a12_50_samples()


def real_pairs(limit: int) -> list[tuple[Path, Path]]:
    """``(dng, jpg)`` pairs from the sample folder the M1/M2 browser tests use, sorted by name."""
    if not SAMPLE_DIR.is_dir():
        return []
    out: list[tuple[Path, Path]] = []
    for dng in sorted(p for p in SAMPLE_DIR.iterdir() if p.is_file() and p.suffix.upper() == ".DNG"):
        jpg = next((dng.with_suffix(ext) for ext in (".JPG", ".jpg") if dng.with_suffix(ext).is_file()), None)
        if jpg is not None:
            out.append((dng, jpg))
        if len(out) >= limit:
            break
    return out


def copy_as(src: Path, dest: Path) -> Path:
    """A copy under the test's temporary folder (keeps the source's modification time)."""
    shutil.copy2(src, dest)
    return dest


def set_orientation(src: Path, dest: Path, value: int) -> bool:
    """Copy ``src`` to ``dest`` and rewrite IFD0's Orientation (0x0112) in the APP1 Exif block.

    The JPEG data is untouched. Returns False when the file has no Orientation tag to rewrite.
    """
    data = bytearray(src.read_bytes())
    i = 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        if marker in (0xD9, 0xDA):
            break
        length = struct.unpack(">H", data[i + 2 : i + 4])[0]
        seg = i + 4
        if marker == 0xE1 and data[seg : seg + 6] == b"Exif\x00\x00":
            tiff = seg + 6
            le = data[tiff : tiff + 2] == b"II"
            end = "<" if le else ">"
            ifd0 = tiff + struct.unpack(end + "I", data[tiff + 4 : tiff + 8])[0]
            count = struct.unpack(end + "H", data[ifd0 : ifd0 + 2])[0]
            for k in range(count):
                e = ifd0 + 2 + 12 * k
                tag, typ = struct.unpack(end + "HH", data[e : e + 4])
                if tag == 0x0112 and typ == 3:
                    data[e + 8 : e + 10] = struct.pack(end + "H", value)
                    dest.write_bytes(bytes(data))
                    return True
            return False
        i += 2 + length
    return False


def decoded_digest(path: Path) -> tuple[str, int, int]:
    """SHA-256 of desktop ``load_jpeg(path, None)``'s RGB bytes, and its width and height."""
    from gxr_studio.finish.compose import load_jpeg

    rgb = np.ascontiguousarray(load_jpeg(path, None))
    return hashlib.sha256(rgb.tobytes()).hexdigest(), int(rgb.shape[1]), int(rgb.shape[0])


def decoded(path: Path) -> np.ndarray:
    from gxr_studio.finish.compose import load_jpeg

    return np.ascontiguousarray(load_jpeg(path, None))
