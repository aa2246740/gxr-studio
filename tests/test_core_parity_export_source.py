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

import time
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
@pytest.mark.skipif(len(REAL) < 20, reason="需要至少 20 张真 DNG（GXR_STUDIO_TEST_PARENT/captures）")
def test_full_size_recipe_render_real_dngs(homes: dict[str, xs.Home], tmp_path: Path) -> None:
    h = homes["all"]
    renders = 0
    t_py = t_rs = 0.0
    for i, (dng, _jpg) in enumerate(REAL):
        meta = read_dng_meta(dng)
        shot = xs.shot_for(dng, None, lens=meta.lens, iso=meta.iso)
        unit = h.render.photo_unit(shot) or "jlaunch"
        for sel, adjust, curves in _render_combos(i, unit):
            t0 = time.perf_counter()
            py, rs, info = xr.render_case(h, tmp_path, dng, shot, EditState(recipe=sel, adjust=adjust, curve=curves))
            t_py += time.perf_counter() - t0
            assert rs.dtype == np.uint8 and rs.shape == py.shape, (dng.name, py.shape, rs.shape)
            bad = np.argwhere(rs != py)
            assert len(bad) == 0, (dng.name, sel, adjust, info, len(bad), bad[:5].tolist())
            renders += 1
    t0 = time.perf_counter()
    py, rs, info = xr.render_case(
        h, tmp_path, REAL[0][0], xs.shot_for(REAL[0][0], None, lens=LENSES[0], iso=200), EditState(recipe=RecipeSel("jlaunch", 2))
    )
    t_rs = time.perf_counter() - t0
    print(f"real full-size recipe renders: {renders} on {len(REAL)} DNGs, 0 codes differ; one case (both sides) {t_rs:.2f}s, total {t_py:.1f}s")


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
        assert rs.shape == py.shape and int((rs != py).sum()) == 0, dng.name
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
def test_neutral_base_renders_stock_04_when_calibrated(
    homes: dict[str, xs.Home], files: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Python quirk kept as-is: base "neutral" plans stock 04 when it can, and labels it neutral."""
    shot = xs.shot_for(files["ok"], None, lens=LENSES[0], iso=200)
    python, rust, py_rgb, rs_rgb = xr.color_case(homes["all"], tmp_path, shot, EditState(), "neutral", monkeypatch=monkeypatch)
    xr.assert_same_result(python, rust, py_rgb, rs_rgb, "neutral-quirk")
    assert rust["path"] == "raw-recipe" and rust["labels"]["id"] == "neutral-preview"
    assert rust["labels"]["description"].startswith("GXR Studio computer prediction"), rust["labels"]


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
