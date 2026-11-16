"""Parity: gxr-cli ImageDraw primitives on L and RGB against Pillow 12.2.0 (Item 2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image, ImageDraw

from support.core_cli import run_json, run_npy
from support.export_pipeline_cases import anon_ids, check, same_pixels


def _cli_ops(canvas: Image.Image, ops: list[dict[str, Any]], tmp_path: Path, tag: str) -> np.ndarray:
    in_npy = tmp_path / f"{tag}_in.npy"
    ops_path = tmp_path / f"{tag}_ops.json"
    out_npy = tmp_path / f"{tag}_out.npy"
    np.save(in_npy, np.asarray(canvas, dtype=np.uint8))
    ops_path.write_text(json.dumps(ops), encoding="utf-8")
    return run_npy(["ink-ops", "--canvas", str(in_npy), "--mode", canvas.mode, "--ops", str(ops_path)], out_npy)


def _blank(mode: str, w: int = 41, h: int = 33) -> Image.Image:
    return Image.new(mode, (w, h), 17 if mode == "L" else (11, 22, 33))


def _ink(mode: str, rgb: tuple[int, int, int]) -> Any:
    return rgb[0] if mode == "L" else list(rgb)


def _pil_ink(mode: str, rgb: tuple[int, int, int]) -> Any:
    return rgb[0] if mode == "L" else rgb


POLY_CASES: list[list[list[float]]] = [
    [[5.0, 5.0], [31.0, 7.0], [27.0, 25.0], [8.0, 22.0]],
    [(x - 2) * 4 / 3.0 + 0.25 for x in []] if False else [[3.25, 4.75], [34.8, 6.1], [29.4, 28.9], [4.1, 21.3]],
    [[-6.4, -3.2], [18.9, 5.5], [12.3, 44.1], [-8.0, 19.7]],
    [[5.0, 5.0], [5.0, 5.0]],
    [[5.0, 5.0], [5.0, 5.0], [5.0, 5.0]],
    [[4.0, 10.0], [20.0, 10.0], [35.0, 10.0]],
    [[10.0, 4.0], [10.0, 18.0], [10.0, 29.0]],
    [[6.0, 6.0], [18.0, 6.0], [30.0, 6.0], [24.0, 24.0], [12.0, 14.0]],
    [[6.0, 6.0], [32.0, 12.0], [14.0, 28.0], [28.0, 6.0], [8.0, 26.0]],
    # Pinned _polygon_generic FMA edge_x_f32 counterexample (Sol r1):
    [[38.0, -13.0], [35.0, 9.0], [-12.0, 15.0]],
]


@pytest.mark.parametrize("mode", ["L", "RGB"], ids=["L", "RGB"])
@pytest.mark.parametrize("pts", POLY_CASES, ids=anon_ids(len(POLY_CASES)))
def test_draw_polygon_fill(mode: str, pts: list[list[float]], tmp_path: Path) -> None:
    canvas = _blank(mode)
    pil = canvas.copy()
    ImageDraw.Draw(pil).polygon([tuple(p) for p in pts], fill=_pil_ink(mode, (210, 120, 45)))
    got = _cli_ops(canvas, [{"op": "draw_polygon", "pts": pts, "fill": _ink(mode, (210, 120, 45))}], tmp_path, "poly")
    same_pixels(got, np.asarray(pil, dtype=np.uint8), "ink draw_polygon")


RECT_BOXES: list[list[float]] = [
    [4.0, 5.0, 25.0, 21.0],
    [3.25, 4.75, 29.99, 24.12],
    [12.0, 8.0, 39.0 - 1.0, 29.0 - 1.0],
    [-5.5, -4.5, 16.5, 14.5],
    [30.0, 20.0, 55.0, 45.0],
    [50.0, 50.0, 60.0, 60.0],
    [10.0, 10.0, 10.0, 10.0],
    [10.0, 10.0, 10.0, 20.0],
]


@pytest.mark.parametrize("mode", ["L", "RGB"], ids=["L", "RGB"])
@pytest.mark.parametrize("box", RECT_BOXES, ids=anon_ids(len(RECT_BOXES)))
@pytest.mark.parametrize("width", [0, 1, 2, 5, 12], ids=anon_ids(5))
def test_draw_rectangle(mode: str, box: list[float], width: int, tmp_path: Path) -> None:
    canvas = _blank(mode)
    pil = canvas.copy()
    ImageDraw.Draw(pil).rectangle(
        tuple(box),
        fill=_pil_ink(mode, (40, 180, 90)),
        outline=_pil_ink(mode, (220, 30, 70)),
        width=width,
    )
    got = _cli_ops(
        canvas,
        [
            {
                "op": "draw_rectangle",
                "box": box,
                "fill": _ink(mode, (40, 180, 90)),
                "outline": _ink(mode, (220, 30, 70)),
                "width": width,
            }
        ],
        tmp_path,
        "rect",
    )
    same_pixels(got, np.asarray(pil, dtype=np.uint8), "ink draw_rectangle")


def test_draw_rectangle_same_fill_and_outline(tmp_path: Path) -> None:
    canvas = _blank("L")
    pil = canvas.copy()
    ImageDraw.Draw(pil).rectangle((4, 4, 24, 20), fill=99, outline=99, width=3)
    got = _cli_ops(
        canvas,
        [{"op": "draw_rectangle", "box": [4, 4, 24, 20], "fill": 99, "outline": 99, "width": 3}],
        tmp_path,
        "rect_same",
    )
    same_pixels(got, np.asarray(pil, dtype=np.uint8), "ink draw_rectangle same ink")


RR_CASES: list[tuple[list[float], float, list[bool] | None]] = [
    ([4.0, 4.0, 34.0, 26.0], 0.0, None),
    ([4.0, 4.0, 34.0, 26.0], 6.0, None),
    ([3.4, 5.6, 35.4 - 1.0, 27.6 - 1.0], 7.5, None),
    ([4.0, 4.0, 20.0, 26.0], 12.0, None),
    ([4.0, 4.0, 34.0, 18.0], 12.0, None),
    ([4.0, 4.0, 24.0, 24.0], 25.0, None),
    ([-4.2, -3.8, 28.1, 22.7], 8.0, None),
    ([4.0, 4.0, 34.0, 26.0], 6.0, [True, False, True, False]),
    ([4.0, 4.0, 34.0, 26.0], 6.0, [False, False, False, False]),
    ([4.0, 4.0, 20.0, 26.0], 6.0, [True, True, False, True]),
]


@pytest.mark.parametrize("mode", ["L", "RGB"], ids=["L", "RGB"])
@pytest.mark.parametrize("case", RR_CASES, ids=anon_ids(len(RR_CASES)))
@pytest.mark.parametrize("width", [0, 1, 2, 4, 9, 12], ids=anon_ids(6))
def test_draw_rounded_rectangle(
    mode: str,
    case: tuple[list[float], float, list[bool] | None],
    width: int,
    tmp_path: Path,
) -> None:
    box, radius, corners = case
    canvas = _blank(mode)
    pil = canvas.copy()
    ImageDraw.Draw(pil).rounded_rectangle(
        tuple(box),
        radius=radius,
        fill=_pil_ink(mode, (190, 160, 50)),
        outline=_pil_ink(mode, (30, 80, 220)),
        width=width,
        corners=tuple(corners) if corners is not None else None,
    )
    op: dict[str, Any] = {
        "op": "draw_rounded_rectangle",
        "box": box,
        "radius": radius,
        "fill": _ink(mode, (190, 160, 50)),
        "outline": _ink(mode, (30, 80, 220)),
        "width": width,
    }
    if corners is not None:
        op["corners"] = corners
    got = _cli_ops(canvas, [op], tmp_path, "rr")
    same_pixels(got, np.asarray(pil, dtype=np.uint8), "ink draw_rounded_rectangle")


ELLIPSE_CASES: list[list[float]] = [
    [4.0, 5.0, 28.0, 23.0],
    [3.3, 4.7, 30.8, 25.2],
    [10.0, 10.0, 10.0, 10.0],
    [10.0, 10.0, 11.0, 10.0],
    [-6.5, -4.5, 19.5, 17.5],
    [25.0, 18.0, 50.0, 44.0],
]


@pytest.mark.parametrize("mode", ["L", "RGB"], ids=["L", "RGB"])
@pytest.mark.parametrize("box", ELLIPSE_CASES, ids=anon_ids(len(ELLIPSE_CASES)))
def test_draw_ellipse_fill(mode: str, box: list[float], tmp_path: Path) -> None:
    canvas = _blank(mode)
    pil = canvas.copy()
    ImageDraw.Draw(pil).ellipse(tuple(box), fill=_pil_ink(mode, (120, 210, 240)))
    got = _cli_ops(
        canvas,
        [{"op": "draw_ellipse", "box": box, "fill": _ink(mode, (120, 210, 240))}],
        tmp_path,
        "ellipse",
    )
    same_pixels(got, np.asarray(pil, dtype=np.uint8), "ink draw_ellipse")


LINE_CASES: list[list[list[float]]] = [
    [[2.0, 5.0], [35.0, 5.0]],
    [[15.0, 2.0], [15.0, 29.0]],
    [[3.0, 3.0], [29.0, 29.0]],
    [[35.0, 26.0], [4.0, 7.0]],
    [[4.2, 27.8], [12.9, 2.1], [37.5, 18.4]],
    [[-5.0, -5.0], [45.0, 38.0]],
    [[12.0, 12.0], [12.0, 12.0]],
    [[12.0, 12.0]],
]


@pytest.mark.parametrize("mode", ["L", "RGB"], ids=["L", "RGB"])
@pytest.mark.parametrize("pts", LINE_CASES, ids=anon_ids(len(LINE_CASES)))
@pytest.mark.parametrize("width", list(range(0, 13)), ids=anon_ids(13))
def test_draw_line_widths(mode: str, pts: list[list[float]], width: int, tmp_path: Path) -> None:
    canvas = _blank(mode)
    pil = canvas.copy()
    ImageDraw.Draw(pil).line([tuple(p) for p in pts], fill=_pil_ink(mode, (240, 90, 180)), width=width)
    got = _cli_ops(
        canvas,
        [{"op": "draw_line", "pts": pts, "fill": _ink(mode, (240, 90, 180)), "width": width}],
        tmp_path,
        "line",
    )
    same_pixels(got, np.asarray(pil, dtype=np.uint8), "ink draw_line")


PINNED_PRIMITIVE_COUNTEREXAMPLES: list[dict[str, Any]] = [
    {"op": "draw_polygon", "pts": [[38, -13], [35, 9], [-12, 15]], "fill": [211, 90, 45]},
    {"op": "draw_polygon", "pts": [[3.5, 4.5], [47.5, 11.5], [19.5, 31.5]], "fill": [40, 200, 120]},
    {"op": "draw_polygon", "pts": [[-10, -5], [40, 7], [22, 33], [-4, 18]], "fill": [180, 60, 210]},
    {"op": "draw_polygon", "pts": [[1, 1], [39, 2], [20, 17], [38, 31], [2, 30]], "fill": [90, 140, 220]},
    {"op": "draw_line", "pts": [[2.0, 3.0], [37.0, 29.0]], "fill": [255, 80, 40], "width": 5},
    {"op": "draw_line", "pts": [[39.0, 1.0], [2.0, 24.0]], "fill": [50, 240, 180], "width": 7},
    {"op": "draw_line", "pts": [[-4.0, 15.0], [45.0, 16.0]], "fill": [210, 210, 30], "width": 9},
    {"op": "draw_line", "pts": [[11.0, -3.0], [19.0, 36.0]], "fill": [30, 170, 250], "width": 6},
    {"op": "draw_line", "pts": [[5.0, 5.0], [23.0, 29.0], [39.0, 7.0]], "fill": [230, 120, 200], "width": 4},
    {"op": "draw_rounded_rectangle", "box": [3.5, 3.5, 35.5, 27.5], "radius": 9.5, "fill": [100, 180, 90], "outline": [20, 30, 40], "width": 2},
    {
        "op": "draw_rounded_rectangle",
        "box": [2.0, 2.0, 38.0, 30.0],
        "radius": 11.0,
        "fill": [200, 150, 70],
        "outline": [15, 15, 15],
        "width": 5,
        "corners": [True, False, True, False],
    },
    {
        "op": "draw_rounded_rectangle",
        "box": [-3.0, -2.0, 26.0, 24.0],
        "radius": 10.0,
        "outline": [240, 220, 50],
        "width": 3,
        "corners": [False, True, True, True],
    },
    {"op": "draw_ellipse", "box": [2.5, 2.5, 37.5, 29.5], "fill": [175, 85, 215]},
    {"op": "draw_rectangle", "box": [-4.5, 2.5, 33.5, 28.5], "fill": [60, 190, 210], "outline": [250, 50, 50], "width": 4},
    {"op": "draw_rectangle", "box": [1.0, 1.0, 15.0, 15.0], "outline": [255, 255, 255], "width": 100},
]


@pytest.mark.parametrize("op", PINNED_PRIMITIVE_COUNTEREXAMPLES, ids=anon_ids(len(PINNED_PRIMITIVE_COUNTEREXAMPLES)))
def test_pinned_primitive_counterexamples(op: dict[str, Any], tmp_path: Path) -> None:
    canvas = _blank("RGB", 48, 40)
    pil = canvas.copy()
    d = ImageDraw.Draw(pil)
    kind = op["op"]
    if kind == "draw_polygon":
        d.polygon([tuple(p) for p in op["pts"]], fill=tuple(op["fill"]))
    elif kind == "draw_line":
        d.line([tuple(p) for p in op["pts"]], fill=tuple(op["fill"]), width=op["width"])
    elif kind == "draw_rounded_rectangle":
        d.rounded_rectangle(
            tuple(op["box"]),
            radius=op["radius"],
            fill=tuple(op["fill"]) if "fill" in op else None,
            outline=tuple(op["outline"]) if "outline" in op else None,
            width=op.get("width", 1),
            corners=tuple(op["corners"]) if "corners" in op else None,
        )
    elif kind == "draw_ellipse":
        d.ellipse(tuple(op["box"]), fill=tuple(op["fill"]))
    elif kind == "draw_rectangle":
        d.rectangle(
            tuple(op["box"]),
            fill=tuple(op["fill"]) if "fill" in op else None,
            outline=tuple(op["outline"]) if "outline" in op else None,
            width=op.get("width", 1),
        )
    got = _cli_ops(canvas, [op], tmp_path, "pinned")
    same_pixels(got, np.asarray(pil, dtype=np.uint8), "ink pinned primitive counterexample")


def _apply_pil_op(d: ImageDraw.ImageDraw, op: dict[str, Any]) -> None:
    kind = op["op"]
    if kind == "draw_polygon":
        d.polygon([tuple(p) for p in op["pts"]], fill=tuple(op["fill"]))
    elif kind == "draw_line":
        d.line([tuple(p) for p in op["pts"]], fill=tuple(op["fill"]), width=op["width"])
    elif kind == "draw_rectangle":
        d.rectangle(
            tuple(op["box"]),
            fill=tuple(op["fill"]),
            outline=tuple(op["outline"]),
            width=op["width"],
        )
    elif kind == "draw_rounded_rectangle":
        d.rounded_rectangle(
            tuple(op["box"]),
            radius=op["radius"],
            fill=tuple(op["fill"]),
            outline=tuple(op["outline"]),
            width=op["width"],
            corners=tuple(op["corners"]),
        )
    else:
        d.ellipse(tuple(op["box"]), fill=tuple(op["fill"]))


def test_draw_1800_primitives_and_100k_polys_and_wide_lines(tmp_path: Path) -> None:
    rng = np.random.default_rng(20261116)
    cw, ch = 64, 64

    # Part A: 1,800 mixed primitives (360 of each kind) across 3 batches of 600 ops.
    mixed_ops: list[dict[str, Any]] = []
    for i in range(1800):
        ink1 = [int(v) for v in rng.integers(0, 256, size=3)]
        ink2 = [int(v) for v in rng.integers(0, 256, size=3)]
        k = i % 5
        if k == 0:
            n_pts = int(rng.integers(3, 6))
            pts = [[int(x), int(y)] for x, y in rng.integers(-24, 88, size=(n_pts, 2))]
            mixed_ops.append({"op": "draw_polygon", "pts": pts, "fill": ink1})
        elif k == 1:
            n_pts = int(rng.integers(2, 4))
            pts = [[int(x), int(y)] for x, y in rng.integers(-24, 88, size=(n_pts, 2))]
            w = int(rng.integers(0, 14))
            mixed_ops.append({"op": "draw_line", "pts": pts, "fill": ink1, "width": w})
        elif k == 2:
            x0, y0 = [int(v) for v in rng.integers(-16, 48, size=2)]
            x1 = x0 + int(rng.integers(0, 48))
            y1 = y0 + int(rng.integers(0, 48))
            w = int(rng.integers(0, 8))
            mixed_ops.append({"op": "draw_rectangle", "box": [x0, y0, x1, y1], "fill": ink1, "outline": ink2, "width": w})
        elif k == 3:
            x0, y0 = [int(v) for v in rng.integers(-10, 40, size=2)]
            dx = int(rng.integers(12, 44))
            dy = int(rng.integers(12, 44))
            corners = [bool(c) for c in rng.integers(0, 2, size=4)]
            max_r = min(dx, dy) / 2.0 if all(corners) else (min(dx, dy) - 2) / 4.0
            rad = float(rng.uniform(0.0, max(0.0, max_r)))
            w = int(rng.integers(0, 6))
            mixed_ops.append(
                {
                    "op": "draw_rounded_rectangle",
                    "box": [x0, y0, x0 + dx, y0 + dy],
                    "radius": round(rad, 2),
                    "fill": ink1,
                    "outline": ink2,
                    "width": w,
                    "corners": corners,
                }
            )
        else:
            x0, y0 = [int(v) for v in rng.integers(-16, 48, size=2)]
            x1 = x0 + int(rng.integers(0, 48))
            y1 = y0 + int(rng.integers(0, 48))
            mixed_ops.append({"op": "draw_ellipse", "box": [x0, y0, x1, y1], "fill": ink1})

    pil = _blank("RGB", cw, ch)
    for b_idx in range(0, len(mixed_ops), 600):
        batch = mixed_ops[b_idx : b_idx + 600]
        rs_arr = _cli_ops(pil, batch, tmp_path, f"mix_{b_idx}")
        d = ImageDraw.Draw(pil)
        for op in batch:
            _apply_pil_op(d, op)
        same_pixels(rs_arr, np.asarray(pil, dtype=np.uint8), "ink 1800 mixed primitives batch")

    # Part B: 100,000 randomized polygons (50,000) + wide lines (50,000) in 20 batches of 5,000.
    total_count = 0
    pil_big = _blank("RGB", cw, ch)
    for b in range(20):
        batch: list[dict[str, Any]] = []
        d = ImageDraw.Draw(pil_big)
        rs_in = pil_big.copy()
        for j in range(5000):
            ink = [int(v) for v in rng.integers(0, 256, size=3)]
            if (b * 5000 + j) % 2 == 0:
                pts = [[int(x), int(y)] for x, y in rng.integers(-24, 88, size=(3, 2))]
                op = {"op": "draw_polygon", "pts": pts, "fill": ink}
                d.polygon([tuple(p) for p in pts], fill=tuple(ink))
            else:
                pts = [[int(x), int(y)] for x, y in rng.integers(-24, 88, size=(2, 2))]
                w = int(rng.integers(2, 12))
                op = {"op": "draw_line", "pts": pts, "fill": ink, "width": w}
                d.line([tuple(p) for p in pts], fill=tuple(ink), width=w)
            batch.append(op)
        rs_arr = _cli_ops(rs_in, batch, tmp_path, f"bulk_{b}")
        same_pixels(rs_arr, np.asarray(pil_big, dtype=np.uint8), "ink 100k polygons and wide lines")
        total_count += len(batch)
    check(total_count == 100_000, "ran 100,000 randomized polygons and wide lines")


def test_draw_error_paths(tmp_path: Path) -> None:
    rgba = Image.new("RGBA", (10, 10), (0, 0, 0, 255))
    in_npy = tmp_path / "rgba.npy"
    np.save(in_npy, np.asarray(rgba, dtype=np.uint8))
    ops_p = tmp_path / "ops.json"
    bad_ops = [
        ("rgba", [{"op": "draw_rectangle", "box": [0, 0, 5, 5], "fill": [1, 2, 3]}]),
        ("x1_lt_x0", [{"op": "draw_rectangle", "box": [5, 0, 4, 5], "fill": [1, 2, 3]}]),
        ("y1_lt_y0", [{"op": "draw_ellipse", "box": [0, 5, 5, 4], "fill": [1, 2, 3]}]),
        ("ellipse_65536", [{"op": "draw_ellipse", "box": [0, 0, 65536, 65536], "fill": [1, 2, 3]}]),
        ("ellipse_huge", [{"op": "draw_ellipse", "box": [0, 0, 1e30, 1e30], "fill": [1, 2, 3]}]),
        ("rr_bad", [{"op": "draw_rounded_rectangle", "box": [5, 0, 4, 5], "radius": 2.0, "fill": [1, 2, 3]}]),
        ("rr_neg_r", [{"op": "draw_rounded_rectangle", "box": [0, 0, 10, 10], "radius": -5.0, "fill": [1, 2, 3]}]),
        (
            "rr_partial_1e300",
            [
                {
                    "op": "draw_rounded_rectangle",
                    "box": [0, 0, 10, 10],
                    "radius": 1e300,
                    "corners": [True, True, False, True],
                    "fill": [1, 2, 3],
                }
            ],
        ),
        (
            "rr_partial_overflow",
            [
                {
                    "op": "draw_rounded_rectangle",
                    "box": [4, 4, 20, 26],
                    "radius": 15.0,
                    "corners": [True, True, False, True],
                    "fill": [1, 2, 3],
                }
            ],
        ),
        ("line_huge", [{"op": "draw_line", "pts": [[0, 0], [1e30, 0]], "fill": [1, 2, 3], "width": 5}]),
        ("poly_1pt", [{"op": "draw_polygon", "pts": [[1, 1]], "fill": [1, 2, 3]}]),
        ("poly_huge", [{"op": "draw_polygon", "pts": [[0, 0], [1e30, 0], [0, 10]], "fill": [1, 2, 3]}]),
    ]
    for idx, (tag, ops) in enumerate(bad_ops):
        p = in_npy if tag == "rgba" else tmp_path / "rgb.npy"
        if tag != "rgba" and not p.exists():
            np.save(p, np.asarray(_blank("RGB", 10, 10), dtype=np.uint8))
        ops_p.write_text(json.dumps(ops), encoding="utf-8")
        out_p = tmp_path / f"err_{idx}.npy"
        code, payload = run_json(["ink-ops", "--canvas", str(p), "--ops", str(ops_p), "--out", str(out_p)])
        check(code != 0, "bad draw op fails")
        check(payload["error"]["kind"] == "value_error", "bad draw op kind")
