"""The stamp-size domain through the real geometry path (manager decisions of 2026-11-14).

`finish_unframed` warps and crops before it stamps, and the stamp's pixel size depends only on the
final photo's short side (it can then only shrink). A 1:1 preset or a free crop can pick the largest
fully valid square of the warped image, so that square decides the maximum. The worker's search
(`round-6/mobile-frame/probe/domain/`) found it at a 3021×2848 source with distortion −100 and
vertical ±100: a 3404×3404 photo, a 266 px large stamp. Everything here uses synthetic RGB only.
"""

from __future__ import annotations

import itertools
from typing import Any

import numpy as np

from gxr_studio.finish.compose import finish_unframed
from gxr_studio.finish.geometry import _has_window, _valid_integral, warp_valid
from gxr_studio.finish.spec import Geometry, StampSpec, parse_geometry
from gxr_studio.gpu.box import final_box
from gxr_studio.render.params import EditState

# Sources up to 4288×2848 in either orientation (a superset of what GXR writes).
GXR_LONG, GXR_SHORT = 4288, 2848
EXTREME_SOURCE = (3021, 2848)

# label, source (w, h), geometry, crop preset, expected photo (w, h)
NAMED_INPUTS: list[tuple[str, tuple[int, int], dict[str, Any], str | None, tuple[int, int]]] = [
    ("Sol r2: distortion -100", (4288, 2848), {"distortion": -100}, None, (5305, 3119)),
    ("extreme, vertical +100, 1:1", EXTREME_SOURCE, {"distortion": -100, "vertical": 100}, "1:1", (3404, 3404)),
    ("extreme, vertical -100, 1:1", EXTREME_SOURCE, {"distortion": -100, "vertical": -100}, "1:1", (3404, 3404)),
    ("transpose, horizontal +100, 1:1", (2848, 3021), {"distortion": -100, "horizontal": 100}, "1:1", (3404, 3404)),
    ("transpose, horizontal -100, 1:1", (2848, 3021), {"distortion": -100, "horizontal": -100}, "1:1", (3404, 3404)),
    ("extreme, default crop", EXTREME_SOURCE, {"distortion": -100, "vertical": 100}, None, (3385, 3379)),
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


def _largest_square(valid: np.ndarray) -> int:
    """What the 1:1 preset can reach: the side of the largest fully valid square window."""
    sheet = _valid_integral(valid)
    lo, hi = 0, min(valid.shape)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _has_window(sheet, mid, mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def neighbourhood() -> list[tuple[tuple[int, int], dict[str, Any]]]:
    """Full-resolution neighbours of the analytic extreme: source widths around the peak, and small
    changes to every other parameter."""
    base = {"distortion": -100, "vertical": 100}
    out = [((w, GXR_SHORT), base) for w in (3016, 3020, 3021, 3023, 3060, 4288)]
    for extra in ({"horizontal": 5}, {"straighten": 0.1}, {"vertical": 99}, {"distortion": -99}):
        out.append((EXTREME_SOURCE, {**base, **extra}))
    return out


def neighbourhood_square(source: tuple[int, int], geometry: dict[str, Any]) -> int:
    return _largest_square(warp_valid(source[0], source[1], parse_geometry(geometry)))


def coarse_grid(scale: int = 8) -> list[tuple[int, tuple[int, int], dict[str, Any]]]:
    """The whole slider space at reduced scale (the warp is scale-invariant): estimated largest
    square (scaled back up), source and geometry, largest first."""
    sliders = (-100, -50, 0, 50, 100)
    out = []
    for w in (2848, 3024, 3200, 3600, 4288):
        for d, v, hz in itertools.product(sliders, sliders, sliders):
            geometry = {"distortion": d, "vertical": v, "horizontal": hz}
            if not Geometry(**geometry).expands():
                continue
            side = _largest_square(warp_valid(w // scale, GXR_SHORT // scale, Geometry(**geometry))) * scale
            out.append((side, (w, GXR_SHORT), geometry))
    out.sort(key=lambda row: -row[0])
    return out


# The three 2-px reproductions: a free square crop of the extreme's valid square (whose top-left
# is (807, 388) in the warped canvas), seg7, large, "上海上海上海", top-left corner.
REPRO_SIDES = (3148, 3302, 3315)


def repro_photo(side: int) -> tuple[int, int]:
    w, h = EXTREME_SOURCE
    geometry = {"distortion": -100, "vertical": 100}
    vh, vw = warp_valid(w, h, parse_geometry(geometry)).shape
    x0, y0 = 807 + (3404 - side) // 2, 388 + (3404 - side) // 2
    return finished_photo(EXTREME_SOURCE, geometry, None, (x0 / vw, y0 / vh, side / vw, side / vh))
