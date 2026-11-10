"""g181 Amount 按匹配目录的合法档位反查，不从编码猜百分比。"""

from __future__ import annotations

import json

import numpy as np
import pytest
from PIL import Image

from gxr_studio.camdata import store
from gxr_studio.camdata.recipe_tags import KNOB_ORDER, decoded_levels, parse_recipe_tag, recipe_payload
from gxr_studio.render.camera_recipe import recipe_tag_payload, recorded_recipe
from gxr_studio.render.params import EditError
from gxr_studio.render.recipes import CameraData, Menu
from test_recipe_tag_render import SyntheticCamera
from test_recipe_tags import sample


def quarter_amounts(camera, unit="qlaunch"):
    """只改合成目录的一个单元、06 槽为 25% 间隔，保留其它单元作反例。"""
    unit_index = ("jlaunch", "qlaunch", "l08firm").index(unit)
    for item in camera._items(unit_index, 6):
        if camera.menu["controls"][item["control"]]["name"] != "amount":
            continue
        old = item["points"]
        levels = [0, 25, 50, 75, 100, 125, 150]
        item["points"] = [
            {"level": level, "dose": int(round(np.interp(level, [p["level"] for p in old], [p["dose"] for p in old])))} for level in levels
        ]
        item["point_count"] = len(levels)
        item["amount_step"] = 25
    for (_kind, _identity, index), extra in camera._extras.items():
        if index == unit_index and extra["info"]["slot"] == 6:
            extra["info"]["controls"] = camera.controls(unit_index, 6)


@pytest.mark.parametrize(("code", "expected"), (("2", 25), ("7", 75), ("C", 125)))
def test_amount_character_uses_the_exact_units_legal_catalog_levels(code, expected):
    camera = SyntheticCamera()
    quarter_amounts(camera)
    tag = parse_recipe_tag(f"GXR1 6a3a5a3c02 28 06 {code}99999999-")
    assert tag["levels"]["amount"] is None
    assert recorded_recipe(tag, camera).levels_dict()["amount"] == expected
    payload = recipe_tag_payload(tag, camera)
    assert payload["levels"]["amount"] == expected and payload["changes"]["amount"] == expected
    assert payload["amount_unknown"] is False
    assert recorded_recipe(parse_recipe_tag(f"GXR1 6a3a5a3c02 50 06 {code}99999999-"), camera).levels_dict()["amount"] == (
        20 if code == "2" else 70 if code == "7" else 120
    )


@pytest.mark.parametrize("code", ("9", "G", "H", "I"))
def test_an_unmatched_amount_stays_unknown_and_cannot_select_a_prediction(code):
    camera = SyntheticCamera()
    quarter_amounts(camera)
    tag = parse_recipe_tag(f"GXR1 6a3a5a3c02 28 06 {code}99999999-")
    assert tag["knob_code"] == f"{code}99999999-"
    payload = recipe_tag_payload(tag, camera)
    assert payload["levels"]["amount"] is None and payload["amount_unknown"] is True
    assert "amount" not in payload["changes"]
    with pytest.raises(EditError, match="强度未知") as error:
        recorded_recipe(tag, camera)
    assert error.value.code == "camera_amount_unknown"


def test_missing_or_wrong_catalog_does_not_decode_amount_from_a_fallback_menu():
    tag = parse_recipe_tag("GXR1 6a3a5a3c02 28 06 799999999-")
    for menu in (None, Menu()):
        payload = recipe_tag_payload(tag, menu)
        assert payload["amount_unknown"] is True and payload["levels"]["amount"] is None
    camera = SyntheticCamera()
    camera.manifest["generation"] = 176
    assert recipe_tag_payload(tag, camera)["amount_unknown"] is True
    assert decoded_levels(tag, (70, 75))["amount"] is None, "有碰撞时不能任挑一个"
    assert recipe_payload(parse_recipe_tag("GXR1 6a3a5a3c02 28 04 -99999999-"))["amount_unknown"] is False


@pytest.mark.parametrize("old_cache", (None, {"version": 2, "levels": {"amount": 180}}))
def test_cached_copyright_is_reparsed_for_new_amount_semantics_without_reading_the_photo(lib, monkeypatch, old_cache):
    sample(lib.root / "2026/2026-11-10/R0000001.JPG", "GXR1 6a3a5a3c02 28 06 I99999999-")
    lib.rebuild()
    lib.db.execute("UPDATE shots SET recipe_tag=?", (json.dumps(old_cache) if old_cache else None,))
    monkeypatch.setattr(Image, "open", lambda *_a, **_kw: pytest.fail("Amount 缓存升级不能重读照片"))
    tag = lib.list_shots()[0]["recipe_tag"]
    assert tag["version"] == 3 and tag["knob_code"] == "I99999999-" and tag["levels"]["amount"] is None


@pytest.mark.parametrize("generation", (178, 181, 182))
def test_current_sealed_catalog_has_no_per_recipe_amount_character_collisions(tmp_path, generation):
    from g178_support import need_bundle

    raw, library = need_bundle(generation)
    store.import_from_dat(raw, tmp_path, library=library)
    camera = CameraData(tmp_path)
    checked = 0
    for unit in camera.listing():
        for recipe in unit["recipes"]:
            for control in recipe["controls"]:
                if control["name"] == "amount":
                    encoded = [level // 10 for level in control["levels"]]
                    assert len(encoded) == len(set(encoded)), (unit["id"], recipe["position"])
                    checked += 1
    assert checked > 0


@pytest.mark.parametrize(
    ("text", "kind", "amount"),
    (
        ("GXR1 6a3a5a3c02 28 04 -99999999-", "builtin", None),
        ("GXR1 6a3a5a3c02 28 06 A99999999-", "record", 100),
    ),
)
@pytest.mark.parametrize("generation", (178, 181, 182))
def test_g181_sample_tags_parse_and_resolve_against_the_sealed_catalog(tmp_path, generation, text, kind, amount):
    from g178_support import need_bundle

    raw, library = need_bundle(generation)
    store.import_from_dat(raw, tmp_path, library=library)
    camera = CameraData(tmp_path)
    tag = parse_recipe_tag(text)
    selected = recorded_recipe(tag, camera)
    assert selected.kind == kind and selected.unit == "qlaunch"
    assert selected.levels_dict().get("amount") == amount
    assert len(camera.resolve(selected).entry) == 552
    payload = recipe_tag_payload(tag, camera)
    assert payload["name"] == ("Std" if kind == "builtin" else "F-CN")
    assert payload["amount_unknown"] is False


@pytest.mark.parametrize("generation", (178, 181, 182))
def test_quarter_amount_levels_in_the_real_catalog_resolve_to_the_recorded_percent(tmp_path, generation):
    from g178_support import need_bundle

    raw, library = need_bundle(generation)
    store.import_from_dat(raw, tmp_path, library=library)
    camera = CameraData(tmp_path)
    checked = 0
    for unit, code in zip(camera.listing(), ("50", "28", "MT")):
        for position, recipe in enumerate(unit["recipes"], 1):
            controls = {control["name"]: control for control in recipe["controls"]}
            for amount, character in ((25, "2"), (75, "7"), (125, "C")):
                if amount not in controls.get("amount", {}).get("levels", []):
                    continue
                knobs = character + "".join("9" if name in controls else "-" for name in KNOB_ORDER[1:])
                tag = parse_recipe_tag(f"GXR1 6a3a5a3c02 {code} {position:02d} {knobs}")
                assert recorded_recipe(tag, camera).levels_dict()["amount"] == amount
                checked += 1
    assert checked > 0
