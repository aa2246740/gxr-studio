"""The seven new styles: export pixels, real-data captions and saved settings."""

from __future__ import annotations

import importlib
import io
import sys

import numpy as np
import pytest
from PIL import Image

from gxr_studio.finish.fonts import FontMetrics
from gxr_studio.finish.frame import apply_frame
from gxr_studio.finish.frame_texture import carrier_contours
from gxr_studio.finish.layout import frame_layout
from gxr_studio.finish.spec import FrameSpec, parse_frame
from test_api import find
from test_finish_routes import client as client
from test_frame_reference import META, REFERENCE
from test_paste_edits import ok, paste, state
from test_web_frontend import run_node

STYLES = ("blur_fill", "thin_keyline", "cinema", "exhibition_label", "slide_mount", "darkroom", "postage_stamp")


def png_bytes(rgb):
    output = io.BytesIO()
    Image.fromarray(rgb).save(output, format="PNG")
    return output.getvalue()


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("ratio", ("original", "4:5", "1:1"))
def test_new_styles_preserve_photo_pixels_and_export_identical_bytes(style, ratio):
    y, x = np.mgrid[:240, :360]
    photo = np.stack((x % 256, y % 256, (x + y) % 256), axis=2).astype(np.uint8)
    spec = FrameSpec(style=style, output_ratio=ratio)
    layout = frame_layout(360, 240, spec, META, FontMetrics())
    assert layout is not None
    first = apply_frame(photo, spec, META)
    second = apply_frame(photo, spec, META)
    assert png_bytes(first) == png_bytes(second)
    x0, y0 = layout.photo
    assert np.array_equal(first[y0 : y0 + 240, x0 : x0 + 360], photo)
    assert first.shape[:2] == (layout.height, layout.width)


@pytest.mark.parametrize("style", STYLES)
def test_new_settings_survive_defaults_sidecars_copy_paste_and_undo(client, style):
    source, target = find(client, "R0000102"), find(client, "R0000103")
    frame = {"style": style, "output_ratio": "4:5", "color": "black", "text": "海边", "show_params": False}
    parsed = parse_frame(frame)
    assert parsed is not None
    expected = parsed.to_json()
    assert client.put(f"/api/photos/{source['id']}/edit", json={"frame": frame}).status_code == 200
    assert client.get(f"/api/photos/{source['id']}/edit").json()["frame"] == expected
    assert client.put("/api/defaults/finish", json={"frame": frame, "stamp": {}}).status_code == 200
    assert client.get("/api/defaults/finish").json()["frame"] == expected
    before = state(client, target)
    result = ok(paste(client, ["frame"], [target], source_id=source["id"]))
    assert state(client, target)["edit"]["frame"] == expected
    assert client.post("/api/edits/paste/undo", json={"batch_id": result["batch_id"]}).status_code == 200
    assert state(client, target) == before


@pytest.mark.parametrize(
    ("lens", "expected"), [("A12 50mm", "RICOH GXR  ·  50"), ("A12 28mm", "RICOH GXR  ·  28"), ("MOUNT A12", "RICOH GXR"), ("", "RICOH GXR")]
)
def test_stamp_uses_the_actual_focal_length_and_omits_unknown_data(lens, expected):
    layout = frame_layout(1200, 800, FrameSpec(style="postage_stamp"), {"lens_short": lens}, FontMetrics())
    assert layout is not None
    assert next(i["text"] for i in layout.items if i["k"] == "text") == expected


def test_blur_uses_an_even_colourful_backdrop_in_original_ratio_too():
    photo = np.full((240, 360, 3), (200, 80, 40), np.uint8)
    spec = FrameSpec(style="blur_fill", output_ratio="original", show_params=False)
    out = apply_frame(photo, spec, {})
    luma = np.float32(200) * np.float32(0.299) + np.float32(80) * np.float32(0.587) + np.float32(40) * np.float32(0.114)
    expected = ((luma + (np.array([200, 80, 40], np.float32) - luma) * np.float32(1.15)) * np.float32(0.9)).astype(np.uint8)
    # Corners lie outside the reference's local shadow. No gradient veil or
    # transparent black band changes the photo-derived colour at these points.
    assert np.max(np.abs(out[0, 0].astype(int) - expected)) <= 1
    assert np.array_equal(out[0, 0], out[0, -1])


def test_stamp_semicircles_reveal_the_stockbook_and_darkroom_has_paper_grain():
    photo = np.full((240, 360, 3), 90, np.uint8)
    spec = FrameSpec(style="postage_stamp", output_ratio="4:5")
    layout = frame_layout(360, 240, spec, {}, FontMetrics())
    assert layout is not None
    stamp = apply_frame(photo, spec, {})
    for item in layout.items:
        if item["k"] == "ellipse":
            assert tuple(stamp[item["cy"], item["cx"]]) == (22, 23, 27)
    paper = apply_frame(photo, FrameSpec(style="darkroom", output_ratio="4:5"), {})
    assert 2 < float(paper[:20, :, 0].std()) < 7


@pytest.mark.parametrize("size", [(1200, 800), (800, 1200), (1300, 480)])
@pytest.mark.skipif(not (REFERENCE / "gxr_core.py").is_file(), reason="local approved design reference is absent")
def test_export_carrier_expands_to_the_approved_organic_contours(size):
    sys.path.insert(0, str(REFERENCE))
    try:
        reference = importlib.import_module("gxr_styles_b").darkroom(*size, META)
    finally:
        sys.path.pop(0)
    w, h = size
    c = max(10, round(0.017 * min(size)))
    jit = max(4, round(0.5 * c))
    contours = carrier_contours(w, h, c, jit, 19790324)
    x, y = reference["photo"]
    for actual, expected in zip(contours, reference["items"][:2], strict=True):
        np.testing.assert_allclose(np.array(actual) + [x, y], expected["pts"], rtol=0, atol=1e-10)


def test_browser_normalization_defaults_and_payload_preserve_all_new_styles(tmp_path):
    out = run_node(
        tmp_path,
        f"""
      const styles={list(STYLES)!r};
      return styles.map(style=>{{
        const frame=F.normalizeFrame({{version:2,style,text:'海边',output_ratio:'4:5',show_params:false}});
        return {{body:F.frameBody(frame),defaults:F.defaultsPayload(frame,{{}}).frame,
          old:F.resolveFinish({{frame:{{...frame,retired:true}}}},{{}}).frame}};
      }});
    """,
    )
    for style, value in zip(STYLES, out, strict=True):
        expected = FrameSpec(style=style, text="海边", output_ratio="4:5", show_params=False).to_json()
        assert value["body"] == value["defaults"] == expected
        assert value["old"] == FrameSpec().to_json()
