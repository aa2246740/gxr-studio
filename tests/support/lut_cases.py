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
    ("", "synthetic", "input_lİnear.cube", lambda x: 1 - x),
    ("# Output: Gamma 2.4\n", "synthetic", "input_lİnear.cube", lambda x: 1 - x),
    ("", "synthetic", "input_ſRGB.cube", lambda x: 1 - x),
    ("# inputtransferfunction: S-Log3\n", "synthetic", "input_ſRGB.cube", lambda x: 1 - x),
    ("", "synthetic", "ſRGB_to_VLog.cube", lambda x: 1 - x),
    ("", "synthetic", "ſlog_custom.cube", lambda x: 1 - x),
    ("", "synthetic", "xİlinear.cube", lambda x: 1 - x),
    ("", "synthetic", "linearK.cube", lambda x: 1 - x),
    ("", "synthetic", "input_vlog\nto_srgb.cube", lambda x: 1 - x),
    ("", "synthetic", "input_vlog\nsrgb.cube", lambda x: 1 - x),
    ("", "synthetic", "input_\nsrgb.cube", lambda x: 1 - x),
    ("", "synthetic", "input_\ninput_vlog.cube", lambda x: 1 - x),
    ("# İnput: linear\n", "synthetic", "plain.cube", lambda x: 1 - x),
    ("# ınput: sRGB\n", "synthetic", "plain.cube", lambda x: 1 - x),
    ("# İnput: V-Log\n", "synthetic", "plain.cube", lambda x: 1 - x),
    ("# İnput: linear; Output: sRGB\n", "synthetic", "plain.cube", lambda x: 1 - x),
    ("", "synthetic", "\u0345input_srgb_target_vlog.cube", lambda x: 1 - x),
    ("", "synthetic", "\u05b0input_srgb_target_vlog.cube", lambda x: 1 - x),
    ("", "synthetic", "VLog_to_Style_FLog_２.cube", lambda x: 1 - x),
    ("", "synthetic", "VLog_to_Style_FLog_٢.cube", lambda x: 1 - x),
    ("", "synthetic", "VLog_to_Style_FLog\n.cube", lambda x: 1 - x),
    ("", "synthetic", "VLog_to_Style_FLog\n\n.cube", lambda x: 1 - x),
    ("", "synthetic", "VLog_to_Style_\n_FLog\n.cube", lambda x: 1 - x),
]


THRESHOLD_CASES: list[tuple[str, bool, float, dict[str, Any]]] = [
    (
        "case-0-1",
        False,
        0.000859710772678478,
        {
            "input_space": "rec709",
            "output_space": "gamma24",
            "input_gamut": "vgamut",
            "output_gamut": "rec2020",
            "input_range": "legal",
            "output_range": "full",
        },
    ),
    (
        "case-0-3",
        False,
        -0.0006469059803890353,
        {
            "input_space": "srgb",
            "output_space": "rec709",
            "input_gamut": "sgamut3",
            "output_gamut": "sgamut3cine",
            "input_range": "legal",
            "output_range": "legal",
        },
    ),
    (
        "case-0-5",
        False,
        0.0010696582373039426,
        {"input_space": "gamma24", "output_space": "rec709", "input_range": "legal", "output_range": "legal", "output_gamut": "rec2020"},
    ),
    (
        "case-0-7",
        False,
        8.436415748521388e-05,
        {
            "input_space": "vlog",
            "output_space": "rec709",
            "input_gamut": "vgamut",
            "output_gamut": "sgamut3",
            "input_range": "legal",
            "output_range": "legal",
        },
    ),
    (
        "case-0-8",
        False,
        -0.001055648360661672,
        {
            "input_space": "slog3",
            "output_space": "gamma24",
            "input_gamut": "sgamut3cine",
            "output_gamut": "rec2020",
            "input_range": "full",
            "output_range": "legal",
        },
    ),
    (
        "case-0-9",
        False,
        -0.001037939657389417,
        {
            "input_space": "applelog",
            "output_space": "linear",
            "input_gamut": "rec2020",
            "output_gamut": "vgamut",
            "input_range": "legal",
            "output_range": "full",
        },
    ),
    (
        "case-1-0",
        True,
        -0.0015950376688570868,
        {
            "input_space": "srgb",
            "output_space": "srgb",
            "input_gamut": "rec2020",
            "output_gamut": "rec709",
            "input_range": "full",
            "output_range": "full",
        },
    ),
    (
        "case-1-1",
        True,
        -0.0007597779766745206,
        {
            "input_space": "rec709",
            "output_space": "gamma24",
            "input_gamut": "vgamut",
            "output_gamut": "rec2020",
            "input_range": "legal",
            "output_range": "full",
        },
    ),
    (
        "case-1-2",
        True,
        -0.000382188103990858,
        {
            "input_space": "gamma24",
            "output_space": "srgb",
            "input_gamut": "sgamut3cine",
            "output_gamut": "vgamut",
            "input_range": "full",
            "output_range": "legal",
        },
    ),
    (
        "case-1-3",
        True,
        -3.511699036318982e-05,
        {
            "input_space": "srgb",
            "output_space": "rec709",
            "input_gamut": "sgamut3",
            "output_gamut": "sgamut3cine",
            "input_range": "legal",
            "output_range": "legal",
        },
    ),
    (
        "case-1-4",
        True,
        -5.111796098881682e-05,
        {
            "input_space": "rec709",
            "output_space": "linear",
            "input_gamut": "rec709",
            "output_gamut": "rec2020",
            "input_range": "full",
            "output_range": "legal",
        },
    ),
    (
        "case-1-5",
        True,
        -0.0006887639229434095,
        {"input_space": "gamma24", "output_space": "rec709", "input_range": "legal", "output_range": "legal", "output_gamut": "rec2020"},
    ),
    (
        "case-1-6",
        True,
        0.00015799218710662035,
        {
            "input_space": "llog",
            "output_space": "srgb",
            "input_gamut": "rec2020",
            "output_gamut": "rec709",
            "input_range": "full",
            "output_range": "full",
        },
    ),
    (
        "case-1-7",
        True,
        0.00048005859595542043,
        {
            "input_space": "vlog",
            "output_space": "rec709",
            "input_gamut": "vgamut",
            "output_gamut": "sgamut3",
            "input_range": "legal",
            "output_range": "legal",
        },
    ),
    (
        "case-1-9",
        True,
        0.0009581451846035673,
        {
            "input_space": "applelog",
            "output_space": "linear",
            "input_gamut": "rec2020",
            "output_gamut": "vgamut",
            "input_range": "legal",
            "output_range": "full",
        },
    ),
]


AMPLIFIED_CASES: list[tuple[str, bool, float, dict[str, Any]]] = [
    (
        "amplified-24",
        True,
        0.0006716134621756898,
        {
            "input_space": "gamma24",
            "output_space": "srgb",
            "input_gamut": "rec709",
            "output_gamut": "sgamut3cine",
            "input_range": "full",
            "output_range": "full",
        },
    ),
    (
        "amplified-25",
        False,
        0.0013261247207500496,
        {
            "input_space": "gamma24",
            "output_space": "rec709",
            "input_gamut": "rec709",
            "output_gamut": "rec2020",
            "input_range": "legal",
            "output_range": "full",
        },
    ),
    (
        "amplified-35",
        False,
        -0.001849334008636518,
        {
            "input_space": "gamma24",
            "output_space": "applelog",
            "input_gamut": "rec709",
            "output_gamut": "rec2020",
            "input_range": "legal",
            "output_range": "legal",
        },
    ),
]

SHAPER_APPLELOG_CUBE = "LUT_1D_SIZE 2\n-3.1415926535897931 0.19927806966646006 0.3\n2.7058973220700397 0.19927806966646006 0.3\n"
SHAPER_APPLELOG_SPEC: dict[str, Any] = {"input_space": "srgb", "output_space": "applelog", "output_gamut": "rec2020"}
R5_PATHOLOGICAL_CUBE = "LUT_1D_SIZE 2\n-31415926535897.93 0.2 0.3\n27058973220700.398 0.2 0.3\n"


def shaper_applelog_rgb() -> np.ndarray:
    return np.array([[[137, 201, 83]]], dtype=np.uint8)


def shaper_zero_cases() -> list[tuple[str, str]]:
    """42 near-zero 1D shaper constructions around Apple Log's sqrt branch (Sol r4)."""
    import math

    from support.lut_oracle import inv_seq, matmul_t_seq, matrix_seq, seq_decode, seq_encode

    lin = seq_decode(shaper_applelog_rgb() / 255.0, "srgb")
    encoded = seq_encode(matmul_t_seq(matmul_t_seq(lin, matrix_seq("rec709")), inv_seq(matrix_seq("rec709"))), "srgb")
    x = float(encoded[0, 0, 0])
    cases: list[tuple[str, str]] = []
    for number, first in enumerate([-0.1, -0.2, -0.3, -0.7, -1.0, -math.pi]):
        second = first - first / x
        for shift in (-8, -2, -1, 0, 1, 2, 8):
            value = second + shift * abs(float(np.spacing(second)))
            cases.append((f"shaper-zero-{number}-{shift + 8}", f"LUT_1D_SIZE 2\n{first:.17g} 0.2 0.3\n{value:.17g} 0.2 0.3\n"))
    return cases


def steep_slope_lut_cases(kind: str) -> list[tuple[str, str, dict[str, Any]]]:
    """Sol r5 three steep-slope ill-conditioned LUT families (`1d`, `3d`, `1d3d`) across output spaces."""
    import math

    from gxr_studio.lut import spaces
    from support.lut_oracle import inv_seq, matmul_t_seq, matrix_seq, seq_decode, seq_encode

    lin = seq_decode(shaper_applelog_rgb() / 255.0, "srgb")
    encoded = seq_encode(matmul_t_seq(matmul_t_seq(lin, matrix_seq("rec709")), inv_seq(matrix_seq("rec709"))), "srgb")
    x = float(encoded[0, 0, 0])
    first = -math.pi * 1e15
    second = first - first / x
    cases: list[tuple[str, str, dict[str, Any]]] = []
    for output in spaces.SPACES:
        spec = {"input_space": "srgb", "output_space": output, "output_gamut": "rec2020"}
        for shift in (-2, 0, 2):
            end = second + shift * abs(float(np.spacing(second)))
            if kind == "1d":
                txt = f"LUT_1D_SIZE 2\n{first:.17g} 0.2 0.3\n{end:.17g} 0.2 0.3\n"
            elif kind in ("3d", "1d3d"):
                rows = ["LUT_1D_SIZE 2", "LUT_3D_SIZE 2", "0 0 0", "1 1 1"] if kind == "1d3d" else ["LUT_3D_SIZE 2"]
                for _b in range(2):
                    for _g in range(2):
                        for r in range(2):
                            rows.append(f"{(end if r else first):.17g} 0.2 0.3")
                txt = "\n".join(rows) + "\n"
            else:
                raise ValueError(f"unknown steep-slope family {kind!r}")
            cases.append((f"r5-steep-{kind}-{output}-s{shift + 2}", txt, spec))
    return cases


_R3_THRESHOLD_RGB_HEX = (
    "0000008d8800ff00ffffffff00ff00010101808080160eb6481338fe80a90f651edd8923d3751b014acbaa6dc5dbdef1b79f95402f260ebc439bfc23"
    "e04a8f2674dff0eb7caa1a35d49804241c2811758651c77f5638e48496703639cb98033fb22c8395a160c8448b28be199d0ae7b41c3df4b361fde98"
    "77e013f0043e4da1333ba1ceee837f251df7a54ea94d87e8f687f95835b0afd505deed0e977d2fe2c38c78d24de1fd1747528f8669225ffc0f69981"
    "bf3e6d8554d56001731ee9197341bacd518e6d2bcf8cc618123a2f86532cd8196c4c59d6a3ce27faa0c53c7c714c088e54959b1d7ffca7045be7748"
    "c74f4bacaf60699c7e7951ee94fad558bacadd94726d67c404099a8acccae0f5bcdf46e0b224bb0a61b38169a09e7186290d5df485bf8786bfae03c"
    "30afdec34a299aaaec915b9b3bbe6ba26c3897f631d1996788ed48d5ec441375da8a87616ed164fca35e07b15dc088895358d4db020e0112034b2f2"
    "aaa97bf1612379ee8ba585b623bb2888dd98f43c3c41511b5ded406beb79a3acd14598d8e51e58494af8a5c2f57c63663c1e7b3275e1c1cd3081d73"
    "5f936848a60cea2f2eb9f9e9fac31ebab313f7ba2268ddedb31e50e35bb59f203c963f8027d3d41ec7f4d3e52ddd5ce1c6dcd9bb7281a9dca231c66"
    "c7757b4f8383ae5d55e656de3c1bea54980f575d01b04f9761c2f575426dc8f06dca8d43f811979e8040e3ca592b088176052e66035f48a44ae1376e"
    "a62fa289ab28ee6714ecadefac93d94cea27444a046bc19236196bffeb2c10ce0aff5387c9360ecfd0d4af6f3f8c71b7fa53e8df7976eea04ccd6d8"
    "443c017d5cdbaab6802ed65c15ef8dad8822994fb6defb77bb6157cd5d9f41525bdaa854798d4b0bc3d463fe58503ed949ed3ea63b405354aa00a49"
    "c6fef0ae0a9993c0dd970f4bcec631e0961278e74368866b49abb9cab1e95e31a5609d2b6b179bb52d0618cd65feec33ffbc968397aa39cf6290815"
    "3fe57274e47673924651316f16e098e2816761e5d8dfd85e4f9920a893a435c344c54c7a145f21aa40a52678716374af83473836261977b07f19b3f9"
    "a388c7b2c07cae3fbcc2e8036d71d26373ff0ffdad2923496b058311cee83e0b70e16155878b26e959717b5650fd7dfc5a53fe71fbe1492dadf254d"
    "0cf667b6dd223c29b42c4474e5976bd8f2c1aef88adddba30eb3eb730f40ef6f8a238095c0cbeecce5f57b080801e424f13ee36de774096d0de8ff0"
    "6384a7f4d3a12976ed68ef2d87d80d9d13aae47eddb6f91c8aaf610977adaecf68af0119b2e7416e530a2554ff12ddde6eeafd10c7239596099c7e"
    "9346db35705f301b091d439923e1927da318fce4781f6274340853488ef76bf87d5cd0f9eb8c64737502a5501cc67a7d9ce7035a47ceee71a438020"
    "397225a8cddd574132febc931b2ee76e08e96a64845255e93497d1a970ac55556d14aa8b0b08207e29070fba06df82b22e6f0571872a11dac01cc12"
    "b150fd6c38c61fda7b7fcb4a7b71627a24d8cb74da962fd3a437f46b5963b515f2a2344ebfd59156ff667f9b4fe539f674b7a9bb8824d926bb5d2b6"
    "1e0ee24c72026624be0b80010fcd332190a09a5470d9df87c4feaecd00e2deadf0d2c514b60ae52aaa90eb6f9f26d143bd0ed4081833e6e8b866b7c"
    "7f5f3b4acd44b8a6739793ea2c76ea1bfdf7014c5dd1028e1f65f9cb45ba1434d63cd81e4a7614689399b599a92a5d2c771c6eb76a74cf48fbe3afb"
    "5cb968e9428e16dbfbabdece400835d025f7553bb76e6f78b744a01c82e1447881454fdcd1b32ec879372ed0fda0ab07235f4b930d914ba4c1862c8"
    "1b6128514ca47367c8c0a9e4aa9dab38a8a886f403315ed88e467d460e1b957a026b392712eb897aa0fcdd5c00d930b68337efc51bb5124e62c164a"
    "e817722ac5ae960d0f2e74f71867a7fd2f96176ab0007d457c3051779c8aca8fa1f81f808942c6d2f8b8fec8373b79e6d638783af0e6de8f3afa3b4"
    "9f4d5356139f12e4f38577b9a00a3f9846440a5195b4bdae1980d3599954e2bbe80874fe80f447b5b774fe55806998821c46c7a6e015b4e1d5ba9270"
    "34978a4184fb2212f154129b0983c87f0b2014df1fb2a81a3b0e448669319fb63532f0a7584dfa17a68949eb507322"
)


def r3_threshold_rgb() -> np.ndarray:
    return np.frombuffer(bytes.fromhex(_R3_THRESHOLD_RGB_HEX), dtype=np.uint8).reshape(16, 32, 3).copy()


BAD_CUBE_CASES: list[tuple[str, str]] = [
    ("hello\n", "line 1: unsupported directive hello"),
    ("LUT_3D_SIZE 2\n0 0 0\n", "LUT 数据行数与声明不符"),
    ("LUT_3D_SIZE 1\n0 0 0\n", "line 1: LUT size out of range"),
    ("LUT_3D_SIZE 130\n", "line 1: LUT size out of range"),
    ("LUT_1D_SIZE 65537\n", "line 1: LUT size out of range"),
    ("LUT_3D_SIZE 18446744073709551615\n", "line 1: LUT size out of range"),
    (f"LUT_3D_SIZE {'9' * 200}\n", "line 1: LUT size out of range"),
    ("LUT_3D_SIZE -5\n", "line 1: LUT size out of range"),
    ("LUT_3D_SIZE 1.5\n", "invalid literal for int()"),
    ("LUT_3D_SIZE _2\n", "invalid literal for int()"),
    ("LUT_3D_SIZE 2\n0 0 1_.0\n", "line 2: unsupported directive 0"),
    ("LUT_3D_SIZE 2\nLUT_3D_SIZE 2\n" + "0 0 0\n" * 8, "line 2: duplicate or late header"),
    ("LUT_3D_SIZE 2\n" + "0 0 nan\n" * 8, "line 2: expected three finite numbers"),
    ("LUT_3D_SIZE 2\nDOMAIN_MIN 0 0 inf\n" + "0 0 0\n" * 8, "line 2: non-finite header"),
    ("LUT_3D_SIZE 2\nDOMAIN_MIN 1 1 1\nDOMAIN_MAX 0 0 0\n" + "0 0 0\n" * 8, "DOMAIN_MAX"),
    ("LUT_3D_SIZE 2\nLUT_3D_INPUT_RANGE 1 0\n" + "0 0 0\n" * 8, "INPUT_RANGE"),
    ("LUT_3D_SIZE 2\nLUT_1D_INPUT_RANGE 0.5 0.5\n" + "0 0 0\n" * 8, "INPUT_RANGE"),
    ("LUT_3D_SIZE 2\nDOMAIN_MIN 0 0 0\nLUT_3D_INPUT_RANGE 0 1\n" + "0 0 0\n" * 8, "DOMAIN"),
    ('TITLE "unclosed\nLUT_3D_SIZE 2\n' + "0 0 0\n" * 8, "No closing quotation"),
]


def threshold_cube_text(offset: float, shaper: bool) -> str:
    ramps = np.linspace(0, 1, 9)
    nodes = np.array([[r, g, b] for b in ramps for g in ramps for r in ramps])
    rows = np.clip(nodes * [1.05, 0.96, 0.91] + [0.02 + offset, -0.01, 0.03], 0, 1)
    header = "LUT_1D_SIZE 5\nLUT_3D_SIZE 9\n" if shaper else "LUT_3D_SIZE 9\n"
    shape = "0 0 0\n0.20 0.22 0.24\n0.47 0.50 0.53\n0.76 0.78 0.80\n1 1 1\n" if shaper else ""
    return header + shape + "\n".join(" ".join(format(float(x), ".17g") for x in row) for row in rows) + "\n"


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


def write_wasm_threshold_table_manifest(tmp_path: Path, label: str) -> list[Path]:
    """Build a single 256^3 threshold table manifest so each WASM run stays well under 180s."""
    import json

    cube = tmp_path / f"{label}.cube"
    if label == "shaper-applelog":
        cube.write_text(SHAPER_APPLELOG_CUBE, encoding="utf-8")
        space = SHAPER_APPLELOG_SPEC
    else:
        shaper, offset, space = next((sh, off, sp) for lbl, sh, off, sp in (*THRESHOLD_CASES, *AMPLIFIED_CASES) if lbl == label)
        cube.write_text(threshold_cube_text(offset, shaper), encoding="utf-8")
    manifest = tmp_path / f"{label}-table.json"
    manifest.write_text(json.dumps({"kind": "table", "cube": str(cube), "space": space}), encoding="utf-8")
    return [manifest]


def write_wasm_lut_manifests(tmp_path: Path) -> list[Path]:
    """Build synthetic cubes, npy inputs, depth-limit specs, and JSON manifests for `wasm_cases/lut.mjs`."""
    import json

    v = np.linspace(0, 1, 17)
    nodes = np.clip(np.array([[r, g, b] for b in v for g in v for r in v]) * [0.86, 0.97, 1.05], 0, 1)
    shaper_cube = tmp_path / "shaper17.cube"
    shaper_cube.write_text(
        "LUT_1D_SIZE 3\nLUT_3D_SIZE 17\n0 0 0\n0.6 0.55 0.5\n1 1 1\n" + "\n".join(" ".join(f"{c:.17g}" for c in row) for row in nodes) + "\n",
        encoding="utf-8",
    )
    log_cube = tmp_path / "Leica_L-Log_warm.cube"
    log_cube.write_text(cube_text(lambda x: x * [0.95, 1.0, 1.05], size=5), encoding="utf-8")
    bad_cube = tmp_path / "bad.cube"
    bad_cube.write_text("LUT_3D_SIZE 2\nDOMAIN_MIN 0 0 0\nLUT_3D_INPUT_RANGE 0 1\n" + "0 0 0\n" * 8, encoding="utf-8")
    steep_cube = tmp_path / "finite-steep.cube"
    steep_cube.write_text("LUT_1D_SIZE 3\n0 0 0\n1e308 1e308 1e308\n1e308 1e308 1e308\n", encoding="utf-8")
    px64_npy = tmp_path / "px64_u8.npy"
    np.save(px64_npy, np.full((1, 1, 3), 64, dtype=np.uint8))

    rgb_u8 = np.array([[[10, 20, 30], [128, 64, 32]], [[200, 150, 100], [255, 255, 255]]], dtype=np.uint8)
    rgb_npy = tmp_path / "rgb_u8.npy"
    np.save(rgb_npy, rgb_u8)
    xyz_f64 = np.full((2, 2, 3), 0.18, dtype=np.float64)
    xyz_npy = tmp_path / "xyz_f64.npy"
    np.save(xyz_npy, xyz_f64)

    u_size = tmp_path / "underscore-size.cube"
    u_size.write_text("LUT_3D_SIZE 0_2\n" + "0 0 0\n" * 8, encoding="utf-8")
    u_float = tmp_path / "underscore-float.cube"
    u_float.write_text("LUT_3D_SIZE 2\n" + "0 0 0_0\n" * 8, encoding="utf-8")
    uni_size = tmp_path / "unicode-size.cube"
    uni_size.write_text("LUT_3D_SIZE ２\n" + "0 0 0\n" * 8, encoding="utf-8")
    huge64 = tmp_path / "huge64.cube"
    huge64.write_text("LUT_3D_SIZE 18446744073709551615\n", encoding="utf-8")

    inv2 = tmp_path / "inv2.cube"
    inv2.write_text(cube_text(lambda x: 1 - x, size=2, title="synthetic"), encoding="utf-8")
    id_dotted = tmp_path / "id-dotted.cube"
    id_dotted.write_text(cube_text(lambda x: 1 - x, size=2, title="synthetic", comments="# İnput: linear\n"), encoding="utf-8")
    th_rgb_npy = tmp_path / "th_rgb.npy"
    th_rgb = np.random.default_rng(42).integers(0, 256, (12, 10, 3), dtype=np.uint8)
    np.save(th_rgb_npy, th_rgb)
    r3_rgb_npy = tmp_path / "r3_rgb.npy"
    np.save(r3_rgb_npy, r3_threshold_rgb())
    shaper_al_cube = tmp_path / "shaper-applelog.cube"
    shaper_al_cube.write_text(SHAPER_APPLELOG_CUBE, encoding="utf-8")
    shaper_al_rgb_npy = tmp_path / "shaper_al_rgb.npy"
    np.save(shaper_al_rgb_npy, shaper_applelog_rgb())
    r5_reduced_cube = tmp_path / "r5-reduced.cube"
    r5_reduced_cube.write_text(R5_PATHOLOGICAL_CUBE, encoding="utf-8")
    r5_steep_cubes: list[tuple[Path, dict[str, Any]]] = []
    for _kind in ("1d", "3d", "1d3d"):
        _lbl, _txt, _sp = next(c for c in steep_slope_lut_cases(_kind) if c[0] == f"r5-steep-{_kind}-applelog-s2")
        _p = tmp_path / f"{_lbl}.cube"
        _p.write_text(_txt, encoding="utf-8")
        r5_steep_cubes.append((_p, _sp))
    th_map = {lbl: (sh, off, sp) for lbl, sh, off, sp in (*THRESHOLD_CASES, *AMPLIFIED_CASES)}
    th_01_cube = tmp_path / "case-0-1.cube"
    th_01_cube.write_text(threshold_cube_text(th_map["case-0-1"][1], th_map["case-0-1"][0]), encoding="utf-8")
    th_10_cube = tmp_path / "case-1-0.cube"
    th_10_cube.write_text(threshold_cube_text(th_map["case-1-0"][1], th_map["case-1-0"][0]), encoding="utf-8")
    th_14_cube = tmp_path / "case-1-4.cube"
    th_14_cube.write_text(threshold_cube_text(th_map["case-1-4"][1], th_map["case-1-4"][0]), encoding="utf-8")
    amp_cubes: list[tuple[str, Path, dict[str, Any]]] = []
    for amp_lbl in ("amplified-24", "amplified-25", "amplified-35"):
        amp_sh, amp_off, amp_sp = th_map[amp_lbl]
        amp_p = tmp_path / f"{amp_lbl}.cube"
        amp_p.write_text(threshold_cube_text(amp_off, amp_sh), encoding="utf-8")
        amp_cubes.append((amp_lbl, amp_p, amp_sp))
    manifests: list[Path] = [
        shaper_cube,
        log_cube,
        bad_cube,
        u_size,
        u_float,
        uni_size,
        huge64,
        r5_reduced_cube,
        *(p for p, _sp in r5_steep_cubes),
    ]
    cases_specs: list[dict[str, Any]] = [
        {"kind": "table", "cube": str(shaper_cube), "space": {"input_space": "srgb", "output_space": "vlog"}},
        {"kind": "table", "cube": str(log_cube), "space": {"input_space": "llog", "output_space": "gamma24"}},
        {"kind": "table", "cube": str(shaper_cube), "space": {"input_space": "llog", "output_space": "srgb"}},
        {
            "kind": "apply_u8",
            "cube": str(shaper_cube),
            "space": {"input_space": "srgb", "output_space": "vlog"},
            "rgb_npy": str(rgb_npy),
            "height": 2,
            "width": 2,
        },
        {
            "kind": "apply_u8",
            "cube": str(log_cube),
            "space": {"input_space": "llog", "output_space": "srgb"},
            "rgb_npy": str(rgb_npy),
            "xyz_npy": str(xyz_npy),
            "height": 2,
            "width": 2,
        },
        {
            "kind": "apply_u8",
            "cube": str(log_cube),
            "space": {"input_space": "llog", "output_space": "srgb"},
            "rgb_npy": str(rgb_npy),
            "height": 2,
            "width": 2,
        },
        {"kind": "identify", "cube": str(inv2), "name": "input_lİnear.cube"},
        {"kind": "identify", "cube": str(inv2), "name": "input_ſRGB.cube"},
        {"kind": "identify", "cube": str(inv2), "name": "input_vlog\nto_srgb.cube"},
        {"kind": "identify", "cube": str(inv2), "name": "input_vlog\nsrgb.cube"},
        {"kind": "identify", "cube": str(id_dotted), "name": "plain.cube"},
        {"kind": "identify", "cube": str(inv2), "name": "\u0345input_srgb_target_vlog.cube"},
        {"kind": "identify", "cube": str(inv2), "name": "VLog_to_Style_FLog_２.cube"},
        {"kind": "identify", "cube": str(inv2), "name": "VLog_to_Style_FLog\n.cube"},
        {"kind": "apply_u8", "cube": str(th_01_cube), "space": th_map["case-0-1"][2], "rgb_npy": str(th_rgb_npy), "height": 12, "width": 10},
        {"kind": "apply_u8", "cube": str(th_10_cube), "space": th_map["case-1-0"][2], "rgb_npy": str(th_rgb_npy), "height": 12, "width": 10},
        {"kind": "apply_u8", "cube": str(th_14_cube), "space": th_map["case-1-4"][2], "rgb_npy": str(th_rgb_npy), "height": 12, "width": 10},
        *(
            {"kind": "apply_u8", "cube": str(amp_p), "space": amp_sp, "rgb_npy": str(r3_rgb_npy), "height": 16, "width": 32}
            for _amp_lbl, amp_p, amp_sp in amp_cubes
        ),
        {
            "kind": "apply_u8",
            "cube": str(shaper_al_cube),
            "space": SHAPER_APPLELOG_SPEC,
            "rgb_npy": str(shaper_al_rgb_npy),
            "height": 1,
            "width": 1,
        },
        {
            "kind": "apply_u8",
            "cube": str(r5_reduced_cube),
            "space": SHAPER_APPLELOG_SPEC,
            "rgb_npy": str(shaper_al_rgb_npy),
            "height": 1,
            "width": 1,
        },
        *(
            {"kind": "apply_u8", "cube": str(_p), "space": _sp, "rgb_npy": str(shaper_al_rgb_npy), "height": 1, "width": 1}
            for _p, _sp in r5_steep_cubes
        ),
        {
            "kind": "apply_u8",
            "cube": str(steep_cube),
            "space": {"input_space": "srgb"},
            "rgb_npy": str(px64_npy),
            "height": 1,
            "width": 1,
        },
    ]
    huge1 = write_huge_npy_header(tmp_path / "huge-4294967295.npy", 4294967295, 4294967295)
    huge2 = write_huge_npy_header(tmp_path / "huge-2147483648.npy", 2147483648, 1)
    cases_specs.extend(
        [
            {
                "kind": "apply_u8",
                "cube": str(shaper_cube),
                "space": {"input_space": "srgb", "output_space": "srgb"},
                "rgb_npy": str(huge1),
                "height": 4294967295,
                "width": 4294967295,
            },
            {
                "kind": "apply_u8",
                "cube": str(shaper_cube),
                "space": {"input_space": "srgb", "output_space": "srgb"},
                "rgb_npy": str(huge2),
                "height": 2147483648,
                "width": 1,
            },
        ]
    )
    for kind in ("array", "object"):
        for depth in (128, 129, 20_000):
            sp = tmp_path / f"space-{kind}-{depth}.json"
            sp.write_text(space_depth_text(kind, depth), encoding="utf-8")
            cases_specs.append({"kind": "table", "cube": str(log_cube), "space_file": str(sp)})
            cases_specs.append(
                {"kind": "apply_u8", "cube": str(shaper_cube), "space_file": str(sp), "rgb_npy": str(rgb_npy), "height": 2, "width": 2}
            )
    for kind in ("bare_array", "bare_object"):
        sp = tmp_path / f"space-{kind}-129.json"
        sp.write_text(space_depth_text(kind, 129), encoding="utf-8")
        cases_specs.append({"kind": "table", "cube": str(shaper_cube), "space_file": str(sp)})

    for i, item in enumerate(cases_specs):
        p = tmp_path / f"case-{i}.json"
        p.write_text(json.dumps(item), encoding="utf-8")
        manifests.append(p)
    return manifests


def assert_rel(python: np.ndarray, rust: np.ndarray, *, label: str = "value") -> None:
    import pytest

    assert python.shape == rust.shape, f"{label}: shape {python.shape} != {rust.shape}"
    assert python.dtype == np.float64 and rust.dtype == np.float64
    py_nan = np.isnan(python)
    rs_nan = np.isnan(rust)
    one_nan = py_nan ^ rs_nan
    inf_bad = (np.isinf(python) | np.isinf(rust)) & (python != rust)
    finite = np.isfinite(python) & np.isfinite(rust)
    limit = np.where(finite, 1e-12 * np.maximum(1.0, np.abs(python)), 0.0)
    diff = np.zeros_like(python)
    diff[finite] = np.abs(python[finite] - rust[finite])
    bad = one_nan | inf_bad | (finite & ~(diff <= limit))
    if np.any(bad):
        idx = tuple(int(v) for v in np.argwhere(bad)[0])
        pytest.fail(f"{label} differs at {idx}: python={python[idx]!r} rust={rust[idx]!r} diff={diff[idx]!r}")


def assert_meta_same(py_meta: dict[str, Any], rs_meta: dict[str, Any]) -> None:
    assert set(py_meta) == set(rs_meta)
    for key, py_val in py_meta.items():
        rs_val = rs_meta[key]
        if key in ("domain_min", "domain_max", "range3", "xyz_to_input", "out_to_srgb"):
            assert_rel(np.asarray(py_val, np.float64), np.asarray(rs_val, np.float64), label=f"meta.{key}")
        else:
            assert py_val == rs_val, f"meta.{key}: {py_val!r} != {rs_val!r}"


def real_dngs(captures_dir: Path, cap: int = 60) -> list[Path]:
    if not captures_dir.is_dir():
        return []
    found = [
        path for path in captures_dir.rglob("*") if path.is_file() and path.suffix.upper() == ".DNG" and "backup" not in path.as_posix().lower()
    ]
    found.sort(key=lambda path: path.as_posix())
    if len(found) <= cap:
        return found
    return [found[(i * len(found)) // cap] for i in range(cap)]


def check_scene_xyz(dng_path: Path, codes: np.ndarray, out_path: Path) -> None:
    from gxr_studio.gpu.source import affine_of
    from gxr_studio.lut.scene import SceneError, scene_colour
    from gxr_studio.raw import open_dng
    from gxr_studio.raw.dng import RawDecodeError
    from support.core_cli import error_of, run_json

    py_raw_err: RawDecodeError | None = None
    try:
        dng = open_dng(dng_path)
        try:
            col = scene_colour(dng)
            py_xyz = col.xyz(codes)
            py_aff = affine_of(col)
            py_err: SceneError | None = None
        finally:
            dng.release()
    except RawDecodeError as exc:
        py_xyz, py_aff, py_err, py_raw_err = None, None, None, exc
    except SceneError as exc:
        py_xyz, py_aff, py_err = None, None, exc

    codes_npy = out_path.with_name("codes.npy")
    np.save(codes_npy, codes)
    code, payload = run_json(["scene-xyz", "--dng", str(dng_path), "--codes", str(codes_npy), "--out", str(out_path)])
    rs_err = error_of(code, payload)

    if py_raw_err is not None:
        assert rs_err is not None and rs_err.kind == "raw_decode" and rs_err.message == str(py_raw_err)
        return
    if py_err is not None:
        assert rs_err is not None and rs_err.kind == py_err.code and rs_err.message == py_err.message
        return

    assert rs_err is None and py_xyz is not None
    rs_xyz = np.load(out_path)
    assert_rel(py_xyz, rs_xyz, label=f"scene-xyz({dng_path.name})")

    if py_aff is not None:
        assert payload["affine"] is not None
        assert_rel(np.asarray(py_aff["A"], np.float64), np.asarray(payload["affine"]["A"], np.float64), label="affine.A")
        assert_rel(np.asarray(py_aff["b"], np.float64), np.asarray(payload["affine"]["b"], np.float64), label="affine.b")
        # Also verify affine_of matches xyz within 1e-12 relative (measured up to 44 ULP)
        aff_xyz = codes @ np.asarray(payload["affine"]["A"], np.float64).T + np.asarray(payload["affine"]["b"], np.float64)
        assert_rel(rs_xyz, aff_xyz, label="affine_of vs xyz")
