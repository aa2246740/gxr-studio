"""合成目录和 CFA 证明拍摄旋钮进入编辑、像素预测和 LUT 基准；另对照可选的封存 g178。"""

from __future__ import annotations

import hashlib
import io
from dataclasses import replace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import PORT
from gxr_studio.app import create_app
from gxr_studio.camdata import store
from gxr_studio.camdata.recipe_tags import parse_recipe_tag
from gxr_studio.lut.errors import LutError
from gxr_studio.lut.service import LutService
from gxr_studio.render import edits
from gxr_studio.render.camera_recipe import photo_edit, recorded_recipe
from gxr_studio.render.params import EditError, EditState, RecipeSel
from gxr_studio.render.recipes import CameraData, Menu
from gxr_studio.render.service import RenderService
from test_clone_coverage import _install_camera, _render_entry, _tiny_dng
from test_lut_library import cube_bytes

TAG = "GXR1 6a3a5a3c02 50 06 A8999B999-"
LEGACY = "GXR1 6a3a5a3c02 50 06"


class SyntheticCamera(CameraData):
    """只用公开的合成条目和菜单，不伪造已验证的 DAT。"""

    def __init__(self):
        Menu.__init__(self)
        self.manifest = {"generation": 178}
        self.manifest_sha256 = "synthetic-camera"
        entry = _render_entry()
        self.entries = {(u, s): entry for u in range(3) for s in range(1, 9)}
        self.stock = [entry] * 3
        self._extras = {}
        self._groups = {}
        for unit in range(3):
            for i, name in enumerate(("F-CC", "F-CN")):
                self._add_extra(unit, "record", i, name, 6, entry, {})


@pytest.fixture
def camera():
    return SyntheticCamera()


@pytest.fixture
def shot(lib):
    path = lib.root / "2026/2026-10-16/R0000001.DNG"
    path.parent.mkdir(parents=True)
    path.write_bytes(_tiny_dng(root_extra=[(33432, "ASCII", TAG)]))
    lib.rebuild()
    return lib.list_shots()[0]


@pytest.fixture
def renderer(settings, lib, camera, monkeypatch):
    _install_camera(settings.home_dir, _render_entry())
    service = RenderService(settings.home_dir, lib)
    service._camera = camera
    monkeypatch.setattr(service, "recipe_menu", lambda: camera)
    monkeypatch.setattr(service, "_require", lambda unit=None: camera)
    return service


def test_editor_selects_the_record_and_actual_knobs_without_writing(lib, shot, camera):
    before = lib.sidecar_path_for(shot).read_bytes()
    edit, side = edits.read(lib, shot, camera)
    assert edit.recipe.kind == "record" and edit.recipe.slot == 6 and edit.recipe.unit == "jlaunch"
    assert edit.recipe.id == camera.listing()[0]["recipes"][5]["id"]
    assert edit.recipe.levels_dict() == {
        "amount": 100,
        "contrast": -1,
        "highlight": 0,
        "shadow": 0,
        "fade": 0,
        "saturation": 2,
        "warmth": 0,
        "tint": 0,
        "split": 0,
    }
    assert "recipe" not in side and lib.sidecar_path_for(shot).read_bytes() == before
    assert edits.describe(side, edit)["recipe_from_camera"] is True


def test_legacy_unknown_knobs_use_defaults_but_do_not_claim_zero(camera):
    tag = parse_recipe_tag(LEGACY)
    assert tag["levels"] is None
    selected = recorded_recipe(tag, camera)
    assert selected.levels_dict() == {}
    assert camera.resolve(selected).levels["amount"] == 100


def test_user_choice_and_explicit_off_survive_later_edits_and_reopen(lib, shot, camera):
    edited, _ = edits.write(lib, shot, {"adjust": {"contrast": 2}}, camera)
    assert edited.recipe.levels_dict()["saturation"] == 2
    chosen = {"unit": "jlaunch", "slot": 4, "levels": {"contrast": 1}}
    edited, _ = edits.write(lib, shot, {"recipe": chosen}, camera)
    assert edited.recipe == RecipeSel("jlaunch", 4, (("contrast", 1),))
    edited, side = edits.write(lib, shot, {"recipe": None}, camera)
    assert edited.recipe is None and "recipe" in side and side["recipe"] is None
    assert edits.describe(side, edited)["recipe_from_camera"] is False
    lib.close()
    lib.open()
    edited, _ = edits.read(lib, lib.get_shot(shot["shot_id"]), camera)
    assert edited.recipe is None
    edited, _ = edits.write(lib, shot, {"adjust": {"contrast": 3}}, camera)
    assert edited.recipe is None


def test_missing_or_different_catalog_is_not_replaced_by_same_position(camera):
    tag = parse_recipe_tag(TAG)
    with pytest.raises(EditError, match="拍摄时"):
        recorded_recipe(tag, Menu())
    camera.manifest["generation"] = 176
    with pytest.raises(EditError, match="拍摄时"):
        recorded_recipe(tag, camera)
    assert recorded_recipe(parse_recipe_tag(TAG.replace("6a3a5a3c02", "0123456789")), camera) is None
    assert photo_edit({"recipe": None}, {"recipe_tag": tag}, Menu()).recipe is None


def test_a_hyphen_for_an_adjustable_knob_does_not_turn_into_default(camera):
    tag = parse_recipe_tag(TAG.replace("A8999B999-", "-8999B999-"))
    with pytest.raises(EditError, match="不匹配"):
        recorded_recipe(tag, camera)


def test_prediction_pixels_and_plan_use_actual_knobs(renderer, lib, shot, camera):
    edit, _ = edits.read(lib, shot, camera)
    expected = RecipeSel("jlaunch", 6, (("contrast", -1), ("saturation", 2)), "record", edit.recipe.id)
    actual = renderer.render_preview(shot, edit, max_edge=64)
    literal = renderer.render_preview(shot, EditState(recipe=expected), max_edge=64)
    defaults = renderer.render_preview(shot, EditState(recipe=replace(expected, levels=())), max_edge=64)
    assert actual.plan.levels["contrast"] == -1 and actual.plan.levels["saturation"] == 2
    assert actual.jpeg == literal.jpeg and actual.jpeg != defaults.jpeg
    assert actual.plan.entry_sha256 != defaults.plan.entry_sha256


def test_editor_api_returns_the_camera_selection(settings, shot, renderer, camera, monkeypatch):
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        service = client.app.state.ctx.render
        monkeypatch.setattr(service, "recipe_menu", lambda: camera)
        monkeypatch.setattr(service, "_require", lambda unit=None: camera)
        response = client.get(f"/api/photos/{shot['shot_id']}/edit")
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["recipe"]["kind"] == "record" and data["recipe"]["levels"]["saturation"] == 2
        assert data["render"]["effective_levels"]["contrast"] == -1 and data["render"]["defaulted"] is False


def test_explicit_original_and_neutral_do_not_require_the_recorded_catalog(settings, shot, renderer):
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        sid = shot["shot_id"]
        unavailable = client.get(f"/api/photos/{sid}/edit")
        assert unavailable.status_code == 200 and unavailable.json()["render"]["reason"] == "camera_recipe_catalog_missing"
        assert unavailable.json()["render"]["available"] is False
        original = client.post(f"/api/photos/{sid}/render?stage=original&max_edge=64", json={})
        assert original.status_code == 200, original.text[:200]
        explicit_off = client.post(f"/api/photos/{sid}/render?stage=final&max_edge=64", json={"recipe": None})
        assert explicit_off.status_code == 200, explicit_off.text[:200]
        neutral = client.post(f"/api/photos/{sid}/export", json={"source": "neutral"})
        assert neutral.status_code == 200, neutral.text


@pytest.mark.parametrize("text", (TAG, LEGACY))
@pytest.mark.parametrize("matching_catalog", (True, False))
def test_jpeg_only_keeps_camera_pixels_and_copyright(settings, lib, camera, monkeypatch, text, matching_catalog):
    path = lib.root / "2026/2026-11-10/R0000002.JPG"
    path.parent.mkdir(parents=True)
    exif = Image.Exif()
    exif[33432] = text
    Image.new("RGB", (24, 16), (90, 110, 120)).save(path, "JPEG", exif=exif)
    lib.rebuild()
    sid = lib.list_shots()[0]["shot_id"]
    menu = camera if matching_catalog else Menu()
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        monkeypatch.setattr(client.app.state.ctx.render, "recipe_menu", lambda: menu)
        preview = client.post(f"/api/photos/{sid}/render?stage=final&max_edge=64", json={})
        assert preview.status_code == 200, preview.text[:200]
        with Image.open(io.BytesIO(preview.content)) as image:
            np.testing.assert_allclose(np.asarray(image.convert("RGB"))[8, 12], [90, 110, 120], atol=2)
        response = client.post(f"/api/photos/{sid}/export", json={"source": "auto"})
        assert response.status_code == 200, response.text
        exported = response.json()
        assert exported["render_label"] == "original"
        with Image.open(exported["path"]) as image:
            assert image.getexif()[33432] == text
            np.testing.assert_allclose(np.asarray(image.convert("RGB"))[8, 12], [90, 110, 120], atol=2)
        assert client.get(f"/api/photos/{sid}/edit").json()["recipe"] is None


def test_lut_preview_uses_the_actual_recipe_and_user_override(renderer, settings, lib, shot, camera):
    service = LutService(settings.home_dir, lib, renderer)
    item = service.add_cube(cube_bytes(lambda rgb: rgb * [0.6, 1, 1], size=2), "synthetic.cube")
    actual, info = service.preview(item["cube_id"], "lut", {"space": "srgb"}, shot, 64)
    assert info["mode"] == "recipe-prediction"
    edit, _ = edits.read(lib, shot, camera)
    codes = renderer.proxy_codes(shot, 64)
    before_lut = renderer.renderer(shot, EditState(recipe=edit.recipe)).display(codes)
    expected = np.floor(np.clip(before_lut * [0.6, 1, 1], 0, 255) + 0.5).astype(np.uint8)
    encoded = io.BytesIO()
    from gxr_studio.export import srgb_icc

    Image.fromarray(expected).save(encoded, "JPEG", quality=88, icc_profile=srgb_icc())
    np.testing.assert_array_equal(np.asarray(Image.open(io.BytesIO(actual))), np.asarray(Image.open(io.BytesIO(encoded.getvalue()))))
    edits.write(lib, shot, {"recipe": {"unit": "jlaunch", "slot": 4}}, camera)
    overridden, _ = service.preview(item["cube_id"], "lut", {"space": "srgb"}, shot, 64)
    assert overridden != actual


def test_lut_does_not_silently_fall_back_when_the_recorded_catalog_is_missing(renderer, settings, lib, shot, monkeypatch):
    service = LutService(settings.home_dir, lib, renderer)
    item = service.add_cube(cube_bytes(size=2), "synthetic.cube")
    monkeypatch.setattr(renderer, "recipe_menu", Menu)
    with pytest.raises(LutError, match="拍摄时"):
        service.preview(item["cube_id"], "lut", {"space": "srgb"}, shot, 64)


def test_actual_g178_record_receives_the_decoded_knobs(tmp_path):
    from g178_support import need_bundle

    raw, library = need_bundle(178)
    store.import_from_dat(raw, tmp_path, library=library)
    camera = CameraData(tmp_path)
    tag = parse_recipe_tag(TAG)
    actual = recorded_recipe(tag, camera)
    literal = RecipeSel("jlaunch", 6, (("contrast", -1), ("saturation", 2)), "record", camera.listing()[0]["recipes"][5]["id"])
    assert camera.resolve(actual).entry == camera.resolve(literal).entry
    assert camera.resolve(actual).entry != camera.resolve(replace(literal, levels=())).entry


def test_catalog_amount_75_drives_prediction_pixels_and_lut_preview(renderer, settings, lib, shot, camera):
    from gxr_studio.export import srgb_icc
    from test_recipe_tag_amount import quarter_amounts

    original = recorded_recipe(parse_recipe_tag(TAG), camera)
    changed_entry = camera.resolve(replace(original, levels=(("contrast", 3),))).entry
    extra = camera._extras[(original.kind, original.id, original.unit_index)]
    extra["entry"] = changed_entry
    extra["info"]["entry_sha256"] = hashlib.sha256(changed_entry).hexdigest()
    quarter_amounts(camera, "jlaunch")
    shot = {**shot, "recipe_tag": parse_recipe_tag(TAG.replace("A8999B999-", "78999B999-"))}
    edit, _ = edits.read(lib, shot, camera)
    assert edit.recipe.levels_dict()["amount"] == 75
    literal = RecipeSel("jlaunch", 6, (("amount", 75), ("contrast", -1), ("saturation", 2)), "record", original.id)
    actual = renderer.render_preview(shot, edit, max_edge=64)
    expected = renderer.render_preview(shot, EditState(recipe=literal), max_edge=64)
    hundred_edit = EditState(recipe=replace(literal, levels=(("amount", 100), ("contrast", -1), ("saturation", 2))))
    hundred = renderer.render_preview(shot, hundred_edit, max_edge=64)
    assert actual.plan.levels["amount"] == 75 and actual.jpeg == expected.jpeg and actual.jpeg != hundred.jpeg
    service = LutService(settings.home_dir, lib, renderer)
    cube = service.add_cube(cube_bytes(lambda rgb: rgb * [0.6, 1, 1], size=2), "amount.cube")
    image, info = service.preview(cube["cube_id"], "lut", {"space": "srgb"}, shot, 64)
    assert info["mode"] == "recipe-prediction"
    rgb = renderer.renderer(shot, EditState(recipe=literal)).display(renderer.proxy_codes(shot, 64))
    pixels = np.floor(np.clip(rgb * [0.6, 1, 1], 0, 255) + 0.5).astype(np.uint8)
    encoded = io.BytesIO()
    Image.fromarray(pixels).save(encoded, "JPEG", quality=88, icc_profile=srgb_icc())
    np.testing.assert_array_equal(np.asarray(Image.open(io.BytesIO(image))), np.asarray(Image.open(io.BytesIO(encoded.getvalue()))))
    unknown = {**shot, "recipe_tag": parse_recipe_tag(TAG.replace("A8999B999-", "98999B999-"))}
    with pytest.raises(LutError, match="强度未知") as error:
        service.preview(cube["cube_id"], "lut", {"space": "srgb"}, unknown, 64)
    assert error.value.code == "camera_amount_unknown"
