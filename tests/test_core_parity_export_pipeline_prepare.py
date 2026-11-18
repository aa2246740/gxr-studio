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
from support.export_pipeline_cases import (
    check,
    cli_ok,
    mtime_args,
    note,
    produced,
    real_pairs,
    same_file_redacted,
    same_kind,
    same_pixels,
    same_redacted,
)
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
    cli_ok(code, payload, "jpeg-encode for the ICC pin")
    with Image.open(tmp_path / "icc.jpg") as im:
        rs = bytes(im.info["icc_profile"])
    py = srgb_icc()
    check(len(rs) == len(py) == 588 and rs[:24] == py[:24] and rs[36:] == py[36:], "ICC profiles differ outside bytes 24..36")
    monkeypatch.setattr(export_mod, "_ICC", rs)
    return rs


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


def _complete(tmp: Path, tag: str, canvas: np.ndarray, state: dict[str, Any], src: Path) -> tuple[bytes | None, Any]:
    """``export-run-complete`` with the source photo's own bytes (export-meta reads its EXIF)."""
    c, s, o = (tmp / f"{tag}.{x}" for x in ("c.npy", "state.json", "jpg"))
    np.save(c, np.ascontiguousarray(canvas, dtype=np.uint8))
    s.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    code, payload = run_json(["export-run-complete", "--canvas", str(c), "--state", str(s), "--src", str(src), "--out", str(o)])
    return (o.read_bytes() if code == 0 else None), payload


def _photo_only(photo: np.ndarray, layout: FrameLayout) -> np.ndarray:
    bare = replace(layout, items=tuple(it for it in layout.items if it.get("k") == "photo"))
    return np.asarray(draw_frame(Image.fromarray(photo, "RGB"), bare, PilFonts()))


def _write(tmp: Path, name: str, obj: Any) -> str:
    path = tmp / name
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_e2e_camera_jpg_shots_match_export_finish(tmp_path: Path, lib: Any, settings: Any, pinned_icc: bytes) -> None:
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
            # Rust computes meta, taken and file_time itself (export-meta photo_meta) from the JPG's
            # bytes and the file time the phone passes; the layout only carries the mark.
            layout_req = {"mark": desktop_mark()}
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
                *mtime_args(jpg),
            ]
            rgb0, from_raw, _labels0 = color_image(render, shot, edit, max_edge=None, quality=quality, lib=lib, full=True)
            np.save(tmp_path / f"{stem}-jpg.npy", load_jpeg(jpg, None))
            # Rust runs export-source's color_image itself: here the camera-JPG branch.
            args += ["--jpg", str(tmp_path / f"{stem}-jpg.npy"), "--jpg-bytes", str(jpg)]
            canvas, payload = _prepare(tmp_path, stem, args)
            produced(canvas, payload, "prepare")
            prepared = payload["prepared"]
            same_redacted(list(prepared["labels"].items()), list(py_labels.items()), f"{stem} labels")

            frame = resolved_frame(edit.frame, frame_def)
            stamp = resolved_stamp(edit.stamp, stamp_def)
            original = payload["original"]
            no_ink = original or (frame.style == "none" and prepared["stamp"] is None)
            if original:
                tally["original"] += 1
                same_pixels(canvas, py_rgb, f"{stem} canvas")
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
                same_redacted(
                    prepared["frame"], None if py_frame is None else json.loads(json.dumps(py_frame.to_json())), f"{stem} frame layout"
                )
                same_redacted(
                    prepared["stamp"], None if py_stamp is None else json.loads(json.dumps(py_stamp.to_json())), f"{stem} stamp layout"
                )
                want_canvas = photo if py_frame is None else _photo_only(photo, py_frame)
                same_pixels(canvas, want_canvas, f"{stem} canvas")
                if no_ink:
                    same_pixels(canvas, py_rgb, f"{stem} canvas")
            fed = canvas if no_ink else py_rgb
            same_redacted(prepared["state"]["source"], "jpg", f"{stem} source")
            got, done = _complete(tmp_path, stem, fed, prepared["state"], jpg)
            produced(got, done, "complete")
            same_file_redacted(got, py_file, stem)
            same_redacted(done["result"]["file_name"], res.path.name, f"{stem} file name")
            for key in ("width", "height", "render", "render_label", "render_kind", "calibrated", "detail", "hdr", "hdr_reason"):
                same_redacted(done["result"][key], getattr(res, key), f"{stem} result.{key}")
            tally["no_ink" if no_ink else "ink"] += 1
    note("e2e camera-jpg", tally)
    check(tally["original"] >= 1 and tally["ink"] >= 3 and tally["no_ink"] >= 3, "too few cases")


def test_prepare_errors(tmp_path: Path) -> None:
    np.save(tmp_path / "p.npy", np.zeros((4, 4, 3), dtype=np.uint8))
    shot = _write(tmp_path, "shot.json", {"name": "R", "width": 4, "height": 4, "files": {"jpg": {}}})
    base = ["--shot", shot, "--jpg", str(tmp_path / "p.npy")]
    cases = [
        (["--side", _write(tmp_path, "s1.json", {"adjust": {"contrast": "x"}}), "--quality", "95"], "bad_adjust"),
        (["--side", _write(tmp_path, "s2.json", {"stamp": "x"}), "--quality", "95"], "bad_stamp"),
        (["--side", _write(tmp_path, "s3.json", {}), "--quality", "101"], "bad_quality"),
        (["--side", _write(tmp_path, "s4.json", {}), "--quality", "95", "--long-edge", "10"], "bad_size"),
        (["--side", _write(tmp_path, "s5.json", [1]), "--quality", "95"], "bad_request"),
    ]
    for extra, kind in cases:
        canvas, payload = _prepare(tmp_path, kind, base + extra)
        check(canvas is None, "expected an error")
        same_kind(payload, kind, "prepare error")


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
            _write(tmp_path, "shot.json", {"name": "R", "width": 30, "height": 20, "files": {"jpg": {}}}),
            "--side",
            _write(tmp_path, "side.json", {"adjust": {"contrast": 5}}),
            "--defaults",
            str(defaults_path(home)),
            "--jpg",
            str(tmp_path / "p.npy"),
            "--quality",
            "95",
        ]
        canvas, payload = _prepare(tmp_path, f"d{i}", args)
        produced(canvas, payload, "prepare")
        want_frame = None if frame.style == "none" else frame_layout(30, 20, frame, {}, FontMetrics(PilFonts()))
        got_frame = payload["prepared"]["frame"]
        same_redacted(got_frame, None if want_frame is None else json.loads(json.dumps(want_frame.to_json())), f"defaults {i} frame layout")
        # No capture time and no override: the stamp text is empty, so nothing is drawn.
        check(format_stamp(stamp, None, None) == "" or bool(stamp.date_override), "stamp text without a date")
        check(payload["prepared"]["stamp"] is None, "a stamp was laid out")


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
                cli_ok(code, got, f"{stem} sizes {j}")
                same_redacted(got, json.loads(json.dumps(want)), f"{stem} sizes {j}")
                compared += 1
    check(compared >= 30, "too few sizes cases")


# target: (child pytest node, plugin target or None for a probe module, failures expected, each
# failure's message as (label or None for any, file of the failing call, a needle on that line,
# the helper's detail)). Labels are printed only as a hash, followed by the caller's file:line.
Site = tuple[str | None, str, str, str]
_PROBE = "export_pipeline_canary_probe.py"
_HDR = "test_core_parity_export_pipeline_hdr.py"
_SLIP = "EXPORT_PIPELINE_EXIF_REDACTION_CANARY"
CANARY_TARGETS: dict[str, tuple[str, str | None, int, list[Site]]] = {
    "sizes": (
        "test_core_parity_export_pipeline_prepare.py::test_output_sizes_match_desktop",
        "sizes",
        1,
        [("S000 sizes 0", "test_core_parity_export_pipeline_prepare.py", "same_redacted(got, json.loads", "mismatch: $.meta.lens: got=")],
    ),
    "bytes": (
        "test_core_parity_export_pipeline_prepare.py::test_e2e_camera_jpg_shots_match_export_finish",
        "bytes",
        1,
        [("P000", "test_core_parity_export_pipeline_prepare.py", "same_file_redacted(got, py_file, stem)", "lengths ")],
    ),
    "raw": (
        "test_core_parity_export_pipeline_raw.py::test_raw_branches_real_pairs_match_export_finish",
        "raw",
        1,
        [(None, "test_core_parity_export_pipeline_raw.py", "same_file_redacted(got, want_file, stem)", "lengths ")],
    ),
    "probe": (
        f"support/{_PROBE}",
        None,
        14,
        [
            ("SYNTHETIC HDR: Gate A", _HDR, ': Gate A")', "lengths "),
            ("SYNTHETIC HDR: Gate B", _HDR, ': Gate B")', "lengths "),
            ("SYNTHETIC hdr state", _PROBE, '"SYNTHETIC hdr state")', "mismatch: $.problem: got="),
            ("SYNTHETIC pixels", _PROBE, '"SYNTHETIC pixels")', "1 values differ, first at (2, 1, 0)"),
            ("SYNTHETIC field", _PROBE, '"SYNTHETIC field")', "mismatch: $.meta.#"),
            ("SYNTHETIC kind", _PROBE, '"SYNTHETIC kind")', "kind unknown#"),
            ("SYNTHETIC dtype", _PROBE, '"SYNTHETIC dtype")', "shape/dtype (2,) dtype#"),
            # Sol r5's slips: labels built from the photo's LensModel (the canary).
            (f"{_SLIP} metadata", _PROBE, 'f"{photo} metadata")\n', "mismatch: $.lens: got="),
            (f"{_SLIP} export", _PROBE, 'f"{photo} export")', "lengths "),
            (f"{_SLIP} canvas", _PROBE, 'f"{photo} canvas")', "1 values differ, first at (0,)"),
            (f"{_SLIP} prepare", _PROBE, 'f"{photo} prepare")', "exit 2, kind bad_input"),
            (f"{_SLIP} complete", _PROBE, 'f"{photo} complete")', "no output, kind bad_input"),
            (f"{_SLIP} kind", _PROBE, 'f"{photo} kind")', "kind bad_input vs json"),
            (f"{_SLIP} metadata", _PROBE, "# canary slip", "check failed"),
        ],
    ),
}


def _site_pattern(site: Site, repo: Path) -> str:
    """The expected failure message: the label's hash, the caller's file:line, the helper detail."""
    import re

    from support.export_pipeline_cases import _sha

    label, fname, needle, detail = site
    where = repo / "tests" / fname if (repo / "tests" / fname).is_file() else repo / "tests" / "support" / fname
    text = where.read_text(encoding="utf-8")
    line = text[: text.index(needle)].count("\n") + 1
    digest = re.escape(_sha(label)) if label is not None else "[0-9a-f]{16}"
    return f"label sha256 {digest} at {re.escape(fname)}:{line}: {re.escape(detail)}"


@pytest.mark.parametrize("verbosity", ["-q", "-v", "-vv"])
@pytest.mark.parametrize("target", ["bytes", "probe", "raw", "sizes"])
def test_failed_real_comparison_prints_no_exif(target: str, verbosity: str, tmp_path: Path) -> None:
    """Sol r1–r5 P1: force failures on a synthetic EXIF canary through the real comparison code
    (meta, finished file bytes, the RAW/LUT ``run_case``, HDR Gate A and B, HDR state, pixels),
    through the helpers' fixed vocabularies and through Sol r5's slips (labels made from the
    photo's LensModel, ``check(label=...)``), under ``-q``, ``-v`` and ``-vv``. Every child test must
    fail with the label's hash, the caller's file:line and the helper's detail, and neither the
    canary nor the pixel values may reach stdout, stderr or the JUnit file (threat model in
    docs/mobile.md)."""
    import os
    import re
    import subprocess
    import sys

    from support.export_pipeline_canary import CANARY
    from support.export_pipeline_canary_probe import SECRETS

    check(_SLIP == CANARY, "the slip labels use another canary")
    node, plugin, failures, messages = CANARY_TARGETS[target]
    if target == "raw":
        from support.export_source_cases import real_dat

        if real_dat() is None:
            pytest.skip("the RAW path needs the DAT")
    repo = Path(__file__).resolve().parents[1]
    junit = tmp_path / "canary.xml"
    env = {
        **os.environ,
        "EXPORT_PIPELINE_CANARY_DIR": str(tmp_path / "canary-input"),
        "EXPORT_PIPELINE_CANARY_TARGET": plugin or "",
        "PYTHONPATH": str(repo / "tests"),
    }
    env.pop("EXPORT_PIPELINE_NOTES", None)
    args = [sys.executable, "-m", "pytest", verbosity, "-p", "no:randomly", node, f"--basetemp={tmp_path / 'child'}", f"--junitxml={junit}"]
    args[3:3] = ["-p", "support.export_pipeline_canary"] if plugin is not None else ["-o", "python_files=*.py"]
    proc = subprocess.run(args, cwd=repo / "tests", env=env, capture_output=True, text=True, timeout=900, check=False)  # noqa: S603  # nosec B603
    check(proc.returncode == 1, "the forced failure did not fail the child")
    check(f"{failures} failed" in proc.stdout, "wrong number of child failures")
    for site in messages:
        check(re.search(_site_pattern(site, repo), proc.stdout) is not None, "a child failure lacks its hashed label, location or detail")
    for text in (proc.stdout, proc.stderr, junit.read_text(encoding="utf-8")):
        check(CANARY not in text, "the canary reached the output")
        check(not any(secret in text for secret in SECRETS), "pixel values reached the output")


@pytest.mark.parametrize("warnings_as_errors", [True, False])
def test_teardown_errors_and_warning_errors_still_fail(warnings_as_errors: bool, tmp_path: Path) -> None:
    """Sol r4 P1-01 / P2-03: nothing in these tests swallows pytest's own outcomes. A failing
    yield-fixture teardown is an error, and ``-W error`` turns a warning into a failure."""
    import os
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[1]
    args = [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "-o", "python_files=*.py"]
    args += ["-W", "error::UserWarning"] if warnings_as_errors else []
    args += ["support/export_pipeline_semantics_probe.py", f"--basetemp={tmp_path / 'child'}"]
    env = {**os.environ, "PYTHONPATH": str(repo / "tests")}
    proc = subprocess.run(args, cwd=repo / "tests", env=env, capture_output=True, text=True, timeout=300, check=False)  # noqa: S603  # nosec B603
    check(proc.returncode == 1, "the child did not fail")
    check("1 error" in proc.stdout and "SEMANTICS teardown failed" in proc.stdout, "the teardown error was not reported")
    check("UserWarning: SEMANTICS warning" in proc.stdout, "the warning was not shown")
    if warnings_as_errors:
        check("1 failed, 1 passed, 1 error" in proc.stdout, "-W error did not fail on the warning")
    else:
        check("2 passed, 1 warning, 1 error" in proc.stdout, "the warning was not shown as a warning")


@pytest.mark.parametrize("command", ["export-run-plan", "export-run-prepare"])
def test_blank_sidecar_is_an_empty_object(command: str, tmp_path: Path) -> None:
    """Sol r1 P2-01: an omitted, empty or whitespace-only sidecar gives exactly what ``{}`` gives."""
    np.save(tmp_path / "px.npy", np.arange(18, dtype=np.uint8).reshape(2, 3, 3))
    shot = _write(tmp_path, "shot.json", {"name": "SYNTHETIC", "width": 3, "height": 2, "files": {"jpg": {}}})
    (tmp_path / "empty.json").write_text("", encoding="utf-8")
    (tmp_path / "blank.json").write_text(" \n\t", encoding="utf-8")
    (tmp_path / "obj.json").write_text("{}", encoding="utf-8")
    outs = []
    for side in (None, "empty.json", "blank.json", "obj.json"):
        args = [command, "--shot", shot]
        if side is not None:
            args += ["--side", str(tmp_path / side)]
        if command == "export-run-prepare":
            args += ["--jpg", str(tmp_path / "px.npy"), "--out", str(tmp_path / f"c-{side}.npy")]
        code, payload = run_json(args)
        cli_ok(code, payload, f"blank sidecar {side}")
        outs.append(payload)
    same_redacted(outs[1:], [outs[0]] * 3, "blank sidecars vs omitted")
