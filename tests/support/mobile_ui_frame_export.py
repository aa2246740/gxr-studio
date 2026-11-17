"""Desktop side of the frame export parity check.

The phone's ``export_ink`` flag says whether the file can match desktop ``export_finish`` byte for
byte. When it cannot (a hinted glyph was drawn), the dev server saved the inked canvas and the
8-bit glyph masks Rust blended. ``mobile_ui_frame_gate`` checks them: each mask against Pillow's
under ink-text's frozen Gate B, the canvas against a Python paint that blends those same masks,
and every pixel outside the glyph windows against the ordinary Python paint. The built-in mark is
the GXR word in Atkinson, which is what the phone draws; the reference paint does the same.

Nothing here prints a photo name, a capture time, or EXIF. Callers use the redacted helpers.
"""

from __future__ import annotations

import io
import json
from contextlib import contextmanager
from dataclasses import dataclass
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
from support.export_pipeline_cases import check, cli_ok, note, same_file_redacted, same_redacted
from support.mobile_ui_frame_gate import Scene, _stamp_on, compare_canvas, read_glyph_report

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


def read_canvas(path: Path) -> np.ndarray:
    """The dev hook's canvas: little-endian width and height, then packed RGB."""
    raw = path.read_bytes()
    check(len(raw) >= 8, "canvas header")
    width, height = int.from_bytes(raw[:4], "little"), int.from_bytes(raw[4:8], "little")
    check(width > 0 and height > 0 and len(raw) == 8 + width * height * 3, "canvas length")
    return np.frombuffer(raw[8:], dtype=np.uint8).reshape(height, width, 3).copy()


def masks_path(canvas: Path) -> Path:
    """The glyph report the dev hook posts next to ``<prefix>-<index>.bin``."""
    return canvas.with_name(f"{canvas.stem}-masks.bin")


@dataclass(frozen=True)
class Fonts:
    """The font files the phone was given: the shipped ones, and ink-text's de-hinted Gate A copies."""

    real: dict[str, Path]
    dehint: dict[str, Path]


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


def _photo(render: Any, lib: Any, shot: dict[str, Any], edit: Any, quality: int) -> np.ndarray:
    rgb, _from_raw, _labels = color_image(render, shot, edit, max_edge=None, quality=quality, lib=lib, base="auto", full=True)
    return np.ascontiguousarray(rgb)


def paint_reference(
    photo: np.ndarray,
    layout: FrameLayout,
    frame: FrameSpec,
    stamp: StampSpec,
    taken: Any,
    file_time: Any,
    logo_images: dict[str, Image.Image] | None,
) -> np.ndarray:
    """Python's inked canvas: the desktop's ``draw_frame`` and ``apply_stamp`` with the GXR mark."""
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

    return draw(photo.copy())


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


def same_stage_peak(step: dict[str, Any]) -> int:
    """Peak of WASM+JS measured together in one stage, not the sum of the two separate peaks."""
    facts = step["facts"]
    marks = facts.get("marks")
    if isinstance(marks, dict):
        stages = marks.get("stages")
        if isinstance(stages, dict) and stages:
            totals = [int(stage.get("total") or 0) for stage in stages.values() if isinstance(stage, dict)]
            if totals:
                return max(totals)
        if "total" in marks:
            return int(marks["total"])
    mem = facts["mem"]
    if "total" in mem:
        return int(mem["total"])
    return int(mem.get("wasm") or 0) + int(mem.get("js") or 0)


def fit_stage_cap(peak: int, requested: int) -> int:
    """Lower the staging estimate until peak + cap fits in the 1.2 GiB page budget."""
    room = PAGE_BUDGET - max(0, peak)
    if room >= requested:
        return requested
    return max(0, room)


def memory_ok(step: dict[str, Any], *, hdr: bool) -> dict[str, Any]:
    """WASM ceiling, and the same-stage peak plus the staging estimate, against 1.2 GiB."""
    mem = step["facts"]["mem"]
    wasm = int(mem["wasm"])
    staged = int(step["facts"]["stagedHigh"])
    cap = int(step["facts"]["cap"])
    peak = same_stage_peak(step)
    ceiling = HDR_CEILING if hdr else SDR_CEILING
    default = STAGE_CAP_HDR if hdr else STAGE_CAP_SDR
    fitted = fit_stage_cap(peak, default)
    row = {"wasm": wasm, "js": int(mem.get("js") or 0), "staged": staged, "cap": cap, "peak": peak, "workers": mem.get("workers")}
    note("ui_m4_frame_memory", {"hdr": hdr, **row})
    check(0 < wasm <= ceiling, "hdr wasm ceiling" if hdr else "sdr wasm ceiling")
    check(staged <= cap, "hdr staged bytes" if hdr else "sdr staged bytes")
    check(cap == fitted, "hdr staging cap" if hdr else "sdr staging cap")
    check(peak + cap <= PAGE_BUDGET, "hdr page budget" if hdr else "sdr page budget")
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
    fonts: Fonts,
    dehinted_canvas: Path | None = None,
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
    scene = Scene(photo, layout, frame, stamp, taken, file_time, logo_images)
    report = read_glyph_report(masks_path(canvas_path))
    check(report.exact is False and fact.get("exact") is False, "glyph report for an inexact export")
    dehinted = read_canvas(dehinted_canvas) if dehinted_canvas is not None and dehinted_canvas.is_file() else None
    dehinted_report = read_glyph_report(masks_path(dehinted_canvas)) if dehinted is not None and dehinted_canvas is not None else None
    phone = read_canvas(canvas_path)
    return compare_canvas(phone, scene, report, fonts.real, fonts.dehint, dehinted_phone=dehinted, dehinted_report=dehinted_report)


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
    fonts: Fonts,
    dehinted_canvas: Path | None = None,
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
        gated = _gate(desk, work, shot, fact, _posted_canvas(canvas_path), logo_images, quality, fonts, dehinted_canvas)
        backdrop_rows = gated.pop("backdrop_rows", [])
        row.update(gated)
        row["backdrop_rows"] = backdrop_rows
    noted = dict(row)
    if isinstance(noted.get("backdrop_rows"), list):
        noted["backdrop_rows"] = len(noted["backdrop_rows"])
    note("ui_m4_frame_pair", noted)
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
