"""Desktop side of the frame export parity check.

The phone's ``export_ink`` flag says whether the file can match desktop ``export_finish`` byte for
byte. When it cannot (a hinted glyph was drawn), the dev server saved the inked canvas. That canvas
is compared with a Python paint of the same layout: pixels outside glyph boxes match, and the ink
inside each box stays inside ink-text's frozen Gate B bounds. The built-in mark is the GXR word in
Atkinson, which is what the phone draws; the reference paint does the same.

Nothing here prints a photo name, a capture time, or EXIF. Callers use the redacted helpers.
"""

from __future__ import annotations

import io
import json
import math
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import numpy as np
from PIL import Image, ImageDraw

from gxr_studio.finish import fonts as fonts_mod
from gxr_studio.finish import frame as frame_mod
from gxr_studio.finish import stamp as stamp_mod
from gxr_studio.finish.fonts import ASSETS, FONT_FILES, FontMetrics, PilFonts
from gxr_studio.finish.layout import FrameLayout, glyph_xs, stamp_layout
from gxr_studio.finish.logo_store import prepare_uploaded_logo
from gxr_studio.finish.serve import color_image
from gxr_studio.finish.spec import FrameSpec, StampSpec
from gxr_studio.finish.stamp import format_stamp
from gxr_studio.privacy import PrivateDir
from gxr_studio.render.params import parse_edit
from support.core_cli import run_json
from support.export_pipeline_cases import check, cli_ok, note, same_file_redacted, same_pixels, same_redacted
from support.ink_text_cases import (
    GATE_B_COVERAGE_RATIO_GE_16,
    GATE_B_HORIZ_EDGE_BOUND,
    GATE_B_SMALL_PX_COVERAGE_RATIO,
    GATE_B_SMALL_PX_MIN_SUM,
    GATE_B_VERT_EDGE_BOUNDS,
)

GXR_MARK = {"w": 1951, "h": 1240, "ink_top": 270, "ink_bottom": 961}
MIB = 1024 * 1024
SDR_CEILING = 512 * MIB
HDR_CEILING = 768 * MIB
PAGE_BUDGET = int(1.2 * 1024 * MIB)
STAGE_CAP_SDR = 384 * MIB
STAGE_CAP_HDR = 256 * MIB
RESULT_KEYS = (
    "file_name",
    "width",
    "height",
    "format",
    "render",
    "render_label",
    "render_kind",
    "calibrated",
    "detail",
    "hdr",
    "hdr_reason",
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
_DRAW_RUNS = (fonts_mod, frame_mod, stamp_mod)


def read_canvas(path: Path) -> np.ndarray:
    """The dev hook's canvas: little-endian width and height, then packed RGB."""
    raw = path.read_bytes()
    check(len(raw) >= 8, "canvas header")
    width, height = int.from_bytes(raw[:4], "little"), int.from_bytes(raw[4:8], "little")
    check(width > 0 and height > 0 and len(raw) == 8 + width * height * 3, "canvas length")
    return np.frombuffer(raw[8:], dtype=np.uint8).reshape(height, width, 3).copy()


def plant_logo(store: Any, logo_id: str, png_path: Path) -> tuple[Image.Image, dict[str, Any]]:
    """Prepare the synthetic logo the way the phone does, and store it under the phone's id."""
    png, meta = prepare_uploaded_logo(png_path.read_bytes(), key_mode="auto", tol=30)
    entry = {
        "id": logo_id,
        "name": "studio_logo",
        "w": meta["w"],
        "h": meta["h"],
        "keyed": meta["keyed"],
        "tol": meta["tol"],
        "created": "2026-11-17T00:00:00",
        "coverage": meta["coverage"],
        "opaque": meta["opaque"],
        "lum": meta["lum"],
    }
    payload = json.dumps({"version": 1, "logos": [entry]}, ensure_ascii=False).encode("utf-8") + b"\n"
    with PrivateDir(store.root, "logos", store.forbidden) as private:
        private.replace_file(f"{logo_id}.png", png)
        private.replace_file("index.json", payload)
    with Image.open(io.BytesIO(png)) as im:
        im.load()
        rgba = im.convert("RGBA")
    return rgba, meta


def _font_args() -> list[str]:
    args: list[str] = []
    for name, filename in FONT_FILES.items():
        args += ["--font", f"{name}={ASSETS / 'fonts' / filename}"]
    return args


def _layout_from_json(raw: dict[str, Any]) -> FrameLayout:
    bg = raw["bg"]
    bgfx = raw.get("bgfx")
    return FrameLayout(
        width=int(raw["width"]),
        height=int(raw["height"]),
        photo=(int(raw["photo"][0]), int(raw["photo"][1])),
        bg=(int(bg[0]), int(bg[1]), int(bg[2])),
        items=tuple(raw["items"]),
        bgfx=None if bgfx is None else bgfx,
        unit_box=tuple(int(v) for v in raw.get("unit_box", (0, 0, 0, 0))),
        fixed=bool(raw.get("fixed", False)),
        grain=float(raw.get("grain") or 0.0),
    )


def rust_frame_layout(
    tmp: Path, photo_w: int, photo_h: int, spec: dict[str, Any], meta: dict[str, str], logos: list[dict[str, Any]]
) -> FrameLayout:
    """The phone's frame layout: Rust ``frame-layout`` with the GXR mark, not the desktop wordmark."""
    request: dict[str, Any] = {"w": photo_w, "h": photo_h, "meta": meta, "mark": GXR_MARK}
    if logos:
        request["logos"] = logos
    req_path = tmp / "frame-request.json"
    spec_path = tmp / "frame-spec.json"
    req_path.write_text(json.dumps(request), encoding="utf-8")
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    code, payload = run_json(["frame-layout", "--meta", str(req_path), "--spec", str(spec_path), *_font_args()])
    cli_ok(code, payload, "frame layout")
    check(isinstance(payload, dict), "frame layout object")
    return _layout_from_json(payload)


def _logo_fill(item: dict[str, Any], bg: tuple[int, int, int]) -> tuple[int, int, int]:
    raw = item.get("fill")
    if isinstance(raw, (list, tuple)) and len(raw) >= 3:
        return (int(raw[0]), int(raw[1]), int(raw[2]))
    luma = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
    return (244, 244, 246) if luma < 128 else (22, 22, 26)


def _paint_gxr(canvas: Image.Image, item: dict[str, Any], fonts: PilFonts, bg: tuple[int, int, int]) -> None:
    px = max(1, round(1000 * int(item["h"]) / GXR_MARK["h"]))
    metrics = FontMetrics(fonts)
    run = {
        "font": "atkinson",
        "px": px,
        "cjk": False,
        "text": "GXR",
        "y": int(item["y"]),
        "xs": glyph_xs(metrics, "atkinson", px, "GXR", float(item["x"])),
    }
    fonts_mod.draw_run(ImageDraw.Draw(canvas), run, fonts, _logo_fill(item, bg))


@contextmanager
def _logo_painter(bg: tuple[int, int, int]) -> Iterator[None]:
    original = frame_mod._paint_item

    def painted(
        canvas: Image.Image,
        base: Image.Image,
        image: Image.Image,
        item: dict[str, Any],
        fonts: PilFonts,
        logo_images: dict[str, Image.Image] | None,
    ) -> None:
        if item.get("k") == "logo":
            _paint_gxr(canvas, item, fonts, bg)
            return
        original(canvas, base, image, item, fonts, logo_images)

    frame_mod._paint_item = painted
    try:
        yield
    finally:
        frame_mod._paint_item = original


@contextmanager
def _silent_glyphs() -> Iterator[None]:
    saved = [module.draw_run for module in _DRAW_RUNS]

    def noop(*_args: object, **_kwargs: object) -> None:
        return None

    for module in _DRAW_RUNS:
        module.draw_run = noop
    try:
        yield
    finally:
        for module, fn in zip(_DRAW_RUNS, saved, strict=True):
            module.draw_run = fn


def _photo(render: Any, lib: Any, shot: dict[str, Any], edit: Any, quality: int) -> np.ndarray:
    rgb, _from_raw, _labels = color_image(render, shot, edit, max_edge=None, quality=quality, lib=lib, base="auto", full=True)
    return np.ascontiguousarray(rgb)


def _stamp_on(frame: FrameSpec, stamp: StampSpec) -> str | None:
    if not stamp.enabled:
        return None
    if stamp.placement == "border" and frame.style == "instant":
        return "canvas"
    return "photo"


def paint_reference(
    photo: np.ndarray,
    layout: FrameLayout,
    frame: FrameSpec,
    stamp: StampSpec,
    taken: Any,
    file_time: Any,
    logo_images: dict[str, Image.Image] | None,
    *,
    glyphs: bool,
) -> np.ndarray:
    """Python's inked canvas. ``glyphs`` false leaves text, the date stamp and the GXR mark unpainted."""
    where = _stamp_on(frame, stamp)

    def draw(image: np.ndarray) -> np.ndarray:
        if where == "photo":
            image = stamp_mod.apply_stamp(image, stamp, taken, file_time)
        with _logo_painter(layout.bg):
            framed = frame_mod.draw_frame(Image.fromarray(image, "RGB"), layout, PilFonts(), logo_images)
        out = np.asarray(framed)
        if where == "canvas":
            # w, h stay the photo size; the frame argument puts the stamp on the border in canvas coordinates.
            text = format_stamp(stamp, taken, file_time)
            laid = stamp_layout(image.shape[1], image.shape[0], stamp, text, FontMetrics(), layout)
            if laid is not None:
                out = stamp_mod.apply_stamp(out, stamp, taken, file_time, laid)
        return np.ascontiguousarray(out)

    if glyphs:
        return draw(photo.copy())
    with _silent_glyphs():
        return draw(photo.copy())


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
    boxes: list[tuple[tuple[int, int, int, int], str, int, float | None]],
    box: tuple[int, int, int, int] | None,
    font: str,
    px: int,
    glow: float | None,
) -> None:
    if box is None:
        return
    boxes.append((box, font, px, glow))


def _text_boxes(
    fonts: PilFonts,
    item: dict[str, Any],
    ox: int,
    oy: int,
    glow: float | None,
    boxes: list[tuple[tuple[int, int, int, int], str, int, float | None]],
) -> None:
    font = str(item["font"])
    px = int(item["px"])
    if item.get("rot"):
        left, top, right, bottom = _rotated_box(item, fonts)
        _push(boxes, (left + ox, top + oy, right + ox, bottom + oy), font, px, glow)
        return
    for ch, x in zip(str(item["text"]), item["xs"], strict=True):
        box = _char_box(fonts, ch, int(x) + ox, int(item["y"]) + oy, font, px)
        _push(boxes, box, font, px, glow)


def glyph_boxes(
    layout: FrameLayout,
    frame: FrameSpec,
    stamp: StampSpec,
    taken: Any,
    file_time: Any,
    photo_w: int,
    photo_h: int,
) -> list[tuple[tuple[int, int, int, int], str, int, float | None]]:
    """Every character box on the finished canvas, plus the glow radius of a date stamp."""
    fonts = PilFonts()
    boxes: list[tuple[tuple[int, int, int, int], str, int, float | None]] = []
    for item in layout.items:
        kind = item.get("k")
        if kind == "text":
            _text_boxes(fonts, item, 0, 0, None, boxes)
        elif kind == "logo":
            px = max(1, round(1000 * int(item["h"]) / GXR_MARK["h"]))
            metrics = FontMetrics(fonts)
            for ch, x in zip("GXR", glyph_xs(metrics, "atkinson", px, "GXR", float(item["x"])), strict=True):
                _push(boxes, _char_box(fonts, ch, int(x), int(item["y"]), "atkinson", px), "atkinson", px, None)
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


def _ink_bbox(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


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


def _glyph_ok(
    phone: np.ndarray, py: np.ndarray, blank: np.ndarray, window: tuple[int, int, int, int], font: str, px: int
) -> tuple[bool, int, int, float | None]:
    left, top, right, bottom = window
    py_ink = np.any(py[top:bottom, left:right] != blank[top:bottom, left:right], axis=2)
    rs_ink = np.any(phone[top:bottom, left:right] != blank[top:bottom, left:right], axis=2)
    py_box, rs_box = _ink_bbox(py_ink), _ink_bbox(rs_ink)
    if (py_box is None) != (rs_box is None):
        return False, 0, 0, None
    horiz = vert = 0
    if py_box is not None and rs_box is not None:
        horiz = max(abs(rs_box[0] - py_box[0]), abs(rs_box[2] - py_box[2]))
        vert = max(abs(rs_box[1] - py_box[1]), abs(rs_box[3] - py_box[3]))
    py_sum, rs_sum = int(py_ink.sum()), int(rs_ink.sum())
    ratio = None if py_sum == 0 else rs_sum / py_sum
    gate = _GATE_FONT[font]
    ok = horiz <= GATE_B_HORIZ_EDGE_BOUND and vert <= GATE_B_VERT_EDGE_BOUNDS[gate] and _ratio_ok(font, px, rs_sum, py_sum)
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


def compare_canvas(
    phone: np.ndarray,
    photo: np.ndarray,
    layout: FrameLayout,
    frame: FrameSpec,
    stamp: StampSpec,
    taken: Any,
    file_time: Any,
    logo_images: dict[str, Image.Image] | None,
) -> dict[str, Any]:
    """Exact outside glyph boxes; frozen Gate B on the ink inside them."""
    py = paint_reference(photo, layout, frame, stamp, taken, file_time, logo_images, glyphs=True)
    if phone.shape != py.shape or phone.dtype != py.dtype:
        same_pixels(phone, py, "inked canvas shape")
    if np.array_equal(phone, py):
        return {"outside": 0, "glyphs": 0, "max_horiz": 0, "max_vert": 0, "worst_ratio": None, "blur_smear": 0}
    boxes = glyph_boxes(layout, frame, stamp, taken, file_time, photo.shape[1], photo.shape[0])
    check(all(font in _GATE_FONT for _box, font, _px, _glow in boxes), "glyph font")
    height, width = py.shape[:2]
    allow = np.zeros((height, width), dtype=bool)
    windows: list[tuple[tuple[int, int, int, int], str, int]] = []
    for box, font, px, glow in boxes:
        window = _clip(_window(box, font, glow), height, width)
        if window is None:
            continue
        left, top, right, bottom = window
        allow[top:bottom, left:right] = True
        windows.append((window, font, px))
    differ = np.any(phone != py, axis=-1)
    backdrop = _blur_backdrop(layout, height, width)
    smear = int(np.count_nonzero(differ & backdrop))
    outside = int(np.count_nonzero(differ & ~allow & ~backdrop))
    note("ui_m4_frame_outside", {"outside": outside, "glyphs": len(windows), "blur_smear": smear})
    if outside:
        shown_phone = phone.copy()
        shown_py = py.copy()
        shown_phone[allow | backdrop] = 0
        shown_py[allow | backdrop] = 0
        same_pixels(shown_phone, shown_py, "outside glyph boxes")
    blank = paint_reference(photo, layout, frame, stamp, taken, file_time, logo_images, glyphs=False)
    max_horiz = max_vert = 0
    worst: float | None = None
    for window, font, px in windows:
        ok, horiz, vert, ratio = _glyph_ok(phone, py, blank, window, font, px)
        max_horiz, max_vert = max(max_horiz, horiz), max(max_vert, vert)
        if ratio is not None and (worst is None or abs(ratio - 1) > abs(worst - 1)):
            worst = ratio
        if not ok:
            note("ui_m4_frame_glyph", {"horiz": horiz, "vert": vert, "ratio": ratio, "px": px, "font": font})
        check(ok, "gate B glyph")
    return {"outside": outside, "glyphs": len(windows), "max_horiz": max_horiz, "max_vert": max_vert, "worst_ratio": worst, "blur_smear": smear}


def sidecar_ok(phone_side: dict[str, Any], desk_side: dict[str, Any]) -> None:
    same_redacted(parse_edit(phone_side).to_json(), parse_edit(desk_side).to_json(), "parse_edit")
    same_redacted(phone_side.get("border"), desk_side.get("border"), "border keys")
    same_redacted(phone_side.get("stamp"), desk_side.get("stamp"), "stamp keys")


def result_ok(phone: dict[str, Any], desk: Any) -> None:
    got = {key: phone.get(key) for key in RESULT_KEYS}
    want = {
        "file_name": desk.path.name,
        "width": desk.width,
        "height": desk.height,
        "format": desk.format,
        "render": desk.render,
        "render_label": desk.render_label,
        "render_kind": desk.render_kind,
        "calibrated": desk.calibrated,
        "detail": desk.detail,
        "hdr": desk.hdr,
        "hdr_reason": desk.hdr_reason,
    }
    same_redacted(got, want, "result JSON")


def memory_ok(step: dict[str, Any], *, hdr: bool) -> dict[str, Any]:
    """WASM linear-memory high water and the staging cap, against the ink-text ceilings."""
    mem = step["facts"]["mem"]
    wasm = int(mem["wasm"])
    staged = int(step["facts"]["stagedHigh"])
    cap = int(step["facts"]["cap"])
    ceiling = HDR_CEILING if hdr else SDR_CEILING
    cap_want = STAGE_CAP_HDR if hdr else STAGE_CAP_SDR
    row = {"wasm": wasm, "js": int(mem["js"]), "staged": staged, "cap": cap, "workers": mem.get("workers")}
    note("ui_m4_frame_memory", {"hdr": hdr, **row})
    check(0 < wasm <= ceiling, "hdr wasm ceiling" if hdr else "sdr wasm ceiling")
    check(staged <= cap and cap == cap_want, "hdr staging cap" if hdr else "sdr staging cap")
    check(wasm + cap <= PAGE_BUDGET, "hdr page budget" if hdr else "sdr page budget")
    return row


def file_ok(phone: bytes | None, desk_path: Path) -> None:
    same_file_redacted(phone, desk_path.read_bytes(), "phone file equals desktop export_finish")


def _changes(side: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(side.get("border"), dict):
        out["frame"] = side["border"]
    if isinstance(side.get("stamp"), dict):
        out["stamp"] = side["stamp"]
    return out


def _posted_canvas(path: Path | None) -> Path:
    check(path is not None and path.is_file(), "ink canvas posted")
    return path if path is not None else Path()


def _gate(
    desk: Any,
    work: Path,
    shot: dict[str, Any],
    fact: dict[str, Any],
    canvas_path: Path,
    logo_images: dict[str, Image.Image] | None,
    quality: int,
) -> dict[str, Any]:
    from gxr_studio.finish.meta_text import photo_meta

    edit = parse_edit(fact["side"])
    frame = edit.frame if isinstance(edit.frame, FrameSpec) else FrameSpec()
    stamp = edit.stamp if isinstance(edit.stamp, StampSpec) else StampSpec()
    check(frame.style != "none", "parsed frame")
    photo = _photo(desk.render, desk.lib, shot, edit, quality)
    meta, taken, file_time = photo_meta(desk.lib, shot)
    logos = [dict(item) for item in fact.get("layout", {}).get("logos") or []]
    layout = rust_frame_layout(work, int(photo.shape[1]), int(photo.shape[0]), frame.to_json(), meta, logos)
    return compare_canvas(read_canvas(canvas_path), photo, layout, frame, stamp, taken, file_time, logo_images)


def finish_one(
    desk: Any,
    work: Path,
    shot: dict[str, Any],
    fact: dict[str, Any],
    phone_path: Path | None,
    canvas_path: Path | None,
    logo_images: dict[str, Image.Image] | None,
    *,
    quality: int,
    long_edge: int | None,
    hdr: bool,
    role: str,
) -> dict[str, Any]:
    """One phone export against the desktop sidecar, result JSON, and either the file or the canvas."""
    from gxr_studio.finish.serve import export_finish

    phone_side = fact["side"]
    desk_side = desk.sidecar(shot, _changes(phone_side))
    sidecar_ok(phone_side, desk_side)
    same_redacted(phone_side.get("frame"), desk_side.get("frame"), "frame size")
    dest = work / "desk"
    dest.mkdir(parents=True, exist_ok=True)
    res = export_finish(
        desk.render,
        desk.lib,
        shot,
        parse_edit(desk_side),
        desk_side,
        dest,
        [],
        quality=quality,
        long_edge=long_edge,
        fmt="jpeg",
        base="auto",
        hdr=hdr,
    )
    result_ok(fact["result"], res)
    exact = fact.get("exact") is True
    row: dict[str, Any] = {
        "role": role,
        "exact": exact,
        "hdr": res.hdr,
        "hdr_reason": res.hdr_reason,
        "quality": quality,
        "long_edge": long_edge,
    }
    if exact:
        payload = phone_path.read_bytes() if phone_path is not None and phone_path.is_file() else None
        file_ok(payload, res.path)
        row["kind"] = "exact"
    else:
        row["kind"] = "gate"
        row.update(_gate(desk, work, shot, fact, _posted_canvas(canvas_path), logo_images, quality))
    note("ui_m4_frame_pair", row)
    return row


def size_line(render: Any, lib: Any, shot: dict[str, Any], side: dict[str, Any], tier: str) -> str:
    """The 05a line for one framed photo, from the desktop's ``output_sizes``."""
    from gxr_studio.finish.sizes import output_sizes

    sizes = output_sizes(render, lib, shot, parse_edit(side), None, None)
    width, height = sizes["sizes"][tier]
    ratio = sizes["ratio"]
    text = f"{int(width)} × {int(height)} 像素"
    if ratio:
        text += f" · {_ratio_text(int(ratio[0]), int(ratio[1]))}"
    return text


def _ratio_text(width: int, height: int) -> str:
    specials = {(191, 100): "1.91:1", (100, 191): "1:1.91", (47, 20): "2.35:1", (20, 47): "1:2.35", (8, 5): "16:10", (5, 8): "10:16"}
    return specials.get((width, height), f"{width}:{height}")


def logo_stats_ok(phone_logo: dict[str, Any], meta: dict[str, Any]) -> None:
    # JSON has no float/int distinction, so a coverage of 1 arrives as an int.
    got = {key: phone_logo.get(key) for key in ("w", "h", "coverage", "opaque", "lum")}
    got["coverage"] = float(got["coverage"])
    got["lum"] = float(got["lum"])
    same_redacted(got, {key: meta[key] for key in ("w", "h", "coverage", "opaque", "lum")}, "prepared logo")
