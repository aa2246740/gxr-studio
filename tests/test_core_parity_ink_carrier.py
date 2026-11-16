"""Parity: gxr-cli ink-carrier and paint_carrier against frame_texture.py (Item 6)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from gxr_studio.finish import frame_texture as tex
from gxr_studio.finish.fonts import FontMetrics
from gxr_studio.finish.layout import frame_layout
from gxr_studio.finish.spec import OUTPUT_RATIOS, FrameSpec
from support.core_cli import error_of, run_json, run_npy
from support.export_pipeline_cases import anon_ids, check, same_pixels
from support.ink_gates import (
    carrier_contours_oracle,
    check_gate_a_arrays,
    gate_b_carrier_report,
    libm_exp_vec,
    libm_sin_vec,
    report_gate_b,
)

CARRIER_CONTOUR_CASES: list[tuple[int, int, int, int, int]] = [
    (16, 24, 3, 2, 19790324),
    (37, 53, 5, 3, 42),
    (120, 80, 8, 4, 0),
    (80, 120, 8, 4, 1),
    (240, 240, 12, 6, 999),
    (320, 240, 10, 5, 19790324),
    (4288, 2848, 48, 24, 19790324),
]

CARRIER_PAINT_CASES: list[tuple[int, int, int, int, int, int, int, int, int]] = [
    (96, 84, 16, 14, 60, 48, 6, 3, 19790324),
    (128, 160, 18, 22, 80, 110, 8, 4, 42),
    (110, 110, -6, 12, 70, 70, 10, 5, 77),
    (150, 120, 14, -8, 130, 100, 12, 6, 0),
    (420, 520, 47, 130, 320, 240, 10, 5, 19790324),
]


def _synth_rgb(w: int, h: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)


@pytest.mark.parametrize(
    ("w", "h", "c", "jit", "seed"),
    CARRIER_CONTOUR_CASES,
    ids=anon_ids(len(CARRIER_CONTOUR_CASES)),
)
def test_carrier_contours_gate_a_and_negative_control(w: int, h: int, c: int, jit: int, seed: int) -> None:
    code, payload = run_json(
        ["ink-carrier", "--w", str(w), "--h", str(h), "--c", str(c), "--jit", str(jit), "--seed", str(seed)],
    )
    check(code == 0, "ink-carrier json exit 0")
    rs_outer = np.asarray(payload["outer"], dtype=np.float64)
    rs_inner = np.asarray(payload["inner"], dtype=np.float64)

    or_outer_l, or_inner_l = carrier_contours_oracle(w, h, c, jit, seed, use_libm=True, perturb_ulp=0)
    or_outer = np.asarray(or_outer_l, dtype=np.float64)
    or_inner = np.asarray(or_inner_l, dtype=np.float64)
    check_gate_a_arrays(or_outer, rs_outer, "carrier outer Gate A", rel_tol=1e-12)
    check_gate_a_arrays(or_inner, rs_inner, "carrier inner Gate A", rel_tol=1e-12)

    # Negative control: 1e-10 perturbation and non-finite NaN/Inf values must trip Gate A.
    bad_outer_l, _ = carrier_contours_oracle(w, h, c, jit, seed, use_libm=True, inject=1e-10)
    bad_outer = np.asarray(bad_outer_l, dtype=np.float64)
    with pytest.raises(AssertionError):
        check_gate_a_arrays(bad_outer, rs_outer, "carrier negative control", rel_tol=1e-12)
    nan_outer = rs_outer.copy()
    nan_outer[0, 0] = np.nan
    with pytest.raises(AssertionError):
        check_gate_a_arrays(nan_outer, nan_outer.copy(), "carrier nan negative control", rel_tol=1e-12)
    inf_outer = rs_outer.copy()
    inf_outer[0, 0] = np.inf
    with pytest.raises(AssertionError):
        check_gate_a_arrays(or_outer, inf_outer, "carrier inf negative control", rel_tol=1e-12)
    sin_ok = libm_sin_vec(np.array([0.75, 2.3], dtype=np.float64), use_libm=True, perturb_ulp=0)
    sin_bad = libm_sin_vec(np.array([0.75, 2.3], dtype=np.float64), use_libm=True, perturb_ulp=1)
    exp_ok = libm_exp_vec(np.array([-0.25, -1.5], dtype=np.float64), use_libm=True, perturb_ulp=0)
    exp_bad = libm_exp_vec(np.array([-0.25, -1.5], dtype=np.float64), use_libm=True, perturb_ulp=1)
    check(bool(np.all(sin_ok != sin_bad)) and bool(np.all(exp_ok != exp_bad)), "carrier sin/exp negative control perturbations distinct")


@pytest.mark.parametrize(
    ("cw", "ch", "x", "y", "w", "h", "c", "jit", "seed"),
    CARRIER_PAINT_CASES,
    ids=anon_ids(len(CARRIER_PAINT_CASES)),
)
def test_carrier_gate_b_contours_and_paint_pixels(
    cw: int,
    ch: int,
    x: int,
    y: int,
    w: int,
    h: int,
    c: int,
    jit: int,
    seed: int,
    tmp_path: Path,
) -> None:
    tex.carrier_contours.cache_clear()
    py_outer, py_inner = tex.carrier_contours(w, h, c, jit, seed)
    fill = (14, 14, 15)
    paper = (246, 242, 233)
    base_arr = _synth_rgb(cw, ch, 500 + w + h)

    py_im = Image.fromarray(base_arr.copy(), "RGB")
    tex.paint_carrier(
        py_im,
        {"k": "carrier", "x": x, "y": y, "w": w, "h": h, "c": c, "jit": jit, "seed": seed, "fill": fill, "paper": paper},
    )
    py_pixels = np.asarray(py_im, dtype=np.uint8)

    in_npy = tmp_path / "canvas.npy"
    out_npy = tmp_path / "out_carrier.npy"
    np.save(in_npy, base_arr)
    code, payload = run_json(
        [
            "ink-carrier",
            "--w",
            str(w),
            "--h",
            str(h),
            "--c",
            str(c),
            "--jit",
            str(jit),
            "--seed",
            str(seed),
            "--x",
            str(x),
            "--y",
            str(y),
            "--fill",
            f"{fill[0]},{fill[1]},{fill[2]}",
            "--paper",
            f"{paper[0]},{paper[1]},{paper[2]}",
            "--in",
            str(in_npy),
            "--out",
            str(out_npy),
        ],
    )
    check(code == 0, "ink-carrier paint exit 0")
    rs_pixels = np.load(out_npy)

    pt_diffs, px_diffs = gate_b_carrier_report(
        py_outer,
        py_inner,
        payload["outer"],
        payload["inner"],
        py_pixels,
        rs_pixels,
        "carrier",
        w=w,
        h=h,
        c=c,
        jit=jit,
        seed=seed,
    )
    check(pt_diffs >= 0 and px_diffs == 0, "carrier Gate B contours attributed and pixels exact")

    # Verify ink-ops paint_carrier op produces identical canvas bytes.
    ops = [{"op": "paint_carrier", "x": x, "y": y, "w": w, "h": h, "c": c, "jit": jit, "seed": seed, "fill": list(fill), "paper": list(paper)}]
    ops_p = tmp_path / "carrier_ops.json"
    ops_out = tmp_path / "ops_out.npy"
    ops_p.write_text(json.dumps(ops), encoding="utf-8")
    got_ops = run_npy(["ink-ops", "--canvas", str(in_npy), "--mode", "RGB", "--ops", str(ops_p)], ops_out)
    same_pixels(got_ops, rs_pixels, "paint_carrier via ink-ops matches ink-carrier")


def test_carrier_darkroom_layouts_across_ratios_and_4288x2848(tmp_path: Path) -> None:
    metrics = FontMetrics()
    total_pt_diffs = 0
    for w, h in ((320, 240), (240, 320), (300, 300), (4288, 2848)):
        for ratio in ("original", *sorted(OUTPUT_RATIOS)):
            layout = frame_layout(w, h, FrameSpec(style="darkroom", color="black", output_ratio=ratio), {}, metrics)
            if layout is None:
                continue
            carriers = [it for it in layout.items if it.get("k") == "carrier"]
            if not carriers:
                continue
            item: dict[str, Any] = carriers[0]
            tex.carrier_contours.cache_clear()
            py_outer, py_inner = tex.carrier_contours(item["w"], item["h"], item["c"], item["jit"], item["seed"])
            code, payload = run_json(
                [
                    "ink-carrier",
                    "--w",
                    str(item["w"]),
                    "--h",
                    str(item["h"]),
                    "--c",
                    str(item["c"]),
                    "--jit",
                    str(item["jit"]),
                    "--seed",
                    str(item["seed"]),
                ],
            )
            check(code == 0, "ink-carrier layout contours ok")
            pt_diffs = sum(
                1
                for p_list, r_list in ((py_outer, payload["outer"]), (py_inner, payload["inner"]))
                for pp, rp in zip(p_list, r_list, strict=True)
                if pp[0] != rp[0] or pp[1] != rp[1]
            )
            total_pt_diffs += pt_diffs
            if w <= 320:
                base_arr = _synth_rgb(layout.width, layout.height, 800 + w + h)
                py_im = Image.fromarray(base_arr.copy(), "RGB")
                tex.paint_carrier(py_im, item)
                in_npy = tmp_path / "lay.npy"
                out_npy = tmp_path / "lay_out.npy"
                np.save(in_npy, base_arr)
                fill = item["fill"]
                paper = item["paper"]
                got = run_npy(
                    [
                        "ink-carrier",
                        "--w",
                        str(item["w"]),
                        "--h",
                        str(item["h"]),
                        "--c",
                        str(item["c"]),
                        "--jit",
                        str(item["jit"]),
                        "--seed",
                        str(item["seed"]),
                        "--x",
                        str(item["x"]),
                        "--y",
                        str(item["y"]),
                        "--fill",
                        f"{fill[0]},{fill[1]},{fill[2]}",
                        "--paper",
                        f"{paper[0]},{paper[1]},{paper[2]}",
                        "--in",
                        str(in_npy),
                    ],
                    out_npy,
                )
                same_pixels(got, np.asarray(py_im, dtype=np.uint8), "darkroom layout carrier pixels")
    report_gate_b("carrier-darkroom-sweep-contours", total_pt_diffs)


def test_carrier_rejects_nonpositive_dimensions() -> None:
    for w, h in ((0, 10), (10, 0), (-5, 10), (10, -5)):
        code, payload = run_json(["ink-carrier", "--w", str(w), "--h", str(h), "--c", "5", "--jit", "3"])
        err = error_of(code, payload)
        check(err is not None and err.kind == "value_error", "nonpositive carrier dims rejected")
