"""Child-pytest probes for plain pytest semantics the export-pipeline tests rely on (Sol r4 P1-01,
P2-03): a yield fixture whose teardown raises must be reported as an error, and ``-W error`` must
turn a warning into a failure. Collected only when a parent test runs this file explicitly
(``-o python_files=*.py``); nothing here reads a photo. Outside the leak lint on purpose: it warns.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator

import pytest


@pytest.fixture
def broken_teardown() -> Iterator[int]:
    yield 1
    raise RuntimeError("SEMANTICS teardown failed")


def test_teardown_failure_is_reported(broken_teardown: int) -> None:
    del broken_teardown


def test_warning_becomes_failure() -> None:
    warnings.warn("SEMANTICS warning", UserWarning, stacklevel=1)
