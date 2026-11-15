"""WASM vs native for the export pipeline: ``export_plan``, ``export_prepare``, ``export_complete`` and
``export_output_sizes`` must give the CLI's bytes, errors included (``mobile/scripts/wasm_cases/export.mjs``).

Covers the UNIT-COMMON amendments: numbers as f64 (``2**32+1``, ``-1``, ``2**53``, NaN, Infinity,
``1.5``) for every size and option, huge sizes checked before any copy, and JSON nesting on both
sides of the 128 limit.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from gxr_studio.finish.spec import FrameSpec, StampSpec
from support.export_pipeline_cases import source_exif_json, synthetic_source
from support.frame_cases import FIXTURE_CJK, desktop_mark, font_pairs
from support.wasm_check import run_area

HOSTILE = [2**32 + 1, -1, 2**53, math.nan, math.inf, 1.5, 0, 4294967295, 2147483648]


def _w(tmp: Path, name: str, obj: Any) -> str:
    path = tmp / name
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, allow_nan=True), encoding="utf-8")
    return str(path)


def _deep(n: int, kind: str) -> str:
    return ("[" * n + "]" * n) if kind == "array" else ('{"a":' * n + "1" + "}" * n)


def _num(v: float) -> Any:
    """Numbers go to the manifest as JSON numbers; NaN and Infinity as the strings ``Number()``
    reads back (``JSON.parse`` rejects the bare tokens)."""
    return v if math.isfinite(v) else repr(v).replace("nan", "NaN").replace("inf", "Infinity")


def test_wasm_matches_native_export_pipeline(tmp_path: Path) -> None:
    fonts = font_pairs()
    rng = np.random.default_rng(7)
    pic = tmp_path / "pic.npy"
    np.save(pic, rng.integers(0, 256, size=(48, 64, 3), dtype=np.uint8))
    shot = _w(tmp_path, "shot.json", {"name": "R0001", "width": 64, "height": 48, "files": {"jpg": {}, "dng": {}}})
    shot_dng = _w(tmp_path, "shot-dng.json", {"name": "R0002", "width": 64, "height": 48, "files": {"dng": {}}})
    layout = _w(
        tmp_path,
        "layout.json",
        {
            "meta": {"lens": "A12 50mm", "aperture": "f/2.5", "shutter": "1/250s", "iso": "ISO 200"},
            "mark": desktop_mark(),
            "taken": [2026, 11, 15, 10, 30],
        },
    )
    labels = _w(
        tmp_path, "labels.json", {"kind": "prediction", "label": "prediction", "text": "电脑预测", "id": "recipe-prediction", "tag": ""}
    )
    sides = {
        "empty": {},
        "adjust-crop": {"adjust": {"exposure_ev": 0.3, "contrast": 12}, "crop": {"rect": [0.1, 0.1, 0.8, 0.8], "preset": "free"}},
        "gallery-stamp": {
            "border": FrameSpec(style="gallery").to_json(),
            "stamp": StampSpec(enabled=True).to_json(),
            "geometry": {"straighten": 1.5},
        },
        "instant-border": {
            "border": FrameSpec(style="instant", fmt="instax_mini").to_json(),
            "stamp": StampSpec(enabled=True, placement="border", date_override="2026-11-15").to_json(),
        },
        "leica": {"border": FrameSpec(style="leica", show_logo=True).to_json()},
        "blur": {"border": FrameSpec(style="blur_fill").to_json()},
        "bad-adjust": {"adjust": {"contrast": "x"}},
    }
    side_paths = {k: _w(tmp_path, f"side-{k}.json", v) for k, v in sides.items()}
    side_paths["deep-127"] = _w(tmp_path, "side-deep127.json", '{"x":' + _deep(126, "array") + "}")
    side_paths["deep-129"] = _w(tmp_path, "side-deep129.json", '{"x":' + _deep(129, "array") + "}")
    side_paths["deep-obj"] = _w(tmp_path, "side-deepobj.json", _deep(200, "object"))
    side_paths["not-json"] = _w(tmp_path, "side-bad.json", "{")
    cases: list[dict[str, Any]] = []
    for key, path in side_paths.items():
        cases.append({"label": f"plan-{key}", "kind": "plan", "shot": shot, "side": path})
        cases.append({"label": f"plan-dng-{key}", "kind": "plan", "shot": shot_dng, "side": path, "base": "camera-jpg"})
    base = {"kind": "prepare", "shot": shot_dng, "layout": layout, "labels": labels, "picture": str(pic), "fonts": fonts, "quality": 95}
    for key, path in side_paths.items():
        for q, edge in [(95, None), (85, 64), (70, 30000)]:
            cases.append(
                {**base, "label": f"prep-{key}-{q}", "side": path, "quality": q, "long_edge": edge, "out": str(tmp_path / f"p-{key}-{q}")}
            )
    cases.append({**base, "label": "prep-original", "shot": shot, "side": side_paths["empty"], "out": str(tmp_path / "p-orig")})
    cases.append({**base, "label": "prep-from-raw", "side": side_paths["adjust-crop"], "from_raw": True, "out": str(tmp_path / "p-raw")})
    for i, v in enumerate(HOSTILE):
        cases.append({**base, "label": f"prep-w-{i}", "side": side_paths["empty"], "w": _num(v), "out": str(tmp_path / f"p-w{i}")})
        cases.append({**base, "label": f"prep-h-{i}", "side": side_paths["empty"], "h": _num(v), "out": str(tmp_path / f"p-h{i}")})
        cases.append({**base, "label": f"prep-q-{i}", "side": side_paths["empty"], "quality": _num(v), "out": str(tmp_path / f"p-q{i}")})
        cases.append({**base, "label": f"prep-e-{i}", "side": side_paths["empty"], "long_edge": _num(v), "out": str(tmp_path / f"p-e{i}")})

    src = synthetic_source(tmp_path / "src.jpg")
    exif = _w(tmp_path, "exif.json", source_exif_json(src))
    state = {"name": "R0001", "quality": 95, "long_edge": None, "labels": {"text": "原图"}, "hdr": None}
    states = {
        "q95": state,
        "q85-edge": {**state, "quality": 85, "long_edge": 64},
        "q70": {**state, "quality": 70},
        "bad-q": {**state, "quality": 0},
        "float-q": {**state, "quality": 95.0},
        "no-name": {k: v for k, v in state.items() if k != "name"},
    }
    for key, st in states.items():
        cases.append(
            {
                "label": f"done-{key}",
                "kind": "complete",
                "canvas": str(pic),
                "state": _w(tmp_path, f"st-{key}.json", st),
                "exif": exif,
                "out": str(tmp_path / f"d-{key}.jpg"),
            }
        )
    st95 = _w(tmp_path, "st95.json", state)
    for i, v in enumerate(HOSTILE):
        for dim in ("w", "h"):
            cases.append(
                {
                    "label": f"done-{dim}-{i}",
                    "kind": "complete",
                    "canvas": str(pic),
                    "state": st95,
                    dim: _num(v),
                    "out": str(tmp_path / f"d-{dim}{i}.jpg"),
                }
            )
    cases.append(
        {
            "label": "done-huge",
            "kind": "complete",
            "canvas": str(pic),
            "state": st95,
            "w": 4294967295,
            "h": 4294967295,
            "out": str(tmp_path / "d-huge.jpg"),
        }
    )
    cases.append(
        {
            "label": "done-tall",
            "kind": "complete",
            "canvas": str(pic),
            "state": st95,
            "w": 2147483648,
            "h": 1,
            "out": str(tmp_path / "d-tall.jpg"),
        }
    )
    deep_state = _w(tmp_path, "st-deep.json", '{"name":"R","quality":95,"labels":{},"x":' + _deep(129, "array") + "}")
    cases.append({"label": "done-deep", "kind": "complete", "canvas": str(pic), "state": deep_state, "out": str(tmp_path / "d-deep.jpg")})

    sizes_base = {"kind": "sizes", "width": 4288, "height": 2848, "layout": layout, "fonts": fonts}
    for key in ("empty", "gallery-stamp", "instant-border", "leica", "bad-adjust", "deep-129"):
        cases.append({**sizes_base, "label": f"sizes-{key}", "side": side_paths[key]})
    cases.append({**sizes_base, "label": "sizes-xpan", "side": side_paths["empty"], "preset": "xpan-65-24"})
    cases.append({**sizes_base, "label": "sizes-rect", "side": side_paths["empty"], "rect": "[0.1, 0.1, 0.5, 0.5]", "preset": "free"})
    cases.append({**sizes_base, "label": "sizes-bad-rect", "side": side_paths["empty"], "rect": "[1, 2]"})
    for i, v in enumerate(HOSTILE):
        cases.append({**sizes_base, "label": f"sizes-w-{i}", "side": side_paths["empty"], "width": _num(v)})

    manifest = _w(tmp_path, "export-cases.json", json.dumps(cases, ensure_ascii=False, allow_nan=True))
    assert FIXTURE_CJK.is_file()
    proc = run_area("export", [Path(manifest)], tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
