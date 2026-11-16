"""End-to-end phone export pipeline parity for ``export_ink`` / ``export-run-ink`` (Item 8).

Covers:
- Full ``export-run-prepare`` -> ``export-run-ink`` -> ``export-run-complete`` Gate A parity
  against Python ``export_finish`` (with de-hinted TTF fonts and pinned ICC profile) across
  ``canvas="full"``, ``canvas="photo"`` (``blur_fill``, ``darkroom``), ``border_stamp``,
  ``leica`` custom ``clogo``, and ``"original": true``.
- HDR ``stamp_hdr_mask`` parity (``stamp_on="photo"`` non-empty mask vs ``stamp_on="canvas"``
  empty mask) and complete Ultra HDR JPEG output.
- Native CLI vs WASM parity via ``mobile/scripts/wasm_cases/export.mjs`` (``kind="ink"``),
  covering ``ink-json``, ``ink-canvas``, ``ink-mask``, ``unknown_logo``, ``bad_state``, and
  hostile ``f64`` dimensions.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from gxr_studio import export as export_mod
from gxr_studio.finish.compose import finish_array
from gxr_studio.finish.fonts import FONT_FILES, PilFonts, open_face
from gxr_studio.finish.logo_math import LogoInfo
from gxr_studio.finish.spec import FrameSpec, StampSpec
from gxr_studio.render.params import parse_edit
from support.core_cli import require_cli, run_json
from support.export_pipeline_cases import (
    anon_ids,
    check,
    cli_ok,
    same_file_redacted,
    same_pixels,
    synthetic_source,
)
from support.frame_cases import FIXTURE_CJK, desktop_mark
from support.ink_text_cases import dehinted_font_paths, font_path
from support.wasm_check import run_area


@pytest.fixture(autouse=True)
def _cli() -> None:
    require_cli()


@pytest.fixture
def pinned_icc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> bytes:
    rgb = np.zeros((8, 8, 3), dtype=np.uint8)
    np.save(tmp_path / "icc.npy", rgb)
    code, payload = run_json(["jpeg-encode", "--in", str(tmp_path / "icc.npy"), "--quality", "95", "--out", str(tmp_path / "icc.jpg")])
    cli_ok(code, payload, "jpeg-encode for the ICC pin")
    with Image.open(tmp_path / "icc.jpg") as im:
        rs = bytes(im.info["icc_profile"])
    monkeypatch.setattr(export_mod, "_ICC", rs)
    return rs


@contextmanager
def _patched_pil_fonts(font_map: dict[str, Path]) -> Any:
    saved = PilFonts._load

    @staticmethod
    def _custom_load(font: str, px: int, cjk: bool) -> Any:
        key = "cjk" if cjk else font
        return open_face(str(font_map[key]), px)

    PilFonts._load = _custom_load  # type: ignore[method-assign]
    try:
        yield
    finally:
        PilFonts._load = saved  # type: ignore[method-assign]


def _w(tmp: Path, name: str, obj: Any) -> str:
    p = tmp / name
    p.write_text(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, allow_nan=True), encoding="utf-8")
    return str(p)


def _font_args(font_map: dict[str, Path]) -> list[str]:
    out: list[str] = []
    for name, path in font_map.items():
        out += ["--font", f"{name}={path}"]
    return out


def _font_pairs(font_map: dict[str, Path]) -> list[list[str]]:
    return [[name, str(path)] for name, path in font_map.items()]


def _synth_rgb(w: int = 160, h: int = 120, seed: int = 77) -> np.ndarray:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    grad = np.stack(
        [
            (xx * 220 // max(1, w - 1)),
            (yy * 220 // max(1, h - 1)),
            ((xx + yy) * 180 // max(1, w + h - 2)),
        ],
        axis=-1,
    ).astype(np.int16)
    return np.clip(grad + rng.integers(-15, 16, size=(h, w, 3), dtype=np.int16), 0, 255).astype(np.uint8)


def _synth_logo_rgba(w: int = 96, h: int = 72) -> np.ndarray:
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    arr[10 : h - 10, 12 : w - 12] = [200, 90, 40, 220]
    arr[20 : h - 20, 24 : w - 24] = [25, 30, 36, 255]
    return arr


PIPELINE_SIDECARS: list[tuple[str, dict[str, Any], bool]] = [
    ("original", {}, False),
    ("stamp_only", {"stamp": StampSpec(enabled=True, font="seg7", size="medium", date_override="2026-11-15").to_json()}, False),
    (
        "gallery_stamp",
        {
            "border": FrameSpec(style="gallery", text="GALLERY").to_json(),
            "stamp": StampSpec(enabled=True, font="typewriter", placement="photo", date_override="2026-11-15").to_json(),
        },
        False,
    ),
    (
        "instant_border",
        {
            "border": FrameSpec(style="instant", fmt="instax_mini", text="MINI").to_json(),
            "stamp": StampSpec(enabled=True, font="seg7", placement="border", date_override="2026-11-15").to_json(),
        },
        False,
    ),
    (
        "blur_fill_stamp",
        {
            "border": FrameSpec(style="blur_fill", show_logo=False, text="BLUR").to_json(),
            "stamp": StampSpec(enabled=True, font="sans", placement="photo", date_override="2026-11-15").to_json(),
        },
        False,
    ),
    (
        "darkroom_crop",
        {
            "adjust": {"exposure_ev": 0.2, "contrast": 8},
            "crop": {"rect": [0.05, 0.05, 0.9, 0.9], "preset": "free"},
            "border": FrameSpec(style="darkroom", text="DARK").to_json(),
            "stamp": StampSpec(enabled=True, placement="photo", date_override="2026-11-15").to_json(),
        },
        False,
    ),
    (
        "leica_custom_logo",
        {
            "border": FrameSpec(style="leica", show_logo=True, logo="custom1", logo_mode="auto", text="A12").to_json(),
        },
        True,
    ),
]


@pytest.mark.parametrize("case_idx", range(len(PIPELINE_SIDECARS)), ids=anon_ids(len(PIPELINE_SIDECARS)))
def test_end_to_end_prepare_ink_complete_parity(case_idx: int, tmp_path: Path, pinned_icc: bytes) -> None:
    """Gate A: prepare -> ink -> complete matches Python finish_array & write_rgb_with_exif 0-diff."""
    tag, side_obj, use_clogo = PIPELINE_SIDECARS[case_idx]
    dehinted = dehinted_font_paths(tmp_path, zero_maxp=False)
    rgb = _synth_rgb(160, 120, seed=500 + case_idx)
    jpg_npy = tmp_path / f"{tag}-jpg.npy"
    np.save(jpg_npy, rgb)
    src = synthetic_source(tmp_path / f"{tag}-src.jpg")

    meta = {"lens": "A12 50mm", "lens_short": "50mm", "aperture": "f/2.5", "shutter": "1/250s", "iso": "ISO 200", "date": "2026-11-15"}
    logo_info = LogoInfo("custom1", "Custom", 96, 72, 0.5, False, 110.0)
    logo_arr = _synth_logo_rgba()
    logo_npy = tmp_path / f"{tag}-clogo.npy"
    if use_clogo:
        np.save(logo_npy, logo_arr)

    layout_obj: dict[str, Any] = {
        "meta": meta,
        "mark": desktop_mark(),
        "taken": [2026, 11, 15, 10, 30],
    }
    if use_clogo:
        layout_obj["logos"] = [
            {
                "id": logo_info.id,
                "name": logo_info.name,
                "w": logo_info.w,
                "h": logo_info.h,
                "coverage": logo_info.coverage,
                "opaque": logo_info.opaque,
                "lum": logo_info.lum,
            }
        ]

    shot_path = _w(tmp_path, f"{tag}-shot.json", {"name": "R0050", "width": 160, "height": 120, "files": {"jpg": {}}})
    side_path = _w(tmp_path, f"{tag}-side.json", side_obj)
    layout_path = _w(tmp_path, f"{tag}-layout.json", layout_obj)

    prep_canvas = tmp_path / f"{tag}-prep.npy"
    code_p, payload_p = run_json(
        [
            "export-run-prepare",
            "--shot",
            shot_path,
            "--side",
            side_path,
            "--layout",
            layout_path,
            "--jpg",
            str(jpg_npy),
            "--quality",
            "95",
            *_font_args(dehinted),
            "--out",
            str(prep_canvas),
        ]
    )
    cli_ok(code_p, payload_p, "export-run-prepare")
    prep_json_path = _w(tmp_path, f"{tag}-prepared.json", payload_p)

    ink_canvas = tmp_path / f"{tag}-ink.npy"
    ink_args = [
        "export-run-ink",
        "--canvas",
        str(prep_canvas),
        "--prepared",
        prep_json_path,
        *_font_args(dehinted),
        "--out",
        str(ink_canvas),
    ]
    if use_clogo:
        ink_args += ["--logo", f"custom1={logo_npy}"]
    code_i, payload_i = run_json(ink_args)
    cli_ok(code_i, payload_i, "export-run-ink")
    check(payload_i.get("exact") is True, "expected exact=True for dehinted export-run-ink")
    check(payload_i.get("mask_width") == 0 and payload_i.get("mask_height") == 0, "expected empty SDR stamp mask")

    got_canvas = np.load(ink_canvas)
    edit = parse_edit(side_obj)
    frame_spec = edit.frame if edit.frame is not None else FrameSpec()
    stamp_spec = edit.stamp if edit.stamp is not None else StampSpec()
    crop_raw = side_obj.get("crop") if isinstance(side_obj.get("crop"), dict) else {}
    raw_rect = crop_raw.get("rect") if isinstance(crop_raw, dict) else None
    crop_rect = (
        (float(raw_rect[0]), float(raw_rect[1]), float(raw_rect[2]), float(raw_rect[3]))
        if isinstance(raw_rect, list) and len(raw_rect) == 4
        else None
    )
    raw_preset = crop_raw.get("preset") if isinstance(crop_raw, dict) else None
    crop_preset = raw_preset if isinstance(raw_preset, str) else None
    with _patched_pil_fonts(dehinted):
        want_canvas, _ = finish_array(
            rgb,
            edit,
            stage="final",
            frame=frame_spec,
            stamp=stamp_spec,
            crop_rect=crop_rect,
            meta=meta,
            taken=datetime(2026, 11, 15, 10, 30),
            file_time=None,
            from_raw=False,
            crop_preset=crop_preset,
            logos={"custom1": logo_info} if use_clogo else None,
            logo_images={"custom1": Image.fromarray(logo_arr, "RGBA")} if use_clogo else None,
        )
    same_pixels(got_canvas, want_canvas, f"export-ink-{tag}")

    # Step 3: export-run-complete byte parity against Python write_rgb_with_exif
    state_obj = (
        {"name": "R0050", "quality": 95, "long_edge": None, "labels": {"text": "原图"}, "hdr": None}
        if payload_p.get("original")
        else payload_p["prepared"]["state"]
    )
    state_path = _w(tmp_path, f"{tag}-state.json", state_obj)
    out_jpg = tmp_path / f"{tag}-out.jpg"
    code_c, payload_c = run_json(
        [
            "export-run-complete",
            "--canvas",
            str(ink_canvas),
            "--state",
            state_path,
            "--src",
            str(src),
            "--out",
            str(out_jpg),
        ]
    )
    cli_ok(code_c, payload_c, "export-run-complete")
    note_text = state_obj.get("labels", {}).get("description") or state_obj.get("labels", {}).get("text") or "原图"
    want_jpg = export_mod.write_rgb_with_exif(want_canvas, src, note_text, quality=95, fmt="jpeg")
    same_file_redacted(out_jpg.read_bytes(), want_jpg, f"complete-jpg-{tag}")


def test_export_ink_hdr_mask_photo_vs_border_stamp(tmp_path: Path) -> None:
    """HDR active + stamp_on='photo' produces exact HDR stamp mask; stamp_on='canvas' leaves mask empty."""
    dehinted = dehinted_font_paths(tmp_path, zero_maxp=False)
    photo = _synth_rgb(160, 120, seed=601)
    canvas_npy = tmp_path / "hdr-photo.npy"
    np.save(canvas_npy, photo)

    sspec = StampSpec(enabled=True, font="seg7", size="medium", date_override="2026-11-15")
    from gxr_studio.finish.fonts import FontMetrics
    from gxr_studio.finish.layout import frame_layout, stamp_layout
    from gxr_studio.finish.stamp import apply_stamp, format_stamp

    metrics = FontMetrics()
    taken = datetime(2026, 11, 15, 12, 0)
    sl_photo = stamp_layout(160, 120, sspec, format_stamp(sspec, taken, None), metrics, None)
    check(sl_photo is not None, "photo stamp layout must exist")
    if sl_photo is None:
        return

    fl = frame_layout(160, 120, FrameSpec(style="instant", fmt="instax_mini"), {}, metrics)
    check(fl is not None, "instant layout must exist")
    if fl is None:
        return
    sl_border = stamp_layout(160, 120, sspec, format_stamp(sspec, taken, None), metrics, fl)
    check(sl_border is not None, "border stamp layout must exist")
    if sl_border is None:
        return

    with _patched_pil_fonts(dehinted):
        black = np.zeros_like(photo)
        white = np.full_like(photo, 255)
        want_mask = (
            ((apply_stamp(black.copy(), sspec, taken, None, sl_photo) != black).any(axis=2))
            | ((apply_stamp(white.copy(), sspec, taken, None, sl_photo) != white).any(axis=2))
        ).astype(np.uint8)

    # Case 1: HDR active + stamp_on == "photo"
    hdr_state = {
        "name": "R0060",
        "quality": 95,
        "long_edge": None,
        "labels": {},
        "hdr": {"asked": True, "active": True, "shape": [120, 160, 2], "problem": None, "frame": None},
    }
    prep_photo = {
        "original": False,
        "prepared": {
            "canvas": "photo",
            "frame": fl.to_json(),
            "stamp": sl_photo.to_json(),
            "stamp_on": "photo",
            "photo": {"x": fl.photo[0], "y": fl.photo[1], "w": 160, "h": 120},
            "state": hdr_state,
        },
    }
    mask_out = tmp_path / "hdr-mask-photo.npy"
    out_npy = tmp_path / "hdr-ink-photo.npy"
    code, payload = run_json(
        [
            "export-run-ink",
            "--canvas",
            str(canvas_npy),
            "--prepared",
            _w(tmp_path, "prep-hdr-photo.json", prep_photo),
            *_font_args(dehinted),
            "--stamp-mask-out",
            str(mask_out),
            "--out",
            str(out_npy),
        ]
    )
    cli_ok(code, payload, "export-run-ink hdr photo stamp")
    check(payload.get("mask_width") == 160 and payload.get("mask_height") == 120, "hdr mask dims mismatch")
    same_pixels(np.load(mask_out), want_mask, "hdr-stamp-mask-photo")

    # Case 2: HDR active + stamp_on == "canvas" (border stamp -> empty stamp mask)
    prep_border = {
        "original": False,
        "prepared": {
            "canvas": "photo",
            "frame": fl.to_json(),
            "stamp": sl_border.to_json(),
            "stamp_on": "canvas",
            "photo": {"x": fl.photo[0], "y": fl.photo[1], "w": 160, "h": 120},
            "state": hdr_state,
        },
    }
    mask_border_out = tmp_path / "hdr-mask-border.npy"
    out_border_npy = tmp_path / "hdr-ink-border.npy"
    code2, payload2 = run_json(
        [
            "export-run-ink",
            "--canvas",
            str(canvas_npy),
            "--prepared",
            _w(tmp_path, "prep-hdr-border.json", prep_border),
            *_font_args(dehinted),
            "--stamp-mask-out",
            str(mask_border_out),
            "--out",
            str(out_border_npy),
        ]
    )
    cli_ok(code2, payload2, "export-run-ink hdr border stamp")
    check(payload2.get("mask_width") == 0 and payload2.get("mask_height") == 0, "border stamp mask must be empty")


def test_wasm_matches_native_export_ink(tmp_path: Path) -> None:
    """WASM export_ink matches CLI export-run-ink across full/photo/HDR/logos/original and errors."""
    dehinted = dehinted_font_paths(tmp_path, zero_maxp=False)
    real_fonts = {name: font_path(name) for name in FONT_FILES}
    real_fonts["cjk"] = FIXTURE_CJK
    fonts_pair_list = _font_pairs(dehinted)
    fonts_real_list = _font_pairs(real_fonts)

    rgb = _synth_rgb(96, 72, seed=701)
    pic_npy = tmp_path / "wasm-pic.npy"
    np.save(pic_npy, rgb)
    logo_npy = tmp_path / "wasm-clogo.npy"
    np.save(logo_npy, _synth_logo_rgba(64, 48))

    shot = _w(tmp_path, "shot.json", {"name": "R0070", "width": 96, "height": 72, "files": {"jpg": {}}})
    layout = _w(
        tmp_path,
        "layout.json",
        {
            "meta": {"lens": "A12 50mm", "lens_short": "50mm", "aperture": "f/2.5", "shutter": "1/250s", "iso": "ISO 200"},
            "mark": desktop_mark(),
            "taken": [2026, 11, 15, 10, 30],
            "logos": [{"id": "custom1", "name": "Custom", "w": 64, "h": 48, "coverage": 0.5, "opaque": False, "lum": 110.0}],
        },
    )

    prep_specs = [
        ("original", {}, False, False),
        (
            "gallery_hdr_stamp",
            {"border": FrameSpec(style="gallery").to_json(), "stamp": StampSpec(enabled=True, date_override="2026-11-15").to_json()},
            True,
            False,
        ),
        (
            "instant_border",
            {
                "border": FrameSpec(style="instant", fmt="instax_mini").to_json(),
                "stamp": StampSpec(enabled=True, placement="border", date_override="2026-11-15").to_json(),
            },
            False,
            False,
        ),
        ("blur_fill", {"border": FrameSpec(style="blur_fill", show_logo=False, text="BLUR").to_json()}, False, False),
        ("darkroom", {"border": FrameSpec(style="darkroom", text="DARK").to_json()}, False, False),
        ("leica_builtin_logo", {"border": FrameSpec(style="leica", show_logo=True, text="GXR").to_json()}, False, False),
        ("leica_clogo", {"border": FrameSpec(style="leica", show_logo=True, logo="custom1", text="CUSTOM").to_json()}, False, True),
    ]

    cases: list[dict[str, Any]] = []
    for idx, (tag, side_obj, hdr, use_clogo) in enumerate(prep_specs):
        side_p = _w(tmp_path, f"ws-{tag}.json", side_obj)
        out_c = tmp_path / f"wp-{tag}.npy"
        code, payload = run_json(
            [
                "export-run-prepare",
                "--shot",
                shot,
                "--side",
                side_p,
                "--layout",
                layout,
                "--jpg",
                str(pic_npy),
                "--quality",
                "95",
                *_font_args(dehinted),
                "--out",
                str(out_c),
            ]
        )
        cli_ok(code, payload, "prepare for wasm ink test")
        if hdr and not payload.get("original"):
            payload["prepared"]["state"]["hdr"] = {"asked": True, "active": True, "shape": [72, 96, 2], "problem": None, "frame": None}
        prep_p = _w(tmp_path, f"wprep-{tag}.json", payload)
        fonts_use = fonts_real_list if tag == "leica_builtin_logo" else fonts_pair_list
        entry: dict[str, Any] = {
            "label": f"ink-{tag}",
            "kind": "ink",
            "canvas": str(out_c),
            "prepared": prep_p,
            "fonts": fonts_use,
            "hdr": hdr,
            "out": str(tmp_path / f"wout-{idx}"),
        }
        if use_clogo:
            entry["logos"] = [["custom1", str(logo_npy)]]
        cases.append(entry)
        if tag == "leica_clogo":
            # Missing logo in bundle -> unknown_logo parity between CLI & WASM
            cases.append(
                {
                    "label": "ink-unknown-logo",
                    "kind": "ink",
                    "canvas": str(out_c),
                    "prepared": prep_p,
                    "fonts": fonts_use,
                    "hdr": False,
                    "out": str(tmp_path / "wout-err-logo"),
                }
            )

    # Bad prepared JSON + hostile f64 dims
    cases.append(
        {
            "label": "ink-bad-state",
            "kind": "ink",
            "canvas": str(pic_npy),
            "prepared": _w(tmp_path, "wprep-bad.json", "42"),
            "fonts": fonts_pair_list,
            "hdr": False,
            "out": str(tmp_path / "wout-badstate"),
        }
    )
    ok_prep = _w(tmp_path, "wprep-ok.json", {"original": True})
    for i, dim_val in enumerate(["NaN", "Infinity", -1, 1.5, 2**32 + 1]):
        cases.append(
            {
                "label": f"ink-dim-w-{i}",
                "kind": "ink",
                "canvas": str(pic_npy),
                "prepared": ok_prep,
                "fonts": fonts_pair_list,
                "hdr": False,
                "w": dim_val,
                "h": 72,
                "out": str(tmp_path / f"wout-dim-{i}"),
            }
        )

    manifest = _w(tmp_path, "ink-export-cases.json", cases)
    proc = run_area("export", [Path(manifest)], tmp_path)
    check(proc.returncode == 0, "wasm export_ink parity failed")
