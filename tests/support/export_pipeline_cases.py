"""Shared helpers for ``tests/test_core_parity_export_pipeline*.py``.

Real samples come from ``support.samples.captures()`` (never copied into the repo). EXIF values
from real photos are compared without printing them: a mismatch reports tag ids and hashes only.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from gxr_studio.export import source_exif
from support.samples import captures

CAPTURES = captures()


def exif_val_json(v: Any) -> Any:
    """One Pillow EXIF value in the JSON shape ``gxr_jpeg::parse_exif_json`` reads."""
    if isinstance(v, bytes):
        return {"bytes": list(v)}
    if isinstance(v, IFDRational):
        return {"rational": [int(v.numerator), int(v.denominator)]}
    if isinstance(v, tuple):
        if v and all(isinstance(x, IFDRational) for x in v):
            return {"rationals": [[int(x.numerator), int(x.denominator)] for x in v]}
        return [exif_val_json(x) for x in v]
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return int(v)
    if isinstance(v, float):
        return float(v)
    return str(v)


def source_exif_json(source: Path) -> str:
    """``source_exif`` (without the ``270`` note, which the pipeline adds) as gxr-jpeg JSON."""
    base, sub = source_exif(source, "")
    base.pop(270, None)
    return json.dumps(
        {"base": {str(k): exif_val_json(v) for k, v in base.items()}, "exif": {str(k): exif_val_json(v) for k, v in sub.items()}},
        ensure_ascii=False,
    )


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def first_difference(a: bytes, b: bytes) -> str:
    """Where two files part, without printing their content (they may hold real EXIF)."""
    n = next((i for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y), min(len(a), len(b)))
    return f"lengths {len(a)} vs {len(b)}, first difference at byte {n}, sha {digest(a)} vs {digest(b)}"


def real_pairs(limit: int) -> list[tuple[Path, Path]]:
    """Up to ``limit`` DNG+JPG pairs, spread evenly over the sorted capture list."""
    if not CAPTURES.is_dir():
        return []
    dngs = sorted(
        (p for p in CAPTURES.rglob("*") if p.is_file() and p.suffix.upper() == ".DNG" and "backup" not in p.as_posix().lower()),
        key=lambda p: p.as_posix(),
    )
    pairs: list[tuple[Path, Path]] = []
    for d in dngs:
        for ext in (".JPG", ".jpg"):
            j = d.with_suffix(ext)
            if j.is_file():
                pairs.append((j, d))
                break
    if len(pairs) <= limit:
        return pairs
    return [pairs[(i * len(pairs)) // limit] for i in range(limit)]


def synthetic_source(path: Path, *, with_lens: bool = True) -> Path:
    """A small camera-like JPEG with Make/Model/DateTime/Copyright and an EXIF sub-IFD."""
    im = Image.new("RGB", (8, 6), (120, 130, 140))
    exif = Image.Exif()
    exif[271] = "RICOH"
    exif[272] = "GXR "
    exif[306] = "2026:11:15 10:00:00"
    exif[33432] = "© test"
    sub = exif.get_ifd(0x8769)
    sub[36867] = "2026:11:15 10:00:00"
    sub[34855] = 400
    sub[33434] = IFDRational(1, 250)
    sub[33437] = IFDRational(28, 10)
    if with_lens:
        sub[42036] = "A12 50mm"
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90, exif=exif)
    path.write_bytes(buf.getvalue())
    return path


def synthetic_images() -> list[tuple[str, np.ndarray]]:
    rng = np.random.default_rng(20261115)
    out: list[tuple[str, np.ndarray]] = []
    for h, w in [(1, 1), (7, 13), (16, 16), (17, 33), (64, 48), (97, 131)]:
        out.append((f"noise{h}x{w}", rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)))
    yy, xx = np.mgrid[0:120, 0:200]
    ramp = np.stack([xx * 255 // 199, yy * 255 // 119, (xx + yy) * 255 // 318], axis=-1).astype(np.uint8)
    out.append(("ramp120x200", ramp))
    out.append(("flat40x30", np.full((30, 40, 3), 77, dtype=np.uint8)))
    out.append(("black9x9", np.zeros((9, 9, 3), dtype=np.uint8)))
    out.append(("white9x9", np.full((9, 9, 3), 255, dtype=np.uint8)))
    return out
