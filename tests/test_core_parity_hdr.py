"""Parity: ``gxr-cli hdr-*`` (crate ``gxr-hdr``) against ``gxr_studio.hdr`` and scipy.

Synthetic inputs are generated here; real RAW+JPG pairs come from ``GXR_STUDIO_TEST_PARENT``
(``captures/``) and are skipped when absent. Gates are in ``support/hdr_parity.py``.
"""

from __future__ import annotations

import io
import json
import struct
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageCms
from scipy import ndimage

import hdr_support as hs
from gxr_studio.hdr import capture
from gxr_studio.hdr import container as c
from gxr_studio.hdr import gain as g
from gxr_studio.raw.tiff import Tiff
from support import hdr_parity as hp
from support.core_cli import require_cli
from support.wasm_check import run_area

SHAPES = [(1, 1), (1, 7), (5, 3), (2, 9), (13, 17), (40, 33), (64, 48)]
RNG_SEED = 20261113


@pytest.fixture(autouse=True)
def _cli() -> None:
    require_cli()


def _planes(dtype: type) -> list[np.ndarray]:
    rng = np.random.default_rng(RNG_SEED)
    return [(rng.random(s) * 4 - 1).astype(dtype) for s in SHAPES]


# ---------------------------------------------------------------- 1. scipy equivalents


@pytest.mark.parametrize("dtype", [np.float32, np.float64])
@pytest.mark.parametrize("sigma", [0.5, 0.6, 8.0, 2.3, 1e-16, -1.0])
def test_gaussian_filter(tmp_path: Path, dtype: type, sigma: float) -> None:
    for i, a in enumerate(_planes(dtype)):
        out = tmp_path / f"o{i}.npy"
        hp.ok(["hdr-filter", "--op", "gaussian", "--in", hp.save(tmp_path / f"a{i}.npy", a), "--sigma", repr(sigma), "--out", str(out)])
        python, rust = ndimage.gaussian_filter(a, sigma), np.load(out)
        if dtype is np.float32:
            hp.same_f32(python, rust, f"gaussian {a.shape} sigma {sigma}")
        else:
            hp.close_f64(python, rust, f"gaussian {a.shape} sigma {sigma}")


def test_gaussian_nan_and_infinite_sigma(tmp_path: Path) -> None:
    a = _planes(np.float32)[4].copy()
    a[3, 5] = np.nan
    hp.ok(["hdr-filter", "--op", "gaussian", "--in", hp.save(tmp_path / "a.npy", a), "--sigma", "0.6", "--out", str(tmp_path / "o.npy")])
    hp.same_f32(ndimage.gaussian_filter(a, 0.6), np.load(tmp_path / "o.npy"), "gaussian with NaN")
    with pytest.raises(OverflowError) as caught:
        ndimage.gaussian_filter(a, float("inf"))
    _, err = hp.call(["hdr-filter", "--op", "gaussian", "--in", str(tmp_path / "a.npy"), "--sigma", "inf", "--out", str(tmp_path / "x.npy")])
    hp.same_error(caught.value, err)


@pytest.mark.parametrize("dtype", [np.float32, np.float64])
@pytest.mark.parametrize("size", [-1, 0, 1, 2, 3, 4, 5, 16, 32, 64])
def test_uniform_filter(tmp_path: Path, dtype: type, size: int) -> None:
    planes = _planes(dtype)
    planes[4][2, 2] = np.nan
    for i, a in enumerate(planes):
        out = tmp_path / f"o{i}.npy"
        hp.ok(["hdr-filter", "--op", "uniform", "--in", hp.save(tmp_path / f"a{i}.npy", a), "--size", str(size), "--out", str(out)])
        python, rust = ndimage.uniform_filter(a, size, mode="reflect"), np.load(out)
        if dtype is np.float32:
            hp.same_f32(python, rust, f"uniform {a.shape} size {size}")
        else:
            assert hp.close_f64(python, rust, f"uniform {a.shape} size {size}") == 0


@pytest.mark.parametrize("kind", ["f4", "f8", "u1", "b1"])
@pytest.mark.parametrize("size", [0, 1, 2, 3, 4, 5])
def test_minimum_filter(tmp_path: Path, kind: str, size: int) -> None:
    for i, a in enumerate(_planes(np.float64)):
        a = {"f4": a.astype(np.float32), "f8": a, "u1": (a * 60 + 60).astype(np.uint8), "b1": a > 0.3}[kind]
        out = tmp_path / f"o{i}.npy"
        hp.ok(["hdr-filter", "--op", "minimum", "--in", hp.save(tmp_path / f"a{i}.npy", a), "--size", str(size), "--out", str(out)])
        hp.same_exact(ndimage.minimum_filter(a, size), np.load(out), f"minimum {kind} {a.shape} size {size}")


def test_label_and_dilation(tmp_path: Path) -> None:
    rng = np.random.default_rng(RNG_SEED)
    for i in range(40):
        m = rng.random((int(rng.integers(1, 40)), int(rng.integers(1, 40)))) < rng.uniform(0.1, 0.9)
        src = hp.save(tmp_path / f"m{i}.npy", m)
        got = hp.ok(["hdr-filter", "--op", "label", "--in", src, "--out", str(tmp_path / f"l{i}.npy")])
        labels, count = ndimage.label(m)
        assert got["count"] == count
        hp.same_exact(labels.astype(np.int32), np.load(tmp_path / f"l{i}.npy"), f"label {m.shape}")
        for it in (1, 2, 3, 0, -1):
            hp.ok(["hdr-filter", "--op", "dilate", "--in", src, "--iterations", str(it), "--out", str(tmp_path / f"d{i}.npy")])
            hp.same_exact(ndimage.binary_dilation(m, iterations=it), np.load(tmp_path / f"d{i}.npy"), f"dilate {m.shape} x{it}")


@pytest.mark.parametrize("dtype", [np.float32, np.float64])
def test_map_coordinates(tmp_path: Path, dtype: type) -> None:
    rng = np.random.default_rng(RNG_SEED)
    for i, a in enumerate(_planes(dtype)):
        h, w = a.shape
        sy = (rng.random((9, 11)) * (h + 4) - 2).astype(np.float32)
        sx = (rng.random((9, 11)) * (w + 4) - 2).astype(np.float32)
        sy[0, :3], sx[0, :3] = [0, h - 1, h - 1], [0, w - 1, 0.5]
        args = ["hdr-map", "--in", hp.save(tmp_path / f"a{i}.npy", a), "--sy", hp.save(tmp_path / f"y{i}.npy", sy)]
        hp.ok([*args, "--sx", hp.save(tmp_path / f"x{i}.npy", sx), "--out", str(tmp_path / f"o{i}.npy")])
        python = ndimage.map_coordinates(a, [sy, sx], order=1, mode="nearest")
        rust = np.load(tmp_path / f"o{i}.npy")
        if dtype is np.float32:
            hp.same_f32(python, rust, f"map_coordinates {a.shape}")
        else:
            hp.close_f64(python, rust, f"map_coordinates {a.shape}")


@pytest.mark.parametrize("dtype", [np.float32, np.float64])
def test_block_mean_edge_pads(tmp_path: Path, dtype: type) -> None:
    rng = np.random.default_rng(RNG_SEED)
    cases = [
        ((2, 3), 4),
        ((13, 17), 4),
        ((40, 33), 6),
        ((50, 61), 9),
        ((64, 64), 1),
        ((30, 30), 12),
        ((5, 400), 4),
        ((400, 5), 6),
        ((300, 411), 3),
    ]
    for i, (shape, f) in enumerate(cases):
        a = (rng.random(shape) * 2).astype(dtype)
        hp.ok(["hdr-block-mean", "--in", hp.save(tmp_path / f"a{i}.npy", a), "--factor", str(f), "--out", str(tmp_path / f"o{i}.npy")])
        python, rust = g.block_mean(a, f), np.load(tmp_path / f"o{i}.npy")
        if dtype is np.float32:
            hp.same_f32(python, rust, f"block_mean {shape} / {f}")
        else:
            assert hp.close_f64(python, rust, f"block_mean {shape} / {f}") == 0
    two = np.arange(6, dtype=np.float32).reshape(2, 3)
    hp.ok(["hdr-block-mean", "--in", hp.save(tmp_path / "t.npy", two), "--factor", "4", "--out", str(tmp_path / "t-o.npy")])
    assert np.load(tmp_path / "t-o.npy")[0, 0] == 3.5


def test_sdr_luma_grid_every_code(tmp_path: Path) -> None:
    codes = np.arange(256, dtype=np.uint8)
    rgb = np.stack(np.meshgrid(codes, codes[::-1], indexing="ij"), axis=-1)
    rgb = np.concatenate([rgb, rgb[..., :1]], axis=-1).astype(np.uint8)
    for f in (1, 4, 5):
        hp.ok(["hdr-luma", "--rgb", hp.save(tmp_path / "rgb.npy", rgb), "--factor", str(f), "--out", str(tmp_path / f"y{f}.npy")])
        hp.check_sdr(g.sdr_luma_grid(rgb, f), np.load(tmp_path / f"y{f}.npy"), rgb, f, f"sdr_luma_grid factor {f}")


def test_srgb_to_linear_is_correctly_rounded(tmp_path: Path) -> None:
    """PORT-WAVE-1 platform-libm rule: the oracle is the correctly rounded float32 (``decimal``,
    50 digits, rounded once). numpy's float32 ``**`` calls the host libm."""
    codes = np.arange(256, dtype=np.uint8)
    oracle, bases = hp.srgb_oracle()
    hp.ok(["hdr-srgb", "--out", str(tmp_path / "lin.npy")])
    hp.same_f32(oracle, np.load(tmp_path / "lin.npy"), "srgb_to_linear vs correctly rounded")
    # 1. The power's inputs are bit-identical: numpy's float32 base equals the oracle's.
    v = codes.astype(np.float32) / 255.0
    base = (v + np.float32(0.055)) / np.float32(1.055)
    on_pow = ~np.isnan(bases)
    assert np.array_equal(base[on_pow].view(np.uint32), bases[on_pow].view(np.uint32))
    if sys.platform != "darwin":
        return
    # 3. On this Mac, Apple's powf differs from the correctly rounded value exactly here.
    numpy_lin = g.srgb_to_linear(codes)
    differ = np.nonzero(numpy_lin.view(np.uint32) != oracle.view(np.uint32))[0].tolist()
    for code in differ:
        got, want = numpy_lin[code], oracle[code]
        print(f"srgb code {code}: numpy {got!r} 0x{int(got.view(np.uint32)):08x}, correctly rounded {want!r} 0x{int(want.view(np.uint32)):08x}")
    assert differ == list(hp.POW_CODES)


def test_huge_caller_sizes_are_refused_not_allocated(tmp_path: Path) -> None:
    """UNIT-COMMON size amendment: caller dimensions go through checked u64 and the 2**32 - 1
    count limit; the answer is one core error (exit 2), never a panic, abort or wrap."""
    big = "array is too big; `arr.size * arr.dtype.itemsize` is larger than the maximum possible size."
    plane = hp.save(tmp_path / "p.npy", np.ones((6, 8), np.float32))
    mask = hp.save(tmp_path / "m.npy", np.zeros((6, 8), bool))
    (tmp_path / "al.json").write_text(json.dumps({"scale": 1.0, "dy": 0, "dx": 0}))
    rgb = hp.save(tmp_path / "rgb.npy", np.zeros((6, 8, 3), np.uint8))
    out = ["--out", str(tmp_path / "o.npy")]
    cases = {
        "register": ["hdr-register", "--raw", plane, "--clip", mask, "--align", str(tmp_path / "al.json")]
        + ["--shape", "4294967295", "4294967295", "--grid", "6", "8", *out, "--clip-out", str(tmp_path / "c.npy")],
        "register-wide": ["hdr-register", "--raw", plane, "--clip", mask, "--align", str(tmp_path / "al.json")]
        + ["--shape", "1", "18446744073709551615", "--grid", "6", "8", *out, "--clip-out", str(tmp_path / "c.npy")],
        "block-mean": ["hdr-block-mean", "--in", plane, "--factor", "4294967295", *out],
        "block-mean-pad": ["hdr-block-mean", "--in", plane, "--factor", "65536", *out],
        "luma": ["hdr-luma", "--rgb", rgb, "--factor", "4294967296", *out],
        "align": ["hdr-align", "--sdr", plane, "--raw", plane, "--sdr-shape", "4294967295", "4294967295"],
        "gain": ["hdr-gain", "--sdr", plane, "--raw", plane, "--clip", mask, "--sdr-shape", "4294967295", "4294967295", *out],
    }
    for name, args in cases.items():
        _, err = hp.call(args)
        assert err is not None and (err.kind, err.message) == ("value_error", big), name
    _, err = hp.call(["hdr-align", "--sdr", plane, "--raw", plane, "--sdr-shape", "2147483648", "1"])
    assert err is not None and (err.kind, err.message) == ("bad_input", "数组长度和形状对不上")


# ---------------------------------------------------------------- 2. FFT alignment


def test_phase_shift_peaks(tmp_path: Path) -> None:
    rng = np.random.default_rng(RNG_SEED)
    for i, (h, w) in enumerate([(24, 30), (31, 17), (96, 144), (101, 67), (100, 150), (128, 64)]):
        base = ndimage.gaussian_filter(rng.random((h, w)), 1.5).astype(np.float32)
        shift = (int(rng.integers(-h // 3, h // 3)), int(rng.integers(-w // 3, w // 3)))
        moved = (np.roll(base, shift, axis=(0, 1)) + rng.normal(0, 0.01, (h, w))).astype(np.float32)
        a, b = g._norm(g._hp(moved + 1)), g._norm(g._hp(base + 1))
        got = hp.ok(["hdr-phase", "--a", hp.save(tmp_path / f"a{i}.npy", a), "--b", hp.save(tmp_path / f"b{i}.npy", b)])
        assert (got["dy"], got["dx"]) == g._phase_shift(a, b), f"{(h, w)} shift {shift}"


def _pair_gate(tmp_path: Path, jpeg: bytes, dng: bytes, tag: str) -> hp.Flow:
    return hp.flow(tmp_path, jpeg, dng, tag)


@pytest.mark.parametrize("seed,orientation", [(0, 1), (1, 1), (2, 1), (3, 6), (4, 8), (5, 3)])
def test_pair_alignment_register_and_gain(tmp_path: Path, seed: int, orientation: int) -> None:
    result = _pair_gate(tmp_path, *hs.pair(seed, orientation), f"pair{seed}")
    # Python itself declines some seeds (tiles_unverified); the fields above matched either way.
    assert any(s.startswith("gain:ok") for s in result.stages) == (result.check is None)
    assert all(s in ("e2e-align:same", "e2e-gain-codes:0") for s in result.stages if s.startswith("e2e"))


def test_small_translation_seed_locks_dy(tmp_path: Path) -> None:
    big = hs.scene(400, 600, seed=2)
    result = hp.flow(tmp_path, hs.camera_jpeg(big[4:388, 12:588]), hs.camera_dng(big[8:392, 8:584]), "shift")
    a = result.alignment
    assert (a["scale"], a["dy"], a["dx"]) == (1.0, 1, -1) and result.check is None


def _moved(size: int, distance: int, quiet: bool = False) -> tuple[bytes, bytes]:
    first = hs.scene()
    if quiet:
        first[64:224, 360:568] = 0.22
    second = first.copy()
    first[96 : 96 + size, 420 : 420 + size] = 0.035
    second[96 : 96 + size, 420 + distance : 420 + distance + size] = 0.035
    return hs.camera_jpeg(second), hs.camera_dng(first)


@pytest.mark.parametrize("case", ["moved8", "moved40q", "local", "another", "flat", "shortmove"])
def test_not_aligned_and_no_curve_reasons(tmp_path: Path, case: str) -> None:
    rgb = hs.scene(seed=0)
    if case == "moved8":
        jpeg, dng = _moved(8, 48)
    elif case == "moved40q":
        jpeg, dng = _moved(40, 80, quiet=True)
    elif case == "local":
        moved = rgb.copy()
        moved[:192, :288] = np.roll(rgb, (12, 12), axis=(0, 1))[:192, :288]
        jpeg, dng = hs.camera_jpeg(moved), hs.camera_dng(rgb)
    elif case == "another":
        jpeg, dng = hs.camera_jpeg(np.roll(rgb, 140, axis=1)), hs.camera_dng(rgb)
    elif case == "flat":
        flat = np.full((128, 192, 3), 0.01)
        jpeg, dng = hs.camera_jpeg(flat), hs.camera_dng(flat)
    else:
        second = rgb.copy()
        rgb[96:104, 180:188] = 0.035
        second[96:104, 188:196] = 0.035
        jpeg, dng = hs.camera_jpeg(second), hs.camera_dng(rgb)
    result = hp.flow(tmp_path, jpeg, dng, case)
    assert result.check is not None or result.stages[-1] != "gain:ok"


# ---------------------------------------------------------------- 3. gain curve and plane


def _registered(seed: int = 1) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    from gxr_studio.raw import open_dng

    jpeg, dng = hs.pair(seed)
    raw = g.raw_luma(open_dng(dng))
    sdr = g.sdr_luma_grid(g.decode_rgb(jpeg), 4)
    al = g.align(sdr, raw.y)
    y, clip = g.register(raw, sdr.shape, al, sdr.shape)
    return sdr, y, clip


PARAMS = [
    None,
    {"knee": 0.99},
    {"max_stops": 1.0, "hdr_slope": 0.5},
    {"gate_lo": 0.05, "gate_hi": 0.95},
    {"knee": 0.2, "max_stops": 4.0, "map_factor": 2, "quality": 80},
]


@pytest.mark.parametrize("params", PARAMS, ids=lambda p: json.dumps(p))
def test_gain_params(tmp_path: Path, params: dict | None) -> None:
    sdr, y, clip = _registered()
    assert hp.compare_gain(tmp_path, sdr, y, clip, params, "p").startswith("ok")


def test_gain_non_photo_dark_and_errors(tmp_path: Path) -> None:
    sdr, y, clip = _registered(2)
    stamped = y.copy()
    stamped[10:20, 30:70] = np.nan
    stamped[-8:, :] = np.nan
    assert hp.compare_gain(tmp_path, sdr, stamped, clip, None, "stamp").startswith("ok")
    assert hp.compare_gain(tmp_path, sdr * np.float32(0.2), y, clip, None, "dark").startswith("ok")
    assert hp.compare_gain(tmp_path, sdr, y, np.ones_like(clip), None, "allclip") == "no_curve"
    assert hp.compare_gain(tmp_path, sdr, np.full_like(y, np.nan), clip, None, "nophoto") == "no_curve"
    narrow = np.full_like(y, 0.02)
    narrow[::7] = 0.0201
    assert hp.compare_gain(tmp_path, np.full_like(sdr, 0.2), narrow, clip, None, "narrow") == "no_curve"


def test_fit_curve(tmp_path: Path) -> None:
    rng = np.random.default_rng(RNG_SEED)
    for i in range(12):
        n = int(rng.integers(80, 4000))
        x = (rng.normal(-3, 1.5, n)).astype(np.float32)
        y = (np.minimum(x + rng.normal(0, 0.2, n), 0) + (i % 3) * 0.1).astype(np.float32)
        valid = rng.random(n) < 0.9
        args = [
            "hdr-fit",
            "--x",
            hp.save(tmp_path / "x.npy", x),
            "--y",
            hp.save(tmp_path / "y.npy", y),
            "--valid",
            hp.save(tmp_path / "v.npy", valid),
        ]
        payload, err = hp.call(args)
        try:
            cx, cy = g.fit_curve(x, y, valid)
        except g.HdrUnsupported as exc:
            hp.same_error(exc, err)
            continue
        assert err is None
        assert payload["cx"] == cx.tolist()
        hp.close_f64(cy, np.array(payload["cy"]), f"fit_curve cy n={n}")


# ---------------------------------------------------------------- 4. container and capture


def _jpeg(rgb: np.ndarray, **kw: object) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, "JPEG", **kw)
    return buf.getvalue()


def _primaries() -> dict[str, bytes]:
    rgb = hs.srgb_bytes(hs.scene(24, 32))
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    exif = Image.Exif()
    exif[271], exif[274] = "RICOH", 1
    plain = _jpeg(rgb, quality=90, subsampling=0, optimize=True, icc_profile=icc, exif=exif)
    gain = c.encode_gainmap(np.linspace(0, 1.25, 48, dtype=np.float32).reshape(6, 8), c.GainMeta(gain_max=1.25, cap_max=1.25), 90)
    return {
        "plain": plain,
        "progressive": _jpeg(rgb, quality=85, progressive=True),
        "wrapped": c.assemble(plain, gain, c.GainMeta(gain_max=1.25, cap_max=1.25)),
        "camera": hs.pair(0)[0],
    }


METAS = [
    {"gain_max": 1.25, "cap_max": 1.25},
    {"gain_min": -0.5, "gain_max": 2.4999, "cap_max": 2.4999},
    {"gain_max": 1.23456789, "cap_max": 0.05, "offset_sdr": 0.1, "offset_hdr": 0.0},
    {"gamma": -1.0},
    {"cap_min": -0.1},
    {"gain_max": float("nan")},
    {"gain_max": float("inf")},
    {"gain_max": 1e10},
    {"cap_max": 5e9},
    {"gain_max": 3e-13, "cap_max": 123456.7},
]


def _wrap(tmp_path: Path, primary: bytes, gain: bytes, meta: dict, tag: str) -> None:
    (tmp_path / f"{tag}-a.jpg").write_bytes(primary)
    (tmp_path / f"{tag}-b.jpg").write_bytes(gain)
    (tmp_path / f"{tag}-m.json").write_text(json.dumps(meta))
    args = ["hdr-assemble", "--primary", str(tmp_path / f"{tag}-a.jpg"), "--gain", str(tmp_path / f"{tag}-b.jpg")]
    payload, err = hp.call([*args, "--meta", str(tmp_path / f"{tag}-m.json"), "--out", str(tmp_path / f"{tag}-o.npy")])
    try:
        expect = c.assemble(primary, gain, c.GainMeta(**meta))
    except (ValueError, OverflowError, struct.error) as exc:
        hp.same_error(exc, err)
        return
    assert err is None, f"{tag}: rust raised {err.kind}: {err.message}"
    rust = np.load(tmp_path / f"{tag}-o.npy")
    assert rust.shape == (1, len(expect)) and rust.tobytes() == expect, f"{tag}: assembled bytes differ"


@pytest.mark.parametrize("meta", METAS, ids=lambda m: json.dumps(m))
def test_assemble_bytes(tmp_path: Path, meta: dict) -> None:
    gain = c.encode_gainmap(np.linspace(0, 2.0, 117, dtype=np.float32).reshape(9, 13), c.GainMeta(gain_max=2.0, cap_max=2.0), 90)
    for name, primary in _primaries().items():
        _wrap(tmp_path, primary, gain, meta, name)
        _wrap(tmp_path, primary, primary, meta, name + "-selfgain")


def _broken() -> dict[str, bytes]:
    jpg = hs.pair(0)[0]
    sos = jpg.index(b"\xff\xda")
    return {
        "empty": b"",
        "text": b"not a jpeg",
        "soi": b"\xff\xd8",
        "half": jpg[: len(jpg) // 2],
        "short-length": jpg[:2] + b"\xff\xe1\x00",
        "bad-length": jpg[:2] + b"\xff\xe1\x00\x01" + jpg[2:],
        "past-end": jpg[:2] + b"\xff\xe1\xff\xf0abc",
        "not-marker": jpg[:2] + b"\x00" + jpg[2:],
        "fill": jpg[:2] + b"\xff\xff\xff" + jpg[2:],
        "sos-past-end": jpg[:sos] + b"\xff\xda\xff\xff",
        "no-eoi": jpg[:-2],
        "between": jpg[:-2] + b"\xff\xc4\x00\x04\x00\x00\xff\xd9",
        "between-short": jpg[:-2] + b"\xff\xc4",
    }


@pytest.mark.parametrize("name", list(_broken()))
def test_jpeg_segments_and_container_errors(tmp_path: Path, name: str) -> None:
    data = _broken()[name]
    (tmp_path / "x.jpg").write_bytes(data)
    payload, err = hp.call(["hdr-segments", "--in", str(tmp_path / "x.jpg")])
    try:
        expect = hp.segments_of(data)
    except c.ContainerError as exc:
        hp.same_error(exc, err)
    else:
        assert err is None and payload == expect
    good = hs.pair(1)[0]
    _wrap(tmp_path, data, good, {}, "as-primary")
    _wrap(tmp_path, good, data, {}, "as-gain")


def test_segments_of_good_files(tmp_path: Path) -> None:
    for name, data in _primaries().items():
        (tmp_path / f"{name}.jpg").write_bytes(data)
        assert hp.ok(["hdr-segments", "--in", str(tmp_path / f"{name}.jpg")]) == hp.segments_of(data), name


CAPTURE = {
    36867: "2000:01:01 12:00:00",
    37521: "123",
    33434: 0.01,
    33437: 2.5,
    34855: 200,
    42036: "RICOH GR LENS A12 50mm F2.5 MACRO",
    42016: "0123456789abcdef0123456789abcdef",
}


def _review_pair(jpeg_changes: dict | None = None, dng_changes: dict | None = None) -> tuple[bytes, bytes]:
    from raw_support import bayer_scene, build_dng

    rgb = hs.scene(48, 64)
    jf, rf = {**CAPTURE, **(jpeg_changes or {})}, {**CAPTURE, **(dng_changes or {})}
    exif = Image.Exif()
    exif[271], exif[272] = "RICOH", jf.pop(272, "GXR A12")
    exif[34665] = {k: v for k, v in jf.items() if v is not None}
    jpeg = _jpeg(hs.srgb_bytes(rgb * hs.EXPOSURE), quality=95, exif=exif)
    rows = []
    for tag, value in rf.items():
        if tag == 272:
            continue
        kind = "ASCII" if isinstance(value, str) or value is None else "SHORT" if tag == 34855 else "RATIONAL"
        rows.append((tag, kind, "" if value is None else value if kind == "ASCII" else [value]))
    dng = build_dng(
        bayer_scene(48, 64, rgb * np.array(hs.NEUTRAL)),
        white=4095,
        when=CAPTURE[36867],
        exif_entries=rows,
        root_extra=[(272, "ASCII", rf.get(272, "GXR A12"))],
    )
    return jpeg, dng


def _dng_tags(data: bytes) -> dict:
    tiff = Tiff(data)
    root = tiff.ifd(tiff.root)
    tags = {tag: entry["value"] for tag, entry in root.items()}
    if 34665 in root:
        tags.update({tag: entry["value"] for tag, entry in tiff.ifd(root[34665]["value"][0]).items()})
    return tags


def _jpeg_tags(data: bytes) -> dict:
    with Image.open(io.BytesIO(data)) as image:
        exif = image.getexif()
        return {**dict(exif), **exif.get_ifd(34665)}


REVIEW = [
    ({k: v}, None)
    for k, v in [
        (36867, "2000:01:02 12:00:00"),
        (37521, "124"),
        (33434, 0.02),
        (33437, 4.0),
        (34855, 400),
        (272, "GXR OTHER"),
        (42036, "RICOH GXR MOUNT A12"),
        (42016, "abcdef0123456789abcdef0123456789"),
    ]
]
REVIEW += [(m, None) for m in ({37521: None}, {33437: None, 42036: None}, dict.fromkeys(CAPTURE))] + [(None, dict.fromkeys(CAPTURE))]
REVIEW += [({37521: "1230", 42036: "  RICOH GR LENS A12 50mm F2.5 MACRO  "}, None), (None, None)]

EDGE_TAGS = [
    {271: " Ri\x00CoH ", 36867: "0000:00:00", 37521: "12a", 33434: [0.004, 1.0], 34855: 0, 37386: -3.5},
    {272: "Ａ B\x1cC", 37521: "１２", 33437: "  2.8 ", 33434: "1_0", 34855: True, 37386: "inf"},
    {50708: 7, 42036: 1.5, 37521: "000", 33434: [], 33437: "nan", 34855: [200], 37386: "0x10"},
    {271: None, 42016: "ẞtraße µ ς", 33434: [[0.5]], 33437: "1e1_0", 37386: ".5"},
]


@pytest.mark.parametrize("case", range(len(REVIEW) + len(EDGE_TAGS)))
def test_capture_fields_and_differs(tmp_path: Path, case: int) -> None:
    if case < len(REVIEW):
        jpeg, dng = _review_pair(*REVIEW[case])
        raw_tags, jpg_tags = _dng_tags(dng), _jpeg_tags(jpeg)
    else:
        raw_tags = EDGE_TAGS[case - len(REVIEW)]
        jpg_tags = dict(EDGE_TAGS[(case - len(REVIEW) + 1) % len(EDGE_TAGS)])
    (tmp_path / "r.json").write_text(hp.tag_json(raw_tags))
    (tmp_path / "j.json").write_text(hp.tag_json(jpg_tags))
    got = hp.ok(["hdr-capture", "--raw", str(tmp_path / "r.json"), "--jpeg", str(tmp_path / "j.json")])
    raw_cap, jpg_cap = capture._capture(raw_tags), capture._capture(jpg_tags)
    expect = {"raw": raw_cap, "jpeg": jpg_cap, "differs": capture.differs(raw_cap, jpg_cap)}
    assert json.loads(json.dumps(expect)) == got
    assert list(got["raw"]) == list(raw_cap)


# ---------------------------------------------------------------- real pairs and WASM

REAL = hp.real_pairs()


def test_real_pair_set() -> None:
    """At least five of the sampled pairs go through align and gain (Python can open the DNG)."""
    from gxr_studio.raw.dng import RawDecodeError, open_dng

    if not REAL:
        pytest.skip(f"no RAW+JPG pairs under {hp.CAPTURES}")
    decodable = 0
    for _jpg, dng in REAL:
        try:
            open_dng(dng)
        except RawDecodeError:
            continue
        decodable += 1
    assert decodable >= 5


@pytest.mark.parametrize("pair", REAL, ids=[p[1].name for p in REAL])
def test_real_pair_flow(tmp_path: Path, pair: tuple[Path, Path]) -> None:
    jpg, dng = pair
    result = hp.flow(tmp_path, jpg.read_bytes(), dng, dng.stem)
    print(f"real {dng.name}: {result.stages} check={result.check}")
    assert result.stages[0] in ("raw_luma", "raw_luma:raw_decode")


def test_real_r8052275_correlation_follows_the_grid(tmp_path: Path) -> None:
    """The one real pair where the pixel chain moved ``correlation``: Python's ``align`` on
    Rust's (correctly rounded) grid gives exactly Rust's value, and the uint8 codes hold."""
    found = [p for p in hp.CAPTURES.rglob("R8052275.DNG") if "backup" not in p.as_posix().lower()] if hp.CAPTURES.is_dir() else []
    jpg = found[0].with_suffix(".JPG") if found else None
    if jpg is None or not jpg.is_file():
        pytest.skip("R8052275 is not under captures/")
    result = hp.flow(tmp_path, jpg.read_bytes(), found[0], "R8052275")
    note = "e2e-align:correlation python=0.9247201681137085 rust=0.9247203469276428"
    assert note in result.stages and "e2e-gain-codes:0" in result.stages, result.stages


def test_wasm_matches_native(tmp_path: Path) -> None:
    sdr, y, clip = _registered()
    folders = []
    for name, (s, r, cl, params) in {
        "gain": (sdr, y, clip, None),
        "params": (sdr, y, clip, {"knee": 0.99}),
        "nocurve": (sdr, y, np.ones_like(clip), None),
    }.items():
        d = tmp_path / name
        d.mkdir()
        hp.save(d / "sdr.npy", s)
        hp.save(d / "reg.npy", r)
        hp.save(d / "clip.npy", cl)
        if params is not None:
            (d / "params.json").write_text(json.dumps(params))
        folders.append(str(d))
    from gxr_studio.raw import open_dng

    jpeg, dng = hs.pair(3)
    d = tmp_path / "align"
    d.mkdir()
    hp.save(d / "sdr.npy", g.sdr_luma_grid(g.decode_rgb(jpeg), 4))
    hp.save(d / "raw.npy", g.raw_luma(open_dng(dng)).y)
    folders.append(str(d))
    gain = c.encode_gainmap(np.linspace(0, 1.25, 48, dtype=np.float32).reshape(6, 8), c.GainMeta(gain_max=1.25, cap_max=1.25), 90)
    for name, meta in {"wrap": {"gain_max": 1.25, "cap_max": 1.25}, "badmeta": {"gamma": -1.0}}.items():
        d = tmp_path / name
        d.mkdir()
        (d / "primary.jpg").write_bytes(_primaries()["plain"])
        (d / "gain.jpg").write_bytes(gain)
        (d / "meta.json").write_text(json.dumps(meta))
        folders.append(str(d))
    proc = run_area("hdr", folders, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
