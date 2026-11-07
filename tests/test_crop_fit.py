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
