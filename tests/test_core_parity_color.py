"""Parity: the color-* commands against engine.knobs, engine.menu, camdata.calib,
the frozen engine's predict, render.chain's bake and apply, and render.tones.

Gates: knob bytes, stats, uint8 codes, error kinds and messages are exact; the bake
table is float32 bytes exact; float64 predict is within ``1e-12 * max(1, |a|)``.
A miss reports the first index and both values. ``GXR_REQUIRE_GPU_TEST`` does not
make the CLI required.
"""

from __future__ import annotations

import io
import json
import os
import struct
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest

import e0_support as e0
from gxr_studio.camdata import calib, datcodec, store
from gxr_studio.engine import knobs, menu
from gxr_studio.render import tones
from support import color_fixtures as cf
from support import color_npz_corpus as nc
from support import color_parity as cp
from support.core_cli import CoreCliError, error_of, require_cli, run_json, run_npy

MENU = Path(__file__).resolve().parents[1] / "src/gxr_studio/engine/data/final.json"


def _knots(buf: bytearray, at: int) -> None:
    for i in range(31):
        struct.pack_into(">H", buf, at + 2 * i, (i + 1) * 128)


def _curve(buf: bytearray, at: int, tilt: int) -> None:
    for i in range(33):
        buf[at + i] = min(255, i * 7 + tilt)


def _entry(flags: int = 0x17, kind: int = 4) -> bytes:
    buf = bytearray(552)
    buf[0] = kind
    buf[1] = flags
    for at in (20, 284, 456):
        _knots(buf, at)
    _curve(buf, 82, 0)
    _curve(buf, 346, 1)
    _curve(buf, 379, 2)
    _curve(buf, 412, 3)
    _curve(buf, 518, 4)
    for sector in range(24):
        a = 8 + (sector % 5)
        c = 3 + (sector % 3)
        buf[115 + 4 * sector : 115 + 4 * sector + 4] = bytes((a, c, c ^ 0x80, a))
    words: list[int] = []
    for _half in range(2):
        words.extend([400, 312, 312, 0, 0, 0, 0, 0, 0, 0, 128, 128, 0, 0])
    struct.pack_into(">28H", buf, 216, *words)
    return bytes(buf)


def _apply(tmp: Path, stock: bytes, base: bytes, params: dict, name: str) -> tuple[int, dict, bytes]:
    entry = tmp / f"{name}-base.bin"
    stock_path = tmp / f"{name}-stock.bin"
    params_path = tmp / f"{name}.json"
    out = tmp / f"{name}-out.bin"
    entry.write_bytes(base)
    stock_path.write_bytes(stock)
    params_path.write_text(json.dumps(params), encoding="utf-8")
    code, payload = run_json(
        [
            "color-knobs-apply",
            "--entry",
            str(entry),
            "--stock",
            str(stock_path),
            "--menu",
            str(MENU),
            "--params",
            str(params_path),
            "--out",
            str(out),
        ]
    )
    written = out.read_bytes() if out.is_file() else b""
    return code, payload, written


def _doses(**overrides: int) -> list[int]:
    doses = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    names = ("contrast", "highlight", "shadow", "fade", "saturation", "warmth", "tint", "split", "filter", "tone")
    for name, value in overrides.items():
        doses[names.index(name)] = value
    return doses


def _expect_ok(tmp: Path, stock: bytes, base: bytes, params: dict, name: str) -> None:
    request = knobs.Request(
        params.get("amount", 100),
        tuple(params.get("doses", [0] * 10)),
        params.get("wb_method", 0),
        params.get("tone_method", 0),
    )
    entry, stats = knobs.apply(stock, base, request)
    code, payload, written = _apply(tmp, stock, base, params, name)
    assert error_of(code, payload) is None
    assert written == entry
    assert payload["stats"] == stats
    assert payload["length"] == 552


def _expect_error(tmp: Path, stock: bytes, base: bytes, params: dict, name: str) -> None:
    request = knobs.Request(
        params.get("amount", 100),
        tuple(params.get("doses", [0] * 10)),
        params.get("wb_method", 0),
        params.get("tone_method", 0),
    )
    with pytest.raises(ValueError) as caught:
        knobs.apply(stock, base, request)
    code, payload, _written = _apply(tmp, stock, base, params, name)
    err = error_of(code, payload)
    assert err is not None
    assert err.kind == "value"
    assert err.message == str(caught.value)


def test_knobs_match_on_synthetic_entries(tmp_path: Path) -> None:
    require_cli()
    stock = _entry()
    base = _entry(flags=0x17)
    other = _entry(flags=0x01)
    _expect_ok(tmp_path, stock, base, {"amount": 100, "doses": _doses()}, "identity")
    _expect_ok(tmp_path, stock, other, {"amount": 0, "doses": _doses()}, "amount-zero")
    _expect_ok(
        tmp_path,
        stock,
        other,
        {"amount": 80, "doses": _doses(contrast=400, highlight=-300, shadow=100, fade=200, saturation=500)},
        "blend",
    )
    _expect_ok(
        tmp_path,
        stock,
        other,
        {
            "amount": 40,
            "doses": _doses(warmth=-100, tint=50, split=80, filter=-200, tone=300),
            "wb_method": 0,
            "tone_method": 0,
        },
        "matrix",
    )
    _expect_ok(
        tmp_path,
        stock,
        other,
        {"amount": 60, "doses": _doses(warmth=80, tint=-40, split=-20, tone=-100), "wb_method": 1, "tone_method": 1},
        "curves",
    )
    _expect_ok(tmp_path, stock, _entry(kind=3), {"amount": 70, "doses": _doses(saturation=1000, filter=400)}, "bw")
    _expect_error(tmp_path, stock, base, {"amount": 151, "doses": _doses()}, "amount")
    _expect_error(tmp_path, stock, base, {"amount": 100, "doses": _doses(fade=-1)}, "fade")
    _expect_error(tmp_path, stock, base, {"amount": 100, "doses": _doses(contrast=16385)}, "doses")
    _expect_error(tmp_path, stock, base, {"amount": 100, "doses": _doses(), "wb_method": 2}, "method")
    _expect_error(tmp_path, b"\x00" * 10, base, {"amount": 100, "doses": _doses()}, "short")


def test_menu_levels_match_final_json(tmp_path: Path) -> None:
    require_cli()
    data = menu.load()
    recipe = data["recipes"][0]
    items = data["items"][recipe["first_item"] : recipe["first_item"] + recipe["item_count"]]
    levels = [item["points"][0]["level"] for item in items]
    params = {"unit": 0, "slot": 1, "levels": levels}
    code, payload, written = _apply(tmp_path, _entry(), _entry(flags=0x01), params, "menu")
    req = menu.request(data, 0, 1, levels)
    entry, stats = knobs.apply(_entry(), _entry(flags=0x01), req)
    assert error_of(code, payload) is None
    assert written == entry
    assert payload["stats"] == stats
    bad = dict(params)
    bad["levels"] = levels[:-1]
    code, payload, _written = _apply(tmp_path, _entry(), _entry(), bad, "menu-count")
    with pytest.raises(ValueError) as caught:
        menu.request(data, 0, 1, levels[:-1])
    err = error_of(code, payload)
    assert err is not None and err.kind == "value" and err.message == str(caught.value)
    worse = dict(params)
    worse["unit"] = 3
    code, payload, _written = _apply(tmp_path, _entry(), _entry(), worse, "menu-unit")
    with pytest.raises(ValueError) as caught:
        menu.request(data, 3, 1, levels)
    err = error_of(code, payload)
    assert err is not None and err.message == str(caught.value)
    floated = dict(params)
    floated["levels"] = [1.0, *levels[1:]]
    code, payload, _written = _apply(tmp_path, _entry(), _entry(), floated, "menu-float")
    err = error_of(code, payload)
    assert err is not None and err.message == "integer UI level required"


def _case_params(case: dict) -> dict:
    if case["kind"] == "menu":
        return {"unit": case["unit"], "slot": case["slot"], "levels": case["levels"]}
    return {
        "amount": case["amount"],
        "doses": case["doses"],
        "wb_method": case["wb_method"],
        "tone_method": case["tone_method"],
    }


def _same_outcome(outcome: tuple, code: int, payload: dict, written: bytes) -> bool:
    if outcome[0] == "error":
        err = error_of(code, payload)
        return err is not None and err.kind == "value" and err.message == outcome[1]
    err = error_of(code, payload)
    return err is None and written == outcome[1] and payload["stats"] == outcome[2]


def test_golden_knob_list_matches_python(tmp_path: Path) -> None:
    """Every knob case from the g127 fixture, against Python and the golden digests."""
    require_cli()
    raw = e0.fixture_dat(127)
    if raw is None:
        pytest.skip("g127 fixture DAT is not on this machine")
    parsed = datcodec.parse(raw)
    data = menu.load()
    cases = e0.knob_cases(data)
    golden = json.loads((e0.GOLDEN / "knobs_cases.json").read_text(encoding="utf-8"))
    assert golden["cases"] == len(cases)
    mismatch: list[str] = []
    ok = 0
    errors = 0
    for case in cases:
        stock = parsed.entry_of(case["unit"], 4)
        base = parsed.entry_of(case["unit"], case["slot"])
        outcome = e0.run_case(knobs, menu, data, case, stock, base)
        code, payload, written = _apply(tmp_path, stock, base, _case_params(case), "golden")
        if not _same_outcome(outcome, code, payload, written):
            mismatch.append(case["id"])
            continue
        if e0.outcome_digest(outcome) != golden["digests"][case["id"]]:
            mismatch.append(case["id"] + ":golden")
            continue
        if outcome[0] == "error":
            errors += 1
        else:
            ok += 1
    assert mismatch == [], mismatch[:5]
    print(f"golden cases={len(cases)} ok={ok} errors={errors}")


# ---- calibration: `_context` and `model_for` ---------------------------------------------


def _context_case(tmp: Path, name: str, npz: bytes, entry: bytes) -> tuple[BaseException | None, CoreCliError | None]:
    path = tmp / f"{name}.npz"
    path.write_bytes(npz)
    entry_path = tmp / f"{name}-entry.bin"
    entry_path.write_bytes(entry)
    payload, err = cp.cli(["color-calib-check", "--npz", path, "--entry", entry_path])
    py = cp.python_error(lambda: calib._context(npz, calib.stock_words_of_entry(entry)))
    if py is None and err is None:
        got = calib._context(npz, calib.stock_words_of_entry(entry))
        assert payload == {"rows": len(got[2]), "d0": 24, "d1": 24}, name
    return py, err


def test_context_matches_python(tmp_path: Path) -> None:
    """`_context` on stored npz files: both sizes, and every error path."""
    require_cli()
    entry = cf.entry()
    stock = calib.stock_words_of_entry(entry)
    for rows in (4, 10):
        d0, d1, coef = cf.arrays(rows)
        py, err = _context_case(tmp_path, f"ok{rows}", cf.source_npz(stock, d0, d1, coef), entry)
        assert py is None and err is None, (py, err)
    d0, d1, coef = cf.arrays(4)
    s_eff = ((stock[:, 0] + d0) - 1j * (stock[:, 1] + d1)) / 32

    def npz(**arrays: np.ndarray) -> bytes:
        buf = io.BytesIO()
        np.savez(buf, **arrays)
        return buf.getvalue()

    full = {"stock": stock, "d0": d0, "d1": d1, "input_coefficients": coef, "S_eff": s_eff}
    bad = {
        "stock": {**full, "stock": stock + 1},
        "stock-shape": {**full, "stock": stock[:12]},
        "d0-shape": {**full, "d0": d0[:20]},
        "coef-shape": {**full, "input_coefficients": np.zeros((5, 3))},
        "nonfinite": {**full, "d1": np.where(np.arange(24) == 3, np.inf, d1)},
        "s-eff": {**full, "S_eff": s_eff * (1 + 1e-15)},
    }
    for missing in ("stock", "d0", "d1", "input_coefficients", "S_eff"):
        bad[f"missing-{missing}"] = {k: v for k, v in full.items() if k != missing}
    for name, arrays in bad.items():
        py, err = _context_case(tmp_path, name, npz(**arrays), entry)
        assert cp.same_error(py, err) is None, (name, cp.same_error(py, err))
    for name, damaged in cf.npz_damage(cf.source_npz(stock, d0, d1, coef), "input_coefficients.npy").items():
        py, err = _context_case(tmp_path, name, damaged, entry)
        assert cp.same_error(py, err) is None, (name, cp.same_error(py, err))
    py, err = _context_case(tmp_path, "pickle", b"not an npz at all", entry)
    assert cp.same_error(py, err) is None, cp.same_error(py, err)
    buf = io.BytesIO()
    np.savez_compressed(buf, **full)
    _payload, err = cp.cli(
        ["color-calib-check", "--npz", _write(tmp_path / "z.npz", buf.getvalue()), "--entry", _write(tmp_path / "e.bin", entry)]
    )
    assert err is not None and err.kind == "zip", err


def _npz_same(tmp: Path, name: str, data: bytes, entry: bytes) -> str | None:
    """None when `_context` and `color-calib-check` agree on this archive (both accept, or the
    same kind and message)."""
    py, err = _context_case(tmp, name, data, entry)
    if py is None and err is None:
        return None
    return cp.same_error(py, err)


def test_npz_duplicate_members_follow_numpy(tmp_path: Path) -> None:
    """Duplicate names: the last central record wins (`NameToInfo`), and a key matches the full
    name before `key + ".npy"` (`NpzFile`); the selected member is the one checked and read."""
    require_cli()
    corpus = nc.variants()
    stock = nc.source_parts()[0]
    for name in nc.DUPLICATES:
        assert _npz_same(tmp_path, name, corpus[name], stock) is None, (name, _npz_same(tmp_path, name, corpus[name], stock))
    ctx = nc.runtime_variants()["runtime_duplicate"]
    pack = cp.Pack(tmp_path / "runtime-duplicate", stock, ctx, json.dumps(cf.ring()).encode())
    _check_bake(tmp_path, pack, cf.entry(words=2), params={"step": 16})


def test_npz_stored_members_read_like_zipfile(tmp_path: Path) -> None:
    """Payload size and CRC come from the central directory, data descriptors are accepted, and
    a local header is checked only when its member is read, in zipfile's order and text."""
    require_cli()
    corpus = nc.variants()
    stock = nc.source_parts()[0]
    bad = [(name, why) for name in nc.STORED_RULES if (why := _npz_same(tmp_path, name, corpus[name], stock)) is not None]
    assert bad == [], bad
    ctx = nc.runtime_variants()["runtime_descriptor"]
    pack = cp.Pack(tmp_path / "runtime-descriptor", stock, ctx, json.dumps(cf.ring()).encode())
    _check_bake(tmp_path, pack, cf.entry(words=2), params={"step": 16})


def test_npz_corpus_matches_python(tmp_path: Path) -> None:
    """Sol's 42-variant NPZ corpus (review r2): `color-calib-check` accepts or refuses each
    archive exactly as `_context` does, with the same kind and message. The three runtime ctx
    variants bake the same table as Python."""
    require_cli()
    corpus = nc.variants()
    assert len(corpus) == 42
    stock = nc.source_parts()[0]
    differ = [(name, why) for name, data in corpus.items() if (why := _npz_same(tmp_path, name, data, stock)) is not None]
    assert [d for d in differ if d[0] not in nc.SHARED_LIMIT_EXCEPTIONS] == [], differ
    for name, ctx in nc.runtime_variants().items():
        pack = cp.Pack(tmp_path / name, stock, ctx, json.dumps(cf.ring()).encode())
        _check_bake(tmp_path, pack, cf.entry(words=2), params={"step": 16})


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def _pack_home(tmp: Path, stock04: bytes, unit: str = "jlaunch") -> Path:
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


def _pick(home: Path, iso: int, unit: str, stock: Path) -> tuple[Any, Any]:
    payload, err = cp.cli(["color-calib-check", "--dir", calib.calib_dir(home), "--unit", unit, "--iso", iso, "--stock", stock])
    try:
        _model, info = calib.model_for(home, iso, unit=unit)
        manifest = json.loads((calib.calib_dir(home) / "manifest.json").read_text(encoding="utf-8"))
        pair = manifest["units"][unit]["models"][str(info["model_iso"])]
        py: Any = {**info, "ctx": pair["ctx"], "ring": pair["ring"], "rows": len(_model.input_coefficients)}
    except Exception as exc:  # noqa: BLE001
        py = exc
    return py, (err if err is not None else payload)


def test_pack_model_for_matches_python(tmp_path: Path) -> None:
    """`model_for` on a pack: the ISO split, and the manifest checks."""
    require_cli()
    stock04 = cf.entry()
    home = _pack_home(tmp_path, stock04)
    stock = _write(tmp_path / "stock.bin", stock04)
    for iso in (100, 200, 565, 566, 800, 1600, 3200):
        py, rust = _pick(home, iso, "jlaunch", stock)
        assert py == rust, (iso, py, rust)
    py, rust = _pick(home, 200, "qlaunch", stock)
    assert cp.same_error(py, rust) is None
    py, rust = _pick(home, 200, "nounit", stock)
    assert cp.same_error(py, rust) is None
    other = _write(tmp_path / "other.bin", cf.entry(words=1))
    _payload, err = cp.cli(["color-calib-check", "--dir", calib.calib_dir(home), "--unit", "jlaunch", "--iso", 200, "--stock", other])
    assert err is not None and (err.kind, err.message) == ("stock_mismatch", "校准数据与这个单元的原厂 04 不匹配，请重新导入")
    target = calib.calib_dir(home) / "ring_jlaunch_1600.json"
    target.write_bytes(target.read_bytes() + b" ")
    py, rust = _pick(home, 200, "jlaunch", stock)
    assert cp.same_error(py, rust) is None
    manifest = calib.calib_dir(home) / "manifest.json"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace("calib/2", "calib/1"), encoding="utf-8")
    py, rust = _pick(home, 200, "jlaunch", stock)
    assert cp.same_error(py, rust) is None


# ---- forward model ----------------------------------------------------------------------


def _check_predict(tmp: Path, pack: Any, entry: bytes, codes: np.ndarray, **kw: Any) -> None:
    py, rust = cp.predict(tmp, pack, entry, codes, **kw)
    if isinstance(py, BaseException) or isinstance(rust, CoreCliError):
        assert cp.same_error(py, rust) is None, cp.same_error(py, rust)
        return
    if kw.get("flt"):
        gap, decided = cp.predict_gap(pack, entry, codes, py, rust, half=kw.get("half", 0), base=kw.get("base", False))
        for line in decided:
            print(f"predict allowed: {line}")
        assert gap is None, gap
    else:
        assert py.dtype == np.uint8 and rust.dtype == np.uint8
        assert np.array_equal(py, rust), cf.first_difference(py, rust)


def test_predict_matches_python_on_synthetic_models(tmp_path: Path) -> None:
    require_cli()
    from gxr_studio.engine.model import synthetic

    grid64 = synthetic()
    grid32 = grid64.astype(np.float32)
    random32 = cf.codes()
    for rows, ring_kw in ((4, {}), (10, {"kernel": "hard", "per_sector": False}), (4, {"kappa": 300.0})):
        pack = cp.synthetic_pack(tmp_path / f"p{rows}{len(ring_kw)}", rows=rows, **ring_kw)
        for entry in (cf.entry(words=2, tilt=5), cf.entry(flags=0x17, words=3), cf.entry(kind=3)):
            for codes in (grid64, grid32, random32):
                _check_predict(tmp_path, pack, entry, codes)
                _check_predict(tmp_path, pack, entry, codes, flt=True)
            _check_predict(tmp_path, pack, entry, random32, half=1)
            _check_predict(tmp_path, pack, entry, grid64, base=True)
            _check_predict(tmp_path, pack, entry, grid64, base=True, flt=True)


def test_predict_errors_match_python(tmp_path: Path) -> None:
    require_cli()
    pack = cp.synthetic_pack(tmp_path / "p")
    codes = cf.codes(n=200)
    lopsided = bytearray(cf.entry())
    lopsided[115:119] = bytes((9, 3, 3, 9))
    _check_predict(tmp_path, pack, bytes(lopsided), codes)
    high = bytearray(cf.entry())
    high[216:218] = (0x1000).to_bytes(2, "big")
    _check_predict(tmp_path, pack, bytes(high), codes)
    stock04 = cf.entry()
    d0, d1, coef = cf.arrays(4)
    rings = {
        "schema": {**cf.ring(), "schema": "gxr-sector-model/1"},
        "nodes": {**cf.ring(), "nodes_unwrapped_deg": [10.0 * s for s in range(23)] + [5.0]},
        "kernel": {**cf.ring(), "kernel": {"type": "box", "blur_deg": 5.0}},
        "k": {key: v for key, v in cf.ring().items() if key != "k_coupling"},
    }
    for name, ring in rings.items():
        bad = cp.Pack(tmp_path / name, stock04, cf.ctx_npz(d0, d1, coef), json.dumps(ring).encode())
        _check_predict(tmp_path, bad, cf.entry(), codes)
    for name, c in (("coef5", np.zeros((5, 3))), ("coefnan", np.full((4, 3), np.nan))):
        bad = cp.Pack(tmp_path / name, stock04, cf.ctx_npz(d0, d1, c), json.dumps(cf.ring()).encode())
        _check_predict(tmp_path, bad, cf.entry(), codes)


# ---- bake, apply, tones --------------------------------------------------------------------


def _check_bake(tmp: Path, pack: Any, entry: bytes, **kw: Any) -> Any:
    py, rust = cp.bake(tmp, pack, entry, **kw)
    if isinstance(py, BaseException) or isinstance(rust, CoreCliError):
        assert cp.same_error(py, rust) is None, cp.same_error(py, rust)
        return None
    gap, allowed = cp.bake_gap(py, rust, pack.model(), entry)
    for line in allowed:
        print(f"bake float32 allowed: {line}")
    assert gap is None, gap
    return py[2]


def test_bake_matches_python_on_synthetic_models(tmp_path: Path) -> None:
    require_cli()
    for rows, ring_kw in ((4, {}), (10, {"kernel": "hard", "per_sector": False, "kappa": 300.0})):
        pack = cp.synthetic_pack(tmp_path / f"p{rows}", rows=rows, **ring_kw)
        for entry in (
            cf.entry(words=2, tilt=5),
            cf.entry(flags=0x17, words=4, rows=((300, 600, 124), (-200, -300, 500), (480, -400, -80))),
            cf.entry(kind=3),
        ):
            _check_bake(tmp_path, pack, entry)
        _check_bake(tmp_path, pack, cf.entry(words=1), params={"step": 2.0})
        _check_bake(tmp_path, pack, cf.entry(words=1), params={"step": 1})
    pack = cp.synthetic_pack(tmp_path / "knobs")
    knob = {"amount": 70, "doses": [200, -100, 50, 0, 300, -40, 20, 0, 0, 0], "wb_method": 0, "tone_method": 1}
    _check_bake(tmp_path, pack, cf.entry(words=2), params={"knobs": knob}, menu=MENU)
    flat = cf.entry(rows=((306, 601, 117), (0, 0, 0), (0, 0, 0)))
    _py, rust = cp.bake(tmp_path, cp.Pack(tmp_path / "flat", flat, pack.ctx.read_bytes(), pack.ring.read_bytes()), cf.entry())
    assert isinstance(rust, CoreCliError) and rust.kind == "qhull"


def test_apply_and_tones_match_python(tmp_path: Path) -> None:
    require_cli()
    for rows in (4, 10):
        pack = cp.synthetic_pack(tmp_path / f"p{rows}", rows=rows)
        entry = cf.entry(words=2, tilt=5)
        bk = _check_bake(tmp_path, pack, entry)
        files = cp.write_bake(tmp_path, pack, entry, f"b{rows}")
        codes = cf.codes()
        for adjust, curves in TONE_CASES:
            py, rust = cp.apply(tmp_path, files, bk, codes, adjust, curves)
            assert not isinstance(rust, CoreCliError), rust
            assert rust.dtype == np.uint8 and np.array_equal(py, rust), (adjust, curves, cf.first_difference(py, rust))
        py, rust = cp.apply(tmp_path, files, bk, codes.reshape(40, 100, 3))
        assert np.array_equal(py, rust) and rust.shape == (40, 100, 3)
    for adjust, curves in TONE_CASES:
        a, c, params = cp.tone_args(adjust, curves)
        p, out = tmp_path / "tones.json", tmp_path / "tones.npy"
        p.write_text(json.dumps(params), encoding="utf-8")
        payload, err = cp.cli(["color-tones", "--params", p, "--out", out])
        assert err is None, err
        gains = tones.channel_gains(a)
        assert (payload["gains"] is None) == (gains is None)
        if gains is not None:
            # float64 intermediate (libm exp2): the 1e-12 band; the float32 the chain uses: exact.
            got_gains = np.asarray(payload["gains"], np.float64)
            assert cp.float_gap(gains, got_gains) is None, (payload["gains"], gains)
            assert np.array_equal(got_gains.astype(np.float32), gains.astype(np.float32))
        table = tones.display_table(a, c)
        assert (payload["table"] is None) == (table is None)
        if table is not None:
            got = np.load(out)
            assert got.dtype == np.float32 and np.array_equal(got, table), cf.first_difference(table, got)


TONE_CASES = [
    (None, None),
    ({"exposure_ev": 0.6667, "warm": 35, "tint": -20}, None),
    ({"exposure_ev": -1.25}, None),
    ({"contrast": 40, "highlight": -60, "shadow": 25}, None),
    ({"contrast": -100, "highlight": 100, "shadow": -100, "warm": -100}, None),
    (None, {"master": [[0, 0], [64, 80.5], [192, 170.25], [255, 255]]}),
    ({"shadow": 10}, {"master": [[0, 10], [255, 245]], "r": [[0, 0], [128, 150], [255, 255]], "b": [[30, 0], [200, 255]]}),
    (None, {"g": [[0, 0], [10, 40], [20, 30], [255, 250]]}),
]


# ---- real camera data and calibration, when this machine has them -----------------------

_PARENT = Path(os.environ.get("GXR_STUDIO_TEST_PARENT", str(Path(__file__).resolve().parents[2])))
CAPTURES = _PARENT / "captures"


def _real_dngs(cap: int = 60) -> list[Path]:
    """The raw parity test's sample: up to 60 DNGs under captures/, spread, no backups."""
    if not CAPTURES.is_dir():
        return []
    found = sorted(
        (p for p in CAPTURES.rglob("*") if p.is_file() and p.suffix.upper() == ".DNG" and "backup" not in p.as_posix().lower()),
        key=lambda p: p.as_posix(),
    )
    return found if len(found) <= cap else [found[(i * len(found)) // cap] for i in range(cap)]


@pytest.fixture(scope="module")
def real_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    raw = e0.fixture_dat(127)
    src = calib.dev_sources(e0.REPO)
    if raw is None or not src:
        pytest.skip("相机数据夹具（g127 DAT 和标定文件）不在本机")
    home = tmp_path_factory.mktemp("color-home")
    store.import_from_dat(raw, home)
    calib.import_calibration(home, src)
    return home


def _real_pack(tmp: Path, home: Path, unit: str, iso: int) -> Any:
    _model, info = calib.model_for(home, iso, unit=unit)
    folder = calib.calib_dir(home)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    pair = manifest["units"][unit]["models"][str(info["model_iso"])]
    return cp.Pack(
        tmp / f"{unit}-{iso}", store.load_stock04(home, unit), (folder / pair["ctx"]).read_bytes(), (folder / pair["ring"]).read_bytes()
    )


def test_real_packs_and_bakes_match_python(real_home: Path, tmp_path: Path) -> None:
    """Every unit, both model ISOs, every distinct recipe entry of the g127 card: float32 bytes."""
    require_cli()
    bakes = picks = 0
    for unit in datcodec.NAMES:
        stock = _write(tmp_path / f"{unit}-stock.bin", store.load_stock04(real_home, unit))
        for iso in (100, 200, 566, 1600):
            py, rust = _pick(real_home, iso, unit, stock)
            assert py == rust, (unit, iso, py, rust)
            picks += 1
        for iso in (200, 1600):
            pack = _real_pack(tmp_path, real_home, unit, iso)
            seen: set[bytes] = set()
            for slot in store.SLOTS:
                entry = store.load_entry(real_home, unit, slot)
                if entry in seen:
                    continue
                seen.add(entry)
                _check_bake(tmp_path, pack, entry)
                bakes += 1
    print(f"real picks={picks} bakes={bakes}")


def test_real_predict_matches_python(real_home: Path, tmp_path: Path) -> None:
    require_cli()
    from gxr_studio.engine.model import synthetic

    dngs = _real_dngs()
    crop = None
    if dngs:
        crop = run_npy(["dng-half-codes", str(dngs[0])], tmp_path / "half0.npy")[:60, :200].reshape(-1, 3)
    for unit in datcodec.NAMES:
        pack = _real_pack(tmp_path, real_home, unit, 200)
        for slot in (1, 4, 8):
            entry = store.load_entry(real_home, unit, slot)
            _check_predict(tmp_path, pack, entry, synthetic())
            _check_predict(tmp_path, pack, entry, synthetic(), flt=True)
            if crop is not None:
                _check_predict(tmp_path, pack, entry, crop)
    jl = _real_pack(tmp_path, real_home, "jlaunch", 1600)
    _check_predict(tmp_path, jl, store.load_entry(real_home, "jlaunch", 2), synthetic(), base=True)


def test_real_dngs_apply_matches_python(real_home: Path, tmp_path: Path) -> None:
    """Bake, then apply on half-resolution codes of at least five real DNGs: uint8 exact."""
    require_cli()
    dngs = _real_dngs()
    if len(dngs) < 5:
        pytest.skip("captures/ 下的真实 DNG 少于 5 张")
    picked = [dngs[(i * len(dngs)) // 5] for i in range(5)]
    for i, dng in enumerate(picked):
        unit = datcodec.NAMES[i % 3]
        iso = 1600 if i == 4 else 200
        pack = _real_pack(tmp_path, real_home, unit, iso)
        entry = store.load_entry(real_home, unit, 1 + (i * 3) % 8)
        bk = _check_bake(tmp_path, pack, entry)
        files = cp.write_bake(tmp_path, pack, entry, f"real{i}")
        codes = run_npy(["dng-half-codes", str(dng)], tmp_path / f"half{i}.npy")
        adjust, curves = TONE_CASES[(2 * i) % len(TONE_CASES)]
        py, rust = cp.apply(tmp_path, files, bk, codes, adjust, curves)
        assert not isinstance(rust, CoreCliError), rust
        assert rust.shape == codes.shape and np.array_equal(py, rust), (dng.name, cf.first_difference(py, rust))
        print(f"real apply {dng.name} unit={unit} iso={iso} pixels={codes.size // 3}")
    # The bakes with float32 cells at a rounding midpoint (jlaunch ISO 1600, slots 4 and 5) must
    # still give exact uint8 on a real photo.
    pack = _real_pack(tmp_path, real_home, "jlaunch", 1600)
    for k, slot in enumerate((4, 5)):
        entry = store.load_entry(real_home, "jlaunch", slot)
        bk = _check_bake(tmp_path, pack, entry)
        files = cp.write_bake(tmp_path, pack, entry, f"mid{slot}")
        codes = run_npy(["dng-half-codes", str(picked[k])], tmp_path / f"mid{slot}.npy")
        py, rust = cp.apply(tmp_path, files, bk, codes)
        assert not isinstance(rust, CoreCliError), rust
        assert rust.shape == codes.shape and np.array_equal(py, rust), (picked[k].name, slot, cf.first_difference(py, rust))
        print(f"real apply {picked[k].name} unit=jlaunch iso=1600 slot={slot} pixels={codes.size // 3}")


# ---- WASM against native ------------------------------------------------------------------


def test_wasm_matches_native(tmp_path: Path) -> None:
    require_cli()
    from support.wasm_check import run_area

    pack = cp.synthetic_pack(tmp_path / "w")
    entry = cf.entry(words=2, tilt=5)
    e = _write(tmp_path / "we.bin", entry)
    base = {"kind": "bake", "entry": str(e), "stock": str(pack.stock), "ctx": str(pack.ctx), "ring": str(pack.ring)}
    knob = tmp_path / "wknob.json"
    knob.write_text(json.dumps({"step": 0.5, "knobs": {"amount": 60, "doses": [100, 0, 0, 0, 200, 0, 0, 0, 0, 0]}}), encoding="utf-8")
    lopsided = bytearray(entry)
    lopsided[115:119] = bytes((9, 3, 3, 9))
    files = cp.write_bake(tmp_path, pack, entry, "wb")
    codes = tmp_path / "wcodes.npy"
    np.save(codes, cf.codes().reshape(40, 100, 3))
    tone = tmp_path / "wtone.json"
    tone.write_text(
        json.dumps({"adjust": {"exposure_ev": 0.5, "contrast": 30}, "curves": {"r": [[0, 0], [128, 140], [255, 255]]}}), encoding="utf-8"
    )
    apply = {"kind": "apply", "bake": str(files[0]), "meta": str(files[1]), "codes": str(codes)}
    specs = {
        "bake": base,
        "bake-bw": {**base, "entry": str(_write(tmp_path / "wbw.bin", cf.entry(kind=3)))},
        "bake-knobs": {**base, "params": str(knob), "menu": str(MENU)},
        "bake-error": {**base, "entry": str(_write(tmp_path / "wbad.bin", bytes(lopsided)))},
        "apply": {**apply, "out": str(tmp_path / "wa.npy")},
        "apply-tones": {**apply, "params": str(tone), "out": str(tmp_path / "wt.npy")},
    }
    # Sizes from input past the shared count limit: one error, the same on native and wasm32.
    meta = json.loads(files[1].read_text(encoding="utf-8"))
    for nx, ny in ((4294967295, 4294967295), (2147483648, 1), (2147483648, 2)):
        huge = tmp_path / f"wmeta-{nx}-{ny}.json"
        huge.write_text(json.dumps({**meta, "nx": nx, "ny": ny}), encoding="utf-8")
        specs[f"apply-huge-{nx}x{ny}"] = {**apply, "meta": str(huge), "out": str(tmp_path / f"wh-{nx}-{ny}.npy")}
    # JSON nesting on both sides of the 128 cap, as arrays and as objects.
    for levels in (128, 129):
        for kind in ("array", "object"):
            deep = tmp_path / f"wdeep-{kind}-{levels}.json"
            deep.write_text(cf.deep_json(levels, kind), encoding="utf-8")
            specs[f"bake-deep-{kind}-{levels}"] = {**base, "params": str(deep)}
    specs["apply-deep-array-129"] = {**apply, "params": str(tmp_path / "wdeep-array-129.json"), "out": str(tmp_path / "wd.npy")}
    # Sol r2's runtime ctx variants: valid, data descriptors, a duplicate d0.npy.
    for name, ctx in nc.runtime_variants().items():
        specs[f"bake-{name}"] = {**base, "ctx": str(_write(tmp_path / f"w{name}.npz", ctx))}
    # A pack ctx npz damaged the three ways Sol found.
    for name, damaged in cf.npz_damage(pack.ctx.read_bytes(), "d0.npy").items():
        specs[f"bake-ctx-{name}"] = {**base, "ctx": str(_write(tmp_path / f"wctx-{name}.npz", damaged))}
    fine = tmp_path / "wstep.json"
    fine.write_text(json.dumps({"step": 1e-9}), encoding="utf-8")
    specs["bake-huge-grid"] = {**base, "params": str(fine)}
    inputs = []
    for name, spec in specs.items():
        path = tmp_path / f"case-{name}.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        inputs.append(path)
    run_area("color", inputs, tmp_path)


def _npy_header(shape: tuple[int, ...]) -> bytes:
    """A format 1.0 `.npy` header for float32 with this shape and no data."""
    text = "{'descr': '<f4', 'fortran_order': False, 'shape': (" + ", ".join(str(d) for d in shape) + "), }"
    text += " " * ((64 - (10 + len(text) + 1) % 64) % 64) + "\n"
    return b"\x93NUMPY\x01\x00" + len(text).to_bytes(2, "little") + text.encode()


def test_huge_dimensions_are_refused(tmp_path: Path) -> None:
    """Shapes past the shared count limit fail with numpy's "array is too big" text, no panic."""
    require_cli()
    pack = cp.synthetic_pack(tmp_path / "h")
    files = cp.write_bake(tmp_path, pack, cf.entry(words=2), "h")
    too_big = "array is too big; `arr.size * arr.dtype.itemsize` is larger than the maximum possible size."
    for shape in ((4294967295, 4294967295, 3), (2147483648, 1, 3), (0, 4294967296, 3)):
        codes = _write(tmp_path / "huge.npy", _npy_header(shape))
        _payload, err = cp.cli(["color-apply", "--bake", files[0], "--meta", files[1], "--codes", codes, "--out", tmp_path / "o.npy"])
        assert err is not None and (err.kind, err.message) == ("value", too_big), (shape, err)
    codes = _write(tmp_path / "short.npy", _npy_header((1431655765, 1, 3)))
    _payload, err = cp.cli(["color-apply", "--bake", files[0], "--meta", files[1], "--codes", codes, "--out", tmp_path / "o.npy"])
    assert err is not None and err.kind == "npy", err


def test_float_gates_handle_nan_and_inf() -> None:
    """NaN meets NaN at the same position, NaN on one side fails, infinities compare exactly
    (UNIT-COMMON amendment); injected at Sol's two positions."""
    py = np.linspace(0.0, 255.0, 2958)
    py[1208], py[2585] = 0.3509369663653672, 3.185835616366532
    no_pack = cast(Any, None)
    for at in (1208, 2585):
        rs = py.copy()
        rs[at] = np.nan
        assert cp.float_gap(py, rs) is not None
        gap, _ = cp.predict_gap(no_pack, b"", py.reshape(-1, 3), py, rs, half=0, base=False)
        assert gap is not None and gap.startswith("NaN on one side") and str(at) in gap, gap
    both = py.copy()
    both[7] = np.nan
    assert cp.float_gap(both, both.copy()) is None
    assert cp.predict_gap(no_pack, b"", py.reshape(-1, 3), both, both.copy(), half=0, base=False)[0] is None
    for left, right, ok in (
        (np.inf, np.inf, True),
        (-np.inf, -np.inf, True),
        (np.inf, -np.inf, False),
        (1.0, np.inf, False),
        (np.inf, 1.0, False),
    ):
        a, b = py.copy(), py.copy()
        a[11], b[11] = left, right
        assert (cp.float_gap(a, b) is None) == ok, (left, right)
        assert (cp.predict_gap(no_pack, b"", py.reshape(-1, 3), a, b, half=0, base=False)[0] is None) == ok, (left, right)
