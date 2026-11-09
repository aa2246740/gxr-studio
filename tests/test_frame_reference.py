"""The approved Pillow layouts on synthetic sizes, no camera data or photos.

The committed digests came from DESIGN's renderer. With its local directory
present, also compare every field directly. CI runs the portable digests only.
Regenerate: scripts/frame_reference.py --reference /path/to/frame-design
"""

from __future__ import annotations

import hashlib
import importlib
import itertools
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from gxr_studio.finish.fonts import FontMetrics
from gxr_studio.finish.layout import frame_layout
from gxr_studio.finish.spec import OUTPUT_RATIOS, parse_frame

FIXTURE = Path(__file__).parent / "golden" / "frames_r4.json"
REFERENCE = Path(os.environ.get("GXR_FRAME_REFERENCE", "/Volumes/Workspace/Scratch/gxr-studio-hillclimb/round-3/frame-design"))
META = {
    "lens": "GR LENS A12 50mm F2.5 MACRO",
    "lens_short": "A12 50mm",
    "aperture": "f/7.1",
    "shutter": "1/100s",
    "iso": "ISO 200",
    "date": "2026.10.24",
    "frame_no": "1450",
}
SIZES = [(1200, 800), (800, 1200), (900, 900), (1200, 900), (1300, 480), (720, 1280)]
COLORS = ("white", "black", "cream")
CAPTIONS = ("", "A synthetic caption here")
OPTIONS = {
    "gallery": [{"color": c, "weight": w} for c, w in itertools.product(COLORS, ("thin", "medium", "thick"))],
    "leica": [
        {"color": color, "show_logo": logo, "show_params": params, "show_date": date, "text": text}
        for color, logo, params, date, text in itertools.product(COLORS, (False, True), (False, True), (False, True), CAPTIONS)
    ],
    "hasselblad": [
        {"color": color, "show_params": params, "show_date": date, "text": text}
        for color, params, date, text in itertools.product(COLORS, (False, True), (False, True), CAPTIONS)
    ],
    "film": [{"fmt": f, "table": t} for f, t in itertools.product(("135", "120"), ("light", "black"))],
    "instant": [
        {"fmt": fmt, "show_date": date, "text": text}
        for fmt, date, text in itertools.product(("polaroid", "instax_square", "instax_wide", "instax_mini"), (False, True), CAPTIONS)
    ],
}


def cases() -> list[dict[str, Any]]:
    return [
        {"size": [w, h], "spec": {"style": style, "output_ratio": ratio, **opts}, "meta": meta}
        for (w, h), ratio, (style, options), meta in itertools.product(SIZES, (*OUTPUT_RATIOS, "original"), OPTIONS.items(), (META, {}))
        for opts in options
    ]


def canonical(raw: Any, *, exact: bool = False) -> Any:
    if isinstance(raw, dict):
        return {k: canonical(v, exact=exact) for k, v in raw.items() if k != "cjk"}
    if isinstance(raw, (list, tuple)):
        return [canonical(v, exact=exact) for v in raw]
    if isinstance(raw, float) and not exact:
        return int(raw) if raw.is_integer() else round(raw, 8)
    return raw


def digest(raw: Any) -> str:
    text = json.dumps(canonical(raw), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def reference_layouts(directory: Path, inputs: list[dict[str, Any]]) -> list[Any]:
    # Explicit opt-in path to local design code. It imports fonts, never photos.
    sys.path.insert(0, str(directory))
    try:
        plain = importlib.import_module("gxr_styles_a")
        objects = importlib.import_module("gxr_styles_b")
        out = []
        for case in inputs:
            spec = dict(case["spec"])
            style, ratio = spec.pop("style"), spec.pop("output_ratio")
            fn = getattr(plain if style in ("gallery", "leica", "hasselblad") else objects, style)
            out.append(fn(*case["size"], case["meta"], OUTPUT_RATIOS.get(ratio), **spec))
        return out
    finally:
        sys.path.pop(0)


def test_all_frame_layouts_match_the_approved_reference_digests() -> None:
    fixture = json.loads(FIXTURE.read_text())
    inputs = cases()
    assert len(inputs) == fixture["count"]
    metrics = FontMetrics()
    for index, (case, want) in enumerate(zip(inputs, fixture["sha256"], strict=True)):
        spec = parse_frame(case["spec"])
        assert spec is not None
        layout = frame_layout(*case["size"], spec, case["meta"], metrics)
        assert layout is not None
        assert digest(layout.to_json()) == want, (index, case, layout.to_json())


@pytest.mark.skipif(not (REFERENCE / "gxr_core.py").is_file(), reason="local approved design reference is absent")
def test_every_layout_number_equals_the_local_reference() -> None:
    inputs = cases()
    metrics = FontMetrics()
    for case, want in zip(inputs, reference_layouts(REFERENCE, inputs), strict=True):
        spec = parse_frame(case["spec"])
        assert spec is not None
        layout = frame_layout(*case["size"], spec, case["meta"], metrics)
        assert layout is not None
        assert canonical(layout.to_json(), exact=True) == canonical(want, exact=True), case
