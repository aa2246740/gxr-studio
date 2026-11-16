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


def test_draw_error_paths(tmp_path: Path) -> None:
    rgba = Image.new("RGBA", (10, 10), (0, 0, 0, 255))
    in_npy = tmp_path / "rgba.npy"
    np.save(in_npy, np.asarray(rgba, dtype=np.uint8))
    ops_p = tmp_path / "ops.json"
    bad_ops = [
        ("rgba", [{"op": "draw_rectangle", "box": [0, 0, 5, 5], "fill": [1, 2, 3]}]),
        ("x1_lt_x0", [{"op": "draw_rectangle", "box": [5, 0, 4, 5], "fill": [1, 2, 3]}]),
        ("y1_lt_y0", [{"op": "draw_ellipse", "box": [0, 5, 5, 4], "fill": [1, 2, 3]}]),
        ("rr_bad", [{"op": "draw_rounded_rectangle", "box": [5, 0, 4, 5], "radius": 2.0, "fill": [1, 2, 3]}]),
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
        ("poly_1pt", [{"op": "draw_polygon", "pts": [[1, 1]], "fill": [1, 2, 3]}]),
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
