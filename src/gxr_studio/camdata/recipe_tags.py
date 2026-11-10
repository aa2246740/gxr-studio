"""EXIF Copyright 中的 GXR1 标签。Pillow 已去掉的末尾 NUL 不要求补回。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, TypedDict

from .catalog_names import UNIT_LABELS, recipe_name

_TAG = re.compile(r"GXR1 ([0-9a-fA-F]{10}) (28|50|MT) (0[1-9]|[1-9][0-9])(?: ([0-9A-I-]{10}))?")
MAX_TAG_BYTES = 47  # 含一个结尾 NUL；解码后的文本最多 46 字节
COPYRIGHT_FIELD_CHARS = 69  # 实拍 TIFF Copyright 以 ASCII 空格填满的固定字段，不改变标签自身上限
RECIPE_TAG_VERSION = 3  # Amount 延迟到匹配目录解码；旧索引用缓存 Copyright 重解码。

# g181 使用此顺序；与 Studio 控件 ID / g178 封存目录相同。
KNOB_ORDER = ("amount", "contrast", "highlight", "shadow", "fade", "saturation", "warmth", "tint", "split", "filter")
KNOB_DEFAULTS = {name: 100 if name == "amount" else 0 for name in KNOB_ORDER}
_ALPHABET = "0123456789ABCDEFGHI"


class RecipeTag(TypedDict):
    version: int
    catalog_identity: str
    unit: str
    position: int
    knob_code: str | None
    levels: dict[str, int | None] | None


def copyright_text(value: object) -> str | None:
    """保留普通版权的空格和文字；只去掉 TIFF 字符串的一个结尾 NUL。"""
    if isinstance(value, bytes):
        value = value.decode("latin-1")
    return value.removesuffix("\0") if isinstance(value, str) else None


def parse_recipe_tag(value: object) -> RecipeTag | None:
    if isinstance(value, bytes):
        if len(value) > COPYRIGHT_FIELD_CHARS + 1:
            return None
        try:
            value = value.decode("ascii")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str) or len(value) > COPYRIGHT_FIELD_CHARS + 1 or not value.isascii():
        return None
    text = value.removesuffix("\0")
    if len(text) == COPYRIGHT_FIELD_CHARS:
        text = text.rstrip(" ")
    if len(text) >= MAX_TAG_BYTES:
        return None
    match = _TAG.fullmatch(text)
    if match is None:
        return None
    levels: dict[str, int | None] | None = None
    if match[4] is not None:
        levels = {}
        for name, code in zip(KNOB_ORDER, match[4]):
            value = None if code == "-" else _ALPHABET.index(code)
            levels[name] = None if value is None or name == "amount" else value - 9
    return RecipeTag(
        version=RECIPE_TAG_VERSION, catalog_identity=match[1].lower(), unit=match[2], position=int(match[3]), knob_code=match[4], levels=levels
    )


def decoded_levels(tag: RecipeTag, amount_levels: Sequence[int] | None = None) -> dict[str, int | None] | None:
    if tag["levels"] is None:
        return None
    levels = dict(tag["levels"])
    code = (tag["knob_code"] or "-")[0]
    matches = [] if code == "-" else [level for level in (amount_levels or ()) if level // 10 == _ALPHABET.index(code)]
    levels["amount"] = matches[0] if len(matches) == 1 else None
    return levels


def recipe_payload(tag: RecipeTag | None, *, amount_levels: Sequence[int] | None = None) -> dict[str, Any] | None:
    if tag is None:
        return None
    name = recipe_name(tag["catalog_identity"], tag["unit"], tag["position"])
    levels = decoded_levels(tag, amount_levels)
    amount_unknown = bool(tag["knob_code"] and tag["knob_code"][0] != "-" and levels is not None and levels["amount"] is None)
    return {
        **tag,
        "levels": levels,
        "amount_unknown": amount_unknown,
        "name": name,
        "catalogued": name is not None,
        "unit_label": UNIT_LABELS[tag["unit"]],
        "changes": {key: value for key, value in (levels or {}).items() if value is not None and value != KNOB_DEFAULTS[key]},
    }
