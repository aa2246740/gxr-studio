"""Shared case builders and Pillow/Rust mask comparison helpers for ``ink-text`` parity."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import ImageFont

from gxr_studio.finish.fonts import ASSETS, FONT_FILES, LAYOUT_ENGINE, FontMetrics, PilFonts, open_face
from gxr_studio.finish.layout import frame_layout, stamp_layout
from gxr_studio.finish.spec import FRAME_STYLES, FrameSpec, StampSpec
from support.core_cli import error_of, run_json
from support.ink_text_fonts import dehint_ttf

DRAWABLE_CHARS: tuple[str, ...] = tuple(
    [chr(c) for c in range(0x20, 0x7F)] + ["·", "Á", "É", "Ü"],
)

LARGE_PX_CHARS: tuple[str, ...] = tuple("0123456789:-· GXRFAf/.Á")

# Frozen Gate B bounds (M4-PLAN-NOTES.md, Manager decision on ink-text items 1-3, 2026-11-16).
GATE_B_HORIZ_EDGE_BOUND = 1
GATE_B_VERT_EDGE_BOUNDS: dict[str, int] = {
    "atkinson": 2,
    "inter": 2,
    "inter_sb": 2,
    "courier": 2,
    "patrick": 3,
    "dseg": 4,
}
GATE_B_COVERAGE_RATIO_GE_16: dict[str, tuple[float, float]] = {
    "atkinson": (0.83, 1.15),
    "inter": (0.83, 1.15),
    "inter_sb": (0.83, 1.15),
    "courier": (0.83, 1.15),
    "patrick": (0.52, 2.10),
    "dseg": (0.77, 1.37),
}
GATE_B_SMALL_PX_MIN_SUM = 512
GATE_B_SMALL_PX_COVERAGE_RATIO = (0.45, 1.80)


def font_path(name: str) -> Path:
    return ASSETS / "fonts" / FONT_FILES[name]


def dehinted_font_paths(dest_dir: Path, *, zero_maxp: bool = False) -> dict[str, Path]:
    out: dict[str, Path] = {}
    suffix = "nomaxp" if zero_maxp else "dehint"
    for name in FONT_FILES:
        src = font_path(name)
        dst = dest_dir / f"{name}.{suffix}.ttf"
        if not dst.exists():
            dst.write_bytes(dehint_ttf(src.read_bytes(), zero_maxp=zero_maxp))
        out[name] = dst
    return out


def open_pil_face(path: Path, px: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), max(1, px), layout_engine=LAYOUT_ENGINE)


@contextmanager
def patched_pil_fonts(font_map: dict[str, Path]) -> Iterator[None]:
    """Temporarily replace ``PilFonts._load`` while preserving its raw descriptor."""
    saved = vars(PilFonts)["_load"]

    @staticmethod
    def _custom_load(font: str, px: int, cjk: bool) -> Any:
        key = "cjk" if cjk else font
        return open_face(str(font_map[key]), px)

    PilFonts._load = _custom_load  # type: ignore[method-assign]
    try:
        yield
    finally:
        setattr(PilFonts, "_load", saved)


def pillow_mask(face: ImageFont.FreeTypeFont, ch: str) -> tuple[int, int, int, int, bytes]:
    mask_im, (x_off, y_off) = face.getmask2(ch, "L", anchor="la")
    w, h = mask_im.size
    return int(w), int(h), int(x_off), int(y_off), bytes(mask_im)


def run_rust_batch(
    font: Path,
    pxs: list[int],
    chars: list[str] | tuple[str, ...],
    work_dir: Path,
    tag: str = "batch",
) -> list[tuple[int, int, int, int, int, int, bytes]]:
    cases_path = work_dir / f"{tag}.cases.json"
    bin_path = work_dir / f"{tag}.bin"
    cases_path.write_text(
        json.dumps({"pxs": pxs, "chars": list(chars)}),
        encoding="utf-8",
    )
    code, payload = run_json(
        [
            "inktext-batch",
            "--font",
            str(font),
            "--cases",
            str(cases_path),
            "--out-bin",
            str(bin_path),
        ],
    )
    err = error_of(code, payload)
    if err is not None:
        raise err
    blob = bin_path.read_bytes()
    out: list[tuple[int, int, int, int, int, int, bytes]] = []
    pos = 0
    for px, cp, w, h, x_off, y_off, length in payload["items"]:
        chunk = blob[pos : pos + length]
        assert len(chunk) == length
        pos += length
        out.append((int(px), int(cp), int(w), int(h), int(x_off), int(y_off), chunk))
    assert pos == len(blob)
    return out


def sample_layout_pxs() -> dict[str, set[int]]:
    """Sample ``px`` values produced by ``frame_layout`` & ``stamp_layout`` across 1x1..65535x65535."""
    metrics = FontMetrics()
    meta: dict[str, str] = {
        "model": "GR",
        "lens": "50mm f/2",
        "shutter": "1/250s",
        "iso": "ISO 200",
        "date": "2026-06-01",
    }
    dims = [
        (1, 1),
        (17, 13),
        (64, 64),
        (240, 160),
        (640, 480),
        (1600, 1067),
        (4288, 2848),
        (12000, 8000),
        (32768, 21845),
        (65535, 65535),
    ]
    px_by_font: dict[str, set[int]] = {name: set() for name in FONT_FILES}
    styles = [s for s in FRAME_STYLES if s != "none"]

    for w, h in dims:
        for style in styles:
            spec = FrameSpec(style=style, text="GXR 2026")
            try:
                layout = frame_layout(w, h, spec, meta, metrics)
            except ZeroDivisionError:
                continue
            if layout is None:
                continue
            for item in layout.items:
                if item.get("k") == "text":
                    px_by_font[str(item["font"])].add(int(item["px"]))
        for sfont in ("seg7", "typewriter", "sans"):
            for ssize in ("small", "medium", "large"):
                sspec = StampSpec(enabled=True, font=sfont, size=ssize, show_time=True, text="GXR")
                sl = stamp_layout(w, h, sspec, "'26 06 01 12:30 GXR", metrics)
                if sl is not None:
                    for op in sl.ops:
                        if op.get("k") == "text":
                            px_by_font[str(op["font"])].add(int(op["px"]))
    return px_by_font


@dataclass(frozen=True)
class DiffRecord:
    font: str
    px: int
    ch: str
    diff_pixels: int
    max_abs_delta: int
    d_left: int
    d_top: int
    d_right: int
    d_bottom: int
    rs_sum: int
    py_sum: int


def _place_in_common_box(
    w: int,
    h: int,
    x_off: int,
    y_off: int,
    buf: bytes,
    x0: int,
    y0: int,
    cw: int,
    ch: int,
) -> np.ndarray:
    arr = np.zeros((ch, cw), dtype=np.uint8)
    if w > 0 and h > 0:
        src = np.frombuffer(buf, dtype=np.uint8).reshape((h, w))
        dx = x_off - x0
        dy = y_off - y0
        arr[dy : dy + h, dx : dx + w] = src
    return arr


def _ink_bbox(arr: np.ndarray) -> tuple[int, int, int, int] | None:
    ys, xs = np.nonzero(arr)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def compare_masks_common_space(
    a: tuple[int, int, int, int, bytes],
    b: tuple[int, int, int, int, bytes],
) -> tuple[int, int, tuple[int, int, int, int] | None, tuple[int, int, int, int] | None, int, int]:
    """Compare two ``(w, h, x_off, y_off, bytes)`` masks in common coordinate space."""
    aw, ah, ax, ay, abuf = a
    bw, bh, bx, by, bbuf = b
    x0 = min(ax, bx)
    y0 = min(ay, by)
    x1 = max(ax + aw, bx + bw)
    y1 = max(ay + ah, by + bh)
    cw = max(1, x1 - x0)
    ch = max(1, y1 - y0)
    aa = _place_in_common_box(aw, ah, ax, ay, abuf, x0, y0, cw, ch)
    bb = _place_in_common_box(bw, bh, bx, by, bbuf, x0, y0, cw, ch)
    diff = np.abs(aa.astype(np.int16) - bb.astype(np.int16))
    diff_px = int(np.count_nonzero(diff))
    max_d = int(diff.max()) if diff_px > 0 else 0
    return diff_px, max_d, _ink_bbox(aa), _ink_bbox(bb), int(aa.sum()), int(bb.sum())


def make_inktext_cli_and_wasm_spec(
    *,
    npy_path: Path,
    mode: str,
    ops: list[dict[str, Any]] | str,
    fonts: list[tuple[str, str]],
    w: int | float | str,
    h: int | float | str,
    cli_w: int | None = None,
    cli_h: int | None = None,
    override_dims: bool = False,
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "npy": str(npy_path),
        "mode": mode,
        "ops": ops,
        "fonts": fonts,
        "w": w,
        "h": h,
        "overrideDims": override_dims,
    }
    if cli_w is not None:
        spec["cliW"] = cli_w
    if cli_h is not None:
        spec["cliH"] = cli_h
    return spec
