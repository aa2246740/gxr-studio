"""Shared helpers for ``tests/test_core_parity_export_pipeline*.py``.

Real samples come from ``support.samples.captures()`` (never copied into the repo). EXIF values
from real photos are compared without printing them: a mismatch reports tag ids and hashes only.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import io
import json
import logging
import os
import re
import traceback
import warnings
from collections.abc import Callable
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


def mtime_args(path: Path) -> list[str]:
    """``--mtime-ms`` / ``--tz-offset-min`` as the phone would pass them for ``path``: the file's
    modification time (``File.lastModified``) and the local zone offset at that moment, which is
    what ``datetime.fromtimestamp(path.stat().st_mtime)`` uses on the desktop."""
    from datetime import datetime

    st = path.stat().st_mtime
    offset = datetime.fromtimestamp(st).astimezone().utcoffset()
    minutes = 0 if offset is None else int(offset.total_seconds() // 60)
    return ["--mtime-ms", repr(st * 1000.0), "--tz-offset-min", str(minutes)]


# ---- Redaction at the exit (Sol r1–r3 P1) -------------------------------------------------
#
# Every test and fixture in tests/test_core_parity_export_pipeline*.py carries @redact_failures
# (the AST guard checks it). Whatever a test raises, the report shows only the exception type, the
# innermost file:line in this unit's files and a SHA-256 prefix of the original message, plus the
# detail a redaction helper built itself (lengths, offsets, shapes, counts, identifier-only field
# paths, error kinds). Labels and messages are never shown, only hashed.

_SAFE_SEGMENT = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]{0,40}|\[[0-9]{1,9}\])$")
_SAFE_WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$")
_HELPERS = {"_fail", "check", "same_redacted", "same_file_redacted", "same_pixels", "cli_ok", "_diffs"}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


class RedactedMismatch(Exception):  # noqa: N818  the name reads as what failed
    """A failed comparison: ``detail`` is built by the helper from safe facts only; ``label``
    (caller text, possibly holding photo data) is kept only as a hash."""

    def __init__(self, detail: str, label: str) -> None:
        super().__init__(detail)
        self.detail = detail
        self.label_sha = _sha(label)

    def __str__(self) -> str:
        return f"{self.detail} (label sha256 {self.label_sha})"


def _path_join(path: str, segment: str) -> str:
    return f"{path}.{segment}" if _SAFE_SEGMENT.match(segment) else f"{path}.?"


def _diffs(got: Any, want: Any, path: str, out: list[str]) -> None:
    from test_core_parity_export_meta import _digest_value, _values_equal

    if isinstance(got, dict) and isinstance(want, dict):
        if list(got.keys()) != list(want.keys()):
            out.append(f"{path}: keys or key order differ ({len(got)} vs {len(want)} keys)")
        for k in [k for k in got if k in want]:
            _diffs(got[k], want[k], _path_join(path, str(k)), out)
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
    result fields): a mismatch reports field paths (identifier keys only), types and SHA-256
    prefixes (export-meta's ``_digest_value``)."""
    out: list[str] = []
    _diffs(got, want, "$", out)
    if out:
        raise RedactedMismatch("mismatch: " + "; ".join(out[:20]), label)


def check(ok: bool, label: str) -> None:
    """A plain ``if`` in place of a bare ``assert``; the label is only hashed."""
    if not ok:
        raise RedactedMismatch("check failed", label)


def same_file_redacted(got: bytes | None, want: bytes, label: str) -> None:
    """Whole-file byte equality: lengths, the first differing byte and SHA-256 prefixes only."""
    if got is None:
        raise RedactedMismatch(f"no file (expected {len(want)} bytes, sha {digest(want)})", label)
    if got != want:
        raise RedactedMismatch(first_difference(got, want), label)


def same_pixels(got: Any, want: Any, label: str) -> None:
    """Exact array equality: shapes, dtypes, the count of differing elements and the first index."""
    if got.shape != want.shape or got.dtype != want.dtype:
        raise RedactedMismatch(f"shape/dtype {got.shape} {got.dtype} vs {want.shape} {want.dtype}", label)
    bad = np.argwhere(got != want)
    if len(bad):
        raise RedactedMismatch(f"{len(bad)} values differ, first at {tuple(int(v) for v in bad[0])}", label)


def error_kind(payload: Any) -> str:
    """The core error kind of a CLI payload, if it is an identifier (never the message)."""
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        kind = str(payload["error"].get("kind"))
        return kind if _SAFE_WORD.match(kind) else "?"
    return "-"


def cli_ok(code: int, payload: Any, label: str) -> None:
    """A CLI call that must succeed: the exit code and error kind only."""
    if code != 0:
        raise RedactedMismatch(f"exit {int(code)}, kind {error_kind(payload)}", label)


def _ours(filename: str) -> bool:
    p = Path(filename)
    return p.name.startswith("test_core_parity_export_pipeline") or (p.parent.name == "support" and p.name.startswith("export_pipeline_"))


def _site(exc: BaseException) -> str:
    """The innermost frame in this unit's files, skipping the redaction helpers themselves."""
    site = "?"
    for frame, line in traceback.walk_tb(exc.__traceback__):
        code = frame.f_code
        if not _ours(code.co_filename):
            continue
        if Path(code.co_filename).name == "export_pipeline_cases.py" and code.co_name in _HELPERS | {"wrapper"}:
            continue
        site = f"{Path(code.co_filename).name}:{line}"
    return site


def _summary(exc: BaseException) -> str:
    kind = type(exc).__name__
    if isinstance(exc, RedactedMismatch):
        return f"redacted {kind} at {_site(exc)}: {exc.detail}; label sha256 {exc.label_sha}"
    try:
        text = str(exc)
    except Exception:  # noqa: BLE001  an exception whose str() fails still gets reported
        text = "<unprintable>"
    return f"redacted {kind} at {_site(exc)}; message sha256 {_sha(text)}"


def redact_failures(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a test or fixture so that no failure can carry photo data to stdout, stderr or JUnit.

    - pytest's skip, xfail and exit outcomes pass through unchanged;
    - any other exception becomes ``pytest.fail(summary, pytrace=False)``, raised after the
      ``except`` block so neither ``__cause__`` nor ``__context__`` holds the original;
    - while the test runs, ``sys.stdout`` / ``sys.stderr`` writes, logging and warnings are
      swallowed, so captured-output and warning sections cannot carry data either.
    """
    import pytest

    passthrough = (pytest.skip.Exception, pytest.xfail.Exception, pytest.exit.Exception, KeyboardInterrupt)

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        summary: str | None = None
        sink = io.StringIO()
        previous = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        try:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink), warnings.catch_warnings():
                warnings.simplefilter("ignore")
                try:
                    return fn(*args, **kwargs)
                except passthrough:
                    raise
                except BaseException as exc:  # noqa: BLE001  every failure is redacted here
                    summary = _summary(exc)
                    del exc
        finally:
            logging.disable(previous)
        pytest.fail(summary or "redacted failure", pytrace=False)

    return wrapper


def anon_ids(prefix: str, values: Any) -> list[str]:
    """Anonymous parametrize ids (``P000``, ``P001``, …), so no value reaches a test id."""
    return [f"{prefix}{i:03d}" for i in range(len(values))]


def note(name: str, value: Any) -> None:
    """Record a tally (counts and reason codes only) for the report, outside pytest's output:
    appended as one JSON line to ``$EXPORT_PIPELINE_NOTES`` when that is set."""
    path = os.environ.get("EXPORT_PIPELINE_NOTES")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"name": name, "value": value}, ensure_ascii=False, sort_keys=True) + "\n")
