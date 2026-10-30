#!/usr/bin/env python3
"""开发时给裁切计算做一次 cProfile。结果写到临时文件，不进仓库。"""

from __future__ import annotations

import cProfile
import pstats
import sys
import tempfile
from pathlib import Path

from gxr_studio.crop import default_crop, validate_crop


def _work() -> None:
    width, height = 4288, 2848
    for _ in range(400):
        crop = default_crop("xpan-65-24", width, height)
        validate_crop(crop.preset, crop.rect, crop.angle_deg, width, height)


def main() -> int:
    destination = Path(tempfile.gettempdir()) / "gxr-studio-crop.prof"
    cProfile.runctx("_work()", globals(), globals(), filename=str(destination))
    stats = pstats.Stats(str(destination), stream=sys.stdout)
    stats.sort_stats("cumulative").print_stats(15)
    print(f"profile: {destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
