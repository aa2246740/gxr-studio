"""Parity: gxr-cli GaussianBlur (BoxBlur.c 3-pass) on L and RGB against Pillow 12.2.0 (Item 4)."""

from __future__ import annotations

import itertools
import json
import math
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageFilter

from gxr_studio.finish.fonts import FontMetrics
from gxr_studio.finish.layout import frame_layout, stamp_layout
from gxr_studio.finish.spec import FRAME_STYLES, FrameSpec, StampSpec
from support.core_cli import run_json, run_npy
from support.export_pipeline_cases import anon_ids, check, same_pixels


def _collect_layout_blur_radii() -> list[float]:
    radii: set[float] = {0.0, 0.1, 0.5, 1.0, 0.33, 1.25, 2.75, 4.5, 12.3, 35.0}
    metrics = FontMetrics()
    for w, h in ((37, 61), (320, 240), (800, 1200), (1600, 1200), (4288, 2848)):
        for style in sorted(FRAME_STYLES - {"none"}):
            for weight in ("thin", "medium", "thick"):
                for color in ("white", "black", "cream"):
                    spec = FrameSpec(style=style, color=color, weight=weight, output_ratio="4:5")
                    layout = frame_layout(w, h, spec, {}, metrics)
                    if layout is None:
                        continue
                    if layout.bgfx and "blur" in layout.bgfx:
                        radii.add(float(layout.bgfx["blur"]))
                    for it in layout.items:
                        if it.get("k") == "shadow":
                            radii.add(float(it["blur"]))
        for size in ("small", "medium", "large"):
            for font in ("seg7", "typewriter", "sans"):
                st = StampSpec(enabled=True, font=font, size=size, glow=True, text="88")
                sl = stamp_layout(w, h, st, "26 01 01", metrics)
                if sl is not None and sl.glow is not None:
                    radii.add(float(sl.glow))
    return sorted(radii)


ALL_RADII = _collect_layout_blur_radii()


def _synth(w: int, h: int, mode: str, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    shape = (h, w) if mode == "L" else (h, w, 3)
    return rng.integers(0, 256, size=shape, dtype=np.uint8)


@pytest.mark.parametrize("mode", ["L", "RGB"], ids=["L", "RGB"])
@pytest.mark.parametrize("radius", ALL_RADII, ids=anon_ids(len(ALL_RADII)))
def test_gaussian_blur_radii(mode: str, radius: float, tmp_path: Path) -> None:
    arr = _synth(47, 39, mode, 101)
    pil = Image.fromarray(arr, mode).filter(ImageFilter.GaussianBlur(radius))
    in_npy = tmp_path / "in.npy"
    out_npy = tmp_path / "out.npy"
    np.save(in_npy, arr)
    got = run_npy(["ink-blur", "--in", str(in_npy), "--mode", mode, "--radius", repr(radius)], out_npy)
    same_pixels(got, np.asarray(pil, dtype=np.uint8), "ink-blur")


TINY_LARGE_CASES = list(itertools.product((1, 2, 3, 5, 11), (1, 2, 4, 9), (0.6, 3.5, 19.7)))


@pytest.mark.parametrize("mode", ["L", "RGB"], ids=["L", "RGB"])
@pytest.mark.parametrize(("w", "h", "radius"), TINY_LARGE_CASES, ids=anon_ids(len(TINY_LARGE_CASES)))
def test_gaussian_blur_tiny_and_large_radius(mode: str, w: int, h: int, radius: float, tmp_path: Path) -> None:
    arr = _synth(w, h, mode, 202 + w * 13 + h)
    pil = Image.fromarray(arr, mode).filter(ImageFilter.GaussianBlur(radius))
    in_npy = tmp_path / "in.npy"
    ops_p = tmp_path / "ops.json"
    out_npy = tmp_path / "out.npy"
    np.save(in_npy, arr)
    ops_p.write_text(json.dumps([{"op": "gaussian_blur", "radius": radius}]), encoding="utf-8")
    got = run_npy(["ink-ops", "--canvas", str(in_npy), "--mode", mode, "--ops", str(ops_p)], out_npy)
    same_pixels(got, np.asarray(pil, dtype=np.uint8), "ink gaussian_blur small/large")


def test_gaussian_blur_anisotropic_and_errors(tmp_path: Path) -> None:
    arr = _synth(29, 23, "RGB", 303)
    in_npy = tmp_path / "in.npy"
    np.save(in_npy, arr)

    pil_xy = Image.fromarray(arr, "RGB").filter(ImageFilter.GaussianBlur((2.4, 5.1)))
    out_xy = tmp_path / "out_xy.npy"
    got_xy = run_npy(["ink-blur", "--in", str(in_npy), "--mode", "RGB", "--radius", "2.4", "--y-radius", "5.1"], out_xy)
    same_pixels(got_xy, np.asarray(pil_xy, dtype=np.uint8), "ink-blur anisotropic")

    pil_zero = Image.fromarray(arr, "RGB").filter(ImageFilter.GaussianBlur((0.0, 2.2)))
    out_z = tmp_path / "out_z.npy"
    got_z = run_npy(["ink-blur", "--in", str(in_npy), "--mode", "RGB", "--radius", "0.0", "--y-radius", "2.2"], out_z)
    same_pixels(got_z, np.asarray(pil_zero, dtype=np.uint8), "ink-blur zero x")

    out_err = tmp_path / "err.npy"
    code, payload = run_json(["ink-blur", "--in", str(in_npy), "--mode", "RGB", "--radius", "-1.0", "--out", str(out_err)])
    check(code != 0, "negative blur radius fails")
    check(payload["error"]["kind"] == "value_error", "negative blur radius kind")

    # Huge radius rejected cleanly without float-to-int UB even on non-empty or empty images
    code_huge, payload_huge = run_json(["ink-blur", "--in", str(in_npy), "--mode", "RGB", "--radius", "1e300", "--out", str(out_err)])
    check(code_huge != 0 and payload_huge["error"]["kind"] == "value_error", "huge blur radius rejected cleanly")

    # FMA-sensitive GaussianBlur radius (5793.5390625 differs between fused & unfused _gaussian_blur_radius)
    pil_fma = Image.fromarray(arr, "RGB").filter(ImageFilter.GaussianBlur(5793.5390625))
    out_fma = tmp_path / "out_fma.npy"
    got_fma = run_npy(["ink-blur", "--in", str(in_npy), "--mode", "RGB", "--radius", repr(5793.5390625)], out_fma)
    same_pixels(got_fma, np.asarray(pil_fma, dtype=np.uint8), "ink-blur fma-sensitive radius")


def test_gaussian_blur_fma_weight_boundary(tmp_path: Path) -> None:
    # Sol r2 GaussianBlur numerator FMA boundary: 1x300001 uint8 row (seed 1210308362), radius 167772.15625.
    rng = np.random.default_rng(1210308362)
    row = rng.integers(0, 256, size=(1, 300001), dtype=np.uint8)
    radius = 167772.15625
    im_row = Image.fromarray(row, "L")
    pil_row = np.asarray(im_row.filter(ImageFilter.GaussianBlur(radius)), dtype=np.uint8)

    in_npy = tmp_path / "gauss_row_in.npy"
    out_npy = tmp_path / "gauss_row_out.npy"
    np.save(in_npy, row)
    got_row = run_npy(["ink-blur", "--in", str(in_npy), "--mode", "L", "--radius", repr(radius)], out_npy)
    same_pixels(got_row, pil_row, "ink-blur 1x300001 FMA weight boundary exact")

    # Test-only unfused `gauss_num` variant differs from Pillow/Rust on 113,642 pixels:
    r_f = np.float32(radius)
    sigma2 = np.float32(np.float32(r_f * r_f) / np.float32(3.0))
    l_big = np.float32(math.sqrt(12.0 * float(sigma2) + 1.0))
    l_val = np.float32(math.floor((float(l_big) - 1.0) / 2.0))
    l_p1 = np.float32(l_val + np.float32(1.0))
    a_num_u = np.float32(
        np.float32(np.float32(2.0) * l_val + np.float32(1.0)) * np.float32(np.float32(l_val * l_p1) - np.float32(3.0) * sigma2)
    )
    a_den_u = np.float32(np.float32(6.0) * np.float32(sigma2 - np.float32(l_p1 * l_p1)))
    r_unfused = float(np.float32(l_val + np.float32(a_num_u / a_den_u)))
    pil_unfused = im_row.copy()
    for _ in range(3):
        pil_unfused = pil_unfused.filter(ImageFilter.BoxBlur((r_unfused, 0.0)))
    unfused_arr = np.asarray(pil_unfused, dtype=np.uint8)
    check(int(np.sum(got_row != unfused_arr)) == 113642, "unfused gauss_num differs by 113,642 pixels")
