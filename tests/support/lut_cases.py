"""Synthetic identify / _undeclared_choice test cases shared by LUT parity tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

IDENTIFY_CASES: list[tuple[str, str, str, Any]] = [
    ("#LUMIXPHOTOSTYLE VLOG\n", "FLog2C_to_Style", "FLog2C_to_Style_VLog.cube", lambda x: x),
    ("# Input: Panasonic V-Log / V-Gamut\n", "L-Log_to_Style", "L-Log_to_Style_VLog.cube", lambda x: x),
    ("# INPUT_COLORSPACE V-Log\n", "S-Log3_style", "S-Log3_style_VLog.cube", lambda x: x),
    ("# Input Space = sRGB\n# Output: Rec709\n", "V-Log style", "L-Log.cube", lambda x: 1 - x),
    ("# input_colour_space: S-Log3\n", "FLog2C style", "vlog.cube", lambda x: 1 - x),
    ("# Input: linear\n", "synthetic", "FLog2C.cube", lambda x: 1 - x),
    ("", "V-Log to sRGB", "FLog2C_Style.cube", lambda x: 1 - x),
    ("", "sRGB to V-Log", "vlog_style.cube", lambda x: 1 - x),
    ("", "synthetic", "FLog2C_to_Style_VLog.cube", lambda x: 1 - x),
    ("", "synthetic", "Leica_L-Log_warm.cube", lambda x: 1 - x),
    ("", "Input V-Log / V-Gamut; Target FLog2C", "film.cube", lambda x: 1 - x),
    ("", "synthetic", "input_srgb_target_FLog2C.cube", lambda x: 1 - x),
    ("# Input: V-Log; Output: sRGB\n", "FLog2C style", "film.cube", lambda x: 1 - x),
    ("", "L-Log_to_style", "input_vlog.cube", lambda x: 1 - x),
    ("", "L-Log_to_style", "FLog2C_to_Style_VLog.cube", lambda x: 1 - x),
    ("# Output: BT.709\n", "synthetic", "film.cube", lambda x: 1 - x),
    ("# OUTPUT_COLORSPACE Rec.709 primaries / D65 / BT.709 OETF\n", "synthetic", "film.cube", lambda x: 1 - x),
    ("# OUTPUT_COLORSPACE Rec.709 primaries / D65 / Gamma 2.4\n", "synthetic", "film.cube", lambda x: 1 - x),
    ("# Output transfer function: BT1886\n", "synthetic", "film_sRGB.cube", lambda x: 1 - x),
    ("# Output: sRGB\n", "synthetic", "film_Rec709.cube", lambda x: 1 - x),
    ("# Output gamma: 2.4\n", "synthetic", "film.cube", lambda x: 1 - x),
    ("# Gamma 2.4\n", "synthetic", "film.cube", lambda x: 1 - x),
    ("", "synthetic", "Film_Rec709_BT1886_VLog.cube", lambda x: 1 - x),
    ("# Input: sRGB\n", "synthetic", "sRGB_to_VLog.cube", lambda x: 1 - x),
    ("# Created by Synthetic Author\n", "Synthetic creator", "Creative_1234.cube", lambda x: x),
    ("# Input: V-Log\n# Input: F-Log2\n", "synthetic", "vlog.cube", lambda x: x),
    ("# Input: unsupported-input\n", "synthetic", "vlog.cube", lambda x: x),
    ("#LUMIXPHOTOSTYLE VLOG\n# Input: unsupported\n", "synthetic", "vlog.cube", lambda x: x),
    ("", "input_vlog", "input_srgb.cube", lambda x: 1 - x),
    ("", "synthetic", "FLog2C.cube", lambda x: x),
    ("# Input: V-Log\n# Output: sRGB\n# Output: BT709\n", "synthetic", "film.cube", lambda x: x),
    ("", "film", "srgb.cube", lambda x: x),
    ("", "film", "vlog.cube", lambda x: np.clip(x * 1.6, 0, 1)),
    ("", "film", "plain.cube", lambda x: np.clip(x * 1.6, 0, 1)),
    ("", "film", "plain.cube", lambda x: 1 / (1 + np.exp(-14 * (x - 0.5)))),
    ("", "film", "plain.cube", lambda x: x**1.8),
    ("", "film", "plain.cube", lambda x: x[:, [1, 2, 0]]),
    ("# Creative look for V-Log\n", "film", "film.cube", lambda x: 1 - x),
    ("", "warm film", "warm_film.cube", lambda x: 1 - x),
    ("", "Analog Street", "Analog_Street.cube", lambda x: 1 - x),
    ("", "Catalog look", "catalog.cube", lambda x: 1 - x),
    ("# Output: Rec.709\n", "warm film", "warm_film.cube", lambda x: 1 - x),
    ("# sRGB look\n", "FLog2C style", "FLog2C.cube", lambda x: 1 - x),
    ("# Author: V-Log master\n", "film", "film.cube", lambda x: 1 - x),
]


def space_depth_text(kind: str, depth: int) -> str:
    """Build a RenderSpec JSON with `extra` nested to `depth` levels (root object is level 1)."""
    if kind == "array":
        inner = "[" * (depth - 1) + "]" * (depth - 1)
    elif kind == "object":
        inner = '{"a":' * (depth - 1) + "null" + "}" * (depth - 1)
    elif kind == "bare_array":
        return "[" * depth + "]" * depth
    elif kind == "bare_object":
        return '{"a":' * depth + "null" + "}" * depth
    else:
        raise ValueError(kind)
    return '{"input_space":"srgb","extra":' + inner + "}"


def cube_text(fun: Any = lambda x: x, *, size: int = 17, title: str = "synthetic", comments: str = "", extra_headers: str = "") -> str:
    v = np.linspace(0, 1, size)
    b, g, r = np.meshgrid(v, v, v, indexing="ij")
    nodes = np.stack([r, g, b], axis=-1).reshape(-1, 3)
    rows = fun(nodes)
    title_line = f'TITLE "{title}"\n' if title is not None else ""
    return (
        comments + title_line + f"LUT_3D_SIZE {size}\n" + extra_headers + "\n".join(" ".join(f"{c:.17g}" for c in row) for row in rows) + "\n"
    )


def write_huge_npy_header(path: Path, height: int, width: int) -> Path:
    body = f"{{'descr': '|u1', 'fortran_order': False, 'shape': ({height}, {width}, 3), }}"
    pad = (64 - (10 + len(body) + 1) % 64) % 64
    header = body.encode("latin1") + b" " * pad + b"\n"
    path.write_bytes(b"\x93NUMPY\x01\x00" + len(header).to_bytes(2, "little") + header)
    return path
