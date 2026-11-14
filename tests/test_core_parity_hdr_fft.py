"""Parity: the numpy-exact FFT behind ``_phase_shift`` (unit hdr-fft, crate ``gxr-hdr``).

``gxr-cli hdr-fft`` / ``hdr-phase-plane`` / ``hdr-fft-twiddle`` against numpy 2.4.3 on uint32
bits (NaN equals NaN at the same place). How numpy computes the chain is described in
``support/hdr_fft_ref.py``. Two modes:

- the port as shipped: twiddles from correctly rounded ``cos``/``sin`` (PORT-WAVE-1 libm rule);
- ``--trig host``: the host's ``cos``/``sin``, which is what numpy itself uses. This must equal
  numpy bit for bit everywhere, which shows the structure, FMAs and rounding are exact.

Where the host's ``cos``/``sin`` is not correctly rounded on a twiddle angle, each such angle
is its own test (``test_host_libm_case``): Rust's angle equals Python's, Rust's value is the
correctly rounded one (``decimal``), the host's differs, and the downstream FFT and peak are
stated. Without such an angle in its tables, a length must match numpy exactly in both modes.
numpy comparisons run only on the reference platform (macOS arm64 with the locked numpy).
"""

from __future__ import annotations

import functools
import os
import platform
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from gxr_studio.hdr import gain as g
from support import hdr_fft_alloc as fa
from support import hdr_fft_ref as fr
from support import hdr_parity as hp
from support.core_cli import require_cli
from support.wasm_check import run_area

REFERENCE = sys.platform == "darwin" and platform.machine() == "arm64" and np.__version__ == "2.4.3"
needs_reference = pytest.mark.skipif(not REFERENCE, reason="numpy bit gates hold on macOS arm64 with numpy 2.4.3 only")

SWITCH = [47, 48, 49, 50, 51, 52, 53, 89, 97, 101, 127, 131, 251, 257, 509, 521, 1021, 1031]
REAL_GRID = [712, 1072]  # A12 50mm JPEGs (4288 x 2848) on the factor-4 grid
LENGTHS = sorted(set(range(1, 65)) | set(SWITCH) | set(REAL_GRID))
SEED = 20261114
ARRAY_TOO_BIG = "array is too big; `arr.size * arr.dtype.itemsize` is larger than the maximum possible size."

# Sol's saved 3x5 input (round-6 review P1-01, default_rng(2026111406)), float32 bits.
SOL_A = [0xBD1A22C0, 0x4075C804, 0x3E5243D2, 0x3D423E40, 0x400D4EE1, 0x3F86F4AF, 0xBF3CAB5E, 0x400AC44B]
SOL_A += [0xBFD91E9A, 0x405F2C98, 0x3F7A0998, 0xBFEC238B, 0x3E6EA61A, 0x40099910, 0x3E91CD88]
SOL_B = [0xBBBF47DF, 0x3E4AE39A, 0xC0054A95, 0x3F2838D2, 0x3F91DEDF, 0x3E738CF9, 0xBED91E97, 0x3F815341]
SOL_B += [0x3E94CAA7, 0x3F4750B2, 0x40351E63, 0xBDAEA2F9, 0xBF3B2CCE, 0x3FFC2C22, 0x3EC5DA3F]


# Sol r2 P1-01: the 8x89 integer plane b (row-major); a = roll(b, (-1, -1)) + roll(b, (1, 1)).
NEAR_TIE_B = (
    "1 -15 -15 0 -1 14 16 4 -12 2 -6 0 -9 0 -4 14 6 12 -15 0 -6 15 12 8 -8 -1 1 -1 0 10 0 -16 -13 16 -6 -1 16 -7 4 "
    "-12 11 8 -9 4 15 -4 2 10 11 3 -1 -8 12 -1 5 -2 -2 15 -9 -1 -4 16 -8 12 4 8 -13 0 -12 5 -7 -4 14 -6 8 5 -11 0 -6 "
    "8 -2 15 -11 -7 -13 7 12 -12 -11 "
    "-10 -10 -16 11 -3 13 -8 -12 4 14 12 10 -13 0 -12 7 -15 -14 12 14 -5 -3 -12 -11 8 16 15 -11 0 -15 2 -3 11 -7 3 14 "
    "14 11 16 2 -5 15 13 14 15 14 16 -13 10 3 10 -6 3 10 6 12 11 -6 0 1 7 2 11 14 4 6 -2 -14 -10 -9 11 -6 -3 1 -10 8 "
    "14 16 -16 6 12 3 1 1 2 2 -16 -6 10 "
    "-1 -9 15 -4 -11 -4 14 -4 -10 15 6 -7 14 -12 11 -11 -13 -7 -3 -4 -7 13 -16 10 13 1 -9 -14 -10 -11 -2 -14 2 0 -15 "
    "-1 -1 2 -8 -14 11 -4 9 10 -10 11 3 -10 7 -6 -16 -8 -12 15 0 3 12 6 -4 -5 -15 10 -7 -1 -4 11 -16 -9 -7 -9 -8 -1 "
    "12 16 2 -12 -7 3 13 -14 -11 -15 -14 7 -7 -13 -13 -10 9 "
    "-9 -10 -4 12 -5 -7 -6 -3 3 13 2 -16 -7 -2 13 -14 -6 15 -13 0 -9 -1 10 -11 13 -2 -2 2 10 6 -3 -3 2 6 13 14 -2 5 9 "
    "11 -9 -1 5 3 -15 8 -1 -1 5 14 2 12 13 -1 -6 -10 14 -7 -11 -6 -3 -15 -8 2 1 -5 9 -15 8 8 -8 3 15 -7 -13 -12 16 15 "
    "1 -2 -1 15 -2 -11 -10 8 -13 16 -12 "
    "-4 3 -11 4 8 11 2 -4 -2 2 -10 -11 4 -13 -14 -9 -11 -6 -10 10 -7 15 5 -1 12 12 0 -4 8 -7 0 6 -1 -11 -6 4 0 -6 -16 "
    "-3 -16 13 14 15 -15 12 -6 -2 -4 4 -4 -8 -10 5 15 -1 9 -5 -12 -3 7 -2 4 12 10 -6 12 14 -6 -9 -14 -8 -9 -12 -1 -1 "
    "5 -9 11 0 -8 14 -9 4 -10 2 -1 -3 -12 "
    "11 -2 15 10 12 3 9 1 3 13 3 -11 -6 14 12 -3 -5 -15 4 -8 -2 -6 -11 10 -2 -16 6 14 -10 15 -1 -6 0 13 2 -11 -2 -11 "
    "-6 12 -1 9 -5 -15 13 5 -7 -10 13 1 -2 12 10 -14 3 9 -14 -8 -3 6 -8 3 -8 12 11 10 6 -13 14 14 -16 -10 15 -8 16 "
    "-11 2 0 -13 -14 -5 7 14 11 2 -11 3 -11 -15 "
    "11 7 -8 2 -8 -1 12 14 4 -12 -10 -9 4 -8 7 -9 6 7 -14 -14 -6 6 0 12 7 -2 4 5 -2 11 -2 -7 13 -11 -16 -6 3 11 3 0 "
    "-16 13 -14 2 9 12 0 -8 -15 0 -5 8 -2 -11 -12 -6 14 6 -15 7 4 -15 15 -11 -7 2 -12 11 2 7 16 15 7 -14 -6 8 -9 15 9 "
    "13 -16 13 3 7 16 8 -1 5 15 "
    "-6 -16 -4 -10 -2 -12 -16 -12 -10 -3 13 -8 0 -13 7 8 5 -16 7 -3 -6 0 10 7 0 -5 16 -8 -16 -11 -10 13 -15 -2 3 -2 "
    "-3 -14 8 -12 -11 0 -7 6 -8 -10 -2 0 -4 -10 7 13 1 -2 -9 5 14 -1 13 4 3 1 -16 11 10 16 -10 -14 -9 0 3 12 -12 11 "
    "13 8 -1 -3 -4 -2 9 -11 10 5 1 5 10 6 2 "
)


@pytest.fixture(autouse=True)
def _cli() -> None:
    require_cli()


def _sol() -> tuple[np.ndarray, np.ndarray]:
    a = np.array(SOL_A, np.uint32).view(np.float32).reshape(3, 5)
    b = np.array(SOL_B, np.uint32).view(np.float32).reshape(3, 5)
    return a, b


def _inputs(rng: np.random.Generator, rows: int, n: int) -> np.ndarray:
    """Two rows each of: normal, wide dynamic range, real only, small integers."""
    parts = []
    for style in range(4):
        if style == 0:
            x = rng.standard_normal((rows, n)) + 1j * rng.standard_normal((rows, n))
        elif style == 1:
            mag = 10.0 ** rng.uniform(-30, 30, (rows, n, 2))
            x = rng.standard_normal((rows, n)) * mag[..., 0] + 1j * rng.standard_normal((rows, n)) * mag[..., 1]
        elif style == 2:
            x = rng.standard_normal((rows, n)) + 0j
        else:
            x = rng.integers(-8, 9, (rows, n)) + 1j * rng.integers(-8, 9, (rows, n))
        parts.append(x)
    return np.concatenate(parts).astype(np.complex64)


@functools.cache
def _table_cases(table: int) -> tuple[dict[str, Any], ...]:
    return tuple(fr.host_vs_cr(table))


@functools.cache
def _host_cases() -> tuple[dict[str, Any], ...]:
    tables = sorted({t for n in LENGTHS for t in fr.twiddle_tables(n)})
    return tuple(c for t in tables for c in _table_cases(t))


def _affected(n: int) -> bool:
    """A length whose twiddle tables hold an angle where the host is not correctly rounded."""
    return any(_table_cases(t) for t in fr.twiddle_tables(n))


def test_bit_gate_handles_nan_and_inf() -> None:
    """NaN equals NaN at the same place, a one-sided NaN fails, inf is compared exactly."""
    x = np.array([1.0, np.nan, np.inf, -0.0], np.float32)
    assert fr.bit_diff(x, x.copy()) == 0
    y = x.copy()
    y[0] = np.nan
    assert fr.bit_diff(x, y) == 1
    y = x.copy()
    y[2] = -np.inf
    assert fr.bit_diff(x, y) == 1
    y = x.copy()
    y[3] = 0.0
    assert fr.bit_diff(x, y) == 1
    y = x.copy()
    y[1] = np.float32(np.nan) * np.float32(-1)
    assert fr.bit_diff(x, y) == 0


# ---------------------------------------------------------------- twiddles


@pytest.mark.parametrize("n", sorted({t for n in LENGTHS for t in fr.twiddle_tables(n)}))
def test_twiddle_tables_are_correctly_rounded(tmp_path: Path, n: int) -> None:
    """Rust's raw tables equal the correctly rounded cos/sin of the same angles (decimal), and
    every float and double twiddle is pocketfft's fused product of them."""
    rust, v1 = fr.rust_tables(tmp_path, n, "tables")
    want = fr.table_values(n, "cr")
    assert fr.bit_diff(want, rust) == 0, f"table {n}"
    for kind, width in (("float", np.float32), ("double", np.float64)):
        got, _ = fr.rust_tables(tmp_path, n, kind)
        assert fr.bit_diff(fr.twiddles_from(rust, n, v1, width), got) == 0, f"table {n} {kind}"


@pytest.mark.parametrize("n", [1, 7, 26, 89, 178, 180, 1072])
def test_host_trig_mode_calls_the_host(tmp_path: Path, n: int) -> None:
    """``--trig host`` builds the tables from the host's cos/sin (Python's math uses the same)."""
    rust, _ = fr.rust_tables(tmp_path, n, "tables", "host")
    assert fr.bit_diff(fr.table_values(n, "host"), rust) == 0


_DOWNSTREAM: dict[int, str] = {}


def _downstream(table: int, tmp: Path) -> str:
    if table not in _DOWNSTREAM:
        _DOWNSTREAM[table] = _downstream_of(table, tmp)
    return _DOWNSTREAM[table]


def _downstream_of(table: int, tmp: Path) -> str:
    """For every gated length that builds ``table``: Rust (host twiddles) equals numpy, the
    shipped port's differing values are counted, and the phase-correlation peak is checked."""
    notes = []
    for n in [m for m in LENGTHS if table in fr.twiddle_tables(m)]:
        rng = np.random.default_rng(SEED + n)
        x = _inputs(rng, 2, n)
        for inverse in (False, True):
            ref = fr.numpy_fft(x, inverse=inverse)
            assert fr.bit_diff(ref, fr.rust_fft(tmp, x, inverse=inverse, trig="host")) == 0, (n, inverse)
            notes.append(f"n={n} {'ifft' if inverse else 'fft'}: {fr.bit_diff(ref, fr.rust_fft(tmp, x, inverse=inverse))}/{ref.size * 2}")
        a = rng.standard_normal((n, 5)).astype(np.float32)
        b = np.roll(a, 1, 0) + np.float32(0.3) * rng.standard_normal((n, 5)).astype(np.float32)
        ref_pc = fr.phase_plane(a, b)
        got, peak = fr.rust_plane(tmp, a, b)
        host, _ = fr.rust_plane(tmp, a, b, "host")
        assert fr.bit_diff(ref_pc, host) == 0
        assert peak == g._phase_shift(a, b), f"n={n}: the peak moved"
        notes.append(f"plane ({n}, 5): {fr.bit_diff(ref_pc, got)} values differ, peak {peak} same")
    return "; ".join(notes)


@needs_reference
@pytest.mark.parametrize("case", list(_host_cases()), ids=lambda c: f"{c['n']}-{c['row']}-{c['fn']}-{c['x']:016x}")
def test_host_libm_case(tmp_path_factory: pytest.TempPathFactory, case: dict[str, Any]) -> None:
    """One angle where the host's cos/sin is not correctly rounded (PORT-WAVE-1 libm rule)."""
    tmp = tmp_path_factory.mktemp(f"libm-{case['n']}")
    rust, _ = fr.rust_tables(tmp, case["n"], "tables")
    row = rust[case["row"]]
    angle = int(np.float64(row[2]).view(np.uint64))
    value = abs(float(row[case["part"]]))
    assert angle == case["x"], "the angle passed to cos/sin differs"
    assert fr._bits(value) == case["cr"], "Rust is not the correctly rounded value"
    assert case["host"] != case["cr"], "the host is correctly rounded here"
    head = f"table {case['n']} {case['fn']}(0x{case['x']:016x}): host 0x{case['host']:016x}"
    print(f"{head}, correctly rounded 0x{case['cr']:016x}; {_downstream(case['n'], tmp)}")


# ---------------------------------------------------------------- 1-D and 2-D


@needs_reference
@pytest.mark.parametrize("n", LENGTHS)
def test_fft_matches_numpy(tmp_path: Path, n: int) -> None:
    """fft (complex128 loop, rounded) and ifft (complex64 loop) on the vectorised (8 rows) and
    scalar (1 row) numpy paths."""
    rng = np.random.default_rng(SEED + n)
    x = _inputs(rng, 2, n)
    for batch in (x, x[:1]):
        for inverse in (False, True):
            ref = fr.numpy_fft(batch, inverse=inverse)
            host = fr.rust_fft(tmp_path, batch, inverse=inverse, trig="host")
            assert fr.bit_diff(ref, host) == 0, f"n={n} inverse={inverse} host: {fr.first_diff(ref, host)}"
            got = fr.rust_fft(tmp_path, batch, inverse=inverse)
            if not _affected(n):
                assert fr.bit_diff(ref, got) == 0, f"n={n} inverse={inverse}: {fr.first_diff(ref, got)}"


@needs_reference
@pytest.mark.parametrize("shape", [(1, 1), (3, 5), (8, 4), (13, 64), (64, 13), (89, 6), (5, 101), (712, 9), (7, 1072)])
def test_fft2_matches_numpy(tmp_path: Path, shape: tuple[int, int]) -> None:
    rng = np.random.default_rng(SEED + shape[0] * 1000 + shape[1])
    x = (rng.standard_normal(shape) + 1j * rng.standard_normal(shape)).astype(np.complex64)
    affected = _affected(shape[0]) or _affected(shape[1])
    for inverse in (False, True):
        ref = fr.numpy_fft(x, inverse=inverse, two_d=True)
        assert fr.bit_diff(ref, fr.rust_fft(tmp_path, x, inverse=inverse, two_d=True, trig="host")) == 0
        if not affected:
            assert fr.bit_diff(ref, fr.rust_fft(tmp_path, x, inverse=inverse, two_d=True)) == 0


# ---------------------------------------------------------------- the phase-correlation plane


def _plane_gate(tmp: Path, a: np.ndarray, b: np.ndarray, label: str) -> int:
    """Host mode exact; the shipped port exact unless libm-affected; the peak always Python's.
    Returns the shipped port's differing values (0 when unaffected)."""
    ref = fr.phase_plane(a, b)
    assert ref.dtype == np.float32
    host, host_peak = fr.rust_plane(tmp, a, b, "host")
    assert fr.bit_diff(ref, host) == 0, f"{label} host: {fr.first_diff(ref, host)}"
    got, peak = fr.rust_plane(tmp, a, b)
    want = g._phase_shift(a, b)
    assert host_peak == want and peak == want, f"{label}: peak python={want} rust={peak}"
    diff = fr.bit_diff(ref, got)
    if not (_affected(a.shape[0]) or _affected(a.shape[1])):
        assert diff == 0, f"{label}: {fr.first_diff(ref, got)}"
    return diff


@needs_reference
def test_sol_saved_input_is_bit_exact(tmp_path: Path) -> None:
    """The input where float64 put the peak at (1, 1); numpy says (-1, -1)."""
    a, b = _sol()
    assert g._phase_shift(a, b) == (-1, -1)
    nan = a.copy()
    nan[1, 2] = np.nan
    for label, (sa, sb) in {
        "original": (a, b),
        "positive-luma": (np.float32(0.45) + np.float32(0.04) * a, np.float32(0.45) + np.float32(0.04) * b),
        "nan": (nan, b),
    }.items():
        assert _plane_gate(tmp_path, sa, sb, label) == 0
    _, peak = fr.rust_plane(tmp_path, a, b)
    assert peak == (-1, -1)
    assert hp.ok(["hdr-phase", "--a", hp.save(tmp_path / "a.npy", a), "--b", hp.save(tmp_path / "b.npy", b)]) == {"dy": -1, "dx": -1}


def _near_tie() -> tuple[np.ndarray, np.ndarray]:
    b = np.array(NEAR_TIE_B.split(), np.float32).reshape(8, 89)
    a = (np.roll(b, (-1, -1), (0, 1)) + np.roll(b, (1, 1), (0, 1))).astype(np.float32)  # exact: small integers
    return a, b


@needs_reference
def test_near_tie_peak_flip_is_the_host_libm(tmp_path: Path) -> None:
    """Sol r2 P1-01 (seed 2026111417, candidate 531): a mathematical tie between (-1, -1) and
    (1, 1). numpy breaks it by one ulp that comes from Apple's cos/sin; the shipped port, with
    correctly rounded twiddles, gets an exact tie and keeps the first maximum. Manager decision
    after Sol r2: the libm rule covers this peak, with these five assertions."""
    a, b = _near_tie()
    ref = fr.phase_plane(a, b)
    # 1. With the host's cos/sin the whole plane is numpy's, bit for bit.
    host, host_peak = fr.rust_plane(tmp_path, a, b, "host")
    assert fr.bit_diff(ref, host) == 0, fr.first_diff(ref, host)
    assert host_peak == (-1, -1)
    # 2. The host and shipped twiddle tables of these lengths (8; 89 is Bluestein: 178 and 180)
    #    differ only at the listed angles, where the shipped value is the correctly rounded one.
    listed = 0
    for table in sorted({t for n in a.shape for t in fr.twiddle_tables(n)}):
        shipped, _ = fr.rust_tables(tmp_path, table, "tables")
        host_t, _ = fr.rust_tables(tmp_path, table, "tables", "host")
        assert fr.bit_diff(fr.table_values(table, "cr"), shipped) == 0, table
        assert fr.bit_diff(fr.table_values(table, "host"), host_t) == 0, table
        bits = shipped[:, :2].view(np.uint64) != host_t[:, :2].view(np.uint64)
        differ = {(int(r), int(c)) for r, c in zip(*np.nonzero(bits), strict=True)}
        cases = {(c["row"], c["part"]) for c in fr.host_vs_cr(table)}
        assert differ == cases, (table, sorted(differ ^ cases))
        listed += len(cases)
    assert listed > 0
    # 3. numpy's peak is (-1, -1); the shipped port's is (1, 1).
    assert g._phase_shift(a, b) == (-1, -1)
    got, peak = fr.rust_plane(tmp_path, a, b)
    assert peak == (1, 1)
    assert hp.ok(["hdr-phase", "--a", hp.save(tmp_path / "a.npy", a), "--b", hp.save(tmp_path / "b.npy", b)]) == {"dy": 1, "dx": 1}
    # 4. The shipped plane ties exactly at both places, which hold its maximum, and the two
    #    correlations are equal.
    top = got.max()
    assert got[7, 88].view(np.uint32) == got[1, 1].view(np.uint32) == top.view(np.uint32)
    assert ref[7, 88] > ref[1, 1] == got[1, 1]
    corr = [float(np.mean(a * np.roll(b, s, (0, 1)))) for s in ((-1, -1), (1, 1))]
    assert corr[0] == corr[1] == 91.67134857177734
    # 5. The full align on this input and on its positive-luma variant, against Python.
    stated = []
    for label, (sdr, raw) in {
        "original": (a, b),
        "positive-luma": (np.float32(0.45) + np.float32(0.002) * a, np.float32(0.45) + np.float32(0.002) * b),
    }.items():
        al = g.align(sdr, raw)
        rust = hp.ok(["hdr-align", "--sdr", hp.save(tmp_path / "s.npy", sdr), "--raw", hp.save(tmp_path / "r.npy", raw)])
        assert rust == {"alignment": hp.alignment_fields(al), "check": hp.check_of(al)}, label
        stated.append(f"{label}: align equal ({al.dy}, {al.dx}) corr {al.correlation!r}")
    print(f"near tie: {listed} listed twiddle values; plane differs in {fr.bit_diff(ref, got)}/{ref.size}; " + "; ".join(stated))


@needs_reference
def test_random_planes(tmp_path: Path) -> None:
    rng = np.random.default_rng(SEED)
    stated = []
    for i in range(200):
        h, w = int(rng.integers(1, 97)), int(rng.integers(1, 97))
        a = rng.standard_normal((h, w)).astype(np.float32)
        shift = (int(rng.integers(-3, 4)), int(rng.integers(-3, 4)))
        b = (np.roll(a, shift, (0, 1)) + np.float32(0.3) * rng.standard_normal((h, w))).astype(np.float32)
        if diff := _plane_gate(tmp_path, a, b, f"random {i} {h}x{w}"):
            stated.append(f"{h}x{w}:{diff}")
    print("libm-affected planes (values differing, peak unchanged):", stated)


REAL = hp.real_pairs()


@needs_reference
@pytest.mark.parametrize("pair", REAL, ids=[p[1].name for p in REAL])
def test_real_pair_planes(tmp_path: Path, pair: tuple[Path, Path]) -> None:
    """The five planes ``align`` computes on a real A12 pair."""
    from gxr_studio.raw import open_dng
    from gxr_studio.raw.dng import RawDecodeError

    jpg, dng = pair
    try:
        raw = g.raw_luma(open_dng(dng))
    except RawDecodeError:
        pytest.skip(f"{dng.name}: Python cannot decode it")
    jpeg = jpg.read_bytes()
    sdr = g.sdr_luma_grid(g.decode_rgb(jpeg), 4)
    stored = g.to_storage(raw.y, g.jpeg_orientation(jpeg))
    a = g._norm(g._hp(sdr))
    stated = []
    for s in g.SCALES:
        b = g._norm(g._hp(g.warp(stored, a.shape, s, 0, 0)))
        stated.append(f"{s}:{_plane_gate(tmp_path, a, b, f'{dng.name} scale {s}')}")
    print(f"{dng.name} {a.shape}: values differing per scale (libm) {stated}")


# ---------------------------------------------------------------- errors, sizes and WASM


def test_errors_match_numpy(tmp_path: Path) -> None:
    empty = tmp_path / "e.npy"
    np.save(empty, np.zeros((1, 0, 2), np.float32))
    with pytest.raises(ValueError) as exc:
        np.fft.fft(np.zeros((1, 0), np.complex64))
    _, err = fr.call(["hdr-fft", "--in", str(empty), "--inverse", "0", "--out", str(tmp_path / "o.npy")])
    assert err is not None and (err.kind, err.message) == ("value_error", str(exc.value))
    src = tmp_path / "x.npy"
    np.save(src, np.zeros((2, 3, 2), np.float32))
    for h, n in ((4294967295, 4294967295), (2147483648, 1), (1, 2147483648)):
        _, err = fr.call(["hdr-fft", "--in", str(src), "--inverse", "0", "--in-shape", str(h), str(n), "--out", str(tmp_path / "o.npy")])
        assert err is not None and (err.kind, err.message) == ("value_error", ARRAY_TOO_BIG), (h, n)
    plane = hp.save(tmp_path / "p.npy", np.zeros((3, 4), np.float32))
    # 2**31 elements pass the count limit and then do not match the 12 values given.
    for (h, w), message in (((4294967295, 4294967295), ARRAY_TOO_BIG), ((2147483648, 1), "数组长度和形状对不上")):
        dims = ["--a-shape", str(h), str(w), "--b-shape", str(h), str(w)]
        _, err = fr.call(["hdr-phase-plane", "--a", plane, "--b", plane, *dims, "--out", str(tmp_path / "o.npy")])
        assert err is not None and err.message == message, (h, w)
    _, err = fr.call(["hdr-fft-twiddle", "--n", "4294967295", "--out", str(tmp_path / "t.npy")])
    assert err is not None and err.message == ARRAY_TOO_BIG


@pytest.mark.parametrize("inverse", [False, True])
def test_empty_batch_matches_numpy(tmp_path: Path, inverse: bool) -> None:
    """(0, 3): 1-D returns an empty (0, 3) array; 2-D reports axis 0's point count, 0."""
    x = np.empty((0, 3), np.complex64)
    ref = fr.numpy_fft(x, inverse=inverse)
    got = fr.rust_fft(tmp_path, x, inverse=inverse)
    assert ref.dtype == got.dtype == np.dtype(np.complex64)
    assert ref.shape == got.shape == (0, 3)
    src = tmp_path / "e.npy"
    np.save(src, fr.as_pairs(x))
    assert np.load(src).shape == (0, 3, 2)
    with pytest.raises(ValueError) as exc:
        fr.numpy_fft(x, inverse=inverse, two_d=True)
    args = ["hdr-fft", "--in", str(src), "--inverse", "1" if inverse else "0", "--two-d", "--out", str(tmp_path / "o.npy")]
    _, err = fr.call(args)
    assert err is not None and (err.kind, err.message) == ("value_error", str(exc.value))
    assert str(exc.value) == "Invalid number of FFT data points (0) specified."


def test_allocation_failure_is_a_core_error(tmp_path: Path) -> None:
    """Sol r2 P2-08: the n-th allocation inside an FFT entry point returns null, for every n.
    The core must answer with its size error, never abort."""
    binary = fa.build(tmp_path)
    if binary is None:
        if os.environ.get("GXR_REQUIRE_CORE") == "1":
            pytest.fail("cargo is required to build the allocation-failure driver")
        pytest.skip("cargo is not on PATH")
    checked = {}
    for op in fa.OPS:
        base = fa.run(binary, op, 0)
        assert base.returncode == 0 and base.stdout.startswith("ok "), (op, base.stdout, base.stderr)
        count = int(base.stdout.split()[1])
        assert count > 0
        for nth in range(1, count + 1):
            got = fa.run(binary, op, nth)
            assert got.returncode == 0, f"{op} allocation {nth}: exit {got.returncode} {got.stderr.strip()}"
            assert got.stdout.strip() == f"error value_error: {ARRAY_TOO_BIG}", (op, nth, got.stdout)
        checked[op] = count
    print("allocations failed one at a time:", checked)


def test_wasm_matches_native(tmp_path: Path) -> None:
    rng = np.random.default_rng(SEED)
    folders = []
    for name, (h, n) in {"small": (3, 5), "blue": (4, 89), "grid": (5, 712)}.items():
        d = tmp_path / name
        d.mkdir()
        np.save(d / "fft.npy", fr.as_pairs(_inputs(rng, 1, n)[:h]))
        a = rng.standard_normal((h * 4, n)).astype(np.float32)
        np.save(d / "pa.npy", a)
        np.save(d / "pb.npy", (np.roll(a, 2, 1) + np.float32(0.2) * rng.standard_normal(a.shape)).astype(np.float32))
        folders.append(d)
    a, b = _sol()
    d = tmp_path / "sol"
    d.mkdir()
    np.save(d / "pa.npy", a)
    np.save(d / "pb.npy", b)
    np.save(d / "fft0.npy", np.zeros((1, 0, 2), np.float32))
    np.save(d / "fft-empty.npy", np.zeros((0, 3, 2), np.float32))
    nan = a.copy()
    nan[1, 2] = np.nan
    d2 = tmp_path / "nan"
    d2.mkdir()
    np.save(d2 / "pa.npy", nan)
    np.save(d2 / "pb.npy", b)
    proc = run_area("hdr", [*folders, d, d2], tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "wasm matches native" in proc.stdout
