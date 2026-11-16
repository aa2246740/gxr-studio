"""Parity: gxr-cli (geom-crop-write / geom-geometry-write) and WASM against Python edits.write_crop & render.edits.write."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any

import pytest

from gxr_studio import crop as crop_mod
from gxr_studio.cards.discovery import find_cards
from gxr_studio.cards.importer import ImportOptions, run_import
from gxr_studio.crop import CropError
from gxr_studio.finish.spec import EditError
from gxr_studio.library import edits as lib_edits
from gxr_studio.library import sidecar as sc
from gxr_studio.render import edits as render_edits
from gxr_studio.render.recipes import Menu
from support.core_cli import error_of, require_cli, run_json
from support.wasm_check import run_area


def _assert_close(a: Any, b: Any, label: str = "") -> None:
    if isinstance(a, bool) or isinstance(b, bool):
        assert type(a) is type(b) and a == b, f"{label}: {a!r} != {b!r}"
        return
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if isinstance(a, int) and isinstance(b, int):
            assert a == b, f"{label}: {a!r} != {b!r}"
            return
        fa, fb = float(a), float(b)
        if math.isnan(fa) or math.isnan(fb):
            assert math.isnan(fa) and math.isnan(fb), f"{label}: {a!r} != {b!r}"
            return
        if math.isinf(fa) or math.isinf(fb):
            assert fa == fb, f"{label}: {a!r} != {b!r}"
            return
        limit = 1e-12 * max(1.0, abs(fa))
        assert abs(fa - fb) <= limit, f"{label}: {a!r} != {b!r}"
        return
    if isinstance(a, list) and isinstance(b, list):
        assert len(a) == len(b), f"{label}: len {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            _assert_close(x, y, f"{label}[{i}]")
        return
    if isinstance(a, dict) and isinstance(b, dict):
        assert set(a) == set(b), f"{label}: keys {set(a)} != {set(b)}"
        for k in a:
            _assert_close(a[k], b[k], f"{label}.{k}")
        return
    assert a == b, f"{label}: {a!r} != {b!r}"


def _subset(side: dict[str, Any]) -> dict[str, Any]:
    return {
        "frame": side.get("frame"),
        "geometry": side.get("geometry"),
        "crop": side.get("crop"),
    }


def _describe_no_rev(side: dict[str, Any]) -> dict[str, Any]:
    out = lib_edits.describe(side)
    out.pop("rev", None)
    return out


def _cli_crop_write(tmp_path: Path, side: dict[str, Any] | str, req: dict[str, Any] | str) -> tuple[int, Any]:
    s_path = tmp_path / "side.json"
    r_path = tmp_path / "req.json"
    s_path.write_text(side if isinstance(side, str) else json.dumps(side), encoding="utf-8")
    r_path.write_text(req if isinstance(req, str) else json.dumps(req), encoding="utf-8")
    return run_json(["geom-crop-write", "--side", str(s_path), "--request", str(r_path)])


def _cli_geom_write(tmp_path: Path, side: dict[str, Any] | str, geom: dict[str, Any] | None | str) -> tuple[int, Any]:
    s_path = tmp_path / "side.json"
    g_path = tmp_path / "geom.json"
    s_path.write_text(side if isinstance(side, str) else json.dumps(side), encoding="utf-8")
    g_path.write_text(geom if isinstance(geom, str) else json.dumps(geom), encoding="utf-8")
    return run_json(["geom-geometry-write", "--side", str(s_path), "--geometry", str(g_path)])


@pytest.fixture
def temp_shot(volumes: Path, card: Path, lib: Any) -> tuple[Any, dict[Any, Any]]:
    run_import(lib, find_cards([volumes])[0], ImportOptions(min_free_bytes=0))
    shot = next(s for s in lib.list_shots(limit=100) if s["name"] == "R0000102")
    return lib, shot


def _reset_side(
    lib: Any,
    shot: dict[Any, Any],
    frame: dict[str, Any] | None,
    geometry: dict[str, Any] | None = None,
    crop: dict[str, Any] | None = None,
) -> dict[Any, Any]:
    side = lib.read_sidecar_for_write(shot)
    if frame is None:
        side.pop("frame", None)
    else:
        side["frame"] = copy.deepcopy(frame)
    if geometry is None:
        side.pop("geometry", None)
    else:
        side["geometry"] = copy.deepcopy(geometry)
    side["crop"] = copy.deepcopy(crop)
    return sc.write_atomic(lib.sidecar_path_for(shot), side, tmp_dir=lib.tmp_dir)


@pytest.mark.parametrize("w,h", [(4288, 2848), (2848, 4288)])
@pytest.mark.parametrize("orientation", ["landscape", "portrait"])
@pytest.mark.parametrize("angle", [-45.0, -3.5, 0.0, 1.5, 45.0])
def test_crop_write_presets_orientations_and_limits_match_python(
    tmp_path: Path, temp_shot: tuple[Any, dict[Any, Any]], w: int, h: int, orientation: str, angle: float
) -> None:
    require_cli()
    lib, shot = temp_shot
    for preset in crop_mod.PRESETS:
        base = _reset_side(lib, shot, {"orientation": 1, "width": w, "height": h})
        subset_in = _subset(base)
        py_err: CropError | None = None
        py_desc: dict[str, Any] | None = None
        try:
            py_desc = lib_edits.write_crop(lib, shot, preset=preset, rect=None, angle_deg=angle, orientation=orientation)
        except CropError as exc:
            py_err = exc

        req = {"preset": preset, "rect": None, "angle_deg": angle, "orientation": orientation}
        code, payload = _cli_crop_write(tmp_path, subset_in, req)
        rs_err = error_of(code, payload)

        if py_err is not None:
            assert rs_err is not None and rs_err.kind == py_err.code and rs_err.message == py_err.message
            continue
        assert rs_err is None and py_desc is not None
        py_after = lib.read_sidecar_for_write(shot)
        py_desc.pop("rev", None)
        _assert_close(
            {**_subset(py_after), "describe": py_desc},
            payload,
            f"{preset}@{w}x{h}@{orientation}@{angle}",
        )


def test_crop_write_rects_fit_reset_and_error_kinds_match_python(tmp_path: Path, temp_shot: tuple[Any, dict[Any, Any]]) -> None:
    require_cli()
    lib, shot = temp_shot

    cases: list[tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None, dict[str, Any]]] = [
        # valid inside rect
        ({"orientation": 1, "width": 4288, "height": 2848}, None, None, {"preset": "16:9", "rect": [0.1, 0.1, 0.8, 0.6808], "angle_deg": 0.0}),
        # outside canvas -> bad_rect
        ({"orientation": 1, "width": 4288, "height": 2848}, None, None, {"preset": "free", "rect": [0.5, 0.5, 0.6, 0.6], "angle_deg": 0.0}),
        # rotated corner outside -> crop_outside_image
        ({"orientation": 1, "width": 4288, "height": 2848}, None, None, {"preset": "free", "rect": [0.0, 0.0, 1.0, 1.0], "angle_deg": 5.0}),
        # too small -> too_small
        (
            {"orientation": 1, "width": 4288, "height": 2848},
            None,
            None,
            {"preset": "free", "rect": [0.1, 0.1, 0.001, 0.001], "angle_deg": 0.0},
        ),
        # ratio mismatch -> ratio_mismatch
        (
            {"orientation": 1, "width": 4288, "height": 2848},
            None,
            None,
            {"preset": "xpan-65-24", "rect": [0.0, 0.0, 1.0, 1.0], "angle_deg": 0.0},
        ),
        # fit=True on mismatched ratio & rotated crop
        (
            {"orientation": 1, "width": 4288, "height": 2848},
            None,
            None,
            {"preset": "xpan-65-24", "rect": [0.0, 0.0, 1.0, 1.0], "angle_deg": 3.0, "fit": True},
        ),
        # fit=True on portrait target & tiny box
        (
            {"orientation": 6, "width": 2848, "height": 4288},
            None,
            None,
            {"preset": "4:3", "rect": [0.2, 0.2, 0.002, 0.002], "angle_deg": 0.0, "fit": True},
        ),
        # fit=True preserving target stored angle_deg when angle_deg is None
        (
            {"orientation": 1, "width": 4288, "height": 2848},
            {"straighten": 2.0, "vertical": 0, "horizontal": 0, "distortion": 0, "rotate90": 0, "flip": False},
            {"preset": "1:1", "rect": [0.2, 0.2, 0.4, 0.6], "angle_deg": 2.0},
            {"preset": "xpan-65-24", "rect": [0.0, 0.0, 1.0, 1.0], "fit": True},
        ),
        # unknown preset -> unknown_preset
        ({"orientation": 1, "width": 4288, "height": 2848}, None, None, {"preset": "bogus", "rect": [0.1, 0.1, 0.5, 0.5], "angle_deg": 0.0}),
        # out-of-range angle -> bad_angle
        ({"orientation": 1, "width": 4288, "height": 2848}, None, None, {"preset": "free", "rect": [0.1, 0.1, 0.5, 0.5], "angle_deg": 46.0}),
        # reset with existing geometry
        (
            {"orientation": 1, "width": 4288, "height": 2848},
            {"straighten": 3.5, "vertical": 0, "horizontal": 0, "distortion": 0, "rotate90": 0, "flip": False},
            {"preset": "xpan-65-24", "rect": [0.0, 0.2, 1.0, 0.55], "angle_deg": 3.5},
            {"reset": True},
        ),
        # reset without geometry
        (
            {"orientation": 1, "width": 4288, "height": 2848},
            None,
            {"preset": "1:1", "rect": [0.2, 0.2, 0.4, 0.6], "angle_deg": 0.0},
            {"reset": True},
        ),
    ]

    for idx, (frame, geom, crop, req) in enumerate(cases):
        base = _reset_side(lib, shot, frame, geom, crop)
        subset_in = _subset(base)
        py_err: CropError | None = None
        py_desc: dict[str, Any] | None = None
        try:
            py_desc = lib_edits.write_crop(
                lib,
                shot,
                preset=req.get("preset"),
                rect=req.get("rect"),
                angle_deg=req.get("angle_deg"),
                flip=bool(req.get("flip", False)),
                orientation=req.get("orientation"),
                reset=bool(req.get("reset", False)),
                fit=bool(req.get("fit", False)),
            )
        except CropError as exc:
            py_err = exc

        code, payload = _cli_crop_write(tmp_path, subset_in, req)
        rs_err = error_of(code, payload)

        if py_err is not None:
            assert rs_err is not None and rs_err.kind == py_err.code and rs_err.message == py_err.message, f"case {idx}: {rs_err} vs {py_err}"
            continue
        assert rs_err is None and py_desc is not None, f"case {idx}: {rs_err}"
        py_after = lib.read_sidecar_for_write(shot)
        py_desc.pop("rev", None)
        _assert_close({**_subset(py_after), "describe": py_desc}, payload, f"case-{idx}")

    # Missing frame and no readable photo file -> no_frame_size on both Python and CLI
    missing_dir = lib.root / "2026" / "missing"
    missing_dir.mkdir(parents=True, exist_ok=True)
    missing_shot = {"shot_id": "missing-size", "name": "R9999999", "files": {"dng": {"relpath": "2026/missing/R9999999.DNG"}}}
    base_missing = sc.write_atomic(
        lib.sidecar_path_for(missing_shot),
        sc.new_sidecar(
            dng="2026/missing/R9999999.DNG",
            jpg=None,
            dng_sha256=None,
            jpg_sha256=None,
            orientation=1,
            width=0,
            height=0,
            imported={},
        ),
        tmp_dir=lib.tmp_dir,
    )
    py_missing_err: CropError | None = None
    try:
        lib_edits.write_crop(lib, missing_shot, preset="1:1", rect=None)
    except CropError as exc:
        py_missing_err = exc
    code, payload = _cli_crop_write(tmp_path, _subset(base_missing), {"preset": "1:1"})
    rs_missing_err = error_of(code, payload)
    assert py_missing_err is not None and rs_missing_err is not None
    assert rs_missing_err.kind == py_missing_err.code and rs_missing_err.message == py_missing_err.message


def test_geometry_write_refit_round6_and_clear_match_python(tmp_path: Path, temp_shot: tuple[Any, dict[Any, Any]]) -> None:
    require_cli()
    lib, shot = temp_shot
    menu = Menu()

    # Run a realistic sequence matching render/edits.write's geometry branch
    steps: list[tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]] = [
        # 1. Only straighten on uncropped photo -> creates "free" crop with 6-decimal rect
        (None, None, {"straighten": 3.5}),
        # 2. Straighten with existing 16:9 crop -> refits rect & rounds to 6 decimals
        (None, {"preset": "16:9", "rect": [0.0, 0.1, 1.0, 0.84], "angle_deg": 0.0}, {"straighten": 4.25}),
        # 3. Geometry change that resizes canvas (vertical 0 -> 45) -> force refit
        (
            {"straighten": 1.0, "vertical": 0, "horizontal": 0, "distortion": 0, "rotate90": 0, "flip": False},
            {"preset": "xpan-65-24", "rect": [0.0, 0.222086, 1.0, 0.555829], "angle_deg": 1.0},
            {"straighten": 1.0, "vertical": 45},
        ),
        # 4. Geometry shrink back (vertical 45 -> 0) -> force refit again
        (
            {"straighten": 1.0, "vertical": 45, "horizontal": 0, "distortion": 0, "rotate90": 0, "flip": False},
            {"preset": "xpan-65-24", "rect": [0.05, 0.25, 0.9, 0.5], "angle_deg": 1.0},
            {"straighten": 0.0, "vertical": 0},
        ),
        # 5. Clear geometry (null) while crop is active -> geometry becomes null, crop.angle_deg becomes 0.0
        (
            {"straighten": 2.0, "vertical": 0, "horizontal": 0, "distortion": 0, "rotate90": 0, "flip": False},
            {"preset": "1:1", "rect": [0.2, 0.1, 0.5, 0.752809], "angle_deg": 2.0},
            None,
        ),
        # 6. Error case: straighten out of bounds -> bad_geometry
        (None, None, {"straighten": 45.1}),
    ]

    for idx, (init_geom, init_crop, req_geom) in enumerate(steps):
        base = _reset_side(lib, shot, {"orientation": 1, "width": 4288, "height": 2848}, init_geom, init_crop)
        subset_in = _subset(base)
        py_err: EditError | None = None
        py_side: dict[str, Any] | None = None
        try:
            _, py_side = render_edits.write(lib, shot, {"geometry": req_geom}, menu)
        except EditError as exc:
            py_err = exc

        code, payload = _cli_geom_write(tmp_path, subset_in, req_geom)
        rs_err = error_of(code, payload)

        if py_err is not None:
            assert rs_err is not None and rs_err.kind == py_err.code and rs_err.message == py_err.message, f"geom-step-{idx}"
            continue
        assert rs_err is None and py_side is not None, f"geom-step-{idx}: {rs_err}"
        _assert_close(
            {**_subset(py_side), "describe": _describe_no_rev(py_side)},
            payload,
            f"geom-step-{idx}",
        )


def test_recovered_frame_matches_python(tmp_path: Path, temp_shot: tuple[Any, dict[Any, Any]]) -> None:
    require_cli()
    lib, shot = temp_shot
    # Legacy frame with border style in `frame` + caller photo size (matching real shot's frame)
    base = _reset_side(lib, shot, {"style": "leica", "color": "white"}, None, None)
    py_recovered = lib_edits.recovered_frame(lib, shot, base)
    # Write with photo size matching the synthetic shot's JPG/DNG size
    photo_size = {
        "width": py_recovered["frame"]["width"],
        "height": py_recovered["frame"]["height"],
        "orientation": py_recovered["frame"]["orientation"],
    }
    py_desc = lib_edits.write_crop(lib, shot, preset="1:1", rect=None, angle_deg=0.0)
    py_after = lib.read_sidecar_for_write(shot)
    py_desc.pop("rev", None)

    subset_in = {**_subset(base), "photo": photo_size}
    code, payload = _cli_crop_write(tmp_path, subset_in, {"preset": "1:1"})
    assert code == 0, payload
    _assert_close({**_subset(py_after), "describe": py_desc}, payload, "recovered-frame")


def test_wasm_matches_native_for_crop_edit(tmp_path: Path) -> None:
    specs: list[Path] = []

    def add_crop(name: str, side: dict[str, Any] | str, req: dict[str, Any] | str) -> None:
        sf = tmp_path / f"{name}-side.json"
        rf = tmp_path / f"{name}-req.json"
        sp = tmp_path / f"{name}.json"
        sf.write_text(side if isinstance(side, str) else json.dumps(side), encoding="utf-8")
        rf.write_text(req if isinstance(req, str) else json.dumps(req), encoding="utf-8")
        sp.write_text(json.dumps({"cropWrite": {"sideFile": str(sf), "requestFile": str(rf)}}), encoding="utf-8")
        specs.append(sp)

    def add_geom(name: str, side: dict[str, Any] | str, geom: dict[str, Any] | None | str) -> None:
        sf = tmp_path / f"{name}-side.json"
        gf = tmp_path / f"{name}-geom.json"
        sp = tmp_path / f"{name}.json"
        sf.write_text(side if isinstance(side, str) else json.dumps(side), encoding="utf-8")
        gf.write_text(geom if isinstance(geom, str) else json.dumps(geom), encoding="utf-8")
        sp.write_text(json.dumps({"geometryWrite": {"sideFile": str(sf), "geometryFile": str(gf)}}), encoding="utf-8")
        specs.append(sp)

    std_side = {"frame": {"orientation": 1, "width": 4288, "height": 2848}, "geometry": None, "crop": None}
    add_crop("xpan", std_side, {"preset": "xpan-65-24", "angle_deg": 1.5})
    add_crop("xpan-portrait", std_side, {"preset": "xpan-65-24", "orientation": "portrait"})
    add_crop("fit-mismatch", std_side, {"preset": "xpan-65-24", "rect": [0.0, 0.0, 1.0, 1.0], "fit": True})
    add_crop(
        "reset",
        {
            "frame": {"orientation": 1, "width": 4288, "height": 2848},
            "geometry": {"straighten": 2.0},
            "crop": {"preset": "1:1", "rect": [0.2, 0.2, 0.4, 0.6], "angle_deg": 2.0},
        },
        {"reset": True},
    )
    add_crop(
        "recovered",
        {
            "frame": {"style": "leica"},
            "geometry": None,
            "crop": None,
            "photo": {"width": 4288, "height": 2848, "orientation": 6},
        },
        {"preset": "4:3", "orientation": "portrait"},
    )
    add_crop("err-no-frame", {"frame": None, "geometry": None, "crop": None}, {"preset": "1:1"})
    add_crop("err-bad-angle-nan", std_side, '{"preset":"free","angle_deg":NaN}')
    add_crop("err-bad-angle-lim", std_side, {"preset": "free", "angle_deg": 45.1})
    add_crop("err-unknown-preset", std_side, {"preset": "nope"})
    add_crop("err-too-small", std_side, {"preset": "free", "rect": [0.1, 0.1, 0.001, 0.001]})
    add_crop("err-ratio", std_side, {"preset": "1:1", "rect": [0.0, 0.0, 1.0, 1.0]})
    add_crop("err-outside", std_side, {"preset": "free", "rect": [0.0, 0.0, 1.0, 1.0], "angle_deg": 5.0})
    add_crop("err-depth-129", std_side, ("[" * 129) + "0" + ("]" * 129))
    add_crop("depth-128", std_side, '{"preset":"1:1","extra":' + ("[" * 127) + "0" + ("]" * 127) + "}")

    add_geom("geom-straighten", std_side, {"straighten": 3.25})
    add_geom(
        "geom-clear",
        {
            "frame": {"orientation": 1, "width": 4288, "height": 2848},
            "geometry": {"straighten": 3.25},
            "crop": {"preset": "16:9", "rect": [0.1, 0.1, 0.7, 0.59], "angle_deg": 3.25},
        },
        None,
    )
    add_geom("geom-no-frame", {"frame": None, "geometry": None, "crop": None}, {"straighten": 2.0})
    add_geom("geom-err-nan", std_side, '{"straighten":NaN}')
    add_geom("geom-err-depth-129", std_side, ('{"a":' * 129) + "0" + ("}" * 129))

    proc = run_area("geometry", specs, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
