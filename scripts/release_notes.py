#!/usr/bin/env python3
"""从 git 历史生成 CHANGELOG.md。给发布工作流和本地 `make notes` 用。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "CHANGELOG.md"


def notes() -> str:
    log = subprocess.check_output(
        ["git", "log", "--pretty=format:%h %s"],
        cwd=ROOT,
        text=True,
    )
    lines = [f"- {line}" for line in log.splitlines() if line.strip()]
    body = "\n".join(lines) if lines else "- (no commits)"
    return f"# Changelog\n\n{body}\n"


def main() -> int:
    OUT.write_text(notes(), encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
