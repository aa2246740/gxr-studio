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

import os
from pathlib import Path
from typing import NoReturn

import pytest

STUDIO = Path(__file__).resolve().parents[2]
REQUIRED_MARK = "gxr_sample_missing"


def sample_parent() -> Path:
    """The parent project folder that holds ``captures/`` and ``research/``."""
    env = os.environ.get("GXR_STUDIO_TEST_PARENT")
    return Path(env).resolve() if env else STUDIO.parent


def captures() -> Path:
    return sample_parent() / "captures"


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
