#!/usr/bin/env python3
"""登记过的开关必须在 src/ 里被点名，反过来代码里的开关名也必须登记。

生命周期见 docs/feature-flags.md：超过 180 天还在默认值上的开关，这里会失败。
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

from gxr_studio.flags import FLAGS

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
MAX_AGE_DAYS = 180
NAME = re.compile(r"^[a-z][a-z0-9_]*$")


def _sources() -> list[Path]:
    return [path for path in SRC.rglob("*") if path.suffix in {".py", ".js"} and path.name != "flags.py"]


def main() -> int:
    problems: list[str] = []
    blobs = [(path, path.read_text(encoding="utf-8")) for path in _sources()]
    today = date.today()
    for name, flag in FLAGS.items():
        if not NAME.fullmatch(name):
            problems.append(f"{name}: 名字必须是小写加下划线")
        if not any(name in text for _, text in blobs):
            problems.append(f"{name}: 源码里没有人读取这个开关")
        try:
            added = date.fromisoformat(flag.added)
        except ValueError:
            problems.append(f"{name}: added 不是 YYYY-MM-DD")
            continue
        age = (today - added).days
        if age > MAX_AGE_DAYS:
            problems.append(f"{name}: 已登记 {age} 天，超过 {MAX_AGE_DAYS} 天，删掉或改默认并更新 added")
    # 只认 flags.py 以外、形如 flag_enabled(..., "name") 的引用
    call = re.compile(r"""flag_enabled\([^)]*?["']([a-z][a-z0-9_]*)["']""")
    for path, text in blobs:
        for found in call.findall(text):
            if found not in FLAGS:
                problems.append(f"{path.relative_to(ROOT)}: 使用了未登记的开关 {found}")
    if problems:
        print("\n".join(problems))
        return 1
    print(f"{len(FLAGS)} flags referenced")
    return 0


if __name__ == "__main__":
    sys.exit(main())
