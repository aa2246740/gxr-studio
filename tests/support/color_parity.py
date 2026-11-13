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
from gxr_studio.camdata.unit_model import UnitModel
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


KINDS = {"ValueError": "value", "CalibError": "calib", "StockMismatch": "stock_mismatch", "KeyError": "key"}


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


def float_gap(python: np.ndarray, rust: np.ndarray) -> str | None:
    """The float64 gate: ``|a - b| <= 1e-12 * max(1, |a|)``. None when it holds."""
    assert python.dtype == np.float64 and rust.dtype == np.float64, (python.dtype, rust.dtype)
    assert python.shape == rust.shape, (python.shape, rust.shape)
    bound = 1e-12 * np.maximum(1.0, np.abs(python))
    bad = np.flatnonzero((np.abs(python - rust) > bound).reshape(-1))
    if not len(bad):
        return None
    i = int(bad[0])
    return f"{len(bad)} over 1e-12; first {i}: python {python.reshape(-1)[i]!r} rust {rust.reshape(-1)[i]!r}"


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


def bake_gap(py: tuple[bytes, dict[str, Any], Any], rust: tuple[bytes, dict[str, Any], Any], model: UnitModel, entry: bytes) -> str | None:
    """Bytes and meta of a bake. On a miss: the float index, both float32 values and, in the
    gain grid, Python's float64 gain before the store."""
    data, meta, bk = py
    r_data, r_meta, summary = rust
    if summary["bytes"] != len(r_data):
        return "summary byte count"
    for key in ("nx", "ny", "origin", "step"):
        if meta[key] != r_meta[key]:
            return f"meta {key}: python {meta[key]!r} rust {r_meta[key]!r}"
    coef = np.asarray(r_meta["input_coefficients"], np.float64)
    if not np.array_equal(coef, np.asarray(bk.input_coefficients, np.float64)):
        return "input_coefficients differ"
    if data == r_data:
        return None
    a, b = np.frombuffer(data, "<f4"), np.frombuffer(r_data, "<f4")
    if a.shape != b.shape:
        return f"table sizes differ: {a.size} vs {b.size}"
    bad = np.flatnonzero(a.view(np.uint32) != b.view(np.uint32))
    i = int(bad[0])
    text = f"{len(bad)} float32 differ; first {i}: python {a[i]!r} rust {b[i]!r}"
    head = tables.PRE_FLOATS + 9 + 3 + 30 + 12
    if i >= head:
        cell = (i - head) // 2
        cb = bk.origin[0] + bk.step * (cell % meta["nx"])
        cr = bk.origin[1] + bk.step * (cell // meta["nx"])
        c = complex(cb, cr)
        probe = complex(1e-3, 0) if abs(c) < 1e-9 else c
        z = chain._sector_z(model, entry)
        g = model.engine.table.forward(np.array([probe]), z)[0][0] / probe
        text += f"; cell {cell} at {c}: python float64 gain {g!r}"
    return text


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
