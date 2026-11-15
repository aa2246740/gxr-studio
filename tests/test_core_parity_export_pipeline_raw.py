"""Parity: the RAW and LUT export branches, end to end, against desktop ``export_finish``.

Real DNG+JPG pairs (``support.samples``), the parent project's DAT (sha256-checked by
``e0_support``) and a **synthetic** calibration pack (``export_source_cases.make_home``). Nothing
here prints calibration, DAT or EXIF contents.

Each case runs the desktop ``export_finish`` (spying on ``finish_array`` for the base picture and
on ``_stage_rgb`` for the finished array and labels) and Rust's ``export-run-prepare`` (which
runs export-source's ``color_image`` itself) then ``export-run-complete``. Gates:
- labels exact, in order; frame and stamp layout JSON exact;
- RAW branches: Rust's canvas equals Python's finished array (no ink) or the background+photo
  oracle (ink), and the file equals ``export_finish``'s byte for byte (12 ICC timestamp bytes
  pinned);
- LUT branches: the base picture is gated by export-source's Gate A/B (``export_source_lut``);
  a base difference is attributed point by point, and Python's own array fed to complete must
  still give the desktop file byte for byte.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from gxr_studio import export as export_mod
from gxr_studio.export import srgb_icc
from gxr_studio.finish import serve as serve_mod
from gxr_studio.finish.compose import finish_array as real_finish_array
from gxr_studio.finish.compose import resolved_frame, resolved_stamp
from gxr_studio.finish.fonts import FontMetrics, PilFonts
from gxr_studio.finish.frame import draw_frame
from gxr_studio.finish.layout import FrameLayout, frame_layout, stamp_layout
from gxr_studio.finish.serve import export_finish, load_defaults
from gxr_studio.finish.spec import FrameSpec, StampSpec
from gxr_studio.finish.stamp import format_stamp
from gxr_studio.meta import read_dng_meta
from gxr_studio.render.params import RecipeSel, parse_edit
from support import export_source_cases as xs
from support import export_source_run as xr
from support.core_cli import require_cli, run_json
from support.export_pipeline_cases import check, cli_ok, mtime_args, same_file_redacted, same_pixels, same_redacted
from support.frame_cases import FIXTURE_CJK, cjk_fonts, desktop_mark, font_args
from support.samples import missing

DAT = xs.real_dat()


@pytest.fixture(autouse=True)
def _cli() -> None:
    require_cli()


@pytest.fixture
def pinned_icc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> bytes:
    np.save(tmp_path / "icc.npy", np.zeros((8, 8, 3), dtype=np.uint8))
    code, payload = run_json(["jpeg-encode", "--in", str(tmp_path / "icc.npy"), "--quality", "95", "--out", str(tmp_path / "icc.jpg")])
    cli_ok(code, payload, "jpeg-encode for the ICC pin")
    with Image.open(tmp_path / "icc.jpg") as im:
        rs = bytes(im.info["icc_profile"])
    py = srgb_icc()
    check(len(rs) == len(py) == 588 and rs[:24] == py[:24] and rs[36:] == py[36:], "ICC profiles differ outside bytes 24..36")
    monkeypatch.setattr(export_mod, "_ICC", rs)
    return rs


def _w(tmp: Path, name: str, obj: Any) -> str:
    path = tmp / name
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return str(path)


GALLERY = FrameSpec(style="gallery", output_ratio="original").to_json()
INSTANT = FrameSpec(style="instant", fmt="instax_mini").to_json()
STAMP_ON = StampSpec(enabled=True, date_override="2026-10-31").to_json()
STAMP_BORDER = StampSpec(enabled=True, placement="border", date_override="2026-11-15").to_json()

# (sidecar, base, keep_jpg, quality, long_edge)
RAW_CASES: list[tuple[dict[str, Any], str, bool, int, int | None]] = [
    ({"recipe": {"unit": "jlaunch", "slot": 7}, "adjust": {"exposure_ev": 0.25, "contrast": 10}}, "auto", True, 95, None),
    ({"adjust": {"saturation": 15}, "crop": {"rect": [0.05, 0.05, 0.9, 0.9], "preset": "free"}}, "neutral", True, 85, 1600),
    ({"recipe": {"unit": "jlaunch", "slot": 2}, "border": GALLERY, "stamp": STAMP_ON}, "auto", True, 70, None),
    ({"adjust": {"contrast": -8, "wb": {"warm": 10}}}, "standard", True, 95, 2048),
    (
        {"recipe": {"unit": "jlaunch", "slot": 5}, "border": INSTANT, "stamp": STAMP_BORDER, "geometry": {"straighten": 1.0}},
        "auto",
        True,
        85,
        None,
    ),
    ({"adjust": {"exposure_ev": -0.3}}, "auto", False, 95, None),
    ({"recipe": {"unit": "jlaunch", "slot": 4}, "curve": {"master": [[0, 0], [128, 140], [255, 255]]}}, "recipe", False, 70, 1600),
]


class Spy:
    """Captures ``finish_array``'s base picture and ``_stage_rgb``'s result inside ``export_finish``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.base: tuple[np.ndarray, bool, dict[str, Any]] | None = None
        self.stage: tuple[np.ndarray, dict[str, str]] | None = None
        real_stage = serve_mod._stage_rgb

        def finish_array(rgb: np.ndarray, edit: Any, **kw: Any) -> Any:
            self.base = (np.array(rgb), bool(kw["from_raw"]), kw)
            return real_finish_array(rgb, edit, **kw)

        def stage(*a: Any, **kw: Any) -> Any:
            out = real_stage(*a, **kw)
            self.stage = (np.array(out[0]), dict(out[1]))
            return out

        monkeypatch.setattr(serve_mod, "finish_array", finish_array)
        monkeypatch.setattr(serve_mod, "_stage_rgb", stage)


def _photo_only(photo: np.ndarray, layout: FrameLayout) -> np.ndarray:
    bare = replace(layout, items=tuple(it for it in layout.items if it.get("k") == "photo"))
    return np.asarray(draw_frame(Image.fromarray(photo, "RGB"), bare, PilFonts()))


def _entry_for(h: xs.Home, tmp: Path, shot: dict[str, Any], side: dict[str, Any], base: str) -> list[str]:
    """The entry the resolve unit gives for the selection ``color_image`` will plan."""
    edit_path = xs.write_json(tmp / "sel-edit.json", xs.edit_json(parse_edit(side)))
    picked = json.loads(xr._selection(shot, edit_path, base, tmp))["selection"]
    if picked is None:
        return []
    edit = parse_edit(side)
    sel = RecipeSel(picked["unit"], picked["slot"], kind=picked["kind"])
    if edit.recipe is not None and (edit.recipe.unit, edit.recipe.slot) == (sel.unit, sel.slot):
        sel = edit.recipe
    entry = h.entry(sel)
    if entry is None:
        return []
    path = tmp / "entry.bin"
    path.write_bytes(entry)
    return ["--entry", str(path)]


def run_case(
    h: xs.Home, tmp: Path, monkeypatch: pytest.MonkeyPatch, stem: str, dng: Path, jpg: Path | None, case: tuple[Any, ...], *, lut: Any = None
) -> dict[str, Any]:
    side, base, _keep, quality, long_edge = case
    meta0 = read_dng_meta(dng)
    shot = xs.shot_for(dng, jpg, lens=meta0.lens, iso=meta0.iso)
    with Image.open(jpg if jpg is not None else dng) as im:
        w0, h0 = im.size
    shot.update({"name": stem, "shot_id": stem, "width": w0, "height": h0})
    edit = parse_edit(side)
    dest = tmp / f"{stem}-out"
    with monkeypatch.context() as mp:
        spy = Spy(mp)
        mp.setattr(serve_mod, "_embedded_rgb", lambda _p: None)
        mp.setattr(xr.service.chain, "bake_chroma", xr._cached_bake)
        if lut is not None:
            mp.setattr(h.render, "luts", lut)
        res = export_finish(h.render, h.lib, shot, edit, side, dest, [], quality=quality, long_edge=long_edge, fmt="jpeg", base=base)
        meta, taken, file_time = serve_mod._meta(shot, h.lib)
    py_file = res.path.read_bytes()
    check(spy.stage is not None, f"{stem}: _stage_rgb not reached")
    py_rgb, py_labels = spy.stage
    # meta, taken and file_time come from export-meta in Rust (the shot's own files).
    layout = {"mark": desktop_mark()}
    source = jpg if jpg is not None else dng
    args = [
        "--shot", _w(tmp, f"{stem}-shot.json", shot), "--side", _w(tmp, f"{stem}-side.json", side), "--base", base,
        "--layout", _w(tmp, f"{stem}-layout.json", layout),
        "--quality", str(quality), *(["--long-edge", str(long_edge)] if long_edge else []),
        "--dat", str(h.dat), "--calib", str(h.calib), "--binding", h.binding(), "--dng", str(dng), *mtime_args(source),
        *_entry_for(h, tmp, shot, side, base),
    ]  # fmt: skip
    if jpg is not None:
        np.save(tmp / f"{stem}-jpg.npy", xs.decoded_jpg(jpg))
        args += ["--jpg", str(tmp / f"{stem}-jpg.npy"), "--jpg-bytes", str(jpg)]
    if lut is not None:
        args += ["--cube", str(lut.cube), "--space", str(lut.spec_path)]
    out = tmp / f"{stem}-canvas.npy"
    code, payload = run_json(["export-run-prepare", *args, *font_args(), "--out", str(out)], timeout=600)
    cli_ok(code, payload, f"{stem} prepare")
    canvas = np.load(out)
    prepared = payload["prepared"]
    same_redacted(list(prepared["labels"].items()), list(py_labels.items()), f"{stem} labels")

    frame_def, stamp_def = load_defaults(h.render.home)
    frame = resolved_frame(edit.frame, frame_def)
    stamp = resolved_stamp(edit.stamp, stamp_def)
    check(spy.base is not None, f"{stem}: finish_array not reached")
    rgb0, from_raw, kw = spy.base
    rs_base = _rust_base(tmp, stem, args, side, base) if lut is not None else rgb0
    photo, _hdr = real_finish_array(rs_base, edit, **{**kw, "frame": FrameSpec(style="none"), "stamp": replace(stamp, enabled=False)})
    ph, pw = photo.shape[:2]
    m = FontMetrics(PilFonts())
    py_frame = frame_layout(pw, ph, frame, meta, m)
    border = frame.style == "instant" and stamp.enabled and stamp.placement == "border"
    text = format_stamp(stamp, taken, file_time)
    py_stamp = stamp_layout(pw, ph, stamp, text, m, py_frame if border else None) if stamp.enabled and text else None
    same_redacted(prepared["frame"], None if py_frame is None else json.loads(json.dumps(py_frame.to_json())), f"{stem} frame layout")
    same_redacted(prepared["stamp"], None if py_stamp is None else json.loads(json.dumps(py_stamp.to_json())), f"{stem} stamp layout")
    want_canvas = photo if py_frame is None else _photo_only(photo, py_frame)
    no_ink = py_frame is None and py_stamp is None
    same_pixels(canvas, want_canvas, f"{stem} canvas")
    base_diff = int(np.count_nonzero(rs_base != rgb0)) if rs_base.shape == rgb0.shape else -1
    if lut is None:
        same_redacted(base_diff, 0, f"{stem} base picture differences")
    fed = canvas if (no_ink and base_diff == 0) else py_rgb
    same_redacted(prepared["state"]["source"], "jpg" if jpg is not None else "dng", f"{stem} source")
    s, c, o = (tmp / f"{stem}.{x}" for x in ("state.json", "fed.npy", "jpg"))
    s.write_text(json.dumps(prepared["state"], ensure_ascii=False), encoding="utf-8")
    np.save(c, fed)
    code, done = run_json(["export-run-complete", "--canvas", str(c), "--state", str(s), "--src", str(source), "--out", str(o)])
    cli_ok(code, done, f"{stem} complete")
    got = o.read_bytes()
    same_file_redacted(got, py_file, stem)
    # The source EXIF really travelled (tag ids counted, values never printed).
    with Image.open(o) as im:
        exif = im.getexif()
        check(271 in exif and len(exif.get_ifd(0x8769)) >= 4, f"{stem}: Make or EXIF sub-IFD missing ({len(exif)} root tags)")
    same_redacted(done["result"]["file_name"], res.path.name, f"{stem} file name")
    for key in ("width", "height", "render", "render_label", "render_kind", "calibrated", "detail", "hdr", "hdr_reason"):
        same_redacted(done["result"][key], getattr(res, key), f"{stem} result.{key}")
    return {"no_ink": no_ink, "base_diff": base_diff, "rgb0": rgb0, "rs_base": rs_base, "shot": shot, "render": res.render}


@pytest.mark.skipif(DAT is None, reason="没有 g127 DAT")
def test_raw_branches_real_pairs_match_export_finish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned_icc: bytes) -> None:
    pairs = [(d, j) for d, j in xs.real_pairs(60) if j is not None]
    if len(pairs) < 20:
        missing("captures/ 里不足 20 对 RAW+JPG（export-pipeline）")
    pairs = [pairs[(i * len(pairs)) // 20] for i in range(20)]
    h = xs.make_home(tmp_path / "home", DAT)
    tally: dict[str, int] = {}
    with cjk_fonts((FIXTURE_CJK,)):
        for idx, (dng, jpg) in enumerate(pairs):
            case = RAW_CASES[idx % len(RAW_CASES)]
            keep_jpg = case[2]
            out = run_case(h, tmp_path, monkeypatch, f"W{idx:03d}", dng, jpg if keep_jpg else None, case)
            key = f"{out['render']}/{'no-ink' if out['no_ink'] else 'ink'}/{'pair' if keep_jpg else 'dng-only'}"
            tally[key] = tally.get(key, 0) + 1
    print("export-pipeline e2e raw:", json.dumps(tally, ensure_ascii=False, sort_keys=True))
    check(sum(tally.values()) == 20, "expected 20 exports")
    check(any(k.startswith("recipe-prediction") for k in tally) and any(k.startswith("neutral-preview") for k in tally), "branches missing")


GRADE = (np.array([1.05, 0.96, 0.91]), np.array([0.02, -0.01, 0.03]))
SPECS = {
    "disp": {
        "input_space": "srgb",
        "output_space": "srgb",
        "input_gamut": None,
        "output_gamut": None,
        "input_range": "full",
        "output_range": "full",
    },
    "log": {
        "input_space": "slog3",
        "output_space": "srgb",
        "input_gamut": None,
        "output_gamut": None,
        "input_range": "full",
        "output_range": "full",
    },
}


@pytest.mark.skipif(DAT is None, reason="没有 g127 DAT")
def test_lut_branches_real_pairs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned_icc: bytes) -> None:
    from support import export_source_lut as gates
    from support.lut_cases import cube_text

    pairs = [(d, j) for d, j in xs.real_pairs(20) if j is not None][:3]
    if not pairs:
        missing("captures/ 真实样本不在本机（export-pipeline）")
    h = xs.make_home(tmp_path / "home", DAT)
    # Sidecar LUT ids are 64 hex digits (`parse_lut`).
    ids = {k: hashlib.sha256(k.encode()).hexdigest() for k in SPECS}
    luts = {
        k: xr.Lut(tmp_path, cube_text(lambda x: np.clip(x * GRADE[0] + GRADE[1], 0, 1), size=17), spec, ids[k]) for k, spec in SPECS.items()
    }
    cases: list[tuple[str, tuple[Any, ...]]] = [
        ("disp", ({"lut": None}, "camera-jpg", True, 95, None)),
        ("disp", ({"lut": None, "recipe": {"unit": "jlaunch", "slot": 2}}, "auto", True, 85, 1600)),
        ("log", ({"lut": None, "adjust": {"exposure_ev": 0.2}, "border": GALLERY}, "auto", True, 70, None)),
    ]
    points = {"a": 0, "b": 0, "cases": 0, "base_diff": 0}
    with cjk_fonts((FIXTURE_CJK,)):
        for idx, (dng, jpg) in enumerate(pairs):
            for j, (lut_id, case) in enumerate(cases):
                lut = luts[lut_id]
                side = {**case[0], "lut": lut.id}
                out = run_case(h, tmp_path, monkeypatch, f"L{idx}{j}", dng, jpg, (side, *case[1:]), lut=lut)
                points["cases"] += 1
                if out["base_diff"]:
                    points["base_diff"] += out["base_diff"]
                label = f"L{idx}{j}"
                if lut_id == "disp":
                    # The picture the display LUT is applied to: color_image without the LUT.
                    plain = parse_edit({k: v for k, v in side.items() if k != "lut"})
                    b_py, _f, _l = serve_mod.color_image(
                        h.render, out["shot"], plain, max_edge=None, quality=95, lib=h.lib, base=case[1], full=True
                    )
                    n_a, n_b = gates.display_gate(tmp_path, lut.cube, lut.spec, np.asarray(b_py), out["rgb0"], out["rs_base"], label)
                else:
                    n_a, n_b = gates.log_gate(tmp_path, lut.cube, lut.spec, dng, out["rgb0"], out["rs_base"], label, sample=4000)
                points["a"] += n_a
                points["b"] += n_b
    print("export-pipeline e2e lut:", points)
    check(points["cases"] == 3 * len(pairs), "LUT case count")


def _rust_base(tmp: Path, stem: str, prepare_args: list[str], side: dict[str, Any], base: str) -> np.ndarray:
    """Rust's base picture for the same inputs: export-source's ``source-color-image``."""
    keep = {"--dat", "--calib", "--binding", "--dng", "--jpg", "--entry", "--cube", "--space"}
    args = ["source-color-image", "--shot", prepare_args[prepare_args.index("--shot") + 1]]
    args += ["--edit", str(xs.write_json(tmp / f"{stem}-srcedit.json", xs.edit_json(parse_edit(side)))), "--base", base]
    for i, a in enumerate(prepare_args):
        if a in keep:
            args += [a, prepare_args[i + 1]]
    out = tmp / f"{stem}-rsbase.npy"
    code, payload = run_json([*args, "--out", str(out)], timeout=600)
    cli_ok(code, payload, f"{stem} source-color-image")
    return np.load(out)
