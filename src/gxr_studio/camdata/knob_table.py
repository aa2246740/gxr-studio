"""从已由 datcodec 校验的 R2/R3 块读取整数档位和剂量，名称沿用公开菜单。"""

from __future__ import annotations

import struct
from typing import Any

from .datcodec import u16, u32


def read(block: bytes, menu: dict[str, Any]) -> dict[str, Any]:
    compact = block[:8] == b"GXKV2R3\0"
    recipes = []
    for i in range(u16(block, 22)):
        at = u32(block, 36) + 16 * i
        recipes.append({"unit": block[at], "slot": block[at + 1], "first_item": u16(block, at + 4), "item_count": block[at + 2]})
    items = []
    for i in range(u16(block, 24)):
        at = u32(block, 40) + 16 * i
        control, count = block[at : at + 2]
        lo, hi, default = struct.unpack_from(">3h", block, at + 2)
        step, points_at = block[at + 9], u32(block, at + 12)
        points = []
        for j in range(count):
            if compact:
                level = min(j * step, 150) if control == 0 else lo + j
                dose = struct.unpack_from(">h", block, points_at + 2 * j)[0]
            else:
                level, dose = struct.unpack_from(">2h", block, points_at + 4 * j)
            points.append({"level": level, "dose": dose})
        items.append(
            {
                "control": control,
                "point_count": count,
                "min_level": lo,
                "max_level": hi,
                "default_level": default,
                "cap_lower": block[at + 8] & 1,
                "cap_upper": (block[at + 8] >> 1) & 1,
                "amount_step": step,
                "points": points,
            }
        )
    return {**menu, "recipes": recipes, "items": items}
