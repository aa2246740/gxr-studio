"""Parity: ``export-run-prepare`` (and ``-complete`` after it) against desktop ``export_finish``.

Real camera-JPG shots in a temporary library (``GXR_STUDIO_TEST_PARENT``), so ``color_image`` is the
camera JPG and the whole chain runs on the desktop. The RAW and LUT branches come with export-source.

Gates (UNIT-export-pipeline.md):
- no ink (frame ``none``, no stamp drawn): Rust's canvas equals Python's finished array, and
  prepare → complete gives ``export_finish``'s file byte for byte;
- with ink: Rust's canvas equals ``draw_frame`` with only the background and the photo; feeding
  Python's own inked array into ``export-run-complete`` gives ``export_finish``'s file byte for byte;
- the frame and stamp layout JSON and the labels are exact (order included).
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from gxr_studio import export as export_mod
from gxr_studio.export import srgb_icc
from gxr_studio.finish import serve as serve_mod
from gxr_studio.finish.compose import finish_array, load_jpeg, resolved_frame, resolved_stamp
from gxr_studio.finish.fonts import FontMetrics, PilFonts
from gxr_studio.finish.frame import draw_frame
from gxr_studio.finish.layout import FrameLayout, frame_layout, stamp_layout
from gxr_studio.finish.serve import color_image, export_finish, load_defaults, render_stage_rgb
from gxr_studio.finish.spec import FrameSpec, StampSpec
from gxr_studio.finish.stamp import format_stamp
from gxr_studio.render.params import parse_edit
from gxr_studio.render.service import RenderService
from support.core_cli import require_cli, run_json
from support.export_pipeline_cases import first_difference, real_pairs
from support.frame_cases import FIXTURE_CJK, cjk_fonts, desktop_mark, font_args
from support.samples import missing


@pytest.fixture(autouse=True)
def _cli() -> None:
    require_cli()


@pytest.fixture
def pinned_icc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> bytes:
    """See ``test_core_parity_export_pipeline.pinned_icc``: only header bytes 24..36 differ."""
    np.save(tmp_path / "icc.npy", np.zeros((8, 8, 3), dtype=np.uint8))
    code, payload = run_json(["jpeg-encode", "--in", str(tmp_path / "icc.npy"), "--quality", "95", "--out", str(tmp_path / "icc.jpg")])
    assert code == 0, payload
    with Image.open(tmp_path / "icc.jpg") as im:
        rs = bytes(im.info["icc_profile"])
    py = srgb_icc()
    assert len(rs) == len(py) == 588 and rs[:24] == py[:24] and rs[36:] == py[36:]
    monkeypatch.setattr(export_mod, "_ICC", rs)
    return rs


def _moment(m: datetime | None) -> list[int] | None:
    return None if m is None else [m.year, m.month, m.day, m.hour, m.minute]


GALLERY = FrameSpec(style="gallery", output_ratio="original").to_json()
INSTANT = FrameSpec(style="instant", fmt="instax_mini").to_json()
LEICA = FrameSpec(style="leica", show_logo=True).to_json()
KEYLINE = FrameSpec(style="thin_keyline").to_json()
STAMP_ON = StampSpec(enabled=True, date_override="2026-10-31").to_json()
STAMP_BORDER = StampSpec(enabled=True, placement="border", date_override="2026-11-15").to_json()
STAMP_OFF = StampSpec(enabled=False).to_json()

SIDES: list[dict[str, Any]] = [
    {},
    {"adjust": {"exposure_ev": 0.25, "contrast": 10}, "crop": {"rect": [0.05, 0.05, 0.9, 0.9], "preset": "free"}},
    {"geometry": {"straighten": 2.0}, "crop": {"rect": [0.0, 0.0, 1.0, 1.0], "preset": "3:2"}},
    {"adjust": {"saturation": 20, "vignette": -30}, "border": GALLERY, "stamp": STAMP_ON},
    {"adjust": {"exposure_ev": -0.2}, "border": INSTANT, "stamp": STAMP_BORDER},
    {"border": LEICA, "stamp": STAMP_OFF},
    {"geometry": {"rotate90": 1, "flip": True}, "border": KEYLINE, "stamp": STAMP_ON},
    {
        "curve": {"master": [[0, 0], [128, 150], [255, 255]], "b": [[0, 10], [255, 240]]},
        "adjust": {"wb": {"warm": 15, "tint": -5}, "black_point": 8},
    },
    {"adjust": {"exposure_ev": 0.123456, "highlight": -40, "shadow": 30, "vibrance": 25}, "crop": {"rect": [0.1, 0.2, 0.5, 0.5]}},
    {"stamp": STAMP_ON},
]


def _shot_in_lib(lib: Any, jpg: Path, dng: Path, stem: str, *, copy: bool = False) -> dict[str, Any]:
    """Link (or, for code that refuses links such as ``open_media``, copy) a real pair into the
    temporary library. Nothing is written outside the test's temporary folders."""
    folder = lib.root / "2026" / "pipe"
    folder.mkdir(parents=True, exist_ok=True)
    for src, ext in ((jpg, "JPG"), (dng, "DNG")):
        dest = folder / f"{stem}.{ext}"
        if copy:
            shutil.copyfile(src, dest)
        else:
            dest.symlink_to(src)
    h0, w0 = load_jpeg(jpg, None).shape[:2]
    return {
        "shot_id": stem,
        "name": stem,
        "width": w0,
        "height": h0,
        "files": {"jpg": {"relpath": f"2026/pipe/{stem}.JPG"}, "dng": {"relpath": f"2026/pipe/{stem}.DNG"}},
    }


def _prepare(tmp: Path, tag: str, args: list[str]) -> tuple[np.ndarray | None, Any]:
    out = tmp / f"{tag}-canvas.npy"
    code, payload = run_json(["export-run-prepare", *args, *font_args(), "--out", str(out)])
    if code != 0:
        return None, payload
    return np.load(out), payload


def _complete(tmp: Path, tag: str, canvas: np.ndarray, state: dict[str, Any], exif_json: str) -> tuple[bytes | None, Any]:
    c, s, e, o = (tmp / f"{tag}.{x}" for x in ("c.npy", "state.json", "exif.json", "jpg"))
    np.save(c, np.ascontiguousarray(canvas, dtype=np.uint8))
    s.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    e.write_text(exif_json, encoding="utf-8")
    code, payload = run_json(["export-run-complete", "--canvas", str(c), "--state", str(s), "--exif", str(e), "--out", str(o)])
    return (o.read_bytes() if code == 0 else None), payload


def _photo_only(photo: np.ndarray, layout: FrameLayout) -> np.ndarray:
    bare = replace(layout, items=tuple(it for it in layout.items if it.get("k") == "photo"))
    return np.asarray(draw_frame(Image.fromarray(photo, "RGB"), bare, PilFonts()))


def _write(tmp: Path, name: str, obj: Any) -> str:
    path = tmp / name
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_e2e_camera_jpg_shots_match_export_finish(tmp_path: Path, lib: Any, settings: Any, pinned_icc: bytes) -> None:
    from support.export_pipeline_cases import source_exif_json

    pairs = real_pairs(18)
    if not pairs:
        missing("captures/ 真实样本不在本机（export-pipeline）")
    render = RenderService(settings.home_dir, lib)
    frame_def, stamp_def = load_defaults(render.home)
    tally = {"no_ink": 0, "ink": 0, "original": 0}
    with cjk_fonts((FIXTURE_CJK,)):
        for idx, (jpg, dng) in enumerate(pairs):
            stem = f"P{idx:03d}"
            shot = _shot_in_lib(lib, jpg, dng, stem)
            side = SIDES[idx % len(SIDES)]
            edit = parse_edit(side)
            quality = (95, 85, 70)[idx % 3]
            long_edge = None if idx % 2 == 0 else 1600
            res = export_finish(render, lib, shot, edit, side, settings.export_dir, [], quality=quality, long_edge=long_edge, fmt="jpeg")
            py_file = res.path.read_bytes()
            py_rgb, py_labels = render_stage_rgb(render, lib, shot, edit, side, stage="final", max_edge=None, quality=quality)
            meta, taken, file_time = serve_mod._meta(shot, lib)
            layout_req = {"meta": meta, "mark": desktop_mark(), "taken": _moment(taken), "file_time": _moment(file_time)}
            args = [
                "--shot",
                _write(tmp_path, f"{stem}-shot.json", {k: shot[k] for k in ("name", "width", "height", "files")}),
                "--side",
                _write(tmp_path, f"{stem}-side.json", side),
                "--layout",
                _write(tmp_path, f"{stem}-layout.json", layout_req),
                "--quality",
                str(quality),
                *(["--long-edge", str(long_edge)] if long_edge else []),
            ]
            rgb0, from_raw, labels0 = color_image(render, shot, edit, max_edge=None, quality=quality, lib=lib, full=True)
            np.save(tmp_path / f"{stem}-picked.npy", rgb0)
            np.save(tmp_path / f"{stem}-jpg.npy", load_jpeg(jpg, None))
            args += ["--picked", str(tmp_path / f"{stem}-picked.npy"), "--from-raw", "1" if from_raw else "0"]
            args += ["--labels", _write(tmp_path, f"{stem}-labels.json", labels0), "--jpg", str(tmp_path / f"{stem}-jpg.npy")]
            canvas, payload = _prepare(tmp_path, stem, args)
            assert canvas is not None, (stem, payload)
            prepared = payload["prepared"]
            assert list(prepared["labels"].items()) == list(py_labels.items()), stem

            frame = resolved_frame(edit.frame, frame_def)
            stamp = resolved_stamp(edit.stamp, stamp_def)
            original = payload["original"]
            no_ink = original or (frame.style == "none" and prepared["stamp"] is None)
            if original:
                tally["original"] += 1
                assert np.array_equal(canvas, py_rgb), stem
            else:
                # The photo before ink: the same finish with the stamp off and no frame.
                photo, _hdr = finish_array(
                    rgb0,
                    edit,
                    stage="final",
                    frame=FrameSpec(style="none"),
                    stamp=replace(stamp, enabled=False),
                    crop_rect=tuple(side["crop"]["rect"]) if side.get("crop") else None,
                    meta=meta,
                    taken=taken,
                    file_time=file_time,
                    from_raw=from_raw,
                    crop_preset=side.get("crop", {}).get("preset"),
                )
                h, w = photo.shape[:2]
                m = FontMetrics(PilFonts())
                py_frame = frame_layout(w, h, frame, meta, m)
                border = frame.style == "instant" and stamp.enabled and stamp.placement == "border"
                text = format_stamp(stamp, taken, file_time)
                py_stamp = stamp_layout(w, h, stamp, text, m, py_frame if border else None) if stamp.enabled and text else None
                assert prepared["frame"] == (None if py_frame is None else json.loads(json.dumps(py_frame.to_json()))), stem
                assert prepared["stamp"] == (None if py_stamp is None else json.loads(json.dumps(py_stamp.to_json()))), stem
                want_canvas = photo if py_frame is None else _photo_only(photo, py_frame)
                assert np.array_equal(canvas, want_canvas), stem
                if no_ink:
                    assert np.array_equal(canvas, py_rgb), stem
            exif_json = source_exif_json(jpg)
            fed = canvas if no_ink else py_rgb
            got, done = _complete(tmp_path, stem, fed, prepared["state"], exif_json)
            assert got is not None, (stem, done)
            assert got == py_file, f"{stem}: {first_difference(got, py_file)}"
            assert done["result"]["file_name"] == res.path.name
            for key in ("width", "height", "render", "render_label", "render_kind", "calibrated", "detail", "hdr", "hdr_reason"):
                assert done["result"][key] == getattr(res, key), (stem, key)
            tally["no_ink" if no_ink else "ink"] += 1
    print("export-pipeline e2e camera-jpg:", tally)
    assert tally["original"] >= 1 and tally["ink"] >= 3 and tally["no_ink"] >= 3


def test_prepare_errors(tmp_path: Path) -> None:
    np.save(tmp_path / "p.npy", np.zeros((4, 4, 3), dtype=np.uint8))
    shot = _write(tmp_path, "shot.json", {"name": "R", "width": 4, "height": 4, "files": {"dng": {}}})
    base = ["--shot", shot, "--picked", str(tmp_path / "p.npy"), "--labels", _write(tmp_path, "l.json", {})]
    cases = [
        (["--side", _write(tmp_path, "s1.json", {"adjust": {"contrast": "x"}}), "--quality", "95"], "bad_adjust"),
        (["--side", _write(tmp_path, "s2.json", {"stamp": "x"}), "--quality", "95"], "bad_stamp"),
        (["--side", _write(tmp_path, "s3.json", {}), "--quality", "101"], "bad_quality"),
        (["--side", _write(tmp_path, "s4.json", {}), "--quality", "95", "--long-edge", "10"], "bad_size"),
        (["--side", _write(tmp_path, "s5.json", [1]), "--quality", "95"], "bad_request"),
    ]
    for extra, kind in cases:
        canvas, payload = _prepare(tmp_path, kind, base + extra)
        assert canvas is None and payload["error"]["kind"] == kind, (kind, payload)


def test_defaults_fall_back_like_load_defaults(tmp_path: Path) -> None:
    """Broken stored defaults give the factory frame and stamp, as ``serve.load_defaults`` does."""
    from gxr_studio.finish.serve import defaults_path

    for i, raw in enumerate(['{"frame": 3, "stamp": {"color": "pink"}}', "not json", "[]", '{"frame": {"version": 2, "style": "gallery"}}']):
        home = tmp_path / f"home{i}"
        home.mkdir()
        defaults_path(home).write_text(raw, encoding="utf-8")
        frame, stamp = load_defaults(home)
        np.save(tmp_path / "p.npy", np.full((20, 30, 3), 90, dtype=np.uint8))
        args = [
            "--shot",
            _write(tmp_path, "shot.json", {"name": "R", "width": 30, "height": 20, "files": {"dng": {}}}),
            "--side",
            _write(tmp_path, "side.json", {"adjust": {"contrast": 5}}),
            "--defaults",
            str(defaults_path(home)),
            "--picked",
            str(tmp_path / "p.npy"),
            "--labels",
            _write(tmp_path, "l.json", {"kind": "original"}),
            "--quality",
            "95",
        ]
        canvas, payload = _prepare(tmp_path, f"d{i}", args)
        assert canvas is not None, payload
        want_frame = None if frame.style == "none" else frame_layout(30, 20, frame, {}, FontMetrics(PilFonts()))
        got_frame = payload["prepared"]["frame"]
        assert got_frame == (None if want_frame is None else json.loads(json.dumps(want_frame.to_json()))), raw
        # No capture time and no override: the stamp text is empty, so nothing is drawn.
        assert format_stamp(stamp, None, None) == "" or stamp.date_override
        assert payload["prepared"]["stamp"] is None


def test_output_sizes_match_desktop(tmp_path: Path, lib: Any, settings: Any) -> None:
    from gxr_studio.finish.sizes import output_sizes, source_size

    pairs = real_pairs(6)
    if not pairs:
        missing("captures/ 真实样本不在本机（export-pipeline）")
    render = RenderService(settings.home_dir, lib)
    crops: list[tuple[Any, Any]] = [
        (None, None),
        ([0.1, 0.1, 0.8, 0.7], "free"),
        (None, "xpan-65-24"),
        ([0.0, 0.0, 1.0, 1.0], "4:5"),
        (None, "original"),
    ]
    sides: list[dict[str, Any]] = [
        {},
        {"geometry": {"straighten": 3.0}},
        {"border": GALLERY},
        {"border": FrameSpec(style="gallery", output_ratio="4:5").to_json(), "geometry": {"rotate90": 1}},
        {"border": INSTANT},
        {"border": LEICA, "geometry": {"vertical": 20}},
    ]
    compared = 0
    with cjk_fonts((FIXTURE_CJK,)):
        for idx, (jpg, dng) in enumerate(pairs):
            stem = f"S{idx:03d}"
            shot = _shot_in_lib(lib, jpg, dng, stem, copy=True)
            meta, taken, file_time = serve_mod._meta(shot, lib)
            layout = _write(tmp_path, f"{stem}-layout.json", {"meta": meta, "mark": desktop_mark()})
            for j, side in enumerate(sides):
                rect, preset = crops[(idx + j) % len(crops)]
                edit = parse_edit(side)
                want = output_sizes(render, lib, shot, edit, tuple(rect) if rect else None, preset)
                sw, sh = source_size(render, lib, shot, edit)
                args = ["export-run-sizes", "--width", str(sw), "--height", str(sh), "--side", _write(tmp_path, f"{stem}-{j}.json", side)]
                args += ["--layout", layout, *font_args()]
                if rect is not None:
                    args += ["--rect", json.dumps(rect)]
                if preset is not None:
                    args += ["--preset", preset]
                code, got = run_json(args)
                assert code == 0, (stem, j, got)
                assert got == json.loads(json.dumps(want)), (stem, j)
                compared += 1
    assert compared >= 30
