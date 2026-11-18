"""Platform libm rule (PORT-WAVE-1, 2026-11-13) for the export pipeline's real-photo gates.

macOS libSystem is not correctly rounded for some inputs; the phone core is (``gxr_math``). Where
Python and Rust differ only because of that, the oracle is the correctly rounded value, computed
independently, and every difference must be attributed mechanically:

- **Gate A**: Rust equals the desktop's own computation run with the correctly rounded function,
  exactly (float32 bits, NaN at the same places; uint8 codes);
- **Gate B**: every value where Rust and the desktop differ is one where the desktop differs from
  that oracle (zero unattributed values), and the desktop recomputed with the host function
  reproduces the desktop's own result (the negative control).

Two host functions reach the export pipeline (unit parity-drift-1):

- ``tan`` in ``finish/geometry._slider_s`` (perspective sliders): the HDR data layer warped with it;
- ``powf`` in ``raw/render.srgb_encode``: the neutral RAW base (rawrender's known inputs, for
  example ``0x3eadd76b``).

Like ``export_pipeline_cases``, nothing here prints photo-derived values: failures go through the
redacted helpers and the results are counts.
"""

from __future__ import annotations

import math
import types
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

import numpy as np

from support.export_pipeline_cases import check, same_pixels, same_redacted


def cr_tan(x: float) -> float:
    """``tan(x)`` correctly rounded to float64: sine and cosine series at 80 digits, rounded once.

    The geometry sliders only ask for ``|x| <= 30°`` (0.53 rad), so no argument reduction is needed.
    """
    check(abs(x) < 1.0, "cr_tan argument out of range")
    with localcontext() as ctx:
        ctx.prec = 80
        big = Decimal(x)
        tiny = Decimal(10) ** -78
        term, k, sin = big, 0, Decimal(0)
        while abs(term) > tiny:
            sin += term
            term = -term * big * big / ((2 * k + 2) * (2 * k + 3))
            k += 1
        term, k, cos = Decimal(1), 0, Decimal(0)
        while abs(term) > tiny:
            cos += term
            term = -term * big * big / ((2 * k + 1) * (2 * k + 2))
            k += 1
        return float(sin / cos)


@contextmanager
def geometry_tan(tan: Callable[[float], float]) -> Iterator[list[float]]:
    """``finish.geometry`` with ``math.tan`` replaced by ``tan``; yields the arguments it was called with."""
    from gxr_studio.finish import geometry as geo

    calls: list[float] = []

    def logged(x: float) -> float:
        calls.append(x)
        return tan(x)

    shim = types.SimpleNamespace(**{name: getattr(math, name) for name in dir(math) if not name.startswith("_")})
    shim.tan = logged
    real = geo.math
    geo.math = shim  # type: ignore[assignment]
    try:
        yield calls
    finally:
        geo.math = real


def replay_track(layer: np.ndarray, ops: list[tuple[str, tuple[Any, ...]]], tan: Callable[[float], float]) -> tuple[np.ndarray, list[float]]:
    """The desktop's own ``HdrTrack`` geometry steps (``warp`` / ``box`` / ``crop``, in the order the
    export called them) on ``layer``, with ``tan`` in ``finish.geometry``."""
    from gxr_studio.finish.hdr_export import HdrTrack

    track = HdrTrack(dng=None)
    track.layer = np.array(layer)
    with geometry_tan(tan) as calls:
        for name, args in ops:
            getattr(HdrTrack, name)(track, *args)
    check(track.layer is not None, "replayed track has no layer")
    return np.asarray(track.layer), calls


def _bits_differ(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return (a.view(np.uint32) != b.view(np.uint32)) & ~(np.isnan(a) & np.isnan(b))


def _same_f32(got: np.ndarray, want: np.ndarray, label: str) -> None:
    """Exact float32 bits, NaN matching NaN; reports shapes and counts only."""
    same_redacted([str(got.dtype), list(got.shape)], [str(want.dtype), list(want.shape)], label)
    same_pixels(_bits_differ(got, want), np.zeros(got.shape, bool), label)


def layer_tan(
    python: np.ndarray, rust: np.ndarray, before: np.ndarray | None, ops: list[tuple[str, tuple[Any, ...]]], label: str
) -> dict[str, Any] | None:
    """The HDR track layer before ``stamp``: ``None`` when Rust's equals the desktop's bit for bit.

    Otherwise the difference must come from the host ``tan`` (Gate A/B above): the desktop's own
    geometry steps, replayed from the layer it had before them, give the desktop's layer with the
    host ``tan`` and Rust's layer with the correctly rounded one. Returns the counts.
    """
    if python.shape == rust.shape and python.dtype == rust.dtype == np.float32 and not _bits_differ(python, rust).any():
        return None
    check(before is not None and bool(ops), "layer differs with no geometry step to replay")
    assert before is not None
    host, host_calls = replay_track(before, ops, math.tan)
    _same_f32(host, python, f"{label}: replay with the host tan")
    oracle, calls = replay_track(before, ops, cr_tan)
    _same_f32(rust, oracle, f"{label}: Gate A (correctly rounded tan)")
    differ = _bits_differ(python, rust)
    unattributed = differ & ~_bits_differ(python, oracle)
    same_redacted(int(unattributed.sum()), 0, f"{label}: Gate B unattributed values")
    off = [x for x in calls if math.tan(x) != cr_tan(x)]
    check(bool(off), "a layer difference with no host tan off")
    first = tuple(int(v) for v in np.argwhere(differ)[0])
    ulps = np.abs(python.view(np.int32)[differ].astype(np.int64) - rust.view(np.int32)[differ].astype(np.int64))
    return {
        "values": int(differ.sum()),
        "unattributed": int(unattributed.sum()),
        "first": list(first),
        "max_ulp": int(ulps.max()),
        "host_replay_vs_rust": int(_bits_differ(host, rust).sum()),
        "tan_calls": len(calls),
        "tan_off": len(off),
        "host_calls": len(host_calls),
    }


def neutral_base_powf(python: np.ndarray, rust: np.ndarray, dng: Path, tmp: Path, label: str) -> dict[str, int]:
    """Every uint8 difference between Python's and Rust's neutral RAW base (``render_full(dng,
    NEUTRAL)``, which is what a ``neutral`` export renders) must be the host ``powf`` in
    ``srgb_encode`` (rawrender's attribution, redacted):

    1. the pre-quantisation float32 code is bit-identical (Python ``demosaic_strips`` vs Rust
       ``dng-demosaic``, the whole strip);
    2. Rust's power bits (``dng-srgb-pow``) are the correctly rounded ``powf`` (60-digit
       ``Decimal``) and numpy's differ;
    3. each side's uint8 follows from its own power, and they differ by exactly 1.

    Returns the counts: differing values and distinct ``powf`` inputs.
    """
    from gxr_studio.raw import open_dng
    from gxr_studio.raw.codes import CODE_MAX
    from gxr_studio.raw.demosaic import demosaic_strips
    from support.core_cli import run_json, run_npy
    from support.raw_render_parity import _correctly_rounded_powf, _unoriented_yx

    same_redacted([str(python.dtype), list(python.shape)], [str(rust.dtype), list(rust.shape)], f"{label}: base shapes")
    diffs = np.argwhere(python != rust)
    link = tmp / f"{label.split()[0]}-neutral.DNG"  # a neutral name: nothing below prints the photo's
    if not link.exists():
        link.symlink_to(dng)
    raw = open_dng(link)
    h, w = raw.crop_size[1], raw.crop_size[0]
    strips = list(demosaic_strips(raw))
    rs_strips: dict[int, np.ndarray] = {}
    inputs: set[int] = set()
    for y, x, c in (tuple(int(v) for v in d) for d in diffs):
        uy, ux = _unoriented_yx(y, x, h, w, raw.orientation)
        y0, strip = next((s0, s) for s0, s in strips if s0 <= uy < s0 + s.shape[0])
        if y0 not in rs_strips:
            args = ["dng-demosaic", "--dng", str(link), "--y0", str(y0), "--y1", str(y0 + strip.shape[0])]
            rs_strips[y0] = run_npy(args, tmp / f"{link.stem}-dm-{y0}.npy")
            _same_f32(rs_strips[y0], strip, f"{label}: demosaic strip")
        code = strip[uy - y0, ux, c]
        a = np.clip(code / np.float32(CODE_MAX), np.float32(0.0), np.float32(1.0))
        check(bool(a > np.float32(0.0031308)), "a base difference in the linear segment")
        bits = int(a.view(np.uint32))
        rc, pow_json = run_json(["dng-srgb-pow", "--bits", f"0x{bits:08x}"])
        check(rc == 0 and "power_bits" in pow_json, "dng-srgb-pow failed")
        oracle = _correctly_rounded_powf(a)
        host = np.power(np.array([a], np.float32), 1 / 2.4, dtype=np.float32)[0]
        same_redacted(str(pow_json["power_bits"]), f"0x{int(oracle.view(np.uint32)):08x}", f"{label}: Rust powf is correctly rounded")
        check(int(oracle.view(np.uint32)) != int(host.view(np.uint32)), "base difference where the host powf is right")
        u8_oracle = int(np.rint((np.float32(1.055) * oracle - np.float32(0.055)) * np.float32(255.0)))
        u8_host = int(np.rint((np.float32(1.055) * host - np.float32(0.055)) * np.float32(255.0)))
        same_redacted([int(rust[y, x, c]), int(python[y, x, c])], [u8_oracle, u8_host], f"{label}: uint8 from each power")
        check(abs(u8_oracle - u8_host) == 1, "base difference larger than one code")
        inputs.add(bits)
    return {"base_values": len(diffs), "powf_inputs": len(inputs)}
