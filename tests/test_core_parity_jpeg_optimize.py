"""Parity: ``gxr-cli jpeg-encode --optimize`` (``gxr_jpeg::encode_jpeg_with(.., optimize = true)``)
against desktop ``export.py:_finish``, which saves with Pillow 12.2.0 ``optimize=True``.

Gates (UNIT-jpeg-optimize.md), all on whole files, byte for byte:
- synthetic pictures from 1x1 up (odd and 16-aligned sizes), at 12 qualities from 1 to 100, so
  both chroma rules (4:4:4 at >= 90, 4:2:0 below);
- EXIF rationals as Pillow writes them: ``_limit_rational``, ``_limit_signed_rational`` and its
  RATIONAL/SRATIONAL inference, zero denominators included, and the reduced-EXIF fallback when
  Pillow can't write a value;
- at least 20 real photos (``GXR_STUDIO_TEST_PARENT``) at 95/85/70 with their own EXIF, plus one
  tier each without source EXIF and without the ICC profile;
- WASM ``jpeg_encode(.., optimize = true)`` against the CLI, errors included.

Real photos follow the export-pipeline privacy rules (docs/mobile.md, 测试输出里不出现照片数据):
every comparison goes through the redacted helpers. The encoder's hostile cases (unused tables,
all-zero counts, out-of-range coefficients) are Rust unit tests in ``jpeg_enc/optimize.rs``: an
encoder has no input JPEG to damage.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from gxr_studio import export as export_mod
from gxr_studio.export import ExportError, ExportOptions, _finish, source_exif, srgb_icc
from gxr_studio.finish.compose import load_jpeg
from support.core_cli import require_cli, run_json
from support.export_pipeline_cases import (
    anon_ids,
    check,
    cli_ok,
    exif_val_json,
    note,
    produced,
    real_pairs,
    same_file_redacted,
    same_kind,
)
from support.samples import missing
from support.wasm_check import run_area

QUALITIES = (1, 5, 10, 25, 50, 70, 75, 85, 89, 90, 95, 100)
TIERS = (95, 85, 70)
REAL_COUNT = 20
SIZES = [(1, 1), (1, 2), (2, 1), (2, 2), (3, 5), (7, 9), (8, 8), (9, 8), (15, 17), (16, 16), (17, 16), (31, 33), (97, 131)]


@pytest.fixture(autouse=True)
def _cli() -> None:
    require_cli()


@pytest.fixture
def pinned_icc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> bytes:
    """LittleCMS writes the creation time into ICC bytes 24..36; gxr-jpeg fixes them. Check that
    this is the only difference, then give the desktop code the fixed profile."""
    np.save(tmp_path / "icc.npy", np.zeros((8, 8, 3), dtype=np.uint8))
    code, payload = run_json(["jpeg-encode", "--in", str(tmp_path / "icc.npy"), "--quality", "95", "--out", str(tmp_path / "icc.jpg")])
    cli_ok(code, payload, "jpeg-encode for the ICC pin")
    with Image.open(tmp_path / "icc.jpg") as im:
        rs = bytes(im.info["icc_profile"])
    py = srgb_icc()
    check(len(rs) == len(py) == 588, "ICC profiles are not 588 bytes")
    check(rs[:24] == py[:24] and rs[36:] == py[36:], "ICC profiles differ outside bytes 24..36")
    monkeypatch.setattr(export_mod, "_ICC", rs)
    return rs


def _exif_json(base: dict[int, Any], sub: dict[int, Any]) -> str:
    return json.dumps(
        {"base": {str(k): exif_val_json(v) for k, v in base.items()}, "exif": {str(k): exif_val_json(v) for k, v in sub.items()}},
        ensure_ascii=False,
    )


def _desktop(rgb: np.ndarray, quality: int, base: dict[int, Any], sub: dict[int, Any]) -> bytes:
    """``_finish`` itself, the way ``write_rgb_with_exif`` calls it."""
    data, _size = _finish(Image.fromarray(np.ascontiguousarray(rgb), "RGB"), base, sub, None, ExportOptions(quality=quality, fmt="jpeg"))
    return data


def _encode(tmp: Path, tag: str, rgb: np.ndarray, quality: int, exif_json: str) -> tuple[bytes | None, Any]:
    npy, ex, out = tmp / f"{tag}.npy", tmp / f"{tag}.exif.json", tmp / f"{tag}.jpg"
    np.save(npy, np.ascontiguousarray(rgb, dtype=np.uint8))
    ex.write_text(exif_json, encoding="utf-8")
    code, payload = run_json(["jpeg-encode", "--in", str(npy), "--quality", str(quality), "--exif", str(ex), "--optimize", "--out", str(out)])
    if code != 0:
        return None, payload
    return out.read_bytes(), payload


def _synthetic(h: int, w: int, seed: int) -> list[tuple[str, np.ndarray]]:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    grad = np.stack([xx * 255 // max(w - 1, 1), yy * 255 // max(h - 1, 1), (xx * 7 + yy * 3) % 256], axis=-1).astype(np.uint8)
    return [
        ("noise", rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)),
        ("grad", grad),
        ("flat", np.full((h, w, 3), (30, 140, 220), dtype=np.uint8)),
    ]


SYN_BASE: dict[int, Any] = {270: "GXR Studio · 测试", 271: "RICOH", 272: "GXR A12", 306: "2026:11:16 09:00:00", 33432: "© test"}
SYN_SUB: dict[int, Any] = {
    36867: "2026:11:16 09:00:00",
    34855: 200,
    42036: "A12 50mm",
    33434: IFDRational(10, 2500),
    33437: IFDRational(28, 10),
    37380: IFDRational(-3, 10),
}


@pytest.mark.parametrize("quality", QUALITIES, ids=anon_ids(len(QUALITIES)))
def test_whole_file_synthetic(quality: int, tmp_path: Path, pinned_icc: bytes) -> None:
    count = 0
    for h, w in SIZES:
        for kind, rgb in _synthetic(h, w, h * 1000 + w):
            got, payload = _encode(tmp_path, f"{kind}{h}x{w}", rgb, quality, _exif_json(SYN_BASE, SYN_SUB))
            produced(got, payload, "jpeg-encode --optimize")
            same_file_redacted(got, _desktop(rgb, quality, SYN_BASE, SYN_SUB), f"{kind} {h}x{w} q{quality}")
            count += 1
    note(f"jpeg_optimize_synthetic_q{quality}", count)


def _same_outcome(tmp: Path, tag: str, rgb: np.ndarray, quality: int, base: dict[int, Any], sub: dict[int, Any]) -> bytes | None:
    """The desktop's file, or its ``encode_failed``, and the same from gxr-jpeg."""
    try:
        want: bytes | None = _desktop(rgb, quality, base, sub)
    except ExportError as exc:
        check(exc.code == "encode_failed", "desktop error is not encode_failed")
        want = None
    got, payload = _encode(tmp, tag, rgb, quality, _exif_json(base, sub))
    if want is None:
        check(got is None, "desktop failed, gxr-jpeg wrote a file")
        same_kind(payload, "encode_failed", "Pillow buffer error")
    else:
        same_file_redacted(got, want, tag)
    return want


def _partial_noise(h: int, w: int, rows: int, seed: int) -> np.ndarray:
    rgb = np.full((h, w, 3), 128, dtype=np.uint8)
    rgb[:rows] = np.random.default_rng(seed).integers(0, 256, size=(rows, w, 3), dtype=np.uint8)
    return rgb


def test_pillow_buffer_limit(tmp_path: Path, pinned_icc: bytes) -> None:
    """Pillow sizes one buffer for the whole ``optimize=True`` file (``k*w*h`` plus the markers,
    at least 64 KiB); a file that doesn't fit fails in ``_finish`` ("Suspension not allowed
    here"), first with the full EXIF, then with the reduced one. gxr-jpeg gives the same outcome.
    Long codes too: noise at q100 uses most of the 256 AC symbols per table."""
    noise = np.random.default_rng(16).integers(0, 256, size=(320, 480, 3), dtype=np.uint8)
    outcomes = []
    for quality in (100, 95, 94, 90, 50):
        outcomes.append(_same_outcome(tmp_path, f"noise{quality}", noise, quality, {}, {}) is None)
    check(outcomes[0] and not outcomes[-1], "q100 noise must fail and q50 must fit on the desktop")
    # Near the edge: more and more noisy rows, so the file crosses the buffer size.
    crossed = set()
    for rows in range(150, 321, 10):
        crossed.add(_same_outcome(tmp_path, f"rows{rows}", _partial_noise(320, 480, rows, rows), 92, {}, {}) is None)
    check(crossed == {True, False}, "the row sweep must cross the buffer limit")


def test_pillow_buffer_falls_back_to_reduced_exif(tmp_path: Path, pinned_icc: bytes) -> None:
    """In the 64 KiB-minimum regime a smaller EXIF leaves more room: the full EXIF (with a 20 kB
    comment) doesn't fit, the reduced one does, and both sides write the reduced file."""
    sub = {**SYN_SUB, 37510: b"ASCII\x00\x00\x00" + b"x" * 20000}
    fell_back = 0
    for rows in (100, 110, 120, 130, 140):
        want = _same_outcome(tmp_path, f"fb{rows}", _partial_noise(160, 160, rows, rows), 100, SYN_BASE, sub)
        if want is not None and 37510 not in Image.open(io.BytesIO(want)).getexif().get_ifd(0x8769):
            fell_back += 1
    check(fell_back > 0, "no case used the reduced EXIF")
    note("jpeg_optimize_reduced_exif_fallbacks", fell_back)


R = IFDRational
# (sub-IFD tags) → each written as Pillow does. Tag 37380 has no type in the table, so the value
# decides: SRATIONAL when one is < 0. Tag 41486 likewise.
RATIONAL_CASES: list[dict[int, Any]] = [
    {37380: R(0, 0)},
    {37380: R(5, 0)},
    {37380: R(0, -3)},
    {37380: R(-1, 2)},
    {37380: R(-28, 10)},
    {37380: R(6, -4)},
    {37380: R(-6, -4)},
    {37380: (R(1, 0), R(-1, 2))},
    {37380: (R(-1, 2), R(1, 0))},
    {37380: (R(1, 0), R(1, 2))},
    {37380: (R(0, 0), R(-6, 4))},
    {37380: (R(-5, 0), R(-1, 2))},
    {37380: R(-(1 << 40), 3)},
    {37380: R(-1, 1 << 40)},
    {37380: R(3, -(1 << 33))},
    {37380: R(1 << 40, 3)},
    {37380: R(3, 1 << 40)},
    {37380: R((1 << 40) + 1, (1 << 33) + 7)},
    {37380: (R(-1, 2), R(5, 1 << 40))},
    {37380: (R(-1, 2), R((1 << 40) + 3, 7))},
    {41486: R(4288000, 23400), 41487: R(2848000, 15600)},
    # Pillow can't write these (struct.error / ZeroDivisionError); _finish falls back to the
    # reduced EXIF, and so must gxr-jpeg.
    {37380: R(-5, 0)},
    {37380: (R(-1, 2), R(1 << 40, 0))},
    {37380: (R(-1, 2), R(-(1 << 40), 0))},
]
BASE_RATIONAL_CASES: list[dict[int, Any]] = [
    {282: 300, 283: 72},
    {282: 72.5, 283: 0.1},
    {282: R(720000, 10000), 283: R(1, 3)},
    {282: 1 << 40},
    {282: 0.0},
]


@pytest.mark.parametrize("sub_extra", RATIONAL_CASES, ids=anon_ids(len(RATIONAL_CASES)))
def test_exif_rationals_like_pillow(sub_extra: dict[int, Any], tmp_path: Path, pinned_icc: bytes) -> None:
    rgb = np.full((8, 8, 3), 90, dtype=np.uint8)
    sub = {**SYN_SUB, **sub_extra}
    got, payload = _encode(tmp_path, "rat", rgb, 85, _exif_json(SYN_BASE, sub))
    produced(got, payload, "jpeg-encode --optimize")
    same_file_redacted(got, _desktop(rgb, 85, SYN_BASE, sub), "rational case")


@pytest.mark.parametrize("base_extra", BASE_RATIONAL_CASES, ids=anon_ids(len(BASE_RATIONAL_CASES)))
def test_exif_base_rationals_like_pillow(base_extra: dict[int, Any], tmp_path: Path, pinned_icc: bytes) -> None:
    """Base tags typed RATIONAL by the tag table: ints and floats go through ``float`` and
    ``_limit_rational`` as Pillow does."""
    rgb = np.full((8, 8, 3), 90, dtype=np.uint8)
    base = {**SYN_BASE, **base_extra}
    got, payload = _encode(tmp_path, "brat", rgb, 95, _exif_json(base, SYN_SUB))
    produced(got, payload, "jpeg-encode --optimize")
    same_file_redacted(got, _desktop(rgb, 95, base, SYN_SUB), "base rational case")


_EDGES = [
    -(1 << 63),
    -(1 << 40),
    -(1 << 32),
    -(1 << 31) - 1,
    -(1 << 31),
    -1,
    0,
    1,
    (1 << 31) - 1,
    1 << 31,
    (1 << 32) - 1,
    1 << 32,
    1 << 40,
    (1 << 63) - 1,
]
_FLOATS = [0.0, -0.0, 0.1, 72.5, 1e-20, 1e-22, 1e-23, 1e-24, 1e-30, 1e-100, 1e-300, 5e-324]
_FLOATS += [1e20, 1e22, 1e23, 1e24, 1e30, 1e100, 1e300]
_MIXED = [(R(1, 0), R(-1, 2)), (R(-1, 2), R(1 << 40, 0)), (R(-5, 0), R(-1, 2)), (R(0, -9), R(-7, -3))]


def _sol_r1_cases() -> list[tuple[int, dict[int, Any], dict[int, Any]]]:
    """Sol r1's 415 rational cases: every i64 edge pair in a base and a sub tag, floats from
    5e-324 to 1e300 in XResolution (with exposure and aperture beside it), mixed arrays."""
    cases: list[tuple[int, dict[int, Any], dict[int, Any]]] = []
    for n in _EDGES:
        for d in _EDGES:
            cases.append((85, {282: R(n, d)}, {}))
            cases.append((85, {}, {37380: R(n, d)}))
    for value in _FLOATS:
        cases.append((95, {282: value}, {33434: R(1, 250), 33437: R(28, 10)}))
    for values in _MIXED:
        cases.append((90, {}, {37380: values}))
    return cases


def test_sol_r1_rational_sweep(tmp_path: Path, pinned_icc: bytes) -> None:
    """Whole files (or the same encode_failed) against _finish for all 415 cases; 11 of them
    (|x| from 1e-23 down and from 1e23 up) used to drop to the reduced EXIF."""
    rgb = np.full((3, 5, 3), (17, 63, 199), dtype=np.uint8)
    cases = _sol_r1_cases()
    check(len(cases) == 415, "Sol r1 has 415 rational cases")
    files = 0
    for i, (quality, base, sub) in enumerate(cases):
        files += _same_outcome(tmp_path, f"sol{i}", rgb, quality, base, sub) is not None
    note("jpeg_optimize_sol_r1_cases", len(cases))
    note("jpeg_optimize_sol_r1_files", files)


@pytest.mark.parametrize("value", [1e-300, -1e-300, 1e300, -1e300, 1e-23, 1e23, 5e-324], ids=anon_ids(7))
def test_sol_r1_tiny_and_huge_float_keep_the_full_exif(value: float, tmp_path: Path, pinned_icc: bytes) -> None:
    """Sol r1's 5x3 file: Pillow writes XResolution 0/1 (1/0 via the reciprocal) and keeps
    ExposureTime and FNumber; so must gxr-jpeg."""
    rgb = np.full((3, 5, 3), (17, 63, 199), dtype=np.uint8)
    sub = {33434: R(1, 250), 33437: R(28, 10)}
    want = _same_outcome(tmp_path, "tiny", rgb, 95, {282: value}, sub)
    check(want is not None, "the desktop writes this file")
    exif = Image.open(io.BytesIO(want or b"")).getexif()
    check(282 in exif and {33434, 33437} <= set(exif.get_ifd(0x8769)), "the full EXIF is kept")
    xres = exif[282]
    check((xres.numerator, xres.denominator) == ((0, 1) if abs(value) < 1 else (1, 0)), "XResolution 0/1 or 1/0")


def test_exif_that_neither_side_can_write(tmp_path: Path, pinned_icc: bytes) -> None:
    """A bad value in a tag the reduced EXIF keeps too: Pillow fails both tries, gxr-jpeg too."""
    rgb = np.full((8, 8, 3), 90, dtype=np.uint8)
    sub = {**SYN_SUB, 34855: R(-5, 0)}
    with pytest.raises(ExportError):
        _desktop(rgb, 85, SYN_BASE, sub)
    got, payload = _encode(tmp_path, "nope", rgb, 85, _exif_json(SYN_BASE, sub))
    check(got is None, "expected an error")
    same_kind(payload, "encode_failed", "unwritable EXIF")


def _strip(data: bytes, marker: int, prefix: bytes) -> bytes:
    """``data`` without its APPn segments of ``marker`` whose payload starts with ``prefix``."""
    out, at = bytearray(data[:2]), 2
    while data[at + 1] != 0xDA:
        length = int.from_bytes(data[at + 2 : at + 4], "big")
        seg = data[at : at + 2 + length]
        if not (data[at + 1] == marker and seg[4:].startswith(prefix)):
            out += seg
        at += 2 + length
    return bytes(out + data[at:])


def _pillow_bare(rgb: np.ndarray, quality: int, exif: Image.Exif | None, icc: bytes | None) -> bytes:
    kw: dict[str, Any] = {"quality": quality, "subsampling": 0 if quality >= 90 else 2, "optimize": True}
    if exif is not None:
        kw["exif"] = exif
    if icc is not None:
        kw["icc_profile"] = icc
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(rgb), "RGB").save(buf, "JPEG", **kw)
    return buf.getvalue()


def test_strip_helper_on_synthetic(pinned_icc: bytes) -> None:
    rgb = np.full((4, 4, 3), 10, dtype=np.uint8)
    with_both = _pillow_bare(rgb, 85, Image.Exif(), pinned_icc)
    same_file_redacted(_strip(with_both, 0xE2, b"ICC_PROFILE\x00"), _pillow_bare(rgb, 85, Image.Exif(), None), "strip ICC")
    same_file_redacted(_strip(with_both, 0xE1, b"Exif\x00\x00"), _pillow_bare(rgb, 85, None, pinned_icc), "strip EXIF")


def test_real_photos_whole_file(tmp_path: Path, pinned_icc: bytes) -> None:
    pairs = real_pairs(REAL_COUNT)
    if len(pairs) < REAL_COUNT:
        missing("captures/ 真实样本不足 20 张（jpeg-optimize）")
    files = 0
    for i, (jpg, _dng) in enumerate(pairs):
        rgb = load_jpeg(jpg, None)
        base, sub = source_exif(jpg, "原图")
        exif_json = _exif_json(base, sub)
        for quality in TIERS:
            got, payload = _encode(tmp_path, f"real{i}-{quality}", rgb, quality, exif_json)
            produced(got, payload, "jpeg-encode --optimize on a real photo")
            want = _desktop(rgb, quality, base, sub)
            same_file_redacted(got, want, f"real {i} q{quality}")
            files += 1
            if quality != TIERS[i % len(TIERS)] or got is None:
                continue
            # Without the ICC profile: the rest of the file is _finish's file without icc_profile.
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(export_mod, "_ICC", b"")
                no_icc = _desktop(rgb, quality, base, sub)
            same_file_redacted(_strip(got, 0xE2, b"ICC_PROFILE\x00"), no_icc, f"real {i} no ICC")
            # Without source EXIF (an unreadable source): only the note, as source_exif returns.
            bare, bare_payload = _encode(tmp_path, f"real{i}-bare", rgb, quality, _exif_json({270: "原图"}, {}))
            produced(bare, bare_payload, "jpeg-encode --optimize without source EXIF")
            same_file_redacted(bare, _desktop(rgb, quality, {270: "原图"}, {}), f"real {i} no source EXIF")
            files += 1
    note("jpeg_optimize_real_photos", len(pairs))
    note("jpeg_optimize_real_files", files)
    check(files >= REAL_COUNT * len(TIERS), "fewer real files than planned")


def _spec(tmp: Path, tag: str, **fields: Any) -> Path:
    path = tmp / f"spec_{tag}.json"
    path.write_text(json.dumps({"out": str(tmp / f"out_{tag}.jpg"), **fields}), encoding="utf-8")
    return path


def test_wasm_matches_native_optimize(tmp_path: Path) -> None:
    """Synthetic inputs only: the driver's output names the differing case."""
    specs: list[Path] = []
    exif = tmp_path / "w.exif.json"
    exif.write_text(_exif_json(SYN_BASE, {**SYN_SUB, 41486: R(4288000, 23400), 37381: R(3, -(1 << 33))}), encoding="utf-8")
    for h, w in [(1, 1), (7, 9), (17, 16), (97, 131)]:
        for kind, rgb in _synthetic(h, w, h + w):
            npy = tmp_path / f"w_{kind}{h}x{w}.npy"
            np.save(npy, rgb)
            for q in (100, 95, 90, 89, 70, 1):
                specs.append(_spec(tmp_path, f"{kind}{h}x{w}_{q}", npy=str(npy), exif=str(exif), quality=q, optimize=True))
    big = tmp_path / "w_big.npy"
    np.save(big, np.random.default_rng(5).integers(0, 256, size=(320, 480, 3), dtype=np.uint8))
    specs.append(_spec(tmp_path, "big", npy=str(big), exif=str(exif), quality=100, optimize=True))
    specs.append(_spec(tmp_path, "big_plain", npy=str(big), exif=str(exif), quality=100, optimize=False))
    # Pillow's buffer: noise at q100 fails; the reduced EXIF fits where the full one doesn't.
    fb = tmp_path / "w_fb.npy"
    rgb_fb = np.full((160, 160, 3), 128, dtype=np.uint8)
    rgb_fb[:120] = np.random.default_rng(120).integers(0, 256, size=(120, 160, 3), dtype=np.uint8)
    np.save(fb, rgb_fb)
    big_exif = tmp_path / "w_big.exif.json"
    big_exif.write_text(_exif_json(SYN_BASE, {**SYN_SUB, 37510: b"ASCII\x00\x00\x00" + b"x" * 20000}), encoding="utf-8")
    specs.append(_spec(tmp_path, "fallback_buffer", npy=str(fb), exif=str(big_exif), quality=100, optimize=True))

    # Errors: quality, sizes, EXIF that can't be written, JSON nesting.
    small = tmp_path / "w_flat7x9.npy"
    for tag, q in (("q0", 0), ("q101", 101), ("qfrac", 1.5), ("qnan", "NaN")):
        specs.append(_spec(tmp_path, f"err_{tag}", npy=str(small), exif=str(exif), quality=q, cliQuality=0, optimize=True))
    for tag, w_val, h_val, raw in (
        ("65535x65535", 65535, 65535, b""),
        ("65536x1", 65536, 1, b""),
        ("0x0", 0, 0, b""),
        ("short", 1, 1, b"\x01\x02"),
    ):
        raw_path = tmp_path / f"raw_{tag}.bin"
        raw_path.write_bytes(raw)
        specs.append(_spec(tmp_path, f"raw_{tag}", raw=str(raw_path), width=w_val, height=h_val, quality=85, optimize=True))
    # Sol r1: tiny and huge floats in a RATIONAL tag (0/1 and 1/0, full EXIF kept).
    for i, value in enumerate((1e-300, -1e-300, 1e300, -1e300, 5e-324)):
        tiny = tmp_path / f"tiny{i}.exif.json"
        tiny.write_text(_exif_json({282: value}, {33434: R(1, 250), 33437: R(28, 10)}), encoding="utf-8")
        specs.append(_spec(tmp_path, f"tiny{i}", npy=str(small), exif=str(tiny), quality=95, optimize=True))
    unwritable = tmp_path / "unwritable.exif.json"
    unwritable.write_text(_exif_json(SYN_BASE, {34855: R(-5, 0)}), encoding="utf-8")
    fallback = tmp_path / "fallback.exif.json"
    fallback.write_text(_exif_json(SYN_BASE, {**SYN_SUB, 37380: R(-5, 0)}), encoding="utf-8")
    deep = tmp_path / "deep.exif.json"
    deep.write_text("[" * 129 + "]" * 129, encoding="utf-8")
    for tag, path in (("unwritable", unwritable), ("fallback", fallback), ("deep", deep)):
        specs.append(_spec(tmp_path, f"exif_{tag}", npy=str(small), exif=str(path), quality=85, optimize=True))
    run_area("jpeg", specs, tmp_path)
    note("jpeg_optimize_wasm_cases", len(specs))
