"""本地功能开关。

默认值保持现有行为。覆盖写在应用数据目录的 `flags.json`（不进仓库）。
每个开关都要在代码里被读到；`scripts/dead_flags.py` 会对照登记表。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import Settings


@dataclass(frozen=True)
class Flag:
    name: str
    default: bool
    description: str
    added: str  # YYYY-MM-DD，给清理周期用


FLAGS: dict[str, Flag] = {
    "library_rebuild": Flag(
        name="library_rebuild",
        default=True,
        description="允许 POST /api/library/rebuild。默认打开，和没有这个开关时一样。",
        added="2026-10-30",
    ),
}


def flags_path(settings: Settings) -> Path:
    return settings.home_dir / "flags.json"


def _overrides(settings: Settings) -> dict[str, bool]:
    path = flags_path(settings)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, bool] = {}
    for key, value in raw.items():
        if key in FLAGS and isinstance(value, bool):
            out[key] = value
    return out


def flag_enabled(settings: Settings, name: str) -> bool:
    """登记过的开关。没写进 flags.json 就用默认值。"""
    flag = FLAGS[name]
    return _overrides(settings).get(name, flag.default)
