"""从明确给出的封存数值采样构建私有质量包。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import build_sample_source


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="含source.json的封存采样目录")
    parser.add_argument("destination", type=Path, help="新的私有质量包目录")
    args = parser.parse_args()
    manifest = build_sample_source(args.source, args.destination)
    print(json.dumps({"unit": manifest["unit"], "groups": list(manifest["groups"]), "destination": str(args.destination)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
