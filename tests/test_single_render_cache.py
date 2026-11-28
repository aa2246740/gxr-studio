"""编辑过的单张照片：全尺寸成片按编辑缓存（UNIT backlog-4a），全尺寸成片挤不掉编辑器要的预览。

- 同一张、同一份编辑再要一次全尺寸成片：`X-GXR-Cache: hit`，字节和第一次、和清掉缓存后重算的都一样；
- 改了编辑：miss，字节不同；
- 换 LUT、换标志：miss；
- 一连串全尺寸成片之后，之前几张照片的预览还在缓存里（成片有自己的子限额）。

实拍样本用 captures/a12-50 的 RAW+JPG（conftest 的 real_card），相机数据和校准用 g250。不打印文件名和 EXIF。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from conftest import PORT
from gxr_studio.app import create_app
from test_api import import_all
from test_custom_logo import _transparent_mark_bytes
from test_halfframe_p0 import _setup_g250_calibrated_home
from test_lut import cube_bytes, upload, warm

FULL = "/api/photos/{}/render?stage=final&size=full"
PREVIEW = "/api/photos/{}/render?stage=final&size=preview"
NO_FRAME = {"style": "none"}
NO_STAMP = {"enabled": False}


@pytest.fixture
def studio(settings: Any, real_card: Path) -> Any:
    _setup_g250_calibrated_home(settings)
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        import_all(c)
        ctx = c.app.state.ctx  # type: ignore[attr-defined]
        shots = [ctx.lib.get_shot(p["id"]) for p in c.get("/api/photos").json()]
        # 只要真样本（假卡里的合成照片很小，RAW 也读不出全尺寸）
        photos = [s["shot_id"] for s in shots if {"dng", "jpg"} <= set(s["files"]) and int(s.get("width") or 0) >= 2000]
        assert len(photos) >= 3
        unit = ctx.render.photo_unit(ctx.lib.get_shot(photos[0])) or "jlaunch"
        yield c, photos, unit


def _body(unit: str, ev: float, **over: Any) -> dict[str, Any]:
    """和桌面编辑器 `draftBody` 发的形状一样：边框和时间戳在浏览器里画。"""
    body = {
        "recipe": {"unit": unit, "slot": 3},
        "lut": None,
        "adjust": {"exposure_ev": ev},
        "curve": None,
        "geometry": None,
        "frame": NO_FRAME,
        "stamp": NO_STAMP,
    }
    return {**body, **over}


def _save(c: TestClient, pid: str, body: dict[str, Any]) -> None:
    keep = {k: body[k] for k in ("recipe", "lut", "adjust", "frame", "stamp")}
    r = c.put(f"/api/photos/{pid}/edit", json=keep)
    assert r.status_code == 200, r.text


def _full(c: TestClient, pid: str, body: dict[str, Any]) -> Any:
    r = c.post(FULL.format(pid), json=body)
    assert r.status_code == 200, r.text
    return r


def _stage_bytes(c: TestClient) -> int:
    results = c.app.state.ctx.render._results  # type: ignore[attr-defined]
    return sum(v[1] for k, v in list(results._d.items()) if isinstance(k, tuple) and k[:1] == ("stage",))


def test_edited_single_full_render_twice_is_miss_then_hit_with_equal_bytes(studio: Any) -> None:
    c, photos, unit = studio
    pid, body = photos[0], _body(unit, 0.5)
    _save(c, pid, body)
    first = _full(c, pid, body)
    assert first.headers["X-GXR-Cache"] == "miss"
    assert first.headers["X-GXR-Label"] == "prediction", "应该是 RAW 套配方的成片，不是退回的相机 JPG"
    again = _full(c, pid, body)
    assert again.headers["X-GXR-Cache"] == "hit", "同一份编辑的全尺寸成片又算了一遍"
    assert again.content == first.content
    for name in ("X-GXR-Label", "X-GXR-Calibrated", "X-GXR-Scale", "X-GXR-Prediction"):
        assert again.headers.get(name) == first.headers.get(name), name
    # 缓存给的字节就是重新算出来的字节
    c.app.state.ctx.render._results.clear()  # type: ignore[attr-defined]
    fresh = _full(c, pid, body)
    assert fresh.headers["X-GXR-Cache"] == "miss" and fresh.content == first.content


def test_edited_single_full_render_after_edit_is_miss_with_new_bytes(studio: Any) -> None:
    c, photos, unit = studio
    pid, before_body = photos[0], _body(unit, 0.5)
    _save(c, pid, before_body)
    before = _full(c, pid, before_body)
    assert _full(c, pid, before_body).headers["X-GXR-Cache"] == "hit"
    after_body = _body(unit, 1.0)
    _save(c, pid, after_body)
    after = _full(c, pid, after_body)
    assert after.headers["X-GXR-Cache"] == "miss", "改了编辑还拿到了缓存"
    assert after.content != before.content
    assert _full(c, pid, after_body).headers["X-GXR-Cache"] == "hit"


def test_single_full_render_lut_or_logo_change_is_miss(studio: Any) -> None:
    c, photos, unit = studio
    pid = photos[1]
    luts = []
    for name, fun in (("warm.cube", warm), ("cool.cube", lambda x: x[..., ::-1] * 0.9 + 0.05)):
        r = upload(c, cube_bytes(fun, 17, name), name)
        assert r.status_code == 200, r.text
        cid = r.json()["cube_id"]
        assert c.patch(f"/api/luts/{cid}", json={"input_space": "srgb"}).status_code == 200
        luts.append(cid)
    seen: list[bytes] = []
    for cid in luts:
        body = _body(unit, 0.0, lut=cid)
        _save(c, pid, body)
        miss = _full(c, pid, body)
        assert miss.headers["X-GXR-Cache"] == "miss", "换了 LUT 还拿到了缓存"
        assert _full(c, pid, body).headers["X-GXR-Cache"] == "hit"
        seen.append(miss.content)
    assert seen[0] != seen[1]
    # 同一个 LUT 换了输入色彩空间：出图标识变了，也是 miss
    assert c.patch(f"/api/luts/{luts[-1]}", json={"input_space": "gamma24"}).status_code == 200
    respaced = _full(c, pid, body)
    assert respaced.headers["X-GXR-Cache"] == "miss", "LUT 的色彩规范变了还拿到了缓存"
    assert respaced.content != seen[1]

    logos = []
    for color in ((180, 28, 36, 255), (30, 90, 170, 255)):
        r = c.post("/api/logos", content=_transparent_mark_bytes(color=color))
        assert r.status_code == 200, r.text
        logos.append(r.json()["logo"]["id"])
    marked: list[bytes] = []
    for logo in logos:
        frame = {"style": "leica", "color": "white", "logo": logo, "logo_mode": "auto", "logo_size": "large"}
        body = _body(unit, 0.0, frame=frame)
        _save(c, pid, body)
        miss = _full(c, pid, body)
        assert miss.headers["X-GXR-Cache"] == "miss", "换了标志还拿到了缓存"
        assert _full(c, pid, body).headers["X-GXR-Cache"] == "hit"
        marked.append(miss.content)
    assert marked[0] != marked[1]


def test_preview_entries_survive_a_burst_of_full_renders(studio: Any) -> None:
    """先看几张照片的预览，再在一张上连出全尺寸成片，总量超过 96 MB 的结果缓存：预览照样是 hit，成片不超过子限额。"""
    c, photos, unit = studio
    looked = photos[:3]
    for pid in looked:
        r = c.post(PREVIEW.format(pid), json=_body(unit, 0.25))
        assert r.status_code == 200 and r.headers["X-GXR-Cache"] == "miss", r.text
        assert c.post(PREVIEW.format(pid), json=_body(unit, 0.25)).headers["X-GXR-Cache"] == "hit"
    render = c.app.state.ctx.render  # type: ignore[attr-defined]
    total, step = 0, 0
    while total <= render._results.limit + (8 << 20):
        step += 1
        assert step <= 80, "全尺寸成片太小，凑不出一次挤压"
        r = c.post(FULL.format(photos[-1]) + "&quality=100", json=_body(unit, round(-2.0 + step * 0.05, 2)))
        assert r.status_code == 200 and r.headers["X-GXR-Cache"] == "miss", r.text
        total += len(r.content)
        last = r
    again = c.post(FULL.format(photos[-1]) + "&quality=100", json=_body(unit, round(-2.0 + step * 0.05, 2)))
    assert again.headers["X-GXR-Cache"] == "hit" and again.content == last.content, "全尺寸成片没进缓存，挤压没发生"
    assert _stage_bytes(c) <= 32 << 20
    for pid in looked:
        assert c.post(PREVIEW.format(pid), json=_body(unit, 0.25)).headers["X-GXR-Cache"] == "hit", "全尺寸成片挤掉了编辑器的预览"
