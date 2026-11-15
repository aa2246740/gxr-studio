"""Parity: the HDR export, end to end, against desktop ``export_finish(hdr=True)``.

Real DNG+JPG pairs (``support.samples``), the parent project's DAT and a **synthetic**
calibration pack. Rust runs ``export-run-prepare --hdr`` (hdr-track ``begin`` and the photo's
warp / box / crop) and ``export-run-complete`` (hdr-track ``stamp`` with the stamp mask, ``frame``
from the layout, ``finish`` on this unit's single encode). Gates:
- the track layer Rust's prepare returns equals Python's just before ``track.stamp`` (float32
  bits, NaN at the same places);
- no ink: the file equals the desktop's byte for byte, or the difference is attributed with
  hdr-track's own rules (Gate A: Rust equals the sequential-IEEE oracle built from Python's
  layer; Gate B: Python equals the FMA oracle);
- ink: Python's inked array and Python's stamp mask go into complete; same rule;
- ``hdr``, ``hdr_reason`` and the ``_hdr`` file-name tag equal the desktop's.
Nothing here prints calibration, DAT or EXIF contents.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from gxr_studio.finish import hdr_export
from gxr_studio.finish import serve as serve_mod
from gxr_studio.finish.compose import finish_array as real_finish_array
from gxr_studio.finish.compose import resolved_frame, resolved_stamp
from gxr_studio.finish.hdr_export import HdrTrack
from gxr_studio.finish.serve import export_finish, load_defaults
from gxr_studio.finish.spec import FrameSpec, StampSpec
from gxr_studio.meta import read_dng_meta
from gxr_studio.render.params import parse_edit
from support import export_source_cases as xs
from support import export_source_run as xr
from support import hdr_parity as hp
from support.core_cli import require_cli, run_json
from support.export_pipeline_cases import (
    check,
    cli_ok,
    first_difference,
    mtime_args,
    note,
    same_file_redacted,
    same_pixels,
    same_redacted,
)
from support.frame_cases import FIXTURE_CJK, cjk_fonts, desktop_mark, font_args
from support.samples import missing
from test_core_parity_export_pipeline_raw import Spy, _entry_for, _photo_only, _w, pinned_icc  # noqa: F401
from test_core_parity_hdr_track_finish import _encode_gain_jpeg, _extract_gain_jpeg, _oracle_to_grid, _same_f32_nan, _stamp_changed_mask

DAT = xs.real_dat()

GALLERY = FrameSpec(style="gallery", output_ratio="original").to_json()
INSTANT = FrameSpec(style="instant", fmt="instax_mini").to_json()
LEICA = FrameSpec(style="leica", show_logo=True).to_json()
BLUR = FrameSpec(style="blur_fill").to_json()
STAMP_ON = StampSpec(enabled=True, date_override="2026-10-31").to_json()
STAMP_BORDER = StampSpec(enabled=True, placement="border", date_override="2026-11-15").to_json()
STAMP_OFF = StampSpec(enabled=False).to_json()

# (sidecar, base, keep_jpg, quality)
CASES: list[tuple[dict[str, Any], str, bool, int]] = [
    ({}, "auto", True, 95),
    ({"recipe": {"unit": "jlaunch", "slot": 7}, "adjust": {"exposure_ev": 0.25, "contrast": 10}}, "auto", True, 92),
    ({"adjust": {"saturation": 15}, "crop": {"rect": [0.05, 0.05, 0.9, 0.9], "preset": "free"}}, "neutral", True, 85),
    ({"geometry": {"straighten": 2.0}, "crop": {"rect": [0.0, 0.0, 1.0, 1.0], "preset": "3:2"}, "stamp": STAMP_OFF}, "auto", True, 95),
    ({"recipe": {"unit": "jlaunch", "slot": 2}, "border": GALLERY, "stamp": STAMP_ON}, "auto", True, 70),
    ({"adjust": {"exposure_ev": -0.2}, "border": INSTANT, "stamp": STAMP_BORDER}, "auto", True, 85),
    ({"geometry": {"rotate90": 1, "flip": True}, "crop": {"rect": [0.1, 0.1, 0.8, 0.8]}, "stamp": STAMP_ON}, "auto", True, 95),
    ({"border": BLUR, "stamp": STAMP_OFF}, "auto", True, 92),
    ({"adjust": {"contrast": 6}, "stamp": STAMP_OFF}, "auto", False, 95),
    ({"border": LEICA, "stamp": STAMP_OFF, "geometry": {"vertical": 15}}, "auto", True, 85),
]


@pytest.fixture(autouse=True)
def _cli() -> None:
    require_cli()


class HdrSpy:
    """Python's track layer when ``track.stamp`` is entered, and ``finish``'s inputs and result."""

    def __init__(self, mp: pytest.MonkeyPatch) -> None:
        self.pre_stamp: np.ndarray | None = None
        self.stamp_called = False
        self.finish: dict[str, Any] = {}
        real_stamp = HdrTrack.stamp
        real_finish = hdr_export.finish

        def stamp(track: HdrTrack, spec: Any, taken: Any, file_time: Any) -> None:
            self.stamp_called = True
            self.pre_stamp = None if track.layer is None else track.layer.copy()
            real_stamp(track, spec, taken, file_time)

        def finish(payload: bytes, track: HdrTrack, fmt: str, rgb: np.ndarray) -> Any:
            out, info = real_finish(payload, track, fmt, rgb)
            self.finish = {
                "payload": payload,
                "rgb": np.array(rgb),
                "layer": None if track.layer is None else track.layer.copy(),
                "info": info,
                "problem": track.problem,
            }
            return out, info

        mp.setattr(HdrTrack, "stamp", stamp)
        mp.setattr(serve_mod, "finish_hdr", finish)


def _attribute(rs_file: bytes, py_file: bytes, fin: dict[str, Any], label: str) -> str:
    """hdr-track's Gate A/B on a byte difference; returns what explained it."""
    from gxr_studio.hdr import container as hc
    from gxr_studio.hdr import gain as hg

    check(fin["layer"] is not None, "no layer at finish")
    pre_rgb = fin["rgb"]
    size = (pre_rgb.shape[1], pre_rgb.shape[0])
    or_ry, or_rc = _oracle_to_grid(fin["layer"], size, 4, use_fma=False)
    or_res = hg.compute_gain(hg.block_mean(hp._ORACLE[0][pre_rgb] @ hg.LUMA, 4), or_ry, or_rc, hg.Params())
    or_gain = _encode_gain_jpeg(hp.codes_of(or_res), 90)
    same_file_redacted(_extract_gain_jpeg(rs_file), or_gain, f"{label}: gain map vs the sequential oracle")
    same_file_redacted(rs_file, hc.assemble(fin["payload"], or_gain, or_res.meta), f"{label}: Gate A")
    fma_ry, fma_rc = _oracle_to_grid(fin["layer"], size, 4, use_fma=True)
    fma_res = hg.compute_gain(hg.sdr_luma_grid(pre_rgb, 4), fma_ry, fma_rc, hg.Params())
    same_file_redacted(py_file, hc.assemble(fin["payload"], _encode_gain_jpeg(hp.codes_of(fma_res), 90), fma_res.meta), f"{label}: Gate B")
    return "gate-ab"


def run_hdr_case(
    h: xs.Home,
    tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    stem: str,
    dng: Path | None,
    jpg: Path | None,
    case: tuple[Any, ...],
    long_edge: int | None,
    *,
    jpg_file: Path | None = None,
) -> dict[str, Any]:
    """One export with HDR on both sides. ``jpg_file`` overrides the camera JPG the shot points at
    (crafted reason cases); ``dng=None`` gives a JPG-only shot."""
    side, base, _keep, quality = case
    cam = jpg_file if jpg_file is not None else jpg
    try:
        meta0 = read_dng_meta(dng) if dng is not None else None
    except Exception:  # noqa: BLE001  a DNG neither side can open has no lens or ISO
        meta0 = None
    shot = xs.shot_for(dng, cam, lens=meta0.lens if meta0 else None, iso=meta0.iso if meta0 else None)
    with Image.open(cam if cam is not None else dng) as im:
        w0, h0 = im.size
    shot.update({"name": stem, "shot_id": stem, "width": w0, "height": h0})
    edit = parse_edit(side)
    with monkeypatch.context() as mp:
        spy = Spy(mp)
        hspy = HdrSpy(mp)
        mp.setattr(serve_mod, "_embedded_rgb", lambda _p: None)
        mp.setattr(xr.service.chain, "bake_chroma", xr._cached_bake)
        res = export_finish(
            h.render, h.lib, shot, edit, side, tmp / f"{stem}-out", [], quality=quality, long_edge=long_edge, fmt="jpeg", base=base, hdr=True
        )
        meta, taken, file_time = serve_mod._meta(shot, h.lib)
    py_file = res.path.read_bytes()
    check(spy.stage is not None, "_stage_rgb not reached")
    py_rgb, py_labels = spy.stage
    source = cam if cam is not None else dng
    check(source is not None, "no source file")
    args = [
        "--shot", _w(tmp, f"{stem}-shot.json", shot), "--side", _w(tmp, f"{stem}-side.json", side), "--base", base,
        "--layout", _w(tmp, f"{stem}-layout.json", {"mark": desktop_mark()}), "--quality", str(quality),
        *(["--long-edge", str(long_edge)] if long_edge else []), "--hdr", "--hdr-out", str(tmp / f"{stem}-layer.npy"),
        "--dat", str(h.dat), "--calib", str(h.calib), "--binding", h.binding(), *mtime_args(source),
    ]  # fmt: skip
    if dng is not None:
        args += ["--dng", str(dng), *_entry_for(h, tmp, shot, side, base)]
    if cam is not None:
        np.save(tmp / f"{stem}-jpg.npy", xs.decoded_jpg(cam))
        args += ["--jpg", str(tmp / f"{stem}-jpg.npy"), "--jpg-bytes", str(cam)]
    code, payload = run_json(["export-run-prepare", *args, *font_args(), "--out", str(tmp / f"{stem}-canvas.npy")], timeout=600)
    cli_ok(code, payload, f"{stem} prepare")
    canvas = np.load(tmp / f"{stem}-canvas.npy")
    prepared = payload["prepared"]
    same_redacted(list(prepared["labels"].items()), list(py_labels.items()), f"{stem} labels")
    hdr_state = prepared["state"]["hdr"]
    rs_layer = np.load(tmp / f"{stem}-layer.npy")
    # The track after begin and the photo's geometry.
    if hspy.pre_stamp is None:
        same_redacted([hdr_state["active"], int(rs_layer.size)], [False, 0], f"{stem} inactive hdr state")
    else:
        same_redacted(hdr_state["active"], True, f"{stem} active hdr state")
        _same_f32_nan(hspy.pre_stamp, rs_layer, f"{stem} layer before stamp")

    frame_def, stamp_def = load_defaults(h.render.home)
    frame = resolved_frame(edit.frame, frame_def)
    stamp = resolved_stamp(edit.stamp, stamp_def)
    border = frame.style == "instant" and stamp.enabled and stamp.placement == "border"
    no_ink = prepared["frame"] is None and prepared["stamp"] is None
    if prepared["canvas"] == "photo":
        no_ink = False  # the browser paints the blur backdrop
    if no_ink:
        same_pixels(canvas, py_rgb, f"{stem} canvas")
    elif prepared["frame"] is not None and prepared["canvas"] == "full":
        check(spy.base is not None, "finish_array not reached")
        rgb0, _from_raw, kw = spy.base
        photo, _ = real_finish_array(
            rgb0, edit, **{**kw, "frame": FrameSpec(style="none"), "stamp": replace(stamp, enabled=False), "track": None}
        )
        from gxr_studio.finish.fonts import FontMetrics, PilFonts
        from gxr_studio.finish.layout import frame_layout

        lay = frame_layout(photo.shape[1], photo.shape[0], frame, meta, FontMetrics(PilFonts()))
        check(lay is not None, "no frame layout")
        same_pixels(canvas, _photo_only(photo, lay), f"{stem} framed canvas")
    fed = canvas if no_ink else py_rgb
    np.save(tmp / f"{stem}-fed.npy", fed)
    s = tmp / f"{stem}-state.json"
    s.write_text(json.dumps(prepared["state"], ensure_ascii=False), encoding="utf-8")
    cargs = ["export-run-complete", "--canvas", str(tmp / f"{stem}-fed.npy"), "--state", str(s), "--src", str(source)]
    if rs_layer.size:
        cargs += ["--hdr-layer", str(tmp / f"{stem}-layer.npy")]
    if prepared["stamp_on"] == "photo" and rs_layer.size:
        # Python's own stamp mask, on the photo (what the browser paints on black and white).
        mask = _stamp_changed_mask(rs_layer.shape, stamp if not border else replace(stamp, enabled=False), taken, file_time)
        np.save(tmp / f"{stem}-mask.npy", mask.astype(np.uint8))
        cargs += ["--stamp-mask", str(tmp / f"{stem}-mask.npy")]
    code, done = run_json([*cargs, "--out", str(tmp / f"{stem}.jpg")], timeout=600)
    cli_ok(code, done, f"{stem} complete")
    rs_file = (tmp / f"{stem}.jpg").read_bytes()
    how = "equal"
    if rs_file != py_file:
        how = _attribute(rs_file, py_file, hspy.finish, f"{stem}: {first_difference(rs_file, py_file)}")
    r = done["result"]
    same_redacted([r["hdr"], r["hdr_reason"]], [res.hdr, res.hdr_reason], f"{stem} hdr outcome")
    same_redacted([r["file_name"], r["tag"].endswith("_hdr")], [res.path.name, bool(res.hdr)], f"{stem} file name")
    for key in ("width", "height", "render", "render_label", "render_kind", "calibrated", "detail"):
        same_redacted(r[key], getattr(res, key), f"{stem} result.{key}")
    return {"hdr": res.hdr, "reason": res.hdr_reason, "how": how, "ink": not no_ink}


def _pairs(n: int) -> list[tuple[Path, Path]]:
    pairs = [(d, j) for d, j in xs.real_pairs(60) if j is not None]
    if len(pairs) < n:
        missing(f"captures/ 里不足 {n} 对 RAW+JPG（export-pipeline HDR）")
    return [pairs[(i * len(pairs)) // n] for i in range(n)]


@pytest.mark.skipif(DAT is None, reason="没有 g127 DAT")
@pytest.mark.parametrize("long_edge", [None, 1600])
def test_hdr_real_pairs_match_export_finish(long_edge: int | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned_icc: bytes) -> None:  # noqa: F811
    h = xs.make_home(tmp_path / "home", DAT)
    tally: dict[str, int] = {}
    with cjk_fonts((FIXTURE_CJK,)):
        for idx, (dng, jpg) in enumerate(_pairs(10)):
            case = CASES[idx % len(CASES)]
            out = run_hdr_case(h, tmp_path, monkeypatch, f"H{long_edge or 0}{idx:02d}", dng, jpg if case[2] else None, case, long_edge)
            key = f"hdr={out['hdr']}/{out['reason']}/{'ink' if out['ink'] else 'no-ink'}/{out['how']}"
            tally[key] = tally.get(key, 0) + 1
    note(f"e2e hdr long_edge={long_edge}", tally)
    check(sum(tally.values()) == 10, "expected 10 exports")
    check(any(k.startswith("hdr=True") for k in tally), "no export made HDR")


@pytest.mark.skipif(DAT is None, reason="没有 g127 DAT")
def test_hdr_reasons_on_crafted_shots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned_icc: bytes) -> None:  # noqa: F811
    """``no_raw`` (JPG only), ``raw_unreadable`` (a DNG neither side can open), a camera JPG that is
    not this DNG's (``capture_mismatch`` / ``not_aligned``), a JPG without capture EXIF, and
    ``frame_layout``: each reason equals the desktop's."""
    from support.dng_writer import write_dng
    from test_core_parity_export_source import _mosaic

    h = xs.make_home(tmp_path / "home", DAT)
    (dng_a, jpg_a), (dng_b, jpg_b) = _pairs(2)
    bad = tmp_path / "R9999.DNG"
    bad.write_bytes(write_dng(_mosaic(16, 16, 3), compression=7))
    plain = tmp_path / "plain.JPG"
    with Image.open(jpg_a) as im:
        im.convert("RGB").save(plain, quality=90)  # same pixels, no EXIF
    reasons: dict[str, Any] = {}
    with cjk_fonts((FIXTURE_CJK,)):
        cases = [
            ("no_raw", None, jpg_a, None, ({"adjust": {"contrast": 5}}, "auto", True, 95)),
            ("raw_unreadable", bad, jpg_a, None, ({"adjust": {"contrast": 5}}, "auto", True, 95)),
            ("other_jpg", dng_a, jpg_a, jpg_b, ({}, "auto", True, 95)),
            ("no_exif_jpg", dng_a, jpg_a, plain, ({"adjust": {"contrast": 5}}, "auto", True, 95)),
        ]
        for name, dng, jpg, other, case in cases:
            out = run_hdr_case(h, tmp_path, monkeypatch, f"C{name}", dng, jpg, case, None, jpg_file=other)
            reasons[name] = out["reason"]
        # frame_layout needs a pair whose track survives begin: try pairs until one does.
        for i, (dng, jpg) in enumerate(_pairs(8)):
            out = run_hdr_case(h, tmp_path, monkeypatch, f"Cblur{i}", dng, jpg, ({"border": BLUR, "stamp": STAMP_OFF}, "auto", True, 92), None)
            reasons[f"blur{i}"] = out["reason"]
            if out["reason"] == "frame_layout":
                reasons["blur"] = "frame_layout"
                break
    note("hdr reasons", reasons)
    same_redacted([reasons["no_raw"], reasons["raw_unreadable"]], ["no_raw", "raw_unreadable"], "crafted reasons")
    same_redacted(reasons.get("blur"), "frame_layout", "blur reason")


def _bomb_jpeg(path: Path) -> Path:
    """A small real JPEG whose SOF0 claims 65000×65000, over Pillow's pixel limit (2 × 89,478,485)."""
    import io

    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (90, 100, 110)).save(buf, "JPEG", quality=90)
    data = bytearray(buf.getvalue())
    at = data.index(b"\xff\xc0")
    data[at + 5 : at + 9] = (65000).to_bytes(2, "big") + (65000).to_bytes(2, "big")
    path.write_bytes(bytes(data))
    return path


@pytest.mark.skipif(DAT is None, reason="没有 g127 DAT")
def test_hdr_pixel_limit_deviation(tmp_path: Path) -> None:
    """The accepted deviation (hdr-track r4, docs/mobile.md): a camera JPG over Pillow's pixel
    limit. The desktop raises ``DecompressionBombError`` (uncaught in ``load_jpeg`` /
    ``HdrTrack.begin``). The phone decodes pixels in the browser; for an untouched export its
    ``begin`` reports ``metadata_unreadable`` and the export continues without HDR. For an edited
    shot, export-meta's ``photo_meta`` raises the same Pillow error class, so both sides fail."""
    from PIL import Image as PILImage

    h = xs.make_home(tmp_path / "home", DAT)
    (dng, _jpg), *_ = _pairs(1)
    bomb = _bomb_jpeg(tmp_path / "R0500.JPG")
    shot = xs.shot_for(dng, bomb, lens=None, iso=None)
    shot.update({"name": "R0500", "shot_id": "R0500", "width": 16, "height": 16})
    with pytest.raises(PILImage.DecompressionBombError):
        export_finish(h.render, h.lib, shot, parse_edit({}), {}, tmp_path / "out", [], quality=95, long_edge=None, fmt="jpeg", hdr=True)
    np.save(tmp_path / "px.npy", np.full((16, 16, 3), 100, np.uint8))
    common = ["--shot", _w(tmp_path, "shot.json", shot), "--quality", "95", "--hdr", "--dng", str(dng)]
    common += ["--jpg", str(tmp_path / "px.npy"), "--jpg-bytes", str(bomb), *font_args()]
    code, payload = run_json(["export-run-prepare", *common, "--side", _w(tmp_path, "s0.json", {}), "--out", str(tmp_path / "c0.npy")])
    cli_ok(code, payload, "pixel-limit original")
    check(payload["original"] is True, "pixel-limit: not the original branch")
    same_redacted(payload["prepared"]["state"]["hdr"]["problem"], "metadata_unreadable", "pixel-limit hdr problem")
    code, payload = run_json(
        ["export-run-prepare", *common, "--side", _w(tmp_path, "s1.json", {"adjust": {"contrast": 5}}), "--out", str(tmp_path / "c1.npy")]
    )
    check(code == 2, "pixel-limit edited: expected exit 2")
    check("Bomb" in json.dumps(payload["error"]), "pixel-limit edited: not a decompression-bomb error")
