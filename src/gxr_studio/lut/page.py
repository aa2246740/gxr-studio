"""LUT 页的选择建议。只描述默认选项，不改已存空间或颜色计算。"""

from __future__ import annotations

import re
from typing import Any

from .identify import FIELD, guess_space


def input_choice(item: dict[str, Any]) -> dict[str, Any]:
    space = item.get("input_space")
    evidence = item.get("space_evidence", {})
    declarations = evidence.get("input_declarations", [])
    if item.get("input_space_manual"):
        source, note = "manual", "使用你选择的输入色彩空间。"
    elif space and declarations:
        source, note = "declared", "按 LUT 文件写明的输入色彩空间选择。"
    elif space:
        source, note = "guessed", "根据文件名、标题或注释推测，请核对 LUT 的说明。"
    else:
        space, source, note = _undeclared_choice(item, evidence, declarations)
    return {"default_input_space": space, "default_input_source": source, "default_input_note": note}


def _undeclared_choice(item: dict[str, Any], evidence: dict[str, Any], declarations: list[Any]) -> tuple[str | None, str, str]:
    if item.get("input_space_state") == "conflict":
        return None, "required", "文件里的颜色信息不一致，请按 LUT 的说明选择。"
    if declarations:
        return None, "required", "暂时无法识别文件写明的输入颜色，请按 LUT 的说明选择。"
    naming = f"{item.get('filename', '')} {item.get('title', '')}"
    if re.search(r"log|linear|gamma", naming, re.I) and not guess_space(item.get("filename", ""), item.get("title", ""))[0]:
        return None, "required", "无法确定这个 LUT 的输入颜色，请按 LUT 的说明选择。"
    comments = [
        text
        for text in item.get("comments", [])
        if not any(match[1].lower() == "output" for match in FIELD.finditer(text))
        and not re.match(r"^(?:source|author|license|来源|作者|许可)\s*:", text, re.I)
    ]
    guesses = {space for text in comments if (space := guess_space("", text)[0])}
    if len(guesses) == 1:
        space = next(iter(guesses))
        kind = "display" if space in ("srgb", "rec709", "gamma24") else "log"
        if evidence.get("content") in (None, kind):
            return space, "guessed", "根据文件注释推测，请核对 LUT 的说明。"
    text = " ".join([item.get("filename", ""), item.get("title", ""), *comments])
    if guesses or evidence.get("content") == "log" or re.search(r"log|linear|gamma", text, re.I):
        return None, "required", "无法确定这个 LUT 的输入颜色，请按 LUT 的说明选择。"
    return "srgb", "fallback", "文件没有写明输入颜色，先用创意 LUT 常用的 sRGB（Rec.709 色域）。请核对 LUT 的说明。"
