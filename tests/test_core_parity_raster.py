"""Parity: gxr-cli (finish-*) and WASM against Python finish/{pixels,compose,frame_paint,frame,logo_store,logo_math}.py."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from gxr_studio.finish.compose import finish_unframed, load_jpeg
from gxr_studio.finish.fonts import FontMetrics, PilFonts
from gxr_studio.finish.frame import draw_frame
from gxr_studio.finish.layout import FrameLayout, frame_layout
from gxr_studio.finish.pixels import _linear_to_srgb, _srgb_to_linear, apply_base, apply_extra
from gxr_studio.finish.spec import FRAME_STYLES, FrameSpec, Geometry, StampSpec
from gxr_studio.raw.dng import RawDecodeError, open_dng
from gxr_studio.raw.render import render_half
from gxr_studio.render.params import EditState, parse_adjust, parse_curve
from support.core_cli import error_of, require_cli, run_json

_STUDIO = Path(__file__).resolve().parents[1]
_PARENT = Path(os.environ.get("GXR_STUDIO_TEST_PARENT", str(_STUDIO.parent)))
CAPTURES = _PARENT / "captures"


def _synth_rgb(w: int, h: int, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    r = ((xx * 255) // max(1, w - 1)).astype(np.uint8)
    g = ((yy * 255) // max(1, h - 1)).astype(np.uint8)
    b = rng.integers(0, 256, size=(h, w), dtype=np.uint8)
    rgb = np.stack([r, g, b], axis=-1)
    if h >= 4 and w >= 4:
        # Corners of color cube in top-left 2x4 block
        corners = np.array(
            [
                [0, 0, 0],
                [255, 0, 0],
                [0, 255, 0],
                [0, 0, 255],
                [255, 255, 0],
                [255, 0, 255],
                [0, 255, 255],
                [255, 255, 255],
            ],
            dtype=np.uint8,
        ).reshape(2, 4, 3)
        rgb[:2, :4] = corners
    return np.ascontiguousarray(rgb)


def _save_npy(path: Path, arr: np.ndarray) -> Path:
    np.save(path, arr)
    return path


def _run_finish_array(
    tmp_path: Path,
    rgb: np.ndarray,
    edit: dict[str, Any] | str,
    options: dict[str, Any] | str,
    tag: str,
) -> tuple[int, dict[str, Any], np.ndarray | None]:
    in_path = _save_npy(tmp_path / f"{tag}-in.npy", np.ascontiguousarray(rgb, dtype=np.uint8))
    out_path = tmp_path / f"{tag}-out.npy"
    edit_path = tmp_path / f"{tag}-edit.json"
    opts_path = tmp_path / f"{tag}-opts.json"
    edit_path.write_text(edit if isinstance(edit, str) else json.dumps(edit), encoding="utf-8")
    opts_path.write_text(options if isinstance(options, str) else json.dumps(options), encoding="utf-8")
    code, payload = run_json(
        [
            "finish-array",
            "--in",
            str(in_path),
            "--edit",
            str(edit_path),
            "--options",
            str(opts_path),
            "--out",
            str(out_path),
        ]
    )
    arr = np.load(out_path) if code == 0 and out_path.is_file() else None
    return code, payload, arr


# ---- 1. Adjustments (pixels.py) ----------------------------------------------


def _f32_round_decimal(d: Any) -> np.float32:
    from decimal import Decimal

    c = np.float32(float(d))
    cands = [np.nextafter(c, np.float32(-np.inf)), c, np.nextafter(c, np.float32(np.inf))]
    best = min(abs(Decimal(float(x)) - d) for x in cands)
    near = [x for x in cands if abs(Decimal(float(x)) - d) == best]
    return near[0] if len(near) == 1 else next(x for x in near if int(x.view(np.uint32)) % 2 == 0)


def _srgb_to_linear_oracle_table() -> np.ndarray:
    from decimal import ROUND_HALF_EVEN, Decimal, localcontext

    def _dec32(x: float) -> Decimal:
        return Decimal(float(np.float32(x)))

    out = np.empty(256, dtype=np.float32)
    with localcontext() as ctx:
        ctx.prec = 50
        ctx.rounding = ROUND_HALF_EVEN
        for c in range(256):
            v = _f32_round_decimal(Decimal(c) / Decimal(255))
            dv = Decimal(float(v))
            if v <= np.float32(0.04045):
                out[c] = _f32_round_decimal(dv / _dec32(12.92))
            else:
                base = _f32_round_decimal(Decimal(float(_f32_round_decimal(dv + _dec32(0.055)))) / _dec32(1.055))
                out[c] = _f32_round_decimal(Decimal(float(base)) ** _dec32(2.4))
    return out


def test_srgb_and_linear_roundtrip_and_edge_values(tmp_path: Path) -> None:
    require_cli()
    vals = np.arange(256, dtype=np.uint8).reshape(16, 16, 1)
    rgb = np.repeat(vals, 3, axis=2)
    in_path = _save_npy(tmp_path / "u8.npy", rgb)
    out_lin = tmp_path / "lin.npy"
    code, payload = run_json(["finish-srgb-to-linear", "--in", str(in_path), "--out", str(out_lin)])
    assert code == 0, payload
    rs_lin = np.load(out_lin)
    oracle_lin = _srgb_to_linear_oracle_table()[rgb]
    assert np.array_equal(rs_lin.view(np.uint32), oracle_lin.view(np.uint32))
    py_lin = _srgb_to_linear(rgb)
    # Any difference from real Python is Apple libm powff being 1 ULP off the correctly rounded f32 value.
    rs_u32 = rs_lin[:, :, 0].ravel().view(np.uint32)
    py_u32 = py_lin[:, :, 0].ravel().view(np.uint32)
    ulp_diff = np.abs(rs_u32.astype(np.int64) - py_u32.astype(np.int64))
    assert int(ulp_diff.max()) <= 1

    lin_probe = np.array(
        [[[-0.5, 0.0, 0.003], [0.0031308, 0.0031309, 0.25], [0.5, 1.0, 1.5], [math.nan, 0.123456, 0.999]]],
        dtype=np.float32,
    )
    in_f32 = _save_npy(tmp_path / "f32.npy", lin_probe)
    out_u8 = tmp_path / "u8_back.npy"
    code2, payload2 = run_json(["finish-linear-to-srgb", "--in", str(in_f32), "--out", str(out_u8)])
    assert code2 == 0, payload2
    rs_u8 = np.load(out_u8)
    py_u8 = _linear_to_srgb(lin_probe)
    assert np.array_equal(rs_u8, py_u8)


@pytest.mark.parametrize("w,h", [(1, 1), (1, 7), (7, 1), (17, 13)])
def test_adjustments_noop_and_sliders_match_python(tmp_path: Path, w: int, h: int) -> None:
    require_cli()
    rgb = _synth_rgb(w, h, seed=11)
    # Zero sliders must return input unchanged for both from_raw=False and from_raw=True
    for from_raw in (False, True):
        code, payload, out = _run_finish_array(
            tmp_path,
            rgb,
            {"adjust": {"exposure_ev": 0.0, "wb": {"warm": 0, "tint": 0}}},
            {"stage": "uncropped", "from_raw": from_raw},
            f"noop-{w}x{h}-{from_raw}",
        )
        assert code == 0, payload
        assert out is not None and np.array_equal(out, rgb)

    # Extra-only sliders on odd sizes at extremes
    extra_cases = [
        {"black_point": 100, "saturation": 0, "vibrance": 0, "vignette": 0},
        {"black_point": -100, "saturation": 0, "vibrance": 0, "vignette": 0},
        {"black_point": 0, "saturation": 100, "vibrance": -100, "vignette": 0},
        {"black_point": 0, "saturation": -100, "vibrance": 100, "vignette": 0},
        {"black_point": 0, "saturation": 0, "vibrance": 0, "vignette": 100},
        {"black_point": 0, "saturation": 0, "vibrance": 0, "vignette": -100},
        {"black_point": 45, "saturation": -35, "vibrance": 60, "vignette": -50},
    ]
    for i, adj_dict in enumerate(extra_cases):
        adj = parse_adjust(adj_dict)
        py_out = apply_extra(rgb, adj)
        code, payload, rs_out = _run_finish_array(
            tmp_path,
            rgb,
            {"adjust": adj_dict},
            {"stage": "uncropped", "from_raw": True},
            f"extra-{w}x{h}-{i}",
        )
        assert code == 0, payload
        assert rs_out is not None
        assert np.array_equal(rs_out, py_out), f"extra mismatch {w}x{h} #{i}: max diff={np.abs(rs_out.astype(int) - py_out.astype(int)).max()}"

    # Base exposure/warm/tint + extra sliders
    base_cases: list[dict[str, Any]] = [
        {"exposure_ev": 5.0, "wb": {"warm": 100, "tint": -100}},
        {"exposure_ev": -5.0, "wb": {"warm": -100, "tint": 100}},
        {"exposure_ev": 1.25, "wb": {"warm": 35, "tint": -20}, "black_point": 20, "saturation": 15, "vibrance": 30, "vignette": -40},
    ]
    for i, base_adj in enumerate(base_cases):
        adj = parse_adjust(base_adj)
        py_out = apply_extra(apply_base(rgb, adj, None), adj)
        code, payload, rs_out = _run_finish_array(
            tmp_path,
            rgb,
            {"adjust": base_adj},
            {"stage": "uncropped", "from_raw": False},
            f"base-{w}x{h}-{i}",
        )
        assert code == 0, payload
        assert rs_out is not None
        assert np.array_equal(rs_out, py_out), f"base {w}x{h} #{i} max diff={int(np.abs(rs_out.astype(int) - py_out.astype(int)).max())}"


@pytest.mark.parametrize("w,h", [(1, 1), (1, 7), (7, 1), (17, 13)])
def test_apply_base_tone_and_curves_match_python(tmp_path: Path, w: int, h: int) -> None:
    require_cli()
    rgb = _synth_rgb(w, h, seed=17)
    cases: list[tuple[str, dict[str, Any], dict[str, Any] | None]] = [
        ("contrast_pos", {"contrast": 100}, None),
        ("contrast_neg", {"contrast": -100}, None),
        ("highlight_pos", {"highlight": 100}, None),
        ("highlight_neg", {"highlight": -100}, None),
        ("shadow_pos", {"shadow": 100}, None),
        ("shadow_neg", {"shadow": -100}, None),
        ("tone_combo", {"contrast": 45, "highlight": -60, "shadow": 75}, None),
        ("curve_ident", {}, {"master": [[0, 0], [255, 255]]}),
        ("curve_master", {}, {"master": [[0, 12], [64, 48], [180, 210], [255, 245]]}),
        ("curve_r_only", {}, {"r": [[0, 0], [100, 145], [255, 255]]}),
        ("curve_g_only", {}, {"g": [[0, 10], [128, 95], [255, 240]]}),
        ("curve_b_only", {}, {"b": [[0, 20], [128, 160], [255, 255]]}),
        (
            "curve_all_channels",
            {},
            {
                "master": [[0, 0], [80, 95], [190, 175], [255, 255]],
                "r": [[0, 5], [120, 150], [255, 250]],
                "g": [[0, 0], [128, 118], [255, 255]],
                "b": [[0, 15], [140, 110], [255, 240]],
            },
        ),
        (
            "full_base",
            {"exposure_ev": 1.4, "wb": {"warm": -35, "tint": 40}, "contrast": -30, "highlight": 55, "shadow": -40},
            {
                "master": [[0, 8], [72, 60], [176, 196], [255, 250]],
                "r": [[0, 0], [128, 148], [255, 255]],
                "b": [[0, 10], [128, 108], [255, 245]],
            },
        ),
    ]
    for tag, adj_dict, curve_dict in cases:
        adj = parse_adjust(adj_dict)
        curves = parse_curve(curve_dict)
        py_out = apply_base(rgb, adj, curves)
        edit: dict[str, Any] = {"adjust": adj_dict}
        if curve_dict is not None:
            edit["curve"] = curve_dict
        code, payload, rs_out = _run_finish_array(
            tmp_path,
            rgb,
            edit,
            {"stage": "uncropped", "from_raw": False},
            f"table-{w}x{h}-{tag}",
        )
        assert code == 0, (tag, payload)
        assert rs_out is not None
        assert np.array_equal(rs_out, py_out), (
            f"table mismatch {w}x{h} {tag}: max diff={int(np.abs(rs_out.astype(int) - py_out.astype(int)).max())}"
        )


def test_apply_base_bad_adjust_and_bad_curve_errors(tmp_path: Path) -> None:
    require_cli()
    rgb = _synth_rgb(4, 4, seed=3)
    bad_cases: list[tuple[str, dict[str, Any], str]] = [
        ("ev_oob", {"adjust": {"exposure_ev": 5.1}}, "bad_adjust"),
        ("contrast_oob", {"adjust": {"contrast": 101}}, "bad_adjust"),
        ("unknown_adj", {"adjust": {"clarity": 10}}, "bad_adjust"),
        ("curve_too_short", {"curve": {"master": [[0, 0]]}}, "bad_curve"),
        ("curve_non_mono", {"curve": {"master": [[0, 0], [128, 100], [128, 200]]}}, "bad_curve"),
        ("curve_oob", {"curve": {"r": [[0, 0], [256, 255]]}}, "bad_curve"),
        ("curve_unknown_ch", {"curve": {"luma": [[0, 0], [255, 255]]}}, "bad_curve"),
    ]
    for tag, edit, expected_kind in bad_cases:
        code, payload, _ = _run_finish_array(
            tmp_path,
            rgb,
            edit,
            {"stage": "uncropped", "from_raw": False},
            f"err-{tag}",
        )
        err = error_of(code, payload)
        assert err is not None and err.kind == expected_kind, (tag, code, payload)


# ---- 2. Unframed finish (finish-array) ---------------------------------------


def test_finish_array_synthetic_branches_match_python(tmp_path: Path) -> None:
    require_cli()
    rgb = _synth_rgb(120, 80, seed=5)
    sample_curve = {"master": [[0, 10], [96, 84], [192, 208], [255, 248]], "r": [[0, 0], [128, 144], [255, 255]]}
    cases: list[tuple[dict[str, Any], dict[str, Any]]] = [
        # Pitfall 6: from_raw=True ignores exposure_ev/warm/tint/contrast/highlight/shadow/curve while from_raw=False applies them
        (
            {"adjust": {"exposure_ev": 2.0, "wb": {"warm": 40, "tint": -20}, "contrast": 35, "saturation": 30}, "curve": sample_curve},
            {"stage": "final", "from_raw": True},
        ),
        (
            {
                "adjust": {
                    "exposure_ev": 2.0,
                    "wb": {"warm": 40, "tint": -20},
                    "contrast": 35,
                    "highlight": -25,
                    "shadow": 40,
                    "saturation": 30,
                },
                "curve": sample_curve,
            },
            {"stage": "final", "from_raw": False},
        ),
        # Stages uncropped & original with straighten & rotate/flip
        ({"geometry": {"straighten": -5.5, "rotate90": 1, "flip": True}}, {"stage": "uncropped", "from_raw": True}),
        ({"geometry": {"straighten": 7.2, "vertical": 15}}, {"stage": "original", "from_raw": True}),
        # Presets including XPan and outside-valid crop_rect
        ({"geometry": {"straighten": 6.0}}, {"stage": "final", "from_raw": True, "crop_preset": "xpan-65-24"}),
        ({"geometry": {"straighten": 6.0}}, {"stage": "final", "from_raw": True, "crop_preset": "1:1"}),
        ({"geometry": {"straighten": 8.0}}, {"stage": "final", "from_raw": True, "crop_rect": [-0.1, -0.1, 1.1, 1.1]}),
        ({"geometry": {"rotate90": 2, "flip": True}}, {"stage": "final", "from_raw": True, "crop_rect": [0.1, 0.15, 0.6, 0.7]}),
    ]
    for i, (edit_dict, opts_dict) in enumerate(cases):
        adj = parse_adjust(edit_dict.get("adjust"))
        curves = parse_curve(edit_dict.get("curve"))
        geom_raw = edit_dict.get("geometry")
        geom = Geometry(**geom_raw) if geom_raw else Geometry()
        edit = EditState(adjust=adj, curve=curves, geometry=geom)
        py_rgb, py_header = finish_unframed(
            rgb,
            edit,
            stage=opts_dict["stage"],
            stamp=StampSpec(enabled=False),
            crop_rect=tuple(opts_dict["crop_rect"]) if "crop_rect" in opts_dict else None,
            taken=None,
            file_time=None,
            from_raw=opts_dict["from_raw"],
            crop_preset=opts_dict.get("crop_preset"),
        )
        code, payload, rs_rgb = _run_finish_array(tmp_path, rgb, edit_dict, opts_dict, f"synth-{i}")
        assert code == 0, payload
        assert payload["header"] == py_header
        assert rs_rgb is not None
        assert rs_rgb.shape == py_rgb.shape
        assert np.array_equal(rs_rgb, py_rgb)


def _real_capture_inputs(limit: int = 24) -> list[tuple[str, np.ndarray, bool]]:
    if not CAPTURES.is_dir():
        return []
    files = sorted(
        (
            p
            for p in CAPTURES.rglob("*")
            if p.is_file() and p.suffix.upper() in {".JPG", ".JPEG", ".DNG"} and "backup" not in p.as_posix().lower()
        ),
        key=lambda p: p.as_posix(),
    )
    if len(files) > limit:
        files = [files[(i * len(files)) // limit] for i in range(limit)]
    out: list[tuple[str, np.ndarray, bool]] = []
    for path in files:
        if path.suffix.upper() == ".DNG":
            try:
                dng = open_dng(path)
                img = render_half(dng)
                if max(img.shape[:2]) > 240:
                    im = Image.fromarray(img, "RGB")
                    im.thumbnail((240, 240), Image.Resampling.LANCZOS)
                    img = np.ascontiguousarray(np.asarray(im))
            except (RawDecodeError, Exception):
                continue
            out.append((path.name, img, True))
        else:
            try:
                img = load_jpeg(path, max_edge=240)
            except Exception:
                continue
            out.append((path.name, img, False))
    return out


def test_finish_array_real_photos_parity(tmp_path: Path) -> None:
    require_cli()
    samples = _real_capture_inputs(limit=24)
    if len(samples) < 20:
        pytest.skip(f"need >= 20 real images in {CAPTURES}, got {len(samples)}")
    curve_a = {"master": [[0, 6], [84, 72], [184, 202], [255, 250]]}
    curve_b = {"master": [[0, 0], [128, 138], [255, 255]], "r": [[0, 0], [120, 140], [255, 255]], "b": [[0, 10], [128, 112], [255, 248]]}
    for idx, (name, rgb, from_raw) in enumerate(samples):
        edit_dict: dict[str, Any] = {
            "adjust": {
                "exposure_ev": -0.6 if idx % 2 == 0 else 0.75,
                "wb": {"warm": 18, "tint": -12},
                "contrast": 30 if idx % 2 == 0 else -22,
                "highlight": -35 if idx % 3 == 0 else 20,
                "shadow": 40 if idx % 4 == 0 else -15,
                "black_point": 15 if idx % 3 == 0 else -10,
                "saturation": 22,
                "vibrance": -18,
                "vignette": 25 if idx % 2 == 0 else -30,
            },
            "curve": curve_a if idx % 2 == 0 else curve_b,
            "geometry": {
                "straighten": 2.5 if idx % 3 == 0 else -1.8,
                "rotate90": idx % 4,
                "flip": (idx % 2 == 1),
            },
        }
        stage = "uncropped" if idx % 5 == 0 else "final"
        preset = "16:9" if stage == "final" and idx % 2 == 0 else None
        crop_rect = (0.05, 0.08, 0.85, 0.8) if stage == "final" and preset is None else None
        opts_dict: dict[str, Any] = {"stage": stage, "from_raw": from_raw}
        if preset:
            opts_dict["crop_preset"] = preset
        if crop_rect:
            opts_dict["crop_rect"] = list(crop_rect)

        adj = parse_adjust(edit_dict["adjust"])
        curves = parse_curve(edit_dict["curve"])
        geom = Geometry(**edit_dict["geometry"])
        edit = EditState(adjust=adj, curve=curves, geometry=geom)
        py_rgb, py_header = finish_unframed(
            rgb,
            edit,
            stage=stage,
            stamp=StampSpec(enabled=False),
            crop_rect=crop_rect,
            taken=None,
            file_time=None,
            from_raw=from_raw,
            crop_preset=preset,
        )
        code, payload, rs_rgb = _run_finish_array(tmp_path, rgb, edit_dict, opts_dict, f"real-{idx}")
        assert code == 0, (name, payload)
        assert payload["header"] == py_header, name
        assert rs_rgb is not None and rs_rgb.shape == py_rgb.shape, name
        assert np.array_equal(rs_rgb, py_rgb), name


# ---- 3. Frame canvas (finish-frame-canvas) -----------------------------------


def test_frame_canvas_all_styles_and_backdrop_errors(tmp_path: Path) -> None:
    require_cli()
    photo = _synth_rgb(160, 120, seed=19)
    in_path = _save_npy(tmp_path / "photo.npy", photo)
    fonts_metrics = FontMetrics()
    pil_fonts = PilFonts()
    pil_photo = Image.fromarray(photo, "RGB")
    meta = {
        "camera": "RICOH GXR",
        "lens_short": "A12 50mm",
        "focal": "33mm",
        "aperture": "f/2.5",
        "shutter": "1/500",
        "iso": "ISO 200",
        "date": "2026.05.01",
    }

    style_specs: list[FrameSpec] = []
    for style in sorted(FRAME_STYLES):
        if style == "none":
            continue
        if style == "film":
            style_specs.append(FrameSpec(style="film", table="light"))
            style_specs.append(FrameSpec(style="film", table="black"))
        else:
            style_specs.append(FrameSpec(style=style))

    for i, spec in enumerate(style_specs):
        layout = frame_layout(160, 120, spec, meta, fonts_metrics)
        assert layout is not None
        layout_path = tmp_path / f"layout-{i}.json"
        out_path = tmp_path / f"canvas-{i}.npy"
        layout_path.write_text(json.dumps(layout.to_json()), encoding="utf-8")
        code, payload = run_json(
            [
                "finish-frame-canvas",
                "--in",
                str(in_path),
                "--layout",
                str(layout_path),
                "--out",
                str(out_path),
            ]
        )
        if (layout.bgfx and layout.bgfx.get("kind") == "blur") or layout.grain:
            err = error_of(code, payload)
            assert err is not None and err.kind == "needs_browser_backdrop", spec.style
            continue

        assert code == 0, (spec.style, payload)
        rs_canvas = np.load(out_path)
        photo_only_layout = FrameLayout(
            width=layout.width,
            height=layout.height,
            photo=layout.photo,
            bg=layout.bg,
            items=tuple(it for it in layout.items if it.get("k") == "photo"),
            bgfx=layout.bgfx,
            unit_box=layout.unit_box,
            fixed=layout.fixed,
            grain=layout.grain,
        )
        py_canvas = np.asarray(draw_frame(pil_photo, photo_only_layout, pil_fonts))
        assert np.array_equal(rs_canvas, py_canvas), (spec.style, spec.table)
        px, py = layout.photo
        assert np.array_equal(rs_canvas[py : py + 120, px : px + 160], photo)

    # Odd canvas sizes with glow on/off and clipped negative photo coordinates
    small_photo = _synth_rgb(13, 11, seed=31)
    small_in = _save_npy(tmp_path / "small-photo.npy", small_photo)
    small_pil = Image.fromarray(small_photo, "RGB")
    for j, extra_layout in enumerate(
        [
            FrameLayout(
                width=37,
                height=29,
                photo=(5, 4),
                bg=(180, 190, 200),
                items=({"k": "photo", "x": 5, "y": 4, "w": 13, "h": 11},),
                bgfx={"kind": "glow", "falloff": 0.15},
                grain=0.0,
            ),
            FrameLayout(
                width=37,
                height=29,
                photo=(-3, -2),
                bg=(40, 50, 60),
                items=({"k": "photo", "x": -3, "y": -2, "w": 13, "h": 11},),
                bgfx=None,
                grain=0.0,
            ),
        ]
    ):
        lp = tmp_path / f"extra-layout-{j}.json"
        op = tmp_path / f"extra-canvas-{j}.npy"
        lp.write_text(json.dumps(extra_layout.to_json()), encoding="utf-8")
        code, payload = run_json(["finish-frame-canvas", "--in", str(small_in), "--layout", str(lp), "--out", str(op)])
        assert code == 0, payload
        assert np.array_equal(np.load(op), np.asarray(draw_frame(small_pil, extra_layout, pil_fonts)))
