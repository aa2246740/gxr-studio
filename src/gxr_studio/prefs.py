"""界面偏好存在应用数据目录的 prefs.json。

首次引导被跳过或走完之后不再自动打开。导出面板的 HDR 开关沿用上次的选择。
读缺失或读不出时用默认值，不建文件。写入走 `durable_write`（PrivateDir 原子替换）。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .lut.library import durable_write
from .privacy import PrivateDir

SCHEMA = "gxr-studio/prefs/1"
_NAME = "prefs.json"
_BOOL_KEYS = ("onboarding_dismissed", "export_hdr")
HALFFRAME_MODES = ("diptych", "singles", "both")
_lock = threading.Lock()


def defaults() -> dict[str, Any]:
    return {"onboarding_dismissed": False, "export_hdr": False, "export_halfframe": "diptych"}


def _coerce(doc: Any) -> dict[str, Any]:
    values = defaults()
    if not isinstance(doc, dict):
        return values
    for key in _BOOL_KEYS:
        value = doc.get(key, values[key])
        if isinstance(value, bool):
            values[key] = value
    hf_mode = doc.get("export_halfframe")
    if isinstance(hf_mode, str) and hf_mode in HALFFRAME_MODES:
        values["export_halfframe"] = hf_mode
    return values


def _read(directory: PrivateDir) -> dict[str, Any]:
    if not directory.exists(_NAME):
        return defaults()
    try:
        doc = json.loads(directory.read(_NAME))
    except (OSError, ValueError, UnicodeError):
        return defaults()
    return _coerce(doc)


def load_prefs(home: Path, forbidden: Iterable[Path] = ()) -> dict[str, Any]:
    """目录还不存在时直接给默认值，避免为了读而把应用数据目录建出来。"""
    if not Path(home).exists():
        return defaults()
    with _lock, PrivateDir(home, "prefs", forbidden) as directory:
        return _read(directory)


def update_prefs(home: Path, forbidden: Iterable[Path], changes: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        if not changes and not Path(home).exists():
            return defaults()
        with PrivateDir(home, "prefs", forbidden) as directory:
            current = _read(directory)
            if not changes:
                return current
            current.update(changes)
            durable_write(directory, _NAME, {"schema": SCHEMA, **current})
            return dict(current)
