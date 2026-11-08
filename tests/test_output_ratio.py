"""固定比例加框：保留照片、最小留边和字幕块，只在外面补画布。"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from gxr_studio.finish.fonts import FontMetrics
from gxr_studio.finish.frame import apply_frame
from gxr_studio.finish.layout import advance, frame_layout
from gxr_studio.finish.spec import FrameSpec, parse_frame
from gxr_studio.render.params import EditError
from test_finish_layout import META

STYLES = ("gallery", "leica", "hasselblad", "film", "instant")
RATIOS = [
    ("4:3", 240, 320, 3, 4),
    ("4:3", 2400, 3200, 3, 4),
    ("4:5", 2400, 3000, 4, 5),
    ("1:1", 500, 500, 1, 1),
    ("16:9", 180, 320, 9, 16),
    ("16:9", 1800, 3200, 9, 16),
    ("xpan-65-24", 325, 120, 65, 24),
    ("xpan-65-24", 3250, 1200, 65, 24),
    ("xpan-65-24", 120, 325, 24, 65),
    ("xpan-65-24", 1200, 3250, 24, 65),
]


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize(("preset", "w", "h", "rw", "rh"), RATIOS)
def test_finished_ratio_keeps_the_photo_and_caption_block(style, preset, w, h, rw, rh):
    spec = FrameSpec(style=style, text="Hello", show_date=True)
    m = FontMetrics()
    base = frame_layout(w, h, replace(spec, keep_ratio=False), META, m, preset)
    layout = frame_layout(w, h, spec, META, m, preset)
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
            assert item["x"] + advance(m, item["font"], item["px"], item["text"], item["cjk"]) <= layout.width + 0.5
            assert 0 <= item["y"] and item["y"] + item["px"] <= layout.height + 1
        elif item["k"] == "logo":
            assert 0 <= item["x"] and item["x"] + item["w"] <= layout.width
            assert 0 <= item["y"] and item["y"] + item["h"] <= layout.height
        else:
            assert 0 <= item["x1"] <= layout.width and 0 <= item["x2"] <= layout.width
            assert 0 <= item["y1"] <= layout.height and 0 <= item["y2"] <= layout.height


@pytest.mark.parametrize("style", STYLES)
def test_the_framed_photo_is_not_resampled(style):
    photo = np.random.default_rng(55).integers(0, 256, size=(320, 240, 3), dtype=np.uint8)
    spec = FrameSpec(style=style, text="Hello")
    layout = frame_layout(240, 320, spec, META, FontMetrics(), "4:3")
    out = apply_frame(photo, spec, META, "4:3")
    x, y = layout.photo
    assert out.shape[:2] == (layout.height, layout.width)
    assert np.array_equal(out[y : y + 320, x : x + 240], photo)


@pytest.mark.parametrize("style", (*STYLES, "canvas"))
@pytest.mark.parametrize("preset", (None, "original", "free"))
def test_original_free_and_missing_crops_take_the_old_layout_path(style, preset):
    spec = FrameSpec(style=style, text="Hello")
    assert frame_layout(900, 600, spec, META, FontMetrics(), preset) == frame_layout(
        900, 600, replace(spec, keep_ratio=False), META, FontMetrics()
    )


@pytest.mark.parametrize("style", STYLES)
def test_turning_ratio_off_is_the_existing_layout(style):
    spec = FrameSpec(style=style, keep_ratio=False, text="Hello")
    assert frame_layout(2400, 3200, spec, META, FontMetrics(), "4:3") == frame_layout(2400, 3200, spec, META, FontMetrics())


@pytest.mark.parametrize("value", (0, 1, "false", None, []))
def test_keep_ratio_must_be_a_boolean(value):
    with pytest.raises(EditError) as error:
        parse_frame({"style": "gallery", "keep_ratio": value})
    assert error.value.code == "bad_frame"
    assert "只能打开或关闭" in error.value.detail


def test_older_clients_and_sidecars_follow_the_crop_by_default():
    spec = parse_frame({"style": "instant"})
    assert spec.keep_ratio is True
    assert spec.to_json()["keep_ratio"] is True


def test_hdr_uses_the_same_new_frame_canvas_and_photo_position():
    from gxr_studio.finish.hdr_export import HdrTrack

    spec = FrameSpec(style="instant")
    layout = frame_layout(240, 320, spec, {}, FontMetrics(), "4:3")
    track = HdrTrack(None)
    track.layer = np.full((320, 240, 2), 0.5, dtype=np.float32)
    track.frame(spec, {}, "4:3")
    x, y = layout.photo
    assert track.layer.shape[:2] == (layout.height, layout.width)
    assert np.all(track.layer[y : y + 320, x : x + 240] == 0.5)
    assert np.isnan(track.layer[0, 0]).all() and np.isnan(track.layer[-1, -1]).all()
