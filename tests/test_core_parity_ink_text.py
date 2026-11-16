"""Gate A parity tests for ``gxr-finish::ink_text`` (Items 1-3).

Covers:
- Negative control proving de-hinted fonts stay on FreeType's native TrueType loader.
- Gate A (0 tolerance) across all 5 de-hinted fonts + real DSEG7 for ``px`` 1..=160 and
  layout ``px`` values across 1x1..65535x65535.
- Canvas text drawing parity on ``L``, ``RGB``, and ``RGBA`` modes (including clipping).
- Synthetic outlines (bowtie, sub-pixel, overlap compound, conic, huge overflow).
- Native CLI vs WASM parity including JSON depth cap (> 128) and ``f64`` bounds.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image, ImageDraw

from gxr_studio.finish.fonts import FONT_FILES
from support.core_cli import error_of, run_json, run_npy
from support.export_pipeline_cases import check, same_pixels
from support.ink_text_cases import (
    DRAWABLE_CHARS,
    LARGE_PX_CHARS,
    dehinted_font_paths,
    font_path,
    make_inktext_cli_and_wasm_spec,
    open_pil_face,
    pillow_mask,
    run_rust_batch,
    sample_layout_pxs,
)
from support.ink_text_fonts import make_synthetic_test_font
from support.wasm_check import run_area

HINTED_FONTS = ("atkinson", "inter", "inter_sb", "courier", "patrick")


def test_dehint_negative_control_proves_native_loader(tmp_path: Path) -> None:
    """With ``maxSizeOfInstructions = 0``, FreeType switches to its auto-hinter and output changes.

    Also proves mechanically that unmodified ``DSEG7Classic-Regular.ttf`` (which has empty
    ``fpgm`` and ``prep``) triggers FreeType's auto-hinter in Pillow (``ftobjs.c:1012-1016``).
    """
    native_dehint = dehinted_font_paths(tmp_path, zero_maxp=False)
    autohint_ctrl = dehinted_font_paths(tmp_path, zero_maxp=True)

    changed = 0
    for name in HINTED_FONTS:
        for px in (12, 16, 24, 32, 48):
            f_native = open_pil_face(native_dehint[name], px)
            f_auto = open_pil_face(autohint_ctrl[name], px)
            for ch in "ABeg08":
                if pillow_mask(f_native, ch) != pillow_mask(f_auto, ch):
                    changed += 1
    check(changed >= 20, "expected auto-hinter negative control to differ")

    # DSEG7 proof: unmodified DSEG7 == zero_maxp autohinted DSEG7, while fpgm=b'\x00' stays native.
    real_dseg = font_path("dseg")
    dseg_same_as_auto = 0
    dseg_diff_from_native = 0
    for px in (12, 16, 24, 32, 48):
        f_real = open_pil_face(real_dseg, px)
        f_auto = open_pil_face(autohint_ctrl["dseg"], px)
        f_native = open_pil_face(native_dehint["dseg"], px)
        for ch in "0128:-GXR":
            m_real = pillow_mask(f_real, ch)
            if m_real == pillow_mask(f_auto, ch):
                dseg_same_as_auto += 1
            if m_real != pillow_mask(f_native, ch):
                dseg_diff_from_native += 1
    check(dseg_same_as_auto == 45, "dseg real vs autohint count")
    check(dseg_diff_from_native == 45, "dseg real vs native count")


def _assert_batch_exact(
    _font_name: str,
    path: Path,
    pxs: list[int],
    chars: tuple[str, ...],
    work_dir: Path,
    tag: str,
) -> None:
    rs_items = run_rust_batch(path, pxs, chars, work_dir, tag=tag)
    faces = {px: open_pil_face(path, px) for px in pxs}
    for px, cp, rw, rh, rx, ry, rbuf in rs_items:
        ch = chr(cp)
        pw, ph, px_off, py_off, pbuf = pillow_mask(faces[px], ch)
        check((rw, rh, rx, ry, rbuf) == (pw, ph, px_off, py_off, pbuf), "gate A batch mask mismatch")


def test_gate_a_dehinted_masks_exact(tmp_path: Path) -> None:
    """Gate A: 0-tolerance mask parity for px 1..=160 and sampled layout px up to 5112."""
    dehinted = dehinted_font_paths(tmp_path, zero_maxp=False)
    layout_pxs = sample_layout_pxs()
    max_layout_px = max(max(v) for v in layout_pxs.values() if v)
    check(max_layout_px >= 5112, "max layout px below 5112")

    pxs_1_to_160 = list(range(1, 161))
    for name in FONT_FILES:
        fpath = dehinted[name]
        for idx, start in enumerate(range(0, len(pxs_1_to_160), 40)):
            chunk = pxs_1_to_160[start : start + 40]
            _assert_batch_exact(name, fpath, chunk, DRAWABLE_CHARS, tmp_path, f"{name}-lo-{idx}")

        extra_pxs = sorted(p for p in layout_pxs[name] if p > 160)
        small_extra = [p for p in extra_pxs if p <= 400]
        large_extra = [p for p in extra_pxs if p > 400]
        if small_extra:
            _assert_batch_exact(name, fpath, small_extra, DRAWABLE_CHARS, tmp_path, f"{name}-sm")
        for px in large_extra:
            _assert_batch_exact(name, fpath, [px], LARGE_PX_CHARS, tmp_path, f"{name}-lg-{px}")


@pytest.mark.parametrize("px", [1, 13, 16, 24, 36, 48, 3693])
def test_real_dseg7_autohinted_within_gate_b_bounds(tmp_path: Path, px: int) -> None:
    """Real unmodified DSEG7 is auto-hinted by Pillow (ftobjs.c:1012-1016) and meets Gate B bounds."""
    from support.ink_text_cases import (
        GATE_B_COVERAGE_RATIO_GE_16,
        GATE_B_HORIZ_EDGE_BOUND,
        GATE_B_SMALL_PX_COVERAGE_RATIO,
        GATE_B_SMALL_PX_MIN_SUM,
        GATE_B_VERT_EDGE_BOUNDS,
        compare_masks_common_space,
    )

    dehinted = dehinted_font_paths(tmp_path, zero_maxp=False)
    real_path = font_path("dseg")
    chars = LARGE_PX_CHARS if px > 400 else DRAWABLE_CHARS
    rs_real = run_rust_batch(real_path, [px], chars, tmp_path, tag=f"dseg-real-{px}")
    rs_dh = run_rust_batch(dehinted["dseg"], [px], chars, tmp_path, tag=f"dseg-dh-{px}")
    pil_real_face = open_pil_face(real_path, px)
    pil_dh_face = open_pil_face(dehinted["dseg"], px)

    v_limit = GATE_B_VERT_EDGE_BOUNDS["dseg"]
    r_lo, r_hi = GATE_B_COVERAGE_RATIO_GE_16["dseg"]
    sm_lo, sm_hi = GATE_B_SMALL_PX_COVERAGE_RATIO
    saw_diff = False
    for rs_it, dh_it in zip(rs_real, rs_dh, strict=True):
        _px, cp, rw, rh, rx, ry, rbuf = rs_it
        _dpx, _dcp, dhw, dhh, dhx, dhy, dhbuf = dh_it
        ch = chr(cp)
        pil_real = pillow_mask(pil_real_face, ch)
        rs_mask = (rw, rh, rx, ry, rbuf)
        if rs_mask == pil_real:
            continue
        saw_diff = True
        check((dhw, dhh, dhx, dhy, dhbuf) == pillow_mask(pil_dh_face, ch), "dseg gate B attribution")
        _diff_px, _max_d, box_rs, box_py, rs_sum, py_sum = compare_masks_common_space(rs_mask, pil_real)
        check((box_rs is None) == (box_py is None), "dseg gate B ink presence")
        if box_rs is not None and box_py is not None:
            d_l = box_rs[0] - box_py[0]
            d_t = box_rs[1] - box_py[1]
            d_r = box_rs[2] - box_py[2]
            d_b = box_rs[3] - box_py[3]
            check(max(abs(d_l), abs(d_r)) <= GATE_B_HORIZ_EDGE_BOUND, "dseg gate B horiz edge bound")
            check(max(abs(d_t), abs(d_b)) <= v_limit, "dseg gate B vert edge bound")
        if py_sum > 0:
            ratio = rs_sum / py_sum
            if px >= 16:
                check(r_lo <= ratio <= r_hi, "dseg gate B coverage ratio ge 16")
            elif min(rs_sum, py_sum) >= GATE_B_SMALL_PX_MIN_SUM:
                check(sm_lo <= ratio <= sm_hi, "dseg gate B small px coverage ratio")
    check(saw_diff, "dseg autohint diff expected")


def test_gate_a_draw_text_modes_and_clipping_exact(tmp_path: Path) -> None:
    """Gate A: ``ImageDraw.text((x, y), ch, font, fill)`` across L, RGB, RGBA and clipping."""
    dehinted = dehinted_font_paths(tmp_path, zero_maxp=False)
    fonts_arg = [
        "--font",
        f"atkinson={dehinted['atkinson']}",
        "--font",
        f"inter={dehinted['inter']}",
        "--font",
        f"dseg={dehinted['dseg']}",
    ]

    rng = np.random.default_rng(42)
    cases: list[tuple[str, str, np.ndarray, list[dict[str, Any]]]] = [
        (
            "mode_l",
            "L",
            np.zeros((72, 120), dtype=np.uint8),
            [
                {"op": "char", "font": "atkinson", "px": 28, "ch": "G", "x": 10, "y": 12, "fill": 255},
                {"op": "char", "font": "inter", "px": 19, "ch": "é", "x": -3, "y": -4, "fill": 255},
                {"op": "char", "font": "dseg", "px": 36, "ch": "8", "x": 96, "y": 30, "fill": 200},
            ],
        ),
        (
            "mode_rgb",
            "RGB",
            rng.integers(10, 220, size=(80, 140, 3), dtype=np.uint8),
            [
                {"op": "char", "font": "atkinson", "px": 33, "ch": "R", "x": 12, "y": 8, "fill": [255, 106, 0]},
                {"op": "char", "font": "inter", "px": 25, "ch": "·", "x": 50, "y": 20, "fill": [20, 40, 240]},
                {"op": "char", "font": "dseg", "px": 40, "ch": "0", "x": 110, "y": 42, "fill": [255, 255, 255]},
            ],
        ),
        (
            "mode_rgba_mixed",
            "RGBA",
            np.concatenate(
                [
                    np.full((60, 60, 4), [0, 0, 0, 0], dtype=np.uint8),
                    np.full((60, 60, 4), [30, 60, 90, 100], dtype=np.uint8),
                ],
                axis=1,
            ),
            [
                {"op": "char", "font": "atkinson", "px": 32, "ch": "A", "x": 10, "y": 10, "fill": [255, 140, 0, 255]},
                {"op": "char", "font": "atkinson", "px": 32, "ch": "A", "x": 70, "y": 10, "fill": [255, 140, 0, 180]},
                {"op": "char", "font": "dseg", "px": 24, "ch": ":", "x": 55, "y": 20, "fill": [220, 32, 32, 255]},
            ],
        ),
    ]

    for tag, mode, bg, ops in cases:
        in_npy = tmp_path / f"{tag}-in.npy"
        out_npy = tmp_path / f"{tag}-out.npy"
        ops_json = tmp_path / f"{tag}-ops.json"
        np.save(in_npy, bg)
        ops_json.write_text(json.dumps(ops), encoding="utf-8")

        got = run_npy(
            ["inktext-draw", "--in", str(in_npy), "--mode", mode, "--ops", str(ops_json), *fonts_arg],
            out_npy,
        )

        im = Image.fromarray(bg.copy(), mode)
        draw = ImageDraw.Draw(im)
        for op in ops:
            face = open_pil_face(dehinted[op["font"]], int(op["px"]))
            fill_val = op["fill"] if isinstance(op["fill"], int) else tuple(op["fill"])
            draw.text((int(op["x"]), int(op["y"])), str(op["ch"]), font=face, fill=fill_val)
        want = np.asarray(im)
        same_pixels(got, want, f"inktext draw {tag}")


def test_synthetic_ttf_outlines_match_freetype(tmp_path: Path) -> None:
    """Synthetic TTF exercises bowtie self-intersection, sub-pixel triangle, composite, conic & overflow."""
    syn_ttf = make_synthetic_test_font(tmp_path / "synthetic.ttf")
    for px in (12, 24, 64):
        _assert_batch_exact("synthetic", syn_ttf, [px], ("A", "B", "C", "D"), tmp_path, f"syn-{px}")

    # Huge outline 'E' at px=256 exceeds FreeType bitmap raster limit (width > 10 * x_ppem) on both sides.
    pil_huge = open_pil_face(syn_ttf, 256)
    with pytest.raises(OSError):
        pil_huge.getmask2("E", "L", anchor="la")

    bg = np.zeros((32, 32), dtype=np.uint8)
    in_npy = tmp_path / "huge.npy"
    out_npy = tmp_path / "huge-out.npy"
    ops_json = tmp_path / "huge-ops.json"
    np.save(in_npy, bg)
    ops_json.write_text(
        json.dumps([{"op": "char", "font": "syn", "px": 256, "ch": "E", "x": 0, "y": 0, "fill": 255}]),
        encoding="utf-8",
    )
    code, payload = run_json(
        ["inktext-draw", "--in", str(in_npy), "--mode", "L", "--ops", str(ops_json), "--font", f"syn={syn_ttf}", "--out", str(out_npy)],
    )
    err = error_of(code, payload)
    check(err is not None and err.kind == "bad_geometry", "huge outline bad_geometry error")


def test_wasm_matches_native_inktext(tmp_path: Path) -> None:
    """WASM vs CLI parity across modes, synthetic outlines, JSON depth > 128, and f64 dim edge cases."""
    dehinted = dehinted_font_paths(tmp_path, zero_maxp=False)
    syn_ttf = make_synthetic_test_font(tmp_path / "syn_wasm.ttf")
    specs: list[Path] = []

    def add_spec(name: str, payload: dict[str, Any]) -> None:
        p = tmp_path / f"wasm-{len(specs)}-{name}.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        specs.append(p)

    l_npy = tmp_path / "l.npy"
    np.save(l_npy, np.zeros((48, 48), dtype=np.uint8))
    rgb_npy = tmp_path / "rgb.npy"
    np.save(rgb_npy, np.full((48, 48, 3), 20, dtype=np.uint8))
    rgba_npy = tmp_path / "rgba.npy"
    np.save(rgba_npy, np.zeros((48, 48, 4), dtype=np.uint8))

    fonts_list = [
        ["atkinson", str(dehinted["atkinson"])],
        ["dseg", str(font_path("dseg"))],
        ["syn", str(syn_ttf)],
    ]

    add_spec(
        "ok-l",
        make_inktext_cli_and_wasm_spec(
            npy_path=l_npy,
            mode="L",
            ops=[{"op": "char", "font": "dseg", "px": 24, "ch": "8", "x": 8, "y": 8, "fill": 255}],
            fonts=fonts_list,
            w=48,
            h=48,
        ),
    )
    add_spec(
        "ok-rgb",
        make_inktext_cli_and_wasm_spec(
            npy_path=rgb_npy,
            mode="RGB",
            ops=[{"op": "char", "font": "atkinson", "px": 20, "ch": "G", "x": 6, "y": 6, "fill": [255, 106, 0]}],
            fonts=fonts_list,
            w=48,
            h=48,
        ),
    )
    add_spec(
        "ok-rgba-and-outline",
        make_inktext_cli_and_wasm_spec(
            npy_path=rgba_npy,
            mode="RGBA",
            ops=[
                {"op": "char", "font": "syn", "px": 24, "ch": "C", "x": 4, "y": 4, "fill": [255, 255, 255, 255]},
                {
                    "op": "outline",
                    "points": [[0, 0], [0, 384], [384, 384], [384, 0], [128, 128], [128, 256], [256, 256], [256, 128]],
                    "tags": [1, 1, 1, 1, 1, 1, 1, 1],
                    "contours": [3, 7],
                    "flags": 2,
                    "x": 20,
                    "y": 20,
                    "fill": [0, 255, 0, 255],
                    "ppem": [16, 16],
                },
            ],
            fonts=fonts_list,
            w=48,
            h=48,
        ),
    )
    # Error cases: unknown font, bad mode, JSON depth > 128, bad outline overflow
    add_spec(
        "err-unknown-font",
        make_inktext_cli_and_wasm_spec(
            npy_path=l_npy,
            mode="L",
            ops=[{"op": "char", "font": "missing", "px": 16, "ch": "A", "x": 0, "y": 0, "fill": 255}],
            fonts=fonts_list,
            w=48,
            h=48,
        ),
    )
    add_spec(
        "err-bad-mode",
        make_inktext_cli_and_wasm_spec(npy_path=l_npy, mode="CMYK", ops=[], fonts=[], w=48, h=48),
    )
    add_spec(
        "err-deep-json",
        make_inktext_cli_and_wasm_spec(
            npy_path=l_npy,
            mode="L",
            ops="[" * 130 + "]" * 130,
            fonts=[],
            w=48,
            h=48,
        ),
    )

    # f64 parameter validation edge cases (UNIT-COMMON)
    for field in ("w", "h"):
        for val, cli_val in (
            (2**32 + 1, 2**32 + 1),
            (2**53, 2**53),
            (-1, 0),
            (1.5, 0),
            ("NaN", 0),
            ("Infinity", 0),
            ("-Infinity", 0),
        ):
            add_spec(
                f"dim-{field}-{val}",
                make_inktext_cli_and_wasm_spec(
                    npy_path=l_npy,
                    mode="L",
                    ops=[],
                    fonts=[],
                    w=val if field == "w" else 48,
                    h=val if field == "h" else 48,
                    cli_w=cli_val if field == "w" else 48,
                    cli_h=cli_val if field == "h" else 48,
                    override_dims=True,
                ),
            )

    proc = run_area("inktext", specs, tmp_path)
    check(proc.returncode == 0, "wasm inktext parity failed")
