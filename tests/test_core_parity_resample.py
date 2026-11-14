"""Parity: gxr-cli (resample-*) and gxr-wasm (resample_u8, resample_f32) against Pillow 12.2.0 and sequential IEEE oracle."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image, ImageOps

from gxr_studio.finish.hdr_export import _to_grid
from gxr_studio.finish.layout import fit_size
from gxr_studio.hdr.gain import block_mean
from gxr_studio.raw import NEUTRAL, RawDecodeError, open_dng, render_full
from support.core_cli import error_of, require_cli, run_json, run_npy
from support.resample_alloc import run_resample_oom_probe
from support.resample_oracle import oracle_pixel_f32, oracle_resample_f32, precompute_coeffs_seq
from support.wasm_check import run_area

_STUDIO = Path(__file__).resolve().parents[1]
_PARENT = Path(os.environ.get("GXR_STUDIO_TEST_PARENT", str(_STUDIO.parent)))
CAPTURES = _PARENT / "captures"

_PIL_FILTERS: dict[str, Image.Resampling] = {
    "box": Image.Resampling.BOX,
    "bilinear": Image.Resampling.BILINEAR,
    "bicubic": Image.Resampling.BICUBIC,
    "lanczos": Image.Resampling.LANCZOS,
}


def _collect_real_images(limit_per_kind: int = 12) -> tuple[list[Path], list[Path]]:
    if not CAPTURES.is_dir():
        return [], []
    jpgs: list[Path] = []
    dngs: list[Path] = []
    for path in sorted(CAPTURES.rglob("*"), key=lambda p: p.as_posix()):
        if not path.is_file() or "backup" in path.as_posix().lower():
            continue
        suf = path.suffix.upper()
        if suf in {".JPG", ".JPEG"}:
            jpgs.append(path)
        elif suf == ".DNG":
            dngs.append(path)
    if len(jpgs) > limit_per_kind:
        jpgs = [jpgs[(i * len(jpgs)) // limit_per_kind] for i in range(limit_per_kind)]
    if len(dngs) > limit_per_kind:
        dngs = [dngs[(i * len(dngs)) // limit_per_kind] for i in range(limit_per_kind)]
    return jpgs, dngs


def _synth_u8(w: int, h: int, mode: str, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    ch = {"L": 1, "RGB": 3, "RGBA": 4}[mode]
    if ch == 1:
        arr = rng.integers(0, 256, size=(h, w), dtype=np.uint8)
        if w >= 2 and h >= 2:
            arr[0, 0] = 0
            arr[-1, -1] = 255
        return np.ascontiguousarray(arr)
    arr = rng.integers(0, 256, size=(h, w, ch), dtype=np.uint8)
    if mode == "RGBA":
        # Exercise alpha=0 (hidden non-zero RGB erased by premultiplication), alpha=255, and partial alpha ramp
        flat = arr.reshape(-1, 4)
        for i in range(len(flat)):
            if i % 5 == 0:
                flat[i, :3] = [240, 120, 60]
                flat[i, 3] = 0
            elif i % 5 == 1:
                flat[i, 3] = 255
            else:
                flat[i, 3] = (i * 37) % 256
    return np.ascontiguousarray(arr)


def _synth_f32(w: int, h: int, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    arr = (rng.random((h, w)) * 8.0 - 2.0).astype(np.float32)
    return np.ascontiguousarray(arr)


def _assert_f32_bits_equal(ref: np.ndarray, got: np.ndarray, label: str) -> None:
    assert ref.dtype == np.float32 and got.dtype == np.float32, f"{label}: dtype {ref.dtype} vs {got.dtype}"
    assert ref.shape == got.shape, f"{label}: shape {ref.shape} vs {got.shape}"
    both_nan = np.isnan(ref) & np.isnan(got)
    diff = (ref.view(np.uint32) != got.view(np.uint32)) & ~both_nan
    if diff.any():
        idx = tuple(int(v) for v in np.argwhere(diff)[0])
        pytest.fail(
            f"{label}: {int(diff.sum())}/{ref.size} bits differ; first {idx}: "
            f"ref={ref[idx]!r} (0x{int(ref.view(np.uint32)[idx]):08x}) "
            f"got={got[idx]!r} (0x{int(got.view(np.uint32)[idx]):08x})"
        )


def _cli_u8(arr: np.ndarray, mode: str, filt: str, out_w: int, out_h: int, tmp_path: Path, tag: str) -> np.ndarray:
    in_npy = tmp_path / f"{tag}_in.npy"
    out_npy = tmp_path / f"{tag}_out.npy"
    np.save(in_npy, arr)
    return run_npy(
        [
            "resample-u8",
            "--in",
            str(in_npy),
            "--mode",
            mode,
            "--filter",
            filt,
            "--width",
            str(out_w),
            "--height",
            str(out_h),
        ],
        out_npy,
    )


def _cli_f32(arr: np.ndarray, filt: str, out_w: int, out_h: int, tmp_path: Path, tag: str) -> np.ndarray:
    in_npy = tmp_path / f"{tag}_in.npy"
    out_npy = tmp_path / f"{tag}_out.npy"
    np.save(in_npy, arr)
    return run_npy(
        [
            "resample-f32",
            "--in",
            str(in_npy),
            "--filter",
            filt,
            "--width",
            str(out_w),
            "--height",
            str(out_h),
        ],
        out_npy,
    )


# ---- 1. uint8 parity (L, RGB, RGBA across edge sizes & filters) --------------


_U8_CASES: list[tuple[int, int, int, int, str]] = [
    (1, 1, 1, 1, "1x1-identity"),
    (17, 13, 17, 13, "identity"),
    (1, 50, 1, 17, "1xN-down"),
    (1, 150, 1, 37, "tall-narrow-1x150"),
    (2, 250, 1, 40, "tall-narrow-2x250-both"),
    (2, 250, 2, 40, "tall-narrow-2x250-vert"),
    (1, 150, 3, 40, "tall-narrow-upscale-x"),
    (1, 150, 1, 200, "tall-1xN-upscale-y"),
    (50, 1, 17, 1, "Nx1-down"),
    (150, 1, 37, 1, "wide-Nx1-down"),
    (97, 65, 43, 29, "prime-down"),
    (31, 19, 53, 41, "prime-up"),
    (24, 18, 48, 36, "2x-upscale"),
    (64, 48, 32, 24, "half-downscale"),
    (120, 90, 41, 33, "odd-ratio"),
    (600, 600, 5, 5, "over-100x-2d"),
    (4096, 1, 20, 1, "over-100x-horiz"),
    (1, 4096, 1, 20, "over-100x-vert"),
]


@pytest.mark.parametrize("mode", ["L", "RGB", "RGBA"])
@pytest.mark.parametrize("filt", ["lanczos", "box", "bilinear", "bicubic"])
def test_uint8_synthetic_sizes_and_modes(mode: str, filt: str, tmp_path: Path) -> None:
    require_cli()
    pil_filter = _PIL_FILTERS[filt]
    for idx, (iw, ih, ow, oh, label) in enumerate(_U8_CASES):
        arr = _synth_u8(iw, ih, mode, seed=100 + idx)
        py_out = np.asarray(Image.fromarray(arr, mode).resize((ow, oh), pil_filter))
        rs_out = _cli_u8(arr, mode, filt, ow, oh, tmp_path, f"{mode}_{filt}_{idx}")
        assert rs_out.dtype == np.uint8 and rs_out.shape == py_out.shape
        if not np.array_equal(py_out, rs_out):
            at = tuple(int(v) for v in np.argwhere(py_out != rs_out)[0])
            pytest.fail(f"u8 mismatch {mode} {filt} {label} ({iw}x{ih}->{ow}x{oh}) at {at}: pillow={int(py_out[at])} rust={int(rs_out[at])}")


def test_rgba_alpha_zero_and_ramp_edge_cases(tmp_path: Path) -> None:
    require_cli()
    w, h = 65, 49
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    # Hidden non-zero color under alpha=0 in left half; alpha ramp 0..255 across rows
    rgba[:, : w // 2, 0] = 255
    rgba[:, : w // 2, 1] = 180
    rgba[:, : w // 2, 2] = 90
    rgba[:, w // 2 :, 0] = 40
    rgba[:, w // 2 :, 1] = 210
    rgba[:, w // 2 :, 2] = 250
    for y in range(h):
        rgba[y, w // 2 :, 3] = (y * 255) // (h - 1)
    rgba[:, 0, 3] = 255
    rgba[:, 0, :3] = [255, 0, 255]

    for filt in ("lanczos", "box"):
        # Identity must preserve hidden RGB under alpha=0
        rs_id = _cli_u8(rgba, "RGBA", filt, w, h, tmp_path, f"rgba_id_{filt}")
        py_id = np.asarray(Image.fromarray(rgba, "RGBA").resize((w, h), _PIL_FILTERS[filt]))
        assert np.array_equal(py_id, rs_id)

        # Resize erases alpha=0 color before resampling
        rs = _cli_u8(rgba, "RGBA", filt, 27, 21, tmp_path, f"rgba_rs_{filt}")
        py = np.asarray(Image.fromarray(rgba, "RGBA").resize((27, 21), _PIL_FILTERS[filt]))
        assert np.array_equal(py, rs)


# ---- 2. Real camera JPGs and neutral DNG renders across export tiers --------


def test_uint8_real_exports_fit_size_lanczos(tmp_path: Path) -> None:
    require_cli()
    jpgs, dngs = _collect_real_images(limit_per_kind=12)
    if len(jpgs) + len(dngs) < 20:
        msg = f"need >= 20 real images under {CAPTURES}, found {len(jpgs)} jpg + {len(dngs)} dng"
        if os.environ.get("GXR_REQUIRE_CORE") == "1":
            pytest.fail(msg)
        pytest.skip(msg)

    images: list[tuple[str, np.ndarray]] = []
    for p in jpgs:
        with Image.open(io.BytesIO(p.read_bytes())) as im:
            turned = ImageOps.exif_transpose(im)
            rgb = np.asarray((turned if turned is not None else im).convert("RGB"))
        images.append((f"jpg:{p.name}", np.ascontiguousarray(rgb)))

    for p in dngs:
        try:
            dng = open_dng(p)
        except RawDecodeError:
            continue
        try:
            rgb = render_full(dng, NEUTRAL)
        finally:
            dng.release()
        images.append((f"dng:{p.name}", np.ascontiguousarray(rgb)))

    assert len(images) >= 20
    checked = 0
    for idx, (label, rgb) in enumerate(images):
        h, w = rgb.shape[:2]
        for max_edge in (3000, 2048, 1600):
            ow, oh = fit_size(w, h, max_edge)
            py_out = np.asarray(Image.fromarray(rgb, "RGB").resize((ow, oh), Image.Resampling.LANCZOS))
            rs_out = _cli_u8(rgb, "RGB", "lanczos", ow, oh, tmp_path, f"real_{idx}_{max_edge}")
            if not np.array_equal(py_out, rs_out):
                at = tuple(int(v) for v in np.argwhere(py_out != rs_out)[0])
                pytest.fail(f"real image {label} ({w}x{h}->{ow}x{oh}) differed at {at}: pillow={int(py_out[at])} rust={int(rs_out[at])}")
            checked += 1
    print(f"real_images={len(images)} lanczos_resizes={checked} diffs=0")


# ---- 3. float32 (mode F): Gate A sequential oracle + NaN/Inf + Gate B FMA ---


@pytest.mark.parametrize("filt", ["box", "lanczos", "bilinear", "bicubic"])
def test_f32_gate_a_portability_and_nan_inf(filt: str, tmp_path: Path) -> None:
    require_cli()
    cases = [
        (1, 1, 1, 1),
        (17, 13, 17, 13),
        (1, 50, 1, 17),
        (1, 150, 1, 37),
        (2, 250, 1, 40),
        (50, 1, 17, 1),
        (97, 65, 43, 29),
        (31, 19, 53, 41),
        (64, 48, 32, 24),
        (120, 90, 41, 33),
        (600, 600, 5, 5),
    ]
    for idx, (iw, ih, ow, oh) in enumerate(cases):
        arr = _synth_f32(iw, ih, seed=500 + idx)
        or_out = oracle_resample_f32(arr, ow, oh, filt, use_fma=False)
        rs_out = _cli_f32(arr, filt, ow, oh, tmp_path, f"f32_a_{filt}_{idx}")
        _assert_f32_bits_equal(or_out, rs_out, f"gateA {filt} {iw}x{ih}->{ow}x{oh}")

    # NaN, +inf, -inf, -0.0 propagation & identity preservation
    nan_plane = _synth_f32(19, 13, seed=999)
    nan_plane[2, 3] = np.nan
    nan_plane[5, 7] = np.float32(np.inf)
    nan_plane[8, 1] = np.float32(-np.inf)
    nan_plane[0, 0] = np.float32(-0.0)
    # Identity: exact bits including -0.0 and NaN
    rs_id = _cli_f32(nan_plane, filt, 19, 13, tmp_path, f"f32_nan_id_{filt}")
    py_id = np.asarray(Image.fromarray(nan_plane, "F").resize((19, 13), _PIL_FILTERS[filt]))
    _assert_f32_bits_equal(py_id, rs_id, f"f32 nan identity {filt}")
    _assert_f32_bits_equal(nan_plane, rs_id, f"f32 nan identity self {filt}")

    # Resize: NaN and Inf positions match sequential oracle & real Pillow
    for ow, oh in ((9, 7), (25, 19)):
        rs_nan = _cli_f32(nan_plane, filt, ow, oh, tmp_path, f"f32_nan_{filt}_{ow}x{oh}")
        or_nan = oracle_resample_f32(nan_plane, ow, oh, filt, use_fma=False)
        py_nan = np.asarray(Image.fromarray(nan_plane, "F").resize((ow, oh), _PIL_FILTERS[filt]))
        _assert_f32_bits_equal(or_nan, rs_nan, f"f32 nan oracle {filt} {ow}x{oh}")
        assert np.array_equal(np.isnan(py_nan), np.isnan(rs_nan)), f"NaN mask mismatch {filt}"
        assert np.array_equal(np.isposinf(py_nan), np.isposinf(rs_nan)), f"+inf mask mismatch {filt}"
        assert np.array_equal(np.isneginf(py_nan), np.isneginf(rs_nan)), f"-inf mask mismatch {filt}"


def test_f32_gate_b_real_hdr_shapes_and_fma_attribution(tmp_path: Path) -> None:
    """Gate B: compare float32 BOX/LANCZOS on real _to_grid shapes against real Pillow, attributing any FMA diffs."""
    require_cli()

    # Negative control: prove real Pillow on this host contracts FMA on (120, 90) -> (41, 33) BOX,
    # violating sequential Gate A when host Pillow is tested directly.
    neg_small = _synth_f32(120, 90, seed=77)
    neg_py = np.asarray(Image.fromarray(neg_small, "F").resize((41, 33), Image.Resampling.BOX))
    neg_seq = oracle_resample_f32(neg_small, 41, 33, "box", use_fma=False)
    neg_fma = oracle_resample_f32(neg_small, 41, 33, "box", use_fma=True)
    neg_diff_bits = int(np.sum(neg_py.view(np.uint32) != neg_seq.view(np.uint32)))
    assert neg_diff_bits > 0, "negative control expected Pillow arm64 FMA to differ from sequential IEEE"
    _assert_f32_bits_equal(neg_fma, neg_py, "negative-control FMA oracle matches host Pillow")

    # Real _to_grid shapes on well-conditioned non-negative HDR luminance values ([0.05, 4.0]).
    # Up to 2 ULPs can occur when both horizontal and vertical passes contract FMA at a 0.5-ULP tie.
    shapes: list[tuple[int, int, int, int, str]] = [
        (120, 90, 41, 33, "odd-box"),
        (4288, 2848, 1600, 1063, "hdr-tier-1600"),
        (4288, 2848, 2048, 1360, "hdr-tier-2048"),
        (4288, 2848, 3000, 1993, "hdr-tier-3000"),
        (2848, 4288, 1063, 1600, "hdr-portrait-1600"),
    ]
    for idx, (iw, ih, ow, oh, label) in enumerate(shapes):
        rng_shape = np.random.default_rng(800 + idx)
        src = np.ascontiguousarray(rng_shape.uniform(0.05, 4.0, (ih, iw)).astype(np.float32))
        for filt in ("box", "lanczos"):
            rs_out = _cli_f32(src, filt, ow, oh, tmp_path, f"f32_b_{filt}_{idx}")
            py_out = np.asarray(Image.fromarray(src, "F").resize((ow, oh), _PIL_FILTERS[filt]))
            assert rs_out.dtype == np.float32 and py_out.dtype == np.float32
            assert rs_out.shape == py_out.shape

            diff_mask = rs_out.view(np.uint32) != py_out.view(np.uint32)
            n_diff = int(diff_mask.sum())
            max_ulp = 0
            max_abs = 0.0
            if n_diff > 0:
                rs_bits = rs_out.view(np.uint32)[diff_mask].astype(np.int64)
                py_bits = py_out.view(np.uint32)[diff_mask].astype(np.int64)
                ulps = np.abs(rs_bits - py_bits)
                max_ulp = int(np.max(ulps))
                max_abs = float(np.max(np.abs(rs_out[diff_mask] - py_out[diff_mask])))
                assert max_ulp <= 2, f"{label} {filt}: max_ulp={max_ulp} > 2 (max_abs={max_abs})"
                assert max_abs <= 1e-6, f"{label} {filt}: max_abs={max_abs} > 1e-6"

            if iw <= 128 and ih <= 128:
                or_seq = oracle_resample_f32(src, ow, oh, filt, use_fma=False)
                or_fma = oracle_resample_f32(src, ow, oh, filt, use_fma=True)
                _assert_f32_bits_equal(or_seq, rs_out, f"{label} {filt} gateA")
                _assert_f32_bits_equal(or_fma, py_out, f"{label} {filt} gateB-fma")
            elif n_diff > 0:
                _, bh, kkh = precompute_coeffs_seq(iw, 0.0, float(iw), ow, filt)
                _, bv, kkv = precompute_coeffs_seq(ih, 0.0, float(ih), oh, filt)
                diff_coords = np.argwhere(diff_mask)
                multi_ulp = diff_coords[ulps > 1]
                sample = diff_coords[:500]
                to_check = np.vstack([multi_ulp, sample]) if len(multi_ulp) else sample
                for yy, xx in to_check:
                    yi, xi = int(yy), int(xx)
                    seq_px = oracle_pixel_f32(src, xi, yi, bh, kkh, bv, kkv, use_fma=False)
                    fma_px = oracle_pixel_f32(src, xi, yi, bh, kkh, bv, kkv, use_fma=True)
                    assert seq_px.view(np.uint32) == rs_out[yi, xi].view(np.uint32), (
                        f"{label} {filt} ({yi},{xi}): Rust diverged from sequential oracle"
                    )
                    assert fma_px.view(np.uint32) == py_out[yi, xi].view(np.uint32), (
                        f"{label} {filt} ({yi},{xi}): Pillow diverged from FMA oracle"
                    )
            print(f"[gate-b-f32] {label} ({iw}x{ih}->{ow}x{oh}) {filt}: diff={n_diff}/{rs_out.size} max_ulp={max_ulp} max_abs={max_abs:.3e}")

    # Verify _to_grid end-to-end on a realistic synthetic HDR layer (712x1072 -> 266x400, factor=4)
    rng = np.random.default_rng(2026)
    layer = np.stack(
        [
            rng.uniform(0.05, 4.0, (712, 1072)).astype(np.float32),
            (rng.random((712, 1072)) > 0.92).astype(np.float32),
        ],
        axis=-1,
    )
    py_y, py_clip = _to_grid(layer, (400, 266), 4)
    weight = np.isfinite(layer[..., 0]).astype(np.float32)
    planes = [np.where(weight > 0, layer[..., k], 0.0).astype(np.float32) for k in range(2)] + [weight]
    rs_planes = [_cli_f32(p, "box", 400, 266, tmp_path, f"togrid_{k}") for k, p in enumerate(planes)]
    ry, rc, rw = (block_mean(p, 4) for p in rs_planes)
    rs_photo = rw > 0.999
    rs_y = np.where(rs_photo, ry / np.maximum(rw, 1e-6), np.nan).astype(np.float32)
    rs_clip = (rc / np.maximum(rw, 1e-6)) > 0.01
    assert np.array_equal(py_clip, rs_clip), "_to_grid clip mask differed"
    max_raw_y_diff = float(np.nanmax(np.abs(py_y - rs_y)))
    assert max_raw_y_diff <= 1e-6, f"_to_grid raw_y diff={max_raw_y_diff}"
    print(f"[to_grid] clip_diffs=0 max_raw_y_diff={max_raw_y_diff:.3e}")


# ---- 4. Errors, oversized bounds, OOM injection, and WASM parity -------------


def test_errors_and_oversized_dims_match(tmp_path: Path) -> None:
    require_cli()
    tiny_rgb = _synth_u8(4, 3, "RGB", seed=1)
    tiny_f32 = _synth_f32(4, 3, seed=2)
    in_rgb = tmp_path / "tiny_rgb.npy"
    in_f32 = tmp_path / "tiny_f32.npy"
    out_npy = tmp_path / "err_out.npy"
    np.save(in_rgb, tiny_rgb)
    np.save(in_f32, tiny_f32)

    # Zero dims
    for args in (
        ["resample-u8", "--in", str(in_rgb), "--mode", "RGB", "--filter", "lanczos", "--width", "0", "--height", "4", "--out", str(out_npy)],
        ["resample-u8", "--in", str(in_rgb), "--mode", "RGB", "--filter", "lanczos", "--width", "4", "--height", "0", "--out", str(out_npy)],
        ["resample-f32", "--in", str(in_f32), "--filter", "box", "--width", "0", "--height", "4", "--out", str(out_npy)],
    ):
        code, payload = run_json(args)
        err = error_of(code, payload)
        assert err is not None and err.kind == "value_error"

    # Bad mode / filter / channels / input shape mismatch
    for args in (
        ["resample-u8", "--in", str(in_rgb), "--mode", "CMYK", "--filter", "lanczos", "--width", "2", "--height", "2", "--out", str(out_npy)],
        ["resample-u8", "--in", str(in_rgb), "--mode", "RGB", "--filter", "nearest", "--width", "2", "--height", "2", "--out", str(out_npy)],
        ["resample-u8", "--in", str(in_rgb), "--mode", "L", "--filter", "box", "--width", "2", "--height", "2", "--out", str(out_npy)],
        [
            "resample-u8",
            "--in",
            str(in_rgb),
            "--mode",
            "RGB",
            "--filter",
            "box",
            "--width",
            "2",
            "--height",
            "2",
            "--in-shape",
            "2",
            "4",
            "--out",
            str(out_npy),
        ],
        ["resample-f32", "--in", str(in_f32), "--filter", "nearest", "--width", "2", "--height", "2", "--out", str(out_npy)],
        [
            "resample-f32",
            "--in",
            str(in_f32),
            "--filter",
            "box",
            "--width",
            "2",
            "--height",
            "2",
            "--in-shape",
            "2",
            "4",
            "--out",
            str(out_npy),
        ],
    ):
        code, payload = run_json(args)
        err = error_of(code, payload)
        assert err is not None and err.kind == "value_error", (args, payload)

    # Oversized output / input dims -> bad_geometry ARRAY_TOO_BIG
    for ow, oh in ((4294967295, 4294967295), (2147483648, 1)):
        for args in (
            [
                "resample-u8",
                "--in",
                str(in_rgb),
                "--mode",
                "RGB",
                "--filter",
                "box",
                "--width",
                str(ow),
                "--height",
                str(oh),
                "--out",
                str(out_npy),
            ],
            [
                "resample-f32",
                "--in",
                str(in_f32),
                "--filter",
                "box",
                "--width",
                str(ow),
                "--height",
                str(oh),
                "--out",
                str(out_npy),
            ],
            [
                "resample-u8",
                "--in",
                str(in_rgb),
                "--mode",
                "RGB",
                "--filter",
                "box",
                "--width",
                "2",
                "--height",
                "2",
                "--in-shape",
                str(oh),
                str(ow),
                "--out",
                str(out_npy),
            ],
        ):
            code, payload = run_json(args)
            err = error_of(code, payload)
            assert err is not None and err.kind == "bad_geometry" and "array is too big" in err.message, (args, payload)


def test_rule2_resample_oom_probe(tmp_path: Path) -> None:
    run_resample_oom_probe(tmp_path)


def test_wasm_matches_native_for_resample(tmp_path: Path) -> None:
    specs: list[Path] = []

    def add_u8(
        name: str,
        arr: np.ndarray,
        mode: str,
        filt: str,
        out_w: int,
        out_h: int,
        *,
        in_w: int | None = None,
        in_h: int | None = None,
        override_channels: int | None = None,
    ) -> None:
        npy = tmp_path / f"{name}.npy"
        np.save(npy, arr)
        h, w = arr.shape[:2]
        ch = 1 if arr.ndim == 2 else int(arr.shape[2])
        spec: dict[str, Any] = {
            "kind": "u8",
            "inNpy": str(npy),
            "inW": in_w if in_w is not None else w,
            "inH": in_h if in_h is not None else h,
            "channels": ch,
            "mode": mode,
            "filter": filt,
            "outW": out_w,
            "outH": out_h,
            "overrideInShape": in_w is not None or in_h is not None,
        }
        if override_channels is not None:
            spec["overrideChannels"] = override_channels
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps(spec), encoding="utf-8")
        specs.append(p)

    def add_f32(
        name: str,
        arr: np.ndarray,
        filt: str,
        out_w: int,
        out_h: int,
        *,
        in_w: int | None = None,
        in_h: int | None = None,
    ) -> None:
        npy = tmp_path / f"{name}.npy"
        np.save(npy, arr)
        h, w = arr.shape[:2]
        spec = {
            "kind": "f32",
            "inNpy": str(npy),
            "inW": in_w if in_w is not None else w,
            "inH": in_h if in_h is not None else h,
            "filter": filt,
            "outW": out_w,
            "outH": out_h,
            "overrideInShape": in_w is not None or in_h is not None,
        }
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps(spec), encoding="utf-8")
        specs.append(p)

    add_u8("l-lanczos", _synth_u8(41, 33, "L", seed=1), "L", "lanczos", 19, 15)
    add_u8("rgb-lanczos", _synth_u8(41, 33, "RGB", seed=2), "RGB", "lanczos", 19, 15)
    add_u8("rgb-box", _synth_u8(41, 33, "RGB", seed=3), "RGB", "box", 19, 15)
    add_u8("rgba-lanczos", _synth_u8(41, 33, "RGBA", seed=4), "RGBA", "lanczos", 19, 15)
    add_u8("rgba-identity", _synth_u8(17, 13, "RGBA", seed=5), "RGBA", "lanczos", 17, 13)
    add_u8("tall-narrow-rgb", _synth_u8(2, 250, "RGB", seed=6), "RGB", "lanczos", 1, 40)
    add_u8("tall-narrow-rgba", _synth_u8(1, 150, "RGBA", seed=7), "RGBA", "lanczos", 1, 37)

    nan_plane = _synth_f32(25, 19, seed=8)
    nan_plane[2, 3] = np.nan
    nan_plane[4, 5] = np.float32(np.inf)
    nan_plane[6, 7] = np.float32(-np.inf)
    nan_plane[0, 0] = np.float32(-0.0)
    add_f32("f32-box", _synth_f32(41, 33, seed=9), "box", 19, 15)
    add_f32("f32-lanczos", _synth_f32(41, 33, seed=10), "lanczos", 19, 15)
    add_f32("f32-nan-id", nan_plane, "box", 25, 19)
    add_f32("f32-nan-resize", nan_plane, "box", 11, 9)
    add_f32("f32-tall-narrow", _synth_f32(2, 250, seed=11), "box", 1, 40)

    # Error parity cases
    tiny_rgb = _synth_u8(4, 3, "RGB", seed=12)
    tiny_f32 = _synth_f32(4, 3, seed=13)
    add_u8("err-zero-w", tiny_rgb, "RGB", "lanczos", 0, 3)
    add_u8("err-bad-mode", tiny_rgb, "BAD", "lanczos", 2, 2)
    add_u8("err-bad-filter", tiny_rgb, "RGB", "nearest", 2, 2)
    add_u8("err-bad-channels", tiny_rgb, "RGB", "box", 2, 2, override_channels=4)
    add_u8("err-mismatch-in", tiny_rgb, "RGB", "box", 2, 2, in_w=4, in_h=2)
    add_u8("err-huge-out-1", tiny_rgb, "RGB", "box", 4294967295, 4294967295)
    add_u8("err-huge-out-2", tiny_rgb, "RGB", "box", 2147483648, 1)
    add_u8("err-huge-in", tiny_rgb, "RGB", "box", 2, 2, in_w=2147483648, in_h=1)
    add_f32("err-f32-zero", tiny_f32, "box", 0, 2)
    add_f32("err-f32-filter", tiny_f32, "nearest", 2, 2)
    add_f32("err-f32-mismatch", tiny_f32, "box", 2, 2, in_w=4, in_h=2)
    add_f32("err-f32-huge-1", tiny_f32, "box", 4294967295, 4294967295)
    add_f32("err-f32-huge-2", tiny_f32, "box", 2147483648, 1)

    proc = run_area("resample", specs, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
