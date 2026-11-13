"""Synthetic identify / _undeclared_choice test cases shared by LUT parity tests."""

from __future__ import annotations

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
