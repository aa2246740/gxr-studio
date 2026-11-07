"""存下来的框的三条底线（评审第七轮）：

1. 小的「原始」框存下来以后，原样再写一遍（普通写入）必须被接受：存之前就按 validate_crop 的比例容差检查；
2. `fit` 在普通写入收得下的时候，结果和普通写入一模一样（缩放到 1.91:1 这类比例的框，不能被 fit 改成另一个框）；
3. 每条写入路径上，收完的框短边都不小于 16 像素（扩开的画布上普通拖一下，不能存下 13 × 10 的框）。
"""

from __future__ import annotations

import copy
import itertools
from typing import Any

import pytest

from gxr_studio import crop as cropmod
from gxr_studio.crop import CropError
from gxr_studio.library import edits
from gxr_studio.library import sidecar as sc
from gxr_studio.render import edits as render_edits
from test_crop_property import photo, stored  # noqa: F401


def set_canvas(lib: Any, shot: dict[Any, Any], width: int, height: int, geometry: dict[str, Any] | None = None) -> tuple[int, int]:
    side = lib.read_sidecar_for_write(shot)
    side["frame"] = {"width": width, "height": height}
    side["crop"] = None
    side.pop("geometry", None)
    sc.write_atomic(lib.sidecar_path_for(shot), copy.deepcopy(side), tmp_dir=lib.tmp_dir)
    if geometry:
        render_edits.write(lib, shot, {"geometry": geometry}, None)  # type: ignore[arg-type]
    _side, _geom, _sw, _sh, w, h = stored(lib, shot)
    return w, h


def strict_ok(lib: Any, shot: dict[Any, Any]) -> list[float]:
    """把存下来的框原样再写一遍（普通写入）：被拒就是测试失败；返回存下来的框。"""
    side, geom, _sw, _sh, w, h = stored(lib, shot)
    raw = side["crop"]
    cropmod.validate_crop(
        raw["preset"], raw["rect"], float(raw.get("angle_deg", 0.0)), w, h, mirrored=geom.flip, check_region=not geom.expands()
    )
    edits.write_crop(lib, shot, preset=raw["preset"], rect=raw["rect"], angle_deg=float(raw.get("angle_deg", 0.0)))
    return list(lib.read_sidecar_for_write(shot)["crop"]["rect"])


CANVASES = [
    (4288, 2848, None),
    (3300, 2400, None),
    (2999, 1999, None),
    (4288, 2848, {"vertical": 60}),
    (2000, 3001, {"rotate90": 1, "distortion": 40}),
]


@pytest.mark.parametrize(("width", "height", "geometry"), CANVASES)
def test_a_small_original_box_is_stored_with_a_ratio_the_strict_check_accepts(lib, photo, width, height, geometry):  # noqa: F811
    w, h = set_canvas(lib, photo, width, height, geometry)
    for size in (0.0015, 0.004, 0.0063, 0.009, 0.012, 0.02, 0.03):
        for fit in (True, False):
            for orient in ("landscape", "portrait"):
                try:
                    edits.write_crop(
                        lib,
                        photo,
                        preset="original",
                        rect=[0.31, 0.27, size, size * (w / h if orient == "landscape" else h / w)],
                        orientation=orient,
                        fit=fit,
                    )
                except CropError as exc:
                    assert not fit and exc.code in ("too_small", "ratio_mismatch"), (size, fit, orient, exc.code)
                    continue
                crop = lib.read_sidecar_for_write(photo)["crop"]
                bx, by, bw, bh = cropmod.crop_box(tuple(crop["rect"]), w, h)  # type: ignore[arg-type]
                assert min(bw, bh) >= cropmod.MIN_SIDE_PX, (size, fit, orient, bw, bh)
                strict_ok(lib, photo)


@pytest.mark.parametrize(
    ("preset", "orient"),
    [("1.91:1", "landscape"), ("4:5", "portrait"), ("16:9", "landscape"), ("xpan-65-24", "landscape"), ("1:1", "landscape")],
)
def test_fit_is_exactly_a_plain_write_whenever_a_plain_write_would_succeed(lib, photo, preset, orient):  # noqa: F811
    w, h = set_canvas(lib, photo, 4288, 2848)
    base = lib.read_sidecar_for_write(photo)
    ratio = float(cropmod.target_ratio(preset, w, h))  # type: ignore[arg-type]
    aspect = ratio if orient == "landscape" else 1 / ratio
    checked = 0
    for fraction, (cx, cy) in itertools.product((0.2, 0.35, 0.5, 0.7, 0.9, 1.0), ((0.5, 0.5), (0.3, 0.4), (0.62, 0.55))):
        bw = fraction * min(w, h * aspect)
        bh = bw / aspect
        rect = [min(max(cx - bw / w / 2, 0), 1 - bw / w), min(max(cy - bh / h / 2, 0), 1 - bh / h), bw / w, bh / h]
        sc.write_atomic(lib.sidecar_path_for(photo), copy.deepcopy(base), tmp_dir=lib.tmp_dir)
        try:
            plain = edits.write_crop(lib, photo, preset=preset, rect=rect, orientation=orient)
        except CropError:
            continue
        sc.write_atomic(lib.sidecar_path_for(photo), copy.deepcopy(base), tmp_dir=lib.tmp_dir)
        fitted = edits.write_crop(lib, photo, preset=preset, rect=rect, orientation=orient, fit=True)
        assert fitted["rect"] == plain["rect"], (preset, fraction, cx, cy, plain["rect"], fitted["rect"])
        checked += 1
    assert checked >= 6, "普通写入收得下的用例太少，这个测试没测到什么"


# 画布扩开以后，框的角可以在有效区外面（普通写入不查角）。服务器收框时把它收回有效区，收出来的短边不能小于 16 像素。
# 下面是 4288 × 2848 的画布上，原来存下过 18 × 11、4 × 13、7 × 22、7 × 25 的框的写入
EXPANDED_DRAGS = [
    ({"vertical": 100}, [0.03339, 0.3102, 0.01526, 0.01569]),
    ({"vertical": 100}, [0.99551, 0.0, 0.00449, 0.02237]),
    ({"vertical": 70, "horizontal": 60}, [0.9953, 0.0, 0.0047, 0.02081]),
    ({"vertical": -100, "straighten": 4.0}, [0.99514, 0.97442, 0.00486, 0.02558]),
]


@pytest.mark.parametrize(("geometry", "rect"), EXPANDED_DRAGS)
def test_a_plain_drag_on_an_expanded_canvas_cannot_store_a_box_under_16_pixels(lib, photo, geometry, rect):  # noqa: F811
    w, h = set_canvas(lib, photo, 4288, 2848, geometry)
    assert stored(lib, photo)[1].expands() and max(w, h) > 4288, "这个测试要的是扩开的画布"
    try:
        edits.write_crop(lib, photo, preset="free", rect=rect, angle_deg=float(geometry.get("straighten", 0.0)))
    except CropError as exc:
        assert exc.code == "too_small"
        assert not lib.read_sidecar_for_write(photo).get("crop"), "被拒的写入什么也没存"
        return
    crop = lib.read_sidecar_for_write(photo)["crop"]
    _x, _y, bw, bh = cropmod.crop_box(tuple(crop["rect"]), w, h)  # type: ignore[arg-type]
    assert min(bw, bh) >= cropmod.MIN_SIDE_PX, f"存下了 {bw} × {bh} 的框"
    strict_ok(lib, photo)
