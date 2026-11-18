"""Where tests find real samples, research code and the project folder.

None of these are in the repo. They live in the parent project folder, next to
the studio checkout: ``captures/`` holds the camera samples, ``research/`` the
research code. Every lookup in ``tests/`` goes through :func:`sample_parent`:

- ``GXR_STUDIO_TEST_PARENT`` when it is set (T3 worktrees and other checkouts
  that are not inside the project folder);
- otherwise the folder above the checkout, so the main checkout
  (``<project>/studio``) works with no variable.

When something is missing the test skips with its usual message. With
``GXR_REQUIRE_SAMPLES=1`` it fails instead, like ``GXR_REQUIRE_CORE``. The
public repo has no samples, so this is off by default.
"""

from __future__ import annotations

import hashlib
import os
import struct
from collections.abc import Iterable
from pathlib import Path
from typing import Any, BinaryIO, NoReturn

import pytest

STUDIO = Path(__file__).resolve().parents[2]
REQUIRED_MARK = "gxr_sample_missing"

# Stable real samples (unit parity-drift-1, 2026-11-18). A capture is in a helper's sample when its
# own key is below the helper's share; adding, removing or renaming other files never moves it in
# or out. The share is fixed, so a sample grows with captures/ instead of shifting.
SAMPLE_SALT = b"gxr-studio real-sample v1:"
# Share of captures/ per requested sample. On 2026-11-18 captures/ held 1,336 usable RAW+JPG pairs;
# 1/954 gives about 1.4 times the old spread's count. A hash sample is random across folders, so
# it needs that margin: with it every helper call keeps at least its old count and its old number
# of capture folders.
SAMPLE_UNIT = 1 / 954
# The smallest request: callers that need one or two pairs take them from this share.
SAMPLE_FLOOR = 8


def sample_parent() -> Path:
    """The parent project folder that holds ``captures/`` and ``research/``."""
    env = os.environ.get("GXR_STUDIO_TEST_PARENT")
    return Path(env).resolve() if env else STUDIO.parent


def captures() -> Path:
    return sample_parent() / "captures"


def sample_key(path: Path, root: Path) -> float:
    """A file's place in [0, 1): SHA-256 of ``SAMPLE_SALT`` and its POSIX path relative to ``root``."""
    rel = path.relative_to(root).as_posix().encode("utf-8", "surrogatepass")
    return int.from_bytes(hashlib.sha256(SAMPLE_SALT + rel).digest()[:8], "big") / 2.0**64


def sample_share(limit: int) -> float:
    """The fixed share of captures/ a helper asked for ``limit`` samples takes."""
    return min(1.0, max(limit, SAMPLE_FLOOR) * SAMPLE_UNIT)


def stable_sample(paths: Iterable[Path], root: Path, limit: int) -> list[Path]:
    """The paths whose :func:`sample_key` is below :func:`sample_share` of ``limit``, in path order.

    Whether a file is picked depends only on its own path, so files added elsewhere under
    ``root`` never change it. The count is not capped: it is about 1.4 times ``limit`` at the
    2026-11-18 corpus (never fewer than the old spread gave) and grows with captures/.
    """
    share = sample_share(limit)
    return sorted((p for p in paths if sample_key(p, root) < share), key=lambda p: p.as_posix())


def pinned_capture(rel_sha: str, content_sha: str) -> Path:
    """The capture whose path relative to captures/ has SHA-256 prefix ``rel_sha`` (no name in the repo).

    The file's own SHA-256 must start with ``content_sha``, so a pinned regression never runs on
    another photo. Missing: skip, or fail under ``GXR_REQUIRE_SAMPLES=1``.
    """
    root = captures()
    found = []
    if root.is_dir():
        found = [
            p
            for p in root.rglob("*")
            if p.is_file() and hashlib.sha256(p.relative_to(root).as_posix().encode("utf-8", "surrogatepass")).hexdigest().startswith(rel_sha)
        ]
    if len(found) != 1:
        missing(f"pinned capture {rel_sha} is not under captures/ ({len(found)} matches)")
    got = hashlib.sha256(found[0].read_bytes()).hexdigest()
    if not got.startswith(content_sha):
        pytest.fail(f"pinned capture {rel_sha} changed: content sha256 {got[:16]}, expected {content_sha}")
    return found[0]


def raw_ifd_compression(path: Path) -> int | None:
    """Compression of the full-resolution CFA IFD, or ``None`` if that tag cannot be read.

    Stock GXR firmware writes uncompressed samples (1). Modified-firmware field DNGs use 7
    (lossless JPEG), which the desktop decoder refuses. A missing compression tag counts as 1,
    the TIFF default. ``None`` means the caller keeps the file: only a known value other than 1
    is skipped, never a read error.
    """
    try:
        with path.open("rb") as handle:
            return _raw_ifd_compression(handle)
    except (OSError, ValueError, struct.error, IndexError):
        return None


def _raw_ifd_compression(handle: BinaryIO) -> int | None:
    head = handle.read(8)
    if len(head) < 8 or head[:2] not in (b"II", b"MM"):
        return None
    endian = "<" if head[:2] == b"II" else ">"
    if struct.unpack_from(endian + "H", head, 2)[0] != 42:
        return None

    def ifd(offset: int) -> dict[int, tuple[int, int, int]]:
        handle.seek(offset)
        counted = handle.read(2)
        if len(counted) < 2:
            raise ValueError("short IFD")
        count = struct.unpack(endian + "H", counted)[0]
        entries = handle.read(12 * count)
        if len(entries) < 12 * count:
            raise ValueError("short IFD")
        out: dict[int, tuple[int, int, int]] = {}
        for index in range(count):
            tag, typ, number, raw = struct.unpack_from(endian + "HHII", entries, index * 12)
            out[tag] = (typ, number, raw)
        return out

    def ints(entry: tuple[int, int, int]) -> list[int] | None:
        typ, number, raw = entry
        size = {3: 2, 4: 4}.get(typ)
        if size is None or number < 1:
            return None
        if size * number <= 4:
            blob = struct.pack(endian + "I", raw)
        else:
            handle.seek(raw)
            blob = handle.read(size * number)
            if len(blob) < size * number:
                raise ValueError("short tag")
        return list(struct.unpack(endian + ("H" if size == 2 else "I") * number, blob[: size * number]))

    root = ifd(struct.unpack_from(endian + "I", head, 4)[0])
    found = [root]
    sub = root.get(330)
    if sub is not None:
        for offset in ints(sub) or []:
            try:
                found.append(ifd(offset))
            except (OSError, ValueError, struct.error):
                continue
    for directory in found:
        photo = ints(directory[262]) if 262 in directory else None
        kind = ints(directory[254]) if 254 in directory else [0]
        if not photo or photo[0] != 32803 or not kind or kind[0] != 0:
            continue
        if 259 not in directory:
            return 1
        got = ints(directory[259])
        return int(got[0]) if got else None
    return None


def a12_50_samples() -> Path:
    """The four-pair a12-50 set that synthetic cards copy (same folder as ``DEFAULT_SAMPLES``)."""
    return captures() / "a12-50" / "2026-10-19" / "stage2c-first-run"


def samples_required() -> bool:
    return os.environ.get("GXR_REQUIRE_SAMPLES") == "1"


def missing(reason: str) -> NoReturn:
    """Skip with ``reason``, or fail with it under ``GXR_REQUIRE_SAMPLES=1``."""
    if samples_required():
        pytest.fail(f"{reason}（GXR_REQUIRE_SAMPLES=1，缺样本算失败；GXR_STUDIO_TEST_PARENT={sample_parent()}）")
    pytest.skip(reason)


def needs(present: bool, reason: str) -> pytest.MarkDecorator:
    """A mark for tests that need a sample: ``skipif(not present)``, or a failure under ``GXR_REQUIRE_SAMPLES=1``.

    The failure mark is turned into ``pytest.fail`` by ``tests/conftest.py`` at setup.
    """
    if present or not samples_required():
        return pytest.mark.skipif(not present, reason=reason)
    return getattr(pytest.mark, REQUIRED_MARK)(reason=reason)


def fail_if_marked(item: pytest.Item) -> None:
    """``pytest_runtest_setup`` half of :func:`needs`."""
    mark = item.get_closest_marker(REQUIRED_MARK)
    if mark is not None:
        missing(str(mark.kwargs["reason"]))


def need_sample(present: bool, reason: str) -> None:
    """Inside a test: under ``GXR_REQUIRE_SAMPLES=1`` a missing sample fails; otherwise nothing changes.

    Callers keep their own skip or ``GXR_REQUIRE_CORE`` logic after this call.
    """
    if not present and samples_required():
        missing(reason)


def real_params(values: list[Any], reason: str) -> list[Any]:
    """Parametrize values found on disk. An empty list stays empty by default (pytest's ``NOTSET`` skip).

    Under ``GXR_REQUIRE_SAMPLES=1`` an empty list becomes one ``NOTSET`` case that fails at setup with ``reason``.
    """
    if values or not samples_required():
        return values
    return [pytest.param(None, marks=needs(False, reason), id="NOTSET")]
