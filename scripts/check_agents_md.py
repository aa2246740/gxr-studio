#!/usr/bin/env python3
"""AGENTS.md 里标了 verified-commands 的命令必须还在 Makefile 里。"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "AGENTS.md"
MAKEFILE = ROOT / "Makefile"
BLOCK = re.compile(r"```verified-commands\n(.*?)```", re.S)
TARGET = re.compile(r"^([A-Za-z0-9_-]+):", re.M)


def main() -> int:
    if not AGENTS.is_file():
        print("AGENTS.md missing")
        return 1
    text = AGENTS.read_text(encoding="utf-8")
    match = BLOCK.search(text)
    if match is None:
        print("AGENTS.md has no verified-commands block")
        return 1
    make = MAKEFILE.read_text(encoding="utf-8")
    targets = set(TARGET.findall(make))
    missing = []
    for line in match.group(1).splitlines():
        command = line.strip()
        if not command or command.startswith("#"):
            continue
        if not command.startswith("make "):
            missing.append(f"not a make command: {command}")
            continue
        name = command.split()[1]
        if name not in targets:
            missing.append(f"Makefile has no target {name}")
    if len(text) < 100:
        missing.append("AGENTS.md is too short to be useful")
    if missing:
        print("\n".join(missing))
        return 1
    print(f"verified {len(targets)} make targets against AGENTS.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
