"""HTTP 接口端到端：发现卡 → 导入 → 日期/照片/缩略图/预览 → 裁切 → 导出。（M1 验收 6、7、8 的后端部分）"""

from __future__ import annotations

import io
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import PORT
from gxr_studio import audit
from gxr_studio.app import create_app
from gxr_studio.library.store import sha256_file


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        yield c


def import_all(client, card_label=None, **body):
    cards = client.get("/api/cards").json()
    assert cards, "应该能发现假卡"
    card = cards[0]
    r = client.post("/api/import", json={"card_id": card["id"], **body})
    assert r.status_code == 202, r.text
    job = r.json()
    for _ in range(600):
        job = client.get(f"/api/jobs/{job['id']}").json()
        if not job["running"]:
            return job
        time.sleep(0.05)
    raise AssertionError("导入超时")


def find(client, name, date=None):
    photos = client.get("/api/photos", params={"date": date} if date else {}).json()
    return next(p for p in photos if p["name"] == name)


def test_health_and_settings_use_temp_dirs(client, settings):
    assert client.get("/api/health").json()["ok"] is True
    s = client.get("/api/settings").json()
    assert s["library_dir"] == str(settings.library_dir)
    assert "Pictures" not in s["library_dir"] and "Pictures" not in s["export_dir"]


def test_home_page_lists_photos(client, card):
    html = client.get("/").text
    assert "<title>" in html and "/api/photos" in html


def test_cards_summary_and_import_flow(client, card):
    (c,) = client.get("/api/cards").json()
    assert c["label"] == "NO NAME"
    sm = client.get(f"/api/cards/{c['id']}/summary").json()
    assert sm["shots"] == len(card.shots) and sm["new_shots"] == len(card.shots)
    job = import_all(client)
    assert job["error"] is None and job["progress"]["state"] == "done"
    assert job["progress"]["imported_shots"] == len(card.unique_shots)
    # 不读内容证明不了"已导入"（设计 §4.3，SEEN_TRUST_UUID 默认关）：摘要照样把每张都算作要核对的
    sm2 = client.get(f"/api/cards/{c['id']}/summary").json()
    assert sm2["new_shots"] == len(card.shots) and sm2["verified"] is False
    # 再导一次：读内容核对，什么都不复制
    job2 = import_all(client)
    assert job2["progress"]["copied_files"] == 0 and job2["progress"]["imported_shots"] == 0
    assert job2["progress"]["coverage"]["covered"] == job2["progress"]["coverage"]["total"]


def test_unknown_card_is_404_and_paths_are_not_accepted(client, card):
    # card_id 是挂载会话号：格式不对 400（设计 §15），格式对但不存在 404
    r = client.post("/api/import", json={"card_id": "../../etc"})
    assert r.status_code == 400 and r.json()["error"] == "bad_card_id"
    r = client.post("/api/import", json={"card_id": "0" * 26})
    assert r.status_code == 404 and r.json()["error"] == "card_not_found"


def test_dates_photos_thumb_preview(client, card):
    import_all(client)
    dates = client.get("/api/dates").json()
    assert [d["date"] for d in dates] == sorted((d["date"] for d in dates), reverse=True)
    assert sum(d["count"] for d in dates) == len(card.unique_shots)
    day = client.get("/api/photos", params={"date": "2026-10-18"}).json()
    assert {p["name"] for p in day} >= {"R0000104", "R0000105", "R0000106"}
    p = next(p for p in day if p["name"] == "R0000105")
    assert (p["width"], p["height"]) == (480, 640), "方向 6 的竖拍要按转正后的尺寸"
    assert p["has_raw"] and p["has_jpg"] and p["preview_kind"] == "jpg"
    th = client.get(p["thumb_url"])
    assert th.status_code == 200 and th.headers["content-type"] == "image/jpeg"
    im = Image.open(io.BytesIO(th.content))
    assert max(im.size) <= 256 and im.height > im.width  # 竖拍，缩略图也是竖的
    pv = Image.open(io.BytesIO(client.get(p["preview_url"]).content))
    assert pv.size == (480, 640)  # 不放大
    # 只有 DNG：用 DNG 里的预览
    raw_only = find(client, "R0000109", "2026-10-19")
    assert raw_only["has_jpg"] is False and raw_only["preview_kind"] == "dng-preview"
    assert client.get(raw_only["thumb_url"]).status_code == 200
    assert client.get("/api/photos/deadbeef/thumb").status_code == 404
    assert client.get("/api/photos", params={"date": "unknown"}).json() == []  # 默认策略：没日期的退回文件时间


def test_crop_api(client, card):
    import_all(client)
    p = find(client, "R0000102")
    r = client.get(f"/api/photos/{p['id']}/crop").json()
    assert r["active"] is False and r["pixels"] == {"x": 0, "y": 0, "width": 640, "height": 480}
    put = client.put(f"/api/photos/{p['id']}/crop", json={"preset": "xpan-65-24"})
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["active"] and body["pixels"]["width"] == 640 and body["pixels"]["height"] == 236 and body["rev"] == 1
    # 位置可拖
    x, y, w, h = body["rect"]
    moved = client.put(f"/api/photos/{p['id']}/crop", json={"preset": "xpan-65-24", "rect": [x, 0.0, w, h], "expected_rev": 1})
    assert moved.status_code == 200 and moved.json()["pixels"]["y"] == 0 and moved.json()["rev"] == 2
    # 列表里能看到
    assert find(client, "R0000102")["crop"]["preset"] == "xpan-65-24"
    # 版本冲突、非法
    assert client.put(f"/api/photos/{p['id']}/crop", json={"preset": "1:1", "expected_rev": 1}).status_code == 409
    bad = client.put(f"/api/photos/{p['id']}/crop", json={"preset": "xpan-65-24", "rect": [0, 0, 1, 1]})
    assert bad.status_code == 422 and bad.json()["error"] == "ratio_mismatch"
    assert (
        client.put(f"/api/photos/{p['id']}/crop", json={"preset": "free", "rect": [0, 0, 1, 1], "angle_deg": 50}).json()["error"] == "bad_angle"
    )
    # 还原
    gone = client.delete(f"/api/photos/{p['id']}/crop").json()
    assert gone["active"] is False
    assert find(client, "R0000102")["crop"] is None
    assert any(x["id"] == "xpan-65-24" and x["ratio"] == [65, 24] for x in client.get("/api/crop-presets").json())


def test_export_flow_and_source_untouched(client, card, settings):
    import_all(client)
    p = find(client, "R0000102")
    lib_files = sorted((settings.library_dir / "2026" / "2026-10-17").glob("R0000102.*"))
    before = {f.name: sha256_file(f) for f in lib_files if f.suffix != ".json"}
    client.put(f"/api/photos/{p['id']}/crop", json={"preset": "16:9"})
    r = client.post(f"/api/photos/{p['id']}/export", json={"long_edge": 320, "quality": 80})
    assert r.status_code == 200, r.text
    out = r.json()
    path = Path(out["path"])
    assert path.name == "R0000102_gxr.jpg" and path.parent == settings.export_dir
    im = Image.open(path)
    assert im.size == (320, 180) and (out["width"], out["height"]) == (320, 180)
    assert im.info.get("icc_profile")
    assert im.getexif().get(274) == 1
    sub = im.getexif().get_ifd(0x8769)
    assert sub[36867] == "2026:10:17 10:15:30" and sub[34855] == 200 and "50mm" in sub[42036]
    # 再导一次：不覆盖，加 -2
    r2 = client.post(f"/api/photos/{p['id']}/export", json={"quality": 90}).json()
    assert Path(r2["path"]).name == "R0000102_gxr-2.jpg" and (r2["width"], r2["height"]) == (640, 360)
    assert path.exists()
    after = {f.name: sha256_file(f) for f in lib_files if f.suffix != ".json"}
    assert after == before, "导出不能改动原文件"


def test_export_errors(client, card, settings, volumes):
    import_all(client)
    p = find(client, "R0000102")
    raw_only = find(client, "R0000109", "2026-10-19")
    r = client.post(f"/api/photos/{raw_only['id']}/export", json={})
    assert r.status_code == 409 and r.json()["error"] == "no_jpg_source"
    on_card = client.post(f"/api/photos/{p['id']}/export", json={"dest_dir": str(card.root / "DCIM" / "out")})
    assert on_card.status_code == 422 and on_card.json()["error"] == "bad_destination"
    assert not (card.root / "DCIM" / "out").exists()
    in_lib = client.post(f"/api/photos/{p['id']}/export", json={"dest_dir": str(settings.library_dir / "2026")})
    assert in_lib.status_code == 422 and in_lib.json()["error"] == "bad_destination"
    assert client.post(f"/api/photos/{p['id']}/export", json={"dest_dir": "relative/dir"}).status_code == 422
    refused = "请求无效。请刷新页面后重试。"
    for body, detail in (
        ({"quality": 0}, "1"),
        ({"quality": 101}, "100"),
        ({"long_edge": 63}, "64"),
        ({"long_edge": 30001}, "30000"),
    ):
        bad = client.post(f"/api/photos/{p['id']}/export", json=body)
        assert bad.status_code == 422 and bad.json()["message"] == refused and detail in bad.json()["detail"], bad.text
    preview = client.get(f"/api/photos/{p['id']}/render", params={"stage": "final", "size": "preview", "quality": 29})
    assert preview.status_code == 422 and preview.json()["message"] == refused and "30" in preview.json()["detail"]
    unwritable = client.post(f"/api/photos/{p['id']}/export", json={"dest_dir": "/proc/definitely/not/here"})
    assert unwritable.status_code == 422 and unwritable.json()["error"] == "dest_unwritable"


def test_batch_export_by_date(client, card, settings):
    import_all(client)
    r = client.post("/api/export", json={"date": "2026-10-19", "long_edge": 200, "quality": 70})
    assert r.status_code == 202
    job = r.json()
    for _ in range(300):
        job = client.get(f"/api/jobs/{job['id']}").json()
        if not job["running"]:
            break
        time.sleep(0.05)
    prog = job["progress"]
    assert prog["state"] == "done" and prog["done"] == prog["total"] == 5
    ok = [x for x in prog["results"] if "path" in x]
    bad = [x for x in prog["results"] if "error" in x]
    assert len(ok) == 2 and len(bad) == 3 and {x["error"] for x in bad} == {"no_jpg_source"}  # 2 张有 JPG，3 张只有 RAW
    assert len(list(settings.export_dir.glob("*_gxr.jpg"))) == 2


def test_rebuild_endpoint(client, card, settings):
    import_all(client)
    before = client.get("/api/photos", params={"limit": 1000}).json()
    r = client.post("/api/library/rebuild").json()
    assert r["shots"] == len(card.unique_shots) and r["seen_restored"] > 0 and r["hashed"] == 0
    assert client.get("/api/photos", params={"limit": 1000}).json() == before


def test_audit_shows_no_card_writes_and_no_network(client, card):
    import_all(client)
    p = find(client, "R0000102")
    client.post(f"/api/photos/{p['id']}/export", json={})
    rec = client.get("/api/audit").json()
    assert rec["write_count"] > 0
    assert not [w for w in rec["recent"] if str(card.root) in w["path"]]
    assert audit.network_attempts() == []


def test_import_rejects_second_concurrent_job(client, card, settings):
    import threading

    gate = threading.Event()
    from gxr_studio.cards.importer import ImportOptions

    app = create_app(settings, import_options=ImportOptions(min_free_bytes=0, before_verify=lambda *a: gate.wait(5)))
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c2:
        cid = c2.get("/api/cards").json()[0]["id"]
        first = c2.post("/api/import", json={"card_id": cid})
        assert first.status_code == 202
        second = c2.post("/api/import", json={"card_id": cid})
        assert second.status_code == 409 and second.json()["error"] == "import_running"
        c2.post(f"/api/jobs/{first.json()['id']}/cancel")
        gate.set()
        for _ in range(200):
            if not c2.get(f"/api/jobs/{first.json()['id']}").json()["running"]:
                break
            time.sleep(0.05)
        assert c2.get(f"/api/jobs/{first.json()['id']}").json()["progress"]["state"] in ("cancelled", "done")


def test_real_sample_xpan_export(volumes, real_card, client):
    """真实 4288×2848 样本：XPan 65:24 → 4288×1583，带 EXIF 与 sRGB ICC，系统自带的 sips 能打开。"""
    import_all(client)
    p = find(client, "R8051144")
    assert (p["width"], p["height"]) == (4288, 2848)
    client.put(f"/api/photos/{p['id']}/crop", json={"preset": "xpan-65-24"})
    out = client.post(f"/api/photos/{p['id']}/export", json={}).json()
    assert (out["width"], out["height"]) == (4288, 1583)
    im = Image.open(out["path"])
    assert im.size == (4288, 1583) and im.info.get("icc_profile")
    sub = im.getexif().get_ifd(0x8769)
    assert sub[36867] == "2026:10:16 18:51:53" and sub[34855] == 400 and "A12 50mm" in sub[42036]
    if shutil.which("sips"):
        res = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", out["path"]], capture_output=True, text=True)
        assert "4288" in res.stdout and "1583" in res.stdout
    # 带拉直 4°：尺寸 = 裁切结果
    r = client.put(f"/api/photos/{p['id']}/crop", json={"preset": "xpan-65-24", "angle_deg": 4.0}).json()
    out2 = client.post(f"/api/photos/{p['id']}/export", json={}).json()
    assert (out2["width"], out2["height"]) == (r["pixels"]["width"], r["pixels"]["height"])
