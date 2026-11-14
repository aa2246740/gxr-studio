"""Helpers for the numpy-exact HDR FFT (unit hdr-fft): run ``gxr-cli hdr-fft*`` and mirror
the parts of numpy 2.4.3 that the Rust port must reproduce.

What numpy does for ``_phase_shift`` (float32 ``a``, ``b``), established from the 2.4.3
source and the macOS arm64 wheel:

- ``np.fft.fft2``: per axis (last first) ``pfu.fft(x, 1)``. The Python int ``1`` selects the
  complex128 loop, so pocketfft runs in double and the result is rounded to complex64.
- ``*``, ``np.abs`` (``simd_cabsolute_f32``), ``+ 1e-9`` (float32), ``/`` (``CFLOAT_divide``).
- ``np.fft.ifft2``: per axis ``pfu.ifft(x, float32(1/n))``, the complex64 loop (float).

The twiddles come from ``sincos_2pibyn``: ``cos``/``sin`` in double of ``x * (pi/4)/n``,
folded to the first octant. The host's (Apple's) ``cos``/``sin`` are not correctly rounded on
every such angle; the port uses the correctly rounded value (PORT-WAVE-1 libm rule), so this
module also computes that value with ``decimal``.
"""

from __future__ import annotations

import math
import struct
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np

from support.core_cli import CoreCliError, error_of, run_json


def call(args: list[str]) -> tuple[Any, CoreCliError | None]:
    code, payload = run_json(args)
    err = error_of(code, payload)
    return (None, err) if err is not None else (payload, None)


def ok(args: list[str]) -> Any:
    payload, err = call(args)
    if err is not None:
        raise AssertionError(f"gxr-cli {args[0]} failed: {err.kind}: {err.message}")
    return payload


def as_pairs(x: np.ndarray) -> np.ndarray:
    """complex64 ``(..., n)`` as float32 ``(..., n, 2)``."""
    x = np.asarray(x, np.complex64)
    return np.ascontiguousarray(np.stack([x.real, x.imag], axis=-1).astype(np.float32))


def from_pairs(y: np.ndarray) -> np.ndarray:
    assert y.dtype == np.float32 and y.shape[-1] == 2
    return np.ascontiguousarray(y).view(np.complex64)[..., 0]


def rust_fft(tmp: Path, x: np.ndarray, *, inverse: bool, two_d: bool = False, trig: str | None = None) -> np.ndarray:
    """``gxr-cli hdr-fft`` on complex64 ``x`` of shape ``(n,)`` or ``(h, n)``."""
    src, dst = tmp / "fft-in.npy", tmp / "fft-out.npy"
    np.save(src, as_pairs(x))
    args = ["hdr-fft", "--in", str(src), "--inverse", "1" if inverse else "0", "--out", str(dst)]
    if two_d:
        args.append("--two-d")
    if trig:
        args += ["--trig", trig]
    ok(args)
    return from_pairs(np.load(dst))


def numpy_fft(x: np.ndarray, *, inverse: bool, two_d: bool = False) -> np.ndarray:
    x = np.asarray(x, np.complex64)
    if two_d:
        return np.fft.ifft2(x) if inverse else np.fft.fft2(x)
    return np.fft.ifft(x) if inverse else np.fft.fft(x)


def phase_plane(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """``_phase_shift``'s plane, exactly as gain.py computes it."""
    f = np.fft.fft2(a) * np.conj(np.fft.fft2(b))
    return np.real(np.fft.ifft2(f / (np.abs(f) + 1e-9)))


def rust_plane(tmp: Path, a: np.ndarray, b: np.ndarray, trig: str | None = None) -> tuple[np.ndarray, tuple[int, int]]:
    pa, pb, out = tmp / "pa.npy", tmp / "pb.npy", tmp / "pc.npy"
    np.save(pa, np.ascontiguousarray(a, np.float32))
    np.save(pb, np.ascontiguousarray(b, np.float32))
    args = ["hdr-phase-plane", "--a", str(pa), "--b", str(pb), "--out", str(out)]
    if trig:
        args += ["--trig", trig]
    got = ok(args)
    return np.load(out), (got["dy"], got["dx"])


def bit_diff(expect: np.ndarray, got: np.ndarray) -> int:
    """Count of float32 values whose bits differ; NaN equals NaN at the same place only."""
    assert expect.dtype == got.dtype and expect.shape == got.shape, (expect.dtype, got.dtype, expect.shape, got.shape)
    e = np.ascontiguousarray(expect).view(np.uint32 if expect.dtype in (np.float32, np.complex64) else np.uint64)
    r = np.ascontiguousarray(got).view(e.dtype)
    ef = np.ascontiguousarray(expect).view(np.float32 if e.dtype == np.uint32 else np.float64)
    rf = np.ascontiguousarray(got).view(ef.dtype)
    both_nan = np.isnan(ef) & np.isnan(rf)
    return int(((e != r) & ~both_nan).sum())


def first_diff(expect: np.ndarray, got: np.ndarray) -> str:
    e = np.ascontiguousarray(expect).reshape(-1).view(np.float32)
    r = np.ascontiguousarray(got).reshape(-1).view(np.float32)
    bad = np.flatnonzero((e.view(np.uint32) != r.view(np.uint32)) & ~(np.isnan(e) & np.isnan(r)))
    if not bad.size:
        return "none"
    i = int(bad[0])
    return f"flat float {i}: numpy {e[i]!r} (0x{int(e.view(np.uint32)[i]):08x}) rust {r[i]!r} (0x{int(r.view(np.uint32)[i]):08x})"


# ---------------------------------------------------------------- pocketfft's plan choice


def largest_prime_factor(n: int) -> int:
    res = 1
    while n % 2 == 0:
        res, n = 2, n // 2
    x = 3
    while x * x <= n:
        while n % x == 0:
            res, n = x, n // x
        x += 2
    return n if n > 1 else res


def cost_guess(n: int) -> float:
    ni, result = n, 0.0
    while n % 2 == 0:
        result += 2
        n //= 2
    x = 3
    while x * x <= n:
        while n % x == 0:
            result += x if x <= 5 else 1.1 * x
            n //= x
        x += 2
    if n > 1:
        result += n if n <= 5 else 1.1 * n
    return result * ni


def good_size_cmplx(n: int) -> int:
    if n <= 12:
        return n
    best = 2 * n
    f11 = 1
    while f11 < best:
        f117 = f11
        while f117 < best:
            f1175 = f117
            while f1175 < best:
                x = f1175
                while x < n:
                    x *= 2
                while True:
                    if x < n:
                        x *= 3
                    elif x > n:
                        best = min(best, x)
                        if x & 1:
                            break
                        x >>= 1
                    else:
                        return n
                f1175 *= 5
            f117 *= 7
        f11 *= 11
    return best


def uses_bluestein(n: int) -> bool:
    tmp = 0 if n < 50 else largest_prime_factor(n)
    if tmp * tmp <= n:
        return False
    return 1.5 * (2 * cost_guess(good_size_cmplx(2 * n - 1))) < cost_guess(n)


def twiddle_tables(n: int) -> list[int]:
    """The ``sincos_2pibyn`` table lengths a transform of length ``n`` builds."""
    if n == 1:
        return []
    if uses_bluestein(n):
        n2 = good_size_cmplx(2 * n - 1)
        return [2 * n, n2]
    return [n]


# ---------------------------------------------------------------- twiddle tables


def fma(a: float, b: float, c: float) -> float:
    """``a*b + c`` rounded once (``math.fma`` needs Python 3.13; Fraction to float rounds to nearest)."""
    if not all(math.isfinite(v) for v in (a, b, c)):
        return a * b + c
    exact = Fraction(a) * Fraction(b) + Fraction(c)
    if exact == 0:
        # IEEE: an exact zero sum is -0 only when both addends are -0 (round to nearest).
        product_negative = math.copysign(1.0, a) * math.copysign(1.0, b) < 0
        both_zero = (a == 0 or b == 0) and c == 0
        return -0.0 if both_zero and product_negative and math.copysign(1.0, c) < 0 else 0.0
    return float(exact)


def _bits(v: float) -> int:
    return int(struct.unpack("<Q", struct.pack("<d", v))[0])


def cr(x: float, which: str) -> float:
    """Correctly rounded ``cos``/``sin`` of the double ``x`` in ``[0, pi/4]`` (Taylor series
    in 80-digit ``decimal``; float(Decimal) rounds once, to nearest)."""
    with localcontext() as ctx:
        ctx.prec = 80
        xd = Decimal(x)
        x2 = xd * xd
        term = Decimal(1) if which == "cos" else xd
        total, k = term, 0 if which == "cos" else 1
        while True:
            term = -term * x2 / ((k + 1) * (k + 2))
            k += 2
            nxt = total + term
            if nxt == total:
                break
            total = nxt
        return float(total)


def calc_args(x: int, n: int) -> tuple[tuple[str, int, float], tuple[str, int, float]]:
    """``calc(x, n, ang)``: for the real and the imaginary part, the function (``cos`` or
    ``sin``), the sign, and the angle passed to it."""
    ang = 0.25 * math.pi / n
    x <<= 3
    if x < 4 * n:
        if x < 2 * n:
            if x < n:
                return ("cos", 1, x * ang), ("sin", 1, x * ang)
            return ("sin", 1, (2 * n - x) * ang), ("cos", 1, (2 * n - x) * ang)
        x -= 2 * n
        if x < n:
            return ("sin", -1, x * ang), ("cos", 1, x * ang)
        return ("cos", -1, (2 * n - x) * ang), ("sin", 1, (2 * n - x) * ang)
    x = 8 * n - x
    if x < 2 * n:
        if x < n:
            return ("cos", 1, x * ang), ("sin", -1, x * ang)
        return ("sin", 1, (2 * n - x) * ang), ("cos", -1, (2 * n - x) * ang)
    x -= 2 * n
    if x < n:
        return ("sin", -1, x * ang), ("cos", -1, x * ang)
    return ("cos", -1, (2 * n - x) * ang), ("sin", -1, (2 * n - x) * ang)


def table_args(n: int) -> list[tuple[tuple[str, int, float], tuple[str, int, float]] | None]:
    """The ``calc`` calls behind ``v1`` then ``v2`` (``None`` for the exact ``(1, 0)`` entries)."""
    nval = (n + 2) // 2
    shift = 1
    while (1 << shift) * (1 << shift) < nval:
        shift += 1
    mask = (1 << shift) - 1
    v1: list[Any] = [None] + [calc_args(i, n) for i in range(1, mask + 1)]
    v2: list[Any] = [None] + [calc_args(i * (mask + 1), n) for i in range(1, (nval + mask) // (mask + 1))]
    return v1 + v2


def table_values(n: int, how: str) -> np.ndarray:
    """``v1`` then ``v2`` as ``(len, 3)`` float64 rows ``(re, im, angle)``, from the host's
    ``math.cos``/``math.sin`` (``how="host"``) or the correctly rounded values (``how="cr"``)."""
    rows = []
    for entry in table_args(n):
        if entry is None:
            rows.append((1.0, 0.0, 0.0))
            continue
        pair = []
        for fn, sign, x in entry:
            v = (math.cos(x) if fn == "cos" else math.sin(x)) if how == "host" else cr(x, fn)
            pair.append(-v if sign < 0 else v)
        rows.append((pair[0], pair[1], entry[0][2]))
    return np.array(rows, np.float64)


def twiddles_from(tables: np.ndarray, n: int, v1_len: int, width: type) -> np.ndarray:
    """``operator[](idx)`` for every ``idx < n`` from raw tables, as pocketfft fuses it."""
    nval = (n + 2) // 2
    shift = 1
    while (1 << shift) * (1 << shift) < nval:
        shift += 1
    mask = (1 << shift) - 1
    v1, v2 = tables[:v1_len, :2], tables[v1_len:, :2]
    out = np.empty((n, 2), np.float64)
    for idx in range(n):
        k, flip = (idx, False) if 2 * idx <= n else (n - idx, True)
        x1, x2 = v1[k & mask], v2[k >> shift]
        r = fma(float(x1[0]), float(x2[0]), -(float(x1[1]) * float(x2[1])))
        i = fma(float(x1[0]), float(x2[1]), float(x1[1]) * float(x2[0]))
        r, i = (float(np.float32(r)), float(np.float32(i))) if width is np.float32 else (r, i)
        out[idx] = (r, -i if flip else i)
    return out.astype(width)


def rust_tables(tmp: Path, n: int, kind: str, trig: str | None = None) -> tuple[np.ndarray, int]:
    out = tmp / f"tw-{n}-{kind}.npy"
    args = ["hdr-fft-twiddle", "--n", str(n), "--kind", kind, "--out", str(out)]
    if trig:
        args += ["--trig", trig]
    got = ok(args)
    return np.load(out), int(got["v1"])


def host_vs_cr(n: int) -> list[dict[str, Any]]:
    """Every ``cos``/``sin`` value in table ``n`` where the host differs from the correctly
    rounded value: the row, which part, the angle bits, and both results as bits."""
    found = []
    for row, entry in enumerate(table_args(n)):
        if entry is None:
            continue
        for part, (fn, _sign, x) in enumerate(entry):
            host = math.cos(x) if fn == "cos" else math.sin(x)
            want = cr(x, fn)
            if _bits(host) != _bits(want):
                found.append({"n": n, "row": row, "part": part, "fn": fn, "x": _bits(x), "host": _bits(host), "cr": _bits(want)})
    return found
