"""Parity: gxr-wasm ink_ops and finish_logo_prepare against gxr-cli (Item 8)."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from support.core_cli import error_of, run_json
from support.export_pipeline_cases import check
from support.wasm_check import run_area

HOSTILE_DIMS: list[float] = [2**32 + 1, -1, 2**53, math.nan, math.inf, 1.5, 0, 2_147_483_648]


def _num_token(v: float) -> Any:
    return v if math.isfinite(v) else ("NaN" if math.isnan(v) else "Infinity")


def _deep_json(depth: int, kind: str) -> str:
    if kind == "array":
        return "[" * depth + "]" * depth
    return '{"a":' * depth + "1" + "}" * depth


def _write_case(tmp_path: Path, name: str, spec: dict[str, Any]) -> Path:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


def _synth_rgb(w: int, h: int, seed: int = 17) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)


def _synth_logo(kind: str, w: int = 96, h: int = 80) -> np.ndarray:
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    if kind == "white_bg":
        arr[:, :] = [252, 253, 254, 255]
        arr[10 : h - 10, 12 : w - 12] = [20, 30, 40, 255]
    elif kind == "colored_opaque":
        arr[:, :] = [35, 90, 185, 255]
        arr[12 : h - 12, 12 : w - 12] = [240, 220, 60, 255]
    elif kind == "alpha":
        arr[8 : h - 8, 8 : w - 8] = [210, 190, 170, 200]
    elif kind == "small":
        arr[4:30, 4:30] = [30, 30, 30, 255]
    return arr


def test_wasm_matches_native_ink_ops_and_logo_prepare(tmp_path: Path) -> None:
    photo = _synth_rgb(160, 120, seed=101)
    photo_npy = tmp_path / "photo.npy"
    np.save(photo_npy, photo)

    canvas_rgb = _synth_rgb(140, 100, seed=202)
    canvas_rgb_npy = tmp_path / "canvas_rgb.npy"
    np.save(canvas_rgb_npy, canvas_rgb)

    canvas_rgba = np.full((100, 140, 4), [40, 50, 60, 220], dtype=np.uint8)
    canvas_rgba_npy = tmp_path / "canvas_rgba.npy"
    np.save(canvas_rgba_npy, canvas_rgba)

    logo_alpha = _synth_logo("alpha", w=84, h=72)
    logo_alpha_npy = tmp_path / "logo_alpha.npy"
    np.save(logo_alpha_npy, logo_alpha)

    logo_white = _synth_logo("white_bg", w=160, h=120)
    logo_white_npy = tmp_path / "logo_white.npy"
    np.save(logo_white_npy, logo_white)

    logo_colored = _synth_logo("colored_opaque", w=140, h=110)
    logo_colored_npy = tmp_path / "logo_colored.npy"
    np.save(logo_colored_npy, logo_colored)

    logo_big = _synth_logo("white_bg", w=1500, h=960)
    logo_big_npy = tmp_path / "logo_big.npy"
    np.save(logo_big_npy, logo_big)

    logo_small = _synth_logo("small", w=48, h=48)
    logo_small_npy = tmp_path / "logo_small.npy"
    np.save(logo_small_npy, logo_small)

    cases: list[Path] = []

    # 1. Backdrop blur + paper_grain + frame shapes + hole + shadow + carrier + clogo
    ops_full = [
        {"op": "blurred_background", "w": 180, "h": 150, "sigma": 6.0, "sat": 1.1, "veil": 0.85},
        {"op": "paper_grain", "amount": 0.35},
        {"op": "save_reg", "save_as": "base"},
        {
            "op": "paint_shadow",
            "x": 20,
            "y": 20,
            "w": 120,
            "h": 90,
            "blur": 4.0,
            "dx": 2,
            "dy": 3,
            "radius": 8.0,
            "alpha": 0.35,
            "color": [0, 0, 0],
        },
        {
            "op": "paint_rect",
            "x": 18,
            "y": 18,
            "w": 124,
            "h": 94,
            "radius": 8.0,
            "fill": [248, 246, 240],
            "outline": [30, 30, 32],
            "stroke_w": 1.5,
            "alpha": 0.95,
        },
        {"op": "paint_hole", "x": 24, "y": 24, "w": 80, "h": 60, "radius": 4.0, "base_reg": "base"},
        {"op": "paint_poly", "pts": [[10.0, 10.0], [32.0, 14.0], [22.0, 34.0]], "fill": [200, 60, 40], "alpha": 0.8},
        {"op": "paint_ellipse", "cx": 150.0, "cy": 125.0, "r": 9.0, "fill": [250, 220, 60], "alpha": 0.9},
        {
            "op": "paint_carrier",
            "x": 28,
            "y": 28,
            "w": 72,
            "h": 52,
            "c": 5,
            "jit": 3,
            "seed": 19790324,
            "fill": [16, 16, 18],
            "paper": [244, 240, 230],
        },
        {
            "op": "paint_clogo",
            "id": "u_alpha",
            "x": 100,
            "y": 105,
            "w": 64,
            "h": 40,
            "mode": "auto",
            "ink": [22, 22, 26],
            "auto": True,
            "auto_ink": [[245, 245, 245], [18, 20, 24]],
        },
    ]
    cases.append(
        _write_case(
            tmp_path,
            "ink-full-pipeline",
            {
                "kind": "ink_ops",
                "canvasNpy": str(photo_npy),
                "w": 160,
                "h": 120,
                "mode": "RGB",
                "expectMode": "RGB",
                "ops": ops_full,
                "logos": [{"id": "u_alpha", "rgbaNpy": str(logo_alpha_npy), "w": 84, "h": 72}],
            },
        )
    )

    # 2. ImageDraw primitives + GaussianBlur + background glow
    ops_draw = [
        {"op": "background", "w": 140, "h": 100, "bg": [32, 34, 38], "kind": "glow", "falloff": 0.05},
        {"op": "draw_rectangle", "box": [10.0, 10.0, 130.0, 90.0], "fill": [240, 238, 232], "outline": [20, 20, 20], "width": 2},
        {
            "op": "draw_rounded_rectangle",
            "box": [18.0, 18.0, 122.0, 82.0],
            "radius": 10.0,
            "fill": [220, 215, 205],
            "outline": [40, 40, 45],
            "width": 2,
            "corners": [True, False, True, True],
        },
        {"op": "draw_ellipse", "box": [40.0, 25.0, 100.0, 75.0], "fill": [180, 70, 50]},
        {"op": "draw_polygon", "pts": [[20.0, 75.0], [50.0, 25.0], [80.0, 75.0]], "fill": [40, 90, 150]},
        {"op": "draw_line", "pts": [[12.0, 12.0], [128.0, 88.0]], "fill": [250, 250, 250], "width": 3},
        {"op": "gaussian_blur", "radius": 1.75},
    ]
    cases.append(
        _write_case(
            tmp_path,
            "ink-draw-blur-glow",
            {"kind": "ink_ops", "canvasNpy": str(canvas_rgb_npy), "w": 140, "h": 100, "mode": "RGB", "expectMode": "RGB", "ops": ops_draw},
        )
    )

    # 3. RGBA plumbing + alpha_composite + paste + point_mul + crop_f64 + rotate90
    ops_plumbing = [
        {"op": "save_reg", "save_as": "orig"},
        {"op": "new", "mode": "RGBA", "w": 140, "h": 100, "color": [10, 20, 30, 180], "save_as": "layer"},
        {"op": "draw_rectangle", "target": "layer", "box": [10.0, 10.0, 80.0, 60.0], "fill": [220, 140, 60], "width": 1},
        {"op": "point_mul", "target": "layer", "scale": 0.85},
        {"op": "alpha_composite_reg", "reg": "layer"},
        {"op": "get_alpha", "target": "layer", "save_as": "mask"},
        {"op": "paste_color", "color": [250, 240, 230], "box": [5, 5], "mask_reg": "mask"},
        {"op": "crop", "box": [4.4, 6.5, 130.6, 94.5]},
        {"op": "rotate90"},
    ]
    cases.append(
        _write_case(
            tmp_path,
            "ink-plumbing",
            {
                "kind": "ink_ops",
                "canvasNpy": str(canvas_rgba_npy),
                "w": 140,
                "h": 100,
                "mode": "RGBA",
                "expectMode": "RGBA",
                "ops": ops_plumbing,
            },
        )
    )

    # 4. finish_logo_prepare success & error cases
    for label, npy, dims, key in (
        ("prep-white", logo_white_npy, (160, 120), {"key_mode": "auto", "tol": 30, "id": "u_w", "name": "White"}),
        ("prep-colored", logo_colored_npy, (140, 110), {"key_mode": "auto", "tol": 30, "id": "u_c", "name": "Colored"}),
        ("prep-alpha", logo_alpha_npy, (84, 72), {"key_mode": True, "tol": 20, "id": "u_a", "name": "Alpha"}),
        ("prep-big", logo_big_npy, (1500, 960), {"key_mode": "auto", "tol": 30, "id": "u_b", "name": "Big"}),
        ("prep-small-err", logo_small_npy, (48, 48), {"key_mode": "auto", "tol": 30, "id": "u_s", "name": "Small"}),
    ):
        cases.append(
            _write_case(
                tmp_path,
                label,
                {"kind": "logo_prepare", "rgbaNpy": str(npy), "w": dims[0], "h": dims[1], "key": key},
            )
        )

    # 5. Hostile f64 dims on both ink_ops and finish_logo_prepare
    for i, dim in enumerate(HOSTILE_DIMS):
        cases.append(
            _write_case(
                tmp_path,
                f"ink-hostile-w-{i}",
                {
                    "kind": "ink_ops",
                    "canvasNpy": str(canvas_rgb_npy),
                    "w": _num_token(dim),
                    "h": 100,
                    "mode": "RGB",
                    "ops": [],
                    "overrideDims": True,
                },
            )
        )
        cases.append(
            _write_case(
                tmp_path,
                f"prep-hostile-h-{i}",
                {"kind": "logo_prepare", "rgbaNpy": str(logo_white_npy), "w": 160, "h": _num_token(dim), "key": {}, "overrideDims": True},
            )
        )

    # 6. JSON depth limit: 128 accepted by parser (fails semantic check as bad_request), 129 rejected as TooDeep ("json")
    for depth in (128, 129):
        cases.append(
            _write_case(
                tmp_path,
                f"ink-ops-depth-{depth}",
                {"kind": "ink_ops", "canvasNpy": str(canvas_rgb_npy), "w": 140, "h": 100, "mode": "RGB", "ops": _deep_json(depth, "array")},
            )
        )
        cases.append(
            _write_case(
                tmp_path,
                f"prep-key-depth-{depth}",
                {"kind": "logo_prepare", "rgbaNpy": str(logo_white_npy), "w": 160, "h": 120, "key": _deep_json(depth, "object")},
            )
        )

    # Verify CLI distinguishes depth 128 (bad_request) vs 129 (json: JSON 嵌套太深) directly too.
    dummy_out = tmp_path / "depth_out.npy"
    c128, p128 = run_json(["ink-ops", "--canvas", str(canvas_rgb_npy), "--ops-json", _deep_json(128, "array"), "--out", str(dummy_out)])
    c129, p129 = run_json(["ink-ops", "--canvas", str(canvas_rgb_npy), "--ops-json", _deep_json(129, "array"), "--out", str(dummy_out)])
    e128 = error_of(c128, p128)
    e129 = error_of(c129, p129)
    check(e128 is not None and e128.kind == "bad_request", "depth 128 accepted by parser")
    check(e129 is not None and e129.kind == "json", "depth 129 rejected with json TooDeep")

    run_area("ink", cases, tmp_path)
