"""Negative and positive controls for the frame-export canvas gates. Synthetic glyphs only; no photos."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from gxr_studio.finish import fonts as fonts_mod
from gxr_studio.finish.fonts import FontMetrics, PilFonts
from gxr_studio.finish.layout import frame_layout
from gxr_studio.finish.spec import FrameSpec, StampSpec
from support import mobile_ui_frame_export as fx
from support.export_pipeline_cases import check
from support.ink_text_cases import dehinted_font_paths, font_path, open_pil_face, pillow_mask
from support.mobile_ui_frame_gate import (
    GlyphReport,
    Mask,
    MissingGlyph,
    Scene,
    _blur_backdrop,
    _prove_dehinted,
    compare_canvas,
    composite_ok,
    mask_gate,
    rust_glyphs,
)

REAL = {name: font_path(name) for name in ("atkinson", "inter", "inter_sb", "courier", "patrick", "dseg")}
A_KEY = ("atkinson", 32, "A")


def _rejected(fn: Any, *args: Any, **kwargs: Any) -> bool:
    try:
        fn(*args, **kwargs)
    except BaseException as err:
        return type(err).__name__ == "Failed"
    return False


def _pillow_a() -> Mask:
    return pillow_mask(open_pil_face(REAL["atkinson"], 32), "A")


def _draw_a(background: np.ndarray, fill: tuple[int, int, int]) -> np.ndarray:
    """``fonts.draw_run`` (the desktop path) for one 32 px Atkinson A at (8, 8)."""
    image = Image.fromarray(background.copy(), "RGB")
    run = {"font": "atkinson", "px": 32, "cjk": False, "text": "A", "xs": [8], "y": 8}
    fonts_mod.draw_run(ImageDraw.Draw(image), run, PilFonts(), fill)
    return np.asarray(image).copy()


def _draw_a_with(mask: Mask, background: np.ndarray, fill: tuple[int, int, int]) -> np.ndarray:
    """The same draw, but blending ``mask`` (what the composite gate does with the phone's masks)."""
    with rust_glyphs({A_KEY: mask}, REAL) as used:
        out = _draw_a(background, fill)
    check(used == {A_KEY}, "one glyph drawn")
    return out


def _squashed(mask: Mask) -> Mask:
    width, height, x_off, y_off, buf = mask
    arr = np.frombuffer(buf, dtype=np.uint8).copy()
    arr[arr > 0] = 1
    return width, height, x_off, y_off, arr.tobytes()


def _all(shape: tuple[int, ...]) -> np.ndarray:
    return np.ones(shape[:2], dtype=bool)


def test_identical_masks_pass_both_gates() -> None:
    """Positive control: Pillow's own mask passes the mask gate exactly and composites to Pillow's canvas."""
    mask = _pillow_a()
    result = mask_gate(mask, mask, "atkinson", 32)
    check(result.ok and result.diff_px == 0 and result.ratio == 1.0, "identical masks")
    black = np.zeros((64, 64, 3), dtype=np.uint8)
    phone = _draw_a(black, (255, 255, 255))
    stats = composite_ok(phone, _draw_a_with(mask, black, (255, 255, 255)), _all(phone.shape))
    check(stats == {"composite_inside": 0, "composite_whole": 0}, "Pillow mask composites to Pillow's canvas")


def test_squashed_atkinson_a_fails_mask_gate() -> None:
    """Sol r1 control: every non-zero coverage of a 32 px Atkinson A squashed to 1 must fail."""
    result = mask_gate(_squashed(_pillow_a()), _pillow_a(), "atkinson", 32)
    check(result.ok is False, "squashed atkinson A must fail")
    check(result.ratio is not None and result.ratio < 0.83, "squashed coverage is under the frozen floor")


def test_textured_background_half_coverage_fails_mask_gate() -> None:
    """Sol r2 control: white A on alternating 0/240 with the coverage on every bright pixel removed.

    The RGB-derived estimate accepted it at 94 %. On the real masks the coverage sum is 50 % and the
    mask gate fails, whatever the background; the canvas itself is consistent with that mask.
    """
    mask_image = Image.new("L", (64, 64), 0)
    ImageDraw.Draw(mask_image).text((8, 8), "A", font=PilFonts().face("atkinson", 32, False), fill=255)
    full = np.asarray(mask_image).copy()
    yy, xx = np.indices(full.shape)
    bright = (xx + yy) % 2 == 0
    half = full.copy()
    half[bright] = 0
    ratio = int(half.sum()) / int(full.sum())
    check(0.49 < ratio < 0.51, "half of the coverage removed")
    as_mask = (64, 64, -8, -8, half.tobytes())
    reference = (64, 64, -8, -8, full.tobytes())
    result = mask_gate(as_mask, reference, "atkinson", 32)
    check(result.ok is False and result.ratio is not None and result.ratio < 0.83, "half coverage fails the mask gate")
    background = np.repeat(np.where(bright, 240, 0).astype(np.uint8)[..., None], 3, axis=2)
    phone = np.asarray(Image.composite(Image.new("RGB", (64, 64), "white"), Image.fromarray(background), Image.fromarray(half))).copy()
    stats = composite_ok(phone, _draw_a_with(as_mask, background, (255, 255, 255)), _all(phone.shape))
    check(stats["composite_whole"] == 0, "the canvas is what that mask blends to")


def test_colour_change_fails_composite_gate() -> None:
    """Sol r2 control: a real Pillow glyph drawn in RGB 255/250/255 instead of white fails the composite gate."""
    mask = _pillow_a()
    black = np.zeros((64, 64, 3), dtype=np.uint8)
    phone = _draw_a(black, (255, 250, 255))
    check(mask_gate(mask, mask, "atkinson", 32).ok, "the mask itself is right")
    expected = _draw_a_with(mask, black, (255, 255, 255))
    changed = int(np.count_nonzero(np.any(phone != expected, axis=-1)))
    check(changed > 0, "colour change touches the glyph")
    check(_rejected(composite_ok, phone, expected, _all(phone.shape)), "colour change fails the composite gate")


def test_canvas_not_blended_from_the_reported_mask_fails() -> None:
    """A report that does not match what was blended fails: the canvas has the squashed glyph, the report the full one."""
    black = np.zeros((64, 64, 3), dtype=np.uint8)
    phone = _draw_a_with(_squashed(_pillow_a()), black, (255, 255, 255))
    expected = _draw_a_with(_pillow_a(), black, (255, 255, 255))
    check(_rejected(composite_ok, phone, expected, _all(phone.shape)), "mask and canvas disagree")


def test_missing_glyph_never_falls_back_to_pillow() -> None:
    """A glyph the phone did not report raises; Pillow's rasterizer is not used in its place."""
    black = np.zeros((64, 64, 3), dtype=np.uint8)
    raised = False
    try:
        with rust_glyphs({}, REAL):
            _draw_a(black, (255, 255, 255))
    except MissingGlyph:
        raised = True
    check(raised, "missing glyph raises")


class _PillowMasks(dict[tuple[str, int, str], Mask]):
    """Pillow's masks on demand: stands in for a phone whose Rust masks equal Pillow's."""

    def __init__(self, fonts: dict[str, Path]) -> None:
        super().__init__()
        self.fonts = fonts

    def __missing__(self, key: tuple[str, int, str]) -> Mask:
        font, px, ch = key
        self[key] = pillow_mask(open_pil_face(self.fonts[font], px), ch)
        return self[key]


def _report(scene: Scene, fonts: dict[str, Path]) -> GlyphReport:
    masks = _PillowMasks(fonts)
    with rust_glyphs(masks, fonts):
        scene.paint()
    digests = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in fonts.items()}
    return GlyphReport(False, [], dict(masks), [], digests)


def _blur_scene() -> Scene:
    frame = FrameSpec(style="blur_fill", show_logo=False, show_params=False, show_date=False)
    stamp = StampSpec(enabled=True, content="text", text="TEST", font="sans", glow=False)
    photo = np.full((240, 320, 3), 90, dtype=np.uint8)
    layout = frame_layout(320, 240, frame, {}, FontMetrics())
    check(layout is not None, "synthetic layout")
    assert layout is not None
    return Scene(photo, layout, frame, stamp, None, None, None)


def test_blur_scene_passes_every_gate(tmp_path: Path) -> None:
    """Positive control on a real blur_fill + stamp paint: Pillow's masks pass all gates and the de-hinted proof."""
    scene = _blur_scene()
    dehint = dehinted_font_paths(tmp_path)
    phone = scene.paint()
    stats = compare_canvas(phone, scene, _report(scene, REAL), REAL, dehint)
    check(stats["outside"] == 0 and stats["composite_whole"] == 0 and stats["mask_exact"] == stats["glyphs"] > 0, "clean scene")
    proof = _prove_dehinted(scene, scene.paint_fonts(dehint), _report(scene, dehint), dehint, [{"y": 0, "pixels": 1}])
    check(proof["dehinted_equal"] is True, "de-hinted proof on the de-hinted paint")


def test_inverted_backdrop_fails_both_runs(tmp_path: Path) -> None:
    """Sol's control: inverting the blur backdrop fails the hinted gates and the de-hinted proof."""
    scene = _blur_scene()
    dehint = dehinted_font_paths(tmp_path)
    reference = scene.paint()
    backdrop = _blur_backdrop(scene.layout, *reference.shape[:2])
    mutated = reference.copy()
    mutated[backdrop] = 255 - mutated[backdrop]
    changed = int(np.count_nonzero(np.any(mutated != reference, axis=-1)))
    check(changed == 101808, "inverted backdrop pixel count")
    report = _report(scene, REAL)
    dh_report = _report(scene, dehint)
    check(_rejected(compare_canvas, mutated, scene, report, REAL, dehint, mutated, dh_report), "hinted run rejects the inverted backdrop")
    rows = [{"y": 0, "pixels": changed}]
    check(_rejected(_prove_dehinted, scene, mutated, dh_report, dehint, rows), "de-hinted run rejects the inverted backdrop")


def _memory_rejected(step: dict[str, Any]) -> bool:
    return _rejected(fx.memory_ok, step, hdr=False)


def _step(wasm: int, js: int, cap: int, estimate: int | None) -> dict[str, Any]:
    facts = {"S0": {"estimate": estimate}} if estimate is not None else {}
    return {"facts": {"facts": facts, "mem": {"wasm": wasm, "js": js, "workers": 1}, "stagedHigh": 1, "cap": cap}}


def test_page_budget_old_check_passes_until_the_cap_drops() -> None:
    """Sol's control: WASM 512 + JS 400 + cap 384 fails once JS is counted, and the cap must drop."""
    wasm = 512 * fx.MIB
    js = 400 * fx.MIB
    cap = 384 * fx.MIB
    step = _step(wasm, js, cap, wasm + js)
    check(wasm + cap <= fx.PAGE_BUDGET, "old wasm plus cap check would pass")
    check(wasm + js + cap > fx.PAGE_BUDGET, "same-stage total plus the old cap is over budget")
    check(_memory_rejected(step), "unlowered cap fails")
    lowered = fx.fit_stage_cap(wasm + js, cap)
    check(0 < lowered < cap, "cap drops below the default")
    row = fx.memory_ok(_step(wasm, js, lowered, wasm + js), hdr=False)
    check(row["cap"] == lowered and int(row["peak"]) + lowered <= fx.PAGE_BUDGET, "lowered cap fits the page budget")


def test_preflight_estimate_must_bound_the_measured_peak() -> None:
    """The batch's estimate before the photo must not be below what the photo then used, and must exist."""
    wasm, js, cap = 300 * fx.MIB, 100 * fx.MIB, 384 * fx.MIB
    check(fx.memory_ok(_step(wasm, js, cap, wasm + js), hdr=False)["estimate"] == wasm + js, "exact estimate passes")
    check(_memory_rejected(_step(wasm, js, cap, wasm + js - 1)), "estimate under the peak fails")
    check(_memory_rejected(_step(wasm, js, cap, None)), "missing estimate fails")
