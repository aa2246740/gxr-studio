"""导出前显示的尺寸必须等于文件；查询不会渲染或保存。"""

from __future__ import annotations

import pytest
from PIL import Image

from test_api import find
from test_finish_routes import client as client
from test_raw_export import raw_only as raw_only


@pytest.mark.parametrize("style", ("none", "gallery", "leica", "hasselblad", "film", "instant"))
@pytest.mark.parametrize("ratio", ("3:4", "16:9", "original"))
def test_size_tiers_equal_the_exported_file(client, style, ratio):
    pid = find(client, "R0000102")["id"]
    assert client.put(f"/api/photos/{pid}/crop", json={"preset": "4:3", "orientation": "portrait"}).status_code == 200
    frame = {"style": style, "output_ratio": ratio, "text": "Hello"}
    assert client.put(f"/api/photos/{pid}/edit", json={"frame": frame}).status_code == 200
    result = client.post(f"/api/photos/{pid}/output-size", json={})
    assert result.status_code == 200, result.text
    before = result.json()
    for tier in ("original", "large", "medium", "small"):
        result = client.post(f"/api/photos/{pid}/export", json={"size": tier})
        assert result.status_code == 200, result.text
        out = result.json()
        with Image.open(out["path"]) as image:
            assert list(image.size) == before["sizes"][tier] == [out["width"], out["height"]]
        if tier == "original" and style != "none" and ratio != "original":
            rw, rh = map(int, ratio.split(":"))
            assert out["width"] * rh == out["height"] * rw


def test_size_queries_accept_the_live_crop_without_saving_or_rendering(client, monkeypatch):
    from gxr_studio.finish import serve

    pid = find(client, "R0000102")["id"]
    edit = client.get(f"/api/photos/{pid}/edit").json()
    crop = client.get(f"/api/photos/{pid}/crop").json()

    def no_render(*args, **kwargs):
        pytest.fail("尺寸查询不应渲染照片")

    monkeypatch.setattr(serve, "color_image", no_render)
    result = client.post(
        f"/api/photos/{pid}/output-size",
        json={
            "geometry": {"rotate90": 1},
            "crop": {"preset": "4:3", "rect": [0, 0, 1, 1]},
            "frame": {"style": "instant", "output_ratio": "3:4"},
        },
    )
    assert result.status_code == 200, result.text
    size = result.json()["sizes"]["original"]
    assert size[0] * 4 == size[1] * 3
    assert client.get(f"/api/photos/{pid}/edit").json() == edit
    assert client.get(f"/api/photos/{pid}/crop").json() == crop


@pytest.mark.parametrize("crop", ({"preset": "nope"}, {"rect": [0, 0, -1, 1]}, {"rect": [False, 0, 1, 1]}, {"rect": [0, 0, 2, 1]}))
def test_bad_live_crop_is_rejected(client, crop):
    pid = find(client, "R0000102")["id"]
    assert client.post(f"/api/photos/{pid}/output-size", json={"crop": crop}).status_code == 422


def test_size_query_refuses_a_replaced_jpg_symlink(client, tmp_path):
    pid = find(client, "R0000102")["id"]
    c = client.app.state.ctx
    shot = c.lib.get_shot(pid)
    path = c.lib.abspath(shot["files"]["jpg"]["relpath"])
    other = tmp_path / "other.jpg"
    path.rename(other)
    path.symlink_to(other)
    result = client.post(f"/api/photos/{pid}/output-size", json={})
    assert result.status_code == 422 and result.json()["error"] == "size_unavailable"


@pytest.mark.parametrize("geometry", ({"vertical": 30, "distortion": -20}, {"horizontal": -30, "rotate90": 1}))
def test_geometry_size_is_the_actual_full_size_crop(client, geometry):
    pid = find(client, "R0000102")["id"]
    assert client.put(f"/api/photos/{pid}/edit", json={"geometry": geometry, "frame": {"style": "film"}}).status_code == 200
    assert client.put(f"/api/photos/{pid}/crop", json={"preset": "3:2"}).status_code == 200
    before = client.post(f"/api/photos/{pid}/output-size", json={}).json()
    out = client.post(f"/api/photos/{pid}/export", json={"size": "original"}).json()
    assert before["sizes"]["original"] == [out["width"], out["height"]]


def test_raw_only_size_matches_the_full_sensor_export(lib, settings, raw_only):
    from gxr_studio.finish.serve import export_finish
    from gxr_studio.finish.sizes import output_sizes
    from gxr_studio.finish.spec import FrameSpec
    from gxr_studio.render.params import EditState
    from gxr_studio.render.service import RenderService

    shot, out, _path = raw_only
    render = RenderService(settings.home_dir, lib)
    edit = EditState(frame=FrameSpec(style="instant"))
    sizes = output_sizes(render, lib, shot, edit, None, "3:2")
    result = export_finish(render, lib, shot, edit, {"crop": {"preset": "3:2"}}, out, [], quality=95, long_edge=None, fmt="jpeg")
    assert sizes["sizes"]["original"] == [result.width, result.height]
