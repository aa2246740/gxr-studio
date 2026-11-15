"""Sequential-oracle parity for all real DAT builtins, Looks, records, and user recipes + ±max knobs.

Covers PORT-WAVE-1 Amendments 1-4 and UNIT-color-oracle:
- Gate A: Rust `color-bake` float32 bytes == sequential oracle bytes exact, zero masks.
- Gate B: any Python float32 diff only where Gate A holds, mechanically attributed (or_f64,
  py_f64 batched/single, midpoint side, and host negative control reproducing Python's value).
- Coverage: every builtin, Look, record, and user recipe across all real DATs under
  GXR_STUDIO_TEST_PARENT on jlaunch (ISO 200 & 1600), qlaunch (ISO 200), and l08firm (ISO 200),
  at defaults (step=0.375) and ±max per control (step=2.0), plus explicit g158 M9 STD / instax
  mini / QianXi A checks at step=0.375.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from gxr_studio.camdata import datcodec, knob_table, r5
from gxr_studio.engine import knobs
from gxr_studio.engine import menu as eng_menu
from support import color_fixtures as cf
from support import color_oracle as co
from support import color_parity as cp
from support import samples
from support.color_oracle_libm import color_libm_available, libm_backend
from support.core_cli import require_cli
from support.lut_oracle import interp_1d_seq
from test_core_parity_resolve import R5_BUNDLES, SINGLE_DATS

MODELS: tuple[tuple[str, int, int], ...] = (
    ("jlaunch", 0, 200),
    ("jlaunch", 0, 1600),
    ("qlaunch", 1, 200),
    ("l08firm", 2, 200),
)
MODEL_IDS = [f"{name}-iso{iso}" for name, _u, iso in MODELS]


def _load_real_dats() -> list[tuple[str, Any]]:
    dats: list[tuple[str, Any]] = [(label, datcodec.parse(path.read_bytes())) for label, path in SINGLE_DATS]
    dats.extend((label, r5.parse(dat_path.read_bytes(), fx_path.read_bytes())) for label, dat_path, fx_path in R5_BUNDLES)
    return dats


def _make_pack(folder: Path, stock04: bytes, u: int, iso: int, *, seed: int | None = None) -> tuple[cp.Pack, Any]:
    rows = 10 if iso == 1600 else 4
    ring_kw: dict[str, Any] = {"kernel": "hard", "per_sector": False, "kappa": 300.0} if iso == 1600 else {}
    d0, d1, coef = cf.arrays(rows, seed=(3 + u + iso if seed is None else seed))
    pack = cp.Pack(folder, stock04, cf.ctx_npz(d0, d1, coef), json.dumps(cf.ring(**ring_kw)).encode())
    return pack, pack.model()


def _menu_for(
    dat: Any, emb_menu: dict[str, Any]
) -> tuple[dict[str, Any], dict[tuple[int, int], list[dict[str, Any]]], dict[int, dict[str, Any]]]:
    menu_data = (
        knob_table.read(dat.kv2_block, emb_menu, mount_knobs_from_50mm=datcodec.PROFILES[dat.generation].mount_knobs_from_50mm)
        if dat.kv2_block is not None
        else emb_menu
    )
    ctrls = {c["id"]: c for c in menu_data["controls"]}
    by_slot = {(r["unit"], r["slot"]): menu_data["items"][r["first_item"] : r["first_item"] + r["item_count"]] for r in menu_data["recipes"]}
    return menu_data, by_slot, ctrls


def _collect_unit_cases(u: int, iso: int) -> tuple[list[tuple[bytes, bytes, str, str]], list[tuple[bytes, bytes, str, str]]]:
    """Return deduplicated `(base_cases, minmax_cases)` for `(u, iso)` across all real DATs."""
    dats = _load_real_dats()
    if not dats:
        return [], []
    emb_menu = eng_menu.load()
    base_seen: dict[tuple[bytes, bytes], tuple[str, str]] = {}
    knob_seen: dict[tuple[bytes, bytes], tuple[str, str]] = {}

    for dat_label, dat in dats:
        stock04 = dat.entry_of(u, 4)
        menu_data, by_slot, ctrls = _menu_for(dat, emb_menu)

        def record(family: str, tag: str, slot_base: int, entry_bytes: bytes, defaults_map: dict[str, int] | None = None) -> None:
            items = by_slot.get((u, slot_base), [])
            defs = [(defaults_map or {}).get(ctrls[it["control"]]["name"], it["default_level"]) for it in items]
            base_entry = knobs.apply(stock04, entry_bytes, eng_menu.request(menu_data, u, slot_base, defs))[0] if defaults_map else entry_bytes
            base_seen.setdefault((stock04, base_entry), (family, f"{dat_label}:{tag}"))
            knob_seen.setdefault((stock04, base_entry), (family, f"{dat_label}:{tag}:default"))
            for idx, it in enumerate(items):
                pts = [pt["level"] for pt in it["points"]]
                cname = ctrls[it["control"]]["name"]
                for bound, lv_val in (("min", min(pts)), ("max", max(pts))):
                    lv = list(defs)
                    lv[idx] = lv_val
                    ke = knobs.apply(stock04, entry_bytes, eng_menu.request(menu_data, u, slot_base, lv))[0]
                    knob_seen.setdefault((stock04, ke), (family, f"{dat_label}:{tag}:{cname}={bound}"))

        for s in range(1, 9):
            record("builtin", f"slot{s}", s, dat.entry_of(u, s))
        for rec in dat.recipes:
            if (rec.unit_mask >> u) & 1:
                record("record", rec.name, rec.base, rec.entry)
        for lk in dat.looks:
            if (lk.unit_mask >> u) & 1:
                record("look", lk.name, lk.base, lk.entry)
        for idx, usr in enumerate(dat.users):
            dmap = dict(zip(("contrast", "highlight", "shadow", "saturation"), usr["levels"][:4]))
            record("user", dat.user_names[idx], usr["base"], dat.entry_of(u, usr["base"]), dmap)

    base_cases = [(st, ent, fam, label) for (st, ent), (fam, label) in base_seen.items()]
    knob_cases = [(st, ent, fam, label) for (st, ent), (fam, label) in knob_seen.items()]
    return base_cases, knob_cases


def _run_bake_cases(
    tmp_path: Path,
    cases: list[tuple[bytes, bytes, str, str]],
    u: int,
    iso: int,
    step: float,
) -> None:
    if not cases:
        samples.missing("GXR_STUDIO_TEST_PARENT 下未找到真实 DAT 夹具")
    pack_map: dict[bytes, tuple[cp.Pack, Any, co.OracleGridCache, co.OracleGridCache]] = {}
    fam_total: dict[str, int] = {}
    fam_gate_b_cases: dict[str, int] = {}
    fam_gate_b_pts: dict[str, int] = {}
    fam_gate_a_fail: dict[str, int] = {}
    failures: list[str] = []
    params = None if step == 0.375 else {"step": step}

    work = tmp_path / "bake"
    work.mkdir(exist_ok=True)
    for idx, (stock04, entry, family, label) in enumerate(cases):
        if stock04 not in pack_map:
            pk, mdl = _make_pack(tmp_path / f"pk_{len(pack_map)}", stock04, u, iso)
            c_seq = co.build_grid_cache(mdl, step, use_libm=True, use_seq_matmul=True, use_seq_cplx=True)
            pack_map[stock04] = (pk, mdl, c_seq, c_seq)
        pk, mdl, c_seq, _c_unused = pack_map[stock04]
        py, rs = cp.bake(work, pk, entry, params=params)
        err, allowed = cp.bake_gate(py, rs, mdl, entry, grid_cache=c_seq)
        fam_total[family] = fam_total.get(family, 0) + 1
        if err is not None:
            fam_gate_a_fail[family] = fam_gate_a_fail.get(family, 0) + 1
            failures.append(f"[{idx}] {family} {label}: {err}")
        elif allowed:
            fam_gate_b_cases[family] = fam_gate_b_cases.get(family, 0) + 1
            fam_gate_b_pts[family] = fam_gate_b_pts.get(family, 0) + len(allowed)
            for line in allowed:
                print(f"gate-b u={u} iso={iso} step={step} {family} {label}: {line}")

    for fam in sorted(fam_total):
        print(
            f"summary u={u} iso={iso} step={step} family={fam}: total={fam_total[fam]} "
            f"gate_a_fail={fam_gate_a_fail.get(fam, 0)} "
            f"gate_b_cases={fam_gate_b_cases.get(fam, 0)} gate_b_points={fam_gate_b_pts.get(fam, 0)}"
        )
    assert failures == [], "\n".join(failures[:10])


def test_oracle_negative_controls_restoring_host_kernels_fail(tmp_path: Path) -> None:
    """Restoring host complex ops, vectorized host trig/exp/atan2, or FMA `np.interp` fails Gate A / f64 identity."""
    require_cli()
    g158 = dict(SINGLE_DATS).get("g158")
    if g158 is None:
        samples.missing("g158 fixture DAT not on this machine")
    dat = datcodec.parse(g158.read_bytes())
    stock04 = dat.entry_of(0, 4)
    pack, model = _make_pack(tmp_path / "pk", stock04, 0, 200, seed=3)
    instax = next(lk.entry for lk in dat.looks if lk.name == "instax mini" and (lk.unit_mask & 1))
    qianxi = next(lk.entry for lk in dat.looks if lk.name == "QianXi A" and (lk.unit_mask & 1))

    work = tmp_path / "w"
    work.mkdir()
    _py_i, rs_i = cp.bake(work, pack, instax)
    _py_q, rs_q = cp.bake(work, pack, qianxi)
    c_seq = co.build_grid_cache(model, 0.375)
    c_no_cplx = co.build_grid_cache(model, 0.375, use_seq_cplx=False)
    c_no_libm = co.build_grid_cache(model, 0.375, use_libm=False)
    c_host = co.build_grid_cache(model, 0.375, use_libm=False, use_seq_matmul=False, use_seq_cplx=False)

    d_seq_i, _, _, _ = co.oracle_bake_chroma(model, instax, 0.375, grid_cache=c_seq)
    d_nc_i, _, _, _ = co.oracle_bake_chroma(model, instax, 0.375, grid_cache=c_no_cplx, use_seq_cplx=False)
    d_nl_i, _, _, _ = co.oracle_bake_chroma(model, instax, 0.375, grid_cache=c_no_libm, use_libm=False)
    assert d_seq_i == rs_i[0], "instax mini Gate A must hold with sequential oracle"
    assert d_nc_i != rs_i[0], "negative control: restoring host complex op must fail Gate A on instax mini"
    assert d_nl_i != rs_i[0], "negative control: restoring host trig/atan2/hypot must fail Gate A on instax mini"

    d_seq_q, _, _, _ = co.oracle_bake_chroma(model, qianxi, 0.375, grid_cache=c_seq)
    d_host_q, _, _, _ = co.oracle_bake_chroma(model, qianxi, 0.375, grid_cache=c_host, use_libm=False, use_seq_matmul=False, use_seq_cplx=False)
    assert d_seq_q == rs_q[0], "QianXi A Gate A must hold with sequential oracle"
    assert d_host_q != rs_q[0], "negative control: restoring host BLAS/FMA path must reproduce Python and fail Gate A on QianXi A"

    # Negative control for np.interp FMA contraction on curve interpolation.
    entry = cf.entry(words=2, tilt=5)
    p_seq = co.pre_curves_f64(entry, use_seq_interp=True)
    p_host = co.pre_curves_f64(entry, use_seq_interp=False)
    assert np.any(p_seq != p_host), "host np.interp FMA contraction must differ in float64 from interp_1d_seq"
    xs = np.array([0.0, *np.frombuffer(entry[20:82], dtype=">u2").astype(np.float64), 4095.0])
    ys = np.array(list(entry[82:115]), dtype=np.float64)
    assert np.array_equal(p_seq[:, 0], interp_1d_seq(np.arange(4096, dtype=np.float64), xs, ys))


def test_oracle_negative_control_real_python_f64_drift_fails(tmp_path: Path) -> None:
    """Injecting 1e-10 into real `table.forward` (with float32 unchanged) must fail `bake_gate` (Sol r1 P1)."""
    from gxr_studio.gpu import tables
    from gxr_studio.render import chain

    require_cli()
    pack = cp.synthetic_pack(tmp_path / "pk", rows=4)
    model = pack.model()
    entry = cf.entry(words=2, tilt=5)
    work = tmp_path / "w"
    work.mkdir()
    py, rust = cp.bake(work, pack, entry, params={"step": 2.0})
    assert not isinstance(py, BaseException) and not isinstance(rust, cp.CoreCliError)
    cache = co.build_grid_cache(model, 2.0)
    host_cache = co.build_grid_cache(model, 2.0, use_libm=False, use_seq_matmul=False, use_seq_cplx=False)
    ok_err, _ok_allowed = cp.bake_gate(py, rust, model, entry, grid_cache=cache, host_cache=host_cache)
    assert ok_err is None, ok_err

    original_forward = model.engine.table.forward
    calls = 0
    injected = 0

    def capturing_forward(probes: np.ndarray, z: Any) -> Any:
        nonlocal calls, injected
        calls += 1
        result = original_forward(probes, z)
        g = result[0] / probes
        eligible = (np.abs(g.real) > 0.1) & (np.abs(g.imag) > 0.01)
        target = np.where(eligible, (g.real + 1e-10) + 1j * g.imag, g)
        candidate = target * probes
        actual_candidate = candidate / probes
        same32 = (actual_candidate.real.astype(np.float32) == g.real.astype(np.float32)) & (
            actual_candidate.imag.astype(np.float32) == g.imag.astype(np.float32)
        )
        chosen = eligible & same32
        injected += int(np.count_nonzero(chosen))
        return (np.where(chosen, candidate, result[0]), *result[1:])

    model.engine.table.forward = capturing_forward
    try:
        mutated_bk = chain.bake_chroma(model, entry, 2.0, workers=1)
        mutated_data, mutated_meta = tables.bake_bytes(mutated_bk)
        assert injected > 0 and mutated_data == py[0], "negative control must keep float32 bytes unchanged"
        calls_before = calls
        err_cached, _ = cp.bake_gate((mutated_data, mutated_meta, mutated_bk), rust, model, entry, grid_cache=cache, host_cache=host_cache)
        calls_after_cached = calls
        err_auto, _ = cp.bake_gate((mutated_data, mutated_meta, mutated_bk), rust, model, entry)
        calls_after_auto = calls
        assert calls_after_cached > calls_before, "cached bake_gate must invoke production table.forward"
        assert calls_after_auto > calls_after_cached, "auto-cache bake_gate must invoke production table.forward"
        assert err_cached is not None and "1e-12" in err_cached, err_cached
        assert err_auto is not None and "1e-12" in err_auto, err_auto
    finally:
        model.engine.table.forward = original_forward


@pytest.mark.parametrize("look_name", ["instax mini", "QianXi A", "M9 STD"])
def test_g158_reported_looks_default_and_minmax(tmp_path: Path, look_name: str) -> None:
    """Explicitly check g158 M9 STD, instax mini, and QianXi A on jlaunch ISO 200 (seed=3) at defaults & ±max."""
    require_cli()
    g158 = dict(SINGLE_DATS).get("g158")
    if g158 is None:
        samples.missing("g158 fixture DAT not on this machine")
    dat = datcodec.parse(g158.read_bytes())
    emb_menu = eng_menu.load()
    menu_data, by_slot, ctrls = _menu_for(dat, emb_menu)
    stock04 = dat.entry_of(0, 4)
    cases: list[tuple[bytes, bytes, str, str]] = []
    seen: set[bytes] = set()
    for lk_idx, lk in enumerate(dat.looks):
        if lk.name != look_name or not (lk.unit_mask & 1):
            continue
        if lk.entry not in seen:
            seen.add(lk.entry)
            cases.append((stock04, lk.entry, "look", f"{look_name}[{lk_idx}]:default"))
        items = by_slot.get((0, lk.base), [])
        defs = [it["default_level"] for it in items]
        for idx, it in enumerate(items):
            pts = [pt["level"] for pt in it["points"]]
            cname = ctrls[it["control"]]["name"]
            for bound, lv_val in (("min", min(pts)), ("max", max(pts))):
                lv = list(defs)
                lv[idx] = lv_val
                ke = knobs.apply(stock04, lk.entry, eng_menu.request(menu_data, 0, lk.base, lv))[0]
                if ke not in seen:
                    seen.add(ke)
                    cases.append((stock04, ke, "look", f"{look_name}[{lk_idx}]:{cname}={bound}"))
    assert cases, f"no entries found for {look_name}"
    pack, mdl = _make_pack(tmp_path / "pk_s3", stock04, 0, 200, seed=3)
    c_seq = co.build_grid_cache(mdl, 0.375)
    work = tmp_path / "w"
    work.mkdir()
    failures: list[str] = []
    for _st, entry, _fam, label in cases:
        py, rs = cp.bake(work, pack, entry)
        err, allowed = cp.bake_gate(py, rs, mdl, entry, grid_cache=c_seq)
        if err is not None:
            failures.append(f"{label}: {err}")
        for line in allowed:
            print(f"g158 {label}: {line}")
    assert failures == [], "\n".join(failures)


@pytest.mark.parametrize(("unit_name", "u", "iso"), MODELS, ids=MODEL_IDS)
def test_all_real_dats_base_recipes_step_0375(tmp_path: Path, unit_name: str, u: int, iso: int) -> None:
    """Every unique builtin/Look/record/user base entry of all real DATs at step=0.375 on each model."""
    require_cli()
    base_cases, _ = _collect_unit_cases(u, iso)
    _run_bake_cases(tmp_path, base_cases, u, iso, 0.375)


@pytest.mark.parametrize(
    ("unit_name", "u", "iso", "part"),
    [(*m, p) for m in MODELS for p in (0, 1)],
    ids=[f"{m}-p{p}" for m in MODEL_IDS for p in (0, 1)],
)
def test_all_real_dats_knobs_minmax_step_2(tmp_path: Path, unit_name: str, u: int, iso: int, part: int) -> None:
    """Every unique default and ±max per-control knob variant across all real DATs & 4 models (step=2.0)."""
    require_cli()
    _, knob_cases = _collect_unit_cases(u, iso)
    half = (len(knob_cases) + 1) // 2
    chunk = knob_cases[:half] if part == 0 else knob_cases[half:]
    _run_bake_cases(tmp_path, chunk, u, iso, 2.0)


def test_lut_libm_supports_color_ops() -> None:
    """`gxr-cli lut-libm --stream` must expose exp/sin/cos/atan2/hypot (tags 4-8) from `gxr_math`/libm."""
    require_cli()
    assert color_libm_available()
    assert libm_backend() == "lut-libm"
