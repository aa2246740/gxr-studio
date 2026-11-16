"""Parity of ``gxr_export::source`` (the export's base picture) with the desktop Python.

- ``source-plan``: ``RenderService.plan`` and the non-precise renderer's labels, over every lens
  unit, ISOs on both sides of the model switch, missing camera data, missing calibration, a pack
  without the unit, default selection, cross-unit recipes and local recipes.
- ``source-render``: ``render_full(open_dng(path), renderer)`` on real DNGs, uint8 exact.
- ``source-color-image``: every branch of ``finish.serve.color_image`` (``full=True``) and of
  ``lut.photo.color_image``, labels (Chinese and the EXIF description) exact, errors exact.

Camera data comes from the parent project's DAT (``GXR_STUDIO_TEST_PARENT``); the calibration
pack is synthetic unless the owner's sources are present, and its contents are never printed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

import e0_support as e0
from gxr_studio.camdata import calib
from gxr_studio.meta import read_dng_meta
from gxr_studio.render.params import Adjust, Curves, EditState, RecipeSel
from support import export_source_cases as xs
from support import export_source_run as xr
from support.core_cli import require_cli, run_json
from support.dng_writer import write_dng

DAT = xs.real_dat()
needs_dat = pytest.mark.skipif(DAT is None, reason="相机数据夹具（g127 DAT）不在本机")
REAL = xs.real_pairs(20)
CANDIDATES = xs.real_pairs(60)
LENSES = ("GR LENS A12 50mm F2.5 MACRO", "RICOH LENS A12 28mm F2.5", "GXR MOUNT A12", "S10 24-72mm F2.5-4.4 VC", None)
ADJUST_ON = Adjust(exposure_ev=0.7, warm=12, tint=-6, contrast=25, highlight=-30, shadow=20)
CURVES_ON = Curves(master=((0.0, 0.0), (64.0, 50.0), (192.0, 210.0), (255.0, 255.0)), g=((0.0, 8.0), (255.0, 247.0)))


@pytest.fixture(scope="module")
def homes(tmp_path_factory: pytest.TempPathFactory) -> dict[str, xs.Home]:
    if DAT is None:
        pytest.skip("相机数据夹具（g127 DAT）不在本机")
    require_cli()
    return {
        "all": xs.make_home(tmp_path_factory.mktemp("all"), DAT),
        "jlaunch": xs.make_home(tmp_path_factory.mktemp("jl"), DAT, units=("jlaunch",)),
        "nocal": xs.make_home(tmp_path_factory.mktemp("nocal"), DAT, units=None),
        "nodat": xs.make_home(tmp_path_factory.mktemp("nodat"), None),
    }


# ---- 1. plan ----------------------------------------------------------------------------


@needs_dat
@pytest.mark.parametrize("home", ["all", "jlaunch", "nocal", "nodat"])
def test_plan_matches_python(homes: dict[str, xs.Home], home: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = homes[home]
    recipes = (
        None,
        RecipeSel("jlaunch", 1),
        RecipeSel("qlaunch", 4),
        RecipeSel("l08firm", 8),
        RecipeSel("jlaunch", 3),
        RecipeSel("qlaunch", 6),
    )
    isos = (None, 0, 100, 200, 565, 566, 640, 1600, 3200) if home == "all" else (None, 200, 1600)
    n = errors = 0
    for lens in LENSES:
        for iso in isos:
            for recipe in recipes:
                shot = xs.shot_for(Path("/nonexistent/R0000001.DNG"), None, lens=lens, iso=iso)
                py, rs = xr.plan_case(h, tmp_path, shot, EditState(recipe=recipe), monkeypatch)
                assert rs == py, (home, lens, iso, recipe, py, rs)
                n += 1
                errors += "error" in py
    print(f"plan cases home={home}: {n} ({errors} RenderUnavailable)")
    assert n > 0


@needs_dat
def test_plan_local_recipes_and_missing_entry(homes: dict[str, xs.Home], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = homes["all"]
    local = RecipeSel("jlaunch", 2, kind="local", id="0" * 64)
    only_dng = xs.shot_for(Path("/x/R1.DNG"), None, lens=LENSES[0], iso=200)
    py, rs = xr.plan_case(h, tmp_path, only_dng, EditState(recipe=local), monkeypatch)
    assert py == rs and rs["error"]["kind"] == "local_recipe_needs_pair", (py, rs)
    pair = xs.shot_for(Path("/x/R1.DNG"), Path("/x/R1.JPG"), lens=LENSES[0], iso=200)
    py, rs = xr.plan_case(h, tmp_path, pair, EditState(recipe=local), monkeypatch)
    # Python resolves it in the desktop library; the phone has none and refuses (state is the same).
    assert py["error"]["state"] == rs["error"]["state"] == "local_recipe", (py, rs)
    assert rs["error"]["kind"] == "local_recipe_unsupported", rs
    s = xs.write_json(tmp_path / "s.json", only_dng)
    e = xs.write_json(tmp_path / "e.json", xs.edit_json(EditState()))
    code, payload = run_json(["source-plan", "--shot", str(s), "--edit", str(e), "--dat", str(h.dat), "--calib", str(h.calib)])
    assert code == 2 and payload["error"]["kind"] == "bad_request", payload


# ---- 2. full-size recipe render on real DNGs ---------------------------------------------


def _render_combos(i: int, unit: str) -> list[tuple[RecipeSel, Adjust, Curves | None]]:
    s1, s2 = i % 8 + 1, (i + 3) % 8 + 1
    other = ("qlaunch", "l08firm", "jlaunch")[i % 3]
    return [
        (RecipeSel(unit, s1), Adjust(), None),
        (RecipeSel(unit, s1), ADJUST_ON, CURVES_ON if i % 2 else None),
        (RecipeSel(other if i % 4 == 3 else unit, s2), ADJUST_ON, None),
    ]


@needs_dat
@pytest.mark.skipif(len(CANDIDATES) < 20, reason="需要至少 20 张真 DNG（GXR_STUDIO_TEST_PARENT/captures）")
def test_full_size_recipe_render_real_dngs(homes: dict[str, xs.Home], tmp_path: Path) -> None:
    h = homes["all"]
    renders = refused = 0
    rendered: list[Path] = []
    t_py = t_rs = 0.0
    for i, (dng, _jpg) in enumerate(CANDIDATES):
        if len(rendered) >= 20:
            break
        meta = read_dng_meta(dng)
        shot = xs.shot_for(dng, None, lens=meta.lens, iso=meta.iso)
        unit = h.render.photo_unit(shot) or "jlaunch"
        for sel, adjust, curves in _render_combos(i, unit):
            py, rs, info = xr.render_case(h, tmp_path, dng, shot, EditState(recipe=sel, adjust=adjust, curve=curves))
            t_py += info["python_s"]
            t_rs += info["rust_s"]
            if py is None:
                refused += 1  # same RawDecodeError text on both sides (render_case asserts it)
                break
            assert rs.dtype == np.uint8 and rs.shape == py.shape, (dng.name, py.shape, rs.shape)
            bad = np.argwhere(rs != py)
            assert len(bad) == 0, (dng.name, sel, adjust, info, len(bad), bad[:5].tolist())
            renders += 1
        else:
            rendered.append(dng)
    assert len(rendered) >= 20, (len(rendered), refused)
    print(
        f"real full-size recipe renders: {renders} on {len(rendered)} DNGs, 0 codes differ; {refused} unreadable DNGs refused alike;"
        f" python render_full {t_py:.1f}s, rust CLI (with bake) {t_rs:.1f}s in total"
    )


@needs_dat
@pytest.mark.skipif(not REAL, reason="没有真 DNG")
def test_full_size_render_with_owner_pack(tmp_path: Path) -> None:
    if not calib.dev_sources(e0.REPO):
        pytest.skip("机主的校准来源不在本机")
    h = xs.make_home(tmp_path, DAT, real_calib=True)
    for i, (dng, _jpg) in enumerate(REAL[:3]):
        meta = read_dng_meta(dng)
        shot = xs.shot_for(dng, None, lens=meta.lens, iso=meta.iso)
        sel = RecipeSel(h.render.photo_unit(shot) or "jlaunch", (i * 3) % 8 + 1)
        py, rs, _info = xr.render_case(h, tmp_path, dng, shot, EditState(recipe=sel, adjust=ADJUST_ON if i else Adjust()))
        assert (py is None and rs is None) or (rs.shape == py.shape and int((rs != py).sum()) == 0), dng.name
    print("owner pack renders: 3, 0 codes differ")


# ---- 4. color_image branches on small synthetic files ------------------------------------


def _mosaic(h: int, w: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(200, 3800, (h, w), dtype=np.uint16)


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for name, payload in {
        "ok": write_dng(_mosaic(48, 64, 1), white=4095, orientation=6),
        "ok8": write_dng(_mosaic(40, 56, 2), white=4095, orientation=8),
        "unreadable": write_dng(_mosaic(16, 16, 3), compression=7),
        "truncated": write_dng(_mosaic(48, 64, 4), strip_offset_past_end=True),
        "no_neutral": write_dng(_mosaic(48, 64, 5), white=4095, neutral=None),
    }.items():
        p = tmp_path / f"{name}.DNG"
        p.write_bytes(payload)
        out[name] = p
    rng = np.random.default_rng(9)
    jpg = tmp_path / "camera.JPG"
    Image.fromarray(rng.integers(0, 256, (64, 48, 3), dtype=np.uint8), "RGB").save(jpg, quality=92)
    out["jpg"] = jpg
    return out


PREVIEW = np.random.default_rng(11).integers(0, 256, (30, 40, 3), dtype=np.uint8)
BASES = ("auto", "camera-jpg", "neutral", "recipe", "standard", "other")


def _branch_cases(f: dict[str, Path], full: bool) -> list[tuple[str, dict[str, Any], EditState, str, np.ndarray | None]]:
    lens = LENSES[0]
    cases: list[tuple[str, dict[str, Any], EditState, str, np.ndarray | None]] = []
    shots = {
        "pair": xs.shot_for(f["ok"], f["jpg"], lens=lens, iso=200),
        "dng": xs.shot_for(f["ok8"], None, lens=LENSES[1], iso=1600),
        "jpg": xs.shot_for(None, f["jpg"], lens=lens, iso=200),
        "unreadable-pair": xs.shot_for(f["unreadable"], f["jpg"], lens=lens, iso=200),
        "unreadable-dng": xs.shot_for(f["unreadable"], None, lens=lens, iso=200),
        "truncated-pair": xs.shot_for(f["truncated"], f["jpg"], lens=lens, iso=200),
        "truncated-dng": xs.shot_for(f["truncated"], None, lens=lens, iso=200),
    }
    edits = {
        "none": EditState(),
        "adjust": EditState(adjust=ADJUST_ON, curve=CURVES_ON),
        "recipe": EditState(recipe=RecipeSel("jlaunch", 5), adjust=ADJUST_ON),
        "cross": EditState(recipe=RecipeSel("qlaunch", 2)),
        "local": EditState(recipe=RecipeSel("jlaunch", 2, kind="local", id="0" * 64)),
    }
    if not full:
        # Without (all) the calibration, only the RenderUnavailable handling differs.
        shots = {k: shots[k] for k in ("pair", "dng", "jpg")}
        edits = {k: edits[k] for k in ("none", "adjust", "recipe", "cross")}
    for sname, shot in shots.items():
        for ename, edit in edits.items():
            for base in BASES:
                cases.append((f"{sname}/{ename}/{base}", shot, edit, base, None))
    for sname in ("unreadable-pair", "unreadable-dng") if full else ():
        for ename in ("none", "recipe", "local"):
            for base in ("auto", "neutral", "standard", "recipe"):
                cases.append((f"{sname}+preview/{ename}/{base}", shots[sname], edits[ename], base, PREVIEW))
    return cases


@needs_dat
@pytest.mark.parametrize("home", ["all", "jlaunch", "nocal", "nodat"])
def test_color_image_every_branch(
    homes: dict[str, xs.Home], home: str, files: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = homes[home]
    paths: dict[str, int] = {}
    errors: dict[str, int] = {}
    for label, shot, edit, base, preview in _branch_cases(files, home == "all"):
        python, rust, py_rgb, rs_rgb = xr.color_case(h, tmp_path, shot, edit, base, preview=preview, monkeypatch=monkeypatch)
        xr.assert_same_result(python, rust, py_rgb, rs_rgb, f"{home}:{label}")
        if "error" in python:
            errors[python["error"]["kind"]] = errors.get(python["error"]["kind"], 0) + 1
        else:
            paths[rust["path"]] = paths.get(rust["path"], 0) + 1
    print(f"color_image home={home}: paths={paths} errors={errors}")
    if home == "all":
        assert {"camera-jpg", "camera-jpg-fallback", "raw-recipe", "raw-preview"} <= set(paths), paths
        assert {"no_raw", "local_recipe_needs_pair", "raw_unreadable", "no_jpg_source"} <= set(errors), errors
    if home == "nocal":
        assert "raw-neutral" in paths and "camera-jpg-fallback" in paths and "calibration_missing" in errors, (paths, errors)
    if home == "nodat":
        assert "camera_data_missing" in errors, errors


@needs_dat
def test_neutral_base_is_neutral_when_calibrated(
    homes: dict[str, xs.Home], files: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Base "neutral" never plans a recipe: NEUTRAL pixels, neutral labels and from_raw false, even
    with camera data and calibration for the unit (desktop-quirks fix; it used to render stock 04)."""
    shot = xs.shot_for(files["ok"], None, lens=LENSES[0], iso=200)
    edit = EditState(adjust=ADJUST_ON, curve=CURVES_ON)
    python, rust, py_rgb, rs_rgb = xr.color_case(homes["all"], tmp_path, shot, edit, "neutral", monkeypatch=monkeypatch)
    xr.assert_same_result(python, rust, py_rgb, rs_rgb, "neutral-calibrated")
    assert rust["path"] == "raw-neutral" and rust["from_raw"] is False, rust
    labels = rust["labels"]
    assert (labels["id"], labels["text"], labels["calibrated"]) == ("neutral-preview", "中性转换", "0"), labels
    assert labels["description"].startswith("GXR Studio neutral-preview"), labels


@needs_dat
@pytest.mark.skipif(not REAL, reason="没有真 DNG")
def test_color_image_real_pairs_full_size(homes: dict[str, xs.Home], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The branches that produce pixels, on real RAW+JPG pairs at full size."""
    h = homes["all"]
    n = 0
    pairs = [(d, j) for d, j in REAL if j is not None][:3]
    for dng, jpg in pairs:
        meta = read_dng_meta(dng)
        shot = xs.shot_for(dng, jpg, lens=meta.lens, iso=meta.iso)
        for edit, base in (
            (EditState(recipe=RecipeSel("jlaunch", 7), adjust=ADJUST_ON), "auto"),
            (EditState(adjust=ADJUST_ON), "standard"),
            (EditState(), "auto"),
        ):
            python, rust, py_rgb, rs_rgb = xr.color_case(h, tmp_path, shot, edit, base, monkeypatch=monkeypatch)
            xr.assert_same_result(python, rust, py_rgb, rs_rgb, f"{dng.name}/{base}")
            n += 1
    nocal = homes["nocal"]
    for dng, jpg in pairs[:1]:
        meta = read_dng_meta(dng)
        shot = xs.shot_for(dng, jpg, lens=meta.lens, iso=meta.iso)
        for edit, base in ((EditState(), "standard"), (EditState(recipe=RecipeSel("jlaunch", 1)), "recipe")):
            python, rust, py_rgb, rs_rgb = xr.color_case(nocal, tmp_path, shot, edit, base, monkeypatch=monkeypatch)
            xr.assert_same_result(python, rust, py_rgb, rs_rgb, f"nocal:{dng.name}/{base}")
            n += 1
    print(f"real full-size color_image cases: {n}")


# ---- 3. LUT pictures ---------------------------------------------------------------------

GRADE = (np.array([1.05, 0.96, 0.91]), np.array([0.02, -0.01, 0.03]))
DISPLAY_SPEC = {
    "input_space": "srgb",
    "output_space": "srgb",
    "input_gamut": None,
    "output_gamut": None,
    "input_range": "full",
    "output_range": "full",
}
LOG_SPEC = {
    "input_space": "slog3",
    "output_space": "srgb",
    "input_gamut": None,
    "output_gamut": None,
    "input_range": "full",
    "output_range": "full",
}


def _grade_lut(tmp: Path, spec: dict[str, Any], lut_id: str) -> xr.Lut:
    from support.lut_cases import cube_text

    return xr.Lut(tmp, cube_text(lambda x: np.clip(x * GRADE[0] + GRADE[1], 0, 1), size=17), spec, lut_id)


@needs_dat
def test_lut_branches_and_errors(homes: dict[str, xs.Home], files: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = homes["all"]
    display, log = _grade_lut(tmp_path, DISPLAY_SPEC, "disp"), _grade_lut(tmp_path, LOG_SPEC, "log")
    missing = xr.Lut(tmp_path, (tmp_path / "disp.cube").read_text(encoding="utf-8"), {"input_space": None}, "nospace")
    shots = {
        "pair": xs.shot_for(files["ok"], files["jpg"], lens=LENSES[0], iso=200),
        "dng": xs.shot_for(files["ok8"], None, lens=LENSES[2], iso=200),
        "jpg": xs.shot_for(None, files["jpg"], lens=LENSES[0], iso=200),
        "no-neutral": xs.shot_for(files["no_neutral"], None, lens=LENSES[0], iso=200),
        "unreadable": xs.shot_for(files["unreadable"], files["jpg"], lens=LENSES[0], iso=200),
    }
    from support import export_source_lut as gates

    kinds: dict[str, int] = {}
    points = {"a": 0, "b": 0}
    for lut in (display, log, missing):
        for sname, shot in shots.items():
            for recipe in (None, RecipeSel("jlaunch", 6)):
                for base in ("auto", "camera-jpg", "neutral", "standard"):
                    edit = EditState(recipe=recipe, adjust=ADJUST_ON, lut=lut.id)
                    python, rust, py_rgb, rs_rgb = xr.color_case(h, tmp_path, shot, edit, base, lut=lut, monkeypatch=monkeypatch)
                    label = f"{lut.id}/{sname}/{recipe}/{base}"
                    if "error" in python or py_rgb is None or rs_rgb is None or py_rgb.shape != rs_rgb.shape:
                        xr.assert_same_result(python, rust, py_rgb, rs_rgb, label)
                    else:
                        assert list(rust["labels"].items()) == list(python["labels"].items()), label
                        assert rust["from_raw"] is python["from_raw"] is False, label
                        if rust["path"] == "lut-display":
                            _b, _r, b_py, _b_rs = xr.color_case(h, tmp_path, shot, EditState(recipe=recipe), base, monkeypatch=monkeypatch)
                            assert b_py is not None
                            n_a, n_b = gates.display_gate(tmp_path, lut.cube, lut.spec, b_py, py_rgb, rs_rgb, label)
                        else:
                            n_a, n_b = gates.log_gate(
                                tmp_path, lut.cube, lut.spec, Path(shot["files"]["dng"]["relpath"]), py_rgb, rs_rgb, label, sample=4000
                            )
                        points["a"] += n_a
                        points["b"] += n_b
                    key = python["error"]["kind"] if "error" in python else rust["path"]
                    kinds[key] = kinds.get(key, 0) + 1
    print(f"lut branches: {kinds} gate_a_points={points['a']} gate_b_points={points['b']}")
    assert {"lut-display", "lut-log", "lut_needs_raw", "lut_space_required", "no_neutral"} <= set(kinds), kinds


@needs_dat
@pytest.mark.skipif(not REAL, reason="没有真 DNG")
def test_lut_full_size_real(homes: dict[str, xs.Home], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Display LUT on the camera JPG and on a recipe render; log LUT on the RAW. Gate A and B."""
    from support import export_source_lut as gates

    h = homes["all"]
    display, log = _grade_lut(tmp_path, DISPLAY_SPEC, "disp"), _grade_lut(tmp_path, LOG_SPEC, "log")
    totals = {"a": 0, "b": 0, "cases": 0}
    for dng, jpg in [(d, j) for d, j in REAL if j is not None][:2]:
        meta = read_dng_meta(dng)
        shot = xs.shot_for(dng, jpg, lens=meta.lens, iso=meta.iso)
        for lut, edit, base in (
            (display, EditState(lut="disp"), "camera-jpg"),
            (display, EditState(recipe=RecipeSel("jlaunch", 2), lut="disp"), "auto"),
            (log, EditState(lut="log", adjust=ADJUST_ON), "auto"),
        ):
            python, rust, py_rgb, rs_rgb = xr.color_case(h, tmp_path, shot, edit, base, lut=lut, monkeypatch=monkeypatch)
            assert "error" not in python and "error" not in rust, (python, rust)
            assert list(rust["labels"].items()) == list(python["labels"].items())
            assert py_rgb is not None and rs_rgb is not None and py_rgb.shape == rs_rgb.shape
            if lut is display:
                b_python, b_rust, b_py, _b_rs = xr.color_case(h, tmp_path, shot, EditState(recipe=edit.recipe), base, monkeypatch=monkeypatch)
                assert b_py is not None
                n_a, n_b = gates.display_gate(tmp_path, lut.cube, lut.spec, b_py, py_rgb, rs_rgb, f"{dng.name}/{base}")
            else:
                n_a, n_b = gates.log_gate(tmp_path, lut.cube, lut.spec, dng, py_rgb, rs_rgb, f"{dng.name}/log")
            totals["a"] += n_a
            totals["b"] += n_b
            totals["cases"] += 1
    print(f"real full-size LUT cases={totals['cases']} gate_a_points={totals['a']} gate_b_points={totals['b']}")


# ---- 5. WASM matches native --------------------------------------------------------------


@needs_dat
def test_wasm_matches_native_source(homes: dict[str, xs.Home], files: dict[str, Path], tmp_path: Path) -> None:
    from support.color_fixtures import deep_json
    from support.wasm_check import run_area
    from test_core_parity_raw import hostile_cases

    h, nocal = homes["all"], homes["nocal"]
    specs: list[Path] = []

    def spec(name: str, value: dict[str, Any]) -> None:
        specs.append(xs.write_json(tmp_path / f"{name}.json", value))

    shot = xs.write_json(tmp_path / "shot.json", xs.shot_for(files["ok"], files["jpg"], lens=LENSES[0], iso=640))
    recipe = xs.write_json(tmp_path / "edit.json", xs.edit_json(EditState(recipe=RecipeSel("jlaunch", 3))))
    default = xs.write_json(tmp_path / "edit-default.json", xs.edit_json(EditState()))
    (tmp_path / "deep.json").write_text(deep_json(129, "array"), encoding="utf-8")
    (tmp_path / "deep-ok.json").write_text('{"recipe":null,"x":' + deep_json(126, "array") + "}", encoding="utf-8")
    entry = tmp_path / "entry.bin"
    entry.write_bytes(h.entry(RecipeSel("jlaunch", 3)) or b"")
    stock = tmp_path / "stock.bin"
    stock.write_bytes(h.entry(RecipeSel("jlaunch", 4)) or b"")
    short = tmp_path / "short.bin"
    short.write_bytes(entry.read_bytes()[:551])
    empty_dir = tmp_path / "empty-calib"
    empty_dir.mkdir()
    base = {"shot": str(shot), "dat": str(h.dat), "entry": str(entry), "calib": str(h.calib), "binding": h.binding()}
    spec("plan", {"kind": "plan", "edit": str(recipe), **base})
    spec("plan-default", {"kind": "plan", "edit": str(default), **base})
    spec("plan-nodat", {"kind": "plan", "edit": str(recipe), "shot": str(shot), "calib": str(h.calib)})
    spec("plan-nocal", {"kind": "plan", "edit": str(recipe), "shot": str(shot), "dat": str(nocal.dat), "entry": str(entry)})
    spec("plan-deep", {"kind": "plan", "edit": str(tmp_path / "deep.json"), **base})
    spec("plan-deep-ok", {"kind": "plan", "edit": str(tmp_path / "deep-ok.json"), **base})
    spec("plan-no-entry", {"kind": "plan", "edit": str(recipe), "shot": str(shot), "dat": str(h.dat), "calib": str(h.calib)})
    adjust = xs.write_json(tmp_path / "adjust.json", xs.adjust_json(ADJUST_ON, CURVES_ON))
    render = {"entry": str(entry), "stock": str(stock), "unit": "jlaunch", "calib": str(h.calib), "iso": 640}
    for name in ("ok", "ok8", "unreadable", "truncated", "no_neutral"):
        spec(
            f"render-{name}", {"kind": "render", "dng": str(files[name]), "adjust": str(adjust), "out": str(tmp_path / f"{name}.npy"), **render}
        )
    spec("render-short-entry", {"kind": "render", "dng": str(files["ok"]), "out": str(tmp_path / "s.npy"), **{**render, "entry": str(short)}})
    spec("render-no-pack", {"kind": "render", "dng": str(files["ok"]), "out": str(tmp_path / "np.npy"), **{**render, "calib": str(empty_dir)}})
    spec(
        "render-deep-adjust",
        {"kind": "render", "dng": str(files["ok"]), "adjust": str(tmp_path / "deep.json"), "out": str(tmp_path / "d.npy"), **render},
    )
    for name, payload in hostile_cases().items():
        p = tmp_path / f"hostile-{name}.DNG"
        p.write_bytes(payload)
        spec(f"render-hostile-{name}", {"kind": "render", "dng": str(p), "out": str(tmp_path / f"h-{name}.npy"), **render})
    for name, value in review_r1_specs(tmp_path, recipe, base, render, files).items():
        spec(name, value)
    if REAL:
        spec("render-real", {"kind": "render", "dng": str(REAL[0][0]), "out": str(tmp_path / "real.npy"), "timing": True, **render})
    proc = run_area("source", specs, tmp_path)
    print(proc.stdout.strip())
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---- Sol review r1: input checks ---------------------------------------------------------


def review_r1_specs(tmp: Path, edit: Path, base: dict[str, Any], render: dict[str, Any], files: dict[str, Path]) -> dict[str, dict[str, Any]]:
    """The reviewer's counterexamples, as native/WASM comparison inputs."""
    empty = tmp / "empty.bin"
    empty.write_bytes(b"")
    ok = {"kind": "render", "dng": str(files["ok"]), **render}
    out: dict[str, dict[str, Any]] = {
        # 1. Zero bytes means "not given" on both sides.
        "r1-render-empty-adjust": {**ok, "adjust": str(empty), "out": str(tmp / "r1-ea.npy")},
        "r1-plan-empty-entry": {"kind": "plan", "edit": str(edit), **{**base, "entry": str(empty)}},
        "r1-plan-empty-dat": {"kind": "plan", "edit": str(edit), **{**base, "dat": str(empty)}},
        "r1-plan-empty-library": {"kind": "plan", "edit": str(edit), **{**base, "library": str(empty)}},
    }
    # 2. Entries are exactly 552 bytes, checked before planning or rendering.
    good = Path(base["entry"]).read_bytes()
    for n in (1, 551, 553):
        e = tmp / f"entry-{n}.bin"
        e.write_bytes((good + b"\x00")[:n])
        out[f"r1-plan-entry-{n}"] = {"kind": "plan", "edit": str(edit), **{**base, "entry": str(e)}}
        out[f"r1-render-entry-{n}"] = {**ok, "entry": str(e), "out": str(tmp / f"r1-e{n}.npy")}
        out[f"r1-render-stock-{n}"] = {**ok, "stock": str(e), "out": str(tmp / f"r1-s{n}.npy")}
    # 4. Non-finite exposure and curve points.
    for name, text in NON_FINITE_TONES.items():
        a = tmp / f"tones-{name}.json"
        a.write_text(text, encoding="utf-8")
        out[f"r1-render-tones-{name}"] = {**ok, "adjust": str(a), "out": str(tmp / f"r1-t-{name}.npy")}
    # 5. WASM number parameters: iso arrives as an f64 and is checked before narrowing.
    for text in ISO_NUMBERS:
        out[f"r1-render-iso-{text}"] = {**ok, "iso": text, "out": str(tmp / f"r1-iso-{text}.npy")}
    return out


ISO_NUMBERS = ("4294967297", "-1", "9007199254740992", "NaN", "Infinity", "-Infinity", "1.5")


@needs_dat
def test_iso_numbers(homes: dict[str, xs.Home], files: dict[str, Path], tmp_path: Path) -> None:
    """UNIT-COMMON "WASM number parameters": 2**32+1, -1 and 2**53 pick a model; NaN, ±Infinity, 1.5 fail."""
    h = homes["all"]
    entry, stock = tmp_path / "e.bin", tmp_path / "s.bin"
    entry.write_bytes(h.entry(RecipeSel("jlaunch", 3)) or b"")
    stock.write_bytes(h.entry(RecipeSel("jlaunch", 4)) or b"")
    args = [
        "source-render",
        "--dng",
        str(files["ok"]),
        "--entry",
        str(entry),
        "--stock",
        str(stock),
        "--unit",
        "jlaunch",
        "--calib",
        str(h.calib),
    ]
    pics: dict[str, np.ndarray] = {}
    for text in ("200", "1600", *ISO_NUMBERS):
        out = tmp_path / "o.npy"
        code, payload = run_json([*args, "--iso", text, "--out", str(out)])
        if text in ("NaN", "Infinity", "-Infinity", "1.5"):
            assert (code, payload) == (2, {"error": {"kind": "bad_request", "message": "iso 要是整数"}}), (text, payload)
        else:
            assert code == 0, (text, payload)
            pics[text] = np.load(out)
    assert np.array_equal(pics["4294967297"], pics["1600"]) and np.array_equal(pics["9007199254740992"], pics["1600"])
    assert np.array_equal(pics["-1"], pics["200"])
    code, payload = run_json([*args, "--iso", "9007199254740994", "--out", str(tmp_path / "o.npy")])
    assert (code, payload) == (2, {"error": {"kind": "bad_request", "message": "iso 超出范围"}}), payload


NON_FINITE_TONES = {
    "nan": '{"adjust":{"exposure_ev":NaN}}',
    "inf": '{"adjust":{"exposure_ev":Infinity}}',
    "-inf": '{"adjust":{"exposure_ev":-Infinity}}',
    "curve-nan": '{"curves":{"master":[[0,0],[NaN,255]]}}',
    "curve-inf": '{"curves":{"g":[[0,Infinity],[255,255]]}}',
}


@needs_dat
def test_non_finite_tones_are_refused(homes: dict[str, xs.Home], files: dict[str, Path], tmp_path: Path) -> None:
    """r1 P2-4: Python's parser refuses NaN/Infinity exposure (bad_adjust) and curve points (bad_curve)."""
    import json

    from gxr_studio.render.params import EditError, parse_adjust, parse_curve

    h = homes["all"]
    entry, stock = tmp_path / "e.bin", tmp_path / "s.bin"
    entry.write_bytes(h.entry(RecipeSel("jlaunch", 3)) or b"")
    stock.write_bytes(h.entry(RecipeSel("jlaunch", 4)) or b"")
    shot = xs.write_json(tmp_path / "shot.json", xs.shot_for(files["ok"], None, lens=LENSES[0], iso=200))
    for name, text in NON_FINITE_TONES.items():
        raw = json.loads(text)
        with pytest.raises(EditError) as caught:
            if "adjust" in raw:
                parse_adjust(raw["adjust"])
            else:
                parse_curve(raw["curves"])
        py = {"kind": caught.value.code, "message": caught.value.message}
        a = tmp_path / f"{name}.json"
        a.write_text(text, encoding="utf-8")
        args = ["source-render", "--dng", str(files["ok"]), "--entry", str(entry), "--stock", str(stock), "--unit", "jlaunch"]
        code, payload = run_json([*args, "--calib", str(h.calib), "--iso", "200", "--adjust", str(a), "--out", str(tmp_path / "o.npy")])
        assert (code, payload) == (2, {"error": py}), (name, py, payload)
        e = tmp_path / f"edit-{name}.json"
        e.write_text(text, encoding="utf-8")
        code, payload = run_json(
            ["source-color-image", "--shot", str(shot), "--edit", str(e), "--base", "standard", "--dng", str(files["ok"])]
            + ["--dat", str(h.dat), "--calib", str(h.calib), "--entry", str(stock), "--out", str(tmp_path / "c.npy")]
        )
        assert (code, payload) == (2, {"error": py}), (name, py, payload)


@needs_dat
def test_entry_must_be_552_bytes(homes: dict[str, xs.Home], files: dict[str, Path], tmp_path: Path) -> None:
    """r1 P2-2: plan and render refuse 1, 551 and 553 bytes with `bake_chroma`'s text; 552 plans."""
    h = homes["all"]
    good = h.entry(RecipeSel("jlaunch", 3)) or b""
    assert len(good) == 552
    shot = xs.write_json(tmp_path / "shot.json", xs.shot_for(files["ok"], None, lens=LENSES[0], iso=200))
    edit = xs.write_json(tmp_path / "edit.json", xs.edit_json(EditState(recipe=RecipeSel("jlaunch", 3))))
    stock = tmp_path / "stock.bin"
    stock.write_bytes(h.entry(RecipeSel("jlaunch", 4)) or b"")
    for n in (1, 551, 552, 553):
        e = tmp_path / f"e{n}.bin"
        e.write_bytes((good + b"\x00")[:n])
        code, payload = run_json(
            ["source-plan", "--shot", str(shot), "--edit", str(edit), "--dat", str(h.dat), "--calib", str(h.calib), "--entry", str(e)]
        )
        r_code, r_payload = run_json(
            ["source-render", "--dng", str(files["ok"]), "--entry", str(e), "--stock", str(stock), "--unit", "jlaunch"]
            + ["--calib", str(h.calib), "--iso", "200", "--out", str(tmp_path / "o.npy")]
        )
        if n == 552:
            assert code == 0 and payload["plan"]["calibrated"] is True, payload
            assert r_code == 0, r_payload
        else:
            want = {"error": {"kind": "value", "message": "entry must be 552 bytes"}}
            assert (code, payload) == (2, want), (n, payload)
            assert (r_code, r_payload) == (2, want), (n, r_payload)


@needs_dat
def test_empty_inputs_mean_absent(homes: dict[str, xs.Home], files: dict[str, Path], tmp_path: Path) -> None:
    """r1 P2-1: an empty adjust file is `{}`; an empty DAT, library or entry is not given."""
    h = homes["all"]
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    braces = xs.write_json(tmp_path / "braces.json", {})
    entry, stock = tmp_path / "e.bin", tmp_path / "s.bin"
    entry.write_bytes(h.entry(RecipeSel("jlaunch", 3)) or b"")
    stock.write_bytes(h.entry(RecipeSel("jlaunch", 4)) or b"")
    pics = []
    for adjust in (empty, braces):
        out = tmp_path / f"{adjust.stem}.npy"
        args = ["source-render", "--dng", str(files["ok"]), "--entry", str(entry), "--stock", str(stock), "--unit", "jlaunch"]
        code, payload = run_json([*args, "--calib", str(h.calib), "--iso", "200", "--adjust", str(adjust), "--out", str(out)])
        assert code == 0, payload
        pics.append(np.load(out))
    assert np.array_equal(pics[0], pics[1])
    shot = xs.write_json(tmp_path / "shot.json", xs.shot_for(files["ok"], None, lens=LENSES[0], iso=200))
    edit = xs.write_json(tmp_path / "edit.json", xs.edit_json(EditState(recipe=RecipeSel("jlaunch", 3))))
    plan = ["source-plan", "--shot", str(shot), "--edit", str(edit), "--calib", str(h.calib), "--binding", "b"]
    assert run_json([*plan, "--dat", str(h.dat), "--entry", str(empty)]) == run_json([*plan, "--dat", str(h.dat)])
    assert run_json([*plan, "--dat", str(empty), "--entry", str(entry)]) == run_json([*plan, "--entry", str(entry)])
    assert run_json([*plan, "--dat", str(h.dat), "--library", str(empty), "--entry", str(entry)]) == run_json(
        [*plan, "--dat", str(h.dat), "--entry", str(entry)]
    )
    code, payload = run_json([*plan, "--dat", str(empty), "--entry", str(entry)])
    assert code == 2 and payload["error"]["kind"] == "camera_data_missing", payload


def _npy_with(path: Path, shape: tuple[int, ...], payload: bytes) -> Path:
    body = f"{{'descr': '|u1', 'fortran_order': False, 'shape': {shape}, }}"
    pad = (64 - (10 + len(body) + 1) % 64) % 64
    header = body.encode("latin1") + b" " * pad + b"\n"
    path.write_bytes(b"\x93NUMPY\x01\x00" + len(header).to_bytes(2, "little") + header + payload)
    return path


@needs_dat
def test_decoded_pictures_are_checked(homes: dict[str, xs.Home], files: dict[str, Path], tmp_path: Path) -> None:
    """r1 P2-3: camera JPG and preview arrays with a bad size, channel count or length are refused."""
    h = homes["all"]
    too_big = {"kind": "value", "message": "array is too big; `arr.size * arr.dtype.itemsize` is larger than the maximum possible size."}
    cases = [
        ((1, 2, 3), bytes(3), {"kind": "bad_request", "message": "{}的像素字节数和宽高不符"}),
        ((4, 4, 3), bytes(10), {"kind": "bad_request", "message": "{}的像素字节数和宽高不符"}),
        ((4294967295, 4294967295, 3), bytes(3), too_big),
        ((65536, 65536, 3), b"", too_big),
        ((2, 2, 4), bytes(16), {"kind": "bad_request", "message": "{}要是 RGB 三通道"}),
        ((0, 4, 3), b"", {"kind": "bad_request", "message": "{}的宽高要大于 0"}),
        ((4, 0, 3), b"", {"kind": "bad_request", "message": "{}的宽高要大于 0"}),
    ]
    shot = xs.write_json(tmp_path / "shot.json", xs.shot_for(files["unreadable"], files["jpg"], lens=LENSES[0], iso=200))
    edit = xs.write_json(tmp_path / "edit.json", xs.edit_json(EditState()))
    good = xs.save_rgb(tmp_path / "good.npy", xs.decoded_jpg(files["jpg"]))
    args = ["source-color-image", "--shot", str(shot), "--edit", str(edit), "--dng", str(files["unreadable"]), "--dat", str(h.dat)]
    for i, (shape, payload, err) in enumerate(cases):
        bad = _npy_with(tmp_path / f"bad{i}.npy", shape, payload)
        for flag, what, base in (("--jpg", "相机 JPG", "camera-jpg"), ("--preview", "内嵌预览", "neutral")):
            pics = ["--jpg", str(bad)] if flag == "--jpg" else ["--jpg", str(good), "--preview", str(bad)]
            code, payload_out = run_json([*args, "--base", base, *pics, "--out", str(tmp_path / "o.npy")])
            want = {"error": {**err, "message": err["message"].format(what)}}
            assert (code, payload_out) == (2, want), (shape, flag, payload_out)
    code, ok = run_json([*args, "--base", "camera-jpg", "--jpg", str(good), "--out", str(tmp_path / "o.npy")])
    assert code == 0 and ok["path"] == "camera-jpg", ok


def _peak_rss(args: list[str]) -> tuple[int, str, int]:
    """Exit code, stdout and the peak resident set size in bytes (`/usr/bin/time -l`, macOS)."""
    import re
    import subprocess  # nosec B404

    from support.core_cli import CLI

    proc = subprocess.run(["/usr/bin/time", "-l", str(CLI), *args], capture_output=True, text=True, check=False)  # noqa: S603  # nosec B603
    found = re.search(r"(\d+)\s+maximum resident set size", proc.stderr)
    assert found, proc.stderr
    return proc.returncode, proc.stdout, int(found.group(1))


@needs_dat
@pytest.mark.skipif(not Path("/usr/bin/time").is_file(), reason="需要 /usr/bin/time -l")
def test_bad_picture_npy_is_refused_without_copying(homes: dict[str, xs.Home], files: dict[str, Path], tmp_path: Path) -> None:
    """r2 P2: a .npy whose payload does not fit its header is refused before the pixels are copied.

    The reviewer's inputs (shape (1,2,3), (4294967295,4294967295,3) and (1,2,4) with 8,388,611
    payload bytes, plus too short) fail with the fixed errors, and a 64 MiB payload adds no
    second payload-sized buffer to the process's peak memory."""
    import json

    h = homes["all"]
    shot = xs.write_json(tmp_path / "shot.json", xs.shot_for(None, files["jpg"], lens=LENSES[0], iso=200))
    edit = xs.write_json(tmp_path / "edit.json", xs.edit_json(EditState()))
    base = [
        "source-color-image",
        "--shot",
        str(shot),
        "--edit",
        str(edit),
        "--dat",
        str(h.dat),
        "--base",
        "camera-jpg",
        "--out",
        str(tmp_path / "o.npy"),
    ]
    length = {"kind": "bad_request", "message": "相机 JPG的像素字节数和宽高不符"}
    cases = [
        ((1, 2, 3), 8_388_611, length),
        ((1, 2, 3), 2, length),
        ((1, 2, 3), 7, length),
        (
            (4294967295, 4294967295, 3),
            8_388_611,
            {"kind": "value", "message": "array is too big; `arr.size * arr.dtype.itemsize` is larger than the maximum possible size."},
        ),
        ((1, 2, 4), 8_388_611, {"kind": "bad_request", "message": "相机 JPG要是 RGB 三通道"}),
        ((1, 2, 1), 2, {"kind": "bad_request", "message": "相机 JPG要是 RGB 三通道"}),
    ]
    for i, (shape, n, want) in enumerate(cases):
        bad = _npy_with(tmp_path / f"r2-{i}.npy", shape, bytes(n))
        code, payload = run_json([*base, "--jpg", str(bad)])
        assert (code, payload) == (2, {"error": want}), (shape, n, payload)
    small = _npy_with(tmp_path / "small.npy", (1, 2, 3), bytes(7))
    big_n = 64 << 20
    big = _npy_with(tmp_path / "big.npy", (1, 2, 3), bytes(big_n))
    rows = []
    for path in (small, big):
        code, out, rss = _peak_rss([*base, "--jpg", str(path)])
        assert code == 2 and json.loads(out) == {"error": length}, out
        rows.append(rss)
    extra = rows[1] - rows[0]
    print(f"peak RSS: 7-byte payload {rows[0]} B, 64 MiB payload {rows[1]} B, extra {extra} B ({extra / big_n:.2f}x the payload)")
    # Reading the file costs one payload; a copy before the check would cost a second one.
    assert extra < 1.5 * big_n, (rows, big_n)
