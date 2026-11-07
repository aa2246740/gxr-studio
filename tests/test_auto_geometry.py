"""「自动」纠正（POST /api/photos/{id}/geometry/auto）只看原片，不受照片上已有编辑的影响。

2026-11-07：照片一有编辑记录，分析就不再用相机 JPG，而是带着编辑渲染（RAW 照片还会改走电脑预测，第一次要两秒多），
边框、调整都会混进分析的画面，同一张照片点两次「自动」会得到两个答案。
"""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from conftest import PORT
from gxr_studio.app import create_app
from test_api import find, import_all


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        yield c


def _tilted_lines(width: int = 640, height: int = 480) -> Image.Image:
    image = Image.new("RGB", (width, height), (24, 24, 24))
    draw = ImageDraw.Draw(image)
    for y in range(60, height - 40, 70):
        draw.line((20, y, width - 20, y), fill=(230, 230, 230), width=5)
    return Image.fromarray(np.asarray(image.rotate(4, resample=Image.Resampling.BICUBIC, fillcolor=(24, 24, 24))))


def test_auto_answer_does_not_depend_on_the_edits_the_photo_already_has(client, card):
    _tilted_lines().save(card.root / "DCIM" / "100RICOH" / "TILTED.JPG", quality=95)
    import_all(client)
    shot = find(client, "TILTED")
    first = client.post(f"/api/photos/{shot['id']}/geometry/auto").json()
    assert first["confident"] is True

    # 这些编辑原来都会改变分析用的画面（边框的白边、曝光、已有的裁剪）
    edited = client.put(
        f"/api/photos/{shot['id']}/edit",
        json={"frame": {"style": "gallery"}, "adjust": {"exposure_ev": -1.5, "contrast": 40}},
    )
    assert edited.status_code == 200, edited.text
    cropped = client.put(f"/api/photos/{shot['id']}/crop", json={"preset": "1:1", "rect": None})
    assert cropped.status_code == 200, cropped.text

    again = client.post(f"/api/photos/{shot['id']}/geometry/auto").json()
    assert again == first
