"""Negative controls for the frame-export canvas gate. Synthetic glyphs only; no photos."""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from gxr_studio.finish.fonts import PilFonts
from support import mobile_ui_frame_export as fx
from support.export_pipeline_cases import check


def _atkinson_a() -> np.ndarray:
    glyph = Image.new("RGB", (64, 64), (0, 0, 0))
    ImageDraw.Draw(glyph).text((8, 8), "A", font=PilFonts().face("atkinson", 32, False), fill=(255, 255, 255))
    return np.asarray(glyph).copy()


def test_matching_glyph_keeps_full_coverage() -> None:
    """An untouched glyph is exact, so coverage stays at 1 and attribution is not required."""
    py = _atkinson_a()
    blank = np.zeros_like(py)
    ok, horiz, vert, ratio = fx._glyph_ok(py, py, blank, (0, 0, 64, 64), "atkinson", 32, "A")
    check(ok is True and horiz == 0 and vert == 0 and ratio == 1.0, "identical atkinson A")


def test_squashed_atkinson_a_fails_coverage_sum() -> None:
    """Sol's control: every non-zero coverage of a 32 px Atkinson A squashed to 1 must fail."""
    py = _atkinson_a()
    phone = (py > 0).astype(np.uint8)
    blank = np.zeros_like(py)
    ok, _horiz, _vert, ratio = fx._glyph_ok(phone, py, blank, (0, 0, 64, 64), "atkinson", 32)
    check(ok is False, "squashed atkinson A must fail")
    check(ratio is not None and ratio < 0.83, "squashed coverage is under the frozen floor")


def test_colour_wrong_glyph_fails() -> None:
    """Same coverage shape in another colour is not hinting and must fail."""
    py = _atkinson_a()
    ink = np.any(py > 0, axis=2)
    shifted = py.astype(np.int16)
    shifted[..., 0] = np.clip(shifted[..., 0].astype(np.int16) + 48, 0, 255)
    shifted[..., 2] = np.clip(shifted[..., 2].astype(np.int16) - 48, 0, 255)
    phone = shifted.astype(np.uint8)
    phone[~ink] = 0
    blank = np.zeros_like(py)
    ok, _horiz, _vert, _ratio = fx._glyph_ok(phone, py, blank, (0, 0, 64, 64), "atkinson", 32)
    check(ok is False, "colour-wrong glyph must fail")
