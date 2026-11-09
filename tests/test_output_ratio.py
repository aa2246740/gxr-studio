"""成品比例独立于裁剪；照片原样放入符合最小留白的版面单元。"""

from __future__ import annotations

import math

import numpy as np
import pytest

from gxr_studio.finish.fonts import FontMetrics
from gxr_studio.finish.frame import apply_frame
from gxr_studio.finish.layout import frame_layout
from gxr_studio.finish.spec import OUTPUT_RATIOS, FrameSpec, parse_frame
from gxr_studio.render.params import EditError
from test_finish_layout import META

STYLES = ("gallery", "leica", "hasselblad", "film", "instant")
SHAPES = [(900, 600), (600, 900), (500, 500), (3250, 1200), (1800, 3200), (120, 325)]


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("ratio", OUTPUT_RATIOS)
@pytest.mark.parametrize(("w", "h"), SHAPES)
def test_finished_ratio_keeps_the_photo_inside_the_frame_unit(style, ratio, w, h):
    spec = FrameSpec(style=style, output_ratio=ratio, text="Hello", show_date=True)
    m = FontMetrics()
    layout = frame_layout(w, h, spec, META, m)
    assert layout is not None
    rw, rh = OUTPUT_RATIOS[ratio]
    assert layout.width * rh == layout.height * rw
    ux, uy, uw, uh = layout.unit_box
    assert ux >= math.ceil(0.06 * layout.width)
    assert layout.width - ux - uw >= math.ceil(0.06 * layout.width)
    assert uy >= math.ceil(0.06 * layout.height)
    assert layout.height - uy - uh >= math.ceil(0.04 * layout.height)
    x, y = layout.photo
    assert ux <= x and x + w <= ux + uw
    assert uy <= y and y + h <= uy + uh
    photo = [item for item in layout.items if item["k"] == "photo"]
    assert len(photo) == 1
    assert (photo[0]["x"], photo[0]["y"], photo[0]["w"], photo[0]["h"]) == (x, y, w, h)


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("ratio", (*OUTPUT_RATIOS, "original"))
def test_the_framed_photo_is_not_resampled(style, ratio):
    photo = np.random.default_rng(55).integers(0, 256, size=(320, 240, 3), dtype=np.uint8)
    spec = FrameSpec(style=style, output_ratio=ratio, text="Hello")
    layout = frame_layout(240, 320, spec, META, FontMetrics())
    out = apply_frame(photo, spec, META)
    x, y = layout.photo
    assert out.shape[:2] == (layout.height, layout.width)
    assert np.array_equal(out[y : y + 320, x : x + 240], photo)


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize(("w", "h"), SHAPES)
def test_original_ratio_uses_the_natural_frame_unit(style, w, h):
    spec = FrameSpec(style=style, output_ratio="original", text="Hello", show_date=True)
    layout = frame_layout(w, h, spec, META, FontMetrics())
    assert layout is not None and layout.fixed is False
    ux, uy, uw, uh = layout.unit_box
    x, y = layout.photo
    assert 0 <= ux <= x and x + w <= ux + uw <= layout.width
    assert 0 <= uy <= y and y + h <= uy + uh <= layout.height


@pytest.mark.parametrize("value", (0, True, "2:1", None, [], {}))
def test_output_ratio_must_be_one_of_the_choices(value):
    with pytest.raises(EditError) as error:
        parse_frame({"style": "gallery", "output_ratio": value})
    assert error.value.code == "bad_frame"
    assert error.value.message == "请选择成品比例后再试。"


def test_a_new_frame_defaults_to_three_by_four():
    spec = parse_frame({"style": "instant"})
    assert spec.output_ratio == "3:4"
    assert spec.to_json()["output_ratio"] == "3:4"


def test_hdr_uses_the_same_new_frame_canvas_and_photo_position():
    from gxr_studio.finish.hdr_export import HdrTrack

    spec = FrameSpec(style="instant", output_ratio="16:9")
    layout = frame_layout(240, 320, spec, {}, FontMetrics())
    track = HdrTrack(None)
    track.layer = np.full((320, 240, 2), 0.5, dtype=np.float32)
    track.frame(spec, {})
    x, y = layout.photo
    assert track.layer.shape[:2] == (layout.height, layout.width)
    assert np.all(track.layer[y : y + 320, x : x + 240] == 0.5)
    assert np.isnan(track.layer[0, 0]).all() and np.isnan(track.layer[-1, -1]).all()
