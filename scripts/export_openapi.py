#!/usr/bin/env python3
"""把 FastAPI 的 OpenAPI 文档写到 docs/openapi.json。--check 时只比较，不改文件。"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from gxr_studio.app import create_app
from gxr_studio.config import Settings

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "openapi.json"


def build() -> dict:
    with tempfile.TemporaryDirectory(prefix="gxr-openapi-") as tmp:
        path = Path(tmp)
        settings = Settings(
            library_dir=path / "library",
            home_dir=path / "home",
            export_dir=path / "export",
            volumes_roots=(path / "volumes",),
            token="openapi-export",
            port=8765,
            web_dir=None,
        )
        app = create_app(settings)
        try:
            return app.openapi()
        finally:
            app.state.ctx.lib.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    spec = build()
    text = json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.check:
        if not OUT.is_file() or OUT.read_text(encoding="utf-8") != text:
            print("docs/openapi.json is missing or stale; run scripts/export_openapi.py")
            return 1
        print("openapi.json matches")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
