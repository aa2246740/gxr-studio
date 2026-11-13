"""Helpers for ``tests/test_core_parity_hdr.py``: run ``gxr-cli hdr-*`` and compare with Python.

Gates (UNIT-hdr): float32 arrays exact bits (NaN matches NaN), uint8/bool/int arrays
exact, float64 filter outputs within ``1e-12 * max(1, |a|)``, every ``Alignment`` field
exact, ``GainMeta`` fractions exact, error kind and message exact. A miss names the first
index and both values.
"""

from __future__ import annotations

import json
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from gxr_studio.hdr import gain as g
from gxr_studio.hdr.container import ContainerError
from support.core_cli import CoreCliError, error_of, run_json

_STUDIO = Path(__file__).resolve().parents[2]
_PARENT = Path(os.environ.get("GXR_STUDIO_TEST_PARENT", str(_STUDIO.parent)))
CAPTURES = _PARENT / "captures"


def call(args: list[str]) -> tuple[Any, CoreCliError | None]:
    """Run ``gxr-cli`` and return ``(payload, None)`` or ``(None, error)``."""
    code, payload = run_json(args)
    err = error_of(code, payload)
    return (None, err) if err is not None else (payload, None)


def ok(args: list[str]) -> Any:
    payload, err = call(args)
    if err is not None:
        pytest.fail(f"gxr-cli {args[0]} failed: {err.kind}: {err.message}")
    return payload


def save(path: Path, array: np.ndarray) -> str:
    np.save(path, np.ascontiguousarray(array))
    return str(path)


def py_kind(exc: BaseException) -> str:
    """The ``kind`` the Rust port gives each Python exception on this path."""
    if isinstance(exc, g.HdrUnsupported):
        return exc.reason
    if isinstance(exc, ContainerError):
        return "container"
    if isinstance(exc, struct.error):
        return "struct_error"
    if isinstance(exc, OverflowError):
        return "overflow_error"
    return "value_error" if isinstance(exc, ValueError) else type(exc).__name__


def same_error(exc: BaseException, err: CoreCliError | None) -> None:
    assert err is not None, f"python raised {type(exc).__name__}: {exc}; rust succeeded"
    assert (py_kind(exc), str(exc)) == (err.kind, err.message)


def _first(mask: np.ndarray) -> tuple[int, ...]:
    return tuple(int(i) for i in np.argwhere(mask)[0])


def same_f32(python: np.ndarray, rust: np.ndarray, what: str) -> None:
    """Exact float32 bits; NaN matches NaN."""
    assert python.dtype == np.float32 and rust.dtype == np.float32, f"{what}: dtype {python.dtype} vs {rust.dtype}"
    assert python.shape == rust.shape, f"{what}: shape {python.shape} vs {rust.shape}"
    both_nan = np.isnan(python) & np.isnan(rust)
    differ = (python.view(np.uint32) != rust.view(np.uint32)) & ~both_nan
    if differ.any():
        at = _first(differ)
        pytest.fail(f"{what}: {int(differ.sum())} of {python.size} differ; first {at}: python={python[at]!r} rust={rust[at]!r}")


def same_exact(python: np.ndarray, rust: np.ndarray, what: str) -> None:
    assert python.dtype == rust.dtype, f"{what}: dtype {python.dtype} vs {rust.dtype}"
    assert python.shape == rust.shape, f"{what}: shape {python.shape} vs {rust.shape}"
    if not np.array_equal(python, rust):
        at = _first(python != rust)
        pytest.fail(f"{what}: {int((python != rust).sum())} differ; first {at}: python={python[at]!r} rust={rust[at]!r}")


def close_f64(python: np.ndarray, rust: np.ndarray, what: str) -> int:
    """``|a - b| <= 1e-12 * max(1, |a|)``; returns how many are not bit-identical."""
    assert python.dtype == np.float64 and rust.dtype == np.float64, f"{what}: dtype {python.dtype} vs {rust.dtype}"
    assert python.shape == rust.shape, f"{what}: shape {python.shape} vs {rust.shape}"
    both_nan = np.isnan(python) & np.isnan(rust)
    with np.errstate(invalid="ignore"):
        off = (np.abs(python - rust) > 1e-12 * np.maximum(1.0, np.abs(python))) & ~both_nan
        off |= np.isnan(python) != np.isnan(rust)
    if off.any():
        at = _first(off)
        pytest.fail(f"{what}: first {at}: python={python[at]!r} rust={rust[at]!r}")
    return int(((python != rust) & ~both_nan).sum())


def alignment_fields(al: g.Alignment) -> dict[str, Any]:
    return {
        "scale": al.scale,
        "dy": al.dy,
        "dx": al.dx,
        "correlation": al.correlation,
        "tiles_moved": al.tiles_moved,
        "shift_frac": al.shift_frac,
        "tiles_unverified": al.tiles_unverified,
    }


def check_of(al: g.Alignment) -> dict[str, str] | None:
    try:
        g.check_alignment(al)
    except g.HdrUnsupported as exc:
        return {"kind": exc.reason, "message": str(exc)}
    return None


def codes_of(result: g.GainResult) -> np.ndarray:
    """``encode_gainmap``'s uint8 plane, before Pillow encodes it."""
    m = result.meta
    r = np.clip((result.delta - m.gain_min) / (m.gain_max - m.gain_min), 0, 1) ** m.gamma
    return np.floor(r * 255.0 + 0.5).astype(np.uint8)


def gain_expect(result: g.GainResult) -> dict[str, Any]:
    from fractions import Fraction

    m = result.meta
    names = ("gain_min", "gain_max", "gamma", "offset_sdr", "offset_hdr", "cap_min", "cap_max")
    meta = {k: getattr(m, k) for k in names}
    fracs = {}
    for k in names:
        f = Fraction(meta[k]).limit_denominator(1 << 16)
        fracs[k] = [f.numerator, f.denominator]
    return {"meta": meta, "fractions": fracs, "stats": json.loads(json.dumps(result.stats))}


@dataclass
class Flow:
    """Python and Rust results of one RAW+JPG pair, compared stage by stage."""

    stages: list[str]
    alignment: dict[str, Any]
    check: dict[str, str] | None


def compare_gain(tmp: Path, sdr: np.ndarray, y: np.ndarray, clip: np.ndarray, params: dict[str, Any] | None, tag: str) -> str:
    """``compute_gain`` both ways on the same registered inputs. Returns the outcome."""
    p = g.Params(**(params or {}))
    args = ["hdr-gain", "--sdr", save(tmp / f"{tag}-s.npy", sdr), "--raw", save(tmp / f"{tag}-r.npy", y)]
    args += ["--clip", save(tmp / f"{tag}-c.npy", clip), "--out", str(tmp / f"{tag}-g.npy"), "--delta-out", str(tmp / f"{tag}-d.npy")]
    if params is not None:
        (tmp / f"{tag}-p.json").write_text(json.dumps(params))
        args += ["--params", str(tmp / f"{tag}-p.json")]
    payload, err = call(args)
    try:
        result = g.compute_gain(sdr, y, clip, p)
    except g.HdrUnsupported as exc:
        same_error(exc, err)
        return exc.reason
    assert err is None, f"rust raised {err.kind}: {err.message}"
    assert payload == gain_expect(result)
    same_exact(codes_of(result), np.load(tmp / f"{tag}-g.npy"), f"{tag} gain codes")
    same_f32(result.delta, np.load(tmp / f"{tag}-d.npy"), f"{tag} delta")
    return "ok"


POW_CODES = (71, 180)


def srgb_to_linear_rounded(rgb: np.ndarray) -> np.ndarray:
    """``srgb_to_linear`` with the float64 power rounded once to float32 (the Rust port).

    numpy's float32 ``**`` calls Apple's ``powf``, which is not correctly rounded at codes
    71 and 180 (POW_CODES); everywhere else this equals ``g.srgb_to_linear``.
    """
    v = rgb.astype(np.float32) / np.float32(255.0)
    base = (v + np.float32(0.055)) / np.float32(1.055)
    powed = (base.astype(np.float64) ** np.float64(np.float32(2.4))).astype(np.float32)
    return np.where(v <= np.float32(0.04045), v / np.float32(12.92), powed).astype(np.float32)


def check_sdr(python: np.ndarray, rust: np.ndarray, rgb: np.ndarray, factor: int, what: str) -> int:
    """Rust ``sdr_luma_grid`` must equal numpy's with only the POW_CODES powers rounded once.

    Returns how many grid cells differ from numpy itself (reported, not gated).
    """
    same_f32(g.block_mean(srgb_to_linear_rounded(rgb) @ g.LUMA, factor), rust, what)
    return int((python.view(np.uint32) != rust.view(np.uint32)).sum())


def flow(tmp: Path, jpeg: bytes, dng: bytes | Path, tag: str, *, gain: bool = True) -> Flow:
    """``hdr_from_camera_jpeg``'s numeric steps. Each Rust stage gets the same input as the
    Python stage and must match exactly; the chain run purely from Rust's own ``sdr_luma_grid``
    is recorded in ``stages`` (``e2e:...``)."""
    from gxr_studio.raw import open_dng

    dng_path = dng if isinstance(dng, Path) else tmp / f"{tag}.dng"
    if not isinstance(dng, Path):
        dng_path.write_bytes(dng)
    raw = g.raw_luma(open_dng(dng_path))
    ok(["hdr-raw-luma", str(dng_path), "--out", str(tmp / f"{tag}-y.npy"), "--clip-out", str(tmp / f"{tag}-clip.npy")])
    ry, rclip = np.load(tmp / f"{tag}-y.npy"), np.load(tmp / f"{tag}-clip.npy")
    same_f32(raw.y, ry, f"{tag} raw_luma.y")
    same_exact(raw.clip, rclip, f"{tag} raw_luma.clip")
    rgb = g.decode_rgb(jpeg)
    orientation = g.jpeg_orientation(jpeg)
    sdr = g.sdr_luma_grid(rgb, 4)
    ok(["hdr-luma", "--rgb", save(tmp / f"{tag}-rgb.npy", rgb), "--factor", "4", "--out", str(tmp / f"{tag}-sdr.npy")])
    rsdr = np.load(tmp / f"{tag}-sdr.npy")
    sdr_cells = check_sdr(sdr, rsdr, rgb, 4, f"{tag} sdr_luma_grid")
    stored_y, stored_clip = g.to_storage(raw.y, orientation), g.to_storage(raw.clip, orientation)
    al = g.align(sdr, stored_y)
    raw_in = save(tmp / f"{tag}-raw-in.npy", g.to_storage(ry, orientation))
    rust = ok(["hdr-align", "--sdr", save(tmp / f"{tag}-sdr-in.npy", sdr), "--raw", raw_in])
    expect = {"alignment": alignment_fields(al), "check": check_of(al)}
    assert rust == expect, f"{tag}: alignment python={expect} rust={rust}"
    stages = ["raw_luma", f"sdr_luma_grid:{sdr_cells}", "align"]
    if sdr_cells:
        e2e = ok(["hdr-align", "--sdr", str(tmp / f"{tag}-sdr.npy"), "--raw", raw_in])
        stages.append("e2e-align:" + ("same" if e2e == expect else json.dumps(e2e["alignment"])))
    if gain and expect["check"] is None:
        y, clip = g.register(g.RawLuma(stored_y, stored_clip), sdr.shape, al, sdr.shape)
        (tmp / f"{tag}-al.json").write_text(json.dumps({"scale": al.scale, "dy": al.dy, "dx": al.dx}))
        h, w = sdr.shape
        ok(
            ["hdr-register", "--raw", raw_in, "--clip", save(tmp / f"{tag}-sc.npy", g.to_storage(rclip, orientation))]
            + ["--align", str(tmp / f"{tag}-al.json"), "--shape", str(h), str(w), "--grid", str(h), str(w)]
            + ["--out", str(tmp / f"{tag}-reg.npy"), "--clip-out", str(tmp / f"{tag}-regc.npy")]
        )
        same_f32(y, np.load(tmp / f"{tag}-reg.npy"), f"{tag} register.y")
        same_exact(clip, np.load(tmp / f"{tag}-regc.npy"), f"{tag} register.clip")
        stages += ["register", "gain:" + compare_gain(tmp, sdr, y, clip, None, tag)]
        if sdr_cells:
            stages.append("e2e-gain-codes:" + e2e_codes(tmp, rsdr, y, clip, codes_ref(sdr, y, clip), tag))
    return Flow(stages, expect["alignment"], expect["check"])


def codes_ref(sdr: np.ndarray, y: np.ndarray, clip: np.ndarray) -> np.ndarray | None:
    try:
        return codes_of(g.compute_gain(sdr, y, clip, g.Params()))
    except g.HdrUnsupported:
        return None


def e2e_codes(tmp: Path, rsdr: np.ndarray, y: np.ndarray, clip: np.ndarray, ref: np.ndarray | None, tag: str) -> str:
    """How many uint8 gain codes change when Rust's own ``sdr_luma_grid`` feeds ``compute_gain``."""
    args = ["hdr-gain", "--sdr", str(tmp / f"{tag}-sdr.npy"), "--raw", str(tmp / f"{tag}-r.npy"), "--clip", str(tmp / f"{tag}-c.npy")]
    payload, err = call([*args, "--out", str(tmp / f"{tag}-e2e.npy")])
    if ref is None or err is not None:
        return f"{'none' if ref is None else 'ok'}/{'none' if err is not None else 'ok'}"
    return str(int((np.load(tmp / f"{tag}-e2e.npy") != ref).sum()))


def real_pairs(limit: int = 6) -> list[tuple[Path, Path]]:
    """RAW+JPG pairs (same stem) under ``captures/``, no backup paths, spread in sorted order."""
    if not CAPTURES.is_dir():
        return []
    dngs = sorted(
        (p for p in CAPTURES.rglob("*") if p.is_file() and p.suffix.upper() == ".DNG" and "backup" not in p.as_posix().lower()),
        key=lambda p: p.as_posix(),
    )
    pairs = []
    for d in dngs:
        for ext in (".JPG", ".jpg"):
            j = d.with_suffix(ext)
            if j.is_file():
                pairs.append((j, d))
                break
    if len(pairs) <= limit:
        return pairs
    return [pairs[(i * len(pairs)) // limit] for i in range(limit)]


def tag_json(tags: dict[int, Any]) -> str:
    """The tag dict ``_capture`` reads, as the JSON ``hdr-capture`` takes."""

    def conv(v: Any) -> Any:
        if isinstance(v, (tuple, list)):
            return [conv(x) for x in v]
        if isinstance(v, (str, bool, int)) or v is None:
            return v
        if isinstance(v, bytes):  # `_text` decodes bytes this way; float() of either fails alike
            return v.decode("ascii", errors="replace")
        return float(v)

    return json.dumps({str(k): conv(v) for k, v in tags.items()})


def segments_of(data: bytes) -> dict[str, Any]:
    from gxr_studio.hdr.container import jpeg_segments

    segs, sos, eoi = jpeg_segments(data)
    return {"segments": [[s.marker, s.start, s.end] for s in segs], "sos": sos, "eoi": eoi}
