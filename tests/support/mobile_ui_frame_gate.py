"""Canvas gates for a framed phone export that ``export_ink`` did not mark exact.

The phone's dev hook hands over two things: the inked canvas, and for every glyph ``export_ink``
drew, the 8-bit coverage mask the Rust rasterizer blended (same font pack, same rasterizer).
Three gates use them:

- **Mask gate.** Each Rust mask against the Pillow mask of the same font file, ``px`` and
  character (``compare_masks_common_space``): ink-text's frozen Gate B bounds for the shipped
  hinted fonts, with the exact de-hinted attribution for every glyph that differs; exact for the
  de-hinted export. Nothing is derived from the finished photo's RGB.
- **Composite gate.** Python paints the same layout again, but every glyph Pillow would rasterize
  is replaced by the phone's Rust mask and blended by Pillow's own ``draw_bitmap`` (the
  ``fill_mask_L`` blend ink-text's Gate A ports). Every pixel inside a glyph window, and in fact
  the whole canvas, must then equal the phone's exactly: colour, glow and compositing included.
- **Outside gate.** Outside the glyph windows the phone equals the ordinary Python paint. A blur
  backdrop may differ only when the stamp is drawn before the blur, and then the same export with
  de-hinted fonts must equal Python on every pixel (manager ruling on P2-01).

Nothing here prints a photo name, a capture time, or EXIF.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from gxr_studio.finish.fonts import FONT_FILES, FontMetrics, PilFonts, open_face
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
    open_pil_face,
    pillow_mask,
    run_rust_batch,
)

Mask = tuple[int, int, int, int, bytes]
GlyphKey = tuple[str, int, str]
_GlyphBox = tuple[tuple[int, int, int, int], str, int, float | None, str]
_ATTR_EXACT: dict[tuple[str, str, int, str], bool] = {}


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


# --- the phone's glyph report -------------------------------------------------------------


@dataclass(frozen=True)
class GlyphReport:
    """What the dev hook posted next to the canvas (``<prefix>-<index>-masks.bin``)."""

    exact: bool
    runs: list[dict[str, Any]]
    masks: dict[GlyphKey, Mask]
    clipped: list[GlyphKey]
    fonts: dict[str, str]


def read_glyph_report(path: Path | None) -> GlyphReport:
    """``u32`` LE header length, the JSON header, then the mask bytes it indexes."""
    check(path is not None and path.is_file(), "glyph masks posted")
    raw = path.read_bytes() if path is not None and path.is_file() else b"\2\0\0\0{}"
    size = int.from_bytes(raw[:4], "little")
    check(4 + size <= len(raw), "glyph mask header")
    head = json.loads(raw[4 : 4 + size].decode("utf-8"))
    body = raw[4 + size :]
    masks: dict[GlyphKey, Mask] = {}
    clipped: list[GlyphKey] = []
    end = 0
    for glyph in head.get("glyphs", []):
        key = (str(glyph["font"]), int(glyph["px"]), str(glyph["ch"]))
        start, length = int(glyph["offset"]), int(glyph["length"])
        width, height = int(glyph["w"]), int(glyph["h"])
        check(length == width * height and start == end, "glyph mask length")
        masks[key] = (width, height, int(glyph["x_off"]), int(glyph["y_off"]), body[start : start + length])
        end = start + length
        if glyph.get("clipped"):
            clipped.append(key)
    check(end == len(body), "glyph mask body")
    return GlyphReport(bool(head.get("exact")), list(head.get("runs", [])), masks, clipped, dict(head.get("fonts", {})))


def fonts_match(report: GlyphReport, fonts: dict[str, Path]) -> None:
    """The phone's font pack is byte for byte the files the Pillow masks come from."""
    want = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in fonts.items()}
    check(bool(report.fonts) and all(want.get(name) == digest for name, digest in report.fonts.items()), "phone font files")
    check({font for font, _px, _ch in report.masks} <= set(report.fonts), "glyph fonts are in the phone's pack")


# --- mask gate -----------------------------------------------------------------------------


@dataclass(frozen=True)
class MaskResult:
    ok: bool
    diff_px: int
    horiz: int
    vert: int
    ratio: float | None


def _ratio_ok(font: str, px: int, rs_sum: int, py_sum: int) -> bool:
    if py_sum <= 0:
        return rs_sum == 0
    ratio = rs_sum / py_sum
    if px >= 16:
        lo, hi = GATE_B_COVERAGE_RATIO_GE_16[font]
        return lo <= ratio <= hi
    if min(rs_sum, py_sum) < GATE_B_SMALL_PX_MIN_SUM:
        return True
    lo, hi = GATE_B_SMALL_PX_COVERAGE_RATIO
    return lo <= ratio <= hi


def mask_gate(rust: Mask, pillow: Mask, font: str, px: int) -> MaskResult:
    """Ink-text's frozen Gate B on two 8-bit masks: ink presence, bbox edges, coverage-sum ratio."""
    diff_px, _max_d, rs_box, py_box, rs_sum, py_sum = compare_masks_common_space(rust, pillow)
    ratio = None if py_sum == 0 else rs_sum / py_sum
    if diff_px == 0:
        return MaskResult(True, 0, 0, 0, ratio)
    if (rs_box is None) != (py_box is None):
        return MaskResult(False, diff_px, 0, 0, ratio)
    horiz = vert = 0
    if rs_box is not None and py_box is not None:
        horiz = max(abs(rs_box[0] - py_box[0]), abs(rs_box[2] - py_box[2]))
        vert = max(abs(rs_box[1] - py_box[1]), abs(rs_box[3] - py_box[3]))
    ok = horiz <= GATE_B_HORIZ_EDGE_BOUND and vert <= GATE_B_VERT_EDGE_BOUNDS[font] and _ratio_ok(font, px, rs_sum, py_sum)
    return MaskResult(ok, diff_px, horiz, vert, ratio)


def glyph_hint_attributed(font: str, px: int, ch: str, dehint: dict[str, Path]) -> bool:
    """Gate A for one glyph: de-hinted Rust and de-hinted Pillow masks are identical."""
    path = dehint[font]
    key = (str(path), font, px, ch)
    if key not in _ATTR_EXACT:
        item = run_rust_batch(path, [px], [ch], path.parent, tag=f"attr-{font}-{px}-{ord(ch)}")[0]
        _ATTR_EXACT[key] = (item[2], item[3], item[4], item[5], item[6]) == pillow_mask(open_pil_face(path, px), ch)
    return _ATTR_EXACT[key]


def masks_ok(report: GlyphReport, fonts: dict[str, Path], dehint: dict[str, Path] | None) -> dict[str, Any]:
    """Every reported glyph against Pillow on the same font file.

    ``dehint`` given: the hinted run, frozen Gate B plus the exact de-hinted attribution of every
    glyph that differs. ``dehint`` None: ``fonts`` are the de-hinted files and every mask is exact.
    """
    fonts_match(report, fonts)
    check(not report.clipped, "glyph mask clipped")
    check(bool(report.masks), "glyph masks present")
    stats: dict[str, Any] = {"glyphs": len(report.masks), "mask_exact": 0, "max_horiz": 0, "max_vert": 0, "worst_ratio": None}
    for (font, px, ch), rust in sorted(report.masks.items()):
        check(font in FONT_FILES, "glyph font")
        result = mask_gate(rust, pillow_mask(open_pil_face(fonts[font], px), ch), font, px)
        if result.diff_px == 0:
            stats["mask_exact"] += 1
        else:
            stats["max_horiz"] = max(stats["max_horiz"], result.horiz)
            stats["max_vert"] = max(stats["max_vert"], result.vert)
            worst = stats["worst_ratio"]
            if result.ratio is not None and (worst is None or abs(result.ratio - 1) > abs(worst - 1)):
                stats["worst_ratio"] = result.ratio
        attributed = result.diff_px == 0 or (dehint is not None and glyph_hint_attributed(font, px, ch, dehint))
        if not (result.ok and attributed):
            row = {
                "font": font,
                "px": px,
                "diff": result.diff_px,
                "horiz": result.horiz,
                "vert": result.vert,
                "ratio": result.ratio,
                "attributed": attributed,
            }
            note("ui_m4_frame_glyph", row)
        check(result.ok, "gate B glyph mask")
        check(attributed, "de-hinted glyph is exact" if dehint is not None else "de-hinted glyph mask is exact")
    return stats


# --- composite gate ------------------------------------------------------------------------


class MissingGlyph(KeyError):
    """Python drew a glyph the phone did not report. (Not an AttributeError: ``draw.text``
    swallows those and falls back to ``getmask``.)"""


class _RustFace:
    """A Pillow face whose glyph rasterizer is replaced by the phone's masks. Metrics stay Pillow's."""

    def __init__(self, real: Any, font: str, px: int, masks: dict[GlyphKey, Mask], used: set[GlyphKey]) -> None:
        self._real = real
        self._font = font
        self._px = px
        self._masks = masks
        self._used = used

    def getmask2(self, text: str, mode: str = "", *_args: Any, **kwargs: Any) -> tuple[Any, tuple[int, int]]:
        whole = kwargs.get("start") in (None, (0.0, 0.0))
        check(mode == "L" and kwargs.get("anchor") in (None, "la") and not kwargs.get("stroke_width") and whole, "glyph draw mode")
        key = (self._font, self._px, text)
        try:
            width, height, x_off, y_off, buf = self._masks[key]
        except KeyError:
            raise MissingGlyph(key) from None
        self._used.add(key)
        image = Image.frombytes("L", (width, height), buf) if width and height else Image.new("L", (0, 0))
        return image.im, (x_off, y_off)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


@contextmanager
def rust_glyphs(masks: dict[GlyphKey, Mask], fonts: dict[str, Path]) -> Iterator[set[GlyphKey]]:
    """Inside, every ``PilFonts`` face draws the given masks. Yields the keys actually drawn."""
    saved = vars(PilFonts)["_load"]
    used: set[GlyphKey] = set()

    def _load(font: str, px: int, cjk: bool) -> Any:
        check(not cjk, "no CJK glyphs")
        return _RustFace(open_face(str(fonts[font]), px), font, px, masks, used)

    PilFonts._load = staticmethod(_load)  # type: ignore[method-assign,assignment]
    try:
        yield used
    finally:
        setattr(PilFonts, "_load", saved)


def composite_ok(phone: np.ndarray, rust_paint: np.ndarray, windows: np.ndarray) -> dict[str, int]:
    """Inside every glyph window the phone pixel is exactly blend(background, fill, Rust mask);
    the same paint must also match on the rest of the canvas."""
    if phone.shape != rust_paint.shape or phone.dtype != rust_paint.dtype:
        same_pixels(phone, rust_paint, "composite canvas shape")
    differ = np.any(phone != rust_paint, axis=-1)
    inside = int(np.count_nonzero(differ & windows))
    whole = int(np.count_nonzero(differ))
    note("ui_m4_frame_composite", {"inside": inside, "whole": whole})
    check(inside == 0, "composite inside glyph windows")
    check(whole == 0, "composite whole canvas")
    return {"composite_inside": inside, "composite_whole": whole}


# --- glyph windows -------------------------------------------------------------------------


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


def _push(boxes: list[_GlyphBox], box: tuple[int, int, int, int] | None, font: str, px: int, glow: float | None, text: str) -> None:
    if box is None or not text:
        return
    boxes.append((box, font, px, glow, text))


def _text_boxes(fonts: PilFonts, item: dict[str, Any], ox: int, oy: int, glow: float | None, boxes: list[_GlyphBox]) -> None:
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
    pad = math.ceil(3 * glow) + 2 if glow else 0
    hx = max(GATE_B_HORIZ_EDGE_BOUND, pad)
    vy = max(GATE_B_VERT_EDGE_BOUNDS[font], pad)
    left, top, right, bottom = box
    return left - hx, top - vy, right + hx, bottom + vy


def _clip(box: tuple[int, int, int, int], height: int, width: int) -> tuple[int, int, int, int] | None:
    left, top, right, bottom = box
    left, top = max(0, left), max(0, top)
    right, bottom = min(width, right), min(height, bottom)
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def glyph_windows(boxes: list[_GlyphBox], height: int, width: int) -> np.ndarray:
    allow = np.zeros((height, width), dtype=bool)
    for box, font, _px, glow, _text in boxes:
        check(font in FONT_FILES, "glyph font")
        window = _clip(_window(box, font, glow), height, width)
        if window is not None:
            left, top, right, bottom = window
            allow[top:bottom, left:right] = True
    return allow


# --- blur backdrop (P2-01 ruling) ------------------------------------------------------------


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
    """Pixels of a ``blur_fill`` backdrop not covered by the photo or another item."""
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
    return [{"y": int(y), "pixels": int(np.count_nonzero(mask[int(y)]))} for y in np.nonzero(mask.any(axis=1))[0]]


@dataclass(frozen=True)
class Scene:
    """One export as Python paints it."""

    photo: np.ndarray
    layout: FrameLayout
    frame: FrameSpec
    stamp: StampSpec
    taken: Any
    file_time: Any
    logo_images: dict[str, Image.Image] | None

    def paint(self) -> np.ndarray:
        _mark, paint_reference = _host()
        out = paint_reference(self.photo, self.layout, self.frame, self.stamp, self.taken, self.file_time, self.logo_images)
        return np.asarray(out)

    def paint_fonts(self, fonts: dict[str, Path]) -> np.ndarray:
        """The same paint with Pillow on other font files (the de-hinted Gate A fonts)."""
        from support.ink_text_cases import patched_pil_fonts

        with patched_pil_fonts(fonts):
            return self.paint()

    def paint_masks(self, report: GlyphReport, fonts: dict[str, Path]) -> np.ndarray:
        """The same paint with every glyph replaced by the phone's mask; it must draw exactly those."""
        with rust_glyphs(report.masks, fonts) as used:
            out = self.paint()
        check(used == set(report.masks), "phone reported exactly the glyphs Python draws")
        return out


def _prove_dehinted(
    scene: Scene, phone: np.ndarray | None, report: GlyphReport | None, dehint: dict[str, Path], rows: list[dict[str, int]]
) -> dict[str, Any]:
    """The de-hinted export: exact masks, and the whole canvas, backdrop included, equals Python."""
    check(phone is not None and report is not None, "de-hinted export")
    if phone is None or report is None:
        return {}
    stats = masks_ok(report, dehint, None)
    painted = scene.paint_fonts(dehint)
    equal = phone.shape == painted.shape and bool(np.array_equal(phone, painted))
    pixels = sum(row["pixels"] for row in rows)
    note(
        "ui_m4_frame_dehint", {"pixels": pixels, "rows": len(rows), "equal": equal, "masks": stats["glyphs"], "mask_exact": stats["mask_exact"]}
    )
    check(equal, "de-hinted canvas")
    return {"dehinted_equal": equal, "dehinted_masks": stats["glyphs"], "dehinted_mask_exact": stats["mask_exact"]}


def compare_canvas(
    phone: np.ndarray,
    scene: Scene,
    report: GlyphReport,
    fonts: dict[str, Path],
    dehint: dict[str, Path],
    dehinted_phone: np.ndarray | None = None,
    dehinted_report: GlyphReport | None = None,
) -> dict[str, Any]:
    """The mask, composite and outside gates for one inked canvas."""
    py = scene.paint()
    if phone.shape != py.shape or phone.dtype != py.dtype:
        same_pixels(phone, py, "inked canvas shape")
    height, width = py.shape[:2]
    stats = masks_ok(report, fonts, dehint)
    boxes = glyph_boxes(scene.layout, scene.frame, scene.stamp, scene.taken, scene.file_time, scene.photo.shape[1], scene.photo.shape[0])
    allow = glyph_windows(boxes, height, width)
    stats.update(composite_ok(phone, scene.paint_masks(report, fonts), allow))
    differ = np.any(phone != py, axis=-1)
    backdrop = _blur_backdrop(scene.layout, height, width)
    rows = _row_counts(differ & backdrop & ~allow)
    outside = int(np.count_nonzero(differ & ~allow & ~backdrop))
    pixels = sum(r["pixels"] for r in rows)
    note("ui_m4_frame_outside", {"outside": outside, "windows": len(boxes), "backdrop_pixels": pixels, "backdrop_rows": len(rows)})
    if outside:
        shown_phone, shown_py = phone.copy(), py.copy()
        shown_phone[allow | backdrop] = 0
        shown_py[allow | backdrop] = 0
        same_pixels(shown_phone, shown_py, "outside glyph boxes")
    stats.update({"outside": outside, "windows": len(boxes), "backdrop_rows": rows, "backdrop_pixels": pixels, "dehinted_equal": None})
    if rows:
        check(_stamp_before_blur(scene.layout, scene.frame, scene.stamp), "backdrop difference")
        stats.update(_prove_dehinted(scene, dehinted_phone, dehinted_report, dehint, rows))
    return stats
