"""Parity: gxr-cli ink-ops paint_clogo and ink-logo-prepare against logo_math.py / logo_store.py (Item 7)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from gxr_studio.finish.logo_math import _alpha_luma, paint_clogo
from gxr_studio.finish.logo_store import ERR_TOO_SMALL, LogoError, decode_safe_rgba, prepare_uploaded_logo
from support.core_cli import error_of, run_json, run_npy
from support.export_pipeline_cases import anon_ids, check, same_pixels
from support.ink_gates import check_alpha_luma_gate_a, gate_b_clogo_report, seq_alpha_luma


def _synth_logo_rgba(kind: str, w: int = 96, h: int = 72) -> np.ndarray:
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    if kind == "transparent_dark":
        arr[10 : h - 10, 12 : w - 12] = [24, 30, 38, 220]
        arr[12:20, 20 : w - 20] = [12, 14, 18, 255]
    elif kind == "transparent_mid":
        arr[8 : h - 8, 8 : w - 8] = [190, 185, 175, 200]
        arr[16 : h - 16, 16 : w - 16] = [240, 225, 210, 140]
    elif kind == "transparent_gradient":
        rng = np.random.default_rng(707)
        patch = rng.integers(0, 256, size=(h - 16, w - 16, 4), dtype=np.uint8)
        patch[:, :, 3] = rng.integers(10, 245, size=(h - 16, w - 16), dtype=np.uint8)
        arr[8 : h - 8, 8 : w - 8] = patch
    elif kind == "opaque":
        arr[:, :] = [25, 110, 210, 255]
        arr[10 : h - 10, 10 : w - 10] = [245, 225, 45, 252]
    elif kind == "near_70":
        # Construct logo with luma near 220.0 (220 - 150 = 70.0)
        arr[8 : h - 8, 8 : w - 8] = [220, 220, 220, 200]
    elif kind == "just_below_70":
        arr[8 : h - 8, 8 : w - 8] = [219, 219, 219, 200]
    elif kind == "just_above_70":
        arr[8 : h - 8, 8 : w - 8] = [221, 221, 221, 200]
    elif kind == "white_bg_dark_ink":
        arr[:, :] = [252, 253, 254, 255]
        arr[12 : h - 12, 14 : w - 14] = [18, 22, 28, 255]
        arr[10, 14 : w - 14] = [195, 195, 195, 255]
    elif kind == "colored_bg_opaque":
        arr[:, :] = [30, 85, 180, 255]
        arr[12 : h - 12, 12 : w - 12] = [240, 230, 90, 255]
    elif kind == "too_small":
        arr[:, :] = [255, 255, 255, 255]
        arr[4:30, 4:40] = [20, 20, 20, 255]
    elif kind == "all_transparent":
        arr[:, :] = [120, 120, 120, 0]
    return arr


def _synth_canvas(w: int, h: int, bg_val: int | tuple[int, int, int], mode: str, seed: int = 11) -> np.ndarray:
    if isinstance(bg_val, int):
        if mode == "RGBA":
            arr = np.full((h, w, 4), [bg_val, bg_val, bg_val, 255], dtype=np.uint8)
        else:
            arr = np.full((h, w, 3), [bg_val, bg_val, bg_val], dtype=np.uint8)
        return arr
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)
    base[:, :] = np.clip(base.astype(np.int16) // 3 + np.array(bg_val, dtype=np.int16), 0, 255).astype(np.uint8)
    if mode == "RGBA":
        alpha = np.full((h, w, 1), 235, dtype=np.uint8)
        return np.ascontiguousarray(np.concatenate([base, alpha], axis=2))
    return base


CLOGO_CASES: list[dict[str, Any]] = [
    {
        "logo": "transparent_dark",
        "cw": 160,
        "ch": 120,
        "canvas_mode": "RGB",
        "bg": (60, 60, 60),
        "x": 20,
        "y": 15,
        "w": 64,
        "h": 48,
        "mode": "orig",
        "ink": [20, 20, 25],
        "auto": False,
        "auto_ink": None,
    },
    {
        "logo": "transparent_dark",
        "cw": 160,
        "ch": 120,
        "canvas_mode": "RGB",
        "bg": (60, 60, 60),
        "x": 20,
        "y": 15,
        "w": 64,
        "h": 48,
        "mode": "mono",
        "ink": [240, 180, 40],
        "auto": False,
        "auto_ink": None,
    },
    {
        "logo": "transparent_dark",
        "cw": 160,
        "ch": 120,
        "canvas_mode": "RGBA",
        "bg": (40, 40, 40),
        "x": 24,
        "y": 18,
        "w": 96,
        "h": 72,
        "mode": "mono",
        "ink": [],
        "auto": False,
        "auto_ink": None,
    },
    {
        "logo": "transparent_mid",
        "cw": 180,
        "ch": 140,
        "canvas_mode": "RGB",
        "bg": 210,
        "x": 30,
        "y": 25,
        "w": 120,
        "h": 90,
        "mode": "auto",
        "ink": [10, 10, 10],
        "auto": True,
        "auto_ink": [[245, 245, 245], [16, 18, 22]],
    },
    {
        "logo": "transparent_mid",
        "cw": 180,
        "ch": 140,
        "canvas_mode": "RGB",
        "bg": 40,
        "x": 30,
        "y": 25,
        "w": 72,
        "h": 54,
        "mode": "orig",
        "ink": [10, 10, 10],
        "auto": True,
        "auto_ink": [[245, 245, 245], [16, 18, 22]],
    },
    {
        "logo": "transparent_gradient",
        "cw": 200,
        "ch": 150,
        "canvas_mode": "RGBA",
        "bg": (80, 90, 100),
        "x": 40,
        "y": 30,
        "w": 110,
        "h": 82,
        "mode": "auto",
        "ink": [0, 0, 0],
        "auto": True,
        "auto_ink": [[250, 248, 242], [20, 22, 26]],
    },
    {
        "logo": "opaque",
        "cw": 180,
        "ch": 140,
        "canvas_mode": "RGB",
        "bg": 240,
        "x": 20,
        "y": 20,
        "w": 80,
        "h": 60,
        "mode": "mono",
        "ink": [255, 0, 0],
        "auto": True,
        "auto_ink": [[255, 255, 255], [0, 0, 0]],
    },
    {
        "logo": "transparent_dark",
        "cw": 140,
        "ch": 100,
        "canvas_mode": "RGB",
        "bg": 180,
        "x": -64,
        "y": 10,
        "w": 64,
        "h": 48,
        "mode": "mono",
        "ink": [15, 25, 35],
        "auto": True,
        "auto_ink": [[250, 250, 250], [10, 10, 10]],
    },
    {
        "logo": "transparent_dark",
        "cw": 140,
        "ch": 100,
        "canvas_mode": "RGB",
        "bg": 180,
        "x": -20,
        "y": -10,
        "w": 64,
        "h": 48,
        "mode": "",
        "ink": [15, 25, 35],
        "auto": False,
        "auto_ink": None,
    },
    {
        "logo": "missing_id",
        "cw": 100,
        "ch": 80,
        "canvas_mode": "RGB",
        "bg": 120,
        "x": 10,
        "y": 10,
        "w": 40,
        "h": 30,
        "mode": "mono",
        "ink": [0, 0, 0],
        "auto": False,
        "auto_ink": None,
    },
    {
        "logo": "empty_id",
        "cw": 100,
        "ch": 80,
        "canvas_mode": "RGB",
        "bg": 120,
        "x": 10,
        "y": 10,
        "w": 40,
        "h": 30,
        "mode": "mono",
        "ink": [0, 0, 0],
        "auto": False,
        "auto_ink": None,
    },
]


@pytest.mark.parametrize("case", CLOGO_CASES, ids=anon_ids(len(CLOGO_CASES)))
def test_paint_clogo_parity(case: dict[str, Any], tmp_path: Path) -> None:
    logo_kind = case["logo"]
    logo_arr = _synth_logo_rgba("transparent_dark" if logo_kind in ("missing_id", "empty_id") else logo_kind)
    logo_im = Image.fromarray(logo_arr, "RGBA")
    logo_id = "" if logo_kind == "empty_id" else ("not_present" if logo_kind == "missing_id" else "u_logo")

    canvas_arr = _synth_canvas(case["cw"], case["ch"], case["bg"], case["canvas_mode"])
    py_canvas = Image.fromarray(canvas_arr.copy(), case["canvas_mode"])

    item = {
        "k": "clogo",
        "id": logo_id,
        "x": case["x"],
        "y": case["y"],
        "w": case["w"],
        "h": case["h"],
        "mode": case["mode"],
        "ink": case["ink"],
        "auto": case["auto"],
        "auto_ink": case["auto_ink"],
    }
    paint_clogo(py_canvas, item, {"u_logo": logo_im})
    py_pixels = np.asarray(py_canvas, dtype=np.uint8)

    canvas_npy = tmp_path / "canvas.npy"
    logo_npy = tmp_path / "u_logo.npy"
    out_npy = tmp_path / "out.npy"
    np.save(canvas_npy, canvas_arr)
    np.save(logo_npy, logo_arr)
    op = {"op": "paint_clogo", **item}
    rs_pixels = run_npy(
        ["ink-ops", "--canvas", str(canvas_npy), "--logo", f"u_logo={logo_npy}", "--ops-json", json.dumps([op])],
        out_npy,
    )

    if logo_kind not in ("missing_id", "empty_id"):
        prep_npy = tmp_path / "prep.npy"
        kcode, key_json = run_json(
            ["finish-logo-key", "--in", str(logo_npy), "--key-mode", "false", "--out", str(prep_npy)],
        )
        check(kcode == 0, "logo key metadata ready")
        rs_trimmed = np.load(prep_npy)
        check_alpha_luma_gate_a(float(key_json["alpha_luma"]), rs_trimmed, "clogo source Gate A")
        _, py_lg = _alpha_luma(logo_im)
        rs_lg = float(seq_alpha_luma(logo_arr))
        x0, y0 = max(0, case["x"]), max(0, case["y"])
        x1 = min(case["cw"], case["x"] + case["w"])
        y1 = min(case["ch"], case["y"] + case["h"])
        reg = Image.fromarray(canvas_arr, case["canvas_mode"]).crop((x0, y0, x1, y1)).convert("L")
        bg_l = float(np.asarray(reg, dtype=np.float32).mean()) if reg.width and reg.height else 128.0
        gate_b_clogo_report(
            py_pixels,
            rs_pixels,
            logo_arr,
            py_lg,
            rs_lg,
            bg_l,
            auto=bool(case["auto"]),
            auto_ink=bool(case["auto_ink"]),
            label=f"clogo_{logo_kind}_{case['mode']}",
        )
    else:
        same_pixels(py_pixels, rs_pixels, "clogo noop exact")


THRESHOLD_CASES: list[tuple[str, int, str]] = [
    ("near_70", 149, "bg_149_below_150_diff_71"),
    ("near_70", 150, "bg_150_eq_150_diff_70"),
    ("near_70", 151, "bg_151_above_150_diff_69"),
    ("just_below_70", 150, "lg_219_diff_69_mono"),
    ("just_above_70", 150, "lg_221_diff_71_orig"),
    ("transparent_gradient", 150, "grad_bg_150_threshold"),
]


@pytest.mark.parametrize(("logo_kind", "bg_val", "tag"), THRESHOLD_CASES, ids=anon_ids(len(THRESHOLD_CASES)))
def test_paint_clogo_threshold_boundaries(logo_kind: str, bg_val: int, tag: str, tmp_path: Path) -> None:
    logo_arr = _synth_logo_rgba(logo_kind)
    logo_im = Image.fromarray(logo_arr, "RGBA")
    canvas_arr = _synth_canvas(140, 100, bg_val, "RGB")
    py_canvas = Image.fromarray(canvas_arr.copy(), "RGB")

    item = {
        "k": "clogo",
        "id": "t_logo",
        "x": 16,
        "y": 12,
        "w": 96,
        "h": 72,
        "mode": "auto",
        "ink": [20, 20, 20],
        "auto": True,
        "auto_ink": [[245, 240, 230], [18, 20, 24]],
    }
    paint_clogo(py_canvas, item, {"t_logo": logo_im})
    py_pixels = np.asarray(py_canvas, dtype=np.uint8)

    canvas_npy = tmp_path / "c.npy"
    logo_npy = tmp_path / "l.npy"
    out_npy = tmp_path / "out.npy"
    np.save(canvas_npy, canvas_arr)
    np.save(logo_npy, logo_arr)
    rs_pixels = run_npy(
        [
            "ink-ops",
            "--canvas",
            str(canvas_npy),
            "--logo",
            f"t_logo={logo_npy}",
            "--ops-json",
            json.dumps([{"op": "paint_clogo", **item}]),
        ],
        out_npy,
    )
    _, py_lg = _alpha_luma(logo_im)
    rs_lg = float(seq_alpha_luma(logo_arr))
    gate_b_clogo_report(
        py_pixels,
        rs_pixels,
        logo_arr,
        py_lg,
        rs_lg,
        float(bg_val),
        auto=True,
        auto_ink=True,
        label=f"clogo_thresh_{tag}",
    )


def _to_png_bytes(rgba: np.ndarray, *, mode: str = "RGBA") -> bytes:
    buf = io.BytesIO()
    im = Image.fromarray(rgba, "RGBA")
    if mode == "RGB":
        im = im.convert("RGB")
    im.save(buf, format="PNG")
    return buf.getvalue()


PREPARE_CASES: list[dict[str, Any]] = [
    {"kind": "white_bg_dark_ink", "w": 160, "h": 120, "png_mode": "RGBA", "key_mode": "auto", "tol": 30},
    {"kind": "white_bg_dark_ink", "w": 160, "h": 120, "png_mode": "RGB", "key_mode": True, "tol": -5},
    {"kind": "white_bg_dark_ink", "w": 160, "h": 120, "png_mode": "RGBA", "key_mode": False, "tol": 30},
    {"kind": "white_bg_dark_ink", "w": 160, "h": 120, "png_mode": "RGBA", "key_mode": "yes", "tol": 99},
    {"kind": "colored_bg_opaque", "w": 140, "h": 100, "png_mode": "RGBA", "key_mode": "auto", "tol": 30},
    {"kind": "transparent_dark", "w": 140, "h": 110, "png_mode": "RGBA", "key_mode": True, "tol": 30},
    {"kind": "transparent_gradient", "w": 180, "h": 120, "png_mode": "RGBA", "key_mode": "auto", "tol": 30},
    # STORE_LONG_EDGE > 1200 Lanczos downscale
    {"kind": "white_bg_dark_ink", "w": 1640, "h": 940, "png_mode": "RGBA", "key_mode": "auto", "tol": 30},
    {"kind": "transparent_dark", "w": 920, "h": 1840, "png_mode": "RGBA", "key_mode": "auto", "tol": 30},
    {"kind": "colored_bg_opaque", "w": 1455, "h": 973, "png_mode": "RGB", "key_mode": "false", "tol": 30},
]


@pytest.mark.parametrize("case", PREPARE_CASES, ids=anon_ids(len(PREPARE_CASES)))
def test_finish_logo_prepare_parity(case: dict[str, Any], tmp_path: Path) -> None:
    raw_arr = _synth_logo_rgba(case["kind"], w=case["w"], h=case["h"])
    png_bytes = _to_png_bytes(raw_arr, mode=case["png_mode"])
    decoded_rgba = np.asarray(decode_safe_rgba(png_bytes), dtype=np.uint8)

    stored_png, py_meta = prepare_uploaded_logo(png_bytes, key_mode=case["key_mode"], tol=case["tol"])
    with Image.open(io.BytesIO(stored_png), formats=["PNG"]) as stored:
        stored.load()
        py_stored = np.asarray(stored.convert("RGBA"), dtype=np.uint8)
        _, py_alpha_luma = _alpha_luma(stored.convert("RGBA"))

    in_npy = tmp_path / "in_logo.npy"
    out_npy = tmp_path / "stored_logo.npy"
    np.save(in_npy, decoded_rgba)
    key_opts = {"key_mode": case["key_mode"], "tol": case["tol"], "id": "u_prep", "name": "TestLogo"}
    code, payload = run_json(
        ["ink-logo-prepare", "--in", str(in_npy), "--key-json", json.dumps(key_opts), "--out", str(out_npy)],
    )
    check(code == 0, "ink-logo-prepare exit 0")
    rs_stored = np.load(out_npy)

    same_pixels(py_stored, rs_stored, "finish_logo_prepare RGBA exact")
    check(payload["w"] == py_meta["w"] and payload["h"] == py_meta["h"], "prepared w/h match")
    check(payload["keyed"] == py_meta["keyed"], "prepared keyed match")
    check(payload["tol"] == py_meta["tol"], "prepared tol match")
    check(payload["opaque"] == py_meta["opaque"], "prepared opaque match")
    check(payload["warning"] == py_meta["warning"], "prepared warning match")
    check_alpha_luma_gate_a(float(payload["alpha_luma"]), py_stored, "finish_logo_prepare Gate A")
    check(payload["coverage"] == py_meta["coverage"], "prepared coverage match")
    # Gate B: `lum` rounds float(alpha_luma) to 3 decimals; attribute any BLAS ULP difference cleanly
    ulp_diff = abs(int(np.float32(py_alpha_luma).view(np.uint32)) - int(np.float32(payload["alpha_luma"]).view(np.uint32)))
    check(ulp_diff <= 1, "alpha_luma BLAS ULP bound <= 1")
    check(payload["lum"] == round(float(payload["alpha_luma"]), 3) or payload["lum"] == py_meta["lum"], "prepared lum match")


ERROR_PREPARE_CASES: list[tuple[str, int, int, str]] = [
    ("too_small", 60, 60, "auto"),
    ("all_transparent", 120, 120, "auto"),
    ("colored_bg_opaque", 50, 100, "false"),
]


@pytest.mark.parametrize(("kind", "w", "h", "km"), ERROR_PREPARE_CASES, ids=anon_ids(len(ERROR_PREPARE_CASES)))
def test_finish_logo_prepare_too_small_error_parity(kind: str, w: int, h: int, km: str, tmp_path: Path) -> None:
    raw_arr = _synth_logo_rgba(kind, w=w, h=h)
    png_bytes = _to_png_bytes(raw_arr, mode="RGBA")
    decoded_rgba = np.asarray(decode_safe_rgba(png_bytes), dtype=np.uint8)

    with pytest.raises(LogoError) as py_exc:
        prepare_uploaded_logo(png_bytes, key_mode=km)
    check(py_exc.value.code == "logo_too_small", "python LogoError code logo_too_small")
    check(py_exc.value.message == ERR_TOO_SMALL, "python LogoError message exact")

    in_npy = tmp_path / "err_logo.npy"
    out_npy = tmp_path / "err_out.npy"
    np.save(in_npy, decoded_rgba)
    code, payload = run_json(["ink-logo-prepare", "--in", str(in_npy), "--key-mode", km, "--out", str(out_npy)])
    check(code == 2, "ink-logo-prepare rejects small/empty logo")
    err = error_of(code, payload)
    check(err is not None, "CLI returned CoreCliError")
    check(err is not None and err.kind == "logo_too_small", "CLI error kind matches Python")
    check(err is not None and err.message == ERR_TOO_SMALL, "CLI error message matches Python")
