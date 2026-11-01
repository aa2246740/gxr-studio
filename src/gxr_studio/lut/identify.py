"""文件名关键词和 33 点灰轴的粗分，不把创意曲线当成确定的空间。"""

from __future__ import annotations

from typing import Any

import numpy as np

from .cube import Cube
from .spaces import DISPLAY

# 顺序沿用原服务：先匹配长名称。
GUESS = (
    ("f-log2", "flog2"),
    ("flog2", "flog2"),
    ("f-log", "flog"),
    ("flog", "flog"),
    ("l-log", "llog"),
    ("llog", "llog"),
    ("v-log", "vlog"),
    ("vlog", "vlog"),
    ("s-log3", "slog3"),
    ("slog3", "slog3"),
    ("apple log", "applelog"),
    ("applelog", "applelog"),
    ("srgb", "srgb"),
    ("rec709", "rec709"),
    ("rec.709", "rec709"),
    ("709", "rec709"),
)


def guess_space(name: str, title: str = "") -> tuple[str | None, str | None]:
    text = f"{name} {title}".lower()
    for key, space in GUESS:
        if key in text:
            return space, f"文件名/标题里有“{key}”"
    return None, None


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
    keyword, reason = guess_space(filename, cube.title)
    content, evidence = content_kind(cube)
    keyword_kind = None if keyword is None else "display" if keyword in DISPLAY else "log"
    if keyword is not None and content is not None:
        state = "known" if keyword_kind == content else "conflict"
        selected = keyword if state == "known" else None
    elif keyword is not None:
        state, selected = "guessed", keyword
    elif content == "display":
        state, selected = "guessed", "srgb"
    else:
        state, selected = "unknown", None
    return {
        "input_space": selected,
        "input_space_state": state,
        "input_space_manual": False,
        "input_space_guess": keyword,
        "guess_reason": reason,
        "guess_note": "请选择输入色彩空间" if selected is None else None,
        "space_evidence": {"keyword": keyword, "content": content, **evidence},
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
