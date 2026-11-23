"""g181 Amount 按匹配目录的合法档位反查，不从编码猜百分比。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from gxr_studio.camdata import catalog_names, datcodec, store
from gxr_studio.camdata.recipe_tags import KNOB_ORDER, decoded_levels, parse_recipe_tag, recipe_payload
from gxr_studio.render.camera_recipe import _amount_levels, _catalog_recipe, recipe_tag_payload, recorded_recipe
from gxr_studio.render.params import EditError
from gxr_studio.render.recipes import CameraData, Menu
from test_recipe_tag_render import SyntheticCamera
from test_recipe_tags import sample

REPO = Path(__file__).resolve().parents[1]
SWEEP_DRIVER = REPO / "tests" / "support" / "recipe_tag_amount_sweep.mjs"


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
    # root 规则：50mm F-CN 为 effect=4 (pos=08)；旧 06 为按菜单序的误写
    assert recorded_recipe(parse_recipe_tag(f"GXR1 6a3a5a3c02 50 08 {code}99999999-"), camera).levels_dict()["amount"] == (
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
    ("text", "kind", "amount", "expected_name"),
    (
        ("GXR1 6a3a5a3c02 28 04 -99999999-", "builtin", None, "Std"),
        # root 规则 + 现场标签 R8052696-698：28 06 为 F-CC (effect=2)；28 09 为 F-CN (effect=5)
        ("GXR1 6a3a5a3c02 28 06 A99999999-", "record", 100, "F-CC"),
        ("GXR1 6a3a5a3c02 28 09 A99999999-", "record", 100, "F-CN"),
    ),
)
@pytest.mark.parametrize("generation", (178, 181, 182))
def test_g181_sample_tags_parse_and_resolve_against_the_sealed_catalog(tmp_path, generation, text, kind, amount, expected_name):
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
    assert payload["name"] == expected_name
    assert payload["amount_unknown"] is False


@pytest.mark.parametrize("generation", (178, 181, 182))
def test_quarter_amount_levels_in_the_real_catalog_resolve_to_the_recorded_percent(tmp_path, generation):
    from g178_support import need_bundle

    raw, library = need_bundle(generation)
    store.import_from_dat(raw, tmp_path, library=library)
    camera = CameraData(tmp_path)
    checked = 0
    for unit_index, (unit, code) in enumerate(zip(camera.listing(), ("50", "28", "MT"))):
        record_idx = 0
        for recipe in unit["recipes"]:
            # root 规则：1-4 为基底槽 pos=slot；Look 描述符按 50/28/MT 三元组交错，pos = 3*k + unit_index + 5
            if recipe.get("kind", "builtin") == "builtin":
                position = recipe["slot"]
            else:
                position = 3 * record_idx + unit_index + 5
                record_idx += 1
            controls = {control["name"]: control for control in recipe["controls"]}
            for amount, character in ((25, "2"), (75, "7"), (125, "C")):
                if amount not in controls.get("amount", {}).get("levels", []):
                    continue
                knobs = character + "".join("9" if name in controls else "-" for name in KNOB_ORDER[1:])
                tag = parse_recipe_tag(f"GXR1 6a3a5a3c02 {code} {position:02d} {knobs}")
                assert recorded_recipe(tag, camera).levels_dict()["amount"] == amount
                checked += 1
    assert checked > 0


def _amount_control(levels: list[int]) -> dict[str, Any]:
    return {
        "name": "amount",
        "label": "强度",
        "min": levels[0],
        "max": levels[-1],
        "default": 100,
        "cap_lower": False,
        "cap_upper": False,
        "amount_step": None,
        "levels": levels,
    }


class _SyntheticListingCamera(CameraData):
    """按给定代数与单元配方表构造合成 CameraData，不依赖本机 DAT。"""

    def __init__(self, generation: int, units: list[dict[str, Any]]) -> None:
        Menu.__init__(self)
        self.manifest = {"generation": generation}
        self.manifest_sha256 = f"synthetic-gen-{generation}"
        self._listing_units = units

    def listing(self) -> list[dict[str, Any]]:
        return self._listing_units


def _synthetic_lib176_camera() -> _SyntheticListingCamera:
    # 基底 1..8 与 G176 记录各自给可区分的强度档位；Acros 无强度旋钮，ARRI 709 与槽位 8（Gold）的 120/125 档位不同。
    slot_levels: dict[int, list[int] | None] = {
        1: [0, 25, 50, 75, 100, 125, 150],
        2: [0, 25, 50, 75, 100, 125, 150],
        3: None,
        4: None,
        5: [0, 50, 100, 150],
        6: [0, 20, 60, 100, 140],
        7: [0, 40, 80, 100],
        8: [0, 25, 50, 75, 100, 125, 150],
    }
    units: list[dict[str, Any]] = []
    for unit_idx, unit_id in enumerate(("jlaunch", "qlaunch", "l08firm")):
        unit_bit = 1 << unit_idx
        recipes: list[dict[str, Any]] = []
        for slot in range(1, 9):
            lv = slot_levels[slot]
            recipes.append(
                {
                    "kind": "builtin",
                    "slot": slot,
                    "number": f"{slot:02d}",
                    "controls": [_amount_control(lv)] if lv is not None else [],
                }
            )
        seen: set[str] = set()
        for name, mask in catalog_names.CATALOG_DESCRIPTORS["d25d2f0a68"]:
            if not (mask & unit_bit) or name in seen:
                continue
            seen.add(name)
            no_amount = name in ("Acros", "Tri-X 400")
            lv = None if no_amount else ([0, 20, 40, 60, 80, 100, 120, 140] if name == "ARRI 709" else [0, 10, 50, 100, 150])
            recipes.append(
                {
                    "kind": "record",
                    "id": f"rec-{unit_id}-{name}",
                    "name": name,
                    "slot": 6,
                    "number": f"{len(recipes) + 1:02d}",
                    "controls": [_amount_control(lv)] if lv is not None else [],
                }
            )
        units.append({"id": unit_id, "recipes": recipes})
    return _SyntheticListingCamera(176, units)


def _synthetic_lib178_camera(generation: int = 262) -> _SyntheticListingCamera:
    # lib-178 目录只含 1..4 基底槽，以及全部 KUBIK 记录（含 K64、K25 等与旧基底槽 5/7 同名的 Look）。
    units: list[dict[str, Any]] = []
    for unit_idx, unit_id in enumerate(("jlaunch", "qlaunch", "l08firm")):
        unit_bit = 1 << unit_idx
        recipes: list[dict[str, Any]] = []
        for slot in range(1, 5):
            lv = [0, 25, 50, 75, 100, 125, 150] if slot in (1, 2) else None
            recipes.append(
                {
                    "kind": "builtin",
                    "slot": slot,
                    "number": f"{slot:02d}",
                    "controls": [_amount_control(lv)] if lv is not None else [],
                }
            )
        seen: set[str] = set()
        for name, mask in catalog_names.CATALOG_DESCRIPTORS["0ccc86c19b"]:
            if not (mask & unit_bit) or name in seen:
                continue
            seen.add(name)
            no_amount = name in ("Acros", "Tri-X 400")
            lv = None if no_amount else list(range(0, 151, 10))
            recipes.append(
                {
                    "kind": "record",
                    "id": f"rec-{unit_id}-{name}",
                    "name": name,
                    "slot": 6,
                    "number": f"{len(recipes) + 1:02d}",
                    "controls": [_amount_control(lv)] if lv is not None else [],
                }
            )
        units.append({"id": unit_id, "recipes": recipes})
    return _SyntheticListingCamera(generation, units)


def _sweep_phone_vs_desktop(tmp_path: Path, camera: CameraData, mobile_src: Path | None = None) -> list[tuple[str, str, int, Any, Any]]:
    catalogs = list(catalog_names.CATALOG_DESCRIPTORS.keys())
    units = ["50", "28", "MT"]
    max_pos = 99
    payload_path = tmp_path / f"sweep-{camera.manifest['generation']}.json"
    payload_path.write_text(
        json.dumps(
            {
                "generation": camera.manifest["generation"],
                "listing": camera.listing(),
                "catalogs": catalogs,
                "units": units,
                "maxPos": max_pos,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["node", "--experimental-strip-types", str(SWEEP_DRIVER), "sweep", str(mobile_src or (REPO / "mobile" / "src")), str(payload_path)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    phone_map: dict[str, list[int] | None] = json.loads(proc.stdout)

    mismatches: list[tuple[str, str, int, Any, Any]] = []
    for cid in catalogs:
        for unit in units:
            for pos in range(1, max_pos + 1):
                tag = parse_recipe_tag(f"GXR1 {cid} {unit} {pos:02d} A99999999-")
                try:
                    hit = _catalog_recipe(tag, camera)
                except EditError:
                    hit = None
                desktop_levels = _amount_levels(hit[1]) if hit else None
                phone_levels = phone_map[f"{cid}|{unit}|{pos}"]
                if phone_levels != desktop_levels:
                    mismatches.append((cid, unit, pos, desktop_levels, phone_levels))
    return mismatches


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")
def test_phone_tag_amount_levels_match_desktop_across_all_positions_synthetic(tmp_path: Path) -> None:
    """无 DAT 扫全量 4 目录 × 3 单元 × 99 位置（lib-176 与 lib-178），手机 tagAmountLevels 必须与桌面逐项一致。"""
    mismatches_176 = _sweep_phone_vs_desktop(tmp_path, _synthetic_lib176_camera())
    mismatches_178 = _sweep_phone_vs_desktop(tmp_path, _synthetic_lib178_camera())
    assert mismatches_176 == [], f"lib-176 mismatches ({len(mismatches_176)}): {mismatches_176[:6]}"
    assert mismatches_178 == [], f"lib-178 mismatches ({len(mismatches_178)}): {mismatches_178[:6]}"


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")
def test_phone_tag_amount_levels_match_desktop_across_all_positions_real_dats(tmp_path: Path) -> None:
    """本机存在 g176 与 g262 DAT 时，全量扫 2,376 个位置对拍真实目录。"""
    from g178_support import need_bundle
    from support import g262_cases as g262

    raw176, lib176 = need_bundle(176)
    raw262, lib262, _, _ = g262.need_g262()
    home176, home262 = tmp_path / "home-176", tmp_path / "home-262"
    store.import_from_dat(raw176, home176, library=lib176)
    store.import_from_dat(raw262, home262, library=lib262)

    mismatches_176 = _sweep_phone_vs_desktop(tmp_path, CameraData(home176))
    mismatches_262 = _sweep_phone_vs_desktop(tmp_path, CameraData(home262))
    assert mismatches_176 == [], f"real g176 mismatches ({len(mismatches_176)}): {mismatches_176[:6]}"
    assert mismatches_262 == [], f"real g262 mismatches ({len(mismatches_262)}): {mismatches_262[:6]}"


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")
def test_phone_generation_maps_match_python_profiles_and_catalogs(tmp_path: Path) -> None:
    """手机端代数与封存目录表必须与 Python PROFILES / CATALOG_GENERATIONS / CATALOG_DESCRIPTORS 逐项一致。"""
    cam178 = _synthetic_lib178_camera()
    payload_path = tmp_path / "maps-probe.json"
    payload_path.write_text(
        json.dumps(
            {
                "listing": cam178.listing(),
                "probeTag": "GXR1 0ccc86c19b 50 68 899999999-",
                "unregisteredGenerations": [261, 999],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["node", "--experimental-strip-types", str(SWEEP_DRIVER), "maps", str(REPO / "mobile" / "src"), str(payload_path)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(proc.stdout)

    assert data["hasRecipeTagsModule"] is True
    assert data["tagCatalogGenerations"] == catalog_names.CATALOG_GENERATIONS
    expected_profiles = {str(gen): prof.library_generation for gen, prof in datcodec.PROFILES.items()}
    assert data["datLibraryGenerations"] == expected_profiles
    expected_descriptors = {cid: [[name, mask] for name, mask in items] for cid, items in catalog_names.CATALOG_DESCRIPTORS.items()}
    assert data["catalogDescriptors"] == expected_descriptors
    assert data["unregistered"] == {"261": None, "999": None}
