"""读取全文件的署名注释；原件导出保留字节，仅按需清理 TITLE。"""

from __future__ import annotations

import codecs
import io
import json
import re
from collections.abc import Iterator
from typing import Any

import numpy as np

from .cube import Cube, comment_start

FIELDS = {"source": "Source", "author": "Author", "license": "License"}
KEYS = {
    "source": "source",
    "来源": "source",
    "url": "source",
    "website": "source",
    "author": "author",
    "作者": "author",
    "creator": "author",
    "copyright": "author",
    "license": "license",
    "licence": "license",
    "许可": "license",
}


def _comments(text: str, header_only: bool) -> Iterator[str]:
    for line in text.splitlines():
        stripped = line.lstrip()
        if header_only:
            if not stripped or stripped.startswith(("TITLE", "DOMAIN_", "LUT_")):
                continue
            if not stripped.startswith("#"):
                break
        start = comment_start(line)
        if start >= 0:
            yield line[start + 1 :].lstrip()


def read_header(text: str, *, header_only: bool = False) -> tuple[dict[str, str], list[str]]:
    """默认读完整文件；header_only 仅用于恢复旧库未采集的署名。"""
    values: dict[str, list[str]] = {key: [] for key in FIELDS}
    comments = []
    exact = None
    for comment in _comments(text, header_only):
        if comment.startswith("GXR-STUDIO-METADATA: "):
            try:
                data = json.loads(comment.removeprefix("GXR-STUDIO-METADATA: "))
                if isinstance(data, dict) and set(data) == set(FIELDS) and all(isinstance(v, str) and len(v) <= 8192 for v in data.values()):
                    exact = data
            except ValueError:
                pass
            continue
        match = re.match(r"([^:=]+)[:=] ?(.*)$", comment)
        key = KEYS.get(match[1].strip().lower()) if match else None
        if key is not None and match is not None:
            values[key].append(match[2])
        elif re.search(r"CC[ -]BY|creativecommons\.org/licenses|Creative Commons", comment, re.I):
            values["license"].append(comment)
        else:
            comments.append(comment)
    return exact if exact is not None else {key: "\n".join(lines) for key, lines in values.items()}, comments


def clean_title(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r'["\'\\#\x00-\x1f\x7f]', " ", title)).strip()


def _attribution(record: dict[str, Any]) -> tuple[str, str]:
    lines = []
    for key, label in FIELDS.items():
        value = record.get(key, "")
        if value:
            for line in value.splitlines():
                lines.append(f"# {label}: {line}\n")
    exact = "# GXR-STUDIO-METADATA: " + json.dumps({key: record.get(key, "") for key in FIELDS}, ensure_ascii=True) + "\n"
    return "".join(lines), exact


def _original_export(title: str, record: dict[str, Any], raw: bytes) -> bytes:
    bom = codecs.BOM_UTF8 if raw.startswith(codecs.BOM_UTF8) else b""
    text = raw[len(bom) :].decode("utf-8")
    original_fields, _ = read_header(text)
    cleaned = clean_title(title)
    if cleaned != title:
        lines = text.splitlines(keepends=True)
        for index, line in enumerate(lines):
            if not line.lstrip().startswith("TITLE"):
                continue
            body = line.rstrip("\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029")
            ending = line[len(body) :]
            start = comment_start(body)
            comment = ""
            if start >= 0:
                before = body[:start]
                comment = before[len(before.rstrip()) :] + body[start:]
            indent = body[: len(body) - len(body.lstrip())]
            lines[index] = f'{indent}TITLE "{cleaned}"{comment}{ending}'
            break
        text = "".join(lines)
    if {key: record.get(key, "") for key in FIELDS} == original_fields:
        return bom + text.encode("utf-8")
    header, exact = _attribution(record)
    separator = "" if text.endswith("\n") else "\n"
    # 原注释全部保留；当前字段放在头部，末尾的精确字段供再次导入。
    return bom + (header + text + separator + exact).encode("utf-8")


def export_cube(cube: Cube, record: dict[str, Any], *, original: bytes | None = None) -> bytes:
    if original is not None:
        return _original_export(cube.title, record, original)
    out = io.StringIO()
    for comment in record.get("comments", []):
        for line in str(comment).splitlines():
            out.write(f"# {line}\n")
    header, exact = _attribution(record)
    out.write(header)
    out.write(exact)
    out.write(f'TITLE "{clean_title(cube.title)}"\n')
    if cube.table1 is not None:
        out.write(f"LUT_1D_SIZE {len(cube.table1)}\n")
    if cube.table3 is not None:
        out.write(f"LUT_3D_SIZE {len(cube.table3)}\n")
    if cube.range1 != (0.0, 1.0) or cube.range3 != (0.0, 1.0):
        for table, label, limits in ((cube.table1, "1D", cube.range1), (cube.table3, "3D", cube.range3)):
            if table is not None:
                out.write(f"LUT_{label}_INPUT_RANGE {limits[0]:.17g} {limits[1]:.17g}\n")
    else:
        for label, domain in (("MIN", cube.domain_min), ("MAX", cube.domain_max)):
            out.write(f"DOMAIN_{label} " + " ".join(f"{v:.17g}" for v in domain) + "\n")
    for table in (cube.table1, cube.table3):
        if table is not None:
            np.savetxt(out, table.reshape(-1, 3), fmt="%.17g")
    return out.getvalue().encode("utf-8")
