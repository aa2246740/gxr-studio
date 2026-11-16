"""Parity: gxr-cli supersampled shapes (aa_paint, paint_rect/poly/ellipse/hole/shadow) (Item 3)."""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from gxr_studio.finish import frame_paint as paint
from gxr_studio.finish.fonts import FontMetrics
from gxr_studio.finish.layout import frame_layout
from gxr_studio.finish.spec import FRAME_STYLES, FrameSpec
from support import frame_cases as fc
from support.core_cli import run_json, run_npy
from support.export_pipeline_cases import anon_ids, check, same_pixels

SHAPE_KINDS = {"rect", "poly", "ellipse", "hole", "shadow"}


def _synth_rgb(w: int, h: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)


def _item_to_op(it: dict[str, Any]) -> dict[str, Any]:
    k = it["k"]
    if k == "rect":
        op: dict[str, Any] = {
            "op": "paint_rect",
            "x": it["x"],
            "y": it["y"],
            "w": it["w"],
            "h": it["h"],
            "radius": it.get("radius", 0),
            "stroke_w": it.get("stroke_w", 1),
            "alpha": it.get("alpha", 1.0),
        }
        if it.get("fill") is not None:
            op["fill"] = list(it["fill"])
        if it.get("outline") is not None:
            op["outline"] = list(it["outline"])
        return op
    if k == "poly":
        return {"op": "paint_poly", "pts": [list(p) for p in it["pts"]], "fill": list(it["fill"]), "alpha": it.get("alpha", 1.0)}
    if k == "ellipse":
        return {"op": "paint_ellipse", "cx": it["cx"], "cy": it["cy"], "r": it["r"], "fill": list(it["fill"]), "alpha": it.get("alpha", 1.0)}
    if k == "hole":
        return {"op": "paint_hole", "x": it["x"], "y": it["y"], "w": it["w"], "h": it["h"], "radius": it.get("radius", 0), "base_reg": "base"}
    if k == "shadow":
        return {
            "op": "paint_shadow",
            "x": it["x"],
            "y": it["y"],
            "w": it["w"],
            "h": it["h"],
            "blur": it["blur"],
            "dx": it.get("dx", 0),
            "dy": it.get("dy", 0),
            "radius": it.get("radius", 0),
            "alpha": it.get("alpha", 0.3),
            "color": list(it.get("color", (0, 0, 0))),
        }
    raise ValueError(f"unexpected item kind {k!r}")


def _py_paint_item(canvas: Image.Image, it: dict[str, Any], base: Image.Image) -> None:
    k = it["k"]
    if k == "rect":
        paint.paint_rect(canvas, it)
    elif k == "poly":
        paint.paint_poly(canvas, it)
    elif k == "ellipse":
        paint.paint_ellipse(canvas, it)
    elif k == "hole":
        paint.paint_hole(canvas, it, base)
    elif k == "shadow":
        paint.paint_shadow(canvas, it)
    else:
        raise ValueError(f"unexpected item kind {k!r}")


def _run_batch(w: int, h: int, items: list[dict[str, Any]], tmp_path: Path, tag: str, seed: int) -> None:
    canvas_arr = _synth_rgb(w, h, seed)
    base_arr = _synth_rgb(w, h, seed + 1)
    pil_canvas = Image.fromarray(canvas_arr.copy(), "RGB")
    pil_base = Image.fromarray(base_arr, "RGB")

    in_npy = tmp_path / f"{tag}_in.npy"
    base_npy = tmp_path / f"{tag}_base.npy"
    ops_p = tmp_path / f"{tag}_ops.json"
    out_npy = tmp_path / f"{tag}_out.npy"
    np.save(in_npy, canvas_arr)
    np.save(base_npy, np.dstack([base_arr, np.full((h, w), 255, dtype=np.uint8)]))

    valid_items: list[dict[str, Any]] = []
    for idx, it in enumerate(items):
        snap = pil_canvas.copy()
        try:
            _py_paint_item(snap, it, pil_base)
        except ValueError:
            err_ops_p = tmp_path / f"{tag}_err_{idx}_ops.json"
            err_out_p = tmp_path / f"{tag}_err_{idx}_out.npy"
            err_ops = [
                {"op": "convert", "target": "logo:base_in", "mode": "RGB", "save_as": "base"},
                _item_to_op(it),
            ]
            err_ops_p.write_text(json.dumps(err_ops), encoding="utf-8")
            code, payload = run_json(
                [
                    "ink-ops",
                    "--canvas",
                    str(in_npy),
                    "--mode",
                    "RGB",
                    "--logo",
                    f"base_in={base_npy}",
                    "--ops",
                    str(err_ops_p),
                    "--out",
                    str(err_out_p),
                ]
            )
            check(code != 0 and payload["error"]["kind"] == "value_error", "degenerate shape error parity")
            continue
        pil_canvas = snap
        valid_items.append(it)

    if not valid_items:
        return
    ops = [
        {"op": "convert", "target": "logo:base_in", "mode": "RGB", "save_as": "base"},
    ] + [_item_to_op(it) for it in valid_items]
    ops_p.write_text(json.dumps(ops), encoding="utf-8")
    got = run_npy(["ink-ops", "--canvas", str(in_npy), "--mode", "RGB", "--logo", f"base_in={base_npy}", "--ops", str(ops_p)], out_npy)
    same_pixels(got, np.asarray(pil_canvas, dtype=np.uint8), f"ink shapes {tag}")


class _CachedMetrics(FontMetrics):
    def __init__(self) -> None:
        super().__init__()
        self._upm: dict[tuple[str, bool], int] = {}
        self._glyph: dict[tuple[str, str, bool], Any] = {}
        self._ascent: dict[tuple[str, int, bool], int] = {}

    def upm(self, font: str, cjk: bool) -> int:
        key = (font, cjk)
        if key not in self._upm:
            self._upm[key] = super().upm(font, cjk)
        return self._upm[key]

    def glyph(self, font: str, ch: str, cjk: bool) -> Any:
        key = (font, ch, cjk)
        if key not in self._glyph:
            self._glyph[key] = super().glyph(font, ch, cjk)
        return self._glyph[key]

    def ascent(self, font: str, px: int, cjk: bool) -> int:
        key = (font, px, cjk)
        if key not in self._ascent:
            self._ascent[key] = super().ascent(font, px, cjk)
        return self._ascent[key]


def _collect_layout_batches() -> list[tuple[int, int, list[dict[str, Any]]]]:
    metrics = _CachedMetrics()
    grouped: dict[tuple[int, int], dict[str, dict[str, Any]]] = {}

    def record_layout(cw: int, ch: int, items: list[dict[str, Any]]) -> None:
        bucket = grouped.setdefault((cw, ch), {})
        for it in items:
            if it.get("k") in SHAPE_KINDS:
                bucket.setdefault(json.dumps(it, sort_keys=True), it)

    def record(w: int, h: int, spec: FrameSpec, meta: dict[str, str]) -> None:
        try:
            layout = frame_layout(w, h, spec, meta, metrics)
        except (ZeroDivisionError, ValueError):
            return
        if layout is None:
            return
        record_layout(layout.width, layout.height, list(layout.items))

    saved_factory = fc.METRICS_FACTORY
    fc.METRICS_FACTORY = lambda: metrics
    try:
        with fc.cjk_fonts((fc.FIXTURE_CJK,) if fc.FIXTURE_CJK.is_file() else ()):
            _, wants = fc.frame_cases()
            for want in wants:
                if isinstance(want, dict) and "items" in want:
                    record_layout(int(want["width"]), int(want["height"]), want["items"])
    finally:
        fc.METRICS_FACTORY = saved_factory

    sweep_sizes = ((1, 1), (17, 17), (320, 240), (240, 320), (400, 400), (1600, 1200), (2848, 4288), (4288, 2848))
    for w, h in sweep_sizes:
        wide = w * h > 2_000_000
        styles = sorted(FRAME_STYLES - {"none"})
        weights = ("medium",) if wide else ("thin", "medium", "thick")
        colors = ("white", "black", "cream")
        fmts = ("135", "120", "polaroid", "instax_square", "instax_wide", "instax_mini") if not wide else ("135", "polaroid")
        ratios = ("original", "3:4", "1:1") if wide else ("original", "3:4", "4:5", "1:1", "16:9")
        for style, weight, color, fmt, ratio in itertools.product(styles, weights, colors, fmts, ratios):
            if style == "instant" and fmt not in ("polaroid", "instax_square", "instax_wide", "instax_mini"):
                continue
            if style == "film" and fmt not in ("135", "120"):
                continue
            if style not in ("instant", "film") and fmt != "135":
                continue
            if style == "slide_mount" and color == "white":
                continue
            spec = FrameSpec(style=style, color=color, weight=weight, fmt=fmt, output_ratio=ratio, table="light")
            record(w, h, spec, {})
        record(w, h, FrameSpec(style="darkroom", color="black", weight="medium", table="dark", output_ratio="4:5"), {})

    return [(cw, ch, list(items.values())) for (cw, ch), items in sorted(grouped.items())]


LAYOUT_BATCHES = _collect_layout_batches()


def test_shapes_ss_boundaries_and_synthetics(tmp_path: Path) -> None:
    synth_cases: list[tuple[int, int, list[dict[str, Any]], str]] = [
        (
            80,
            70,
            [
                {
                    "k": "rect",
                    "x": 4,
                    "y": 5,
                    "w": 30,
                    "h": 20,
                    "radius": 0,
                    "fill": (200, 50, 30),
                    "outline": (10, 20, 30),
                    "stroke_w": 2,
                    "alpha": 1.0,
                },
                {
                    "k": "rect",
                    "x": 10,
                    "y": 12,
                    "w": 42,
                    "h": 28,
                    "radius": 8.5,
                    "fill": (40, 180, 90),
                    "outline": (250, 240, 10),
                    "stroke_w": 2.5,
                    "alpha": 0.72,
                },
                {"k": "rect", "x": 2, "y": 2, "w": 1, "h": 1, "radius": 0, "fill": (255, 255, 255), "alpha": 0.5},
                {"k": "poly", "pts": [(5.2, 6.8), (60.4, 9.1), (48.9, 55.3), (8.1, 44.7)], "fill": (90, 120, 210), "alpha": 0.85},
                {"k": "ellipse", "cx": 38.5, "cy": 33.25, "r": 14.75, "fill": (230, 140, 20), "alpha": 0.65},
                {"k": "hole", "x": 15, "y": 15, "w": 26, "h": 22, "radius": 5.0},
                {
                    "k": "shadow",
                    "x": -4,
                    "y": 8,
                    "w": 40,
                    "h": 30,
                    "blur": 3.5,
                    "dx": 2,
                    "dy": -3,
                    "radius": 6.0,
                    "alpha": 0.45,
                    "color": (12, 24, 36),
                },
                {"k": "shadow", "x": 200, "y": 200, "w": 10, "h": 10, "blur": 2.0, "alpha": 0.5},
            ],
            "ss4",
        ),
        (
            700,
            500,
            [
                {
                    "k": "rect",
                    "x": 20,
                    "y": 20,
                    "w": 620,
                    "h": 450,
                    "radius": 18.0,
                    "fill": (210, 200, 190),
                    "outline": (30, 40, 50),
                    "stroke_w": 4,
                    "alpha": 0.9,
                },
                {"k": "hole", "x": 50, "y": 40, "w": 600, "h": 420, "radius": 12.0},
            ],
            "ss3",
        ),
        (
            1600,
            1000,
            [
                {"k": "rect", "x": 40, "y": 30, "w": 1500, "h": 920, "radius": 24.0, "fill": (180, 190, 200), "alpha": 0.8},
                {"k": "hole", "x": 60, "y": 50, "w": 1460, "h": 880, "radius": 16.0},
            ],
            "ss2",
        ),
    ]
    for idx, (w, h, items, tag) in enumerate(synth_cases):
        _run_batch(w, h, items, tmp_path, tag, 500 + idx)


PINNED_SS_COUNTEREXAMPLES: list[dict[str, Any]] = [
    {"k": "poly", "pts": [(38.0 / 3.0, -4.0), (11.5, 9.25), (-4.0, 15.0)], "fill": (210, 90, 40), "alpha": 0.85},
    {"k": "poly", "pts": [(3.25, 4.75), (59.5, 12.25), (27.75, 58.5)], "fill": (40, 180, 220), "alpha": 0.9},
    {"k": "poly", "pts": [(-6.5, 10.5), (68.5, 22.5), (31.5, 61.5), (4.5, 48.5)], "fill": (180, 60, 210), "alpha": 0.75},
    {"k": "poly", "pts": [(5.0, 5.0), (55.0, 7.0), (30.0, 31.0), (52.0, 54.0), (7.0, 52.0)], "fill": (220, 200, 40), "alpha": 0.6},
    {
        "k": "rect",
        "x": 4,
        "y": 4,
        "w": 40,
        "h": 30,
        "radius": 11.5,
        "fill": (100, 190, 80),
        "outline": (20, 30, 40),
        "stroke_w": 2.25,
        "alpha": 0.8,
    },
    {"k": "rect", "x": -5, "y": 10, "w": 45, "h": 35, "radius": 9.0, "fill": (230, 120, 70), "alpha": 0.7},
    {"k": "rect", "x": 12, "y": -6, "w": 36, "h": 42, "radius": 18.0, "outline": (250, 240, 220), "stroke_w": 3.0, "alpha": 1.0},
    {"k": "ellipse", "cx": 31.75, "cy": 29.25, "r": 16.5, "fill": (240, 80, 160), "alpha": 0.82},
    {"k": "ellipse", "cx": 2.5, "cy": 61.5, "r": 14.0, "fill": (50, 210, 140), "alpha": 0.68},
    {"k": "hole", "x": 10, "y": 12, "w": 38, "h": 34, "radius": 10.5},
    {"k": "shadow", "x": 8, "y": 10, "w": 40, "h": 32, "blur": 4.25, "dx": 3, "dy": -2, "radius": 8.0, "alpha": 0.55, "color": (10, 20, 30)},
    {"k": "shadow", "x": -8, "y": -6, "w": 30, "h": 28, "blur": 0.0, "dx": 4, "dy": 4, "radius": 4.0, "alpha": 0.4, "color": (0, 0, 0)},
]


@pytest.mark.parametrize("it", PINNED_SS_COUNTEREXAMPLES, ids=anon_ids(len(PINNED_SS_COUNTEREXAMPLES)))
def test_shapes_pinned_supersampled_counterexamples(it: dict[str, Any], tmp_path: Path) -> None:
    _run_batch(64, 64, [it], tmp_path, "pinned_ss", 701)


def test_shapes_300_randomized_supersampled_polygons(tmp_path: Path) -> None:
    rng = np.random.default_rng(2026111603)
    ss_polys: list[dict[str, Any]] = []
    for i in range(300):
        n_pts = int(rng.integers(3, 6))
        pts = [(round(float(x), 3), round(float(y), 3)) for x, y in rng.uniform(-16.0, 80.0, size=(n_pts, 2))]
        fill = tuple(int(v) for v in rng.integers(0, 256, size=3))
        alpha = round(float(rng.uniform(0.15, 1.0)), 3)
        ss_polys.append({"k": "poly", "pts": pts, "fill": fill, "alpha": alpha})
    for b in range(0, 300, 100):
        _run_batch(64, 64, ss_polys[b : b + 100], tmp_path, f"rand_ss_{b}", 800 + b)
    check(len(ss_polys) == 300, "ran 300 randomized supersampled polygons")


@pytest.mark.parametrize(
    ("cw", "ch", "items"),
    LAYOUT_BATCHES,
    ids=anon_ids(len(LAYOUT_BATCHES)),
)
def test_shapes_every_frame_layout_item(cw: int, ch: int, items: list[dict[str, Any]], tmp_path: Path) -> None:
    _run_batch(cw, ch, items, tmp_path, f"layout_{cw}x{ch}", 900 + cw + ch)


def test_shapes_error_paths(tmp_path: Path) -> None:
    l_img = np.zeros((10, 10), dtype=np.uint8)
    in_npy = tmp_path / "l.npy"
    np.save(in_npy, l_img)
    rgb_npy = tmp_path / "rgb.npy"
    np.save(rgb_npy, np.zeros((10, 10, 3), dtype=np.uint8))
    ops_p = tmp_path / "ops.json"
    out_p = tmp_path / "out.npy"
    ops_p.write_text(json.dumps([{"op": "paint_rect", "x": 0, "y": 0, "w": 5, "h": 5, "fill": [1, 2, 3]}]), encoding="utf-8")
    code, payload = run_json(["ink-ops", "--canvas", str(in_npy), "--mode", "L", "--ops", str(ops_p), "--out", str(out_p)])
    check(code != 0, "paint_rect on L mode fails")
    check(payload["error"]["kind"] == "value_error", "paint_rect on L error kind")

    hostile_ops = [
        [{"op": "paint_shadow", "x": 200, "y": 200, "w": 10, "h": 10, "blur": 1e300, "alpha": 0.5}],
        [{"op": "paint_shadow", "x": 0, "y": 0, "w": 10, "h": 10, "blur": -1.0, "alpha": 0.5}],
        [{"op": "paint_poly", "pts": [[1e30, 0.0], [0.0, 10.0], [10.0, 10.0]], "fill": [1, 2, 3], "alpha": 1.0}],
        [{"op": "paint_rect", "x": 0, "y": 0, "w": 8, "h": 8, "stroke_w": 1e30, "outline": [1, 2, 3], "alpha": 1.0}],
        [{"op": "paint_ellipse", "cx": 1e30, "cy": 0.0, "r": 5.0, "fill": [1, 2, 3], "alpha": 1.0}],
    ]
    for bad in hostile_ops:
        ops_p.write_text(json.dumps(bad), encoding="utf-8")
        c2, p2 = run_json(["ink-ops", "--canvas", str(rgb_npy), "--mode", "RGB", "--ops", str(ops_p), "--out", str(out_p)])
        check(c2 != 0 and p2["error"]["kind"] == "value_error", "hostile shape op fails cleanly with value_error")
