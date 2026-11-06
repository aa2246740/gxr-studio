"""M2 前端冒烟：修图视图（配方、旋钮、调整、曲线、预览）用到的接口，按界面的顺序走一遍。

全部用假卡，没有相机数据（还没导入 GXRCE.DAT），所以预览应该返回"相机数据还没导入"，而不是一张图。
真正的点按、拖动、显示效果在浏览器里人工核对，见 research/studio_m2_web/MANUAL_CHECKLIST.md。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import PORT
from gxr_studio.app import create_app
from support.waiting import wait_job

WEB = Path(__file__).resolve().parents[1] / "web"
ASSETS = WEB / "assets"


@pytest.fixture
def web_client(settings):
    app = create_app(settings.with_(web_dir=WEB))
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        yield c


@pytest.fixture
def imported(web_client, card):
    """插卡、导入，返回 (有 RAW 的照片, 只有 RAW 的照片)。"""
    card_id = web_client.get("/api/cards").json()[0]["id"]
    job = web_client.post("/api/import", json={"card_id": card_id})
    assert wait_job(web_client, job.json()["id"])["progress"]["state"] == "done"
    photos = web_client.get("/api/photos", params={"limit": 5000}).json()
    pair = next(p for p in photos if p["has_raw"] and p["has_jpg"])
    raw_only = next(p for p in photos if p["has_raw"] and not p["has_jpg"])
    return pair, raw_only


# ---- 界面文件 ---------------------------------------------------------------


def test_m2_modules_are_served_and_carry_the_user_facing_wording(web_client):
    for name in ("preview.js", "recipes.js", "adjust.js", "camdata.js"):
        assert web_client.get(f"/assets/{name}").status_code == 200, name

    everything = "\n".join(p.read_text(encoding="utf-8") for p in ASSETS.glob("*.js"))
    # 没有相机数据时的引导（大白话，见任务说明）
    assert "从卡导入" in everything
    assert "选择 GXRCE.DAT 文件" in everything
    # 预览一律标"电脑预测"；重置一键完成（COPY 术语表）；存储为我的配方在配方页里是置灰的占位
    assert "电脑预测" in everything
    assert "'重置'" in (ASSETS / "adjust.js").read_text(encoding="utf-8")
    assert "存储为我的配方" in everything
    # 写不进旧版相机配方文件的旋钮标"仅电脑"（COPY 3.10：调整面板标题旁不再标）
    assert "'仅电脑'" in (ASSETS / "recipes.js").read_text(encoding="utf-8")
    # 导出不再拦截只有 RAW 的照片
    assert "暂时不能导出" not in (ASSETS / "export.js").read_text(encoding="utf-8")


# ---- 配方列表 ---------------------------------------------------------------


def test_recipe_catalog_has_three_units_of_eight_bases(web_client):
    r = web_client.get("/api/recipes")
    assert r.status_code == 200
    units = r.json()["units"]
    assert [u["id"] for u in units] == ["jlaunch", "qlaunch", "l08firm"]
    for unit in units:
        assert len(unit["recipes"]) == 8
        assert [x["slot"] for x in unit["recipes"]] == list(range(1, 9))

    unit = units[0]
    bw = unit["recipes"][2]  # 03 高反差黑白
    stock = unit["recipes"][3]  # 04 标准
    assert bw["has_amount"] is False and stock["has_amount"] is False
    assert {c["name"] for c in bw["controls"]} == {"contrast", "highlight", "shadow", "fade", "filter"}
    base = unit["recipes"][0]
    assert base["has_amount"] is True
    amount = next(c for c in base["controls"] if c["name"] == "amount")
    assert amount["amount_step"] in (10, 20, 25)
    assert amount["default"] == 100
    # 每个旋钮都有可选档位，而且默认档位在里面
    for c in base["controls"]:
        assert c["default"] in c["levels"] or c["name"] == "amount"


# ---- 编辑：没有相机数据时 ----------------------------------------------------


def test_edit_roundtrip_without_camera_data_keeps_scopes_and_says_why(web_client, imported):
    pair, _ = imported

    edit = web_client.get(f"/api/photos/{pair['id']}/edit")
    assert edit.status_code == 200
    body = edit.json()
    assert body["scope"] == {"camera": ["recipe"], "computer_only": ["adjust", "curve", "crop", "lut"]}
    assert body["render"]["available"] is False
    assert body["render"]["reason"] == "camera_data_missing"
    assert body["render"]["message"]

    # 改配方：旋钮档位和 memory 都能保存
    put = web_client.put(
        f"/api/photos/{pair['id']}/edit",
        json={
            "recipe": {
                "kind": "builtin",
                "unit": "jlaunch",
                "slot": 5,
                "levels": {"contrast": 2, "saturation": -1},
                "memory": {"jlaunch/5": {"contrast": 2}},
            },
            "expected_rev": body["rev"],
        },
    )
    assert put.status_code == 200, put.text
    saved = put.json()
    assert saved["recipe"]["slot"] == 5 and saved["recipe"]["levels"] == {"contrast": 2, "saturation": -1}
    assert saved["memory"] == {"jlaunch/5": {"contrast": 2}}
    assert saved["rev"] == body["rev"] + 1

    # 调整、曲线：只在电脑上生效，保存后仍在 computer_only 那一块里
    put = web_client.put(
        f"/api/photos/{pair['id']}/edit",
        json={
            "adjust": {
                "exposure_ev": 0.5,
                "wb": {"warm": 10, "tint": -5},
                "contrast": 20,
                "highlight": 0,
                "shadow": -10,
            },
            "curve": {"master": [[0, 0], [128, 140], [255, 255]]},
            "expected_rev": saved["rev"],
        },
    )
    assert put.status_code == 200, put.text
    assert put.json()["curve"]["master"][1] == [128, 140]
    assert put.json()["adjust"]["wb"] == {"warm": 10, "tint": -5}

    # 还原：恒等曲线会被当作"没有曲线"
    put = web_client.put(
        f"/api/photos/{pair['id']}/edit",
        json={"curve": {"master": [[0, 0], [255, 255]]}, "expected_rev": put.json()["rev"]},
    )
    assert put.status_code == 200 and put.json()["curve"] is None

    # 旧版本号再存：冲突，界面会重新读取
    stale = web_client.put(f"/api/photos/{pair['id']}/edit", json={"adjust": None, "expected_rev": 0})
    assert stale.status_code == 409 and stale.json()["error"] == "rev_conflict"


def test_edit_rejects_what_the_sliders_cannot_send(web_client, imported):
    pair, _ = imported
    url = f"/api/photos/{pair['id']}/edit"

    # 03 没有饱和度（界面里也不显示）
    r = web_client.put(url, json={"recipe": {"kind": "builtin", "unit": "jlaunch", "slot": 3, "levels": {"saturation": 1}}})
    assert r.status_code == 422 and r.json()["error"] == "knob_not_available"
    # 配方在别的单元：和镜头对不上
    r = web_client.put(url, json={"recipe": {"kind": "builtin", "unit": "qlaunch", "slot": 4, "levels": {}}})
    assert r.status_code == 422 and r.json()["error"] == "unit_mismatch"
    # 曝光超过 ±5 EV
    r = web_client.put(url, json={"adjust": {"exposure_ev": 6}})
    assert r.status_code == 422
    # 曲线 17 个点（上限 16）
    pts = [[i * 15, i * 15] for i in range(17)]
    r = web_client.put(url, json={"curve": {"master": pts}})
    assert r.status_code == 422 and r.json()["error"] == "bad_curve"


def test_preview_without_camera_data_is_a_clear_409_not_an_image(web_client, imported):
    pair, _ = imported
    r = web_client.post(
        f"/api/photos/{pair['id']}/render",
        json={"recipe": None, "adjust": {"exposure_ev": 0.3}, "curve": None},
        params={"max_edge": 800},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "camera_data_missing"
    assert body["message"] == "无法生成电脑预测。请先导入相机配方文件。"
    assert body["status"]["ready"] is False and body["status"]["hint"]


def test_raw_only_photo_says_no_raw_and_export_falls_back_to_neutral(web_client, imported, settings):
    _, raw_only = imported
    edit = web_client.get(f"/api/photos/{raw_only['id']}/edit").json()
    assert edit["render"]["available"] is False  # 没有相机数据（这一项先于 RAW 判断）

    # 导出：只有 RAW、没有相机数据时走中性转换；这张卡上的 DNG 是假的（读不出来），所以给出明确原因，而不是拦截按钮
    res = web_client.post(
        f"/api/photos/{raw_only['id']}/export",
        json={"quality": 85, "dest_dir": str(settings.export_dir), "source": "auto"},
    )
    assert res.status_code == 409, res.text
    assert res.json()["error"] in ("no_jpg_source", "raw_unreadable")
    assert res.json()["message"]
