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


def _fit_original(width: int, height: int, geometry, target):
    from gxr_studio.crop import crop_box, fit_target_crop
    from gxr_studio.finish.geometry import fitted_crop, output_size

    w, h = output_size(width, height, geometry)
    grown = fit_target_crop("original", target, geometry.straighten, w, h)
    rect = fitted_crop(geometry, grown.rect, preset="original", width=width, height=height, force=True)
    assert rect is not None
    return w, h, rect, crop_box(rect, w, h)


@pytest.mark.parametrize(
    ("width", "height", "vertical"),
    [(4288, 2848, 0), (4301, 2860, 0), (4301, 2861, 0), (640, 481, 0), (640, 480, 40), (640, 480, -60), (4288, 2848, 80)],
)
def test_original_target_keeps_its_place_and_size_on_any_canvas(width, height, vertical):
    """评审（自己查出来的）P1：原始的比例是画布的宽高比，分子分母互质时是大整数。放大太小目标的规则用它算，
    任何目标都被放大成整张画面（位置丢了）。透视、畸变的画布尺寸来自 output_size，多半就是互质的。"""
    from gxr_studio.finish.spec import Geometry

    geometry = Geometry(vertical=vertical)
    target = [0.6, 0.6, 0.2, 0.2]
    w, h, rect, (x, y, bw, bh) = _fit_original(width, height, geometry, target)
    # 目标的比例不是画布的：收成目标里最大的、画布比例的框，中心不动，不比目标大
    assert bw <= round(0.2 * w) + 1 and bh <= round(0.2 * h) + 1, (w, h, rect)
    assert abs((x + bw / 2) - 0.7 * w) <= 2 and abs((y + bh / 2) - 0.7 * h) <= 2, (w, h, rect)
    assert abs(bw / bh - w / h) / (w / h) < 0.01
    # 已经是画布比例的目标：位置和大小都不变（1 像素以内）
    keep = [0.55, 0.5, 0.2, 0.2]  # 归一化的宽高相等，像素上就是画布的比例
    _w, _h, _rect, (kx, ky, kw, kh) = _fit_original(width, height, geometry, keep)
    assert abs(kx - round(0.55 * w)) <= 1 and abs(ky - round(0.5 * h)) <= 1 and abs(kw - round(0.2 * w)) <= 2 and abs(kh - round(0.2 * h)) <= 2


@pytest.mark.parametrize("size", [(4288, 2848), (4301, 2861), (640, 481), (660, 503)])
def test_a_tiny_original_target_grows_around_its_centre(size):
    from gxr_studio.finish.spec import Geometry

    w, h, _rect, (x, y, bw, bh) = _fit_original(size[0], size[1], Geometry(), [0.4, 0.4, 0.004, 0.004])
    assert min(bw, bh) >= 16, (bw, bh)
    assert abs((x + bw / 2) - 0.402 * w) <= 3 and abs((y + bh / 2) - 0.402 * h) <= 3, "放大是以中心为准的，不是变成整张画面"
    assert bw < 0.2 * w


def test_original_on_a_perspective_canvas_keeps_the_target_through_the_api(client, shot):
    assert put(client, shot, preset="original", rect=[0, 0, 1, 1]).status_code == 200
    assert client.put(f"/api/photos/{shot['id']}/edit", json={"geometry": {"vertical": 40}}).status_code == 200
    out = put(client, shot, preset="original", rect=[0.6, 0.6, 0.2, 0.2], fit=True)
    assert out.status_code == 200, out.text
    px, frame = out.json()["pixels"], out.json()["frame"]
    assert abs(px["x"] + px["width"] / 2 - 0.7 * frame["width"]) <= 2 and px["width"] <= 0.2 * frame["width"] + 1


@pytest.mark.parametrize("preset", ["original", "free", "1:1", "4:5", "3:2", "16:9", "16:10", "1.91:1", "2.35:1", "xpan-65-24"])
@pytest.mark.parametrize("size", [(640, 480), (480, 640), (508, 649), (660, 503)])
def test_a_normal_target_is_never_made_bigger(preset, size):
    """评审（自己查出来的）：放大太小目标的规则只能补不够的那一边，横竖不变。
    1.91:1 的分子是 191：竖画布上 100×191 的目标原来被放大成 191×191，收成了横的 191×100，比目标还大。"""
    from gxr_studio.crop import crop_box, fit_target_crop
    from gxr_studio.finish.geometry import fitted_crop
    from gxr_studio.finish.spec import Geometry

    width, height = size
    for target in ([0.3, 0.2, 0.4, 0.6], [0.2, 0.3, 0.6, 0.4]):  # 竖的、横的（都比任何预设的最小框大）
        grown = fit_target_crop(preset, target, 0.0, width, height)
        assert grown.rect == pytest.approx(tuple(target)), (preset, size, grown.rect)
        rect = fitted_crop(Geometry(), grown.rect, preset=preset, width=width, height=height, force=True)
        assert rect is not None
        x, y, bw, bh = crop_box(rect, width, height)
        assert bw <= round(target[2] * width) + 1 and bh <= round(target[3] * height) + 1, (preset, size, target, (bw, bh))
        assert (bw >= bh) == (target[2] * width >= target[3] * height) or preset in ("free", "1:1"), (preset, size, target, (bw, bh))
