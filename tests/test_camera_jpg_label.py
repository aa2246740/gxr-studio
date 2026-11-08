"""舞台确实在用相机 JPG 时，服务器和显卡两条路径都给出同一个来源标签。"""

import pytest

from test_api import find
from test_finish_routes import client  # noqa: F401  client 是夹具
from test_gpu_state import run


@pytest.mark.parametrize("name", ["R0000101", "R0000102"])
@pytest.mark.parametrize("adjust", [{}, {"exposure_ev": 0.5}])
def test_server_camera_jpg_preview_has_an_explicit_source_label(client, name, adjust):  # noqa: F811
    photo = find(client, name)
    response = client.post(f"/api/photos/{photo['id']}/render?stage=final&max_edge=96", json={"adjust": adjust})
    assert response.status_code == 200
    assert response.headers["X-GXR-Label"] == "camera-jpg"


def test_gpu_camera_jpg_preview_has_the_same_source_label(tmp_path):
    out = run(tmp_path, "camera-jpg-label")
    assert out["way"] == "gpu" and out["label"] == "camera-jpg"
