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
from gxr_studio.finish.logo_math import _alpha_luma
from gxr_studio.finish.logo_store import WARN_OPAQUE, _stats_of, has_white_background, key_white, trim_ink
from gxr_studio.finish.pixels import _linear_to_srgb, _srgb_to_linear, apply_base, apply_extra
from gxr_studio.finish.spec import FRAME_STYLES, FrameSpec, Geometry, StampSpec
from gxr_studio.raw.dng import RawDecodeError, open_dng
from gxr_studio.raw.render import render_half
from gxr_studio.render.params import EditState, parse_adjust
from support.core_cli import error_of, require_cli, run_json
from support.wasm_check import run_area

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


def test_apply_base_tone_or_curve_reports_missing_gxr_color_export(tmp_path: Path) -> None:
    """gxr_color::apply_table is not exported in gxr-color/src/lib.rs (out of our folder)."""
    require_cli()
    rgb = _synth_rgb(8, 6)
    blocked_cases: list[tuple[str, dict[str, Any]]] = [
        ("contrast", {"adjust": {"contrast": 30}}),
        ("curve", {"curve": {"master": [[0, 0], [128, 150], [255, 255]]}}),
    ]
    for tag, edit in blocked_cases:
        code, payload, _ = _run_finish_array(
            tmp_path,
            rgb,
            edit,
            {"stage": "uncropped", "from_raw": False},
            f"blocked-{tag}",
        )
        err = error_of(code, payload)
        assert err is not None and err.kind == "unsupported"
        assert "apply_table" in err.message


# ---- 2. Unframed finish (finish-array) ---------------------------------------


def test_finish_array_synthetic_branches_match_python(tmp_path: Path) -> None:
    require_cli()
    rgb = _synth_rgb(120, 80, seed=5)
    cases: list[tuple[dict[str, Any], dict[str, Any]]] = [
        # Pitfall 6: from_raw=True ignores exposure_ev/warm/tint while from_raw=False applies them
        ({"adjust": {"exposure_ev": 2.0, "wb": {"warm": 40, "tint": -20}, "saturation": 30}}, {"stage": "final", "from_raw": True}),
        ({"adjust": {"exposure_ev": 2.0, "wb": {"warm": 40, "tint": -20}, "saturation": 30}}, {"stage": "final", "from_raw": False}),
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
        geom_raw = edit_dict.get("geometry")
        geom = Geometry(**geom_raw) if geom_raw else Geometry()
        edit = EditState(adjust=adj, geometry=geom)
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
    for idx, (name, rgb, from_raw) in enumerate(samples):
        edit_dict: dict[str, Any] = {
            "adjust": {
                "exposure_ev": -0.6 if idx % 2 == 0 else 0.75,
                "wb": {"warm": 18, "tint": -12},
                "black_point": 15 if idx % 3 == 0 else -10,
                "saturation": 22,
                "vibrance": -18,
                "vignette": 25 if idx % 2 == 0 else -30,
            },
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
        geom = Geometry(**edit_dict["geometry"])
        edit = EditState(adjust=adj, geometry=geom)
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


# ---- 4. Logo keying (finish-logo-key & finish-key-white) ---------------------


def _logo_cases() -> list[tuple[str, np.ndarray]]:
    # 1. White background with dark ink square in center (exercises int16 wrap too!)
    white_bg = np.full((12, 16, 4), [252, 253, 255, 255], dtype=np.uint8)
    white_bg[3:9, 4:12] = [20, 40, 60, 255]
    # 2. Light-gray soft anti-aliased edge inside white background (white_frac <= 128)
    white_bg[2, 4:12] = [180, 180, 180, 255]
    # 3. Coloured background (opaque)
    colored_bg = np.full((10, 10, 4), [20, 80, 200, 255], dtype=np.uint8)
    colored_bg[2:8, 2:8] = [240, 220, 40, 255]
    # 4. Alpha logo (semi-transparent + surrounding transparent margin)
    alpha_logo = np.zeros((10, 12, 4), dtype=np.uint8)
    alpha_logo[2:7, 3:9] = [230, 120, 40, 200]
    alpha_logo[4, 5] = [255, 255, 255, 255]
    # 5. 1x1 white & 1x1 dark & 1x4
    one_white = np.array([[[255, 255, 255, 255]]], dtype=np.uint8)
    one_dark = np.array([[[15, 25, 35, 255]]], dtype=np.uint8)
    strip = np.array([[[255, 255, 255, 255], [160, 160, 160, 255], [250, 250, 250, 255], [255, 255, 255, 255]]], dtype=np.uint8)
    # 6. All-transparent input
    empty = np.zeros((6, 8, 4), dtype=np.uint8)
    # 7. Border-touching white islands + isolated interior white hole (several labels)
    multi = np.full((12, 12, 4), [255, 255, 255, 255], dtype=np.uint8)
    multi[2:10, 2:10] = [140, 140, 140, 255]  # border of ink ring (white_frac = 115 -> no int16 wrap)
    multi[4:8, 4:8] = [255, 255, 255, 255]  # interior white hole NOT touching border -> kept opaque!
    return [
        ("white_bg", white_bg),
        ("colored_bg", colored_bg),
        ("alpha_logo", alpha_logo),
        ("one_white", one_white),
        ("one_dark", one_dark),
        ("strip_1x4", strip),
        ("empty_alpha", empty),
        ("multi_label", multi),
    ]


def _pairwise_f32(a: list[np.float32]) -> np.float32:
    n = len(a)
    if n < 8:
        res = np.float32(-0.0)
        for v in a:
            res = np.float32(res + v)
        return res
    if n <= 128:
        r = [a[i] for i in range(8)]
        i = 8
        while i < n - n % 8:
            for j in range(8):
                r[j] = np.float32(r[j] + a[i + j])
            i += 8
        res = np.float32(
            np.float32(np.float32(r[0] + r[1]) + np.float32(r[2] + r[3])) + np.float32(np.float32(r[4] + r[5]) + np.float32(r[6] + r[7]))
        )
        while i < n:
            res = np.float32(res + a[i])
            i += 1
        return res
    n2 = (n // 2) - ((n // 2) % 8)
    return np.float32(_pairwise_f32(a[:n2]) + _pairwise_f32(a[n2:]))


def _seq_alpha_luma(rgba: np.ndarray) -> np.float32:
    alphas: list[np.float32] = []
    weighted: list[np.float32] = []
    w0, w1, w2 = np.float32(0.299), np.float32(0.587), np.float32(0.114)
    inv255 = np.float32(255.0)
    for r, g, b, a in rgba.reshape(-1, 4):
        af = np.float32(np.float32(a) / inv255)
        luma = np.float32(np.float32(np.float32(r) * w0 + np.float32(g) * w1) + np.float32(b) * w2)
        alphas.append(af)
        weighted.append(np.float32(luma * af))
    alpha_sum = np.float32(np.float32(0.0) + _pairwise_f32(alphas))
    weighted_luma = np.float32(np.float32(0.0) + _pairwise_f32(weighted))
    denom = alpha_sum if float(alpha_sum) > 1e-6 else np.float32(1e-6)
    return np.float32(weighted_luma / denom)


def test_logo_key_and_stats_match_python(tmp_path: Path) -> None:
    require_cli()
    for name, rgba in _logo_cases():
        in_path = _save_npy(tmp_path / f"{name}-in.npy", rgba)
        out_path = tmp_path / f"{name}-out.npy"
        code, payload = run_json(["finish-logo-key", "--in", str(in_path), "--id", "u_test", "--name", "Test", "--out", str(out_path)])
        assert code == 0, (name, payload)
        rs_rgba = np.load(out_path)

        pil_in = Image.fromarray(rgba, "RGBA")
        py_hw = has_white_background(pil_in)
        orig_opaque = int(rgba[:, :, 3].min()) >= 250
        use_key = py_hw and orig_opaque
        working = key_white(pil_in, 30) if use_key else pil_in
        py_trimmed_img, py_has_ink = trim_ink(working)
        py_trimmed = np.asarray(py_trimmed_img)
        py_cov, py_opaque, py_lum = _stats_of(py_trimmed_img)
        _, py_alpha_luma = _alpha_luma(py_trimmed_img)
        oracle_alpha_luma = _seq_alpha_luma(py_trimmed)

        assert payload["has_white_background"] == py_hw, name
        assert payload["keyed"] == use_key, name
        assert payload["has_ink"] == py_has_ink, name
        assert payload["coverage"] == py_cov, name
        assert payload["opaque"] == py_opaque, name
        assert payload["lum"] == py_lum, name
        # Gate A: exact float32 bits against sequential no-BLAS pairwise-sum oracle
        rs_al_f32 = np.float32(payload["alpha_luma"])
        assert int(rs_al_f32.view(np.uint32)) == int(oracle_alpha_luma.view(np.uint32)), (
            name,
            float(rs_al_f32),
            float(oracle_alpha_luma),
        )
        # Gate B: real Python differs only on BLAS `@` FMA (one_dark: 1 f32 ULP)
        py_al_f32 = np.float32(py_alpha_luma)
        ulp_diff = abs(int(rs_al_f32.view(np.uint32)) - int(py_al_f32.view(np.uint32)))
        if name == "one_dark":
            assert ulp_diff <= 1
        else:
            assert ulp_diff == 0, (name, float(rs_al_f32), py_alpha_luma, ulp_diff)
        assert np.array_equal(rs_rgba, py_trimmed), name
        if orig_opaque and not py_hw and not use_key:
            assert payload["warning"] == WARN_OPAQUE
        else:
            assert payload["warning"] == ""

        # Also check standalone key_white directly on 1x4 and multi_label
        kw_out = tmp_path / f"{name}-kw.npy"
        code_kw, _ = run_json(["finish-key-white", "--in", str(in_path), "--tol", "30", "--out", str(kw_out)])
        assert code_kw == 0
        assert np.array_equal(np.load(kw_out), np.asarray(key_white(pil_in, 30))), name


# ---- 5. Logo fit blocked outside crate ---------------------------------------


def test_logo_fit_reports_missing_gxr_frame_export(tmp_path: Path) -> None:
    require_cli()
    code, payload = run_json(
        [
            "finish-logo-fit",
            "--info-json",
            '{"id":"u_1","name":"L","w":100,"h":50,"coverage":0.3,"opaque":false,"lum":120.0}',
            "--request-json",
            '{"cap":24,"max_h":40,"max_w":120,"size":"medium","x":10,"y":20,"mode":"auto","ink":[0,0,0],"bg_lum":255.0}',
        ]
    )
    err = error_of(code, payload)
    assert err is not None and err.kind == "unsupported"
    assert "fit_logo" in err.message


# ---- 6. WASM vs Native parity & bounds/JSON depth ----------------------------


def test_wasm_matches_native(tmp_path: Path) -> None:
    inputs: list[Path] = []
    rgb = _synth_rgb(40, 30, seed=23)
    rgb_path = _save_npy(tmp_path / "rgb.npy", rgb)
    small_path = _save_npy(tmp_path / "small.npy", _synth_rgb(4, 3, seed=29))

    def add_case(name: str, obj: dict[str, Any]) -> None:
        p = tmp_path / f"case-{len(inputs)}-{name}.json"
        p.write_text(json.dumps(obj), encoding="utf-8")
        inputs.append(p)

    # Normal finish_array cases (uncropped, final, preset, from_raw=True/False)
    add_case(
        "arr_raw",
        {
            "kind": "array",
            "rgbNpy": str(rgb_path),
            "w": 40,
            "h": 30,
            "edit": {"adjust": {"exposure_ev": 1.0, "saturation": 35, "vignette": -40}, "geometry": {"straighten": 3.5, "flip": True}},
            "options": {"stage": "final", "from_raw": True, "crop_preset": "16:9"},
        },
    )
    add_case(
        "arr_jpg_uncrop",
        {
            "kind": "array",
            "rgbNpy": str(rgb_path),
            "w": 40,
            "h": 30,
            "edit": {"adjust": {"exposure_ev": -0.75, "wb": {"warm": 25, "tint": -15}, "black_point": 20}},
            "options": {"stage": "uncropped", "from_raw": False},
        },
    )
    # NaN in edit_json / options_json
    add_case(
        "arr_nan",
        {
            "kind": "array",
            "rgbNpy": str(small_path),
            "w": 4,
            "h": 3,
            "edit": '{"adjust":{"exposure_ev":NaN}}',
            "options": {},
        },
    )

    # Frame canvas (solid, glow, blur -> needs_browser_backdrop)
    add_case(
        "canvas_glow",
        {
            "kind": "frame_canvas",
            "rgbNpy": str(rgb_path),
            "w": 40,
            "h": 30,
            "layout": {
                "width": 60,
                "height": 50,
                "bg": [220, 215, 205],
                "bgfx": {"kind": "glow", "falloff": 0.07},
                "grain": 0.0,
                "items": [{"k": "photo", "x": 10, "y": 10, "w": 40, "h": 30}],
            },
        },
    )
    add_case(
        "canvas_blur_err",
        {
            "kind": "frame_canvas",
            "rgbNpy": str(small_path),
            "w": 4,
            "h": 3,
            "layout": {
                "width": 10,
                "height": 10,
                "bg": [0, 0, 0],
                "bgfx": {"kind": "blur", "sigma": 90},
                "grain": 0.0,
                "items": [{"k": "photo", "x": 2, "y": 2, "w": 4, "h": 3}],
            },
        },
    )

    # Logo key cases
    for lname, larr in _logo_cases():
        lp = _save_npy(tmp_path / f"logo-{lname}.npy", larr)
        add_case(
            f"logo_{lname}",
            {"kind": "logo_key", "rgbaNpy": str(lp), "w": int(larr.shape[1]), "h": int(larr.shape[0])},
        )

    # Logo fit error parity
    add_case("logo_fit_err", {"kind": "logo_fit", "info": {"w": 10, "h": 10}, "request": {"cap": 20}})

    # Huge caller dimensions (UNIT-COMMON amendments)
    for w, h in [(4_294_967_295, 4_294_967_295), (2_147_483_648, 1)]:
        add_case(
            f"arr_huge_{w}x{h}",
            {"kind": "array", "rgbNpy": str(small_path), "w": w, "h": h, "overrideDims": True, "edit": {}, "options": {}},
        )
        add_case(
            f"canvas_huge_{w}x{h}",
            {
                "kind": "frame_canvas",
                "rgbNpy": str(small_path),
                "w": w,
                "h": h,
                "overrideDims": True,
                "layout": {"width": 10, "height": 10, "bg": [0, 0, 0], "items": []},
            },
        )
        lp = _save_npy(tmp_path / f"logo-huge-{w}.npy", np.zeros((2, 2, 4), dtype=np.uint8))
        add_case(f"logo_huge_{w}x{h}", {"kind": "logo_key", "rgbaNpy": str(lp), "w": w, "h": h, "overrideDims": True})

    # JSON nesting cap: 128, 129, 20000 for both arrays and objects
    for depth in (128, 129, 20_000):
        arr_j = "[" * depth + "0" + "]" * depth
        obj_j = '{"a":' * depth + "0" + "}" * depth
        add_case(f"arr_deep_a_{depth}", {"kind": "array", "rgbNpy": str(small_path), "w": 4, "h": 3, "edit": arr_j, "options": {}})
        add_case(f"arr_deep_o_{depth}", {"kind": "array", "rgbNpy": str(small_path), "w": 4, "h": 3, "edit": obj_j, "options": {}})
        add_case(f"fit_deep_{depth}", {"kind": "logo_fit", "info": obj_j, "request": "{}"})

    run_area("finish", inputs, tmp_path)
