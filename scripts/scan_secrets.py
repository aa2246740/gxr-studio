#!/usr/bin/env python3
"""扫已跟踪的文本，拦下明显的私钥和令牌。不打印匹配到的秘密本身。"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = (
    ("private-key", re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bghp_[A-Za-z0-9]{20,}")),
    ("github-oauth", re.compile(r"\bgho_[A-Za-z0-9]{20,}")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
)
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".lock"}


def tracked() -> list[Path]:
    out = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    return [ROOT / part.decode() for part in out.split(b"\0") if part]


def main() -> int:
    hits: list[str] = []
    for path in tracked():
        if path.suffix.lower() in SKIP_SUFFIX or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            continue
        for name, pattern in PATTERNS:
            if pattern.search(text):
                hits.append(f"{path.relative_to(ROOT)}: {name}")
    if hits:
        print("\n".join(hits))
        return 1
    print("no high-confidence secrets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
