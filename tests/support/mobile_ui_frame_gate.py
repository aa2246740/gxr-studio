"""Canvas gate for a framed phone export.

Pixels outside glyph boxes match the Python paint. Ink inside a box follows ink-text's frozen
Gate B: 8-bit coverage sums, colour, and an exact de-hinted glyph when the coverage differs.
A blur backdrop may differ only when the stamp was drawn before the blur, and only if the same
export with de-hinted fonts then matches Python on every pixel.

Nothing here prints a photo name, a capture time, or EXIF.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from gxr_studio.finish.fonts import FontMetrics, PilFonts
from gxr_studio.finish.layout import FrameLayout, glyph_xs, stamp_layout
from gxr_studio.finish.spec import FrameSpec, StampSpec
from gxr_studio.finish.stamp import format_stamp
from support.export_pipeline_cases import check, note, same_pixels
from support.ink_text_cases import (
    GATE_B_COVERAGE_RATIO_GE_16,
    GATE_B_HORIZ_EDGE_BOUND,
    GATE_B_SMALL_PX_COVERAGE_RATIO,
    GATE_B_SMALL_PX_MIN_SUM,
    GATE_B_VERT_EDGE_BOUNDS,
    compare_masks_common_space,
    dehinted_font_paths,
    open_pil_face,
    patched_pil_fonts,
    pillow_mask,
    run_rust_batch,
)

_GATE_FONT = {
    "atkinson": "atkinson",
    "inter": "inter",
    "inter_sb": "inter_sb",
    "courier": "courier",
    "patrick": "patrick",
    "dseg": "dseg",
    "seg7": "dseg",
    "typewriter": "courier",
    "sans": "atkinson",
}
_DEHINT: dict[str, Path] | None = None
_ATTR_EXACT: dict[tuple[str, int, str], bool] = {}
_GlyphBox = tuple[tuple[int, int, int, int], str, int, float | None, str]


def _host() -> tuple[dict[str, int], Any]:
    """The painter lives in the other module. Import it lazily so the two files can load."""
    from support.mobile_ui_frame_export import GXR_MARK, paint_reference

    return GXR_MARK, paint_reference


def _stamp_on(frame: FrameSpec, stamp: StampSpec) -> str | None:
    if not stamp.enabled:
        return None
    if stamp.placement == "border" and frame.style == "instant":
        return "canvas"
    return "photo"


def _char_box(fonts: PilFonts, ch: str, x: int, y: int, font: str, px: int) -> tuple[int, int, int, int] | None:
    if ch == " " or ord(ch) < 0x20:
        return None
    face = fonts.face(font, px, False)
    left, top, right, bottom = ImageDraw.Draw(Image.new("L", (1, 1))).textbbox((x, y), ch, font=face)
    return int(left), int(top), int(right), int(bottom)


def _rotated_box(item: dict[str, Any], fonts: PilFonts) -> tuple[int, int, int, int]:
    face = fonts.face(str(item["font"]), int(item["px"]), False)
    width = int(item["tw"])
    height = face.getmetrics()[0] + math.ceil(int(item["px"]) * 0.4)
    return int(item["gx"]), int(item["gy"]), int(item["gx"]) + height, int(item["gy"]) + width


def _push(
    boxes: list[_GlyphBox],
    box: tuple[int, int, int, int] | None,
    font: str,
    px: int,
    glow: float | None,
    text: str,
) -> None:
    if box is None or not text:
        return
    boxes.append((box, font, px, glow, text))


def _text_boxes(
    fonts: PilFonts,
    item: dict[str, Any],
    ox: int,
    oy: int,
    glow: float | None,
    boxes: list[_GlyphBox],
) -> None:
    font = str(item["font"])
    px = int(item["px"])
    if item.get("rot"):
        left, top, right, bottom = _rotated_box(item, fonts)
        _push(boxes, (left + ox, top + oy, right + ox, bottom + oy), font, px, glow, str(item["text"]))
        return
    for ch, x in zip(str(item["text"]), item["xs"], strict=True):
        box = _char_box(fonts, ch, int(x) + ox, int(item["y"]) + oy, font, px)
        _push(boxes, box, font, px, glow, ch)


def glyph_boxes(
    layout: FrameLayout,
    frame: FrameSpec,
    stamp: StampSpec,
    taken: Any,
    file_time: Any,
    photo_w: int,
    photo_h: int,
) -> list[_GlyphBox]:
    """Every character box on the finished canvas, plus the glow radius of a date stamp."""
    mark, _unused = _host()
    fonts = PilFonts()
    boxes: list[_GlyphBox] = []
    for item in layout.items:
        kind = item.get("k")
        if kind == "text":
            _text_boxes(fonts, item, 0, 0, None, boxes)
        elif kind == "logo":
            px = max(1, round(1000 * int(item["h"]) / mark["h"]))
            metrics = FontMetrics(fonts)
            for ch, x in zip("GXR", glyph_xs(metrics, "atkinson", px, "GXR", float(item["x"])), strict=True):
                _push(boxes, _char_box(fonts, ch, int(x), int(item["y"]), "atkinson", px), "atkinson", px, None, ch)
    text = format_stamp(stamp, taken, file_time)
    where = _stamp_on(frame, stamp)
    if where is None or not text:
        return boxes
    metrics = FontMetrics(fonts)
    if where == "photo":
        item_w, item_h = _photo_size(layout)
        check(item_w == photo_w and item_h == photo_h, "photo item matches the picture")
        laid = stamp_layout(photo_w, photo_h, stamp, text, metrics)
        origin = layout.photo
    else:
        laid = stamp_layout(photo_w, photo_h, stamp, text, metrics, layout)
        origin = (0, 0)
    if laid is None:
        return boxes
    for op in laid.ops:
        if op.get("k") == "text":
            _text_boxes(fonts, op, origin[0], origin[1], laid.glow, boxes)
    return boxes


def _photo_size(layout: FrameLayout) -> tuple[int, int]:
    photo = next(item for item in layout.items if item.get("k") == "photo")
    return int(photo["w"]), int(photo["h"])


def _window(box: tuple[int, int, int, int], font: str, glow: float | None) -> tuple[int, int, int, int]:
    gate = _GATE_FONT[font]
    pad = math.ceil(3 * glow) + 2 if glow else 0
    hx = max(GATE_B_HORIZ_EDGE_BOUND, pad)
    vy = max(GATE_B_VERT_EDGE_BOUNDS[gate], pad)
    left, top, right, bottom = box
    return left - hx, top - vy, right + hx, bottom + vy


def _clip(box: tuple[int, int, int, int], height: int, width: int) -> tuple[int, int, int, int] | None:
    left, top, right, bottom = box
    left, top = max(0, left), max(0, top)
    right, bottom = min(width, right), min(height, bottom)
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _ratio_ok(font: str, px: int, rs_sum: int, py_sum: int) -> bool:
    if py_sum <= 0:
        return rs_sum == 0
    ratio = rs_sum / py_sum
    gate = _GATE_FONT[font]
    if px >= 16:
        lo, hi = GATE_B_COVERAGE_RATIO_GE_16[gate]
        return lo <= ratio <= hi
    if min(rs_sum, py_sum) < GATE_B_SMALL_PX_MIN_SUM:
        return True
    lo, hi = GATE_B_SMALL_PX_COVERAGE_RATIO
    return lo <= ratio <= hi


def _project_coverage(delta: np.ndarray, ink: np.ndarray) -> np.ndarray:
    """8-bit coverage along one ink colour. A zero ink vector falls back to the strongest channel."""
    denom = float(np.dot(ink, ink))
    if denom <= 0.0:
        return np.clip(np.max(np.abs(delta), axis=2), 0, 255).astype(np.uint8)
    covered = delta.astype(np.float64) @ ink
    return np.clip(np.rint(covered / denom * 255.0), 0, 255).astype(np.uint8)


def _coverage_planes(phone: np.ndarray, py: np.ndarray, blank: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Phone and Python coverage masks, both measured against Python's ink colour."""
    py_delta = py.astype(np.int16) - blank.astype(np.int16)
    phone_delta = phone.astype(np.int16) - blank.astype(np.int16)
    weight = np.abs(py_delta).sum(axis=2)
    if int(weight.max()) == 0:
        zero = np.zeros(py.shape[:2], dtype=np.uint8)
        return _project_coverage(phone_delta, np.zeros(3, dtype=np.float64)), zero
    flat = int(np.argmax(weight))
    row, col = divmod(flat, weight.shape[1])
    ink = py_delta[row, col].astype(np.float64)
    return _project_coverage(phone_delta, ink), _project_coverage(py_delta, ink)


def _mask_of(plane: np.ndarray) -> tuple[int, int, int, int, bytes]:
    height, width = plane.shape
    return width, height, 0, 0, np.ascontiguousarray(plane).tobytes()


def _ink_angle_ok(phone: np.ndarray, py: np.ndarray, blank: np.ndarray) -> bool:
    """Overlapping ink must share a colour. Hinting may change coverage, not the ink direction."""
    py_delta = py.astype(np.float64) - blank.astype(np.float64)
    phone_delta = phone.astype(np.float64) - blank.astype(np.float64)
    both = (np.max(np.abs(py_delta), axis=2) > 0) & (np.max(np.abs(phone_delta), axis=2) > 0)
    if not np.any(both):
        return True
    left, right = py_delta[both], phone_delta[both]
    scale = np.maximum(np.linalg.norm(left, axis=1) * np.linalg.norm(right, axis=1), 1.0)
    sine = np.linalg.norm(np.cross(left, right), axis=1) / scale
    return bool(np.all(sine <= 0.05))


def _dehint_paths() -> dict[str, Path]:
    global _DEHINT
    if _DEHINT is None:
        folder = Path(tempfile.gettempdir()) / "gxr-frame-dehint"
        folder.mkdir(exist_ok=True)
        _DEHINT = dehinted_font_paths(folder)
    return _DEHINT


def glyph_hint_attributed(font: str, px: int, text: str) -> bool:
    """Gate A for one run: de-hinted Rust and de-hinted Pillow masks of each character are exact."""
    gate = _GATE_FONT.get(font)
    if gate is None:
        return False
    paths = _dehint_paths()
    for ch in text:
        if ch == " " or ord(ch) < 0x20:
            continue
        key = (gate, px, ch)
        cached = _ATTR_EXACT.get(key)
        if cached is None:
            rust = run_rust_batch(paths[gate], [px], [ch], paths[gate].parent, tag=f"attr-{gate}-{px}-{ord(ch)}")
            item = rust[0]
            pillow = pillow_mask(open_pil_face(paths[gate], px), ch)
            cached = (item[2], item[3], item[4], item[5], item[6]) == pillow
            _ATTR_EXACT[key] = cached
        if not cached:
            return False
    return True


def _glyph_ok(
    phone: np.ndarray,
    py: np.ndarray,
    blank: np.ndarray,
    window: tuple[int, int, int, int],
    font: str,
    px: int,
    text: str | None = None,
) -> tuple[bool, int, int, float | None]:
    """Frozen Gate B on 8-bit coverage sums, plus colour and de-hinted attribution."""
    left, top, right, bottom = window
    phone_rgb = phone[top:bottom, left:right]
    py_rgb = py[top:bottom, left:right]
    bg = blank[top:bottom, left:right]
    if not _ink_angle_ok(phone_rgb, py_rgb, bg):
        return False, 0, 0, None
    phone_plane, py_plane = _coverage_planes(phone_rgb, py_rgb, bg)
    _diff_px, _max_d, phone_box, py_box, phone_sum, py_sum = compare_masks_common_space(_mask_of(phone_plane), _mask_of(py_plane))
    if (py_box is None) != (phone_box is None):
        return False, 0, 0, None
    horiz = vert = 0
    if py_box is not None and phone_box is not None:
        horiz = max(abs(phone_box[0] - py_box[0]), abs(phone_box[2] - py_box[2]))
        vert = max(abs(phone_box[1] - py_box[1]), abs(phone_box[3] - py_box[3]))
    ratio = None if py_sum == 0 else phone_sum / py_sum
    gate = _GATE_FONT[font]
    ok = horiz <= GATE_B_HORIZ_EDGE_BOUND and vert <= GATE_B_VERT_EDGE_BOUNDS[gate] and _ratio_ok(font, px, phone_sum, py_sum)
    if ok and text and not np.array_equal(phone_plane, py_plane):
        ok = glyph_hint_attributed(font, px, text)
    return ok, horiz, vert, ratio


def _item_bounds(item: dict[str, Any]) -> tuple[int, int, int, int] | None:
    kind = item.get("k")
    if kind == "text":
        xs = item.get("xs") or item.get("xs0") or []
        if not xs:
            return None
        y, px = int(item["y"]), max(1, int(item["px"]))
        return int(min(xs)), y, int(max(xs)) + px, y + px * 2
    if "x" in item and "w" in item:
        return int(item["x"]), int(item["y"]), int(item["x"]) + int(item["w"]), int(item["y"]) + int(item["h"])
    if "x1" in item:
        return int(item["x1"]), int(item["y1"]), int(item["x2"]) + 1, int(item["y2"]) + 1
    return None


def _blur_backdrop(layout: FrameLayout, height: int, width: int) -> np.ndarray:
    """Pixels of a ``blur_fill`` backdrop not covered by the photo or another item.

    The backdrop is a blur of the stamped photo, so a Gate B stamp difference is smeared
    here. Ink-text draws the stamp before that blur.
    """
    mask = np.zeros((height, width), dtype=bool)
    if not layout.bgfx or layout.bgfx.get("kind") != "blur":
        return mask
    mask[:] = True
    for item in layout.items:
        bounds = _item_bounds(item)
        if bounds is None:
            continue
        left, top, right, bottom = _clip(bounds, height, width) or (0, 0, 0, 0)
        if right > left and bottom > top:
            mask[top:bottom, left:right] = False
    return mask


def _stamp_before_blur(layout: FrameLayout, frame: FrameSpec, stamp: StampSpec) -> bool:
    """The stamp is painted on the photo, and the frame then blurs that photo into the backdrop."""
    kind = layout.bgfx.get("kind") if layout.bgfx else None
    return _stamp_on(frame, stamp) == "photo" and kind == "blur"


def _row_counts(mask: np.ndarray) -> list[dict[str, int]]:
    rows: list[dict[str, int]] = []
    for y in np.nonzero(mask.any(axis=1))[0]:
        rows.append({"y": int(y), "pixels": int(np.count_nonzero(mask[int(y)]))})
    return rows


def paint_dehinted(
    photo: np.ndarray,
    layout: FrameLayout,
    frame: FrameSpec,
    stamp: StampSpec,
    taken: Any,
    file_time: Any,
    logo_images: dict[str, Image.Image] | None,
) -> np.ndarray:
    """The same Python paint with ink-text's Gate A fonts (instructions stripped, native loader kept)."""
    _mark, paint_reference = _host()
    with patched_pil_fonts(_dehint_paths()):
        return paint_reference(photo, layout, frame, stamp, taken, file_time, logo_images, glyphs=True)


def _prove_dehinted(
    phone: np.ndarray | None,
    photo: np.ndarray,
    layout: FrameLayout,
    frame: FrameSpec,
    stamp: StampSpec,
    taken: Any,
    file_time: Any,
    logo_images: dict[str, Image.Image] | None,
    rows: list[dict[str, int]],
) -> None:
    """A second export with de-hinted fonts must match Python on every pixel, backdrop included."""
    check(phone is not None, "de-hinted export")
    painted = paint_dehinted(photo, layout, frame, stamp, taken, file_time, logo_images)
    equal = phone is not None and phone.shape == painted.shape and bool(np.array_equal(phone, painted))
    note("ui_m4_frame_dehint", {"pixels": sum(row["pixels"] for row in rows), "rows": len(rows), "equal": equal})
    check(equal, "de-hinted canvas")


def compare_canvas(
    phone: np.ndarray,
    photo: np.ndarray,
    layout: FrameLayout,
    frame: FrameSpec,
    stamp: StampSpec,
    taken: Any,
    file_time: Any,
    logo_images: dict[str, Image.Image] | None,
    dehinted_phone: np.ndarray | None = None,
) -> dict[str, Any]:
    """Exact outside glyph boxes; frozen Gate B on the ink inside them."""
    _mark, paint_reference = _host()
    py = paint_reference(photo, layout, frame, stamp, taken, file_time, logo_images, glyphs=True)
    if phone.shape != py.shape or phone.dtype != py.dtype:
        same_pixels(phone, py, "inked canvas shape")
    if np.array_equal(phone, py):
        return {
            "outside": 0,
            "glyphs": 0,
            "max_horiz": 0,
            "max_vert": 0,
            "worst_ratio": None,
            "blur_smear": 0,
            "backdrop_rows": [],
            "dehinted_equal": None,
        }
    boxes = glyph_boxes(layout, frame, stamp, taken, file_time, photo.shape[1], photo.shape[0])
    check(all(font in _GATE_FONT for _box, font, _px, _glow, _text in boxes), "glyph font")
    height, width = py.shape[:2]
    allow = np.zeros((height, width), dtype=bool)
    windows: list[tuple[tuple[int, int, int, int], str, int, str]] = []
    for box, font, px, glow, text in boxes:
        window = _clip(_window(box, font, glow), height, width)
        if window is None:
            continue
        left, top, right, bottom = window
        allow[top:bottom, left:right] = True
        windows.append((window, font, px, text))
    differ = np.any(phone != py, axis=-1)
    backdrop = _blur_backdrop(layout, height, width)
    backdrop_diff = differ & backdrop & ~allow
    other = differ & ~allow & ~backdrop
    outside = int(np.count_nonzero(other))
    rows = _row_counts(backdrop_diff)
    smear = int(np.count_nonzero(backdrop_diff))
    note("ui_m4_frame_outside", {"outside": outside, "glyphs": len(windows), "blur_smear": smear, "backdrop_rows": len(rows)})
    if outside:
        shown_phone = phone.copy()
        shown_py = py.copy()
        shown_phone[allow | backdrop] = 0
        shown_py[allow | backdrop] = 0
        same_pixels(shown_phone, shown_py, "outside glyph boxes")
    dehinted_equal: bool | None = None
    if rows:
        check(_stamp_before_blur(layout, frame, stamp), "backdrop difference")
        _prove_dehinted(dehinted_phone, photo, layout, frame, stamp, taken, file_time, logo_images, rows)
        dehinted_equal = True
    blank = paint_reference(photo, layout, frame, stamp, taken, file_time, logo_images, glyphs=False)
    max_horiz = max_vert = 0
    worst: float | None = None
    for window, font, px, text in windows:
        ok, horiz, vert, ratio = _glyph_ok(phone, py, blank, window, font, px, text)
        max_horiz, max_vert = max(max_horiz, horiz), max(max_vert, vert)
        if ratio is not None and (worst is None or abs(ratio - 1) > abs(worst - 1)):
            worst = ratio
        if not ok:
            note("ui_m4_frame_glyph", {"horiz": horiz, "vert": vert, "ratio": ratio, "px": px, "font": font})
        check(ok, "gate B glyph")
    return {
        "outside": outside,
        "glyphs": len(windows),
        "max_horiz": max_horiz,
        "max_vert": max_vert,
        "worst_ratio": worst,
        "blur_smear": smear,
        "backdrop_rows": rows,
        "dehinted_equal": dehinted_equal,
    }
