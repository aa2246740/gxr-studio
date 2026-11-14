"""Stage-by-stage bit comparison of the Rust HDR FFT with numpy 2.4.3 (unit hdr-fft).

Run from the repo root with the project venv, isolated::

    uv run --frozen python -I tests/support/hdr_fft_probe.py --out DIR [--count N] [--stage ...]

Stages (default all): ``twiddles`` (raw cos/sin tables and twiddles against the correctly
rounded value and the host), ``sweep`` (1-D ``fft``/``ifft`` on every length 1-128, the
Bluestein switch lengths and the real grid lengths, ``--count`` random inputs each, both the
vectorised and the scalar numpy paths), ``two_d`` (``fft2``/``ifft2``), ``planes`` (the whole
``_phase_shift`` plane on Sol's input, its positive variant and random shapes) and ``real``
(the planes ``align`` computes on the real A12 pairs, all five scales). Every comparison is
on uint32 bits; NaN equals NaN at the same place. Each stage runs twice: the port as shipped
(correctly rounded twiddles) and ``--trig host`` (the host's cos/sin, as numpy uses them).
Writes ``DIR/probe.json``. Real samples are read from ``GXR_STUDIO_TEST_PARENT``; nothing
is copied.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from support import hdr_fft_ref as fr  # noqa: E402

SWITCH = [47, 48, 49, 50, 51, 52, 53, 89, 97, 101, 127, 131, 251, 257, 509, 521, 1021, 1031]
SOL = Path("/Volumes/Workspace/Scratch/gxr-studio-hillclimb/round-6/sol-review-mobile-hdr-r1/evidence/fft-mismatch-0")
MODES = (None, "host")


def inputs(rng: np.random.Generator, rows: int, n: int, style: int) -> np.ndarray:
    """complex64 ``(rows, n)``: 0 normal, 1 wide dynamic range, 2 real only, 3 small integers."""
    if style == 0:
        x = rng.standard_normal((rows, n)) + 1j * rng.standard_normal((rows, n))
    elif style == 1:
        mag = 10.0 ** rng.uniform(-30, 30, (rows, n, 2))
        x = rng.standard_normal((rows, n)) * mag[..., 0] + 1j * rng.standard_normal((rows, n)) * mag[..., 1]
    elif style == 2:
        x = rng.standard_normal((rows, n)) + 0j
    else:
        x = rng.integers(-8, 9, (rows, n)) + 1j * rng.integers(-8, 9, (rows, n))
    return x.astype(np.complex64)


def real_lengths() -> tuple[list[int], list[tuple[Path, Path]]]:
    from support import hdr_parity as hp

    pairs = hp.real_pairs()
    sizes: set[int] = set()
    from PIL import Image

    for jpg, _dng in pairs:
        with Image.open(jpg) as im:
            w, h = im.size
        sizes.update({-(-h // 4), -(-w // 4)})
    return sorted(sizes), pairs


def stage_twiddles(tmp: Path, lengths: list[int]) -> dict[str, Any]:
    tables = sorted({t for n in lengths for t in fr.twiddle_tables(n)})
    host_cases: list[dict[str, Any]] = []
    bad: list[str] = []
    for n in tables:
        rust_cr, v1 = fr.rust_tables(tmp, n, "tables")
        rust_host, _ = fr.rust_tables(tmp, n, "tables", "host")
        if fr.bit_diff(fr.table_values(n, "cr"), rust_cr):
            bad.append(f"table {n}: rust != correctly rounded")
        if fr.bit_diff(fr.table_values(n, "host"), rust_host):
            bad.append(f"table {n}: rust --trig host != math.cos/sin")
        for kind, width in (("float", np.float32), ("double", np.float64)):
            for trig, raw in ((None, rust_cr), ("host", rust_host)):
                got, _ = fr.rust_tables(tmp, n, kind, trig)
                if fr.bit_diff(fr.twiddles_from(raw, n, v1, width), got):
                    bad.append(f"table {n} {kind} {trig}: operator[] differs")
        host_cases += fr.host_vs_cr(n)
    return {"tables": len(tables), "host_vs_cr": host_cases, "bad": bad}


def stage_sweep(tmp: Path, lengths: list[int], count: int, seed: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for n in lengths:
        rng = np.random.default_rng(seed + n)
        per = max(1, count // 4)
        for inverse in (False, True):
            key = f"{n}-{'inv' if inverse else 'fwd'}"
            row = {mode or "cr": 0 for mode in MODES}
            total = 0
            for style in range(4):
                x = inputs(rng, per, n, style)
                small = [x[:1], x[1:3], x[3:6]]  # numpy's scalar loop for 1..3 rows
                for batch in [x, *small]:
                    if not batch.size:
                        continue
                    ref = fr.numpy_fft(batch, inverse=inverse)
                    total += batch.size
                    for mode in MODES:
                        got = fr.rust_fft(tmp, batch, inverse=inverse, trig=mode)
                        d = fr.bit_diff(ref, got)
                        row[mode or "cr"] += d
                        if d and f"{key}-first-{mode}" not in out:
                            out[f"{key}-first-{mode}"] = {"style": style, "rows": len(batch), "where": fr.first_diff(ref, got)}
            out[key] = {**row, "inputs": total}
    return out


def stage_two_d(tmp: Path, seed: int, shapes: int) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    picks = [(int(rng.integers(1, 65)), int(rng.integers(1, 65))) for _ in range(shapes)]
    picks += [(89, 64), (101, 7), (3, 1031), (712, 16), (16, 1072)]
    diffs = {mode or "cr": 0 for mode in MODES}
    first: dict[str, Any] = {}
    for h, w in picks:
        for style in range(4):
            x = inputs(rng, h, w, style)
            for inverse in (False, True):
                ref = fr.numpy_fft(x, inverse=inverse, two_d=True)
                for mode in MODES:
                    d = fr.bit_diff(ref, fr.rust_fft(tmp, x, inverse=inverse, two_d=True, trig=mode))
                    diffs[mode or "cr"] += d
                    if d and (mode or "cr") not in first:
                        first[mode or "cr"] = {"shape": [h, w], "style": style, "inverse": inverse}
    return {"shapes": len(picks), "diffs": diffs, "first": first}


def plane_case(tmp: Path, a: np.ndarray, b: np.ndarray) -> dict[str, Any]:
    from gxr_studio.hdr import gain as g

    ref = fr.phase_plane(a, b)
    peak = g._phase_shift(a, b)
    row: dict[str, Any] = {"shape": list(a.shape), "python_peak": list(peak)}
    for mode in MODES:
        got, shift = fr.rust_plane(tmp, a, b, mode)
        row[mode or "cr"] = {"diff": fr.bit_diff(ref, got), "peak": list(shift)}
    return row


def stage_planes(tmp: Path, seed: int, count: int) -> dict[str, Any]:
    rows = []
    if SOL.is_dir():
        a, b = np.load(SOL / "a.npy"), np.load(SOL / "b.npy")
        rows.append({"case": "sol-3x5", **plane_case(tmp, a, b)})
        pos = (np.float32(0.45) + np.float32(0.04) * a, np.float32(0.45) + np.float32(0.04) * b)
        rows.append({"case": "sol-3x5-positive", **plane_case(tmp, *pos)})
    rng = np.random.default_rng(seed)
    for i in range(count):
        h, w = int(rng.integers(1, 97)), int(rng.integers(1, 97))
        a = rng.standard_normal((h, w)).astype(np.float32)
        shift = (int(rng.integers(-3, 4)), int(rng.integers(-3, 4)))
        b = np.roll(a, shift, (0, 1)) + np.float32(0.3) * rng.standard_normal((h, w)).astype(np.float32)
        rows.append({"case": f"random-{i}", **plane_case(tmp, a, b.astype(np.float32))})
    return summarise(rows)


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    bad = {m: [r for r in rows if r[m]["diff"] or r[m]["peak"] != r["python_peak"]] for m in ("cr", "host")}
    return {"cases": len(rows), "cr_bad": bad["cr"], "host_bad": bad["host"], "first_rows": rows[:2]}


def stage_real(tmp: Path, pairs: list[tuple[Path, Path]]) -> dict[str, Any]:
    from gxr_studio.hdr import gain as g
    from gxr_studio.raw import open_dng
    from gxr_studio.raw.dng import RawDecodeError

    rows = []
    for jpg, dng in pairs:
        try:
            raw = g.raw_luma(open_dng(dng))
        except RawDecodeError:
            rows.append({"case": dng.name, "skipped": "raw_decode"})
            continue
        jpeg = jpg.read_bytes()
        sdr = g.sdr_luma_grid(g.decode_rgb(jpeg), 4)
        stored = g.to_storage(raw.y, g.jpeg_orientation(jpeg))
        a = g._norm(g._hp(sdr))
        for s in g.SCALES:
            b = g._norm(g._hp(g.warp(stored, a.shape, s, 0, 0)))
            rows.append({"case": f"{dng.name}@{s}", **plane_case(tmp, a, b)})
    planes = [r for r in rows if "skipped" not in r]
    return {**summarise(planes), "skipped": [r for r in rows if "skipped" in r]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--count", type=int, default=10000)
    ap.add_argument("--planes", type=int, default=200)
    ap.add_argument("--seed", type=int, default=20261114)
    ap.add_argument("--stage", action="append", choices=["twiddles", "sweep", "two_d", "planes", "real"])
    ap.add_argument("--lengths", type=str, default="")
    args = ap.parse_args()
    stages = args.stage or ["twiddles", "sweep", "two_d", "planes", "real"]
    grid, pairs = real_lengths()
    lengths = [int(v) for v in args.lengths.split(",")] if args.lengths else sorted(set(range(1, 129)) | set(SWITCH) | set(grid))
    report: dict[str, Any] = {"real_grid_lengths": grid, "lengths": lengths}
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        for st in stages:
            t0 = time.monotonic()
            if st == "twiddles":
                report[st] = stage_twiddles(tmp, lengths)
            elif st == "sweep":
                report[st] = stage_sweep(tmp, lengths, args.count, args.seed)
            elif st == "two_d":
                report[st] = stage_two_d(tmp, args.seed, 60)
            elif st == "planes":
                report[st] = stage_planes(tmp, args.seed, args.planes)
            else:
                report[st] = stage_real(tmp, pairs)
            report[f"{st}_seconds"] = round(time.monotonic() - t0, 1)
            print(st, json.dumps(report[st])[:600], flush=True)
            (args.out / "probe.json").write_text(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
