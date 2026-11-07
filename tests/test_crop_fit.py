"""PUT /api/photos/{id}/crop 的 `fit`：rect 是目标，服务器收成离它最近的合格框（比例、有效区），不拒绝。

撤销、重做、转 90° 之后的框是界面推出来的，落在新画布的有效区外是常事；原来这时只能被拒（422），界面再改请服务器自己算
一个居中的最大框，用户摆好的位置就丢了。`fit` 保留位置；不带 `fit` 的写入仍然严格校验。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import PORT
from gxr_studio import crop as cropmod
from gxr_studio.app import create_app
from test_api import find, import_all


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        yield c


@pytest.fixture
def shot(client, card):
    import_all(client)
    return find(client, "R0000102")


def put(client, shot, **body):
    return client.put(f"/api/photos/{shot['id']}/crop", json=body)


def test_a_box_with_the_wrong_shape_is_rejected_unless_fit_is_set(client, shot):
    target = [0.1, 0.1, 0.5, 0.8]  # 320 × 384，不是 4:3 也不是 3:4
    strict = put(client, shot, preset="4:3", rect=target)
    assert strict.status_code == 422 and strict.json()["error"] == "ratio_mismatch"

    fitted = put(client, shot, preset="4:3", rect=target, fit=True)
    assert fitted.status_code == 200, fitted.text
    out = fitted.json()
    px = out["pixels"]
    assert abs(px["width"] / px["height"] - 3 / 4) < 0.01, "横竖跟着目标框（竖的），比例对"
    # 位置保留：中心还在目标框的中心，大小不超过目标框
    frame = out["frame"]
    cx, cy = px["x"] + px["width"] / 2, px["y"] + px["height"] / 2
    assert abs(cx - (0.1 + 0.25) * frame["width"]) <= 2 and abs(cy - (0.1 + 0.4) * frame["height"]) <= 2
    assert px["width"] <= 0.5 * frame["width"] + 1 and px["height"] <= 0.8 * frame["height"] + 1


def test_a_box_outside_the_straightened_picture_is_pulled_in_when_fit_is_set(client, shot):
    client.put(f"/api/photos/{shot['id']}/edit", json={"geometry": {"straighten": 10}})
    full = [0.0, 0.0, 1.0, 1.0]
    strict = put(client, shot, preset="free", rect=full, angle_deg=10)
    assert strict.status_code == 422 and strict.json()["error"] == "crop_outside_image"
    fitted = put(client, shot, preset="free", rect=full, fit=True)
    assert fitted.status_code == 200, fitted.text
    out = fitted.json()
    px, frame = out["pixels"], out["frame"]
    assert cropmod.box_inside((px["x"], px["y"], px["width"], px["height"]), frame["width"], frame["height"], 10.0, 1.0)
    assert 0.5 < px["width"] / frame["width"] < 1.0, "收小了，但没有缩成一个点"


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"preset": "nope", "rect": [0.1, 0.1, 0.5, 0.5]}, "unknown_preset"),
        ({"preset": "free", "rect": [0.1, 0.1, 0.0, 0.5]}, "bad_rect"),
        ({"preset": "free", "rect": [0.1, 0.1, 0.5, -1.0]}, "bad_rect"),
    ],
)
def test_fit_still_refuses_what_cannot_be_a_box(client, shot, body, error):
    res = put(client, shot, fit=True, **body)
    assert res.status_code == 422 and res.json()["error"] == error


@pytest.mark.parametrize("preset", ["free", "4:3", "1:1", "xpan-65-24"])
def test_a_tiny_target_is_grown_to_a_real_box(client, shot, preset):
    """评审 P2：API 能给到比 16 像素还小的目标（界面的最小框拦着）。收出来的框短边不能小于 16 像素。"""
    res = put(client, shot, preset=preset, rect=[0.4, 0.4, 0.004, 0.004], fit=True)
    assert res.status_code == 200, res.text
    px = res.json()["pixels"]
    assert min(px["width"], px["height"]) >= 16, px


def test_a_box_that_cannot_reach_the_minimum_is_too_small(client, shot, monkeypatch):
    from gxr_studio.library import edits

    monkeypatch.setattr(edits, "fitted_crop", lambda *a, **k: (0.5, 0.5, 0.003, 0.003))  # 有效区本身只剩一点点
    res = put(client, shot, preset="free", rect=[0.4, 0.4, 0.2, 0.2], fit=True)
    assert res.status_code == 422 and res.json()["error"] == "too_small"


@pytest.mark.parametrize(
    "geometry",
    [
        {"straighten": 10},
        {"straighten": 10, "rotate90": 1},
        {"vertical": 40},
        {"straighten": -7, "rotate90": 3, "flip": True, "distortion": 30},
    ],
)
def test_original_keeps_the_canvas_ratio_after_a_geometry_write(client, shot, geometry):
    """评审 P2：原始 + 拉直 / 透视，写几何时服务器收出来的是最大内接矩形，比例不是画布的，
    下一次拖框（严格校验）就 ratio_mismatch。现在收出来的框保持画布的比例，原样再写一遍也过。"""
    assert put(client, shot, preset="original", rect=[0, 0, 1, 1]).status_code == 200
    assert client.put(f"/api/photos/{shot['id']}/edit", json={"geometry": geometry}).status_code == 200
    stored = client.get(f"/api/photos/{shot['id']}/crop").json()
    frame, px = stored["frame"], stored["pixels"]
    assert abs((px["width"] / px["height"]) / (frame["width"] / frame["height"]) - 1) < 0.01, (frame, px)
    again = put(client, shot, preset="original", rect=stored["rect"])
    assert again.status_code == 200, again.text
    # fit 的目标也一样：比例是画布的，位置保留
    fitted = put(client, shot, preset="original", rect=[0.3, 0.3, 0.2, 0.5], fit=True)
    assert fitted.status_code == 200, fitted.text
    px = fitted.json()["pixels"]
    ratio = frame["width"] / frame["height"]
    assert min(abs(px["width"] / px["height"] - ratio) / ratio, abs(px["width"] / px["height"] - 1 / ratio) * ratio) < 0.01


@pytest.mark.parametrize(
    "geometry",
    [
        {"straighten": 4, "vertical": 40},
        {"straighten": -6, "distortion": 50, "flip": True},
        {"straighten": 3, "horizontal": -60, "rotate90": 1},
    ],
)
def test_a_stored_box_under_perspective_and_straighten_can_be_written_again(client, shot, geometry):
    """透视 / 畸变拉开过的画布，加上拉直：服务器按有效蒙版收出来的框，原样再写一遍（严格校验）要通过；
    以前 validate_crop 按拉直后的矩形检查四个角，对不上真正的有效区，同样的框被 crop_outside_image 拒掉。"""
    assert put(client, shot, preset="free", rect=[0, 0, 1, 1]).status_code == 200
    assert client.put(f"/api/photos/{shot['id']}/edit", json={"geometry": geometry}).status_code == 200
    stored = client.get(f"/api/photos/{shot['id']}/crop").json()
    again = put(client, shot, preset="free", rect=stored["rect"])
    assert again.status_code == 200, again.text


def test_a_geometry_write_that_changes_the_canvas_size_refits_the_box(client, shot):
    """拉开 → 收回：画布尺寸变了，框是按旧画布的比例存的，不重收，退回没有透视时比例就不对了（存下来的框通不过 validate_crop）。"""
    assert put(client, shot, preset="4:3", rect=None).status_code == 200
    assert client.put(f"/api/photos/{shot['id']}/edit", json={"geometry": {"vertical": 40}}).status_code == 200
    assert client.put(f"/api/photos/{shot['id']}/edit", json={"geometry": {"vertical": 0}}).status_code == 200
    stored = client.get(f"/api/photos/{shot['id']}/crop").json()
    px = stored["pixels"]
    assert abs(px["width"] / px["height"] - 4 / 3) < 0.013, px
    assert put(client, shot, preset="4:3", rect=stored["rect"]).status_code == 200
