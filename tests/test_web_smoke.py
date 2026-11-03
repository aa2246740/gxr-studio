"""前端冒烟：不开浏览器。页面和静态资源能取到，模块引用都存在，接口按界面的顺序走一遍（插卡→导入→图库→裁切→导出）。

真正的点按、拖动、显示效果在浏览器里人工核对，见 research/studio_m1_web/MANUAL_CHECKLIST.md。
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import PORT
from gxr_studio.app import create_app

WEB = Path(__file__).resolve().parents[1] / "web"
ASSETS = WEB / "assets"
JS_FILES = sorted(ASSETS.glob("*.js"))


@pytest.fixture
def web_client(settings):
    app = create_app(settings.with_(web_dir=WEB))
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        yield c


def _wait_job(client, job_id, limit=600):
    job = client.get(f"/api/jobs/{job_id}").json()
    for _ in range(limit):
        if not job["running"]:
            return job
        time.sleep(0.05)
        job = client.get(f"/api/jobs/{job_id}").json()
    raise AssertionError("任务超时")


def _tree_snapshot(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = (p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest())
    return out


# ---- 页面与资源 -------------------------------------------------------------


def test_page_is_served_with_the_app_shell(web_client):
    r = web_client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "GXR Studio" in html
    assert '<script type="module" src="/assets/app.js">' in html
    assert '<link rel="stylesheet" href="/assets/app.css">' in html
    assert 'id="view"' in html and 'id="banner"' in html and 'id="card-status"' in html


def test_static_assets_are_served(web_client):
    for name in ("app.js", "app.css", "api.js", "state.js", "dom.js"):
        r = web_client.get(f"/assets/{name}")
        assert r.status_code == 200, name


def test_page_needs_the_token(settings):
    app = create_app(settings.with_(web_dir=WEB))
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as anon:
        assert anon.get("/api/cards").status_code == 401
        assert anon.get("/assets/app.js").status_code == 401


def test_frontend_has_no_external_resources():
    files = [WEB / "index.html", *JS_FILES, ASSETS / "app.css"]
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"https?://", text), f"{f.name} 引用了外部地址（应完全离线）"


def test_module_imports_resolve():
    for f in JS_FILES:
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r"from\s+'\./([\w.]+)'", text):
            assert (ASSETS / m.group(1)).is_file(), f"{f.name} 引用了不存在的 {m.group(1)}"


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过语法检查")
def test_javascript_parses(tmp_path):
    # 复制成 .mjs 再检查，不在源目录里执行任何东西
    for f in JS_FILES:
        target = tmp_path / (f.stem + ".mjs")
        target.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
        res = subprocess.run(["node", "--check", str(target)], capture_output=True, text=True, encoding="utf-8")
        assert res.returncode == 0, f"{f.name}: {res.stderr}"


# ---- 用户流程（接口顺序与界面一致）-------------------------------------------


def test_plug_in_import_gallery_crop_export(web_client, card, settings, volumes):
    card_root = volumes / "NO NAME"
    before = _tree_snapshot(card_root)

    # 1. 插卡：发现卡，摘要里有新照片
    cards = web_client.get("/api/cards").json()
    assert len(cards) == 1
    card_id = cards[0]["id"]
    summary = web_client.get(f"/api/cards/{card_id}/summary").json()
    assert summary["new_shots"] > 0

    # 2. 导入：轮询进度直到完成，汇总能算出新增、跳过、读不出来
    job = web_client.post("/api/import", json={"card_id": card_id})
    assert job.status_code == 202
    done = _wait_job(web_client, job.json()["id"])
    p = done["progress"]
    assert p["state"] == "done", p
    assert p["imported_shots"] > 0
    # 假卡 DCIM 里的 ._R0000101.JPG 是 macOS 系统文件（忽略）；Thumbs.db、NOTE.TXT 不会被导入：不说"可以推出"，如实说个数
    assert p["message"].endswith("存储卡上有 2 个文件不会被导入") and "可以推出" not in p["message"]
    assert isinstance(p["unreadable"], list)

    # 3. 再插一次：摘要不读内容，证明不了"都导入过了"；界面会再导入一次，读内容核对后一个都不复制
    again = web_client.get(f"/api/cards/{card_id}/summary").json()
    assert again["new_shots"] == again["shots"] and again["verified"] is False
    p2 = _wait_job(web_client, web_client.post("/api/import", json={"card_id": card_id}).json()["id"])["progress"]
    assert p2["copied_files"] == 0 and p2["coverage"]["covered"] == p2["coverage"]["total"]
    assert p2["message"] == "存储卡上有 2 个文件不会被导入"

    # 4. 图库：日期、照片、角标需要的三种情况都有
    dates = web_client.get("/api/dates").json()
    assert dates and all({"date", "count", "raw_count", "jpg_count", "cover_id"} <= d.keys() for d in dates)
    photos = web_client.get("/api/photos", params={"limit": 5000}).json()
    assert any(p["has_raw"] and p["has_jpg"] for p in photos)
    assert any(p["has_jpg"] and not p["has_raw"] for p in photos)
    assert any(p["has_raw"] and not p["has_jpg"] for p in photos)
    one_day = web_client.get("/api/photos", params={"date": dates[0]["date"]}).json()
    assert one_day and all(p["date"] == dates[0]["date"] for p in one_day)

    pair = next(p for p in photos if p["has_raw"] and p["has_jpg"])
    raw_only = next(p for p in photos if p["has_raw"] and not p["has_jpg"])
    thumb = web_client.get(pair["thumb_url"])
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"
    assert web_client.get(pair["preview_url"]).status_code == 200

    # 5. 大图与裁切：默认没有裁切，设置 3:2 / XPan / 拉直 / 挪位置 / 还原
    crop = web_client.get(f"/api/photos/{pair['id']}/crop").json()
    assert crop["active"] is False and crop["frame"]["width"] > 0

    r = web_client.put(f"/api/photos/{pair['id']}/crop", json={"preset": "3:2", "rect": None, "angle_deg": 0})
    assert r.status_code == 200 and r.json()["active"] is True
    rect = r.json()["rect"]
    assert rect[2] <= 1 and rect[3] <= 1

    r = web_client.put(f"/api/photos/{pair['id']}/crop", json={"preset": "xpan-65-24", "rect": None, "angle_deg": 2.5})
    assert r.status_code == 200, r.text
    xpan = r.json()
    assert xpan["angle_deg"] == 2.5
    # XPan 是满宽的条，只能上下挪：界面拖动松手时就是这样保存的
    x, y, w, h = xpan["rect"]
    new_y = y + 0.05 if y + h + 0.05 <= 1 else y - 0.05
    moved = [x, new_y, w, h]
    r = web_client.put(
        f"/api/photos/{pair['id']}/crop",
        json={"preset": "xpan-65-24", "rect": moved, "angle_deg": 2.5, "expected_rev": xpan["rev"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["rev"] == xpan["rev"] + 1

    # 旧版本号再存：被拒绝，界面会重新读取
    stale = web_client.put(
        f"/api/photos/{pair['id']}/crop",
        json={"preset": "xpan-65-24", "rect": moved, "angle_deg": 2.5, "expected_rev": xpan["rev"]},
    )
    assert stale.status_code == 409 and stale.json()["error"] == "rev_conflict"

    # 框超出画面：被拒绝（界面会提示"挪过头了"并回到上一次的位置）
    out = web_client.put(
        f"/api/photos/{pair['id']}/crop",
        json={"preset": "xpan-65-24", "rect": [0.9, 0.5, 0.3, 0.3], "angle_deg": 0},
    )
    assert out.status_code == 422

    reset = web_client.delete(f"/api/photos/{pair['id']}/crop")
    assert reset.status_code == 200 and reset.json()["active"] is False

    # 6. 导出：一张有 JPG 的能导出；只有 RAW 的给 409，界面会用大白话说明原因
    exp_dir = settings.export_dir
    res = web_client.post(
        f"/api/photos/{pair['id']}/export",
        json={"long_edge": 1600, "quality": 85, "dest_dir": str(exp_dir)},
    )
    assert res.status_code == 200, res.text
    exported = Path(res.json()["path"])
    assert exported.name.endswith("_gxr.jpg") and exported.is_file()
    assert max(res.json()["width"], res.json()["height"]) <= 1600

    raw_res = web_client.post(f"/api/photos/{raw_only['id']}/export", json={"quality": 85, "dest_dir": str(exp_dir)})
    assert raw_res.status_code == 409 and raw_res.json()["error"] == "no_jpg_source"

    # 7. 多选导出：后台任务，结果里 RAW-only 单独报错，其余照常导出
    ids = [pair["id"], raw_only["id"]]
    batch = web_client.post("/api/export", json={"ids": ids, "quality": 85, "dest_dir": str(exp_dir)})
    assert batch.status_code == 202
    bdone = _wait_job(web_client, batch.json()["id"])
    results = bdone["progress"]["results"]
    assert bdone["progress"]["state"] == "done"
    assert len(results) == 2
    assert sum(1 for r in results if r.get("error") == "no_jpg_source") == 1
    assert sum(1 for r in results if "path" in r) == 1

    # 8. 卡只读：整棵卡的内容前后一模一样
    assert _tree_snapshot(card_root) == before


def test_settings_and_rebuild_for_the_settings_sheet(web_client, card):
    s = web_client.get("/api/settings").json()
    assert {"library_dir", "export_dir", "free_bytes", "photo_count", "undated_policy"} <= s.keys()
    r = web_client.post("/api/library/rebuild")
    assert r.status_code == 200 and "shots" in r.json()


def test_cancel_unknown_job_is_404_message(web_client):
    r = web_client.post("/api/jobs/nope/cancel")
    assert r.status_code == 404
    assert r.json()["message"]
