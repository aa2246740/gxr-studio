"""Run the colour commands and their Python references on the same files.

Used by ``tests/test_core_parity_color.py``. Every helper writes its inputs under the
test's ``tmp_path`` and returns both results; the test asserts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from gxr_studio.camdata import calib, store
from gxr_studio.camdata.unit_model import UnitModel, input_codes
from gxr_studio.engine import knobs, menu
from gxr_studio.engine.model import Model
from gxr_studio.gpu import tables
from gxr_studio.render import chain, tones
from gxr_studio.render.params import Adjust, Curves
from support import color_fixtures as cf
from support.color_oracle_gates import bake_gate, capture_bake_chroma  # noqa: F401
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
    "UnicodeDecodeError": "unicode",
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
    """(python (bytes, meta, ChromaBake, gain_f64_witness) or exception, rust (bytes, meta, summary) or error)."""
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
        bk, witness = capture_bake_chroma(pack.model(), entry, step, workers=1)
        data, py_meta = tables.bake_bytes(bk)
        py: Any = (data, py_meta, bk, witness)
    except Exception as exc:  # noqa: BLE001
        py = exc
    return py, rust


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


def context_case(tmp: Path, name: str, npz: bytes, entry: bytes) -> tuple[BaseException | None, CoreCliError | None]:
    """`_context` and `color-calib-check` on the same archive: (Python error, CLI error)."""
    path = tmp / f"{name}.npz"
    path.write_bytes(npz)
    entry_path = tmp / f"{name}-entry.bin"
    entry_path.write_bytes(entry)
    payload, err = cli(["color-calib-check", "--npz", path, "--entry", entry_path])
    py = python_error(lambda: calib._context(npz, calib.stock_words_of_entry(entry)))
    if py is None and err is None:
        got = calib._context(npz, calib.stock_words_of_entry(entry))
        assert payload == {"rows": len(got[2]), "d0": 24, "d1": 24}, name
    return py, err


def npz_same(tmp: Path, name: str, data: bytes, entry: bytes) -> str | None:
    """None when `_context` and `color-calib-check` agree on this archive (both accept, or the
    same kind and message)."""
    py, err = context_case(tmp, name, data, entry)
    if py is None and err is None:
        return None
    return same_error(py, err)


def pack_home(tmp: Path, stock04: bytes, unit: str = "jlaunch") -> Path:
    """A home with this unit's stock entry and a synthetic pack written by `import_calibration`."""
    home = tmp / "home"
    units = store.camera_data_dir(home) / "units" / unit
    units.mkdir(parents=True)
    (units / "stock04.entry").write_bytes(stock04)
    words = calib.stock_words_of_entry(stock04)
    sources = {}
    for iso, rows in ((200, 4), (1600, 10)):
        d0, d1, coef = cf.arrays(rows, seed=iso)
        ring = json.dumps(cf.ring(kernel="hard" if iso == 1600 else "linear")).encode()
        sources[iso] = ((f"src{iso}.npz", cf.source_npz(words, d0, d1, coef)), (f"ring{iso}.json", ring))
    calib.import_calibration(home, {unit: sources})
    return home


def pick(home: Path, iso: int, unit: str, stock: Path) -> tuple[Any, Any]:
    """`model_for` and `color-calib-check --dir` on the same pack: (Python result, CLI result)."""
    payload, err = cli(["color-calib-check", "--dir", calib.calib_dir(home), "--unit", unit, "--iso", iso, "--stock", stock])
    try:
        _model, info = calib.model_for(home, iso, unit=unit)
        manifest = json.loads((calib.calib_dir(home) / "manifest.json").read_text(encoding="utf-8"))
        pair = manifest["units"][unit]["models"][str(info["model_iso"])]
        py: Any = {**info, "ctx": pair["ctx"], "ring": pair["ring"], "rows": len(_model.input_coefficients)}
    except Exception as exc:  # noqa: BLE001
        py = exc
    return py, (err if err is not None else payload)


def bake_spec(tmp: Path, name: str, pack: Pack, entry: bytes) -> Path:
    """A `wasm_cases/color.mjs` bake case for `pack` and `entry`, written as JSON."""
    e = tmp / f"{name}-entry.bin"
    e.write_bytes(entry)
    spec = {"kind": "bake", "entry": str(e), "stock": str(pack.stock), "ctx": str(pack.ctx), "ring": str(pack.ring)}
    path = tmp / f"{name}.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path
