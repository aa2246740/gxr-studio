"""LUT 库 MVP：全部 LUT 与图像都现场合成，不读取第三方 LUT。"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import PORT
from gxr_studio import audit
from gxr_studio.app import create_app
from gxr_studio.lut.cube import Cube
from gxr_studio.lut.errors import LutError
from gxr_studio.lut.identify import identify
from gxr_studio.lut.library import LutLibrary
from gxr_studio.lut.metadata import export_cube, read_header
from gxr_studio.lut.spaces import SPACES
from gxr_studio.lut.worker import _write as write_worker_result
from gxr_studio.privacy import PrivateDir, PrivatePathError
from gxr_studio.render.params import EditError, parse_edit
from raw_support import bayer_scene, build_dng
from test_api import import_all


def cube_bytes(fun=lambda x: x, *, title="synthetic", size=17, comments=""):
    v = np.linspace(0, 1, size)
    nodes = np.array([[r, g, b] for b in v for g in v for r in v])
    rows = fun(nodes)
    return (comments + f'TITLE "{title}"\nLUT_3D_SIZE {size}\n' + "\n".join(" ".join(f"{c:.17g}" for c in row) for row in rows) + "\n").encode()


@pytest.fixture
def library(tmp_path):
    return LutLibrary(tmp_path / "app-data")


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        yield c


def upload(client, data=None, name="sample.cube"):
    raw = cube_bytes(comments="# Input: sRGB\n") if data is None else data
    return client.post("/api/lut/upload", content=raw, headers={"X-GXR-Filename": quote(name, safe="")})


def test_multiple_imports_folder_group_and_duplicate_keep_user_data(library):
    a = library.add(cube_bytes(), "My LUTs/a.cube")
    b = library.add(cube_bytes(lambda x: x * 0.9), "My LUTs/sub/b.CUBE")
    assert a["group"] == b["group"]
    library.patch(a["cube_id"], {"name": "我的名称", "starred": True, "author": "作者"})
    repeated = library.add(cube_bytes(), "Elsewhere/new.cube")
    assert repeated["duplicate"] and repeated["name"] == "我的名称" and repeated["starred"] and repeated["author"] == "作者"
    assert repeated["group"] == a["group"]
    stored = library.listing()
    assert len(stored["luts"]) == 2 and stored["groups"] == [{"id": a["group"], "name": "My LUTs"}]
    assert (library.home / "luts" / a["file"]).read_bytes() == cube_bytes()
    assert all(len(p.stem) == 64 for p in (library.home / "luts").glob("*.cube"))


@pytest.mark.parametrize("name", ["__MACOSX/a.cube", "folder/__MACOSX/ignored.cube", "folder/._a.cube", "._other.png"])
def test_metadata_entries_are_skipped_without_creating_data(library, name):
    assert library.add(b"not needed", name)["skipped"] is True
    assert not library.home.exists()


@pytest.mark.parametrize("name", ["look.3dl", "look.png", "look", "look.cube.txt"])
def test_other_formats_are_reported_without_data(library, name):
    with pytest.raises(LutError, match="不支持这种 LUT 格式"):
        library.add(cube_bytes(), name)
    assert not library.home.exists()


@pytest.mark.parametrize("raw", [b"bad", b"\xff\xfe", b"LUT_3D_SIZE 2\n0 0 0\n", b"LUT_3D_SIZE 2\n" + b"nan 0 0\n" * 8])
def test_bad_cube_has_no_partial_record(library, raw):
    with pytest.raises(LutError) as error:
        library.add(raw, "bad.cube")
    assert error.value.code == "cube_unreadable"
    assert not library.home.exists()


def test_filenames_title_are_data_and_export_title_is_clean(library, tmp_path):
    raw = cube_bytes(title="../<img src=x> ' # name")
    item = library.add(raw, '../<b>\n"file#.cube')
    assert item["cube_id"] == hashlib.sha256(raw).hexdigest()
    assert (library.home / "luts" / f"{item['cube_id']}.cube").read_bytes() == raw
    exported = library.export(item["cube_id"])
    text = Path(exported["path"]).read_text(encoding="utf-8")
    title = next(line for line in text.splitlines() if line.startswith("TITLE"))
    assert title.count('"') == 2 and "#" not in title and "'" not in title
    parsed = Cube.read(exported["path"])
    assert np.array_equal(parsed.table3, Cube.parse(raw.decode()).table3)
    assert not (tmp_path / '<b>\n"file#.cube').exists()


@pytest.mark.parametrize(
    "name,fun,expected_state,expected_space,expected_content",
    [
        ("srgb.cube", lambda x: x, "known", "srgb", "display"),
        ("plain.cube", lambda x: x, "unknown", None, "display"),
        ("vlog.cube", lambda x: x, "conflict", None, "display"),
        ("plain.cube", lambda x: np.clip(x * 1.6, 0, 1), "unknown", None, "log"),
        ("vlog.cube", lambda x: np.clip(x * 1.6, 0, 1), "known", "vlog", "log"),
        ("srgb.cube", lambda x: np.clip(x * 1.6, 0, 1), "conflict", None, "log"),
        ("vlog.cube", lambda x: 1 - x, "guessed", "vlog", None),
        ("plain.cube", lambda x: 1 - x, "unknown", None, None),
        ("plain.cube", lambda x: x * x * (3 - 2 * x), "unknown", None, "display"),
        ("plain.cube", lambda x: 1 / (1 + np.exp(-14 * (x - 0.5))), "unknown", None, None),
        ("plain.cube", lambda x: np.clip(x + [0.2, 0, -0.2], 0, 1), "unknown", None, None),
        ("plain.cube", lambda x: 0.5 + 0.5 * np.sin(x * 7), "unknown", None, None),
    ],
)
def test_space_evidence_combinations(name, fun, expected_state, expected_space, expected_content):
    cube = Cube.parse(cube_bytes(fun).decode())
    result = identify(cube, name)
    assert (result["input_space_state"], result["input_space"]) == (expected_state, expected_space)
    assert result["space_evidence"]["content"] == expected_content


def test_technical_label_requires_independent_neutral_channels():
    assert identify(Cube.parse(cube_bytes(lambda x: x**1.8).decode()), "plain.cube")["technical"]
    assert not identify(Cube.parse(cube_bytes(lambda x: x[:, [1, 2, 0]]).decode()), "plain.cube")["technical"]
    assert not identify(Cube.parse(cube_bytes(lambda x: 1 - x).decode()), "plain.cube")["technical"]


@pytest.mark.parametrize("space", SPACES)
def test_manual_override_has_all_supported_spaces_and_invalidates(library, space):
    item = library.add(cube_bytes(), "vlog.cube")
    assert item["input_space_state"] == "conflict" and item["input_space"] is None
    updated = library.patch(item["cube_id"], {"input_space": space})
    assert updated["input_space"] == space and updated["input_space_manual"] and updated["version"] == 2
    assert updated["fit"] is None and updated["recipe"] is None and updated["camera_lut"] is None
    reopened = LutLibrary(library.home).get(item["cube_id"])
    assert reopened == updated


def test_group_management_and_star_and_rename_persist(library):
    item = library.add(cube_bytes(), "sample.cube")
    first = library.add_group("一组")
    assert library.add_group("一组") == first
    second = library.add_group("二组")
    library.patch(item["cube_id"], {"group": first["id"], "name": "显示名称", "starred": True})
    library.change_group(first["id"], "第一组")
    with pytest.raises(LutError):
        library.change_group(second["id"], "第一组")
    library.change_group(first["id"], None)
    result = LutLibrary(library.home).listing()
    assert result["luts"][0]["group"] is None and result["luts"][0]["name"] == "显示名称"
    assert result["luts"][0]["starred"] is True
    assert result["groups"] == [second]


def test_delete_preserves_legacy_recipe_bytes_without_dual_writing(library):
    item = library.add(cube_bytes(comments="# Author: Synthetic Author\n# License: CC BY-SA 4.0\n"), "sample.cube")
    folder = library.home / "my_recipes"
    folder.mkdir()
    recipe = {"name": "保留", "source": {"kind": "lut", "cube_sha256": item["cube_id"]}, "recipe": {"slot": 4}}
    (folder / "example.json").write_text(json.dumps(recipe), encoding="utf-8")
    before = (folder / "example.json").read_bytes()
    library.delete(item["cube_id"])
    assert library.listing()["luts"] == [] and not (library.home / "luts" / item["file"]).exists()
    assert (folder / "example.json").read_bytes() == before


def test_interrupted_delete_finishes_from_durable_record(library, monkeypatch):
    item = library.add(cube_bytes(), "sample.cube")
    (library.home / "my_recipes").mkdir()
    recipe_path = library.home / "my_recipes/generated.json"
    recipe_path.write_text(json.dumps({"source": {"cube_sha256": item["cube_id"]}, "recipe": {"slot": 4}}), encoding="utf-8")
    remove = PrivateDir.remove

    def stop(self, name):
        if name == f"luts/{item['file']}":
            raise OSError("sync failed")
        return remove(self, name)

    monkeypatch.setattr(PrivateDir, "remove", stop)
    with pytest.raises(OSError):
        library.delete(item["cube_id"])
    persisted = json.loads((library.home / "luts/library.json").read_text(encoding="utf-8"))
    assert item["cube_id"] not in persisted["items"] and persisted["pending_deletes"] == [item["cube_id"]]
    monkeypatch.setattr(PrivateDir, "remove", remove)
    assert LutLibrary(library.home).listing()["luts"] == []
    assert json.loads(recipe_path.read_text(encoding="utf-8"))["source"] == {"cube_sha256": item["cube_id"]}
    assert not (library.home / "luts" / item["file"]).exists()
    assert json.loads((library.home / "luts/library.json").read_text(encoding="utf-8"))["pending_deletes"] == []


@pytest.mark.parametrize(
    "changes",
    [
        {"starred": 1},
        {"input_space": "nope"},
        {"group": "nope"},
        {"author": 3},
        {"license": "a\r\nb"},
        {"name": ""},
        {"file": "somewhere"},
        {"input_gamut": "unknown"},
        {"input_range": "unknown"},
    ],
)
def test_invalid_management_changes_leave_index_identical(library, changes):
    item = library.add(cube_bytes(), "sample.cube")
    path = library.home / "luts/library.json"
    before = path.read_bytes()
    with pytest.raises(LutError):
        library.patch(item["cube_id"], changes)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "comments,expected",
    [
        (
            "# Source: local notes\n# Author: Synthetic Author\n# License: CC BY-SA 4.0\n",
            {"source": "local notes", "author": "Synthetic Author", "license": "CC BY-SA 4.0"},
        ),
        ("# 来源: 本地\n# 作者: 合成作者\n# 许可: CC BY-SA 4.0\n", {"source": "本地", "author": "合成作者", "license": "CC BY-SA 4.0"}),
        ("# A note\n# CC BY-SA 4.0\n", {"source": "", "author": "", "license": "CC BY-SA 4.0"}),
        ("", {"source": "", "author": "", "license": ""}),
    ],
)
def test_attribution_from_header_and_export_roundtrip(library, comments, expected):
    item = library.add(cube_bytes(comments=comments), "sample.cube")
    assert {key: item[key] for key in expected} == expected
    exported = library.export(item["cube_id"])
    saved = Path(exported["path"]).read_bytes()
    assert read_header(saved.decode())[0] == expected
    assert exported["sha256"] == hashlib.sha256(saved).hexdigest()


def test_multiline_attribution_roundtrip_and_no_extra_cube_directives(library):
    item = library.add(cube_bytes(), "sample.cube")
    fields = {"source": "local\nTITLE x", "author": '作者 " #\nDOMAIN_MAX 9 9 9\u2028LUT_3D_SIZE 3', "license": " CC BY-SA 4.0\n\n保留 "}
    library.patch(item["cube_id"], fields)
    exported = library.export(item["cube_id"])
    text = Path(exported["path"]).read_text(encoding="utf-8")
    assert read_header(text)[0] == fields
    assert np.array_equal(Cube.parse(text).table3, Cube.parse(cube_bytes().decode()).table3)
    assert sum(line.startswith("TITLE ") for line in text.splitlines()) == 1


@pytest.mark.parametrize("kind", ["domain", "1d", "shaper", "ranges"])
def test_cube_export_domains_shaper_and_rgb_axis_roundtrip(tmp_path, kind):
    rows = "\n".join(f"{0.7 * r + 0.1 * g} {0.5 * g + 0.2 * b} {0.4 * b + 0.3 * r}" for b in (0, 1) for g in (0, 1) for r in (0, 1))
    one = "0 0 0\n0.1 0.2 0.3\n1 1 1\n"
    text = {
        "domain": "LUT_3D_SIZE 2\nDOMAIN_MIN -1 0 0.2\nDOMAIN_MAX 2 3 4\n" + rows,
        "1d": "LUT_1D_SIZE 3\n" + one,
        "shaper": "LUT_1D_SIZE 3\nLUT_3D_SIZE 2\n" + one + rows,
        "ranges": "LUT_1D_SIZE 3\nLUT_3D_SIZE 2\nLUT_1D_INPUT_RANGE -0.1 1.2\nLUT_3D_INPUT_RANGE 0.1 0.9\n" + one + rows,
    }[kind]
    original = Cube.parse(text)
    data = export_cube(original, {})
    roundtrip = Cube.parse(data.decode())
    probes = np.random.default_rng(301).uniform(-1, 4, (80, 3))
    assert np.array_equal(original.apply(probes), roundtrip.apply(probes))
    if kind == "domain":
        # 直接按规范读取角点，与读写器的互相对拍分开。
        numeric = [list(map(float, line.split())) for line in data.decode().splitlines() if line and line[0].isdigit()]
        assert numeric[1] == [0.7, 0.0, 0.3] and numeric[2] == [0.1, 0.5, 0.0]


def test_group_cube_exports_are_hash_named_and_complete(library):
    a = library.add(cube_bytes(), "Folder/a.cube")
    b = library.add(cube_bytes(lambda x: x * 0.8), "Folder/b.cube")
    library.add(cube_bytes(lambda x: x * 0.6), "other.cube")
    exports = library.export_group(a["group"])
    assert {r["cube_id"] for r in exports} == {a["cube_id"], b["cube_id"]}
    assert all(Path(r["path"]).parent == library.home / "luts/exports" for r in exports)
    assert len(library.export_group(None)) == 1


def test_api_library_management_and_group_export(client):
    a = upload(client, name="Group/a.cube").json()
    b = upload(client, cube_bytes(lambda x: x * 0.9), "Group/b.cube").json()
    listing = client.get("/api/luts").json()
    assert len(listing["luts"]) == 2 and len(listing["groups"]) == 1
    renamed = client.patch(f"/api/luts/{a['cube_id']}", json={"name": "<b>名称</b>", "starred": True}).json()
    assert renamed["name"] == "<b>名称</b>" and renamed["starred"]
    assert client.post(f"/api/luts/{a['cube_id']}/export").status_code == 200
    exported = client.post("/api/lut-groups/export", json={"group": a["group"]}).json()["exports"]
    assert {r["cube_id"] for r in exported} == {a["cube_id"], b["cube_id"]}
    group = client.post("/api/lut-groups", json={"name": "新组"}).json()
    assert client.patch(f"/api/lut-groups/{group['id']}", json={"name": "新名称"}).status_code == 200
    assert client.delete(f"/api/lut-groups/{group['id']}").status_code == 200
    assert client.delete(f"/api/luts/{a['cube_id']}").status_code == 200
    assert len(client.get("/api/luts").json()["luts"]) == 1
    assert client.patch(f"/api/luts/{b['cube_id']}", json={"starred": 1}).status_code == 422


def test_api_checks_format_and_metadata_names_before_reading_body(client, monkeypatch):
    monkeypatch.setattr("gxr_studio.api.lut_routes.cubemod.MAX_BYTES", 1)
    other = upload(client, b"oversize", "other.3dl")
    assert other.status_code == 422 and other.json()["message"] == "不支持这种 LUT 格式"
    skipped = upload(client, b"oversize", "__MACOSX/._sample.cube")
    assert skipped.status_code == 200 and skipped.json()["skipped"]
    too_big = upload(client, b"oversize")
    assert too_big.status_code == 422 and too_big.json()["error"] == "cube_too_big"


@pytest.mark.parametrize("location", ["luts", "luts/library.json", "luts/exports"])
def test_private_locations_reject_links_into_protected_directory(library, tmp_path, location):
    item = library.add(cube_bytes(), "sample.cube")
    protected = tmp_path / "protected"
    protected.mkdir()
    target = protected / "entry" if location.endswith(".json") else protected
    if location.endswith(".json"):
        target.write_text("untouched", encoding="utf-8")
    path = library.home / location
    if path.is_dir():
        path.rename(path.with_name(path.name + "-saved"))
    elif path.exists():
        path.unlink()
    path.symlink_to(target, target_is_directory=not location.endswith(".json"))
    before = {p.name: p.read_bytes() for p in protected.iterdir() if p.is_file()}
    audit.protect(protected)
    try:
        with pytest.raises((PrivatePathError, OSError)):
            library.export(item["cube_id"])
    finally:
        audit.unprotect(protected)
    assert before == {p.name: p.read_bytes() for p in protected.iterdir() if p.is_file()}


def test_private_home_in_repository_is_refused(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    library = LutLibrary(repo / "data")
    with pytest.raises(PrivatePathError):
        library.add(cube_bytes(), "sample.cube")
    assert not library.home.exists()


def test_durable_index_write_failure_is_visible_and_index_stays_old(library, monkeypatch):
    item = library.add(cube_bytes(), "sample.cube")
    original = PrivateDir.replace_file

    def fail(self, relative, data):
        if relative == "luts/library.json":
            raise OSError("sync failed")
        return original(self, relative, data)

    before = (library.home / "luts/library.json").read_bytes()
    monkeypatch.setattr(PrivateDir, "replace_file", fail)
    with pytest.raises(OSError):
        library.patch(item["cube_id"], {"starred": True})
    assert (library.home / "luts/library.json").read_bytes() == before


def test_cube_changes_are_detected_on_every_load(library):
    item = library.add(cube_bytes(), "sample.cube")
    library.load(item["cube_id"])
    (library.home / "luts" / item["file"]).write_bytes(cube_bytes(lambda x: x * 0.8))
    with pytest.raises(LutError) as error:
        library.load(item["cube_id"])
    assert error.value.code == "cube_changed"


def test_reimport_restores_missing_or_changed_bytes_and_keeps_metadata(library):
    raw = cube_bytes()
    item = library.add(raw, "sample.cube")
    library.patch(item["cube_id"], {"author": "保留作者"})
    path = library.home / "luts" / item["file"]
    for replacement in (None, b"changed"):
        if replacement is None:
            path.unlink()
        else:
            path.write_bytes(replacement)
        result = library.add(raw, "second.cube")
        assert result["duplicate"] and result["author"] == "保留作者"
        assert path.read_bytes() == raw


def test_legacy_sha_files_are_registered_on_first_open(library):
    raw = cube_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    directory = library.home / "luts"
    directory.mkdir(parents=True)
    (directory / f"{digest}.cube").write_bytes(raw)
    (directory / f"{digest}.filename").write_text("old_srgb.cube\n", encoding="utf-8")
    listing = library.listing()
    assert len(listing["luts"]) == 1
    assert listing["luts"][0]["filename"] == "old_srgb.cube"
    assert listing["luts"][0]["input_space_state"] == "known"
    assert library.load(digest).source_sha256 == digest
    assert (directory / f"{digest}.cube").read_bytes() == raw
    assert (directory / "library.json").exists()


@pytest.mark.parametrize(
    "broken",
    [
        b"{",
        b"[]",
        b'{"schema":"unknown","groups":[],"items":{}}',
        b'{"schema":"gxr-studio/lut-library/1","groups":[],"items":{},"revision":true}',
    ],
)
def test_unreadable_index_is_reported_and_preserved(library, broken):
    (library.home / "luts").mkdir(parents=True)
    path = library.home / "luts/library.json"
    path.write_bytes(broken)
    with pytest.raises(LutError) as error:
        library.listing()
    assert error.value.code == "lut_library_unreadable" and path.read_bytes() == broken


def test_parsed_cube_cache_remains_bounded_and_checks_current_bytes(library):
    ids = []
    for i in range(6):
        item = library.add(cube_bytes(title=f"synthetic {i}", size=2), "sample.cube")
        ids.append(item["cube_id"])
        assert library.load(item["cube_id"]).title == f"synthetic {i}"
    assert len(library._cubes) == 4
    assert ids[0] not in library._cubes


def test_maximum_size_and_extreme_finite_values(library, monkeypatch):
    monkeypatch.setattr("gxr_studio.lut.library.MAX_BYTES", 10)
    with pytest.raises(LutError) as error:
        library.add(b" " * 11, "sample.cube")
    assert error.value.code == "cube_too_big"
    monkeypatch.setattr("gxr_studio.lut.library.MAX_BYTES", 64 * 1024 * 1024)
    item = library.add(cube_bytes(lambda x: np.full_like(x, 1e308), size=2), "sample.cube")
    assert item["input_space_state"] == "unknown" and item["space_evidence"]["middle"] is None
    assert item["technical"] is False


def test_space_override_preserves_frozen_completed_fit_and_manual_is_not_guessed(client, settings):
    from test_lut import _plant_fit

    item = upload(client, name="srgb.cube").json()
    job_id = "1a" * 6
    _plant_fit(client, item["cube_id"], "srgb", False, job_id)
    assert client.get(f"/api/lut/fit/{job_id}").json()["state"] == "done"
    client.patch(f"/api/luts/{item['cube_id']}", json={"input_space": "srgb"})
    result = client.get(f"/api/lut/fit/{job_id}").json()
    assert result["state"] == "done"
    assert not client.app.state.ctx.lut._filename_guess_matches(item["cube_id"], "srgb")
    assert client.post("/api/my-recipes", json={"job_id": job_id, "name": "冻结结果"}).status_code == 200


def test_generated_recipe_keeps_attribution_after_lut_deletion(client, settings):
    from test_lut import _plant_fit

    item = upload(client).json()
    cid = item["cube_id"]
    fields = {"source": "合成来源", "author": "Synthetic Author", "license": "CC BY-SA 4.0"}
    client.patch(f"/api/luts/{cid}", json=fields)
    job_id = "4d" * 6
    _plant_fit(client, cid, "srgb", False, job_id)
    response = client.post("/api/my-recipes", json={"job_id": job_id, "name": "带署名配方"})
    assert response.status_code == 200, response.text
    recipe = response.json()
    assert recipe["source"]["attribution"] == fields
    assert client.delete(f"/api/luts/{cid}").status_code == 200
    persisted = client.get(f"/api/my-recipes/{recipe['id']}").json()
    assert persisted["source"]["attribution"] == fields
    assert client.get("/api/my-recipes").json()["recipes"][0]["source_available"] is False


def test_space_override_stops_a_running_job(client, settings):
    import sys

    from test_lut import _plant_fit

    item = upload(client).json()
    job_id = "2b" * 6
    _plant_fit(client, item["cube_id"], "srgb", False, job_id)
    (settings.home_dir / "lut_jobs" / job_id / "result.json").unlink()
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    client.app.state.ctx.lut._procs[job_id] = process
    try:
        response = client.patch(f"/api/luts/{item['cube_id']}", json={"input_space": "rec709"})
        assert response.status_code == 200 and process.poll() is not None
        assert client.get(f"/api/lut/fit/{job_id}").json()["state"] == "cancelled"
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_header_extra_comments_and_invalid_optional_metadata_are_readable():
    text = "# GXR-STUDIO-METADATA: {invalid}\n# Website: local\n# Copyright: Synthetic Author\n# Keep this note\n" + cube_bytes().decode()
    metadata, comments = read_header(text)
    assert metadata == {"source": "local", "author": "Synthetic Author", "license": ""}
    assert comments == ["Keep this note"]
    data = export_cube(Cube.parse(text), {**metadata, "comments": comments})
    assert "# Keep this note\n" in data.decode()


def test_missing_groups_and_missing_lut_have_clear_errors(library):
    for action, args in [(library.change_group, ("missing", None)), (library.export_group, ("missing",)), (library.get, ("not-an-id",))]:
        with pytest.raises(LutError) as error:
            action(*args)
        assert error.value.status == 404


def test_api_private_directory_failure_writes_no_bytes(client, settings, tmp_path):
    protected = tmp_path / "protected"
    protected.mkdir()
    if settings.home_dir.exists():
        settings.home_dir.rename(settings.home_dir.with_name("saved-home"))
    settings.home_dir.symlink_to(protected, target_is_directory=True)
    audit.protect(protected)
    try:
        response = upload(client)
    finally:
        audit.unprotect(protected)
    assert response.status_code == 422 and response.json()["error"] == "private_path_forbidden"
    assert list(protected.iterdir()) == []


def test_new_recipe_save_does_not_follow_or_write_legacy_recipe_link(client, settings, tmp_path):
    from test_lut import _plant_fit

    item = upload(client).json()
    job_id = "3c" * 6
    _plant_fit(client, item["cube_id"], "srgb", False, job_id)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (settings.home_dir / "my_recipes").symlink_to(elsewhere, target_is_directory=True)
    response = client.post("/api/my-recipes", json={"job_id": job_id, "name": "保留在应用数据"})
    assert response.status_code == 200 and response.json()["recipe"]["kind"] == "local"
    assert list(elsewhere.iterdir()) == []


def test_worker_result_rejects_a_link_outside_its_private_root(tmp_path):
    home = tmp_path / "app-data"
    home.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (home / "lut_jobs").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(OSError):
        write_worker_result(home / "lut_jobs/job/result.json", {"state": "cancelled"}, home=home)
    assert list(elsewhere.iterdir()) == []


def _photo_files(volumes, *, raw=False, matrix=True, lens="RICOH GR LENS A12 50mm F2.5 MACRO"):
    folder = volumes / "SYNTHETIC" / "DCIM/100RICOH"
    folder.mkdir(parents=True)
    image = np.full((80, 120, 3), [40, 100, 180], np.uint8)
    Image.fromarray(image, "RGB").save(folder / "SYNTHETIC.JPG", quality=100, subsampling=0)
    if raw:
        xyz_matrix = [(50721, "SRATIONAL", [1, 0, 0, 0, 1, 0, 0, 0, 1]), (50778, "SHORT", [21])]
        data = build_dng(bayer_scene(80, 120, image / 255), white=4095, lens=lens, root_extra=xyz_matrix if matrix else [])
        (folder / "SYNTHETIC.DNG").write_bytes(data)
    return folder


def _get_photo(client):
    import_all(client)
    return client.get("/api/photos").json()[0]


def test_photo_lut_preview_export_persist_and_manual_space_change(client, volumes, settings):
    folder = _photo_files(volumes)
    originals = {p.name: p.read_bytes() for p in folder.iterdir()}
    photo = _get_photo(client)
    # 非对称线性映射，对每个通道的期望值直接写出。
    item = upload(client, cube_bytes(lambda x: x * [0.5, 0.8, 1.0])).json()
    assert client.patch(f"/api/luts/{item['cube_id']}", json={"input_space": "srgb"}).status_code == 200
    put = client.put(f"/api/photos/{photo['id']}/edit", json={"lut": item["cube_id"]})
    assert put.status_code == 200, put.text
    assert put.json()["lut"] == item["cube_id"]
    rendered = client.get(f"/api/photos/{photo['id']}/render?stage=final&quality=100")
    assert rendered.status_code == 200 and rendered.headers["x-gxr-label"] == "prediction"
    pixel = np.asarray(Image.open(io.BytesIO(rendered.content)))[40, 60]
    assert np.max(np.abs(pixel.astype(float) - [20, 80, 180])) <= 3
    exported = client.post(f"/api/photos/{photo['id']}/export", json={"format": "png", "quality": 100})
    assert exported.status_code == 200, exported.text
    result = exported.json()
    assert result["render"] == "lut-prediction"
    assert np.max(np.abs(np.asarray(Image.open(result["path"]))[40, 60].astype(float) - [20, 80, 180])) <= 2
    assert client.get(f"/api/photos/{photo['id']}/edit").json()["lut"] == item["cube_id"]
    assert client.patch(f"/api/luts/{item['cube_id']}", json={"input_space": "gamma24"}).status_code == 200
    changed = client.get(f"/api/photos/{photo['id']}/render?stage=final&quality=100")
    assert changed.status_code == 200 and changed.content != rendered.content
    assert originals == {p.name: p.read_bytes() for p in folder.iterdir()}
    assert settings.library_dir in Path(result["path"]).parents or settings.export_dir in Path(result["path"]).parents
    assert client.delete(f"/api/photos/{photo['id']}/edit").json()["lut"] is None


@pytest.mark.parametrize("image_key", ["thumb_url", "preview_url"])
def test_space_override_changes_warm_image_cache_and_browser_url(client, volumes, image_key):
    _photo_files(volumes)
    photo = _get_photo(client)
    item = upload(client, cube_bytes(lambda x: x * [0.5, 0.8, 1.0])).json()
    cid = item["cube_id"]
    client.patch(f"/api/luts/{cid}", json={"input_space": "srgb"})
    client.put(f"/api/photos/{photo['id']}/edit", json={"lut": cid})
    before = client.get(f"/api/photos/{photo['id']}").json()
    first = client.get(before[image_key])
    assert first.status_code == 200
    client.patch(f"/api/luts/{cid}", json={"input_space": "gamma24"})
    after = client.get(f"/api/photos/{photo['id']}").json()
    second = client.get(after[image_key])
    assert second.status_code == 200
    assert before[image_key] != after[image_key] and first.content != second.content
    assert client.get(after[image_key]).content == second.content
    client.patch(f"/api/luts/{cid}", json={"name": "新名称", "starred": True})
    assert client.get(f"/api/photos/{photo['id']}").json()[image_key] == after[image_key]
    client.delete(f"/api/luts/{cid}")
    deleted = client.get(f"/api/photos/{photo['id']}").json()
    assert deleted[image_key] != after[image_key]
    assert client.get(deleted[image_key]).content != second.content


@pytest.mark.parametrize("warm", [False, True])
def test_lut_image_cache_rejects_a_link_outside_library(client, volumes, tmp_path, warm):
    _photo_files(volumes)
    photo = _get_photo(client)
    item = upload(client).json()
    client.put(f"/api/photos/{photo['id']}/edit", json={"lut": item["cube_id"]})
    photo = client.get(f"/api/photos/{photo['id']}").json()
    if warm:
        assert client.get(photo["thumb_url"]).status_code == 200
    directory = client.app.state.ctx.lib.thumbs_dir
    saved = directory.with_name("saved-thumbs")
    directory.rename(saved)
    outside = tmp_path / "outside-cache"
    shutil.copytree(saved, outside)
    directory.symlink_to(outside, target_is_directory=True)
    before = {str(p.relative_to(outside)): p.read_bytes() for p in outside.rglob("*") if p.is_file()}
    response = client.get(photo["thumb_url"])
    assert response.status_code == 422 and response.json()["error"] == "private_path_forbidden"
    assert before == {str(p.relative_to(outside)): p.read_bytes() for p in outside.rglob("*") if p.is_file()}


def test_unknown_lut_space_rejects_application_and_draft_is_not_saved(client, volumes):
    _photo_files(volumes)
    photo = _get_photo(client)
    item = upload(client, cube_bytes(lambda x: 1 - x)).json()
    rev = client.get(f"/api/photos/{photo['id']}/edit").json()["rev"]
    response = client.put(f"/api/photos/{photo['id']}/edit", json={"lut": item["cube_id"]})
    assert response.status_code == 422 and response.json()["error"] == "lut_space_required"
    assert client.get(f"/api/photos/{photo['id']}/edit").json()["rev"] == rev
    client.patch(f"/api/luts/{item['cube_id']}", json={"input_space": "srgb"})
    draft = client.post(f"/api/photos/{photo['id']}/render?stage=final", json={"lut": item["cube_id"]})
    assert draft.status_code == 200
    assert client.get(f"/api/photos/{photo['id']}/edit").json()["lut"] is None


@pytest.mark.parametrize(
    "lens,raw,matrix,accepted",
    [
        ("RICOH GR LENS A12 50mm F2.5 MACRO", True, True, True),
        ("RICOH GR LENS A12 50mm F2.5 MACRO", False, True, False),
        ("RICOH LENS A12 28mm", True, True, True),
        ("GXR MOUNT", True, True, True),
    ],
)
def test_log_lut_uses_scene_xyz_and_requires_raw(client, volumes, lens, raw, matrix, accepted):
    _photo_files(volumes, raw=raw, matrix=matrix, lens=lens)
    photo = _get_photo(client)
    item = upload(client).json()
    client.patch(f"/api/luts/{item['cube_id']}", json={"input_space": "llog"})
    result = client.put(f"/api/photos/{photo['id']}/edit", json={"lut": item["cube_id"]})
    assert result.status_code == (200 if accepted else 422), result.text
    if accepted:
        response = client.get(f"/api/photos/{photo['id']}/render?stage=final&size=preview")
        assert response.status_code == 200, response.text
        exported = client.post(f"/api/photos/{photo['id']}/export", json={"format": "png"})
        assert exported.status_code == 200, exported.text
        assert Image.open(exported.json()["path"]).size == (120, 80)


def test_deleted_lut_is_not_silently_omitted_from_photo_export(client, volumes):
    _photo_files(volumes)
    photo = _get_photo(client)
    item = upload(client).json()
    client.put(f"/api/photos/{photo['id']}/edit", json={"lut": item["cube_id"]})
    client.delete(f"/api/luts/{item['cube_id']}")
    result = client.post(f"/api/photos/{photo['id']}/export", json={})
    assert result.status_code == 422 and result.json()["error"] == "cube_not_found"


def test_display_lut_with_raw_only_uses_full_raw_for_export(client, volumes):
    folder = _photo_files(volumes, raw=True)
    (folder / "SYNTHETIC.JPG").unlink()
    photo = _get_photo(client)
    item = upload(client).json()
    assert client.put(f"/api/photos/{photo['id']}/edit", json={"lut": item["cube_id"]}).status_code == 200
    preview = client.get(f"/api/photos/{photo['id']}/render?stage=final&size=preview")
    assert preview.status_code == 200, preview.text
    assert Image.open(io.BytesIO(preview.content)).size == (60, 40)
    exported = client.post(f"/api/photos/{photo['id']}/export", json={"format": "png"})
    assert exported.status_code == 200 and Image.open(exported.json()["path"]).size == (120, 80)


def test_log_missing_colour_matrix_reports_error_in_preview_and_export(client, volumes):
    _photo_files(volumes, raw=True, matrix=False)
    photo = _get_photo(client)
    item = upload(client).json()
    client.patch(f"/api/luts/{item['cube_id']}", json={"input_space": "llog"})
    client.put(f"/api/photos/{photo['id']}/edit", json={"lut": item["cube_id"]})
    for response in (client.get(f"/api/photos/{photo['id']}/render?stage=final"), client.post(f"/api/photos/{photo['id']}/export", json={})):
        assert response.status_code == 422 and response.json()["error"] == "no_matrix"


def test_draft_log_on_jpg_is_refused_and_original_view_does_not_apply_lut(client, volumes):
    _photo_files(volumes)
    photo = _get_photo(client)
    item = upload(client).json()
    client.patch(f"/api/luts/{item['cube_id']}", json={"input_space": "llog"})
    draft = client.post(f"/api/photos/{photo['id']}/render?stage=final", json={"lut": item["cube_id"]})
    assert draft.status_code == 422 and draft.json()["error"] == "lut_needs_raw"
    original = client.post(f"/api/photos/{photo['id']}/render?stage=original", json={"lut": item["cube_id"]})
    assert original.status_code == 200


def test_nonfinite_display_conversion_is_reported(client, volumes):
    _photo_files(volumes)
    photo = _get_photo(client)
    item = upload(client, cube_bytes(lambda x: np.full_like(x, 1e308), size=2)).json()
    client.patch(f"/api/luts/{item['cube_id']}", json={"input_space": "srgb"})
    with np.errstate(all="ignore"):
        response = client.post(f"/api/photos/{photo['id']}/render?stage=final", json={"lut": item["cube_id"]})
    assert response.status_code == 422 and response.json()["error"] == "lut_nonfinite"


def test_lut_selection_is_part_of_edit_digest():
    a = parse_edit({})
    b = parse_edit({"lut": "a" * 64})
    assert b.digest() != a.digest() and b.to_json()["lut"] == "a" * 64
    with pytest.raises(EditError):
        parse_edit({"lut": "../file"})


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node")
def test_folder_reader_consumes_all_batches_and_import_keeps_each_result(tmp_path):
    source = Path(__file__).resolve().parents[1] / "web/assets/lut_files.js"
    (tmp_path / "files.mjs").write_bytes(source.read_bytes())
    script = tmp_path / "check.mjs"
    script.write_text(
        """
import { droppedFiles, importFiles } from './files.mjs';
const file = (name) => ({ name, isFile:true, file:(resolve)=>resolve({name,size:5}) });
const folder = (name,batches) => ({name,isDirectory:true,createReader:()=>({readEntries:(resolve)=>resolve(batches.shift()||[])})});
const root = folder('Folder',[
  [file('one.cube'),folder('__MACOSX',[[file('ignore.cube')]])],
  [file('two.cube'),file('bad.3dl'),file('._ignore')],[]]);
const files = await droppedFiles({items:[{webkitGetAsEntry:()=>root}]});
const results = await importFiles(files,async (f,path)=>{
  if(f.name.endsWith('.3dl'))throw new Error('不支持这种 LUT 格式');
  return {cube_id:path,duplicate:f.name==='two.cube'};
});
console.log(JSON.stringify({paths:files.map(f=>f.path),results}));
""",
        encoding="utf-8",
    )
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, check=True, timeout=15, encoding="utf-8")
    data = json.loads(out.stdout)
    assert data["paths"] == ["Folder/one.cube", "Folder/two.cube", "Folder/bad.3dl"]
    assert [row["status"] for row in data["results"]] == ["imported", "duplicate", "error"]
    assert data["results"][-1]["message"] == "不支持这种 LUT 格式"
