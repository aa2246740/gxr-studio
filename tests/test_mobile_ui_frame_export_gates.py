"""Negative controls for the frame-export canvas gate. Synthetic glyphs only; no photos."""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from gxr_studio.finish.fonts import FontMetrics, PilFonts
from gxr_studio.finish.layout import frame_layout
from gxr_studio.finish.spec import FrameSpec, StampSpec
from support import mobile_ui_frame_export as fx
from support.export_pipeline_cases import check
from support.mobile_ui_frame_gate import _blur_backdrop, _glyph_ok, compare_canvas


def _atkinson_a() -> np.ndarray:
    glyph = Image.new("RGB", (64, 64), (0, 0, 0))
    ImageDraw.Draw(glyph).text((8, 8), "A", font=PilFonts().face("atkinson", 32, False), fill=(255, 255, 255))
    return np.asarray(glyph).copy()


def test_matching_glyph_keeps_full_coverage() -> None:
    """An untouched glyph is exact, so coverage stays at 1 and attribution is not required."""
    py = _atkinson_a()
    blank = np.zeros_like(py)
    ok, horiz, vert, ratio = _glyph_ok(py, py, blank, (0, 0, 64, 64), "atkinson", 32, "A")
    check(ok is True and horiz == 0 and vert == 0 and ratio == 1.0, "identical atkinson A")


def test_squashed_atkinson_a_fails_coverage_sum() -> None:
    """Sol's control: every non-zero coverage of a 32 px Atkinson A squashed to 1 must fail."""
    py = _atkinson_a()
    phone = (py > 0).astype(np.uint8)
    blank = np.zeros_like(py)
    ok, _horiz, _vert, ratio = _glyph_ok(phone, py, blank, (0, 0, 64, 64), "atkinson", 32)
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
    ok, _horiz, _vert, _ratio = _glyph_ok(phone, py, blank, (0, 0, 64, 64), "atkinson", 32)
    check(ok is False, "colour-wrong glyph must fail")


def _rejected(canvas: np.ndarray, photo: np.ndarray, layout: object, frame: FrameSpec, stamp: StampSpec, dehinted: np.ndarray | None) -> bool:
    try:
        compare_canvas(canvas, photo, layout, frame, stamp, None, None, None, dehinted_phone=dehinted)
    except BaseException as err:
        return type(err).__name__ == "Failed"
    return False


def test_inverted_backdrop_fails_both_runs() -> None:
    """Sol's control: inverting the blur backdrop fails the hinted gate and the de-hinted proof."""
    frame = FrameSpec(style="blur_fill", show_logo=False, show_params=False, show_date=False)
    stamp = StampSpec(enabled=True, content="text", text="TEST", font="sans", glow=False)
    photo = np.full((240, 320, 3), 90, dtype=np.uint8)
    layout = frame_layout(320, 240, frame, {}, FontMetrics())
    check(layout is not None, "synthetic layout")
    if layout is None:
        return
    reference = fx.paint_reference(photo, layout, frame, stamp, None, None, None, glyphs=True)
    backdrop = _blur_backdrop(layout, *reference.shape[:2])
    mutated = reference.copy()
    mutated[backdrop] = 255 - mutated[backdrop]
    changed = int(np.count_nonzero(np.any(mutated != reference, axis=-1)))
    check(changed == 101808, "inverted backdrop pixel count")
    check(_rejected(mutated, photo, layout, frame, stamp, None), "hinted run rejects the inverted backdrop")
    check(_rejected(mutated, photo, layout, frame, stamp, mutated), "de-hinted run rejects the inverted backdrop")
