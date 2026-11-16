"""Gate A / B parity tests for ``gxr-finish::ink_text::{stamp, frame}`` (Items 6, 7 & 8).

Covers:
- ``apply_stamp`` + ``stamp_hdr_mask`` (glow, seg7 poly, typewriter, sans, border stamp, empty/null).
- ``draw_frame`` across every style in ``FRAME_STYLES`` (``grain``, ``blur_fill``, ``carrier``,
  ``hole`` zero-clone vs hole-present, rotated captions, ``clogo`` orig/mono/auto, ``logo`` mark).
- ``unknown_logo`` error parity and all 3 ``decode_logo_bundle`` formats.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from gxr_studio.finish.fonts import FONT_FILES, FontMetrics, PilFonts
from gxr_studio.finish.frame import draw_frame
from gxr_studio.finish.layout import frame_layout, stamp_layout
from gxr_studio.finish.logo_math import LogoInfo
from gxr_studio.finish.spec import FRAME_STYLES, FrameSpec, StampSpec
from gxr_studio.finish.stamp import apply_stamp, format_stamp
from support.core_cli import require_cli, run_json
from support.export_pipeline_cases import anon_ids, check, cli_ok, same_kind, same_pixels
from support.frame_cases import FIXTURE_CJK
from support.ink_text_cases import dehinted_font_paths, font_path, patched_pil_fonts


@pytest.fixture(autouse=True)
def _cli() -> None:
    require_cli()


def _font_args(font_map: dict[str, Path]) -> list[str]:
    out: list[str] = []
    for name, path in font_map.items():
        out += ["--font", f"{name}={path}"]
    return out


def _synth_photo(w: int = 180, h: int = 120, seed: int = 101) -> np.ndarray:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    grad = np.stack(
        [
            (xx * 255 // max(1, w - 1)),
            (yy * 255 // max(1, h - 1)),
            ((xx + yy) * 255 // max(1, w + h - 2)),
        ],
        axis=-1,
    ).astype(np.int16)
    noise = rng.integers(-25, 26, size=(h, w, 3), dtype=np.int16)
    return np.clip(grad + noise, 0, 255).astype(np.uint8)


def _synth_logo_rgba(w: int = 96, h: int = 72) -> np.ndarray:
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    arr[10 : h - 10, 12 : w - 12] = [210, 80, 30, 220]
    arr[18 : h - 18, 22 : w - 22] = [30, 40, 50, 255]
    return arr


STAMP_CASES: list[tuple[str, StampSpec, str | None]] = [
    ("seg7_glow", StampSpec(enabled=True, font="seg7", size="medium", placement="photo", show_time=True, glow=True), None),
    ("seg7_noglow", StampSpec(enabled=True, font="seg7", size="small", placement="photo", show_time=False, glow=False), None),
    (
        "typewriter",
        StampSpec(enabled=True, font="typewriter", size="large", placement="photo", glow=True, color="yellow", text="A12", content="date+text"),
        None,
    ),
    ("sans_date", StampSpec(enabled=True, font="sans", size="medium", placement="photo", date_format="yyyy.mm.dd", glow=False), None),
    ("border_instant", StampSpec(enabled=True, font="seg7", size="medium", placement="border", show_time=True, glow=True), "instant"),
]


@pytest.mark.parametrize("case_idx", range(len(STAMP_CASES)), ids=anon_ids(len(STAMP_CASES)))
def test_gate_a_apply_stamp_and_hdr_mask_exact(case_idx: int, tmp_path: Path) -> None:
    """Gate A: apply_stamp and stamp_hdr_mask match Python 0-diff with de-hinted fonts."""
    tag, sspec, frame_style = STAMP_CASES[case_idx]
    dehinted = dehinted_font_paths(tmp_path, zero_maxp=False)
    taken = datetime(2026, 11, 15, 14, 35)
    photo = _synth_photo(220, 150, seed=200 + case_idx)

    metrics = FontMetrics()
    text = format_stamp(sspec, taken, None)
    border = frame_layout(photo.shape[1], photo.shape[0], FrameSpec(style=frame_style), {}, metrics) if frame_style else None
    canvas = np.asarray(draw_frame(Image.fromarray(photo, "RGB"), border, PilFonts())) if border is not None else photo.copy()
    sl = stamp_layout(photo.shape[1], photo.shape[0], sspec, text, metrics, border)
    check(sl is not None, "stamp_layout unexpectedly None")
    if sl is None:
        return

    with patched_pil_fonts(dehinted):
        want = apply_stamp(canvas.copy(), sspec, taken, None, sl)
        black = np.zeros_like(canvas)
        white = np.full_like(canvas, 255)
        want_mask = (
            ((apply_stamp(black.copy(), sspec, taken, None, sl) != black).any(axis=2))
            | ((apply_stamp(white.copy(), sspec, taken, None, sl) != white).any(axis=2))
        ).astype(np.uint8)

    in_npy = tmp_path / f"{tag}-in.npy"
    out_npy = tmp_path / f"{tag}-out.npy"
    mask_npy = tmp_path / f"{tag}-mask.npy"
    layout_json = tmp_path / f"{tag}-layout.json"
    np.save(in_npy, canvas)
    layout_json.write_text(json.dumps(sl.to_json(), ensure_ascii=False), encoding="utf-8")

    code, payload = run_json(
        [
            "inktext-stamp",
            "--in",
            str(in_npy),
            "--layout",
            str(layout_json),
            *_font_args(dehinted),
            "--hdr-mask-out",
            str(mask_npy),
            "--out",
            str(out_npy),
        ]
    )
    cli_ok(code, payload, "inktext-stamp gate A")
    check(payload.get("exact") is True, "expected exact=True for dehinted stamp")
    same_pixels(np.load(out_npy), want, f"stamp-{tag}")
    same_pixels(np.load(mask_npy), want_mask, f"stamp-mask-{tag}")


def test_stamp_null_and_cjk_exact_flag(tmp_path: Path) -> None:
    """Null stamp layout preserves pixels with exact=True; CJK stamp sets exact=False and non-empty mask."""
    real_fonts = {name: font_path(name) for name in FONT_FILES}
    real_fonts["cjk"] = FIXTURE_CJK

    photo = _synth_photo(180, 120, seed=55)
    in_npy = tmp_path / "null-in.npy"
    out_npy = tmp_path / "null-out.npy"
    mask_npy = tmp_path / "null-mask.npy"
    np.save(in_npy, photo)

    code, payload = run_json(
        [
            "inktext-stamp",
            "--in",
            str(in_npy),
            "--layout-json",
            "null",
            "--hdr-mask-out",
            str(mask_npy),
            "--out",
            str(out_npy),
        ]
    )
    cli_ok(code, payload, "inktext-stamp null")
    check(payload.get("exact") is True, "null stamp should report exact=True")
    same_pixels(np.load(out_npy), photo, "null-stamp-noop")
    same_pixels(np.load(mask_npy), np.zeros(photo.shape[:2], dtype=np.uint8), "null-stamp-mask")

    cjk_layout = {
        "ink": [255, 160, 40],
        "glow": 2.0,
        "rect": [12, 80, 96, 28],
        "ops": [
            {
                "k": "text",
                "font": "inter",
                "px": 22,
                "cjk": True,
                "text": "上海",
                "xs": [16, 40],
                "y": 82,
            }
        ],
    }
    code2, payload2 = run_json(
        [
            "inktext-stamp",
            "--in",
            str(in_npy),
            "--layout-json",
            json.dumps(cjk_layout, ensure_ascii=False),
            *_font_args(real_fonts),
            "--hdr-mask-out",
            str(mask_npy),
            "--out",
            str(out_npy),
        ]
    )
    cli_ok(code2, payload2, "inktext-stamp cjk")
    check(payload2.get("exact") is False, "CJK stamp should report exact=False")
    mask = np.load(mask_npy)
    check(int(mask.sum()) > 100 and set(np.unique(mask).tolist()).issubset({0, 1}), "CJK stamp mask valid")


FRAME_CASES: list[tuple[str, FrameSpec, bool]] = [
    ("leica_nologo", FrameSpec(style="leica", show_logo=False, text="50MM"), False),
    ("leica_clogo_orig", FrameSpec(style="leica", show_logo=True, logo="custom1", logo_mode="orig", text="50MM"), True),
    ("leica_clogo_mono", FrameSpec(style="leica", show_logo=True, logo="custom1", logo_mode="mono", text="MONO"), True),
    ("leica_clogo_auto", FrameSpec(style="leica", show_logo=True, logo="custom1", logo_mode="auto", color="black"), True),
    ("blur_fill", FrameSpec(style="blur_fill", show_logo=False, text="BLUR"), False),
    ("gallery", FrameSpec(style="gallery", text="GALLERY"), False),
    ("hasselblad", FrameSpec(style="hasselblad", show_logo=False, text="HASSEL"), False),
    ("instant_polaroid", FrameSpec(style="instant", fmt="polaroid", text="POLAROID"), False),
    ("instant_instax_mini", FrameSpec(style="instant", fmt="instax_mini", text="MINI"), False),
    ("film_135_holes", FrameSpec(style="film", fmt="135"), False),
    ("film_120_holes", FrameSpec(style="film", fmt="120"), False),
    ("thin_keyline", FrameSpec(style="thin_keyline", show_logo=False, text="KEYLINE"), False),
    ("cinema", FrameSpec(style="cinema", show_logo=False, text="CINEMA"), False),
    ("exhibition_label", FrameSpec(style="exhibition_label", show_logo=False, text="EXHIBITION"), False),
    ("slide_mount", FrameSpec(style="slide_mount", show_logo=False, text="SLIDE"), False),
    ("darkroom", FrameSpec(style="darkroom", text="DARKROOM"), False),
    ("postage_stamp", FrameSpec(style="postage_stamp", show_logo=False, text="POSTAGE"), False),
]


def test_frame_styles_list_is_fully_covered() -> None:
    covered = {spec.style for _, spec, _ in FRAME_CASES} | {"none"}
    check(covered == FRAME_STYLES, "all FRAME_STYLES must be covered in FRAME_CASES")


@pytest.mark.parametrize("case_idx", range(len(FRAME_CASES)), ids=anon_ids(len(FRAME_CASES)))
def test_gate_a_draw_frame_all_styles_exact(case_idx: int, tmp_path: Path) -> None:
    """Gate A: draw_frame matches Python 0-diff across every FRAME_STYLE (including hole, grain, blur, clogo)."""
    tag, spec, use_clogo = FRAME_CASES[case_idx]
    dehinted = dehinted_font_paths(tmp_path, zero_maxp=False)
    photo = _synth_photo(180, 120, seed=300 + case_idx)
    meta = {
        "lens": "A12 50mm F2.5",
        "lens_short": "50mm",
        "aperture": "f/2.5",
        "shutter": "1/250s",
        "iso": "ISO 200",
        "date": "2026-11-15",
        "frame_no": "12",
    }

    logo_arr = _synth_logo_rgba()
    logo_info = LogoInfo("custom1", "Custom", 96, 72, 0.5, False, 110.0)
    logos_meta = {"custom1": logo_info} if use_clogo else None
    logo_images = {"custom1": Image.fromarray(logo_arr, "RGBA")} if use_clogo else None

    metrics = FontMetrics()
    fl = frame_layout(photo.shape[1], photo.shape[0], spec, meta, metrics, logos=logos_meta)
    check(fl is not None, "frame_layout returned None")
    if fl is None:
        return

    with patched_pil_fonts(dehinted):
        want = np.asarray(draw_frame(Image.fromarray(photo, "RGB"), fl, PilFonts(), logo_images=logo_images))

    in_npy = tmp_path / f"{tag}-photo.npy"
    out_npy = tmp_path / f"{tag}-frame.npy"
    layout_json = tmp_path / f"{tag}-layout.json"
    np.save(in_npy, photo)
    layout_json.write_text(json.dumps(fl.to_json(), ensure_ascii=False), encoding="utf-8")

    args = [
        "inktext-frame",
        "--in",
        str(in_npy),
        "--layout",
        str(layout_json),
        *_font_args(dehinted),
        "--out",
        str(out_npy),
    ]
    if use_clogo:
        logo_npy = tmp_path / f"{tag}-clogo.npy"
        np.save(logo_npy, logo_arr)
        args += ["--logo", f"custom1={logo_npy}"]

    code, payload = run_json(args)
    cli_ok(code, payload, "inktext-frame gate A")
    check(payload.get("exact") is True, "expected exact=True for dehinted frame")
    same_pixels(np.load(out_npy), want, f"frame-{tag}")


def test_frame_builtin_logo_mark_and_errors(tmp_path: Path) -> None:
    """Built-in 'logo' mark paints GXR wordmark with exact=False; missing clogo errors unknown_logo."""
    dehinted = dehinted_font_paths(tmp_path, zero_maxp=False)
    photo = _synth_photo(180, 120, seed=401)
    in_npy = tmp_path / "logo-photo.npy"
    np.save(in_npy, photo)

    for color, expected_ink in (("white", (22, 22, 26)), ("black", (244, 244, 246))):
        spec = FrameSpec(style="leica", show_logo=True, color=color, text="GXR")
        fl = frame_layout(photo.shape[1], photo.shape[0], spec, {"lens_short": "50mm"}, FontMetrics())
        check(fl is not None, "leica layout should exist")
        if fl is None:
            return
        layout_path = tmp_path / f"leica-{color}.json"
        layout_path.write_text(json.dumps(fl.to_json()), encoding="utf-8")
        out_npy = tmp_path / f"leica-{color}.npy"
        code, payload = run_json(
            [
                "inktext-frame",
                "--in",
                str(in_npy),
                "--layout",
                str(layout_path),
                *_font_args(dehinted),
                "--out",
                str(out_npy),
            ]
        )
        cli_ok(code, payload, "inktext-frame builtin logo")
        check(payload.get("exact") is False, "builtin logo mark must report exact=False")
        got = np.load(out_npy)
        logo_items = [it for it in fl.items if it.get("k") == "logo"]
        check(len(logo_items) == 1, "expected 1 logo item")
        li = logo_items[0]
        box = got[li["y"] : li["y"] + li["h"], li["x"] : li["x"] + li["w"]]
        # Verify at least some pixels inside logo box reached the expected ink RGB
        dist = np.abs(box.astype(np.int16) - np.array(expected_ink, dtype=np.int16)).sum(axis=2)
        check(int((dist <= 4).sum()) > 10, "builtin logo ink color mismatch")

    # Missing clogo in bundle -> unknown_logo before rendering
    bad_layout = {
        "width": 100,
        "height": 80,
        "photo": [10, 10],
        "bg": [255, 255, 255],
        "items": [
            {"k": "photo", "x": 10, "y": 10, "w": 80, "h": 60},
            {"k": "clogo", "id": "missing_id", "x": 10, "y": 4, "w": 20, "h": 12, "mode": "orig", "ink": [20, 20, 20]},
        ],
    }
    code_err, err_payload = run_json(
        [
            "inktext-frame",
            "--in",
            str(in_npy),
            "--layout-json",
            json.dumps(bad_layout),
            *_font_args(dehinted),
            "--out",
            str(tmp_path / "missing-logo.npy"),
        ]
    )
    check(code_err == 2, "unknown_logo must exit 2")
    same_kind(err_payload, "unknown_logo", "missing clogo id")


def test_decode_logo_bundle_all_three_formats(tmp_path: Path) -> None:
    """decode_logo_bundle accepts tuple array, object array, and object map equivalently."""
    dehinted = dehinted_font_paths(tmp_path, zero_maxp=False)
    photo = _synth_photo(180, 120, seed=402)
    logo_arr = _synth_logo_rgba(64, 48)
    raw_bytes = logo_arr.tobytes()

    in_npy = tmp_path / "bundle-photo.npy"
    np.save(in_npy, photo)
    blob_path = tmp_path / "logos.bin"
    blob_path.write_bytes(raw_bytes)

    layout = {
        "width": 200,
        "height": 160,
        "photo": [10, 20],
        "bg": [255, 255, 255],
        "items": [
            {"k": "photo", "x": 10, "y": 20, "w": 180, "h": 120},
            {"k": "clogo", "id": "l1", "x": 16, "y": 144, "w": 48, "h": 12, "mode": "orig", "ink": [20, 20, 20]},
        ],
    }
    layout_path = tmp_path / "bundle-layout.json"
    layout_path.write_text(json.dumps(layout), encoding="utf-8")

    indexes = {
        "tuples": [["l1", 64, 48]],
        "objects": [{"id": "l1", "w": 64, "h": 48, "offset": 0, "len": len(raw_bytes)}],
        "map": {"l1": {"w": 64, "h": 48, "offset": 0, "len": len(raw_bytes)}},
    }
    outputs: list[np.ndarray] = []
    for name, idx_obj in indexes.items():
        idx_path = tmp_path / f"idx-{name}.json"
        out_npy = tmp_path / f"out-{name}.npy"
        idx_path.write_text(json.dumps(idx_obj), encoding="utf-8")
        code, payload = run_json(
            [
                "inktext-frame",
                "--in",
                str(in_npy),
                "--layout",
                str(layout_path),
                *_font_args(dehinted),
                "--logo-index",
                str(idx_path),
                "--logo-blob",
                str(blob_path),
                "--out",
                str(out_npy),
            ]
        )
        cli_ok(code, payload, "inktext-frame bundle format")
        outputs.append(np.load(out_npy))

    same_pixels(outputs[1], outputs[0], "object-array vs tuple-array")
    same_pixels(outputs[2], outputs[0], "object-map vs tuple-array")
