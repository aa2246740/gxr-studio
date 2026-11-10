"""Regenerate portable synthetic layout digests from the approved local designer code."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from test_frame_reference import FIXTURE, cases, digest, reference_layouts  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    args = parser.parse_args()
    inputs = cases()
    layouts = reference_layouts(args.reference, inputs)
    payload = {
        "source": "R3 revision 2; compact carrier; DESIGN original-blur and focal-data corrections; synthetic sizes only",
        "count": len(inputs),
        "sha256": [digest(v) for v in layouts],
    }
    FIXTURE.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"{len(inputs)} reference layouts written to {FIXTURE}")


if __name__ == "__main__":
    main()
