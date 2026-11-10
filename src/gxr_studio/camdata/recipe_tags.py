"""EXIF Copyright 中的 GXR1 标签。Pillow 已去掉的末尾 NUL 不要求补回。"""

from __future__ import annotations

import re
from typing import Any, TypedDict

from .catalog_names import CATALOG_NAMES, UNIT_LABELS, recipe_name

_TAG = re.compile(r"GXR1 ([0-9a-fA-F]{10}) (28|50|MT) (0[1-9]|[1-9][0-9])(?: ([!-~](?:[ -~]*[!-~])?))?")
MAX_TAG_BYTES = 47  # 含一个结尾 NUL；解码后的文本最多 46 字节


class RecipeTag(TypedDict):
    catalog_identity: str
    unit: str
    position: int
    short_name: str | None
    tweaked: bool


def copyright_text(value: object) -> str | None:
    """保留普通版权的空格和文字；只去掉 TIFF 字符串的一个结尾 NUL。"""
    if isinstance(value, bytes):
        value = value.decode("latin-1")
    return value.removesuffix("\0") if isinstance(value, str) else None


def parse_recipe_tag(value: object) -> RecipeTag | None:
    if isinstance(value, bytes):
        if len(value) > MAX_TAG_BYTES:
            return None
        try:
            value = value.decode("ascii")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str) or len(value) > MAX_TAG_BYTES or not value.isascii():
        return None
    text = value.removesuffix("\0")
    if len(text) >= MAX_TAG_BYTES:
        return None
    tweaked = text.endswith(" *")
    match = _TAG.fullmatch(text[:-2] if tweaked else text)
    if match is None or "*" in (match[4] or ""):
        return None
    return RecipeTag(catalog_identity=match[1].lower(), unit=match[2], position=int(match[3]), short_name=match[4], tweaked=tweaked)


def recipe_payload(tag: RecipeTag | None) -> dict[str, Any] | None:
    if tag is None:
        return None
    name = recipe_name(tag["catalog_identity"], tag["unit"], tag["position"])
    known_catalog = tag["catalog_identity"] in CATALOG_NAMES
    return {
        **tag,
        "name": name if known_catalog else tag["short_name"],
        "catalogued": name is not None,
        "unit_label": UNIT_LABELS[tag["unit"]],
    }
