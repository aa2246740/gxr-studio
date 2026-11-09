"""成品比例独立于裁剪，只增加居中的画布，保留照片和字幕块。"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from gxr_studio.finish.fonts import FontMetrics
from gxr_studio.finish.frame import apply_frame
from gxr_studio.finish.layout import frame_layout, ink_box
from gxr_studio.finish.spec import OUTPUT_RATIOS, FrameSpec, parse_frame
from gxr_studio.render.params import EditError
from test_finish_layout import META
from test_output_ratio_real import frozen_layout as frozen_layout

STYLES = ("gallery", "leica", "hasselblad", "film", "instant")
SHAPES = [(900, 600), (600, 900), (500, 500), (3250, 1200), (1800, 3200), (120, 325)]


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("ratio", OUTPUT_RATIOS)
@pytest.mark.parametrize(("w", "h"), SHAPES)
def test_finished_ratio_keeps_the_photo_and_caption_block(style, ratio, w, h):
    spec = FrameSpec(style=style, output_ratio=ratio, text="Hello", show_date=True)
    m = FontMetrics()
    base = frame_layout(w, h, replace(spec, output_ratio="original"), META, m)
    layout = frame_layout(w, h, spec, META, m)
    rw, rh = OUTPUT_RATIOS[ratio]
    assert layout.width * rh == layout.height * rw
    assert layout.width >= base.width and layout.height >= base.height
    dx, dy = layout.photo[0] - base.photo[0], layout.photo[1] - base.photo[1]
    assert 0 <= layout.width - base.width - 2 * dx <= 1
    assert 0 <= layout.height - base.height - 2 * dy <= 1
    assert layout.photo[0] + w <= layout.width and layout.photo[1] + h <= layout.height
    for old, item in zip(base.items, layout.items, strict=True):
        for key in ("x", "x1", "x2"):
            if key in old:
                assert item[key] == old[key] + dx
        for key in ("y", "y1", "y2"):
            if key in old:
                assert item[key] == old[key] + dy
        if item["k"] == "text":
            assert item["xs"] == [x + dx for x in old["xs"]]
            left, top, right, bottom = ink_box(m, item["font"], item["px"], item["text"], item["cjk"])
            assert 0 <= item["x"] + left <= item["x"] + right <= layout.width
            assert 0 <= item["y"] + top <= item["y"] + bottom <= layout.height
        elif item["k"] == "logo":
            assert 0 <= item["x"] and item["x"] + item["w"] <= layout.width
            assert 0 <= item["y"] and item["y"] + item["h"] <= layout.height
        else:
            stroke = (item["w"] + 1) // 2
            assert 0 <= item["x1"] - stroke <= item["x2"] + stroke <= layout.width
            assert 0 <= item["y1"] - stroke <= item["y2"] + stroke <= layout.height


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
def test_original_ratio_is_the_main_layout_without_ratio_padding(frozen_layout, style, w, h):
    spec = FrameSpec(style=style, output_ratio="original", text="Hello", show_date=True)
    old = SimpleNamespace(**spec.to_json(), keep_ratio=False)
    m = FontMetrics()
    assert frame_layout(w, h, spec, META, m).to_json() == frozen_layout.frame_layout(w, h, old, META, m, "4:3").to_json()


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
