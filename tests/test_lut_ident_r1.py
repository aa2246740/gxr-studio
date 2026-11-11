"""输入、输出声明与自动条目迁移。cube、照片均为合成，不读取第三方 LUT。"""

from __future__ import annotations

import io
import json

import numpy as np
import pytest
from PIL import Image

from gxr_studio.lut.cube import Cube
from gxr_studio.lut.identify import IDENTIFICATION_VERSION, identify
from gxr_studio.lut.library import LutLibrary
from gxr_studio.lut.scene import ANCHOR, scene_colour
from gxr_studio.privacy import PrivateDir
from gxr_studio.raw import open_dng
from test_lut_library import _get_photo, _photo_files, cube_bytes, upload
from test_lut_library import client as client
from test_lut_library import library as library


def synthetic(comments="", title="synthetic", fun=lambda x: x):
    return cube_bytes(fun, title=title, size=5, comments=comments)


@pytest.mark.parametrize("style", ["FLog2C_to_Style", "L-Log_to_Style", "S-Log3_style", "sRGB_style"])
@pytest.mark.parametrize("header", ["#LUMIXPHOTOSTYLE VLOG", "# Input: Panasonic V-Log / V-Gamut", "# INPUT_COLORSPACE V-Log"])
def test_explicit_input_beats_target_style_and_gray_shape(style, header):
    result = identify(Cube.parse(synthetic(header + "\n", title=style).decode()), style + "_VLog.cube")
    assert (result["input_space"], result["input_space_state"]) == ("vlog", "known")
    assert result["space_evidence"]["content"] == "display"


@pytest.mark.parametrize(
    "comments,title,filename,space",
    [
        ("# Input Space = sRGB\n# Output: Rec709\n", "V-Log style", "L-Log.cube", "srgb"),
        ("# input_colour_space: S-Log3\n", "FLog2C style", "vlog.cube", "slog3"),
        ("# Input: linear\n", "synthetic", "FLog2C.cube", "linear"),
        ("", "V-Log to sRGB", "FLog2C_Style.cube", "vlog"),
        ("", "sRGB to V-Log", "vlog_style.cube", "srgb"),
        ("", "synthetic", "FLog2C_to_Style_VLog.cube", "vlog"),
        ("", "synthetic", "Leica_L-Log_warm.cube", "llog"),
        ("", "Input V-Log / V-Gamut; Target FLog2C", "film.cube", "vlog"),
        ("", "synthetic", "input_srgb_target_FLog2C.cube", "srgb"),
        ("# Input: V-Log; Output: sRGB\n", "FLog2C style", "film.cube", "vlog"),
        ("", "L-Log_to_style", "input_vlog.cube", "vlog"),
        ("", "FLog2C_to_style", "input_srgb.cube", "srgb"),
        ("", "L-Log_to_style", "FLog2C_to_Style_VLog.cube", "vlog"),
    ],
)
def test_input_domain_precedes_style(comments, title, filename, space):
    result = identify(Cube.parse(synthetic(comments, title, lambda x: 1 - x).decode()), filename)
    assert result["input_space"] == space


@pytest.mark.parametrize(
    "comments,filename,output",
    [
        ("# Output: BT.709\n", "film.cube", "rec709"),
        ("# OUTPUT_COLORSPACE Rec.709 primaries / D65 / BT.709 OETF\n", "film.cube", "rec709"),
        ("# OUTPUT_COLORSPACE Rec.709 primaries / D65 / Gamma 2.4\n", "film.cube", "gamma24"),
        ("# Output transfer function: BT1886\n", "film_sRGB.cube", "gamma24"),
        ("# Output: sRGB\n", "film_Rec709.cube", "srgb"),
        ("# Output gamma: 2.4\n", "film.cube", "gamma24"),
        ("# Gamma 2.4\n", "film.cube", "gamma24"),
        ("", "Film_VLog_Rec709.cube", "rec709"),
        ("", "Film_VLog_BT.709.cube", "rec709"),
        ("", "Film_Rec709_BT1886_VLog.cube", "gamma24"),
        ("", "Film_VLog_Gamma_2.4.cube", "gamma24"),
        ("", "Film_VLog_sRGB.cube", "srgb"),
        ("# Input: sRGB\n", "sRGB_to_VLog.cube", "srgb"),
    ],
)
def test_output_transfer_is_identified_independently(comments, filename, output):
    result = identify(Cube.parse(synthetic(comments, fun=lambda x: 1 - x).decode()), filename)
    assert result["output_space"] == output


def test_inline_between_data_and_trailer_declarations_are_read():
    text = synthetic(title="Synthetic # sRGB title").decode()
    text = text.replace("0 0 0\n", "0 0 0 # Input: V-Log\n", 1) + "# Output: BT1886\n"
    cube = Cube.parse(text)
    assert cube.title == "Synthetic # sRGB title"
    result = identify(cube, "FLog2C_style.cube")
    assert (result["input_space"], result["output_space"]) == ("vlog", "gamma24")


@pytest.mark.parametrize("fun", [lambda x: x, lambda x: x**2, lambda x: 1 - x, lambda x: np.clip(x * 1.6, 0, 1)])
def test_undeclared_creator_remains_unknown_even_for_a_neutral_gray_axis(fun):
    cube = Cube.parse(synthetic("# Created by Synthetic Author\n", "Synthetic creator", fun).decode())
    result = identify(cube, "Creative_1234.cube")
    assert (result["input_space"], result["input_space_state"], result["output_space"]) == (None, "unknown", "srgb")


@pytest.mark.parametrize("value", ["V-Log\n# Input: F-Log2", "unsupported-input"])
def test_conflicting_or_unsupported_declarations_do_not_fall_back_to_filename(value):
    result = identify(Cube.parse(synthetic(f"# Input: {value}\n").decode()), "vlog.cube")
    assert result["input_space"] is None
    assert result["input_space_state"] == ("conflict" if value.startswith("V-Log") else "unknown")


def test_unsupported_input_declaration_conflicts_with_lumix_header():
    result = identify(Cube.parse(synthetic("#LUMIXPHOTOSTYLE VLOG\n# Input: unsupported\n").decode()), "vlog.cube")
    assert (result["input_space"], result["input_space_state"]) == (None, "conflict")


def test_conflicting_named_input_labels_need_manual_selection():
    result = identify(Cube.parse(synthetic(fun=lambda x: 1 - x).decode()), "input_srgb.cube")
    assert result["input_space"] == "srgb"
    cube = Cube.parse(synthetic(title="input_vlog", fun=lambda x: 1 - x).decode())
    result = identify(cube, "input_srgb.cube")
    assert (result["input_space"], result["input_space_state"]) == (None, "conflict")


def test_flog2c_is_not_an_alias_for_the_supported_flog2_gamut():
    for comments in ("", "# Input: F-Log2C\n"):
        result = identify(Cube.parse(synthetic(comments).decode()), "FLog2C.cube")
        assert (result["input_space"], result["input_space_state"]) == (None, "unknown")


def test_output_conflicts_retain_the_existing_default_for_manual_selection():
    result = identify(Cube.parse(synthetic("# Input: V-Log\n# Output: sRGB\n# Output: BT709\n").decode()), "film.cube")
    assert (result["input_space"], result["output_space"], result["output_space_state"]) == ("vlog", "srgb", "conflict")


def legacy_record(library, raw, name, *, manual=False, space="flog2", output="srgb"):
    item = library.add(raw, name)
    path = library.home / "luts/library.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    record = doc["items"][item["cube_id"]]
    record.pop("identification_version", None)
    record.pop("output_space_state", None)
    record.update(
        input_space=space,
        output_space=output,
        input_space_manual=manual,
        version=7,
        fit={"old": "fit"},
        recipe={"old": "recipe"},
        camera_lut={"old": "camera-lut"},
    )
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return item["cube_id"], record


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("change", ["input", "output", "both", "neither"])
def test_migration_preserves_manual_and_invalidates_only_changed_auto_spaces(library, manual, change):
    raw = synthetic("#LUMIXPHOTOSTYLE VLOG\n# Output: Rec.709\n")
    cid, old = legacy_record(
        library,
        raw,
        "FLog2C_Style_VLog.cube",
        manual=manual,
        space="flog2" if change in ("input", "both") else "vlog",
        output="srgb" if change in ("output", "both") else "rec709",
    )
    token = library.render_token(cid) if manual else None
    recovered = LutLibrary(library.home).get(cid)
    if manual:
        assert recovered == old
        assert library.render_token(cid) == token
    else:
        assert (recovered["input_space"], recovered["output_space"]) == ("vlog", "rec709")
        assert recovered["version"] == (7 if change == "neither" else 8)
        assert recovered["fit"] == (old["fit"] if change == "neither" else None)
        assert recovered["recipe"] == (old["recipe"] if change == "neither" else None)
        assert recovered["camera_lut"] == (old["camera_lut"] if change == "neither" else None)
        assert recovered["identification_version"] == IDENTIFICATION_VERSION
    persisted = (library.home / "luts/library.json").read_bytes()
    assert LutLibrary(library.home).get(cid) == recovered
    assert (library.home / "luts/library.json").read_bytes() == persisted
    assert (library.home / "luts" / recovered["file"]).read_bytes() == raw


def test_output_only_manual_selection_freezes_automatic_migration(library):
    item = library.add(synthetic("#LUMIXPHOTOSTYLE VLOG\n# Output: Rec709\n"), "FLog2C_VLog.cube")
    updated = library.patch(item["cube_id"], {"output_space": "gamma24"})
    assert updated["input_space_manual"]
    path = library.home / "luts/library.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["items"][item["cube_id"]].pop("identification_version")
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert LutLibrary(library.home).get(item["cube_id"])["output_space"] == "gamma24"


def test_old_gray_only_guess_becomes_unknown_and_requires_manual_selection(library):
    from gxr_studio.lut.errors import LutError

    cid, _ = legacy_record(library, synthetic(), "creator.cube", space="srgb")
    item = LutLibrary(library.home).get(cid)
    assert (item["input_space"], item["input_space_state"], item["version"]) == (None, "unknown", 8)
    with pytest.raises(LutError) as error:
        library.target(cid)
    assert error.value.code == "lut_space_required"


def test_interrupted_migration_keeps_the_old_index_and_retries_once(library, monkeypatch):
    cid, _ = legacy_record(library, synthetic("# Input: V-Log\n"), "FLog2C.cube")
    path = library.home / "luts/library.json"
    original = path.read_bytes()

    def fail_write(_self, _name, _data):
        raise OSError("synthetic disk sync failure")

    with monkeypatch.context() as patch:
        patch.setattr(PrivateDir, "replace_file", fail_write)
        with pytest.raises(OSError, match="synthetic disk sync failure"):
            LutLibrary(library.home).get(cid)
    assert path.read_bytes() == original
    assert LutLibrary(library.home).get(cid)["version"] == 8
    assert LutLibrary(library.home).get(cid)["version"] == 8


def test_frozen_result_does_not_reload_changed_lut_metadata(client, settings):
    from test_lut import _plant_fit

    library = client.app.state.ctx.render.luts
    cid, _ = legacy_record(library, synthetic("# Input: sRGB\n# Output: Rec709\n"), "film.cube", space="srgb")
    job_id = "b6" * 6
    _plant_fit(client, cid, "srgb", False, job_id)
    path = settings.home_dir / "lut_jobs" / job_id / "job.json"
    job = json.loads(path.read_text(encoding="utf-8"))
    job["lut_version"] = 7
    path.write_text(json.dumps(job), encoding="utf-8")
    status = client.get(f"/api/lut/fit/{job_id}")
    assert status.status_code == 200
    assert status.json()["state"] == "done"
    assert client.post("/api/my-recipes", json={"job_id": job_id, "name": "冻结配方"}).status_code == 200


@pytest.mark.parametrize("key", ["thumb_url", "preview_url"])
def test_migration_replaces_warm_disk_cache_and_image_url(client, volumes, key):
    _photo_files(volumes)
    photo = _get_photo(client)
    raw = synthetic("# Input: sRGB\n# Output: Rec709\n", fun=lambda x: x * [0.5, 0.8, 1])
    item = upload(client, raw).json()
    library = client.app.state.ctx.render.luts
    path = library.home / "luts/library.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["items"][item["cube_id"]]["output_space"] = "srgb"
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert client.put(f"/api/photos/{photo['id']}/edit", json={"lut": item["cube_id"]}).status_code == 200
    before = client.get(f"/api/photos/{photo['id']}").json()[key]
    original = client.get(before)
    assert original.status_code == 200
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["items"][item["cube_id"]].pop("identification_version")
    path.write_text(json.dumps(doc), encoding="utf-8")
    after = client.get(f"/api/photos/{photo['id']}").json()[key]
    replaced = client.get(after)
    assert replaced.status_code == 200
    assert before != after and original.content != replaced.content
    assert client.get(after).content == replaced.content


@pytest.mark.parametrize("lens", ["GXR MOUNT", "RICOH LENS A12 28mm", "RICOH GR LENS A12 50mm F2.5 MACRO"])
def test_log_preview_and_export_accept_supported_raw_units(client, volumes, lens):
    folder = _photo_files(volumes, raw=True, lens=lens)
    photo = _get_photo(client)
    item = upload(client, synthetic("# Input: V-Log\n# Output: sRGB\n", fun=lambda x: np.full_like(x, [0.1, 0.4, 0.7]))).json()
    result = client.put(f"/api/photos/{photo['id']}/edit", json={"lut": item["cube_id"]})
    assert result.status_code == 200, result.text
    preview = client.get(f"/api/photos/{photo['id']}/render?stage=final&size=preview")
    assert preview.status_code == 200, preview.text
    preview_image = Image.open(io.BytesIO(preview.content))
    assert preview_image.size == (60, 40)
    assert np.max(np.abs(np.asarray(preview_image)[20, 30].astype(float) - [26, 102, 179])) <= 2
    exported = client.post(f"/api/photos/{photo['id']}/export", json={"format": "png"})
    assert exported.status_code == 200, exported.text
    exported_image = Image.open(exported.json()["path"])
    assert exported_image.size == (120, 80)
    # The existing finish/export path encodes an intermediate JPEG for PNG too.
    assert np.max(np.abs(np.asarray(exported_image)[40, 60].astype(float) - [26, 102, 179])) <= 2
    dng = open_dng(folder / "SYNTHETIC.DNG")
    try:
        gray = scene_colour(dng).xyz(np.full((1, 3), ANCHOR["u_m"] * 4095))
        assert gray[0, 1] == pytest.approx(0.18, abs=1e-14)
    finally:
        dng.release()
