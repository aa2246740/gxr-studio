"""Shared helpers for ``tests/test_core_parity_export_pipeline*.py``.

Real samples come from ``support.samples.captures()`` (never copied into the repo). EXIF values
from real photos are compared without printing them: a mismatch reports tag ids and hashes only.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from gxr_studio.export import source_exif
from support.samples import captures, raw_ifd_compression, stable_sample

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
    """The stable sample (``support.samples.stable_sample``) of DNG+JPG pairs, in key order.

    A pair is in it when its DNG's own path key is below the share for ``limit``, so captures added
    elsewhere never change it; the count is about 1.5 times ``limit`` today and grows with captures/.
    A raw IFD whose compression is not 1 is skipped. Stock firmware is uncompressed; modified-firmware
    field DNGs use 7 (lossless JPEG), which the desktop decoder refuses. Unreadable tags stay.
    """
    if not CAPTURES.is_dir():
        return []
    dngs = (
        p
        for p in CAPTURES.rglob("*")
        if p.is_file() and p.suffix.upper() == ".DNG" and "backup" not in p.as_posix().lower() and raw_ifd_compression(p) in (None, 1)
    )
    jpgs = {d: next((j for j in (d.with_suffix(".JPG"), d.with_suffix(".jpg")) if j.is_file()), None) for d in dngs}
    return [(jpg, d) for d in stable_sample((d for d, jpg in jpgs.items() if jpg is not None), CAPTURES, limit) if (jpg := jpgs[d]) is not None]


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


def mtime_args(path: Path) -> list[str]:
    """``--mtime-ms`` / ``--tz-offset-min`` as the phone would pass them for ``path``: the file's
    modification time (``File.lastModified``) and the local zone offset at that moment, which is
    what ``datetime.fromtimestamp(path.stat().st_mtime)`` uses on the desktop."""
    from datetime import datetime

    st = path.stat().st_mtime
    offset = datetime.fromtimestamp(st).astimezone().utcoffset()
    minutes = 0 if offset is None else int(offset.total_seconds() // 60)
    return ["--mtime-ms", repr(st * 1000.0), "--tz-offset-min", str(minutes)]


# ---- Redacted comparisons (threat model: docs/mobile.md, export pipeline) --------------------
#
# These helpers compare with a plain ``if`` and fail with ``pytest.fail(..., pytrace=False)``, so
# pytest never expands the compared values. Their messages are built only from fixed vocabularies:
# field names from a fixed schema (the JSON keys this pipeline's Rust and Python code write), error
# kinds from the core's known error kinds, dtype names from a fixed set, plus lengths, offsets,
# shapes, counts and SHA-256 prefixes. Anything outside a vocabulary is shown only as a hash.

_REPO = Path(__file__).resolve().parents[2]
_FIXED_KEYS = {
    # labels (serve._stage_rgb / _original_labels / color_image)
    *("kind", "label", "text", "id", "tag", "description", "detail", "valid", "calibrated", "cache", "key", "scale", "limited"),
    # photo_meta and the layout request
    *("lens", "lens_short", "aperture", "shutter", "iso", "date", "frame_no", "meta", "mark", "taken", "file_time", "logos"),
    # export_prepare / export_complete / output_sizes / state
    *("canvas", "frame", "stamp", "stamp_on", "photo", "labels", "state", "name", "source", "quality", "long_edge", "hdr"),
    *("result", "sha256", "width", "height", "bytes", "render", "render_label", "format", "render_kind", "hdr_reason"),
    *("recipe_binding", "file_name", "original", "prepared", "sizes", "ratio", "crop_preset", "small", "medium", "large"),
    *("asked", "active", "shape", "problem", "error", "message"),
    # frame and stamp layout items
    *("k", "x", "y", "w", "h", "xs", "xs0", "glow", "rot", "gx", "gy", "tw", "pts", "px", "fill", "font", "ops", "rect", "ink"),
}


def _rust_names(pattern: str) -> set[str]:
    out: set[str] = set()
    for rs in (_REPO / "core" / "crates").glob("gxr-*/src/**/*.rs"):
        out |= set(re.findall(pattern, rs.read_text(encoding="utf-8")))
    return out


# JSON keys the frame and export crates write (`("key", Json…)` / `"key" =>`), and the core error kinds.
_KNOWN_KEYS = frozenset(_FIXED_KEYS | _rust_names(r'\(\s*"([a-z_][a-z0-9_]{0,30})",\s*(?:Json|crate::item|Val|item|v|json)'))
_KNOWN_KINDS = frozenset(
    _rust_names(
        r'(?:CoreError::new|ExportFail::export|Fail::unavailable|EditError::new|EditError::with_detail|\bedit|\bunsupported)\(\s*"([a-z_][a-z0-9_]*)"'
    )
    | _rust_names(r'\(\s*"([a-z_][a-z0-9_]*)",\s*"[^"\n]*[^\x00-\x7f]')  # (code, user-facing message)
    | _rust_names(r'\b(?:kind|code) == "([a-z_][a-z0-9_]*)"')
    | {"raw_decode", "json", "unknown"}
)
_KNOWN_DTYPES = frozenset({"uint8", "uint16", "int16", "int32", "int64", "float32", "float64", "bool"})


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _key(segment: str) -> str:
    return segment if segment in _KNOWN_KEYS else f"#{_sha(segment)}"


def _dtype(d: Any) -> str:
    name = str(d)
    return name if name in _KNOWN_DTYPES and getattr(d, "names", None) is None else f"dtype#{_sha(name)}"


_HERE = Path(__file__).resolve()


def _caller() -> str:
    """The file name and line of the first frame outside this module (frame data only, no
    traceback, no locals), so a failure stays locatable while its label is hashed."""
    import sys

    frame = sys._getframe(1)
    while frame is not None and Path(frame.f_code.co_filename).resolve() == _HERE:
        frame = frame.f_back
    return "?:0" if frame is None else f"{Path(frame.f_code.co_filename).name}:{int(frame.f_lineno or 0)}"


def _fail(label: str, detail: str) -> None:
    """Fail with the label's hash prefix (the label may name a photo), the caller's file:line and
    the helper's own redacted detail."""
    import pytest

    pytest.fail(f"label sha256 {_sha(str(label))} at {_caller()}: {detail}", pytrace=False)


def _diffs(got: Any, want: Any, path: str, out: list[str]) -> None:
    from test_core_parity_export_meta import _digest_value, _values_equal

    if isinstance(got, dict) and isinstance(want, dict):
        if list(got.keys()) != list(want.keys()):
            out.append(f"{path}: keys or key order differ ({len(got)} vs {len(want)} keys)")
        for k in [k for k in got if k in want]:
            _diffs(got[k], want[k], f"{path}.{_key(str(k))}", out)
        return
    if isinstance(got, (list, tuple)) and isinstance(want, (list, tuple)):
        if len(got) != len(want):
            out.append(f"{path}: length {len(got)} vs {len(want)}")
        for i, (a, b) in enumerate(zip(got, want, strict=False)):
            _diffs(a, b, f"{path}[{i}]", out)
        return
    if not _values_equal(got, want):
        out.append(f"{path}: got={_digest_value(got)} want={_digest_value(want)}")


def same_redacted(got: Any, want: Any, label: str) -> None:
    """Exact equality of values that may hold real-photo text (``meta``, layouts, labels, state,
    result fields): field paths (fixed schema; other keys hashed), types, SHA-256 prefixes."""
    out: list[str] = []
    _diffs(got, want, "$", out)
    if out:
        _fail(label, "mismatch: " + "; ".join(out[:20]))


def check(ok: bool, label: str) -> None:
    """A plain ``if`` in place of a bare ``assert``. ``label`` is a literal (the lint checks it,
    positional or ``label=``); like every helper here it is printed only as a hash."""
    if not ok:
        _fail(label, "check failed")


def same_file_redacted(got: bytes | None, want: bytes, label: str) -> None:
    """Whole-file byte equality: lengths, the first differing byte and SHA-256 prefixes only."""
    if got is None:
        _fail(label, f"no file (expected {len(want)} bytes, sha {digest(want)})")
    elif got != want:
        _fail(label, first_difference(got, want))


def same_pixels(got: Any, want: Any, label: str) -> None:
    """Exact array equality: shapes, dtypes (fixed set), the count of differing elements and the
    first index, never the values."""
    if got.shape != want.shape or got.dtype != want.dtype:
        got_shape, want_shape = tuple(int(v) for v in got.shape), tuple(int(v) for v in want.shape)
        _fail(label, f"shape/dtype {got_shape} {_dtype(got.dtype)} vs {want_shape} {_dtype(want.dtype)}")
        return
    bad = np.argwhere(got != want)
    if len(bad):
        _fail(label, f"{len(bad)} values differ, first at {tuple(int(v) for v in bad[0])}")


def error_kind(payload: Any) -> str:
    """The core error kind of a CLI payload when it is a known kind; any other is a hash."""
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        kind = str(payload["error"].get("kind"))
        return kind if kind in _KNOWN_KINDS else f"unknown#{_sha(kind)}"
    return "-"


def cli_ok(code: int, payload: Any, label: str) -> None:
    """A CLI call that must succeed: the exit code and the (known) error kind only."""
    if code != 0:
        _fail(label, f"exit {int(code)}, kind {error_kind(payload)}")


def produced(got: Any, payload: Any, label: str) -> None:
    """A CLI call that must have written its output (``got`` not None): the known error kind only."""
    if got is None:
        _fail(label, f"no output, kind {error_kind(payload)}")


def same_kind(payload: Any, want: str, label: str) -> None:
    """A CLI error payload of kind ``want`` (a literal): known kinds only, others hashed."""
    got = error_kind(payload)
    if got != want:
        _fail(label, f"kind {got} vs {want if want in _KNOWN_KINDS else _sha(want)}")


def synthetic_failure(ok: bool, text: str, label: str) -> None:
    """For runs on synthetic inputs only (the WASM comparison reads no real photo, DAT or
    calibration): fail with the run's own output, which names the differing synthetic cases."""
    if not ok:
        _fail(label, text)


_LINT = re.compile(
    r"^E_[A-Z_]{2,20} test_core_parity_(?:export_pipeline[a-z_]*|jpeg_optimize)\.py:[0-9]{1,6}$"
    r"|^E_[A-Z_]{2,20} export_pipeline_[a-z_]+\.py:[0-9]{1,6}$"
)


# What to do about each lint rule (fixed text, printed with the report).
LINT_HINTS = {
    "E_ASSERT": "compare with same_redacted / same_file_redacted / same_pixels, or check() on a plain scalar",
    "E_RAISE": "fail with a literal message, or use check()",
    "E_OUTPUT": "record counts with note()",
    "E_IDS": "use literal ids or anon_ids(<count>)",
    "E_LABEL": "give check() a literal label",
    "E_NPTEST": "compare arrays with same_pixels (np.testing prints the values)",
    "E_SYNTHETIC": "synthetic_failure prints its text; use it only in the WASM test on synthetic inputs",
}


def lint_report(violations: list[str]) -> None:
    """The guard's report: each entry is a rule code plus file:line (anything else is hashed),
    followed by the fixed hint for each rule that fired."""
    if violations:
        shown = [v if _LINT.match(v) else f"#{_sha(v)}" for v in violations]
        hints = [f"{code}: {LINT_HINTS[code]}" for code in sorted({v.split()[0] for v in shown}) if code in LINT_HINTS]
        _fail("lint", ", ".join(shown) + " | " + "; ".join(hints))


def anon_ids(count: int) -> list[str]:
    """Anonymous parametrize ids (``P000``, ``P001``, …) from a count, so no value reaches a test id."""
    return [f"P{i:03d}" for i in range(int(count))]


def note(name: str, value: Any) -> None:
    """Record a tally (counts and reason codes only) for the report, outside pytest's output:
    appended as one JSON line to ``$EXPORT_PIPELINE_NOTES`` when that is set."""
    path = os.environ.get("EXPORT_PIPELINE_NOTES")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"name": name, "value": value}, ensure_ascii=False, sort_keys=True) + "\n")
