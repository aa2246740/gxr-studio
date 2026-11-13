"""Synthetic colour fixtures for ``tests/test_core_parity_color.py``.

Everything here is generated: 552-byte entries with sign-magnitude matrix rows, a
``gxr-sector-model/2`` ring, calibration arrays and a calibration pack laid out the way
``calib.import_calibration`` writes it. No camera bytes, no calibration from a device.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import struct
from pathlib import Path
from typing import Any

import numpy as np

# Y, Cb, Cr rows in Q10 (about BT.601), plus the tail words 0, 128, 128, 0, 0.
BT601 = ((306, 601, 117), (-173, -339, 512), (512, -429, -83))


def _word(v: int) -> int:
    return (0x800 | -v) if v < 0 else v


def _row(rows: tuple[tuple[int, int, int], ...]) -> list[int]:
    return [_word(v) for r in rows for v in r] + [0, 128, 128, 0, 0]


def entry(*, kind: int = 4, flags: int = 0x13, tilt: int = 0, words: int = 0, rows: tuple[tuple[int, int, int], ...] = BT601) -> bytes:
    """A 552-byte entry the frozen engine accepts: knots, curves, symmetric sector words."""
    buf = bytearray(552)
    buf[0] = kind
    buf[1] = flags
    for at in (20, 284, 456):
        for i in range(31):
            struct.pack_into(">H", buf, at + 2 * i, (i + 1) * 128)
    for at, extra in ((82, 0), (346, 1), (379, 2), (412, 3), (518, 4)):
        for i in range(33):
            buf[at + i] = min(255, i * 7 + extra + (tilt if at in (82, 379, 412) else 0))
    for sector in range(24):
        a = 8 + ((sector + words) % 5)
        c = 3 + ((sector + 2 * words) % 3)
        buf[115 + 4 * sector : 119 + 4 * sector] = bytes((a, c, c ^ 0x80, a))
    struct.pack_into(">28H", buf, 216, *(_row(rows) * 2))
    return bytes(buf)


def ring(*, kernel: str = "linear", per_sector: bool = True, kappa: float = 30.0) -> dict[str, Any]:
    nodes = [7.0 + 15.0 * s + 3.0 * math.sin(s) for s in range(24)]
    out: dict[str, Any] = {
        "schema": "gxr-sector-model/2",
        "nodes_unwrapped_deg": nodes,
        "k_coupling": 0.85,
        "kernel": {"type": kernel, "blur_deg": 5.0, "blur_kappa": kappa},
        "gain": 1.05,
        "bootstrap": {"note": "research keys are ignored", "nan": float("nan")},
    }
    if per_sector:
        out["gain_per_sector"] = [1.0 + 0.01 * ((s * 7) % 11) for s in range(24)]
    return out


def arrays(rows: int = 4, seed: int = 3) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    d0 = rng.uniform(-1.5, 1.5, 24)
    d1 = rng.uniform(-1.0, 1.0, 24)
    coefficients = rng.uniform(-0.01, 0.01, (rows, 3))
    return d0, d1, coefficients


def source_npz(stock_words: np.ndarray, d0: np.ndarray, d1: np.ndarray, coefficients: np.ndarray) -> bytes:
    """A development npz with ``stock`` and ``S_eff``, the input ``_context`` checks."""
    s_eff = ((stock_words[:, 0] + d0) - 1j * (stock_words[:, 1] + d1)) / 32
    buf = io.BytesIO()
    np.savez(buf, stock=stock_words, d0=d0, d1=d1, input_coefficients=coefficients, S_eff=s_eff)
    return buf.getvalue()


def ctx_npz(d0: np.ndarray, d1: np.ndarray, coefficients: np.ndarray) -> bytes:
    """A pack's ctx npz, as ``import_calibration`` writes it."""
    buf = io.BytesIO()
    np.savez(buf, d0=d0, d1=d1, input_coefficients=coefficients)
    return buf.getvalue()


def write_pack(folder: Path, unit: str, stock04: bytes, files: dict[int, tuple[bytes, bytes]]) -> Path:
    """``manifest.json``, ``ctx_*.npz`` and ``ring_*.json`` the way ``import_calibration`` does."""
    folder.mkdir(parents=True, exist_ok=True)
    blobs: dict[str, bytes] = {}
    models: dict[str, dict[str, str]] = {}
    for iso, (ctx, ring_bytes) in sorted(files.items()):
        ctx_name, ring_name = f"ctx_{unit}_{iso}.npz", f"ring_{unit}_{iso}.json"
        blobs[ctx_name], blobs[ring_name] = ctx, ring_bytes
        models[str(iso)] = {"ctx": ctx_name, "ring": ring_name}
    manifest = {
        "schema": "gxr-studio/calib/2",
        "units": {
            unit: {
                "camera_stock_sha256": hashlib.sha256(stock04[115:211]).hexdigest(),
                "models": models,
                "isos": [200, 1600] if unit == "jlaunch" else [200],
                "exposure_unit": "jlaunch",
            }
        },
        "sources": {},
        "files": {name: {"size": len(b), "sha256": hashlib.sha256(b).hexdigest()} for name, b in sorted(blobs.items())},
    }
    for name, b in blobs.items():
        (folder / name).write_bytes(b)
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return folder


def codes(seed: int = 5, n: int = 4000) -> np.ndarray:
    """float32 white-balanced codes: mostly in range, some above 4095 and below 0."""
    rng = np.random.default_rng(seed)
    x = rng.uniform(0, 4095, (n, 3))
    x[:50] = rng.uniform(4000, 6000, (50, 3))
    x[50:80] = rng.uniform(-200, 50, (30, 3))
    x[80:120] = np.repeat(rng.uniform(0, 4095, (40, 1)), 3, axis=1)
    return x.astype(np.float32)


def first_difference(python: np.ndarray, rust: np.ndarray) -> str:
    """The first differing index and both values, for an assertion message."""
    a, b = np.asarray(python).reshape(-1), np.asarray(rust).reshape(-1)
    if a.shape != b.shape:
        return f"shapes differ: python {python.shape} rust {rust.shape}"
    if a.dtype.kind == "f":
        bad = np.flatnonzero(
            a.view(np.uint32 if a.dtype == np.float32 else np.uint64) != b.view(np.uint32 if b.dtype == np.float32 else np.uint64)
        )
    else:
        bad = np.flatnonzero(a != b)
    if not len(bad):
        return "no difference"
    i = int(bad[0])
    return f"{len(bad)} differ; first flat index {i}: python {a[i]!r} rust {b[i]!r}"
