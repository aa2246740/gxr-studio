#!/usr/bin/env python3
"""拦住过大的已跟踪文件。数据 JSON 只看字节；源码还看行数。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 1_000_000
MAX_SOURCE_LINES = 800
SOURCE = {".py", ".js", ".css", ".rs"}


def tracked() -> list[Path]:
    out = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    paths = []
    for part in out.split(b"\0"):
        if not part:
            continue
        path = ROOT / part.decode()
        if path.is_file():
            paths.append(path)
    return paths


def _source_lines(path: Path) -> bool:
    """`.ts` 只数 `mobile/`。`frontend/src/engine.ts` 已经超过 800 行，不在这次的范围里。"""
    if path.suffix == ".ts":
        return path.relative_to(ROOT).parts[:1] == ("mobile",)
    return path.suffix in SOURCE


def main() -> int:
    problems: list[str] = []
    for path in tracked():
        size = path.stat().st_size
        if size > MAX_BYTES:
            problems.append(f"{path.relative_to(ROOT)}: {size} bytes > {MAX_BYTES}")
        if _source_lines(path):
            lines = path.read_text(encoding="utf-8").count("\n") + 1
            if lines > MAX_SOURCE_LINES:
                problems.append(f"{path.relative_to(ROOT)}: {lines} lines > {MAX_SOURCE_LINES}")
    if problems:
        print("\n".join(problems))
        return 1
    print(f"checked {len(tracked())} tracked files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
