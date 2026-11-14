"""Parity: gxr-cli (geom-*) and WASM against Python crop, finish.geometry, gpu.box, and spec.parse_geometry."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from gxr_studio import crop as crop_mod
from gxr_studio.crop import CropError, default_crop, validate_crop
from gxr_studio.crop import crop_box as py_crop_box
from gxr_studio.finish.geometry import (
    clamp_norm_rect,
    fit_ratio_in_valid,
    fitted_crop,
    header_value,
    normalized_valid_rect,
    warp,
    warp_valid,
)
from gxr_studio.finish.spec import EditError, Geometry, parse_geometry
from gxr_studio.gpu.box import final_box
from gxr_studio.raw.dng import RawDecodeError, open_dng
from support import samples
from support.core_cli import error_of, require_cli, run_json
from support.geometry_alloc import run_rule2_oom_probe
from support.wasm_check import run_area

_PARENT = samples.sample_parent()
CAPTURES = _PARENT / "captures"
_CAP = 60


def _real_dng_sizes() -> list[tuple[str, int, int]]:
    if not CAPTURES.is_dir():
        return []
    found = [p for p in CAPTURES.rglob("*") if p.is_file() and p.suffix.upper() == ".DNG" and "backup" not in p.as_posix().lower()]
    found.sort(key=lambda p: p.as_posix())
    if len(found) > _CAP:
        found = [found[(i * len(found)) // _CAP] for i in range(_CAP)]
    sizes: list[tuple[str, int, int]] = []
    for path in found:
        try:
            dng = open_dng(path)
        except RawDecodeError:
            continue
        _y0, _x0, ch, cw = dng.crop_window()
        if dng.orientation in (5, 6, 7, 8):
            cw, ch = ch, cw
        sizes.append((path.relative_to(CAPTURES).as_posix(), cw, ch))
    return sizes


def _assert_close_rect(py_rect: tuple[float, float, float, float], rs_rect: list[float], label: str = "") -> None:
    assert len(rs_rect) == 4, label
    for i, (a, b) in enumerate(zip(py_rect, rs_rect, strict=True)):
        fa = float(a)
        fb = float(b)
        if math.isnan(fa) or math.isnan(fb):
            assert math.isnan(fa) and math.isnan(fb), f"{label}[{i}]: python={a!r} rust={b!r}"
            continue
        if math.isinf(fa) or math.isinf(fb):
            assert fa == fb, f"{label}[{i}]: python={a!r} rust={b!r}"
            continue
        limit = 1e-12 * max(1.0, abs(fa))
        assert abs(fa - fb) <= limit, f"{label}[{i}]: python={a!r} rust={b!r}"


def test_float_rect_gate_enforces_nan_and_inf_rules() -> None:
    _assert_close_rect((math.nan, 0.0, 1.0, 1.0), [math.nan, 0.0, 1.0, 1.0], "both-nan")
    _assert_close_rect((math.inf, 0.0, 1.0, 1.0), [math.inf, 0.0, 1.0, 1.0], "same-pos-inf")
    _assert_close_rect((-math.inf, 0.0, 1.0, 1.0), [-math.inf, 0.0, 1.0, 1.0], "same-neg-inf")
    _assert_close_rect((2.0, 0.0, 1.0, 1.0), [2.0 + 1e-12, 0.0, 1.0, 1.0], "within-bound")

    for py_v, rs_v, tag in (
        (math.nan, 0.0, "python-nan"),
        (0.0, math.nan, "rust-nan"),
        (math.inf, -math.inf, "opposite-inf"),
        (math.inf, 1.0, "python-inf-rust-finite"),
        (1.0, math.inf, "python-finite-rust-inf"),
        (2.0, 2.0 + 1e-11, "over-bound"),
    ):
        with pytest.raises(AssertionError):
            _assert_close_rect((py_v, 0.0, 1.0, 1.0), [rs_v, 0.0, 1.0, 1.0], tag)


def _synth_rgb(width: int, height: int, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:height, 0:width]
    r = ((xx * 255) // max(1, width - 1)).astype(np.uint8)
    g = ((yy * 255) // max(1, height - 1)).astype(np.uint8)
    b = rng.integers(0, 256, size=(height, width), dtype=np.uint8)
    rgb = np.stack([r, g, b], axis=-1)
    # Add high-contrast grid lines so bilinear edges and fill pixels are exercised
    rgb[height // 2 : height // 2 + 2, :] = (250, 10, 240)
    rgb[:, width // 2 : width // 2 + 2] = (15, 245, 30)
    return np.ascontiguousarray(rgb)


# ---- 1. crop.py parity -------------------------------------------------------


@pytest.mark.parametrize("width,height", [(4288, 2848), (2848, 4288), (660, 503), (320, 240)])
@pytest.mark.parametrize("flip", [False, True])
@pytest.mark.parametrize("angle", [0.0, 0.1, -3.3, 10.0, -15.0])
def test_crop_default_and_round_half_up_match_python(width: int, height: int, flip: bool, angle: float) -> None:
    require_cli()
    for preset in crop_mod.PRESETS:
        try:
            py_crop = default_crop(preset, width, height, angle, flip=flip)
            py_err = None
        except CropError as exc:
            py_crop, py_err = None, exc

        crop_arg = json.dumps({"preset": preset, "angle_deg": angle, "flip": flip})
        code, payload = run_json(["geom-crop-box", "--w", str(width), "--h", str(height), "--crop", crop_arg])
        rs_err = error_of(code, payload)

        if py_err is not None:
            assert rs_err is not None and rs_err.kind == py_err.code
            continue
        assert rs_err is None and py_crop is not None
        assert payload["box"] == list(py_crop_box(py_crop.rect, width, height)), (preset, width, height, flip, angle)
        _assert_close_rect(py_crop.rect, payload["crop"]["rect"], f"{preset}@{angle}")


def test_crop_half_integer_ties_use_round_half_up_while_geometry_uses_half_even() -> None:
    """Pitfall 2: crop.crop_box uses round_half_up while geometry.crop_box uses Python round (half-even)."""
    require_cli()
    # width=10, height=10 -> 0.05*10 = 0.5, 0.25*10 = 2.5, 0.45*10 = 4.5
    # Must be >= 16 px for validate_crop, so use width=100, height=100 and *.005 -> 0.5, 2.5, 10.5
    w, h = 100, 100
    for nx, ny in ((0.005, 0.025), (0.105, 0.205)):
        rect = (nx, ny, 0.205, 0.305)
        py_box = py_crop_box(rect, w, h)
        code, payload = run_json(["geom-crop-box", "--w", str(w), "--h", str(h), "--crop", json.dumps({"preset": "free", "rect": list(rect)})])
        assert code == 0
        assert payload["box"] == list(py_box)

        # Compare against final_box which uses finish.geometry.crop_box (half-even)
        py_fbox, _ = final_box(w, h, Geometry(), rect, "free")
        code_f, payload_f = run_json(
            [
                "geom-final-box",
                "--w",
                str(w),
                "--h",
                str(h),
                "--geometry",
                "{}",
                "--crop",
                json.dumps({"preset": "free", "rect": list(rect)}),
            ]
        )
        assert code_f == 0
        assert payload_f["box"] == list(py_fbox)
        assert payload["box"] != payload_f["box"]


def test_crop_mirrored_and_error_kinds_match_python() -> None:
    require_cli()
    w_px, h_px, angle = 4288, 2848, 8.0
    best = default_crop("16:9", w_px, h_px, angle).rect
    bw, bh = best[2] * 0.6, best[3] * 0.6
    mirrored_rects = []
    for right in (0, 1):
        for bottom in (0, 1):
            x, y = 0.0, 0.0
            for step in range(400):
                x = (1 - bw - step * 0.0005) if right else step * 0.0005
                y = (1 - bh - step * 0.0005) if bottom else step * 0.0005
                if crop_mod.box_inside((x * w_px, y * h_px, bw * w_px, bh * h_px), w_px, h_px, -angle, 1e-6):
                    break
            box = (x * w_px, y * h_px, bw * w_px, bh * h_px)
            if not crop_mod.box_inside(box, w_px, h_px, angle, crop_mod.EDGE_TOLERANCE_PX):
                mirrored_rects.append([x, y, bw, bh])
    assert len(mirrored_rects) == 2

    for rect in mirrored_rects:
        ok_crop = validate_crop("16:9", rect, angle, w_px, h_px, mirrored=True)
        code, payload = run_json(
            [
                "geom-crop-box",
                "--w",
                str(w_px),
                "--h",
                str(h_px),
                "--crop",
                json.dumps({"preset": "16:9", "rect": rect, "angle_deg": angle, "mirrored": True}),
            ]
        )
        assert code == 0
        assert payload["box"] == list(py_crop_box(ok_crop.rect, w_px, h_px))

        code_fail, payload_fail = run_json(
            [
                "geom-crop-box",
                "--w",
                str(w_px),
                "--h",
                str(h_px),
                "--crop",
                json.dumps({"preset": "16:9", "rect": rect, "angle_deg": angle, "mirrored": False}),
            ]
        )
        err = error_of(code_fail, payload_fail)
        assert err is not None and err.kind == "crop_outside_image"

    bad_cases: list[tuple[str, Any, Any, str]] = [
        ("xpan-65-24", [0, 0, 1, 1], 0.0, "ratio_mismatch"),
        ("free", [0.5, 0.5, 0.6, 0.6], 0.0, "bad_rect"),
        ("free", [0, 0, 0, 0.5], 0.0, "bad_rect"),
        ("free", [0, 0, 0.001, 0.001], 0.0, "too_small"),
        ("free", [0, 0, 1, 1], 10.5, "crop_outside_image"),
        ("free", [0, 0, 1, 1], 50.0, "bad_angle"),
        ("free", [0, 0, 1, 1], 5.0, "crop_outside_image"),
        ("nope", [0, 0, 1, 1], 0.0, "unknown_preset"),
    ]
    for preset, rect, ang, expected_code in bad_cases:
        with pytest.raises(CropError) as py_exc:
            validate_crop(preset, rect, ang, 4288, 2848)
        assert py_exc.value.code == expected_code
        code, payload = run_json(
            [
                "geom-crop-box",
                "--w",
                "4288",
                "--h",
                "2848",
                "--crop",
                json.dumps({"preset": preset, "rect": rect, "angle_deg": ang}),
            ]
        )
        rs_err = error_of(code, payload)
        assert rs_err is not None and rs_err.kind == expected_code

    # NaN in rect & angle (JSON with NaN literal, matching Python json.loads)
    for raw_json, expected_code in [
        ('{"preset":"free","rect":[0,0,NaN,1],"angle_deg":0.0}', "bad_rect"),
        ('{"preset":"free","rect":[0,0,1,1],"angle_deg":NaN}', "bad_angle"),
    ]:
        code, payload = run_json(["geom-crop-box", "--w", "4288", "--h", "2848", "--crop", raw_json])
        rs_err = error_of(code, payload)
        assert rs_err is not None and rs_err.kind == expected_code


# ---- 2. finish/geometry.py warp + rotate + flip parity -----------------------


WARP_GEOMETRIES: list[dict[str, Any]] = [
    {},
    {"straighten": 0.12345},
    {"straighten": -3.3},
    {"straighten": 12.5},
    {"straighten": -45.0},
    {"vertical": 60},
    {"vertical": -80},
    {"horizontal": 80},
    {"horizontal": -100},
    {"distortion": 70},
    {"distortion": -50},
    {"rotate90": 1},
    {"rotate90": 2},
    {"rotate90": 3},
    {"flip": True},
    {"rotate90": 1, "flip": True, "straighten": -7.5},
    {"straighten": 4.0, "vertical": 40, "horizontal": -30, "distortion": 50, "rotate90": 3, "flip": True},
    {"vertical": 100, "horizontal": -100, "distortion": 100, "straighten": -4.0, "rotate90": 2},
]


@pytest.mark.parametrize("size", [(97, 65), (120, 90), (260, 195)])
@pytest.mark.parametrize("geom_dict", WARP_GEOMETRIES)
def test_warp_is_bit_identical_on_uint8_and_mask(size: tuple[int, int], geom_dict: dict[str, Any], tmp_path: Path) -> None:
    require_cli()
    w, h = size
    rgb = _synth_rgb(w, h, seed=w * 31 + h)
    in_npy = tmp_path / "in.npy"
    out_npy = tmp_path / "out.npy"
    mask_npy = tmp_path / "mask.npy"
    np.save(in_npy, rgb)

    geom = parse_geometry(geom_dict)
    py_rgb, py_mask = warp(rgb, geom)

    code, payload = run_json(
        [
            "geom-warp",
            "--in",
            str(in_npy),
            "--geometry",
            json.dumps(geom_dict),
            "--out",
            str(out_npy),
            "--mask",
            str(mask_npy),
        ]
    )
    assert code == 0, payload
    rs_rgb = np.load(out_npy)
    rs_mask = np.load(mask_npy)

    assert rs_rgb.dtype == np.uint8 and rs_rgb.shape == py_rgb.shape
    assert rs_mask.dtype == np.bool_ and rs_mask.shape == py_mask.shape

    if not np.array_equal(py_mask, rs_mask):
        idx = tuple(int(v) for v in np.argwhere(py_mask != rs_mask)[0])
        pytest.fail(f"mask differs at {idx} for {size} {geom_dict}: python={py_mask[idx]} rust={rs_mask[idx]}")
    if not np.array_equal(py_rgb, rs_rgb):
        idx = tuple(int(v) for v in np.argwhere(py_rgb != rs_rgb)[0])
        pytest.fail(f"rgb differs at {idx} for {size} {geom_dict}: python={int(py_rgb[idx])} rust={int(rs_rgb[idx])}")


def test_bad_geometry_errors_match_python() -> None:
    require_cli()
    bad_geoms = [
        "[]",
        "123",
        '"bad"',
        '{"straighten": 45.1}',
        '{"straighten": NaN}',
        '{"straighten": true}',
        '{"vertical": 101}',
        '{"vertical": 1.5}',
        '{"horizontal": -101}',
        '{"distortion": 100.5}',
        '{"rotate90": 4}',
        '{"rotate90": 1.0}',
        '{"rotate90": true}',
        '{"flip": 1}',
        '{"unknown": 0}',
    ]
    for raw in bad_geoms:
        parsed = json.loads(raw)
        with pytest.raises(EditError) as py_exc:
            parse_geometry(parsed)
        assert py_exc.value.code == "bad_geometry"

        for cmd in (
            ["geom-header", "--geometry", raw],
            ["geom-final-box", "--w", "100", "--h", "80", "--geometry", raw, "--crop", "null"],
        ):
            code, payload = run_json(cmd)
            err = error_of(code, payload)
            assert err is not None and err.kind == "bad_geometry", (raw, cmd, payload)


# ---- 3. header_value, valid_rect, fit_ratio, clamp_norm_rect, fitted_crop ----


@pytest.mark.parametrize(
    "size,geom_dict",
    [
        ((120, 90), {}),
        ((120, 90), {"straighten": 10.0}),
        ((240, 180), {"straighten": 8.0}),
        ((241, 180), {"straighten": 8.0}),
        ((300, 200), {"straighten": 8.0, "vertical": 40}),
        ((4288, 2848), {"straighten": 12.0, "distortion": 50, "rotate90": 1, "flip": True}),
    ],
)
def test_header_value_matches_python(size: tuple[int, int], geom_dict: dict[str, Any]) -> None:
    require_cli()
    w, h = size
    geom = parse_geometry(geom_dict)
    valid = np.ones((h, w), dtype=bool) if geom.is_identity() else warp_valid(w, h, geom)
    py_header = header_value(normalized_valid_rect(valid))
    code, payload = run_json(["geom-header", "--w", str(w), "--h", str(h), "--geometry", json.dumps(geom_dict)])
    assert code == 0
    assert payload["header"] == py_header


def test_fit_ratio_clamp_and_fitted_crop_match_python() -> None:
    require_cli()
    cases = [
        (320, 240, {"straighten": 8.0, "flip": True}, [0.0, 0.0, 0.4, 0.4], 16, 9),
        (320, 240, {"vertical": 60, "straighten": -5.0}, [0.6, 0.6, 0.3, 0.3], 65, 24),
        (4288, 2848, {"straighten": 7.5, "vertical": 40}, [0.0, 0.0, 0.4, 0.4], 3, 2),
    ]
    for w, h, geom_dict, rect, numer, denom in cases:
        geom = parse_geometry(geom_dict)
        valid = warp_valid(w, h, geom)
        py_box = fit_ratio_in_valid(valid, tuple(rect), numer, denom)  # type: ignore[arg-type]
        code, payload = run_json(
            [
                "geom-fit-ratio",
                "--w",
                str(w),
                "--h",
                str(h),
                "--geometry",
                json.dumps(geom_dict),
                "--rect",
                json.dumps(rect),
                "--numer",
                str(numer),
                "--denom",
                str(denom),
            ]
        )
        assert code == 0
        assert payload["box"] == list(py_box)

        py_clamped = clamp_norm_rect(valid, tuple(rect))  # type: ignore[arg-type]
        code_c, payload_c = run_json(
            [
                "geom-clamp-rect",
                "--w",
                str(w),
                "--h",
                str(h),
                "--geometry",
                json.dumps(geom_dict),
                "--rect",
                json.dumps(rect),
            ]
        )
        assert code_c == 0
        _assert_close_rect(py_clamped, payload_c["rect"], "clamp_norm_rect")

    for preset in ("original", "free", "xpan-65-24", "4:5", "16:9"):
        for force in (False, True):
            geom_dict = {"straighten": 6.0, "vertical": 30} if not force else {"rotate90": 1}
            geom = parse_geometry(geom_dict)
            py_fc = fitted_crop(geom, (0.2, 0.2, 0.5, 0.5), preset=preset, width=640, height=480, force=force)
            args = [
                "geom-fitted-crop",
                "--w",
                "640",
                "--h",
                "480",
                "--geometry",
                json.dumps(geom_dict),
                "--crop",
                json.dumps({"preset": preset, "rect": [0.2, 0.2, 0.5, 0.5]}),
            ]
            if force:
                args.append("--force")
            code, payload = run_json(args)
            assert code == 0
            if py_fc is None:
                assert payload["rect"] is None
            else:
                _assert_close_rect(py_fc, payload["rect"], f"fitted_crop({preset},{force})")


# ---- 4. gpu/box.py final_box parity -----------------------------------------


FINAL_BOX_CASES = [
    (4288, 2848, {}, None, None),
    (4288, 2848, {"rotate90": 1, "flip": True}, None, None),
    (4288, 2848, {"straighten": 8.0}, (0.0, 0.0, 1.0, 1.0), "xpan-65-24"),
    (4288, 2848, {"straighten": 8.0, "flip": True}, (0.1, 0.1, 0.6, 0.5), "16:9"),
    (2848, 4288, {"vertical": 30, "distortion": -20}, (0.0, 0.0, 1.0, 1.0), "3:2"),
    (2848, 4288, {"horizontal": -30, "distortion": 30, "rotate90": 1}, None, None),
    (1424, 2144, {"vertical": 100, "horizontal": -100, "distortion": 100, "straighten": -4.0, "rotate90": 2}, (0.0, 0.0, 1.0, 1.0), "3:2"),
    (660, 503, {"straighten": -7.5, "vertical": -60}, (0.2, 0.3, 0.5, 0.4), "free"),
    (660, 503, {"straighten": 3.5}, (0.2, 0.3, 0.5, 0.4), "original"),
    (199, 133, {"straighten": 5.0, "distortion": 40}, (0.1, 0.1, 0.8, 0.8), "4:5"),
]


@pytest.mark.parametrize("w,h,geom_dict,rect,preset", FINAL_BOX_CASES)
def test_final_box_matches_python_exact(
    w: int,
    h: int,
    geom_dict: dict[str, Any],
    rect: tuple[float, float, float, float] | None,
    preset: str | None,
) -> None:
    require_cli()
    geom = parse_geometry(geom_dict)
    py_box, py_valid = final_box(w, h, geom, rect, preset)
    crop_payload = {"rect": list(rect) if rect is not None else None, "preset": preset}
    code, payload = run_json(
        [
            "geom-final-box",
            "--w",
            str(w),
            "--h",
            str(h),
            "--geometry",
            json.dumps(geom_dict),
            "--crop",
            json.dumps(crop_payload),
        ]
    )
    assert code == 0, payload
    assert payload == {
        "box": list(py_box),
        "valid": list(py_valid),
        "header": header_value(py_valid),
    }


def test_final_box_rejects_unrepresentable_crop_coords_and_preserves_clamp() -> None:
    require_cli()
    for rect_json in ('{"rect":[1e18,0,1e18,0.5]}', '{"rect":[0,1e18,0.5,1e18]}', '{"rect":[1e308,0,1e308,0.5]}'):
        for geom_json in ("{}", '{"straighten":5}'):
            code, payload = run_json(["geom-final-box", "--w", "32", "--h", "24", "--geometry", geom_json, "--crop", rect_json])
            err = error_of(code, payload)
            assert err is not None and err.kind == "bad_rect"
            assert err.message == "裁剪框超出画面范围"

    u32_edge_rects: list[tuple[float, float, float, float]] = []
    for px in (2**32 - 1, 2**32, 2**32 + 1):
        u32_edge_rects.append((0.0, 0.0, px / 32.0, 0.5))
        u32_edge_rects.append((0.0, 0.0, 0.5, px / 24.0))
    u32_edge_rects.append((0.0, 0.0, math.nextafter(float(2**63), 0.0) / 32.0, 0.5))

    for geom_dict in ({}, {"straighten": 5.0}):
        geom = parse_geometry(geom_dict)
        for preset in (None, "16:9", "3:2"):
            for rect in ((-0.2, 0.1, 0.5, 0.5), (0.8, 0.8, 0.5, 0.5), (2.0, 0.0, 0.5, 0.5), *u32_edge_rects):
                py_box, py_valid = final_box(32, 24, geom, rect, preset)
                code, payload = run_json(
                    [
                        "geom-final-box",
                        "--w",
                        "32",
                        "--h",
                        "24",
                        "--geometry",
                        json.dumps(geom_dict),
                        "--crop",
                        json.dumps({"rect": list(rect), **({"preset": preset} if preset else {})}),
                    ]
                )
                assert code == 0, (geom_dict, preset, rect, payload)
                assert payload == {"box": list(py_box), "valid": list(py_valid), "header": header_value(py_valid)}


def test_final_box_rejects_oversized_dimensions() -> None:
    require_cli()
    for bad_w, bad_h, geom_dict in (
        (4294967295, 4294967295, {}),
        (2147483648, 1, {}),
        (4294967295, 4294967295, {"straighten": 5.0}),
        (4294967295, 4294967295, {"vertical": 40}),
    ):
        bad_code, bad_payload = run_json(
            [
                "geom-final-box",
                "--w",
                str(bad_w),
                "--h",
                str(bad_h),
                "--geometry",
                json.dumps(geom_dict),
                "--crop",
                "null",
            ]
        )
        bad_err = error_of(bad_code, bad_payload)
        assert bad_err is not None and bad_err.kind == "bad_geometry"
        assert "array is too big" in bad_err.message


def test_final_box_on_real_dng_dimensions() -> None:
    require_cli()
    sizes = _real_dng_sizes()
    if not sizes:
        if os.environ.get("GXR_REQUIRE_CORE") == "1":
            pytest.fail(f"no DNGs under {CAPTURES}")
        samples.missing(f"no DNGs under {CAPTURES}")
    seen: dict[tuple[int, int], str] = {}
    for rel, w, h in sizes:
        seen.setdefault((w, h), rel)
    print(f"real_dngs={len(sizes)} unique_dims={sorted(seen)}")
    for (w, h), rel in seen.items():
        for geom_dict, preset in (
            ({}, None),
            ({"straighten": 6.5, "rotate90": 1}, "xpan-65-24"),
            ({"vertical": 45, "distortion": -25, "flip": True}, "3:2"),
        ):
            geom = parse_geometry(geom_dict)
            rect = (0.0, 0.0, 1.0, 1.0) if preset else None
            py_box, py_valid = final_box(w, h, geom, rect, preset)
            code, payload = run_json(
                [
                    "geom-final-box",
                    "--w",
                    str(w),
                    "--h",
                    str(h),
                    "--geometry",
                    json.dumps(geom_dict),
                    "--crop",
                    json.dumps({"rect": list(rect) if rect else None, "preset": preset}),
                ]
            )
            assert code == 0, (rel, payload)
            assert payload == {
                "box": list(py_box),
                "valid": list(py_valid),
                "header": header_value(py_valid),
            }, rel


# ---- 5. WASM vs native parity ------------------------------------------------


def test_wasm_matches_native_for_geometry(tmp_path: Path) -> None:
    specs: list[Path] = []

    def add_case(
        name: str,
        w: int,
        h: int,
        geometry: Any,
        crop: Any,
        *,
        rgb: np.ndarray | None = None,
        expect_error: bool = False,
        check_final_box: bool = True,
        override_dims: bool = False,
    ) -> None:
        rgb_npy = None
        if rgb is not None:
            rgb_npy = tmp_path / f"{name}.npy"
            np.save(rgb_npy, rgb)
        spec_path = tmp_path / f"{name}.json"
        spec_path.write_text(
            json.dumps(
                {
                    "w": w,
                    "h": h,
                    "geometry": geometry,
                    "crop": crop,
                    "rgbNpy": str(rgb_npy) if rgb_npy else None,
                    "expectError": expect_error,
                    "checkFinalBox": check_final_box,
                    "overrideDims": override_dims,
                }
            ),
            encoding="utf-8",
        )
        specs.append(spec_path)

    img_small = _synth_rgb(97, 65, seed=7)
    img_over240 = _synth_rgb(260, 195, seed=11)

    add_case("identity", 97, 65, {}, None, rgb=img_small)
    add_case("straighten", 97, 65, {"straighten": 7.25}, {"rect": [0.0, 0.0, 1.0, 1.0], "preset": "xpan-65-24"}, rgb=img_small)
    add_case(
        "full-warp",
        97,
        65,
        {"straighten": -4.5, "vertical": 45, "horizontal": -30, "distortion": 60, "rotate90": 1, "flip": True},
        {"rect": [0.1, 0.1, 0.6, 0.6], "preset": "16:9"},
        rgb=img_small,
    )
    add_case(
        "over240",
        260,
        195,
        {"straighten": 10.0, "vertical": -40, "rotate90": 3},
        {"rect": [0.2, 0.2, 0.5, 0.5], "preset": "free"},
        rgb=img_over240,
    )
    add_case(
        "large-box-only", 4288, 2848, {"straighten": 8.0, "vertical": 40, "flip": True}, {"rect": [0.0, 0.0, 1.0, 1.0], "preset": "xpan-65-24"}
    )
    add_case("bad-geom", 97, 65, '{"straighten": 99}', None, rgb=img_small, expect_error=True)
    add_case("bad-geom-nan", 97, 65, '{"straighten": NaN}', None, rgb=img_small, expect_error=True)
    add_case("bad-crop", 97, 65, {}, '{"preset": "nope"}', expect_error=True)
    add_case("huge-4294967295", 4294967295, 4294967295, {}, None, expect_error=True)
    add_case("huge-2147483648x1", 2147483648, 1, {}, None, expect_error=True)
    add_case("huge-expand-4294967295", 4294967295, 4294967295, {"vertical": 40}, None, expect_error=True)
    add_case("mismatch-warp", 97, 64, {}, None, rgb=img_small, expect_error=True, check_final_box=False, override_dims=True)
    add_case("huge-warp-dims", 4294967295, 4294967295, {}, None, rgb=img_small, expect_error=True, override_dims=True)
    add_case("overflow-crop-x", 32, 24, {"straighten": 5}, '{"rect":[1e18,0,1e18,0.5]}', expect_error=True)
    add_case("overflow-crop-y", 32, 24, {"straighten": 5}, '{"rect":[0,1e18,0.5,1e18]}', expect_error=True)
    add_case("overflow-crop-1e308", 32, 24, {}, '{"rect":[1e308,0,1e308,0.5]}', expect_error=True)
    add_case("clamp-negative-crop", 32, 24, {"straighten": 5}, {"rect": [-0.2, 0.1, 0.5, 0.5]})
    for px in (2**32 - 1, 2**32, 2**32 + 1):
        for preset in ("16:9", "3:2"):
            add_case(f"u32-edge-w-{px}-{preset}", 32, 24, {"straighten": 5}, {"rect": [0.0, 0.0, px / 32.0, 0.5], "preset": preset})
            add_case(f"u32-edge-h-{px}-{preset}", 32, 24, {"straighten": 5}, {"rect": [0.0, 0.0, 0.5, px / 24.0], "preset": preset})
    add_case(
        "i64-edge-w-16-9", 32, 24, {"straighten": 5}, {"rect": [0.0, 0.0, math.nextafter(float(2**63), 0.0) / 32.0, 0.5], "preset": "16:9"}
    )

    def nest_arr(depth: int, inner: str = "0") -> str:
        return ("[" * depth) + inner + ("]" * depth)

    def nest_obj(depth: int, inner: str = "0") -> str:
        return ('{"a":' * depth) + inner + ("}" * depth)

    add_case("depth-128-crop-arr", 32, 24, {}, '{"ignored":' + nest_arr(127) + "}")
    add_case("depth-128-crop-obj", 32, 24, {}, '{"ignored":' + nest_obj(127) + "}")
    add_case("depth-128-geom-arr", 32, 24, nest_arr(128), None, rgb=img_small, expect_error=True)
    add_case("depth-128-geom-obj", 32, 24, nest_obj(128), None, rgb=img_small, expect_error=True)
    for depth in (129, 20_000):
        add_case(f"depth-{depth}-geom-arr", 32, 24, nest_arr(depth), None, rgb=img_small, expect_error=True)
        add_case(f"depth-{depth}-geom-obj", 32, 24, nest_obj(depth), None, rgb=img_small, expect_error=True)
        add_case(f"depth-{depth}-crop-arr", 32, 24, {}, nest_arr(depth), expect_error=True)
        add_case(f"depth-{depth}-crop-obj", 32, 24, {}, nest_obj(depth), expect_error=True)

    proc = run_area("geometry", specs, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_rule2_allocations_return_core_error_on_oom_injection(tmp_path: Path) -> None:
    run_rule2_oom_probe(tmp_path)


def test_json_nesting_limit_128_across_all_cli_entry_points(tmp_path: Path) -> None:
    require_cli()

    def arr(d: int, inner: str = "0") -> str:
        return ("[" * d) + inner + ("]" * d)

    def obj(d: int, inner: str = "0") -> str:
        return ('{"a":' * d) + inner + ("}" * d)

    in_npy = tmp_path / "tiny.npy"
    out_npy = tmp_path / "out.npy"
    mask_npy = tmp_path / "mask.npy"
    np.save(in_npy, _synth_rgb(8, 8, seed=1))

    def cmds(raw: str, is_crop_or_rect: bool) -> list[list[str]]:
        if is_crop_or_rect:
            return [
                ["geom-crop-box", "--w", "32", "--h", "24", "--crop", raw],
                ["geom-final-box", "--w", "32", "--h", "24", "--geometry", "{}", "--crop", raw],
                ["geom-fitted-crop", "--w", "32", "--h", "24", "--geometry", "{}", "--crop", raw],
                ["geom-clamp-rect", "--w", "32", "--h", "24", "--geometry", "{}", "--rect", raw],
                ["geom-fit-ratio", "--w", "32", "--h", "24", "--geometry", "{}", "--rect", raw, "--numer", "1", "--denom", "1"],
            ]
        return [
            ["geom-final-box", "--w", "32", "--h", "24", "--geometry", raw, "--crop", "null"],
            ["geom-warp", "--in", str(in_npy), "--geometry", raw, "--out", str(out_npy), "--mask", str(mask_npy)],
            ["geom-warp-valid", "--w", "32", "--h", "24", "--geometry", raw, "--out", str(mask_npy)],
            ["geom-header", "--geometry", raw],
            ["geom-fitted-crop", "--geometry", raw],
            ["geom-clamp-rect", "--w", "32", "--h", "24", "--geometry", raw, "--rect", "[0,0,1,1]"],
            ["geom-fit-ratio", "--w", "32", "--h", "24", "--geometry", raw, "--rect", "[0,0,1,1]", "--numer", "1", "--denom", "1"],
        ]

    for is_crop in (False, True):
        for raw128 in (arr(128, ""), obj(128, "0")):
            for cmd in cmds(raw128, is_crop):
                code, payload = run_json(cmd)
                err = error_of(code, payload)
                assert err is None or err.kind != "json", (cmd, payload)
        for cmd in cmds('{"ignored":' + arr(127, "") + "}", True)[:3]:
            code, payload = run_json(cmd)
            assert code == 0, (cmd, payload)
        for cmd in cmds('{"ignored":' + obj(127, "0") + "}", True)[:3]:
            code, payload = run_json(cmd)
            assert code == 0, (cmd, payload)
        for depth in (129, 20_000):
            for bad in (arr(depth, ""), obj(depth, "0")):
                for cmd in cmds(bad, is_crop):
                    code, payload = run_json(cmd)
                    err = error_of(code, payload)
                    assert err is not None and err.kind == "json" and err.message == "JSON 嵌套太深", (cmd[0], depth, payload)
