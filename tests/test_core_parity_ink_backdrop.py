"""Parity: gxr-cli blurred_background, paper_grain (PCG64 + Ziggurat), and background (Item 5)."""

from __future__ import annotations

import gc
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from gxr_studio.finish import frame_paint as paint
from gxr_studio.finish import frame_texture as tex
from gxr_studio.finish.compose import load_jpeg
from gxr_studio.finish.fonts import FontMetrics
from gxr_studio.finish.layout import frame_layout
from gxr_studio.finish.spec import FRAME_STYLES, OUTPUT_RATIOS, FrameSpec
from support.core_cli import run_json, run_npy
from support.export_pipeline_cases import anon_ids, check, real_pairs, same_pixels
from support.ink_gates import attribute_ziggurat_gate_b, check_ziggurat_negative_control, run_ziggurat_from_raw
from support.samples import need_sample


class _CachedMetrics(FontMetrics):
    def __init__(self) -> None:
        super().__init__()
        self._upm: dict[tuple[str, bool], int] = {}
        self._glyph: dict[tuple[str, str, bool], Any] = {}
        self._ascent: dict[tuple[str, int, bool], int] = {}

    def upm(self, font: str, cjk: bool) -> int:
        key = (font, cjk)
        if key not in self._upm:
            self._upm[key] = super().upm(font, cjk)
        return self._upm[key]

    def glyph(self, font: str, ch: str, cjk: bool) -> Any:
        key = (font, ch, cjk)
        if key not in self._glyph:
            self._glyph[key] = super().glyph(font, ch, cjk)
        return self._glyph[key]

    def ascent(self, font: str, px: int, cjk: bool) -> int:
        key = (font, px, cjk)
        if key not in self._ascent:
            self._ascent[key] = super().ascent(font, px, cjk)
        return self._ascent[key]


_METRICS = _CachedMetrics()


def _synth_rgb(w: int, h: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)


def _blur_fill_cases() -> list[tuple[int, int, str, bool]]:
    ratios = ["original", *sorted(OUTPUT_RATIOS)]
    cases: list[tuple[int, int, str, bool]] = []
    for w, h in ((37, 53), (120, 80), (80, 120), (100, 100)):
        for ratio in ratios:
            for show_params in (True, False):
                cases.append((w, h, ratio, show_params))
    return cases


BLUR_FILL_CASES = _blur_fill_cases()


def test_rng_pcg64_and_ziggurat_ten_million_draws_bitwise_and_gates(tmp_path: Path) -> None:
    draws = 10_000_000
    raw_draws = 10_250_000
    seeds = (42, 0, 1, 2**63)
    for seed in seeds:
        u64_npy = tmp_path / f"u64_{seed}.npy"
        norm_npy = tmp_path / f"norm_{seed}.npy"
        code, payload = run_json(
            [
                "ink-grain",
                "--draws",
                str(draws),
                "--u64-draws",
                str(raw_draws),
                "--seed",
                str(seed),
                "--u64-out",
                str(u64_npy),
                "--normal-out",
                str(norm_npy),
            ],
            timeout=300.0,
        )
        check(code == 0 and payload["draws"] == draws, "ink-grain draws cli ok")

        py_u64 = np.random.default_rng(seed).bit_generator.random_raw(draws)
        rs_u64 = np.load(u64_npy)
        u64_match = bool(np.array_equal(rs_u64[:draws], py_u64))
        check(u64_match, "pcg64 u64 10M stream bitwise match")

        py_norm = np.random.default_rng(seed).standard_normal(draws)
        rs_norm = np.load(norm_npy)
        norm_diff = attribute_ziggurat_gate_b("paper_grain-rng-10m", seed, rs_u64, rs_norm, py_norm)
        check(norm_diff >= 0, "pcg64 normal 10M stream Gate B attributed")

        if seed == 42:
            # Gate A + negative control on the first 120,000 draws (contains tail draw 113982 and wedge hits).
            gate_draws = 120_000
            raw_needed = int(gate_draws * 2)
            or_norm, tail_idx, wedge_idx = run_ziggurat_from_raw(rs_u64[:raw_needed], gate_draws, use_libm=True)
            tail_wedge_hits = len(tail_idx) + len(wedge_idx)
            check(len(tail_idx) > 0 and len(wedge_idx) > 0, "ziggurat slice covers tail and wedge")
            gate_a_diff = int(np.sum(or_norm.view(np.uint64) != rs_norm[:gate_draws].view(np.uint64)))
            check(gate_a_diff == 0, "ziggurat Gate A pinned libm bitwise match")
            check_ziggurat_negative_control(rs_u64[:raw_needed], gate_draws, rs_norm[:gate_draws], tail_idx, wedge_idx)
            gate_b_tw = attribute_ziggurat_gate_b(
                "paper_grain-rng-tail-wedge",
                seed,
                rs_u64[:raw_needed],
                rs_norm[:gate_draws],
                py_norm[:gate_draws],
            )
            check(gate_b_tw >= 0 and tail_wedge_hits > 0, "ziggurat tail/wedge Gate B attributed")

        del py_u64, rs_u64, py_norm, rs_norm
        u64_npy.unlink(missing_ok=True)
        norm_npy.unlink(missing_ok=True)
        gc.collect()


@pytest.mark.parametrize(
    ("w", "h", "ratio", "show_params"),
    BLUR_FILL_CASES,
    ids=anon_ids(len(BLUR_FILL_CASES)),
)
def test_blurred_background_blur_fill_synthetics(
    w: int,
    h: int,
    ratio: str,
    show_params: bool,
    tmp_path: Path,
) -> None:
    metrics = _METRICS
    spec = FrameSpec(style="blur_fill", output_ratio=ratio, show_params=show_params)
    layout = frame_layout(w, h, spec, {}, metrics)
    if layout is None or not layout.bgfx or layout.bgfx.get("kind") != "blur":
        return
    fx = layout.bgfx
    photo_arr = _synth_rgb(w, h, 100 + w + h)
    py_out = tex.blurred_background(Image.fromarray(photo_arr, "RGB"), layout.width, layout.height, fx)

    in_npy = tmp_path / "photo.npy"
    out_npy = tmp_path / "bg.npy"
    np.save(in_npy, photo_arr)
    got = run_npy(
        [
            "ink-backdrop",
            "--in",
            str(in_npy),
            "--w",
            str(layout.width),
            "--h",
            str(layout.height),
            "--sigma",
            repr(float(fx["sigma"])),
            "--sat",
            repr(float(fx.get("sat", 1.0))),
            "--veil",
            repr(float(fx["veil"])),
        ],
        out_npy,
    )
    same_pixels(got, np.asarray(py_out, dtype=np.uint8), "blurred_background synth")


def test_blurred_background_real_photos_all_ratios(tmp_path: Path) -> None:
    pairs = real_pairs(2)
    need_sample(bool(pairs), "no real JPEG/DNG samples under captures/")
    if not pairs:
        pytest.skip("no real samples under captures/")
    metrics = _METRICS
    ratios = ["original", *sorted(OUTPUT_RATIOS)]
    for p_idx, (jpg_path, _) in enumerate(pairs):
        photo_arr = load_jpeg(jpg_path, None)
        h, w = int(photo_arr.shape[0]), int(photo_arr.shape[1])
        in_npy = tmp_path / f"real_{p_idx}.npy"
        np.save(in_npy, photo_arr)
        pil_photo = Image.fromarray(photo_arr, "RGB")
        for r_idx, ratio in enumerate(ratios):
            spec = FrameSpec(style="blur_fill", output_ratio=ratio, show_params=(r_idx % 2 == 0))
            layout = frame_layout(w, h, spec, {}, metrics)
            if layout is None or not layout.bgfx:
                continue
            fx = layout.bgfx
            py_out = np.asarray(tex.blurred_background(pil_photo, layout.width, layout.height, fx), dtype=np.uint8)
            out_npy = tmp_path / f"real_{p_idx}_{r_idx}.npy"
            got = run_npy(
                [
                    "ink-backdrop",
                    "--in",
                    str(in_npy),
                    "--w",
                    str(layout.width),
                    "--h",
                    str(layout.height),
                    "--sigma",
                    repr(float(fx["sigma"])),
                    "--sat",
                    repr(float(fx.get("sat", 1.0))),
                    "--veil",
                    repr(float(fx["veil"])),
                ],
                out_npy,
                timeout=300.0,
            )
            same_pixels(got, py_out, "blurred_background real photo")
            out_npy.unlink(missing_ok=True)
        in_npy.unlink(missing_ok=True)
        del photo_arr, pil_photo
        gc.collect()


def test_paper_grain_styles_and_4288x2848(tmp_path: Path) -> None:
    metrics = _METRICS
    grain_styles: list[tuple[str, float]] = []
    for style in sorted(FRAME_STYLES - {"none"}):
        spec = FrameSpec(style=style, color="black" if style == "darkroom" else "white")
        layout = frame_layout(320, 240, spec, {}, metrics)
        if layout is not None and layout.grain > 0.0:
            grain_styles.append((style, float(layout.grain)))
    check(len(grain_styles) >= 1, "at least one frame style sets paper grain")

    # Small sizes across all grain-setting styles + edge amounts.
    small_cases: list[tuple[int, int, float]] = [(37, 53, 0.0), (64, 48, 0.05)]
    for _, amt in grain_styles:
        for w, h in ((17, 29), (120, 80), (240, 320)):
            small_cases.append((w, h, amt))

    for idx, (w, h, amt) in enumerate(small_cases):
        arr = _synth_rgb(w, h, 300 + idx)
        py_out = np.asarray(tex.paper_grain(Image.fromarray(arr, "RGB"), amt), dtype=np.uint8)
        in_npy = tmp_path / f"gs_{idx}.npy"
        out_npy = tmp_path / f"gs_out_{idx}.npy"
        np.save(in_npy, arr)
        got = run_npy(["ink-grain", "--in", str(in_npy), "--amount", repr(amt)], out_npy)
        same_pixels(got, py_out, "paper_grain small")

    # Full-size 4288x2848 test (direct photo size and max framed darkroom canvas).
    amt = grain_styles[0][1]
    big_layout = frame_layout(4288, 2848, FrameSpec(style="darkroom", color="black", table="dark", output_ratio="4:5"), {}, metrics)
    check(big_layout is not None, "darkroom 4288x2848 layout exists")
    assert big_layout is not None
    for tag, bw, bh in (("photo", 4288, 2848), ("max_frame", int(big_layout.width), int(big_layout.height))):
        arr = _synth_rgb(bw, bh, 999 if tag == "photo" else 1001)
        py_out = np.asarray(tex.paper_grain(Image.fromarray(arr, "RGB"), amt), dtype=np.uint8)
        in_npy = tmp_path / f"grain_{tag}.npy"
        out_npy = tmp_path / f"grain_out_{tag}.npy"
        np.save(in_npy, arr)
        got = run_npy(["ink-grain", "--in", str(in_npy), "--amount", repr(amt)], out_npy, timeout=300.0)
        same_pixels(got, py_out, "paper_grain large")
        in_npy.unlink(missing_ok=True)
        out_npy.unlink(missing_ok=True)
        del arr, py_out, got
        gc.collect()


def test_background_solid_and_glow_parity(tmp_path: Path) -> None:
    cases: list[tuple[int, int, tuple[int, int, int], dict[str, Any] | None]] = [
        (1, 1, (243, 241, 236), None),
        (1, 1, (243, 241, 236), {"kind": "glow", "falloff": 0.07}),
        (17, 29, (24, 24, 26), {"kind": "glow", "falloff": 0.0}),
        (120, 80, (243, 241, 236), {"kind": "glow", "falloff": 0.07}),
        (240, 320, (250, 248, 243), {"kind": "glow", "falloff": 0.05}),
        (80, 120, (12, 34, 56), None),
    ]
    for idx, (w, h, bg, fx) in enumerate(cases):
        py_out = np.asarray(paint.background(w, h, bg, fx), dtype=np.uint8)
        out_npy = tmp_path / f"bg_{idx}.npy"
        args = ["ink-backdrop", "--w", str(w), "--h", str(h), "--bg", f"{bg[0]},{bg[1]},{bg[2]}"]
        if fx and fx.get("kind") == "glow":
            args.extend(["--falloff", repr(float(fx["falloff"]))])
        got = run_npy(args, out_npy)
        same_pixels(got, py_out, "background solid or glow")

    # Verify ink-ops integration for background, blurred_background, paper_grain.
    src = _synth_rgb(64, 48, 777)
    in_npy = tmp_path / "ops_in.npy"
    np.save(in_npy, src)
    ops: list[dict[str, Any]] = [
        {"op": "blurred_background", "w": 80, "h": 100, "sigma": 90.0, "sat": 1.15, "veil": 0.9},
        {"op": "paper_grain", "amount": 0.016},
    ]
    ops_p = tmp_path / "ops.json"
    ops_p.write_text(json.dumps(ops), encoding="utf-8")
    out_ops = tmp_path / "ops_out.npy"
    got_ops = run_npy(["ink-ops", "--canvas", str(in_npy), "--mode", "RGB", "--ops", str(ops_p)], out_ops)
    py_blur = tex.blurred_background(Image.fromarray(src, "RGB"), 80, 100, {"sigma": 90.0, "sat": 1.15, "veil": 0.9})
    py_grain = np.asarray(tex.paper_grain(py_blur, 0.016), dtype=np.uint8)
    same_pixels(got_ops, py_grain, "ink-ops backdrop and paper_grain")
