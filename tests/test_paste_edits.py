"""POST /api/edits/paste 和 /api/edits/paste/undo：把一张照片的编辑粘贴到别的照片，整批撤销。

粘贴走的就是手动保存的那两条写入路径（`render/edits.write` 和 `write_crop`），所以和手动改成同样的值结果一样；
没勾选的组原样不动；不能用的（别的镜头单元的配方、JPG 上需要 RAW 的 LUT）跳过并说明；撤销不覆盖粘贴之后的修改。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import PORT
from gxr_studio.app import create_app
from test_api import find, import_all
from test_lut_library import cube_bytes, upload

RECIPE = {"kind": "builtin", "unit": "jlaunch", "slot": 2, "levels": {}}
ADJUST = {"exposure_ev": 0.4, "wb": {"warm": 6, "tint": 0}, "contrast": 8, "highlight": -5, "shadow": 12}
CURVE = {"master": [[0, 0], [128, 118], [255, 255]]}
LOOK = {"recipe": RECIPE, "adjust": ADJUST, "curve": CURVE}
FINISH = {"geometry": {"straighten": 1.5}, "frame": {"style": "film"}, "stamp": {"enabled": True, "color": "red"}}
BOX = {"preset": "free", "rect": [0.2, 0.2, 0.5, 0.5]}


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        yield c


@pytest.fixture
def photos(client, card):
    import_all(client)
    return {n: find(client, n) for n in ("R0000101", "R0000102", "R0000103", "R0000104", "R0000105", "R0000106")}


def put_edit(client, photo, **body):
    r = client.put(f"/api/photos/{photo['id']}/edit", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def put_crop(client, photo, **body):
    r = client.put(f"/api/photos/{photo['id']}/crop", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def state(client, photo):
    """编辑和裁剪现在的样子（去掉版本号和只跟版本有关的字段，方便两张照片互相比较）。"""
    edit = client.get(f"/api/photos/{photo['id']}/edit").json()
    crop = client.get(f"/api/photos/{photo['id']}/crop").json()
    for key in ("rev", "render", "warning"):
        edit.pop(key, None)
    for key in ("rev", "warning"):
        crop.pop(key, None)
    return {"edit": edit, "crop": crop}


def rev_of(client, photo):
    return client.get(f"/api/photos/{photo['id']}/edit").json()["rev"]


def paste(client, groups, targets, *, source=None, source_id=None, **extra):
    body = {"groups": groups, "targets": [t if isinstance(t, dict) else {"id": t["id"]} for t in targets], **extra}
    if source is not None:
        body["source"] = source
    if source_id is not None:
        body["source_id"] = source_id
    return client.post("/api/edits/paste", json=body)


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def dress(client, photo, *, finish=True, crop=True):
    """给一张照片摆上全套编辑：配方、调整、曲线，以及几何、边框、时间戳和裁剪。"""
    put_edit(client, photo, **LOOK, **(FINISH if finish else {}))
    if crop:
        put_crop(client, photo, **BOX)


def test_pasting_every_group_equals_applying_the_same_values_by_hand(client, photos):
    source, pasted, by_hand = photos["R0000101"], photos["R0000102"], photos["R0000103"]
    dress(client, source)
    put_edit(client, by_hand, **LOOK, **FINISH)  # 手动：同样的值，先编辑再裁剪，和粘贴的写入顺序一样
    put_crop(client, by_hand, **BOX)
    res = ok(paste(client, ["recipe", "adjust", "geometry", "crop", "frame", "stamp"], [pasted], source_id=source["id"]))
    (item,) = res["results"]
    assert item["status"] == "applied" and item["skipped"] == [] and item["rev"] == rev_of(client, pasted)
    assert item["applied"] == ["recipe", "adjust", "geometry", "crop", "frame", "stamp"]
    assert state(client, pasted) == state(client, by_hand)
    assert res["counts"] == {"applied": 1, "partial": 0, "skipped": 0, "error": 0}


@pytest.mark.parametrize("ratio", ("3:4", "9:16", "16:9", "original"))
def test_frame_ratio_choice_survives_paste_to_another_crop_and_batch_undo(client, photos, ratio):
    source, target = photos["R0000101"], photos["R0000102"]
    put_crop(client, source, preset="4:3", orientation="portrait")
    put_edit(client, source, frame={"style": "instant", "output_ratio": ratio})
    put_crop(client, target, preset="1:1")
    put_edit(client, target, frame={"style": "gallery", "output_ratio": "1:1"})
    before = state(client, target)
    result = ok(paste(client, ["frame"], [target], source_id=source["id"]))
    assert result["results"][0]["status"] == "applied"
    after = state(client, target)
    assert after["edit"]["frame"]["output_ratio"] == ratio
    assert after["crop"] == before["crop"]
    sizes = client.post(f"/api/photos/{target['id']}/output-size", json={}).json()
    w, h = sizes["sizes"]["original"]
    if ratio != "original":
        rw, rh = map(int, ratio.split(":"))
        assert w * rh == h * rw
    assert client.post("/api/edits/paste/undo", json={"batch_id": result["batch_id"]}).status_code == 200
    assert state(client, target) == before


def test_an_inline_source_is_the_same_as_a_source_photo(client, photos):
    source, a, b = photos["R0000101"], photos["R0000102"], photos["R0000103"]
    dress(client, source)
    ok(paste(client, ["recipe", "adjust"], [a], source_id=source["id"]))
    inline = {"recipe": RECIPE, "lut": None, "adjust": ADJUST, "curve": CURVE}
    ok(paste(client, ["recipe", "adjust"], [b], source=inline))
    assert (
        state(client, a)["edit"]["adjust"]
        == state(client, b)["edit"]["adjust"]
        == client.get(f"/api/photos/{source['id']}/edit").json()["adjust"]
    )
    assert state(client, a)["edit"]["recipe"] == state(client, b)["edit"]["recipe"]


def test_rgb_curves_survive_save_paste_read_and_batch_undo(client, photos):
    source, target = photos["R0000101"], photos["R0000102"]
    curves = {
        "master": [[0, 5], [255, 250]],
        "r": [[0, 0], [200, 175], [255, 240]],
        "g": [[0, 0], [128, 140], [255, 255]],
        "b": [[0, 18], [64, 90], [255, 255]],
    }
    put_edit(client, source, curve=curves)
    put_edit(client, target, curve={"g": [[0, 10], [255, 245]]}, adjust={"exposure_ev": 0.3})
    before = state(client, target)
    result = ok(paste(client, ["adjust"], [target], source_id=source["id"], include_state=True))
    assert result["results"][0]["edit"]["curve"] == curves
    assert state(client, target)["edit"]["curve"] == curves
    assert client.post("/api/edits/paste/undo", json={"batch_id": result["batch_id"]}).status_code == 200
    assert state(client, target) == before
    # 内联的剪贴板来源也携带四通道。
    ok(paste(client, ["adjust"], [target], source={"curve": curves}))
    assert state(client, target)["edit"]["curve"] == curves


def test_groups_that_are_not_asked_for_stay_exactly_as_they_were(client, photos):
    source, target = photos["R0000101"], photos["R0000102"]
    dress(client, source)
    put_edit(
        client,
        target,
        recipe={**RECIPE, "slot": 5},
        adjust={"exposure_ev": -0.7},
        frame={"style": "gallery"},
        stamp={"enabled": True, "color": "white"},
    )
    put_crop(client, target, preset="1:1", rect=[0.1, 0.1, 0.5625, 0.75])
    before = state(client, target)
    (item,) = ok(paste(client, ["adjust"], [target], source_id=source["id"]))["results"]
    assert item["applied"] == ["adjust"]
    after = state(client, target)
    for key in ("recipe", "lut", "geometry", "frame", "stamp", "memory"):
        assert after["edit"][key] == before["edit"][key], key
    assert after["crop"] == before["crop"]
    assert after["edit"]["adjust"]["exposure_ev"] == 0.4 and after["edit"]["curve"] == {"master": [[0.0, 0.0], [128.0, 118.0], [255.0, 255.0]]}


def test_an_empty_group_in_the_source_clears_that_group_on_the_target(client, photos):
    target = photos["R0000102"]
    put_edit(client, target, **LOOK, frame={"style": "film"})
    res = ok(paste(client, ["adjust", "frame"], [target], source={"adjust": None, "curve": None, "frame": None}))
    assert res["results"][0]["status"] == "applied"
    now = state(client, target)["edit"]
    assert now["curve"] is None and now["frame"] is None and now["adjust"]["exposure_ev"] == 0
    assert now["recipe"]["slot"] == 2, "没勾配方，配方不动"


def test_a_group_the_source_did_not_bring_is_skipped_and_said_so(client, photos):
    target = photos["R0000102"]
    put_edit(client, target, adjust=ADJUST)
    res = ok(paste(client, ["adjust", "stamp"], [target], source={"adjust": {"exposure_ev": 1.0}, "curve": None}))
    (item,) = res["results"]
    assert item["status"] == "partial" and item["applied"] == ["adjust"]
    assert [(s["group"], s["reason"]) for s in item["skipped"]] == [("stamp", "not_copied")]
    assert all(s["message"] for s in item["skipped"])


def test_geometry_group_never_carries_rotation_or_flip(client, photos):
    source, target = photos["R0000101"], photos["R0000102"]
    put_edit(client, source, geometry={"straighten": 3, "vertical": 10, "horizontal": -4, "distortion": 2, "rotate90": 2, "flip": True})
    put_edit(client, target, geometry={"rotate90": 1, "flip": False})
    (item,) = ok(paste(client, ["geometry"], [target], source_id=source["id"]))["results"]
    assert item["status"] == "applied"
    geometry = state(client, target)["edit"]["geometry"]
    assert geometry["rotate90"] == 1 and geometry["flip"] is False, "目标自己的方向不变"
    assert (geometry["straighten"], geometry["vertical"], geometry["horizontal"], geometry["distortion"]) == (3, 10, -4, 2)


def test_a_recipe_from_another_lens_unit_is_not_pasted_but_the_rest_is(client, photos):
    target = photos["R0000102"]
    ctx = client.app.state.ctx
    with ctx.lib.lock:
        ctx.lib.db.execute("UPDATE shots SET lens=? WHERE shot_id=?", ("GR LENS A12 28mm F2.5", target["id"]))
    res = ok(paste(client, ["recipe", "adjust"], [target], source={"recipe": RECIPE, "lut": None, "adjust": ADJUST, "curve": None}))
    (item,) = res["results"]
    assert item["status"] == "partial" and item["applied"] == ["adjust"]
    (skip,) = item["skipped"]
    assert (skip["group"], skip["part"], skip["reason"]) == ("recipe", "recipe", "unit_mismatch")
    assert "镜头" in skip["message"]
    now = state(client, target)["edit"]
    assert now["recipe"] is None and now["adjust"]["contrast"] == 8


def test_a_recipe_the_menu_rejects_is_skipped_not_failed(client, photos):
    target = photos["R0000102"]
    bad = {"unit": "jlaunch", "slot": 1, "levels": {"saturation": 7}}
    (item,) = ok(paste(client, ["recipe", "adjust"], [target], source={"recipe": bad, "lut": None, "adjust": ADJUST, "curve": None}))["results"]
    assert item["applied"] == ["adjust"] and item["skipped"][0]["reason"] == "recipe_rejected"
    assert state(client, target)["edit"]["recipe"] is None


def test_a_jpg_only_photo_skips_the_recipe_and_a_raw_only_lut(client, photos):
    jpg_only, raw = photos["R0000106"], photos["R0000101"]
    log_lut = upload(client, cube_bytes(lambda x: x * 0.9)).json()["cube_id"]
    assert client.patch(f"/api/luts/{log_lut}", json={"input_space": "llog"}).status_code == 200
    source = {"recipe": RECIPE, "lut": log_lut, "adjust": ADJUST, "curve": None}
    res = ok(paste(client, ["recipe", "adjust"], [jpg_only, raw], source=source))
    by_id = {r["id"]: r for r in res["results"]}
    one = by_id[jpg_only["id"]]
    assert one["status"] == "partial" and one["applied"] == ["adjust"]
    assert sorted((s["part"], s["reason"]) for s in one["skipped"]) == [("lut", "needs_raw"), ("recipe", "needs_raw")]
    assert "只有 JPG" in "".join(s["message"] for s in one["skipped"])
    assert state(client, jpg_only)["edit"]["recipe"] is None and state(client, jpg_only)["edit"]["lut"] is None
    full = by_id[raw["id"]]
    assert full["status"] == "applied"
    assert state(client, raw)["edit"]["lut"] == log_lut and state(client, raw)["edit"]["recipe"]["slot"] == 2
    assert res["counts"] == {"applied": 1, "partial": 1, "skipped": 0, "error": 0}


def test_a_display_space_lut_goes_onto_a_jpg_only_photo(client, photos):
    jpg_only = photos["R0000106"]
    lut = upload(client, cube_bytes(lambda x: x * 0.9)).json()["cube_id"]
    assert client.patch(f"/api/luts/{lut}", json={"input_space": "srgb"}).status_code == 200
    (item,) = ok(paste(client, ["recipe"], [jpg_only], source={"recipe": None, "lut": lut}))["results"]
    assert item["status"] == "applied"
    assert state(client, jpg_only)["edit"]["lut"] == lut


def test_a_lut_that_is_no_longer_in_the_library_is_skipped(client, photos):
    (item,) = ok(
        paste(client, ["recipe", "adjust"], [photos["R0000102"]], source={"recipe": None, "lut": "ab" * 32, "adjust": ADJUST, "curve": None})
    )["results"]
    assert item["applied"] == ["adjust"], "配方 + LUT 是一个整体：LUT 用不了，这一组整个不套用"
    assert [(s["part"], s["reason"]) for s in item["skipped"]] == [("lut", "lut_missing")]


def test_nothing_applied_is_reported_as_skipped_and_writes_nothing(client, photos):
    jpg_only = photos["R0000106"]
    (item,) = ok(paste(client, ["recipe"], [jpg_only], source={"recipe": RECIPE}))["results"]
    assert item["status"] == "skipped" and item["applied"] == [] and "rev" not in item
    assert rev_of(client, jpg_only) == 0


def test_crop_group_pastes_the_ratio_and_box_as_a_target_with_fit(client, photos):
    portrait = photos["R0000105"]  # 480 × 640：横图上合格的 3:2 框，换到竖图上比例就不对了
    landscape_box = {"preset": "3:2", "rect": [0.1, 0.1, 0.6, 0.5333333]}
    strict = client.put(f"/api/photos/{portrait['id']}/crop", json=landscape_box)
    assert strict.status_code == 422 and strict.json()["error"] == "ratio_mismatch"
    res = ok(paste(client, ["crop"], [portrait], source={"crop": landscape_box}))
    (item,) = res["results"]
    assert item["status"] == "applied" and item["applied"] == ["crop"]
    crop = client.get(f"/api/photos/{portrait['id']}/crop").json()
    px = crop["pixels"]
    assert crop["preset"] == "3:2" and abs(px["width"] / px["height"] - 2 / 3) < 0.01, "竖图上 3:2 是竖的"


def test_a_source_without_a_crop_resets_the_target_box_to_the_whole_picture(client, photos):
    target = photos["R0000102"]
    put_crop(client, target, **BOX)
    (item,) = ok(paste(client, ["crop"], [target], source={"crop": None}))["results"]
    assert item["status"] == "applied"
    crop = client.get(f"/api/photos/{target['id']}/crop").json()
    assert crop["preset"] == "original" and crop["rect"] == [0.0, 0.0, 1.0, 1.0]


def test_a_target_with_the_wrong_revision_is_left_alone(client, photos):
    source, stale, fresh = photos["R0000101"], photos["R0000102"], photos["R0000103"]
    dress(client, source, finish=False, crop=False)
    put_edit(client, stale, adjust={"exposure_ev": 0.9})
    before = state(client, stale)
    res = ok(
        paste(client, ["adjust"], [{"id": stale["id"], "expected_rev": 0}, {"id": fresh["id"], "expected_rev": 0}], source_id=source["id"])
    )
    bad, good = res["results"]
    assert bad["status"] == "error" and bad["code"] == "rev_conflict" and "rev" not in bad
    assert good["status"] == "applied"
    assert state(client, stale) == before
    assert res["counts"]["error"] == 1


def test_an_unknown_target_is_an_error_for_that_photo_only(client, photos):
    res = ok(paste(client, ["adjust"], [{"id": "nope"}, photos["R0000102"]], source={"adjust": ADJUST, "curve": None}))
    assert res["results"][0]["code"] == "photo_not_found" and res["results"][1]["status"] == "applied"


def test_include_state_returns_what_the_editor_needs_to_show_the_result(client, photos):
    target = photos["R0000102"]
    res = ok(paste(client, ["adjust", "crop"], [target], source={"adjust": ADJUST, "curve": None, "crop": BOX}, include_state=True))
    (item,) = res["results"]
    assert item["edit"]["rev"] == item["rev"] == item["crop"]["rev"] == rev_of(client, target)
    assert item["edit"]["adjust"]["contrast"] == 8 and "render" in item["edit"]
    assert item["crop"]["preset"] == "free"
    plain = ok(paste(client, ["adjust"], [target], source={"adjust": ADJUST, "curve": None}))["results"][0]
    assert "edit" not in plain and "crop" not in plain


def test_a_recipe_put_with_empty_memory_clears_the_memory_a_paste_brought(client, photos):
    """编辑器里撤销配方粘贴走普通的 PUT：配方里带空的 memory 才清掉粘贴带来的旋钮档位；不带 memory 键是保留原来的。"""
    source, target = photos["R0000101"], photos["R0000102"]
    memory = {"jlaunch/2": {"amount": 60}}
    put_edit(client, source, recipe={**RECIPE, "memory": memory})
    put_edit(client, target, recipe={**RECIPE, "slot": 5})
    assert state(client, target)["edit"]["memory"] == {}
    ok(paste(client, ["recipe"], [target], source_id=source["id"]))
    assert state(client, target)["edit"]["memory"] == memory
    put_edit(client, target, recipe={**RECIPE, "slot": 5})
    assert state(client, target)["edit"]["memory"] == memory, "不带 memory 键：保留"
    put_edit(client, target, recipe={**RECIPE, "slot": 5, "memory": {}})
    assert state(client, target)["edit"]["memory"] == {}, "带空的 memory：清掉"


def snapshot_all(client, photos_):
    return {p["id"]: state(client, p) for p in photos_}


def test_undo_restores_every_target_and_refuses_a_photo_changed_after_the_paste(client, photos):
    source, a, b, c = photos["R0000101"], photos["R0000102"], photos["R0000103"], photos["R0000104"]
    dress(client, source)
    put_edit(client, a, adjust={"exposure_ev": -0.3}, frame={"style": "gallery"})
    put_crop(client, b, preset="1:1", rect=[0.1, 0.1, 0.5, 0.666667])
    before = snapshot_all(client, [a, b, c])
    groups = ["recipe", "adjust", "geometry", "crop", "frame", "stamp"]
    res = ok(paste(client, groups, [a, b, c], source_id=source["id"]))
    batch = res["batch_id"]
    assert [r["status"] for r in res["results"]] == ["applied"] * 3
    assert state(client, a) != before[a["id"]]
    put_edit(client, b, adjust={"exposure_ev": 1.1})  # 粘贴之后用户又改了 b
    after_b = state(client, b)
    undo = ok(client.post("/api/edits/paste/undo", json={"batch_id": batch}))
    statuses = {r["id"]: r["status"] for r in undo["results"]}
    assert statuses == {a["id"]: "restored", b["id"]: "changed", c["id"]: "restored"}
    assert undo["counts"] == {"restored": 2, "changed": 1, "error": 0}
    assert state(client, a) == before[a["id"]] and state(client, c) == before[c["id"]]
    assert state(client, b) == after_b, "用户在粘贴之后改的东西不被覆盖"
    assert "没有撤销" in next(r["message"] for r in undo["results"] if r["status"] == "changed")
    again = client.post("/api/edits/paste/undo", json={"batch_id": batch})
    assert again.status_code == 404 and again.json()["error"] == "paste_undo_expired"


def test_undo_writes_a_new_revision_and_the_thumbnail_cache_follows(client, photos):
    target = photos["R0000102"]
    res = ok(paste(client, ["adjust"], [target], source={"adjust": ADJUST, "curve": None}))
    pasted_rev = res["results"][0]["rev"]
    undo = ok(client.post("/api/edits/paste/undo", json={"batch_id": res["batch_id"]}))
    restored_rev = undo["results"][0]["rev"]
    assert restored_rev > pasted_rev == 1
    assert find(client, "R0000102")["sidecar_rev"] == restored_rev
    assert state(client, target)["edit"]["adjust"]["contrast"] == 0


def test_requests_with_the_same_batch_id_undo_together(client, photos):
    a, b = photos["R0000102"], photos["R0000103"]
    source = {"adjust": ADJUST, "curve": None}
    ok(paste(client, ["adjust"], [a], source=source, batch_id="day-1"))
    ok(paste(client, ["adjust"], [b], source=source, batch_id="day-1"))
    undo = ok(client.post("/api/edits/paste/undo", json={"batch_id": "day-1"}))
    assert undo["counts"]["restored"] == 2
    assert state(client, a)["edit"]["adjust"]["contrast"] == 0 == state(client, b)["edit"]["adjust"]["contrast"]


def test_pasting_twice_in_one_batch_undoes_to_the_first_state(client, photos):
    target = photos["R0000102"]
    ok(paste(client, ["adjust"], [target], source={"adjust": {"exposure_ev": 0.5}, "curve": None}, batch_id="b"))
    ok(paste(client, ["adjust"], [target], source={"adjust": {"exposure_ev": 1.5}, "curve": None}, batch_id="b"))
    assert ok(client.post("/api/edits/paste/undo", json={"batch_id": "b"}))["counts"]["restored"] == 1
    assert state(client, target)["edit"]["adjust"]["exposure_ev"] == 0


def test_old_undo_batches_are_dropped_after_the_limit(client, photos):
    from gxr_studio.render.paste import MAX_UNDO_BATCHES

    target = photos["R0000102"]
    for n in range(MAX_UNDO_BATCHES + 1):
        ok(paste(client, ["adjust"], [target], source={"adjust": {"exposure_ev": 0.1}, "curve": None}, batch_id=f"b{n}"))
    assert client.post("/api/edits/paste/undo", json={"batch_id": "b0"}).status_code == 404
    assert client.post("/api/edits/paste/undo", json={"batch_id": f"b{MAX_UNDO_BATCHES}"}).status_code == 200


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"groups": [], "targets": [{"id": "x"}], "source": {}}, None),
        ({"groups": ["adjust"], "targets": [], "source": {}}, None),
        ({"groups": ["adjust", "adjust"], "targets": [{"id": "x"}], "source": {}}, "bad_paste"),
        ({"groups": ["colour"], "targets": [{"id": "x"}], "source": {}}, "bad_paste"),
        ({"groups": ["adjust"], "targets": [{"id": "x"}, {"id": "x"}], "source": {}}, "bad_paste"),
        ({"groups": ["adjust"], "targets": [{"id": "x"}]}, "bad_paste"),
        ({"groups": ["adjust"], "targets": [{"id": "x"}], "source": {}, "source_id": "y"}, "bad_paste"),
        ({"groups": ["adjust"], "targets": [{"id": "x"}], "source": {"adjust": {"exposure_ev": 99}}}, "bad_adjust"),
        ({"groups": ["adjust"], "targets": [{"id": "x"}], "source": {"curve": {"master": [[5, 5]]}}}, "bad_curve"),
        ({"groups": ["recipe"], "targets": [{"id": "x"}], "source": {"recipe": "jlaunch"}}, "bad_recipe"),
        ({"groups": ["recipe"], "targets": [{"id": "x"}], "source": {"lut": "not-a-hash"}}, "bad_lut"),
        ({"groups": ["geometry"], "targets": [{"id": "x"}], "source": {"geometry": {"rotate90": 7}}}, "bad_geometry"),
        ({"groups": ["frame"], "targets": [{"id": "x"}], "source": {"frame": {"style": "neon"}}}, "bad_frame"),
        ({"groups": ["stamp"], "targets": [{"id": "x"}], "source": {"stamp": {"color": "pink"}}}, "bad_stamp"),
        ({"groups": ["crop"], "targets": [{"id": "x"}], "source": {"crop": {"preset": "free"}}}, "bad_crop"),
        ({"groups": ["crop"], "targets": [{"id": "x"}], "source": {"crop": {"preset": "nope", "rect": [0, 0, 1, 1]}}}, "bad_crop"),
        ({"groups": ["crop"], "targets": [{"id": "x"}], "source": {"crop": {"preset": "free", "rect": [0, 0, 0, 1]}}}, "bad_crop"),
        ({"groups": ["crop"], "targets": [{"id": "x"}], "source": {"crop": {"preset": "free", "rect": [0, 0, True, 1]}}}, "bad_crop"),
    ],
)
def test_malformed_requests_are_refused_before_anything_is_written(client, photos, body, error):
    body = {**body, "targets": [{"id": photos["R0000102"]["id"]} if t["id"] == "x" else t for t in body["targets"]]}
    before = state(client, photos["R0000102"])
    res = client.post("/api/edits/paste", json=body)
    assert res.status_code == 422, res.text
    if error:
        assert res.json()["error"] == error
    assert state(client, photos["R0000102"]) == before


def test_an_unknown_source_photo_is_not_found_and_a_bad_undo_body_is_refused(client, photos):
    res = paste(client, ["adjust"], [photos["R0000102"]], source_id="nope")
    assert res.status_code == 404 and res.json()["error"] == "photo_not_found"
    assert client.post("/api/edits/paste/undo", json={}).status_code == 422
    assert client.post("/api/edits/paste/undo", json={"batch_id": "never-existed"}).status_code == 404


def test_the_endpoints_need_the_token(client, photos):
    anonymous = TestClient(client.app, base_url=f"http://127.0.0.1:{PORT}")
    assert anonymous.post("/api/edits/paste", json={}).status_code in (401, 403)
    assert anonymous.post("/api/edits/paste/undo", json={}).status_code in (401, 403)
