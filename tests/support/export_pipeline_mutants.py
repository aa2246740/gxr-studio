"""Deliberately leaky tests (Sol r3): each one fails in a way that would put a synthetic EXIF canary
into the pytest output, and each carries ``@redact_failures``. A parent test runs this file in a
child pytest (``-o python_files=*.py``) under ``-q`` and ``-vv`` and checks that every test fails,
that each failure names the line marked ``# MUTANT``, and that the canary reaches none of stdout,
stderr or JUnit. This file is the guard's one exclusion: it is the corpus of what the guard bans.
The canary is read back with Pillow from a synthetic JPEG's EXIF Make, as a real lens name would
be."""

from __future__ import annotations

import io
import logging
import os
import warnings
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from support.export_pipeline_canary import CANARY
from support.export_pipeline_cases import check, redact_failures


def _lens() -> str:
    folder = Path(os.environ["EXPORT_PIPELINE_CANARY_DIR"])
    folder.mkdir(parents=True, exist_ok=True)
    jpg = folder / "mutant.JPG"
    exif = Image.Exif()
    exif[271] = CANARY
    buf = io.BytesIO()
    Image.new("RGB", (4, 3), (1, 2, 3)).save(buf, "JPEG", exif=exif)
    jpg.write_bytes(buf.getvalue())
    with Image.open(jpg) as im:
        return str(im.getexif()[271])


@redact_failures
def test_len_of_a_string() -> None:
    lens = _lens()
    assert len(lens) == 0  # MUTANT len_str


@redact_failures
def test_len_of_a_dict() -> None:
    lens = _lens()
    assert len({"lens": lens}) == 0  # MUTANT len_dict


@redact_failures
def test_dynamic_format_spec() -> None:
    lens = _lens()
    assert 1 == 2, f"{0:{lens}}"  # MUTANT format_spec


@redact_failures
def test_raise_assertion_error() -> None:
    lens = _lens()
    raise AssertionError(lens)  # MUTANT raise


@redact_failures
def test_helper_label() -> None:
    lens = _lens()
    check(False, f"{lens}")  # MUTANT check_label


@redact_failures
def test_print_then_fail() -> None:
    lens = _lens()
    print(lens)  # noqa: T201  the point of this mutant
    assert 1 == 2  # MUTANT print


@redact_failures
def test_logging_then_fail() -> None:
    lens = _lens()
    logging.getLogger("mutant").error("%s", lens)
    assert 1 == 2  # MUTANT logging


@redact_failures
def test_warning_then_fail() -> None:
    lens = _lens()
    warnings.warn(lens, stacklevel=1)
    assert 1 == 2  # MUTANT warning


@redact_failures
def test_pytest_fail_with_data() -> None:
    lens = _lens()
    pytest.fail(lens, pytrace=False)  # MUTANT pytest_fail


@redact_failures
def test_numpy_testing() -> None:
    lens = _lens()
    np.testing.assert_array_equal(np.array([lens]), np.array(["other"]))  # MUTANT np_testing


MUTANTS = 10
