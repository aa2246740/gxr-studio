"""LUT 复审修复：合成署名、入口文案和真实短写的回归。"""

from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from conftest import PORT
from gxr_studio import privacy
from gxr_studio.app import create_app
from gxr_studio.lut.errors import LutError
from gxr_studio.lut.library import LutLibrary, label
from gxr_studio.lut.metadata import read_header
from gxr_studio.lut.service import clean_name

ATTRIBUTION = {"source": "本地合成参考", "author": "合成作者 A", "license": "合成许可文字 B"}
COMMENTS = "# Source: 本地合成参考\n# Author: 合成作者 A\n# License: 合成许可文字 B\n"
BODY = (
    '\nTITLE "Synthetic review"\nLUT_3D_SIZE 2\n'
    "0 0 0\n0.5 0 0\n0 0.80000000000000004 0\n0.5 0.80000000000000004 0\n"
    "0 0 1\n0.5 0 1\n0 0.80000000000000004 1\n0.5 0.80000000000000004 1\n"
)
REVIEW_SHA = {
    "header": "5b9029d249b88be16b234ba9ffa0e714588227049442217d7fd0302f5db170fa",
    "middle": "43fd4be7462ecf8ab195e32147ad1e81af2a4c61d278969f7e98fe883a2e2058",
    "trailer": "6987cd42fbebc6e84bb2180eb3f6f5dfaa23340ae409d84d3e0348fe63d762bb",
}


def review_bytes(position):
    text = {"header": COMMENTS + BODY, "middle": BODY.replace("0 0.800", COMMENTS + "0 0.800", 1), "trailer": BODY + COMMENTS}[position]
    return text.encode()


@pytest.fixture
def library(tmp_path):
    return LutLibrary(tmp_path / "app-data")


@pytest.fixture
def api(settings):
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        yield client


@pytest.mark.parametrize("position", ["header", "middle", "trailer"])
def test_review_attribution_api_roundtrip_is_byte_identical(api, position):
    raw = review_bytes(position)
    assert hashlib.sha256(raw).hexdigest() == REVIEW_SHA[position]
    response = api.post("/api/lut/upload", content=raw, headers={"X-GXR-Filename": quote("Review/example.cube", safe="")})
    assert response.status_code == 200
    item = response.json()
    assert {key: item[key] for key in ATTRIBUTION} == ATTRIBUTION
    exported = api.post(f"/api/luts/{item['cube_id']}/export")
    assert exported.status_code == 200
    saved = Path(exported.json()["path"]).read_bytes()
    assert saved == raw
    assert exported.json()["sha256"] == REVIEW_SHA[position]
    assert read_header(saved.decode())[0] == ATTRIBUTION
    group = api.post("/api/lut-groups/export", json={"group": item["group"]})
    assert group.status_code == 200
    assert Path(group.json()["exports"][0]["path"]).read_bytes() == raw


@pytest.mark.parametrize("position", ["header", "middle", "trailer"])
@pytest.mark.parametrize("ending", ["lf", "bom-crlf", "no-final-newline"])
def test_unchanged_export_preserves_every_comment_and_file_bytes(library, position, ending):
    raw = review_bytes(position).replace(b"0 0 0\n", b"0 0 0 # inline note\n")
    raw += b"#\n  #  repeated note\n# repeated note\n# GXR-STUDIO-METADATA: {invalid}\n"
    if ending == "bom-crlf":
        raw = b"\xef\xbb\xbf" + raw.replace(b"\n", b"\r\n")
    elif ending == "no-final-newline":
        raw = raw.rstrip(b"\n")
    item = library.add(raw, "example.cube")
    assert Path(library.export(item["cube_id"])["path"]).read_bytes() == raw


def test_title_cleanup_changes_only_title_and_keeps_its_inline_comment(library):
    raw = review_bytes("middle").replace(b'TITLE "Synthetic review"', b'  TITLE "Synthetic # review"  # title note')
    raw = b"\xef\xbb\xbf" + raw.replace(b"\n", b"\r\n")
    item = library.add(raw, "example.cube")
    expected = raw.replace(b'"Synthetic # review"', b'"Synthetic review"')
    assert Path(library.export(item["cube_id"])["path"]).read_bytes() == expected


def test_edited_attribution_keeps_original_comments_and_roundtrips_new_fields(library):
    raw = review_bytes("middle") + b"# trailer note\n"
    item = library.add(raw, "example.cube")
    fields = {"source": "updated\nsource", "author": "更新作者", "license": "新说明\n\n保留 "}
    library.patch(item["cube_id"], fields)
    saved = Path(library.export(item["cube_id"])["path"]).read_bytes()
    assert raw in saved
    assert read_header(saved.decode())[0] == fields
    assert saved.startswith(b"# Source: updated\n# Source: source\n")


@pytest.mark.parametrize("edited", [False, True])
def test_existing_library_recovers_missed_attribution_without_replacing_edits(library, edited):
    raw = review_bytes("trailer")
    item = library.add(raw, "example.cube")
    path = library.home / "luts/library.json"
    doc = json.loads(path.read_text())
    record = doc["items"][item["cube_id"]]
    record.pop("attribution_version", None)
    record.update(source="", author="用户作者" if edited else "", license="", comments=[])
    path.write_text(json.dumps(doc))
    reopened = LutLibrary(library.home)
    recovered = reopened.get(item["cube_id"])
    expected = {**ATTRIBUTION, **({"author": "用户作者"} if edited else {})}
    assert {key: recovered[key] for key in expected} == expected
    assert recovered["attribution_version"] == 2
    assert recovered["version"] == item["version"]
    persisted = path.read_bytes()
    assert reopened.get(item["cube_id"])["author"] == expected["author"]
    assert path.read_bytes() == persisted
    saved = Path(reopened.export(item["cube_id"])["path"]).read_bytes()
    assert raw in saved and read_header(saved.decode())[0] == expected


@pytest.mark.parametrize("failure", ["short-write", "file-sync"])
def test_index_write_failure_removes_temporary_file_and_preserves_original(library, monkeypatch, failure):
    item = library.add(review_bytes("header"), "example.cube")
    index = library.home / "luts/library.json"
    original = index.read_bytes()
    real_write = os.write
    calls = 0

    def short_then_fail(fd, content):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, bytes(content)[: max(1, len(content) // 2)])
        raise OSError(errno.ENOSPC, "storage full")

    def fail_sync(_fd):
        raise OSError(errno.EIO, "sync failed")

    with monkeypatch.context() as patch:
        if failure == "short-write":
            patch.setattr(os, "write", short_then_fail)
        else:
            patch.setattr(privacy, "_full_sync", fail_sync)
        with pytest.raises(OSError):
            library.patch(item["cube_id"], {"starred": True})
    assert index.read_bytes() == original
    assert list(index.parent.glob(".library.json.*.tmp")) == []
    assert (index.parent / item["file"]).read_bytes() == review_bytes("header")
    assert not LutLibrary(library.home).get(item["cube_id"])["starred"]
    assert library.patch(item["cube_id"], {"starred": True})["starred"] is True


@pytest.mark.parametrize("name", ["", "名" * 256], ids=["empty", "too-long"])
@pytest.mark.parametrize("entry", ["lut", "group-add", "group-rename"])
def test_name_limits_are_specific_at_each_library_entry(api, name, entry):
    if entry == "lut":
        item = api.post("/api/lut/upload", content=review_bytes("header"), headers={"X-GXR-Filename": "example.cube"}).json()
        response = api.patch(f"/api/luts/{item['cube_id']}", json={"name": name})
        message = "LUT 名称需为 1–255 个字符。"
    elif entry == "group-add":
        response = api.post("/api/lut-groups", json={"name": name})
        message = "分组名称需为 1–255 个字符。"
    else:
        group = api.post("/api/lut-groups", json={"name": "现有分组"}).json()
        response = api.patch(f"/api/lut-groups/{group['id']}", json={"name": name})
        message = "分组名称需为 1–255 个字符。"
    assert response.status_code == 422 and response.json()["message"] == message


@pytest.mark.parametrize("name", ["", "名" * 41], ids=["empty", "too-long"])
@pytest.mark.parametrize("entry", ["save", "rename"])
def test_recipe_name_limits_are_specific_at_each_entry(api, settings, name, entry):
    if entry == "save":
        response = api.post("/api/my-recipes", json={"job_id": "ab" * 6, "candidate": 0, "name": name})
    else:
        rid = "cd" * 6
        api.app.state.ctx.lut._write_json(settings.home_dir / f"my_recipes/{rid}.json", {"id": rid, "name": "现有配方"})
        response = api.put(f"/api/my-recipes/{rid}", json={"name": name})
    assert response.status_code == 422
    assert response.json()["message"] == "配方名称需为 1–40 个字符。"


def test_name_boundaries_and_invalid_values_use_standard_term(library):
    item = library.add(review_bytes("header"), "example.cube")
    assert library.patch(item["cube_id"], {"name": "名" * 255})["name"] == "名" * 255
    assert library.add_group("组" * 255)["name"] == "组" * 255
    assert clean_name("名" * 40) == "名" * 40
    for clean in (label, clean_name):
        for value in (None, "a\x00b"):
            with pytest.raises(LutError) as error:
                clean(value)
            assert error.value.message == "无法保存。名称无效。"
