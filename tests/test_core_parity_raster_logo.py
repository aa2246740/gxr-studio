"""Parity: gxr-cli (finish-logo-*, finish-key-white) and finish WASM exports against Python."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from gxr_studio.finish.logo_math import LogoInfo, _alpha_luma, aspect_mult, clogo_item, coverage_comp, fit_logo, pick_logo_mode
from gxr_studio.finish.logo_store import WARN_OPAQUE, _stats_of, has_white_background, key_white, trim_ink
from support.core_cli import error_of, require_cli, run_json
from support.lut_oracle import _libm_pow
from support.wasm_check import run_area


def _synth_rgb(w: int, h: int, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    r = ((xx * 255) // max(1, w - 1)).astype(np.uint8)
    g = ((yy * 255) // max(1, h - 1)).astype(np.uint8)
    b = rng.integers(0, 256, size=(h, w), dtype=np.uint8)
    return np.ascontiguousarray(np.stack([r, g, b], axis=-1))


def _save_npy(path: Path, arr: np.ndarray) -> Path:
    np.save(path, arr)
    return path


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


# ---- 5. Logo fit (finish-logo-fit via gxr-frame) ------------------------------


def _logo_fit_cases() -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    return [
        # Size variants
        (
            "small",
            {"id": "u_s", "name": "Small", "w": 150, "h": 100, "coverage": 0.28, "opaque": False, "lum": 180.0},
            {"cap": 24, "max_h": 60, "max_w": 180, "size": "small", "x": 12, "y": 18, "mode": "auto", "ink": [10, 20, 30], "bg_lum": 255.0},
        ),
        (
            "medium",
            {"id": "u_m", "name": "Med", "w": 150, "h": 100, "coverage": 0.28, "opaque": False, "lum": 180.0},
            {"cap": 24, "max_h": 60, "max_w": 180, "size": "medium", "x": 12, "y": 18, "mode": "auto", "ink": [10, 20, 30], "bg_lum": 255.0},
        ),
        (
            "large",
            {"id": "u_l", "name": "Large", "w": 150, "h": 100, "coverage": 0.28, "opaque": False, "lum": 180.0},
            {"cap": 24, "max_h": 80, "max_w": 240, "size": "large", "x": 0, "y": 0, "mode": "auto", "ink": [255, 255, 255], "bg_lum": 10.0},
        ),
        (
            "unknown_size_default",
            {"id": "u_u", "name": "Unk", "w": 150, "h": 100, "coverage": 0.28, "opaque": False, "lum": 180.0},
            {"cap": 24, "max_h": 60, "max_w": 180, "size": "xl", "x": 4, "y": 5, "mode": "auto", "ink": [0, 0, 0], "bg_lum": 200.0},
        ),
        # Aspect ratio breakpoints (<=0.5, 1.0, 1.5, 2.5, 4.0, 6.0, 9.0, >9.0) & zero edges
        (
            "aspect_tall",
            {"id": "u_t", "name": "Tall", "w": 1, "h": 200, "coverage": 0.005, "opaque": False, "lum": 50.0},
            {"cap": 20, "max_h": 100, "max_w": 120, "size": "medium", "x": 2, "y": 3, "mode": "auto", "ink": [0, 0, 0], "bg_lum": 255.0},
        ),
        (
            "aspect_sq",
            {"id": "u_sq", "name": "Sq", "w": 100, "h": 100, "coverage": 0.35, "opaque": False, "lum": 120.0},
            {"cap": 22, "max_h": 90, "max_w": 90, "size": "medium", "x": 5, "y": 5, "mode": "auto", "ink": [15, 15, 15], "bg_lum": 255.0},
        ),
        (
            "aspect_2_5",
            {"id": "u_25", "name": "2.5", "w": 250, "h": 100, "coverage": 0.12, "opaque": False, "lum": 90.0},
            {"cap": 24, "max_h": 90, "max_w": 300, "size": "medium", "x": 8, "y": 9, "mode": "auto", "ink": [20, 30, 40], "bg_lum": 120.0},
        ),
        (
            "aspect_4_0",
            {"id": "u_40", "name": "4.0", "w": 400, "h": 100, "coverage": 0.95, "opaque": False, "lum": 210.0},
            {"cap": 24, "max_h": 90, "max_w": 300, "size": "medium", "x": 8, "y": 9, "mode": "auto", "ink": [20, 30, 40], "bg_lum": 240.0},
        ),
        (
            "aspect_6_0",
            {"id": "u_60", "name": "6.0", "w": 600, "h": 100, "coverage": 0.5, "opaque": False, "lum": 10.0},
            {"cap": 30, "max_h": 90, "max_w": 500, "size": "large", "x": 1, "y": 2, "mode": "auto", "ink": [250, 250, 250], "bg_lum": 15.0},
        ),
        (
            "aspect_wide",
            {"id": "u_99", "name": "Wide", "w": 1000, "h": 50, "coverage": 0.02, "opaque": False, "lum": 180.0},
            {"cap": 24, "max_h": 80, "max_w": 600, "size": "medium", "x": 3, "y": 4, "mode": "mono", "ink": [1, 2, 3], "bg_lum": 10.0},
        ),
        # _fit_inside when tight max_w forces iw/ih shrink below min 8
        (
            "fit_inside_tight",
            {"id": "u_ti", "name": "Tight", "w": 900, "h": 100, "coverage": 0.05, "opaque": False, "lum": 100.0},
            {"cap": 40, "max_h": 100, "max_w": 20, "size": "large", "x": 0, "y": 0, "mode": "orig", "ink": [0, 0, 0], "bg_lum": 120.0},
        ),
        # Zero width / zero max_w degenerate guard
        (
            "zero_w",
            {"id": "u_z", "name": "Z", "w": 0, "h": 10, "coverage": 0.0, "opaque": True, "lum": 0.0},
            {"cap": 20, "max_h": 50, "max_w": 50, "size": "medium", "x": 0, "y": 0, "mode": "auto", "ink": [0, 0, 0], "bg_lum": 20.0},
        ),
        (
            "zero_max_w",
            {"id": "u_zmw", "name": "ZMW", "w": 100, "h": 100, "coverage": 0.5, "opaque": False, "lum": 120.0},
            {"cap": 20, "max_h": 50, "max_w": 0, "size": "medium", "x": 0, "y": 0, "mode": "auto", "ink": [0, 0, 0], "bg_lum": 255.0},
        ),
        # Light/dark threshold (|lum - bg_lum| = 69.9 -> mono, 70.0 -> orig) & opaque override
        (
            "threshold_mono",
            {"id": "u_tm", "name": "TM", "w": 200, "h": 100, "coverage": 0.3, "opaque": False, "lum": 185.1},
            {"cap": 24, "max_h": 50, "max_w": 200, "size": "medium", "x": 7, "y": 8, "mode": "auto", "ink": [22, 22, 26], "bg_lum": 255.0},
        ),
        (
            "threshold_orig",
            {"id": "u_to", "name": "TO", "w": 200, "h": 100, "coverage": 0.3, "opaque": False, "lum": 185.0},
            {"cap": 24, "max_h": 50, "max_w": 200, "size": "medium", "x": 7, "y": 8, "mode": "auto", "ink": [22, 22, 26], "bg_lum": 255.0},
        ),
        (
            "opaque_stays_orig",
            {"id": "u_op", "name": "Opq", "w": 200, "h": 100, "coverage": 0.8, "opaque": True, "lum": 255.0},
            {"cap": 24, "max_h": 50, "max_w": 200, "size": "medium", "x": 7, "y": 8, "mode": "mono", "ink": [22, 22, 26], "bg_lum": 255.0},
        ),
    ]


def _coverage_comp_oracle(cov: float, *, use_libm: bool) -> float:
    base = np.array([0.35 / max(cov, 0.02)], dtype=np.float64)
    return float(min(1.25, max(0.90, float(_libm_pow(base, 0.25, use_libm=use_libm)[0]))))


def _expected_logo_fit(info_dict: dict[str, Any], req_dict: dict[str, Any], *, use_libm: bool) -> dict[str, Any]:
    logo = LogoInfo(
        id=str(info_dict["id"]),
        name=str(info_dict.get("name", "")),
        w=int(info_dict["w"]),
        h=int(info_dict["h"]),
        coverage=float(info_dict["coverage"]),
        opaque=bool(info_dict["opaque"]),
        lum=float(info_dict["lum"]),
    )
    fw, fh = fit_logo(logo, int(req_dict["cap"]), int(req_dict["max_h"]), int(req_dict["max_w"]), str(req_dict.get("size", "medium")))
    mode = pick_logo_mode(logo, str(req_dict.get("mode", "auto")), float(req_dict.get("bg_lum", 255.0)))
    ink_raw = req_dict.get("ink", [0, 0, 0])
    ink = (int(ink_raw[0]), int(ink_raw[1]), int(ink_raw[2]))
    item = clogo_item(
        logo,
        int(req_dict.get("x", 0)),
        int(req_dict.get("y", 0)),
        fw,
        fh,
        str(req_dict.get("mode", "auto")),
        ink,
        float(req_dict.get("bg_lum", 255.0)),
    )
    return {
        "w": fw,
        "h": fh,
        "mode": mode,
        "aspect_mult": aspect_mult(logo.aspect),
        "coverage_comp": _coverage_comp_oracle(logo.coverage, use_libm=use_libm),
        "item": item,
    }


def test_logo_fit_matches_python() -> None:
    require_cli()
    for tag, info_dict, req_dict in _logo_fit_cases():
        code, payload = run_json(
            [
                "finish-logo-fit",
                "--info-json",
                json.dumps(info_dict),
                "--request-json",
                json.dumps(req_dict),
            ]
        )
        assert code == 0, (tag, payload)
        expected = _expected_logo_fit(info_dict, req_dict, use_libm=True)
        assert payload == expected, tag
        assert int(np.float64(payload["coverage_comp"]).view(np.uint64)) == int(np.float64(expected["coverage_comp"]).view(np.uint64)), tag


# PORT-WAVE-1 Amendment 2 Gate B points from 512-case coverage_comp scan (1 f64 ULP between
# pinned libm::pow and Apple host pow; integers fw/fh, mode, aspect_mult, and item remain exact).
_COVERAGE_COMP_GATE_B_POINTS: list[tuple[dict[str, Any], dict[str, Any], float, int, float, int]] = [
    (
        {"id": "synthetic", "name": "", "w": 1663, "h": 4954, "coverage": 0.182089, "opaque": False, "lum": 14.065},
        {"cap": 187, "max_h": 1635, "max_w": 1189, "size": "large"},
        1.1774597856207203,
        0x3FF2D6E0127989E4,
        1.1774597856207205,
        0x3FF2D6E0127989E5,
    ),
    (
        {"id": "synthetic", "name": "", "w": 3957, "h": 3233, "coverage": 0.296059, "opaque": False, "lum": 52.802},
        {"cap": 10, "max_h": 3544, "max_w": 642, "size": "small"},
        1.0427313816871413,
        0x3FF0AF0719EDBF24,
        1.042731381687141,
        0x3FF0AF0719EDBF23,
    ),
    (
        {"id": "synthetic", "name": "", "w": 1723, "h": 400, "coverage": 0.193075, "opaque": False, "lum": 212.988},
        {"cap": 116, "max_h": 643, "max_w": 2156, "size": "large"},
        1.1603406316740768,
        0x3FF290C15694298A,
        1.1603406316740765,
        0x3FF290C156942989,
    ),
    (
        {"id": "synthetic", "name": "", "w": 4399, "h": 2130, "coverage": 0.523735, "opaque": True, "lum": 163.737},
        {"cap": 144, "max_h": 1954, "max_w": 3353, "size": "medium"},
        0.9041471374384993,
        0x3FECEEC5FA42407C,
        0.9041471374384992,
        0x3FECEEC5FA42407B,
    ),
    (
        {"id": "synthetic", "name": "", "w": 4342, "h": 4143, "coverage": 0.230441, "opaque": False, "lum": 107.405},
        {"cap": 1, "max_h": 4295, "max_w": 2158, "size": "large"},
        1.1101382671049382,
        0x3FF1C32057F40FA8,
        1.110138267104938,
        0x3FF1C32057F40FA7,
    ),
    (
        {"id": "synthetic", "name": "", "w": 425, "h": 2356, "coverage": 0.525875, "opaque": False, "lum": 44.372},
        {"cap": 95, "max_h": 439, "max_w": 1717, "size": "medium"},
        0.9032258944738045,
        0x3FECE739FDB54A72,
        0.9032258944738044,
        0x3FECE739FDB54A71,
    ),
    (
        {"id": "synthetic", "name": "", "w": 971, "h": 1396, "coverage": 0.258338, "opaque": True, "lum": 165.143},
        {"cap": 177, "max_h": 1498, "max_w": 2869, "size": "small"},
        1.0788720393592026,
        0x3FF1430F53D9DD98,
        1.0788720393592024,
        0x3FF1430F53D9DD97,
    ),
    (
        {"id": "synthetic", "name": "", "w": 844, "h": 2480, "coverage": 0.49153, "opaque": True, "lum": 86.013},
        {"cap": 192, "max_h": 2798, "max_w": 2240, "size": "small"},
        0.9186064778708194,
        0x3FED6539698B2A54,
        0.9186064778708193,
        0x3FED6539698B2A53,
    ),
    (
        {"id": "synthetic", "name": "", "w": 980, "h": 1222, "coverage": 0.416654, "opaque": True, "lum": 229.777},
        {"cap": 111, "max_h": 733, "max_w": 3668, "size": "medium"},
        0.957355247720989,
        0x3FEEA2A778F3B336,
        0.9573552477209889,
        0x3FEEA2A778F3B335,
    ),
    (
        {"id": "synthetic", "name": "", "w": 4091, "h": 3078, "coverage": 0.146365, "opaque": False, "lum": 95.343},
        {"cap": 21, "max_h": 3982, "max_w": 883, "size": "large"},
        1.24353410828355,
        0x3FF3E58405689D2A,
        1.2435341082835503,
        0x3FF3E58405689D2B,
    ),
    (
        {"id": "synthetic", "name": "", "w": 321, "h": 521, "coverage": 0.246543, "opaque": False, "lum": 188.332},
        {"cap": 177, "max_h": 413, "max_w": 4549, "size": "large"},
        1.0915505228817857,
        0x3FF176FDAE5B57F0,
        1.091550522881786,
        0x3FF176FDAE5B57F1,
    ),
    (
        {"id": "synthetic", "name": "", "w": 1236, "h": 1525, "coverage": 0.152249, "opaque": False, "lum": 218.502},
        {"cap": 138, "max_h": 1920, "max_w": 3981, "size": "medium"},
        1.2313411657537605,
        0x3FF3B392CB52183C,
        1.2313411657537607,
        0x3FF3B392CB52183D,
    ),
    (
        {"id": "synthetic", "name": "", "w": 333, "h": 4059, "coverage": 0.407699, "opaque": True, "lum": 189.451},
        {"cap": 60, "max_h": 2392, "max_w": 3873, "size": "small"},
        0.9625695067342706,
        0x3FEECD5E90F19E0E,
        0.9625695067342704,
        0x3FEECD5E90F19E0D,
    ),
    (
        {"id": "synthetic", "name": "", "w": 593, "h": 2426, "coverage": 0.215431, "opaque": False, "lum": 113.078},
        {"cap": 30, "max_h": 4238, "max_w": 670, "size": "large"},
        1.1289896530883454,
        0x3FF21057745896DA,
        1.1289896530883456,
        0x3FF21057745896DB,
    ),
    (
        {"id": "synthetic", "name": "", "w": 613, "h": 2417, "coverage": 0.410368, "opaque": True, "lum": 191.794},
        {"cap": 125, "max_h": 1508, "max_w": 3819, "size": "large"},
        0.9610005565209934,
        0x3FEEC0843D364142,
        0.9610005565209933,
        0x3FEEC0843D364141,
    ),
    (
        {"id": "synthetic", "name": "", "w": 2413, "h": 1807, "coverage": 0.500379, "opaque": True, "lum": 44.73},
        {"cap": 122, "max_h": 1920, "max_w": 3246, "size": "small"},
        0.9145179673129717,
        0x3FED43BB2F26D5E6,
        0.9145179673129716,
        0x3FED43BB2F26D5E5,
    ),
]


def test_logo_fit_coverage_comp_gate_a_and_listed_gate_b_points() -> None:
    require_cli()
    for idx, (info_dict, req_dict, rs_val, rs_bits, py_val, py_bits) in enumerate(_COVERAGE_COMP_GATE_B_POINTS):
        code, payload = run_json(["finish-logo-fit", "--info-json", json.dumps(info_dict), "--request-json", json.dumps(req_dict)])
        assert code == 0, (idx, payload)
        expected_libm = _expected_logo_fit(info_dict, req_dict, use_libm=True)
        expected_host = _expected_logo_fit(info_dict, req_dict, use_libm=False)
        # Non-pow fields remain identical to host Python across all 16 points.
        for key in ("w", "h", "mode", "aspect_mult", "item"):
            assert payload[key] == expected_host[key], (idx, key, payload[key], expected_host[key])
        # Gate A: bit-exact match against pinned libm oracle and recorded Rust float64 bits.
        got_bits = int(np.float64(payload["coverage_comp"]).view(np.uint64))
        assert got_bits == int(np.float64(expected_libm["coverage_comp"]).view(np.uint64)) == rs_bits, idx
        assert payload["coverage_comp"] == rs_val, idx
        # Negative control: host pow oracle fails bit-exact match and reproduces real Python.
        host_bits = int(np.float64(coverage_comp(float(info_dict["coverage"]))).view(np.uint64))
        assert host_bits == int(np.float64(expected_host["coverage_comp"]).view(np.uint64)) == py_bits, idx
        assert coverage_comp(float(info_dict["coverage"])) == py_val, idx
        assert got_bits != host_bits and abs(got_bits - host_bits) == 1, idx


def test_logo_fit_invalid_inputs() -> None:
    require_cli()
    valid_info = {"id": "u_1", "name": "L", "w": 100, "h": 50, "coverage": 0.3, "opaque": False, "lum": 120.0}
    valid_req = {"cap": 24, "max_h": 40, "max_w": 120}
    bad_cases: list[tuple[str, str, str]] = [
        ("missing_id", json.dumps({"w": 10, "h": 10, "coverage": 0.3, "opaque": False, "lum": 100.0}), json.dumps(valid_req)),
        ("neg_w", json.dumps({**valid_info, "w": -1}), json.dumps(valid_req)),
        ("non_bool_opaque", json.dumps({**valid_info, "opaque": 1}), json.dumps(valid_req)),
        ("nan_lum", '{"id":"u_1","w":10,"h":10,"coverage":0.3,"opaque":false,"lum":NaN}', json.dumps(valid_req)),
        ("missing_cap", json.dumps(valid_info), json.dumps({"max_h": 40, "max_w": 120})),
        ("bad_ink_len", json.dumps(valid_info), json.dumps({**valid_req, "ink": [1, 2]})),
        ("bad_ink_type", json.dumps(valid_info), json.dumps({**valid_req, "ink": [1, "x", 3]})),
    ]
    for tag, info_s, req_s in bad_cases:
        code, payload = run_json(["finish-logo-fit", "--info-json", info_s, "--request-json", req_s])
        err = error_of(code, payload)
        assert err is not None and err.kind == "bad_request", (tag, code, payload)


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

    # Normal finish_array cases (uncropped, final, preset, from_raw=True/False, contrast + highlight + shadow + curves)
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
            "edit": {
                "adjust": {
                    "exposure_ev": -0.75,
                    "wb": {"warm": 25, "tint": -15},
                    "contrast": 30,
                    "highlight": -40,
                    "shadow": 35,
                    "black_point": 20,
                },
                "curve": {"master": [[0, 8], [128, 148], [255, 250]], "r": [[0, 0], [128, 140], [255, 255]]},
            },
            "options": {"stage": "uncropped", "from_raw": False},
        },
    )
    # NaN & bad adjust/curve in edit_json
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
    add_case(
        "arr_bad_curve",
        {
            "kind": "array",
            "rgbNpy": str(small_path),
            "w": 4,
            "h": 3,
            "edit": {"curve": {"master": [[0, 0], [128, 100], [128, 200]]}},
            "options": {"from_raw": False},
        },
    )
    add_case(
        "arr_wb_false",
        {"kind": "array", "rgbNpy": str(small_path), "w": 4, "h": 3, "edit": {"adjust": {"wb": False}}, "options": {"from_raw": False}},
    )
    add_case(
        "arr_wb_zero_list",
        {"kind": "array", "rgbNpy": str(small_path), "w": 4, "h": 3, "edit": {"adjust": {"wb": []}}, "options": {"from_raw": False}},
    )
    add_case(
        "arr_wb_true_err",
        {"kind": "array", "rgbNpy": str(small_path), "w": 4, "h": 3, "edit": {"adjust": {"wb": True}}, "options": {"from_raw": False}},
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

    # Logo fit parity (valid cases + error cases)
    for tag, info_dict, req_dict in _logo_fit_cases():
        add_case(f"logo_fit_{tag}", {"kind": "logo_fit", "info": info_dict, "request": req_dict})
    add_case("logo_fit_err_missing", {"kind": "logo_fit", "info": {"w": 10, "h": 10}, "request": {"cap": 20}})
    add_case(
        "logo_fit_err_ink",
        {
            "kind": "logo_fit",
            "info": {"id": "u_1", "w": 10, "h": 10, "coverage": 0.3, "opaque": False, "lum": 100.0},
            "request": {"cap": 20, "max_h": 40, "max_w": 100, "ink": [1, 2]},
        },
    )

    # Zero and huge caller dimensions (UNIT-COMMON / raster zero-dim parity)
    logo_2x2 = _save_npy(tmp_path / "logo-dim-check.npy", np.zeros((2, 2, 4), dtype=np.uint8))
    for w, h in [(0, 1), (1, 0), (4_294_967_295, 4_294_967_295), (2_147_483_648, 1)]:
        add_case(
            f"arr_dim_{w}x{h}",
            {"kind": "array", "rgbNpy": str(small_path), "w": w, "h": h, "overrideDims": True, "edit": {}, "options": {}},
        )
        add_case(
            f"canvas_dim_{w}x{h}",
            {
                "kind": "frame_canvas",
                "rgbNpy": str(small_path),
                "w": w,
                "h": h,
                "overrideDims": True,
                "layout": {"width": 10, "height": 10, "bg": [0, 0, 0], "items": []},
            },
        )
        add_case(f"logo_dim_{w}x{h}", {"kind": "logo_key", "rgbaNpy": str(logo_2x2), "w": w, "h": h, "overrideDims": True})

    # JSON nesting cap: 128, 129, 20000 for both arrays and objects
    for depth in (128, 129, 20_000):
        arr_j = "[" * depth + "0" + "]" * depth
        obj_j = '{"a":' * depth + "0" + "}" * depth
        add_case(f"arr_deep_a_{depth}", {"kind": "array", "rgbNpy": str(small_path), "w": 4, "h": 3, "edit": arr_j, "options": {}})
        add_case(f"arr_deep_o_{depth}", {"kind": "array", "rgbNpy": str(small_path), "w": 4, "h": 3, "edit": obj_j, "options": {}})
        add_case(f"fit_deep_{depth}", {"kind": "logo_fit", "info": obj_j, "request": "{}"})

    run_area("finish", inputs, tmp_path)
