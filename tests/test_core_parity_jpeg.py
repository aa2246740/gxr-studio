"""Parity: gxr-cli jpeg-* and gxr-wasm jpeg_encode against Python export/container.

Gates (UNIT-jpeg.md & PORT-WAVE-1):
- Structure (Gate A): Pillow decodes the output; SOF sampling factors are 4:4:4 for quality >= 90
  and 4:2:0 for quality < 90; APP2 ICC is 588 bytes matching Python's ``srgb_icc()`` outside the
  12-byte LittleCMS creation timestamp at bytes 24..36; DQT quantization tables match Pillow's
  IJG scaling for the same quality.
- Picture (Gate B): decoded per-channel mean absolute difference <= 0.5 and max <= 4 on
  synthetic test images and 5 real exported arrays against Pillow's single encode with the same
  ``_finish`` arguments.
- EXIF: ``im.getexif()`` and ``im.getexif().get_ifd(0x8769)`` are identical to Python ``_finish``.
- Container helpers: ``jpeg-segments`` and ``jpeg-set-meta`` are exact against a Python
  reference using ``gxr_studio.hdr.container.jpeg_segments``.
- WASM: ``jpeg_encode`` matches ``gxr-cli jpeg-encode`` byte-for-byte, errors included.
"""

from __future__ import annotations

import io
import json
import math
import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image, ImageCms, JpegImagePlugin
from PIL.TiffImagePlugin import IFDRational

from gxr_studio.export import QUALITY_TIERS, SOFTWARE, srgb_icc
from gxr_studio.hdr import container
from gxr_studio.raw import NEUTRAL, open_dng, render_full
from support.core_cli import CoreCliError, error_of, require_cli, run_json
from support.wasm_check import run_area

_STUDIO = Path(__file__).resolve().parents[1]
_PARENT = Path(os.environ.get("GXR_STUDIO_TEST_PARENT", str(_STUDIO.parent)))
CAPTURES = _PARENT / "captures"
_REAL_EXPORT_COUNT = 5
_DROP_BASE_TAGS = {274, 34665, 34853, 330, 513, 514}
_DROP_EXIF_TAGS = {37500}


def _real_dngs(limit: int = _REAL_EXPORT_COUNT) -> list[Path]:
    """Up to ``limit`` real DNGs under captures/, spread in sorted order, skipping backups."""
    if not CAPTURES.is_dir():
        return []
    found = [
        path for path in CAPTURES.rglob("*") if path.is_file() and path.suffix.upper() == ".DNG" and "backup" not in path.as_posix().lower()
    ]
    found.sort(key=lambda path: path.as_posix())
    if len(found) <= limit:
        return found
    return [found[(i * len(found)) // limit] for i in range(limit)]


REAL_DNGS = _real_dngs()


def _real_id(path: Path) -> str:
    return path.relative_to(CAPTURES).as_posix().replace("/", "_")


def _python_single_encode(rgb: np.ndarray, quality: int, base: dict[int, Any], sub: dict[int, Any]) -> bytes:
    """Single-encode Pillow oracle using ``export.py:_finish`` arguments and ``build_exif``."""
    im = Image.fromarray(np.ascontiguousarray(rgb), "RGB")

    def build_exif(full: bool) -> Image.Exif:
        out = Image.Exif()
        keep = base if full else {k: base[k] for k in (270, 271, 272, 306, 33432) if k in base}
        for k, v in keep.items():
            if k in _DROP_BASE_TAGS:
                continue
            if k == 33432 and isinstance(v, str):
                v = v.encode("latin-1")
            out[k] = v.encode("utf-8") if k == 270 and isinstance(v, str) else v
        out[274] = 1
        out[305] = SOFTWARE
        osub = out.get_ifd(0x8769)
        for k, v in sub.items():
            if k not in _DROP_EXIF_TAGS and (full or k in (36867, 36868, 34855, 42036)):
                osub[k] = v
        osub[40962], osub[40963] = im.width, im.height
        return out

    for full in (True, False):
        buf = io.BytesIO()
        try:
            im.save(
                buf,
                "JPEG",
                quality=quality,
                subsampling=0 if quality >= 90 else 2,
                optimize=True,
                exif=build_exif(full),
                icc_profile=srgb_icc(),
            )
            return buf.getvalue()
        except Exception:
            continue
    raise AssertionError("Pillow single encode failed")


def _exif_to_json(base: dict[int, Any], sub: dict[int, Any]) -> str:
    def encode_val(v: Any) -> Any:
        if isinstance(v, bytes):
            return {"bytes": list(v)}
        if isinstance(v, IFDRational):
            return {"rational": [int(v.numerator), int(v.denominator)]}
        if isinstance(v, tuple):
            return [encode_val(item) for item in v]
        if isinstance(v, int):
            return int(v)
        if isinstance(v, float):
            return float(v)
        return str(v)

    return json.dumps(
        {
            "base": {str(k): encode_val(v) for k, v in base.items()},
            "exif": {str(k): encode_val(v) for k, v in sub.items()},
        },
        ensure_ascii=False,
    )


def _cli_encode(
    rgb: np.ndarray,
    quality: int,
    base: dict[int, Any],
    sub: dict[int, Any],
    tmp_path: Path,
    stem: str = "img",
) -> bytes:
    npy_path = tmp_path / f"{stem}.npy"
    exif_path = tmp_path / f"{stem}.exif.json"
    out_path = tmp_path / f"{stem}.jpg"
    np.save(npy_path, np.ascontiguousarray(rgb, dtype=np.uint8))
    exif_path.write_text(_exif_to_json(base, sub), encoding="utf-8")
    code, payload = run_json(
        [
            "jpeg-encode",
            "--in",
            str(npy_path),
            "--quality",
            str(quality),
            "--exif",
            str(exif_path),
            "--out",
            str(out_path),
        ]
    )
    err = error_of(code, payload)
    if err is not None:
        raise err
    assert isinstance(payload, dict)
    data = out_path.read_bytes()
    assert payload["bytes"] == len(data)
    assert payload["width"] == rgb.shape[1] and payload["height"] == rgb.shape[0]
    assert payload["quality"] == quality
    return data


def _sof_sampling(jpeg_bytes: bytes) -> list[tuple[int, int]]:
    segs, _sos, _eoi = container.jpeg_segments(jpeg_bytes)
    for seg in segs:
        if seg.marker in (0xC0, 0xC1, 0xC2):
            p = seg.payload(jpeg_bytes)
            n = p[5]
            return [(p[6 + 3 * i + 1] >> 4, p[6 + 3 * i + 1] & 0x0F) for i in range(n)]
    raise AssertionError("missing SOF marker")


def _synthetic_images() -> list[tuple[str, np.ndarray]]:
    imgs: list[tuple[str, np.ndarray]] = []
    # Smooth gradient + color ramps (odd dimensions not divisible by 16)
    h, w = 97, 131
    yy, xx = np.mgrid[0:h, 0:w]
    grad = np.stack(
        [
            np.rint(xx * (255.0 / (w - 1))).astype(np.uint8),
            np.rint(yy * (255.0 / (h - 1))).astype(np.uint8),
            np.rint(((xx + yy) / (w + h - 2)) * 255.0).astype(np.uint8),
        ],
        axis=-1,
    )
    imgs.append(("gradient_97x131", grad))

    # Color blocks + frequency patterns (divisible by 16)
    rng = np.random.default_rng(7)
    patch = np.zeros((128, 192, 3), dtype=np.uint8)
    patch[:64, :96] = [210, 45, 30]
    patch[:64, 96:] = [35, 185, 80]
    patch[64:, :96] = [40, 85, 220]
    # Smooth low-amplitude noise on last quadrant so DCT exercises high freqs without harsh ringing
    smooth = np.clip(128 + rng.normal(0, 12, (64, 96, 3)), 0, 255).astype(np.uint8)
    patch[64:, 96:] = smooth
    imgs.append(("blocks_128x192", patch))

    # Tiny edge case 1x1 and 17x15
    imgs.append(("tiny_1x1", np.array([[[123, 45, 200]]], dtype=np.uint8)))
    imgs.append(("odd_17x15", np.full((17, 15, 3), (64, 128, 192), dtype=np.uint8)))
    return imgs


def _assert_structure_and_measure_picture(
    rgb: np.ndarray,
    quality: int,
    rust_jpg: bytes,
    py_jpg: bytes,
    label: str,
) -> tuple[list[float], list[int], int, int]:
    im_r = Image.open(io.BytesIO(rust_jpg))
    im_p = Image.open(io.BytesIO(py_jpg))
    im_r.load()
    im_p.load()

    # Gate A: structure
    assert im_r.format == "JPEG"
    assert im_r.size == (rgb.shape[1], rgb.shape[0])
    expected_sub = 0 if quality >= 90 else 2
    assert JpegImagePlugin.get_sampling(im_r) == expected_sub, f"{label}: sampling mismatch"
    sampling = _sof_sampling(rust_jpg)
    if quality >= 90:
        assert sampling == [(1, 1), (1, 1), (1, 1)], f"{label}: SOF {sampling}"
    else:
        assert sampling == [(2, 2), (1, 1), (1, 1)], f"{label}: SOF {sampling}"

    icc_r = im_r.info.get("icc_profile")
    icc_ref = srgb_icc()
    assert isinstance(icc_r, bytes) and len(icc_r) == len(icc_ref) == 588
    # LittleCMS embeds current UTC wall-clock dateTimeNumber in bytes 24..36; all other 576 bytes are identical.
    assert icc_r[:24] == icc_ref[:24] and icc_r[36:] == icc_ref[36:], f"{label}: ICC non-timestamp bytes differ"
    prof = ImageCms.ImageCmsProfile(io.BytesIO(icc_r))
    assert ImageCms.getProfileDescription(prof) == ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc_ref)))

    assert getattr(im_r, "quantization", None) == getattr(im_p, "quantization", None), f"{label} q={quality}: DQT tables differ"

    # Gate B measurements
    arr_r = np.asarray(im_r.convert("RGB"), dtype=np.int16)
    arr_p = np.asarray(im_p.convert("RGB"), dtype=np.int16)
    diff = np.abs(arr_r - arr_p)
    mean_per_ch = [float(diff[..., c].mean()) for c in range(3)]
    max_per_ch = [int(diff[..., c].max()) for c in range(3)]
    gt4_count = int((diff.max(axis=2) > 4).sum())
    total_pixels = int(diff.shape[0] * diff.shape[1])
    return mean_per_ch, max_per_ch, gt4_count, total_pixels


@pytest.mark.parametrize("quality", [QUALITY_TIERS["high"], QUALITY_TIERS["medium"], QUALITY_TIERS["low"], 90, 89, 100, 1])
def test_structure_and_picture_synthetic(quality: int, tmp_path: Path) -> None:
    require_cli()
    base = {270: "GXR Studio neutral-preview", 271: "RICOH", 272: "GXR A12", 306: "2026:10:16 18:51:53"}
    sub = {36867: "2026:10:16 18:51:53", 34855: 200, 42036: "GR LENS A12 50mm F2.5 MACRO"}
    for name, rgb in _synthetic_images():
        rust_jpg = _cli_encode(rgb, quality, base, sub, tmp_path, f"{name}_q{quality}")
        py_jpg = _python_single_encode(rgb, quality, base, sub)
        mean_ch, max_ch, gt4_count, total_px = _assert_structure_and_measure_picture(rgb, quality, rust_jpg, py_jpg, name)
        print(f"synthetic {name} q={quality}: mean={[round(m, 5) for m in mean_ch]} max={max_ch} gt4={gt4_count}/{total_px}")


def test_real_exported_arrays_gate_a_and_b(tmp_path: Path) -> None:
    require_cli()
    if len(REAL_DNGS) < _REAL_EXPORT_COUNT:
        if os.environ.get("GXR_REQUIRE_CORE") == "1":
            pytest.fail(f"need at least {_REAL_EXPORT_COUNT} real DNGs under {CAPTURES}, found {len(REAL_DNGS)}")
        pytest.skip(f"need {_REAL_EXPORT_COUNT} real DNGs under {CAPTURES}")

    for idx, dng_path in enumerate(REAL_DNGS[:_REAL_EXPORT_COUNT]):
        dng = open_dng(dng_path)
        try:
            rgb = render_full(dng, NEUTRAL)
        finally:
            dng.release()
        with Image.open(dng_path) as src:
            ex = src.getexif()
            base = {k: ex[k] for k in (271, 272, 306, 33432) if k in ex}
            base[270] = "GXR Studio neutral-preview: white-balanced linear sensor data to sRGB, no recipe applied"
            sub = {k: v for k, v in ex.get_ifd(0x8769).items() if k not in _DROP_EXIF_TAGS}
        for quality in (95, 85, 70):
            rust_jpg = _cli_encode(rgb, quality, base, sub, tmp_path, f"real{idx}_q{quality}")
            py_jpg = _python_single_encode(rgb, quality, base, sub)
            label = _real_id(dng_path)
            mean_ch, max_ch, gt4_count, total_px = _assert_structure_and_measure_picture(rgb, quality, rust_jpg, py_jpg, label)
            gt4_frac = gt4_count / total_px
            print(
                f"real[{idx}] {label} q={quality}: mean={[round(m, 5) for m in mean_ch]} "
                f"max={max_ch} gt4={gt4_count}/{total_px} ({gt4_frac:.6e})"
            )
            _assert_mean_within_bound(mean_ch, 0.5, label, quality)
            if quality >= 85:
                assert max(max_ch) <= 4, f"{label} q={quality}: channel max abs diff {max_ch} > 4"
            else:
                assert gt4_frac <= 1e-4, f"{label} q={quality}: fraction > 4 ({gt4_frac:.6e}) > 1e-4"
                assert max(max_ch) <= 16, f"{label} q={quality}: channel max abs diff {max_ch} > 16"
            _assert_exif_identical(rust_jpg, py_jpg)


def test_real_cropped_non16_arrays_gate_a_and_b(tmp_path: Path) -> None:
    """F2 regression: real arrays cropped to non-16-aligned odd and even sizes at all 3 tiers."""
    require_cli()
    if len(REAL_DNGS) < 3:
        if os.environ.get("GXR_REQUIRE_CORE") == "1":
            pytest.fail(f"need at least 3 real DNGs under {CAPTURES}, found {len(REAL_DNGS)}")
        pytest.skip(f"need 3 real DNGs under {CAPTURES}")

    crop_specs = [
        (0, 286, 430),  # even, non-16-aligned
        (1, 287, 431),  # odd, non-16-aligned
        (2, 214, 311),  # mixed even height, odd width, non-16-aligned
    ]
    for dng_idx, crop_h, crop_w in crop_specs:
        dng_path = REAL_DNGS[dng_idx]
        dng = open_dng(dng_path)
        try:
            rgb = np.ascontiguousarray(render_full(dng, NEUTRAL)[:crop_h, :crop_w])
        finally:
            dng.release()
        base = {270: "GXR Studio neutral-preview", 271: "RICOH", 272: "GXR"}
        sub = {34855: 200}
        label = f"{_real_id(dng_path)}[{crop_h}x{crop_w}]"
        for quality in (95, 85, 70):
            rust_jpg = _cli_encode(rgb, quality, base, sub, tmp_path, f"crop{dng_idx}_{crop_h}x{crop_w}_q{quality}")
            py_jpg = _python_single_encode(rgb, quality, base, sub)
            mean_ch, max_ch, gt4_count, total_px = _assert_structure_and_measure_picture(rgb, quality, rust_jpg, py_jpg, label)
            gt4_frac = gt4_count / total_px
            print(
                f"real_crop[{dng_idx}] {label} q={quality}: mean={[round(m, 5) for m in mean_ch]} "
                f"max={max_ch} gt4={gt4_count}/{total_px} ({gt4_frac:.6e})"
            )
            _assert_mean_within_bound(mean_ch, 0.5, label, quality)
            if quality >= 85:
                assert max(max_ch) <= 4, f"{label} q={quality}: channel max abs diff {max_ch} > 4"
            else:
                assert gt4_frac <= 1e-4, f"{label} q={quality}: fraction > 4 ({gt4_frac:.6e}) > 1e-4"
                assert max(max_ch) <= 16, f"{label} q={quality}: channel max abs diff {max_ch} > 16"


def _assert_mean_within_bound(mean_ch: list[float], bound: float, label: str, quality: int) -> None:
    for ch, m in enumerate(mean_ch):
        assert math.isfinite(m), f"{label} q={quality}: channel {ch} mean abs diff is non-finite ({m})"
        assert m <= bound, f"{label} q={quality}: channel {ch} mean abs diff {m} > {bound}"


def _assert_exif_identical(rust_jpg: bytes, py_jpg: bytes) -> None:
    ex_r = Image.open(io.BytesIO(rust_jpg)).getexif()
    ex_p = Image.open(io.BytesIO(py_jpg)).getexif()
    assert dict(ex_r) == dict(ex_p)
    assert dict(ex_r.get_ifd(0x8769)) == dict(ex_p.get_ifd(0x8769))


def test_exif_map_tags_encodings_and_fallback(tmp_path: Path) -> None:
    require_cli()
    rgb = np.full((32, 48, 3), 120, dtype=np.uint8)

    # Full EXIF: UTF-8 description, latin-1 copyright, orientation override, dropped MakerNote/GPS, rationals
    base: dict[int, Any] = {
        270: "电脑预测 · 未校准 · 近似",
        271: "RICOH",
        272: "GXR A12",
        274: 6,  # must be forced to 1
        305: "Old Software",  # must be forced to "GXR Studio"
        306: "2026:10:16 18:51:53",
        33432: "© 2026 GXR Studio\xa0Test",
        34853: 999,  # GPS pointer must be dropped
    }
    sub: dict[int, Any] = {
        36864: b"0230",
        36867: "2026:10:16 18:51:53",
        36868: "2026:10:16 18:51:54",
        34855: 400,
        42036: "GR LENS A12 50mm F2.5 MACRO",
        33434: IFDRational(1, 125),
        33437: IFDRational(25, 10),
        37380: IFDRational(-1, 3),
        37386: IFDRational(33, 1),
        37500: b"RICOH_MAKERNOTE_BYTES",  # must be dropped
    }
    rust_jpg = _cli_encode(rgb, 95, base, sub, tmp_path, "full_exif")
    py_jpg = _python_single_encode(rgb, 95, base, sub)
    _assert_exif_identical(rust_jpg, py_jpg)

    ex_r = Image.open(io.BytesIO(rust_jpg)).getexif()
    assert ex_r[274] == 1
    assert ex_r[305] == "GXR Studio"
    assert ex_r[270].encode("latin-1").decode("utf-8") == "电脑预测 · 未校准 · 近似"
    assert ex_r[33432] == "© 2026 GXR Studio\xa0Test"
    assert 34853 not in ex_r
    sub_r = ex_r.get_ifd(0x8769)
    assert 37500 not in sub_r
    assert (sub_r[40962], sub_r[40963]) == (48, 32)

    # Reduced EXIF fallback when full EXIF > 65533 bytes
    huge_sub = {**sub, 37510: b"X" * 66000}
    rust_fb = _cli_encode(rgb, 95, base, huge_sub, tmp_path, "fallback_exif")
    py_fb = _python_single_encode(rgb, 95, base, huge_sub)
    _assert_exif_identical(rust_fb, py_fb)
    assert 37510 not in Image.open(io.BytesIO(rust_fb)).getexif().get_ifd(0x8769)


def _py_set_meta(data: bytes, exif_bytes: bytes | None, icc_bytes: bytes | None) -> bytes:
    """Python reference for ``jpeg-set-meta`` using ``hdr.container.jpeg_segments``."""
    segs, sos, eoi = container.jpeg_segments(data)
    keep_app0: list[bytes] = []
    keep_app_other: list[bytes] = []
    keep_rest: list[bytes] = []
    for s in segs:
        p = s.payload(data)
        if exif_bytes is not None and s.marker == 0xE1 and p.startswith(b"Exif\x00\x00"):
            continue
        if icc_bytes is not None and s.marker == 0xE2 and p.startswith(b"ICC_PROFILE\x00"):
            continue
        raw = data[s.start : s.end]
        if s.marker == 0xE0:
            keep_app0.append(raw)
        elif 0xE1 <= s.marker <= 0xEF:
            keep_app_other.append(raw)
        else:
            keep_rest.append(raw)
    exif_seg = container.app_segment(0xE1, exif_bytes) if exif_bytes is not None else b""
    icc_seg = b""
    if icc_bytes:
        icc_seg = container.app_segment(0xE2, b"ICC_PROFILE\x00\x01\x01" + icc_bytes)
    return b"\xff\xd8" + b"".join(keep_app0) + exif_seg + icc_seg + b"".join(keep_app_other) + b"".join(keep_rest) + data[sos:eoi]


def test_container_segments_and_set_meta_byte_exact(tmp_path: Path) -> None:
    require_cli()
    rgb = _synthetic_images()[0][1]

    # 1. Baseline JPEG + progressive JPEG + JPEG with extra XMP/APP14
    buf_base = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf_base, "JPEG", quality=88)
    buf_prog = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf_prog, "JPEG", quality=92, progressive=True)
    xmp_payload = b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta/>"
    with_xmp = _py_set_meta(buf_base.getvalue(), b"Exif\x00\x00MM\x00*\x00\x00\x00\x08\x00\x00\x00\x00\x00\x00", srgb_icc())
    segs0, sos0, eoi0 = container.jpeg_segments(with_xmp)
    with_xmp = with_xmp[: with_xmp.find(b"\xff\xdb")] + container.app_segment(0xE1, xmp_payload) + with_xmp[with_xmp.find(b"\xff\xdb") :]

    for idx, jpg_bytes in enumerate((buf_base.getvalue(), buf_prog.getvalue(), with_xmp)):
        jpg_path = tmp_path / f"seg_{idx}.jpg"
        jpg_path.write_bytes(jpg_bytes)
        py_segs, py_sos, py_eoi = container.jpeg_segments(jpg_bytes)
        code, payload = run_json(["jpeg-segments", "--in", str(jpg_path)])
        assert error_of(code, payload) is None
        assert payload["sos"] == py_sos and payload["eoi"] == py_eoi
        # Compare all segments before SOS
        got_before_sos = payload["segments"][:-1]
        assert len(got_before_sos) == len(py_segs)
        for g, p in zip(got_before_sos, py_segs):
            assert g["marker"] == p.marker
            assert g["offset"] == p.start
            assert g["length"] == p.end - p.start - 2
        assert payload["segments"][-1]["marker"] == 0xDA
        assert payload["segments"][-1]["offset"] == py_sos

        # Now test jpeg-set-meta replacing EXIF + ICC while preserving XMP and scan bytes
        base = {270: "Test set-meta", 271: "RICOH", 272: "GXR"}
        sub = {34855: 200}
        exif_path = tmp_path / f"meta_{idx}.json"
        exif_path.write_text(_exif_to_json(base, sub), encoding="utf-8")
        out_path = tmp_path / f"set_meta_{idx}.jpg"
        code, res = run_json(["jpeg-set-meta", "--in", str(jpg_path), "--exif", str(exif_path), "--icc", "--out", str(out_path)])
        assert error_of(code, res) is None
        got_bytes = out_path.read_bytes()

        # Build exact Python EXIF bytes via Pillow for the same metadata + image dimensions
        im_src = Image.open(io.BytesIO(jpg_bytes))
        ex = Image.Exif()
        ex[270] = b"Test set-meta"
        ex[271] = "RICOH"
        ex[272] = "GXR"
        ex[274] = 1
        ex[305] = SOFTWARE
        ex_sub = ex.get_ifd(0x8769)
        ex_sub[34855] = 200
        ex_sub[40962], ex_sub[40963] = im_src.width, im_src.height
        # Note: Pillow's srgb_icc differs in timestamp bytes 24..36, so compare segments & scan with ICC from Rust
        im_out = Image.open(io.BytesIO(got_bytes))
        got_icc = im_out.info["icc_profile"]
        ref_bytes = _py_set_meta(jpg_bytes, ex.tobytes(), got_icc)
        assert got_bytes == ref_bytes
        assert got_bytes[-(py_eoi - py_sos) :] == jpg_bytes[py_sos:py_eoi]
        assert xmp_payload in got_bytes if idx == 2 else True


def test_error_paths(tmp_path: Path) -> None:
    require_cli()
    rgb = np.zeros((16, 16, 3), dtype=np.uint8)
    for bad_q in (0, 101, -5):
        with pytest.raises(CoreCliError) as exc:
            _cli_encode(rgb, bad_q, {}, {}, tmp_path, f"badq_{bad_q}")
        assert exc.value.kind == "bad_quality"

    # Bad EXIF JSON + JSON nesting depth cap (128 allowed, 129+ -> json / "JSON 嵌套太深")
    npy = tmp_path / "e.npy"
    np.save(npy, rgb)
    bad_exif = tmp_path / "bad.json"
    bad_exif.write_text("{not json", encoding="utf-8")
    code, payload = run_json(["jpeg-encode", "--in", str(npy), "--quality", "90", "--exif", str(bad_exif), "--out", str(tmp_path / "o.jpg")])
    err = error_of(code, payload)
    assert err is not None and err.kind == "bad_exif"

    for tag, json_text in (
        ("arr_129", "[" * 129 + "]" * 129),
        ("obj_129", '{"a":' * 128 + "{}" + "}" * 128),
        ("arr_20000", "[" * 20_000 + "]" * 20_000),
        ("obj_20000", '{"a":' * 20_000 + "{}" + "}" * 20_000),
    ):
        deep_path = tmp_path / f"{tag}.json"
        deep_path.write_text(json_text, encoding="utf-8")
        code, payload = run_json(
            ["jpeg-encode", "--in", str(npy), "--quality", "90", "--exif", str(deep_path), "--out", str(tmp_path / f"{tag}.jpg")]
        )
        err = error_of(code, payload)
        assert err is not None and err.kind == "json" and err.message == "JSON 嵌套太深", f"{tag}: got {payload}"

    # Non-latin1 copyright when reduced fallback also has it
    with pytest.raises(CoreCliError) as exc:
        _cli_encode(rgb, 90, {33432: "版权"}, {}, tmp_path, "non_latin1")
    assert exc.value.kind == "encode_failed"

    # Broken JPEG containers
    valid_buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(valid_buf, "JPEG", quality=85)
    valid = valid_buf.getvalue()
    broken_cases = [
        ("not_jpeg", b"not a jpeg"),
        ("truncated", valid[: len(valid) // 2]),
        ("bad_seg", valid[:2] + b"\xff\xe1\x00"),
        ("bad_len", valid[:2] + b"\xff\xe1\x00\x01"),
        ("no_eoi", valid[:-2]),
    ]
    for name, payload_bytes in broken_cases:
        p = tmp_path / f"{name}.jpg"
        p.write_bytes(payload_bytes)
        code, res = run_json(["jpeg-segments", "--in", str(p)])
        err = error_of(code, res)
        assert err is not None and err.kind == "container", f"{name}: got {res}"


def test_wasm_matches_native_jpeg(tmp_path: Path) -> None:
    specs: list[Path] = []
    for idx, (name, rgb) in enumerate(_synthetic_images()[:2]):
        npy = tmp_path / f"w_{idx}.npy"
        np.save(npy, rgb)
        exif = tmp_path / f"w_{idx}.exif.json"
        exif.write_text(
            _exif_to_json(
                {270: "电脑预测 · WASM", 271: "RICOH", 272: "GXR", 33432: "© 2026\xa0WASM"},
                {36867: "2026:11:13 10:00:00", 34855: 200, 33434: IFDRational(1, 250)},
            ),
            encoding="utf-8",
        )
        for q in (95, 85, 70):
            spec = tmp_path / f"spec_{idx}_{q}.json"
            spec.write_text(
                json.dumps({"npy": str(npy), "exif": str(exif), "quality": q, "out": str(tmp_path / f"out_{idx}_{q}.jpg")}),
                encoding="utf-8",
            )
            specs.append(spec)

    # Error cases: bad quality, bad exif json, and raw buffer/dimension validation (F3: 65535x65535 & 65535x21846)
    bad_exif = tmp_path / "bad_wasm.exif.json"
    bad_exif.write_text("{broken", encoding="utf-8")
    for tag, q, ex_path in (("bad_q0", 0, tmp_path / "w_0.exif.json"), ("bad_q101", 101, tmp_path / "w_0.exif.json"), ("bad_ex", 90, bad_exif)):
        spec = tmp_path / f"spec_{tag}.json"
        spec.write_text(
            json.dumps({"npy": str(tmp_path / "w_0.npy"), "exif": str(ex_path), "quality": q, "out": str(tmp_path / f"out_{tag}.jpg")}),
            encoding="utf-8",
        )
        specs.append(spec)

    raw_error_cases = [
        ("empty_65535x65535", 65535, 65535, b""),
        ("empty_65535x21846", 65535, 21846, b""),
        ("zero_0x0", 0, 0, b""),
        ("too_wide_65536x1", 65536, 1, b""),
        ("too_high_1x65536", 1, 65536, b""),
        ("short_1x1", 1, 1, b"\x01\x02"),
        ("long_1x1", 1, 1, b"\x01\x02\x03\x04"),
    ]
    for tag, w_val, h_val, raw_bytes in raw_error_cases:
        raw_path = tmp_path / f"raw_{tag}.bin"
        raw_path.write_bytes(raw_bytes)
        spec = tmp_path / f"spec_{tag}.json"
        spec.write_text(
            json.dumps(
                {
                    "raw": str(raw_path),
                    "width": w_val,
                    "height": h_val,
                    "quality": 85,
                    "out": str(tmp_path / f"out_{tag}.jpg"),
                }
            ),
            encoding="utf-8",
        )
        specs.append(spec)

    # JSON nesting depth cases on both sides of the 128 limit (arrays and objects)
    json_depth_cases = [
        ("depth_arr_128", "[" * 128 + "1" + "]" * 128),
        ("depth_arr_129", "[" * 129 + "]" * 129),
        ("depth_arr_20000", "[" * 20_000 + "]" * 20_000),
        ("depth_obj_128", '{"a":' * 127 + '{"b":1}' + "}" * 127),
        ("depth_obj_129", '{"a":' * 128 + "{}" + "}" * 128),
        ("depth_obj_20000", '{"a":' * 20_000 + "{}" + "}" * 20_000),
    ]
    for tag, json_text in json_depth_cases:
        ex_path = tmp_path / f"{tag}.exif.json"
        ex_path.write_text(json_text, encoding="utf-8")
        spec = tmp_path / f"spec_{tag}.json"
        spec.write_text(
            json.dumps({"npy": str(tmp_path / "w_0.npy"), "exif": str(ex_path), "quality": 85, "out": str(tmp_path / f"out_{tag}.jpg")}),
            encoding="utf-8",
        )
        specs.append(spec)

    proc = run_area("jpeg", specs, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_mean_bound_rejects_nan_and_inf() -> None:
    _assert_mean_within_bound([0.0, 0.25, 0.5], 0.5, "ok", 95)
    for bad in ([0.0, float("nan"), 0.0], [float("inf"), 0.0, 0.0], [0.0, 0.0, float("-inf")]):
        with pytest.raises(AssertionError):
            _assert_mean_within_bound(bad, 0.5, "injected", 95)
