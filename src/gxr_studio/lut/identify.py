"""先读空间声明，再读输入域关键词；灰轴只核对猜测，不为无声明的 LUT 选空间。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import numpy as np

from .cube import Cube
from .spaces import DISPLAY

IDENTIFICATION_VERSION = 2
PATTERNS = (
    (r"s[-_ ]?log3[-_ ]s[-_ ]?gamut3(?!cine)", "slog3-sgamut3"),
    (r"f[-_ ]?log2", "flog2"),
    (r"f[-_ ]?log", "flog"),
    (r"l[-_ ]?log709", "llog709"),
    (r"l[-_ ]?log", "llog"),
    (r"v[-_ ]?log", "vlog"),
    (r"s[-_ ]?log3", "slog3"),
    (r"apple[-_ ]?log", "applelog"),
    (r"srgb", "srgb"),
    (r"(?:rec|bt)[ ._-]*709", "rec709"),
    (r"gamma[ _-]*2[._ ]?4|bt[ ._-]*1886", "gamma24"),
    (r"linear", "linear"),
)
FIELD = re.compile(
    r"(?:^|;)\s*(input|output)(?:[ _-]*(?:colou?r[ _-]*space|space|transfer(?:[ _-]*function)?|gamma))?\b\s*[:=]?\s*",
    re.I,
)


def _spaces(text: str) -> dict[str, str]:
    # A primary/gamut name does not declare its transfer function.
    text = re.sub(r"(?:rec[ ._-]*709|srgb)\s+(?:primaries|gamut)", "", text, flags=re.I)
    found = {}
    occupied: list[tuple[int, int]] = []
    for pattern, space in PATTERNS:
        for match in re.finditer(r"(?<![a-z0-9])(?:" + pattern + r")(?![a-z0-9])", text, re.I):
            if any(start <= match.start() < end for start, end in occupied):
                continue
            found[space] = match[0]
            occupied.append(match.span())
    return found


def _output_spaces(text: str) -> dict[str, str]:
    found = {space: word for space, word in _spaces(text).items() if space in DISPLAY}
    # Rec.709 + BT1886 / Gamma 2.4 names the primaries and the transfer separately.
    if "gamma24" in found:
        found.pop("rec709", None)
    return found


def _declarations(cube: Cube) -> tuple[list[str], list[str], list[str]]:
    inputs: list[str] = []
    outputs: list[str] = []
    other: list[str] = []
    for comment in cube.comments:
        lumix = re.fullmatch(r"LUMIXPHOTOSTYLE\s+(.+)", comment, re.I)
        if lumix:
            inputs.append(lumix[1])
            continue
        fields = list(FIELD.finditer(comment))
        if not fields:
            other.append(comment)
        for index, match in enumerate(fields):
            end = fields[index + 1].start() if index + 1 < len(fields) else len(comment)
            value = comment[match.end() : end]
            if re.search(r"gamma", match[0], re.I):
                value = "Gamma " + value
            (inputs if match[1].lower() == "input" else outputs).append(value)
    return inputs, outputs, other


def _named_input(name: str, title: str) -> dict[str, str]:
    named = (title, Path(name).stem)
    marked = {}
    # Inspect explicit input labels in BOTH sources before interpreting any
    # conversion/style name. Neither TITLE nor filename wins by iteration order.
    for text in named:
        match = re.search(r"\b(?:input|from)[ :=_-]+(.+)", text, re.I)
        if match:
            domain = re.split(r"[;]|\bto\b|_to_|->|[_ -]+(?:style|look|target)[ :=_-]+", match[1], maxsplit=1, flags=re.I)[0]
            marked.update(_spaces(domain))
    # V-Log Alchemy names a style's source before _to_, and its actual input
    # domain after the style: FLog2C_to_Style_VLog.
    destination = re.split(r"_to_", named[1], maxsplit=1, flags=re.I)
    suffix = re.search(r".+[_ ]((?:v|f|l|s)[- ]?log\d?|apple[- ]?log)(?:[_ ](?:srgb|rec[ ._-]*709|\d+))*$", destination[-1], re.I)
    if len(destination) > 1 and suffix:
        marked.update(_spaces(suffix[1]))
    if marked:
        return marked
    directional = {}
    for text in named:
        source = re.split(r"(?:\bto\b|_to_|->)", text, maxsplit=1, flags=re.I)
        if len(source) > 1:
            directional.update(_spaces(source[0]))
    if directional:
        return directional
    found = {}
    for text in named:
        found.update(_spaces(text))
    logs = {space: word for space, word in found.items() if space not in DISPLAY}
    return logs or found


def guess_space(name: str, title: str = "") -> tuple[str | None, str | None]:
    found = _named_input(name, title)
    if len(found) == 1:
        space, word = next(iter(found.items()))
        return space, f"文件名/标题里有“{word}”"
    return None, None


def _output(cube: Cube, filename: str, declarations: list[str], other: list[str]) -> tuple[str, str, dict[str, str]]:
    found = {}
    if declarations:
        for text in declarations:
            found.update(_output_spaces(text))
        state = "known"
    else:
        for text in (cube.title, Path(filename).stem, *other):
            # A source display space in sRGB_to_VLog is not the output.
            destination = re.split(r"(?:\bto\b|_to_|->)", text, flags=re.I)[-1]
            found.update(_output_spaces(destination))
        state = "guessed"
    if len(found) == 1:
        return next(iter(found)), state, found
    return "srgb", "conflict" if len(found) > 1 else "unknown", found


def content_kind(cube: Cube) -> tuple[str | None, dict[str, Any]]:
    x = np.linspace(0, 1, 33)
    with np.errstate(all="ignore"):
        gray = cube.apply(np.repeat(x[:, None], 3, axis=1))
        y = gray.mean(axis=1)
    if not np.isfinite(gray).all() or not np.isfinite(y).all():
        return None, {"black": None, "white": None, "middle": None, "mid_slope": None, "channel_spread": None}
    with np.errstate(all="ignore"):
        slopes = np.diff(y) * 32
        mid_slope = float((y[18] - y[14]) * 8)
        spread = float(np.max(np.ptp(gray, axis=1)))
    if not np.isfinite(slopes).all() or not np.isfinite(mid_slope) or not np.isfinite(spread):
        return None, {"black": None, "white": None, "middle": None, "mid_slope": None, "channel_spread": None}
    info = {"black": float(y[0]), "white": float(y[-1]), "middle": float(y[16]), "mid_slope": mid_slope, "channel_spread": spread}
    # 有限、单调、近中性，且两端仍覆盖主要显示范围才有分类依据。
    if not np.isfinite(gray).all() or np.min(slopes) < -0.01 or spread > 0.08 or abs(y[0]) > 0.06 or not 0.80 <= y[-1] <= 1.20:
        return None, info
    # 中间特别陡、两端同时压平的强 S 曲线不作为 Log 证据。
    if mid_slope > 1.5 and slopes[:4].mean() < 0.45 and slopes[-4:].mean() < 0.45:
        return None, info
    if mid_slope > 1.5:
        return "log", info
    if 0.40 <= y[16] <= 0.55:
        return "display", info
    return None, info


def identify(cube: Cube, filename: str) -> dict[str, Any]:
    inputs, outputs, other = _declarations(cube)
    named = _named_input(filename, cube.title)
    declared = {}
    unsupported = False
    for text in inputs:
        found = _spaces(text)
        unsupported |= not found
        declared.update(found)
    candidates = declared if inputs else named
    keyword = next(iter(candidates)) if len(candidates) == 1 else None
    reason = f"输入色彩空间：{' / '.join(inputs)}" if inputs else guess_space(filename, cube.title)[1]
    content, evidence = content_kind(cube)
    keyword_kind = None if keyword is None else "display" if keyword in DISPLAY else "log"
    if inputs:
        state = "known" if keyword is not None and not unsupported else "conflict" if declared else "unknown"
        selected = keyword if state == "known" else None
    elif len(candidates) > 1:
        state, selected = "conflict", None
    elif keyword is not None and content is not None:
        state = "known" if keyword_kind == content else "conflict"
        selected = keyword if state == "known" else None
    elif keyword is not None:
        state, selected = "guessed", keyword
    else:
        state, selected = "unknown", None
    output, output_state, output_evidence = _output(cube, filename, outputs, other)
    return {
        "identification_version": IDENTIFICATION_VERSION,
        "input_space": selected,
        "input_space_state": state,
        "input_space_manual": False,
        "input_space_guess": keyword,
        "guess_reason": reason,
        "guess_note": "请选择输入色彩空间" if selected is None else None,
        "space_evidence": {"keyword": keyword, "content": content, "input_declarations": inputs, "output": output_evidence, **evidence},
        "output_space": output,
        "output_space_state": output_state,
        "technical": _technical(cube),
    }


def _technical(cube: Cube) -> bool:
    # 同一输入通道值换到不同颜色中，输出应仍仅取决于这个通道。
    values = np.array([0.125, 0.375, 0.625, 0.875])
    probes = np.array([[r, g, b] for r in values for g in values for b in values])
    with np.errstate(all="ignore"):
        out = cube.apply(probes)
        reference = cube.apply(np.repeat(values[:, None], 3, axis=1))
    if not np.isfinite(out).all() or not np.isfinite(reference).all() or np.max(reference) > 1.2 or np.min(reference) < -0.1:
        return False
    if np.max(np.ptp(reference, axis=1)) > 0.015 or np.any(np.diff(reference, axis=0) < -0.001):
        return False
    for channel in range(3):
        for i, value in enumerate(values):
            if np.max(np.abs(out[probes[:, channel] == value, channel] - reference[i, channel])) > 0.005:
                return False
    return True
