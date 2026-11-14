"""Run the colour commands and their Python references on the same files.

Used by ``tests/test_core_parity_color.py``. Every helper writes its inputs under the
test's ``tmp_path`` and returns both results; the test asserts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from gxr_studio.camdata import calib
from gxr_studio.camdata.unit_model import UnitModel, input_codes
from gxr_studio.engine import knobs, menu
from gxr_studio.engine.model import Model
from gxr_studio.gpu import tables
from gxr_studio.render import chain, tones
from gxr_studio.render.params import Adjust, Curves
from support import color_fixtures as cf
from support.core_cli import CoreCliError, error_of, run_json


class Pack:
    """One unit model on disk (stock entry, ctx npz, ring JSON) and the Python model."""

    def __init__(self, folder: Path, stock04: bytes, ctx: bytes, ring: bytes) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        self.stock = folder / "stock04.bin"
        self.ctx = folder / "ctx.npz"
        self.ring = folder / "ring.json"
        self.stock.write_bytes(stock04)
        self.ctx.write_bytes(ctx)
        self.ring.write_bytes(ring)
        self.stock04 = stock04
        self.folder = folder

    def model(self) -> UnitModel:
        """What ``model_for`` builds from these files."""
        import io

        with np.load(io.BytesIO(self.ctx.read_bytes()), allow_pickle=False) as z:
            d0, d1, coefficients = (z[k].copy() for k in ("d0", "d1", "input_coefficients"))
        return UnitModel(calib.stock_words_of_entry(self.stock04), d0, d1, json.loads(self.ring.read_bytes()), self.stock04, coefficients)

    def args(self) -> list[str]:
        return ["--stock", str(self.stock), "--ctx", str(self.ctx), "--ring", str(self.ring)]


def synthetic_pack(folder: Path, *, rows: int = 4, **ring_kw: Any) -> Pack:
    stock04 = cf.entry()
    d0, d1, coefficients = cf.arrays(rows)
    return Pack(folder, stock04, cf.ctx_npz(d0, d1, coefficients), json.dumps(cf.ring(**ring_kw)).encode())


def cli(args: list[str]) -> tuple[Any, CoreCliError | None]:
    code, payload = run_json([str(a) for a in args])
    return payload, error_of(code, payload)


def python_error(fn: Any) -> BaseException | None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001  the test compares kinds and messages
        return exc
    return None


KINDS = {
    "ValueError": "value",
    "CalibError": "calib",
    "StockMismatch": "stock_mismatch",
    "KeyError": "key",
    "BadZipFile": "zip",
    "NotImplementedError": "not_implemented",
    "RuntimeError": "runtime",
    "EOFError": "eof",
}


def same_error(py: BaseException | None, rs: CoreCliError | None) -> str | None:
    """None when both failed the same way; otherwise a message for the assertion."""
    if py is None or rs is None:
        return f"one side failed: python {py!r} rust {rs!r}"
    kind = KINDS.get(type(py).__name__)
    if kind != rs.kind or str(py) != rs.message:
        return f"python {type(py).__name__}({py}) rust {rs.kind}({rs.message})"
    return None


def predict(tmp: Path, pack: Pack, entry: bytes, codes: np.ndarray, *, flt: bool = False, half: int = 0, base: bool = False) -> tuple[Any, Any]:
    """(python result or exception, rust array or CoreCliError)."""
    e, c, out = tmp / "entry.bin", tmp / "codes.npy", tmp / "pred.npy"
    e.write_bytes(entry)
    np.save(c, codes)
    flags = (["--float"] if flt else []) + (["--half", str(half)] if half else []) + (["--base"] if base else [])
    _payload, err = cli(["color-predict", "--entry", e, *pack.args(), "--codes", c, "--out", out, *flags])
    rust: Any = err if err is not None else np.load(out)
    try:
        model = pack.model()
        fn = Model.predict if base else UnitModel.predict
        py: Any = fn(model, entry, codes, quantize=not flt, half=half)
    except Exception as exc:  # noqa: BLE001
        py = exc
    return py, rust


def nonfinite_gap(python: np.ndarray, rust: np.ndarray) -> tuple[str | None, np.ndarray]:
    """The non-finite rule of every float gate (UNIT-COMMON amendment, 2026-11-14): NaN must
    meet NaN at the same position, a NaN on one side fails, and infinities compare exactly.
    Returns (failure or None, the mask of positions where both values are finite)."""
    a, b = np.asarray(python).reshape(-1), np.asarray(rust).reshape(-1)
    nan_a, nan_b = np.isnan(a), np.isnan(b)
    for mask, what in ((nan_a != nan_b, "NaN on one side"), ((np.isinf(a) | np.isinf(b)) & ~nan_a & ~nan_b & (a != b), "infinity differs")):
        hit = np.flatnonzero(mask)
        if len(hit):
            i = int(hit[0])
            return f"{what}: {len(hit)} positions; first {i}: python {a[i]!r} rust {b[i]!r}", np.zeros(a.shape, bool)
    return None, np.isfinite(a) & np.isfinite(b)


def finite_gaps(a: np.ndarray, b: np.ndarray, finite: np.ndarray) -> np.ndarray:
    """``|a - b|`` where both are finite, 0 elsewhere (the non-finite rule decides those)."""
    out = np.zeros(a.shape)
    out[finite] = np.abs(a[finite] - b[finite])
    return out


def float_gap(python: np.ndarray, rust: np.ndarray) -> str | None:
    """The default float64 gate: ``|a - b| <= 1e-12 * max(1, |a|)`` on finite values, after
    the non-finite rule. None when it holds."""
    assert python.dtype == np.float64 and rust.dtype == np.float64, (python.dtype, rust.dtype)
    assert python.shape == rust.shape, (python.shape, rust.shape)
    failure, finite = nonfinite_gap(python, rust)
    if failure is not None:
        return failure
    a, b = python.reshape(-1), rust.reshape(-1)
    bad = np.flatnonzero(finite & (finite_gaps(a, b, finite) > 1e-12 * np.maximum(1.0, np.abs(a))))
    if not len(bad):
        return None
    i = int(bad[0])
    return f"{len(bad)} over 1e-12; first {i}: python {python.reshape(-1)[i]!r} rust {rust.reshape(-1)[i]!r}"


def _hue_spread(model: UnitModel, entry: bytes, x: np.ndarray, ch: int, *, half: int, base: bool) -> float:
    """S: Python's own output change for one pixel when its hue input moves one float64 ulp
    (the larger of + and -). The steps are `UnitModel.predict` / `Model.predict` with the hue
    taken out so it can be moved."""
    if entry[0] == 3:
        return 0.0
    pre = model.pre(entry, x if base else input_codes(x, model.input_coefficients))
    at = 216 + half * 28
    row = model.mm.MatrixRow.from_bytes(entry[at : at + 28])
    z = chain._sector_z(model, entry)
    y, ci = row.apply(pre)
    cl = ci if base else model.mm.MatrixRow.from_bytes(model.factory[at : at + 28]).apply(pre)[1]
    table = model.engine.table
    hue, mag = table.sm.hue_deg(cl), abs(cl)

    def out(h: np.ndarray) -> float:
        co = (ci * table.S_of(h, mag)) * table.z(z, h, mag)
        return float(np.clip(model.mm.rgb_from_ycc(y, co), 0, 255)[0, ch])

    here = out(hue)
    return max(abs(out(np.nextafter(hue, np.inf)) - here), abs(out(np.nextafter(hue, -np.inf)) - here))


def predict_gap(
    pack: Pack, entry: bytes, codes: np.ndarray, py: np.ndarray, rust: np.ndarray, *, half: int, base: bool
) -> tuple[str | None, list[str]]:
    """The float64 predict gate (UNIT-color manager amendment 2, 2026-11-13):
    ``|rs - py| <= max(1e-12 * max(1, |py|), 2 * |py_batched - py_single|, 2 * S)``.

    `py` is the batched result; S is `_hue_spread`. Both are computed for every element the
    first term does not already decide. Returns (failure or None, the cases the second or
    third term decided, each with its term).
    """
    assert py.dtype == np.float64 and rust.dtype == np.float64, (py.dtype, rust.dtype)
    assert py.shape == rust.shape, (py.shape, rust.shape)
    failure, finite = nonfinite_gap(py, rust)
    if failure is not None:
        return failure, []
    flat_py, flat_rs = py.reshape(-1), rust.reshape(-1)
    over = np.flatnonzero(finite & (finite_gaps(flat_py, flat_rs, finite) > 1e-12 * np.maximum(1.0, np.abs(flat_py))))
    decided: list[str] = []
    if not len(over):
        return None, decided
    model = pack.model()
    fn = Model.predict if base else UnitModel.predict
    for i in over:
        i = int(i)
        px, ch = divmod(i, 3)
        x = codes[px : px + 1]
        single = float(fn(model, entry, x, quantize=False, half=half)[0, ch])
        spread = _hue_spread(model, entry, x, ch, half=half, base=base)
        a, r = float(flat_py[i]), float(flat_rs[i])
        terms = {"1e-12": 1e-12 * max(1.0, abs(a)), "batched-single": 2.0 * abs(a - single), "hue ulp": 2.0 * spread}
        term, bound = max(terms.items(), key=lambda kv: kv[1])
        text = f"flat {i} (pixel {px}, channel {ch}): python batched {a!r} single {single!r} rust {r!r}; S {spread:.3e}"
        if abs(r - a) > bound:
            return text + f"; |rs - py| {abs(r - a):.3e} > bound {bound:.3e}", decided
        decided.append(text + f"; |rs - py| {abs(r - a):.3e} <= {bound:.3e} ({term})")
    return None, decided


def bake(tmp: Path, pack: Pack, entry: bytes, *, params: dict[str, Any] | None = None, menu: Path | None = None) -> tuple[Any, Any]:
    """(python (bytes, meta, ChromaBake) or exception, rust (bytes, meta, summary) or error)."""
    e, out, meta = tmp / "bake-entry.bin", tmp / "bake.bin", tmp / "bake.meta.json"
    e.write_bytes(entry)
    args: list[Any] = ["color-bake", "--entry", e, *pack.args()]
    step = (params or {}).get("step", 0.375)
    if params is not None:
        p = tmp / "bake-params.json"
        p.write_text(json.dumps(params), encoding="utf-8")
        args += ["--params", p]
    if menu is not None:
        args += ["--menu", menu]
    payload, err = cli([*args, "--out", out, "--meta", meta])
    rust: Any = err if err is not None else (out.read_bytes(), json.loads(meta.read_text(encoding="utf-8")), payload)
    try:
        knob = (params or {}).get("knobs")
        if knob is not None:
            entry = knobs.apply(pack.stock04, entry, knob_request(knob))[0]
        bk = chain.bake_chroma(pack.model(), entry, step, workers=1)
        data, py_meta = tables.bake_bytes(bk)
        py: Any = (data, py_meta, bk)
    except Exception as exc:  # noqa: BLE001
        py = exc
    return py, rust


def _midpoint_ulps(v: float) -> float:
    """How many float64 ulps `v` sits from the nearest float32 rounding midpoint."""
    f = np.float32(v)
    lo = f if float(f) <= v else np.nextafter(f, np.float32(-np.inf))
    hi = np.nextafter(lo, np.float32(np.inf))
    mid = (float(lo) + float(hi)) / 2.0
    return abs(v - mid) / float(np.spacing(abs(v)))


def _gains(model: UnitModel, entry: bytes, bk: Any, nx: int, cell: int) -> tuple[complex, complex, complex]:
    """Python's float64 gain at `cell`: as the bake computed it (the same 4096-cell chunk of
    `todo`), alone (P=1), and the value the bake stored, re-read as float32."""
    from scipy.spatial import ConvexHull

    ny = bk.gain.shape[0]
    cb = bk.origin[0] + bk.step * np.arange(nx)
    cr = bk.origin[1] + bk.step * np.arange(ny)
    c = (cb[None, :] + 1j * cr[:, None]).reshape(-1)
    probe = np.where(np.abs(c) < 1e-9, 1e-3 + 0j, c)
    lookup = model.mm.MatrixRow.from_bytes(model.factory[216:244])
    lm = lookup.effective(100.0)
    lo = lookup.offsets - np.array([0.0, 128.0, 128.0])
    corners = np.array([[r, g, b] for r in (0, 255) for g in (0, 255) for b in (0, 255)], np.float64) @ lm.T + lo
    hull = ConvexHull(corners[:, 1:])
    near = np.all(hull.equations[:, :2] @ np.column_stack([c.real, c.imag]).T + hull.equations[:, 2:3] <= 2.0 * bk.step * np.sqrt(2.0), axis=0)
    todo = np.flatnonzero(near)
    pos = int(np.searchsorted(todo, cell))
    assert pos < len(todo) and todo[pos] == cell, f"cell {cell} was not computed by the bake"
    start = (pos // 4096) * 4096
    sel = todo[start : start + 4096]
    z = chain._sector_z(model, entry)
    table = model.engine.table
    batched = complex((table.forward(probe[sel], z)[0] / probe[sel])[pos - start])
    single = complex(table.forward(probe[[cell]], z)[0][0] / probe[cell])
    stored = complex(float(bk.gain[cell // nx, cell % nx, 0]), float(bk.gain[cell // nx, cell % nx, 1]))
    return batched, single, stored


def bake_gap(
    py: tuple[bytes, dict[str, Any], Any], rust: tuple[bytes, dict[str, Any], Any], model: UnitModel, entry: bytes
) -> tuple[str | None, list[str]]:
    """The bake gate (UNIT-color manager amendments 1 and 2, 2026-11-13).

    Every float32 byte is exact, except a gain component where either
    (a) Python's own float64 value, batched or single-cell, lies within 4 float64 ulp of a
        float32 rounding midpoint and the Rust float32 is the neighbouring float32, or
    (b) ``|rs - py| <= 8 * ulp_f64(|g_py|)``, with ``g_py`` Python's float64 gain at the cell as
        the bake computed it (batched) and ``py`` its component.
    Returns (failure or None, the allowed cells with their rule).
    """
    data, meta, bk = py
    r_data, r_meta, summary = rust
    if summary["bytes"] != len(r_data):
        return "summary byte count", []
    for key in ("nx", "ny", "origin", "step"):
        if meta[key] != r_meta[key]:
            return f"meta {key}: python {meta[key]!r} rust {r_meta[key]!r}", []
    coef = np.asarray(r_meta["input_coefficients"], np.float64)
    if not np.array_equal(coef, np.asarray(bk.input_coefficients, np.float64)):
        return "input_coefficients differ", []
    if data == r_data:
        return None, []
    a, b = np.frombuffer(data, "<f4"), np.frombuffer(r_data, "<f4")
    if a.shape != b.shape:
        return f"table sizes differ: {a.size} vs {b.size}", []
    head = tables.PRE_FLOATS + 9 + 3 + 30 + 12
    allowed: list[str] = []
    for i in np.flatnonzero(a.view(np.uint32) != b.view(np.uint32)):
        i = int(i)
        text = f"float {i}: python {a[i]!r} rust {b[i]!r}"
        if i < head:
            return text + " (outside the gain grid)", allowed
        cell, part = divmod(i - head, 2)
        batched, single, stored = _gains(model, entry, bk, meta["nx"], cell)
        pick = (lambda v: v.imag) if part else (lambda v: v.real)
        text += f"; cell {cell} {'imag' if part else 'real'}: python f64 batched {pick(batched)!r} single {pick(single)!r}"
        if np.float32(pick(batched)) != np.float32(pick(stored)):
            return text + "; the recomputed batch does not reproduce the stored value", allowed
        neighbour = b[i] in (np.nextafter(a[i], np.float32(np.inf)), np.nextafter(a[i], np.float32(-np.inf)))
        ulps = min(_midpoint_ulps(pick(batched)), _midpoint_ulps(pick(single)))
        if neighbour and ulps <= 4.0:
            allowed.append(text + f"; rule (a): midpoint distance {ulps:.2f} f64 ulp")
            continue
        gap = abs(float(b[i]) - pick(batched))
        scale = 8.0 * float(np.spacing(abs(batched)))
        if gap <= scale:
            allowed.append(text + f"; rule (b): |rs - py| {gap:.3e} <= 8 ulp_f64(|g| = {abs(batched)!r}) = {scale:.3e}")
            continue
        return (
            text + f"; midpoint distance {ulps:.2f} f64 ulp (neighbour={neighbour}); |rs - py| {gap:.3e} > 8 ulp_f64(|g|) {scale:.3e}",
            allowed,
        )
    return None, allowed


def knob_request(params: dict[str, Any]) -> knobs.Request:
    """The request ``color-knobs-apply`` builds from the same JSON."""
    if "levels" in params:
        return menu.request(menu.load(), params["unit"], params["slot"], params["levels"])
    return knobs.Request(
        params.get("amount", 100), tuple(params.get("doses", [0] * 10)), params.get("wb_method", 0), params.get("tone_method", 0)
    )


def tone_args(adjust: dict[str, Any] | None, curves: dict[str, Any] | None) -> tuple[Adjust, Curves | None, dict[str, Any]]:
    a = Adjust(**(adjust or {}))
    c = None if curves is None else Curves(**{k: (None if v is None else tuple(tuple(p) for p in v)) for k, v in curves.items()})
    params: dict[str, Any] = {}
    if adjust:
        params["adjust"] = adjust
    if curves is not None:
        params["curves"] = curves
    return a, c, params


def apply(
    tmp: Path,
    bake_files: tuple[Path, Path],
    bk: Any,
    codes: np.ndarray,
    adjust: dict[str, Any] | None = None,
    curves: dict[str, Any] | None = None,
) -> tuple[np.ndarray, Any]:
    a, c, params = tone_args(adjust, curves)
    codes_path, p, out = tmp / "apply-codes.npy", tmp / "apply-params.json", tmp / "apply.npy"
    np.save(codes_path, codes)
    p.write_text(json.dumps(params), encoding="utf-8")
    _payload, err = cli(["color-apply", "--bake", bake_files[0], "--meta", bake_files[1], "--codes", codes_path, "--params", p, "--out", out])
    rust: Any = err if err is not None else np.load(out)
    python = chain.chroma_u8(codes, bk, tones.channel_gains(a), tones.display_table(a, c), workers=1)
    return python, rust


def write_bake(tmp: Path, pack: Pack, entry: bytes, name: str = "b") -> tuple[Path, Path]:
    """Bake with the CLI and keep the files for ``apply``."""
    e, out, meta = tmp / f"{name}-entry.bin", tmp / f"{name}.bin", tmp / f"{name}.meta.json"
    e.write_bytes(entry)
    _payload, err = cli(["color-bake", "--entry", e, *pack.args(), "--out", out, "--meta", meta])
    assert err is None, err
    return out, meta
