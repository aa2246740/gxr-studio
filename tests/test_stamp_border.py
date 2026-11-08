"""拍立得底边上的日期戳：照片像素不变，预览和导出的坐标相同，旧编辑的 JPEG 字节不变。"""

from dataclasses import replace
from datetime import datetime

import numpy as np
import pytest

from gxr_studio.finish.compose import encode_jpeg, finish_array
from gxr_studio.finish.fonts import FontMetrics
from gxr_studio.finish.frame import apply_frame
from gxr_studio.finish.hdr_export import HdrTrack
from gxr_studio.finish.layout import frame_layout, ink_box, stamp_layout
from gxr_studio.finish.spec import FrameSpec, parse_stamp
from gxr_studio.finish.stamp import format_stamp
from gxr_studio.render.params import EditError, EditState
from test_finish_compose import STYLES, _old_finish_array, _scene


@pytest.mark.parametrize("size", [(360, 240), (240, 360), (900, 600), (160, 120)])
@pytest.mark.parametrize("caption", ["", "Paris"])
@pytest.mark.parametrize("corner", ["tl", "br"])
def test_border_stamp_leaves_the_photo_untouched_and_clears_the_caption(size, caption, corner):
    w, h = size
    rgb = np.full((h, w, 3), (40, 70, 90), np.uint8)
    frame = FrameSpec(style="instant", text=caption)
    stamp = parse_stamp({"enabled": True, "placement": "border", "corner": corner})
    assert stamp is not None
    taken = datetime(2026, 11, 8)
    track = HdrTrack(None, layer=np.ones((h, w, 2), np.float32))
    result, _ = finish_array(
        rgb,
        EditState(),
        stage="final",
        frame=frame,
        stamp=stamp,
        crop_rect=None,
        meta={},
        taken=taken,
        file_time=None,
        from_raw=False,
        crop_preset="4:3",
        track=track,
    )
    layout = frame_layout(w, h, frame, {}, FontMetrics(), "4:3")
    assert layout is not None
    x, y = layout.photo
    assert np.array_equal(result[y : y + h, x : x + w], rgb), "印记必须在照片外"
    assert not np.array_equal(result, apply_frame(rgb, frame, {}, "4:3")), "底边确实印出了日期"
    assert track.layer is not None and np.isfinite(track.layer[y : y + h, x : x + w]).all()
    assert stamp.to_json()["placement"] == "border"
    mark = stamp_layout(w, h, stamp, format_stamp(stamp, taken, None), FontMetrics(), layout)
    assert mark is not None and mark.rect[1] >= y + h
    for item in layout.items:
        box = ink_box(FontMetrics(), item["font"], item["px"], item["text"], item["cjk"])
        assert mark.rect[1] > item["y"] + box[3], "印记不能盖住边框题字"


@pytest.mark.parametrize("style", STYLES)
def test_edits_without_stamp_placement_keep_the_same_jpeg_bytes(style):
    rgb, meta, taken, file_time = _scene()
    stamp = parse_stamp({"enabled": True})
    assert stamp is not None and "placement" not in stamp.to_json()
    frame = FrameSpec(style=style, text="Hello", show_date=True)
    args = dict(stage="final", frame=frame, stamp=stamp, crop_rect=None, meta=meta, taken=taken, file_time=file_time, from_raw=False)
    got, _ = finish_array(rgb, EditState(), **args)
    want, _ = _old_finish_array(rgb, EditState(), **args)
    assert encode_jpeg(got, 95) == encode_jpeg(want, 95)
    if style != "instant":
        border = replace(stamp, placement="border")
        other, _ = finish_array(rgb, EditState(), **{**args, "stamp": border})
        assert np.array_equal(other, got), "没有拍立得边框时，沿用照片上的印记"


def test_stamp_placement_rejects_unknown_or_non_string_values():
    for placement in ("canvas", True, [], {}):
        with pytest.raises(EditError) as error:
            parse_stamp({"placement": placement})
        assert error.value.code == "bad_stamp"
        assert "照片或拍立得边框" in error.value.detail
