"""边框样式和照片尺寸不再共用 sidecar 的 frame 键。裁切角度不传时保持原值。静态资源不进长期缓存。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from conftest import PORT
from gxr_studio.app import create_app
from test_api import find, import_all


def _client(settings):
    app = create_app(settings)
    return TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token})


def _side(client, photo_id):
    lib = client.app.state.ctx.lib
    shot = lib.get_shot(photo_id)
    path = lib.sidecar_path_for(shot)
    return path, json.loads(path.read_text(encoding="utf-8")), shot


def test_border_keeps_frame_size_and_crop_still_opens(settings, card):
    with _client(settings) as client:
        import_all(client)
        photo = find(client, "R0000102")
        before = client.get(f"/api/photos/{photo['id']}/crop").json()
        width, height = before["frame"]["width"], before["frame"]["height"]
        put = client.put(f"/api/photos/{photo['id']}/edit", json={"frame": {"style": "gallery"}})
        assert put.status_code == 200, put.text
        assert put.json()["frame"]["style"] == "gallery"
        crop = client.get(f"/api/photos/{photo['id']}/crop")
        assert crop.status_code == 200, crop.text
        assert crop.json()["frame"]["width"] == width and crop.json()["frame"]["height"] == height
        _path, side, _shot = _side(client, photo["id"])
        assert side["frame"]["width"] == width and side["frame"]["height"] == height and "style" not in side["frame"]
        assert side["border"]["style"] == "gallery"


def test_delete_edit_and_crop_keep_the_frame_size(settings, card):
    with _client(settings) as client:
        import_all(client)
        photo = find(client, "R0000102")
        width = client.get(f"/api/photos/{photo['id']}/crop").json()["frame"]["width"]
        assert client.put(f"/api/photos/{photo['id']}/edit", json={"frame": {"style": "leica"}}).status_code == 200
        assert client.put(f"/api/photos/{photo['id']}/crop", json={"preset": "1:1", "angle_deg": 2.0}).status_code == 200
        assert client.delete(f"/api/photos/{photo['id']}/edit").status_code == 200
        assert client.delete(f"/api/photos/{photo['id']}/crop").status_code == 200
        crop = client.get(f"/api/photos/{photo['id']}/crop")
        assert crop.status_code == 200, crop.text
        assert crop.json()["active"] is False and crop.json()["frame"]["width"] == width
        _path, side, _shot = _side(client, photo["id"])
        assert side["frame"]["width"] == width and "style" not in side["frame"] and "border" not in side


def test_style_only_frame_heals_from_the_photo(settings, card):
    with _client(settings) as client:
        import_all(client)
        photo = find(client, "R0000102")
        original = client.get(f"/api/photos/{photo['id']}/crop").json()["frame"]
        path, side, _shot = _side(client, photo["id"])
        side["frame"] = {"style": "film", "color": "black", "weight": "thin"}
        side.pop("border", None)
        path.write_text(json.dumps(side, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        crop = client.get(f"/api/photos/{photo['id']}/crop")
        assert crop.status_code == 200, crop.text
        body = crop.json()
        assert body["frame"]["width"] == original["width"] and body["frame"]["height"] == original["height"]
        healed = json.loads(path.read_text(encoding="utf-8"))
        assert healed["frame"]["width"] == original["width"] and healed["frame"]["height"] == original["height"]
        assert "style" not in healed["frame"]
        assert healed["border"]["style"] == "none" and healed["border"]["version"] == 2
        edit = client.get(f"/api/photos/{photo['id']}/edit").json()
        assert edit["frame"]["style"] == "none"


def test_omitted_crop_angle_keeps_the_previous_value(settings, card):
    with _client(settings) as client:
        import_all(client)
        photo = find(client, "R0000102")
        first = client.put(f"/api/photos/{photo['id']}/crop", json={"preset": "1:1", "angle_deg": 3.5})
        assert first.status_code == 200, first.text
        assert first.json()["angle_deg"] == 3.5
        kept = client.put(f"/api/photos/{photo['id']}/crop", json={"preset": "1:1", "rect": first.json()["rect"]})
        assert kept.status_code == 200, kept.text
        assert kept.json()["angle_deg"] == 3.5
        zero = client.put(f"/api/photos/{photo['id']}/crop", json={"preset": "1:1", "angle_deg": 0})
        assert zero.status_code == 200 and zero.json()["angle_deg"] == 0.0


def test_jpg_only_capabilities_reason_is_the_short_sentence(settings, card):
    with _client(settings) as client:
        import_all(client)
        photo = find(client, "R0000106")
        cap = client.get(f"/api/photos/{photo['id']}/capabilities").json()
        assert cap["raw"] is False and cap["recipe"] == "none"
        assert cap["reason"] == "配方需要 RAW 文件"


def test_assets_send_cache_control_no_cache(settings, tmp_path):
    assets = tmp_path / "web" / "assets"
    assets.mkdir(parents=True)
    (assets / "app.css").write_text("body{}\n", encoding="utf-8")
    app = create_app(settings.with_(web_dir=tmp_path / "web"))
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        got = client.get("/assets/app.css")
        assert got.status_code == 200, got.text
        assert got.headers["cache-control"] == "no-cache"
