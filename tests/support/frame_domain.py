"""Photos the real geometry path produces before it stamps (synthetic RGB only).

`finish_unframed` warps and crops before it stamps, and the stamp's pixel size follows the final
photo's short side. These inputs are a record of sizes that path can reach, not a bound: the CJK
gate (manager decision after Sol review r3) holds at any stamp size, so no maximum is claimed.
- Sol r2: a 4288×2848 source with distortion −100 gives 5305×3119 (243 px large stamp).
- The worker's search: 3021×2848, distortion −100, vertical ±100, 1:1 gives 3404×3404 (266 px).
- Sol r3: a 4288×1 source with distortion −100 and straighten 40° has no valid pixel in the
  240-px thumbnail mask, so `normalized_valid_rect` falls back to the whole canvas: 4436×3724
  (290 px). The transpose gives 3724×4436.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from gxr_studio.finish.compose import finish_unframed
from gxr_studio.finish.geometry import warp_valid
from gxr_studio.finish.spec import StampSpec, parse_geometry
from gxr_studio.gpu.box import final_box
from gxr_studio.render.params import EditState

EXTREME_SOURCE = (3021, 2848)

# label, source (w, h), geometry, crop preset, expected photo (w, h)
NAMED_INPUTS: list[tuple[str, tuple[int, int], dict[str, Any], str | None, tuple[int, int]]] = [
    ("Sol r2: distortion -100", (4288, 2848), {"distortion": -100}, None, (5305, 3119)),
    ("extreme, vertical +100, 1:1", EXTREME_SOURCE, {"distortion": -100, "vertical": 100}, "1:1", (3404, 3404)),
    ("extreme, vertical -100, 1:1", EXTREME_SOURCE, {"distortion": -100, "vertical": -100}, "1:1", (3404, 3404)),
    ("transpose, horizontal +100, 1:1", (2848, 3021), {"distortion": -100, "horizontal": 100}, "1:1", (3404, 3404)),
    ("transpose, horizontal -100, 1:1", (2848, 3021), {"distortion": -100, "horizontal": -100}, "1:1", (3404, 3404)),
    ("extreme, default crop", EXTREME_SOURCE, {"distortion": -100, "vertical": 100}, None, (3385, 3379)),
    ("Sol r3: 4288x1, distortion -100, straighten 40", (4288, 1), {"distortion": -100, "straighten": 40}, None, (4436, 3724)),
    ("Sol r3: 1x4288, distortion -100, straighten -40", (1, 4288), {"distortion": -100, "straighten": -40}, None, (3724, 4436)),
]


def finished_photo(source: tuple[int, int], geometry: dict[str, Any], preset: str | None, crop_rect: Any = None) -> tuple[int, int]:
    """(w, h) of the photo `finish_unframed` stamps, checked against `final_box` (the GPU box)."""
    w, h = source
    geom = parse_geometry(geometry)
    img, _header = finish_unframed(
        np.zeros((h, w, 3), np.uint8),
        EditState(geometry=geom),
        stage="final",
        stamp=StampSpec(enabled=False),
        crop_rect=crop_rect,
        taken=None,
        file_time=None,
        from_raw=True,
        crop_preset=preset,
    )
    box, _valid = final_box(w, h, geom, crop_rect, preset)
    oh, ow = img.shape[:2]
    assert (ow, oh) == (box[2], box[3]), (source, geometry, preset, (ow, oh), box)
    return ow, oh


# The three 2-px reproductions: a free square crop of the extreme's valid square (whose top-left
# is (807, 388) in the warped canvas), seg7, large, "上海上海上海", top-left corner.
REPRO_SIDES = (3148, 3302, 3315)


def repro_photo(side: int) -> tuple[int, int]:
    w, h = EXTREME_SOURCE
    geometry = {"distortion": -100, "vertical": 100}
    vh, vw = warp_valid(w, h, parse_geometry(geometry)).shape
    x0, y0 = 807 + (3404 - side) // 2, 388 + (3404 - side) // 2
    return finished_photo(EXTREME_SOURCE, geometry, None, (x0 / vw, y0 / vh, side / vw, side / vh))
