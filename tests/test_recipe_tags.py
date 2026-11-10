"""GXR1：合成 JPEG / CFA DNG，导入缓存、相机顺序、信息面板与导出。没有真机照片。"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import PORT
from gxr_studio import meta
from gxr_studio.api.routes import photo_json
from gxr_studio.app import create_app
from gxr_studio.camdata import datcodec
from gxr_studio.camdata.catalog_names import CAMERA_UNITS, CATALOG_NAMES, recipe_name
from gxr_studio.camdata.recipe_tags import copyright_text, parse_recipe_tag, recipe_payload
from gxr_studio.cards.discovery import CardReader, find_cards
from gxr_studio.cards.importer import ImportOptions, run_import
from gxr_studio.export import ExportOptions, render, render_raw, write_with_exif
from gxr_studio.library.store import sha256_file
from raw_support import build_dng

TAG = "GXR1 6a3a5a3c02 28 06 Classic Neg"
UNKNOWN = "GXR1 0123456789 MT 99 Camera name"
ROOT = Path(__file__).resolve().parents[1]


def sample(path: Path, copyright: str | None) -> Path:
    """测试时生成最小 JPEG 或带 CFA 的 DNG，不保存二进制夹具。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".DNG":
        extra = [(33432, "ASCII", copyright)] if copyright is not None else []
        path.write_bytes(build_dng(np.full((16, 24), 1000, dtype=np.uint16), root_extra=extra))
    else:
        exif = Image.Exif()
        exif[306] = "2026:10:16 18:51:53"
        if copyright is not None:
            exif[33432] = copyright
        Image.new("RGB", (24, 16), (90, 110, 120)).save(path, "JPEG", exif=exif)
    return path


@pytest.mark.parametrize(
    ("text", "identity", "unit", "position", "name", "tweaked"),
    [
        (TAG, "6a3a5a3c02", "28", 6, "Classic Neg", False),
        (UNKNOWN, "0123456789", "MT", 99, "Camera name", False),
        ("GXR1 d25d2f0a68 50 01 Clas", "d25d2f0a68", "50", 1, "Clas", False),
        ("GXR1 d25d2f0a68 50 01", "d25d2f0a68", "50", 1, None, False),
        (TAG + " *", "6a3a5a3c02", "28", 6, "Classic Neg", True),
        ("GXR1 6A3A5A3C02 MT 01 *", "6a3a5a3c02", "MT", 1, None, True),
    ],
)
def test_strict_parser_accepts_complete_truncated_and_nameless_tags(text, identity, unit, position, name, tweaked):
    expected = {"catalog_identity": identity, "unit": unit, "position": position, "short_name": name, "tweaked": tweaked}
    # Pillow 会去掉结尾 NUL；原始字节没有 NUL 时也安全地按有界文本解析。
    for value in (text, text + "\0", text.encode(), (text + "\0").encode()):
        assert parse_recipe_tag(value) == expected


@pytest.mark.parametrize(
    "text",
    [
        None,
        33432,
        [],
        "",
        "Alice, 2026",
        "GXR1",
        "GXR1 ",
        "GXR1 6a3a5a3c02 28",
        "GXR1 6a3a5a3c0 28 06",
        "GXR1 6a3a5a3c020 28 06",
        "GXR1 6a3a5a3c0z 28 06",
        "GXR1 6a3a5a3c02 29 06",
        "GXR1 6a3a5a3c02 mt 06",
        "GXR1 6a3a5a3c02 28 00",
        "GXR1 6a3a5a3c02 28 100",
        "GXR1 6a3a5a3c02 28 6",
        "GXR1  6a3a5a3c02 28 06",
        " " + TAG,
        TAG + " ",
        TAG + "\n",
        TAG.replace("28 ", "28\t"),
        TAG + "é",
        TAG + "\x00junk",
        TAG + "\0\0",
        TAG + "*",
        TAG + " **",
        TAG + " * *",
        TAG + "\x01",
        b"GXR1 0123456789 28 01 \xff",
        "GXR1 0123456789 28 01 " + "x" * 26,
    ],
)
def test_malformed_or_ordinary_copyright_is_not_a_recipe(text):
    assert parse_recipe_tag(text) is None


def test_limit_counts_the_terminator_and_does_not_hide_non_ascii():
    text = "GXR1 0123456789 28 01 " + "x" * 24
    assert len(text) == 46
    assert parse_recipe_tag(text + "\0") is not None
    assert parse_recipe_tag((text + "\0").encode()) is not None
    assert parse_recipe_tag(text + "x") is None
    assert parse_recipe_tag(text + "x\0") is None
    assert copyright_text("  Alice © 2026  \0") == "  Alice © 2026  "
    assert copyright_text(b"Alice\0") == "Alice"
    assert copyright_text(123) is None


@pytest.mark.parametrize("kind", ("JPG", "DNG"))
@pytest.mark.parametrize("text", (TAG, UNKNOWN, TAG[:-4], "GXR1 0123456789 50 01", TAG + " *", "  Alice 2026  ", None, "GXR1 bad"))
def test_jpeg_and_dng_use_the_existing_exif_read_once(tmp_path, monkeypatch, kind, text):
    path = sample(tmp_path / f"photo.{kind}", text)
    opened = []
    original = Image.open

    def open_once(*args, **kwargs):
        opened.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(Image, "open", open_once)
    parsed = meta.read_jpeg_meta(path) if kind == "JPG" else meta.read_dng_meta(path)
    assert parsed.error is None
    assert parsed.copyright == text
    assert parsed.recipe_tag == parse_recipe_tag(text)
    assert opened == [path]


def test_lookup_is_bound_to_identity_unit_and_camera_position():
    assert CAMERA_UNITS == {"28": "qlaunch", "50": "jlaunch", "MT": "l08firm"}
    assert recipe_name("6a3a5a3c02", "28", 6) == "F-CN"
    assert recipe_name("d25d2f0a68", "50", 19) == "Vision3 500T"
    assert recipe_name("d25d2f0a68", "28", 19) == "F-CC"
    assert recipe_name("d25d2f0a68", "MT", 19) == "Vision3 500T"
    for identity, unit, position in (("0123456789", "28", 6), ("6a3a5a3c02", "28", 24), ("6a3a5a3c02", "XX", 1)):
        assert recipe_name(identity, unit, position) is None
    assert recipe_payload(None) is None
    assert recipe_payload(parse_recipe_tag(TAG))["name"] == "F-CN"
    assert recipe_payload(parse_recipe_tag(UNKNOWN))["name"] == "Camera name"
    # 已知目录里不存在的位置不能拿相机短名冒充目录名。
    assert recipe_payload(parse_recipe_tag("GXR1 6a3a5a3c02 28 99 Wrong"))["name"] is None


@pytest.mark.parametrize("generation,identity", ((176, "d25d2f0a68"), (178, "6a3a5a3c02")))
def test_all_registered_names_match_the_sealed_catalog(generation, identity):
    from g178_support import need_bundle

    raw, library = need_bundle(generation)
    assert library[84:89].hex() == identity
    dat = datcodec.parse(raw, library)
    count = 4 if generation == 178 else 8
    for index, code in enumerate(("50", "28", "MT")):
        native = tuple(record[4:36].split(b"\0")[0].decode("ascii")[3:] for record in dat.modules[index].records[:count])
        extras = tuple(record.name for record in dat.recipes if record.unit_mask >> index & 1)
        assert CATALOG_NAMES[identity][code] == native + extras


@pytest.mark.parametrize("kind", ("JPG", "DNG"))
def test_import_rebuild_and_reopen_cache_the_tag_without_reading_again(lib, volumes, monkeypatch, kind):
    sample(volumes / "TEST" / "DCIM" / "100RICOH" / f"R0000001.{kind}", TAG)
    (card,) = find_cards([volumes])
    result = run_import(lib, card, ImportOptions(min_free_bytes=0, make_thumbs=False))
    assert result.imported_shots == 1
    (shot,) = lib.list_shots()
    assert shot["recipe_tag"] == parse_recipe_tag(TAG)
    assert lib.rebuild().shots == 1
    assert lib.get_shot(shot["shot_id"])["copyright"] == TAG
    lib.close()
    lib.open()

    def unexpected_read(*_args, **_kwargs):
        pytest.fail("列出照片不应重读 EXIF")

    monkeypatch.setattr(meta, "read_jpeg_meta", unexpected_read)
    monkeypatch.setattr(meta, "read_dng_meta", unexpected_read)
    (cached,) = lib.list_shots()
    assert lib.get_shot(shot["shot_id"])["recipe_tag"] == parse_recipe_tag(TAG)
    assert photo_json(cached)["recipe_tag"]["name"] == "F-CN"


def test_migration_keeps_old_shots_and_requires_no_photo_read(lib, monkeypatch):
    path = sample(lib.root / "2026/2026-10-16/R0000001.JPG", TAG)
    lib.rebuild()
    (shot,) = lib.list_shots()
    for column in ("copyright", "recipe_tag"):
        lib.db.execute(f"ALTER TABLE shots DROP COLUMN {column}")
    lib.close()
    monkeypatch.setattr(Image, "open", lambda *_a, **_kw: pytest.fail("迁移不应重读照片"))
    lib.open()
    (old,) = lib.list_shots()
    assert old["shot_id"] == shot["shot_id"] and old["files"] == shot["files"]
    assert old["copyright"] is None and old["recipe_tag"] is None and path.is_file()


@pytest.mark.parametrize(
    ("first_kind", "first_text", "new_text", "expected"),
    [("DNG", TAG, "Alice", "Alice"), ("JPG", "Alice", TAG, "Alice"), ("JPG", None, TAG, TAG)],
)
def test_completing_a_pair_keeps_jpeg_copyright_precedence(lib, volumes, first_kind, first_text, new_text, expected):
    directory = volumes / "TEST" / "DCIM" / "100RICOH"
    sample(directory / f"R0000001.{first_kind}", first_text)
    new_kind = "JPG" if first_kind == "DNG" else "DNG"
    sample(directory / f"R0000001.{new_kind}", new_text)

    class FirstHalfOnly(CardReader):
        def open_rb(self, path, **kwargs):
            if Path(path).suffix == f".{new_kind}":
                raise OSError("synthetic unreadable second half")
            return super().open_rb(path, **kwargs)

    (card,) = find_cards([volumes])
    options = ImportOptions(min_free_bytes=0, make_thumbs=False)
    assert run_import(lib, card, options, reader=FirstHalfOnly()).imported_shots == 1
    assert run_import(lib, card, options).completed_shots == 1
    (shot,) = lib.list_shots()
    assert shot["copyright"] == expected and shot["recipe_tag"] == parse_recipe_tag(expected)


def test_photo_api_returns_the_cached_recipe(settings, volumes):
    sample(volumes / "TEST/DCIM/100RICOH/R0000001.JPG", TAG + " *")
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        from test_api import import_all

        import_all(client)
        (photo,) = client.get("/api/photos").json()
        assert photo["copyright"] == TAG + " *"
        tag = client.get(f"/api/photos/{photo['id']}").json()["recipe_tag"]
        assert tag["catalog_identity"] == "6a3a5a3c02" and tag["unit"] == "28" and tag["position"] == 6
        assert tag["name"] == "F-CN" and tag["catalogued"] is True and tag["tweaked"] is True


@pytest.mark.parametrize("kind", ("JPG", "DNG"))
@pytest.mark.parametrize("fmt", ("jpeg", "png"))
@pytest.mark.parametrize("text", (TAG + " *", "  Alice 2026  ", "GXR1 malformed", None))
def test_all_export_paths_preserve_original_copyright(tmp_path, kind, fmt, text):
    path = sample(tmp_path / f"photo.{kind}", text)
    before = sha256_file(path)
    with Image.open(path) as source:
        original = source.getexif().get(33432)
    options = ExportOptions(None, 90, fmt)
    data, _ = render(path, None, options) if kind == "JPG" else render_raw(path, None, options)
    with Image.open(io.BytesIO(data)) as exported:
        assert exported.getexif().get(33432) == original
    finished = write_with_exif(data, path, "GXR Studio edited", quality=90, fmt=fmt)
    with Image.open(io.BytesIO(finished)) as exported:
        assert exported.getexif().get(33432) == original
    assert sha256_file(path) == before


@pytest.mark.parametrize("kind", ("JPG", "DNG"))
def test_export_does_not_replace_non_ascii_copyright_bytes(tmp_path, kind):
    path = sample(tmp_path / f"photo.{kind}", "Owner X 2026")
    path.write_bytes(path.read_bytes().replace(b"Owner X 2026", b"Owner \xe9 2026"))
    with Image.open(path) as source:
        original = source.getexif()[33432]
    assert original == "Owner é 2026"
    parsed = meta.read_jpeg_meta(path) if kind == "JPG" else meta.read_dng_meta(path)
    assert parsed.copyright == original and parsed.recipe_tag is None
    options = ExportOptions(None, 90)
    data, _ = render(path, None, options) if kind == "JPG" else render_raw(path, None, options)
    with Image.open(io.BytesIO(data)) as exported:
        assert exported.getexif()[33432] == original
    finished = write_with_exif(data, path, "edited", quality=90, fmt="jpeg")
    with Image.open(io.BytesIO(finished)) as exported:
        assert exported.getexif()[33432] == original


def test_copyright_survives_the_reduced_exif_fallback(tmp_path, monkeypatch):
    path = sample(tmp_path / "photo.JPG", TAG)
    save = Image.Image.save
    attempts = []

    def fail_full(self, fp, *args, **kwargs):
        attempts.append(33432 in kwargs["exif"])
        if len(attempts) == 1:
            raise ValueError("synthetic full EXIF encoder failure")
        return save(self, fp, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", fail_full)
    data, _ = render(path, None, ExportOptions(None, 90))
    with Image.open(io.BytesIO(data)) as exported:
        assert exported.getexif()[33432] == TAG
    assert attempts == [True, True]


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")
def test_info_panel_displays_recipe_fallback_copyright_and_tweaks(tmp_path):
    modules = tmp_path / "modules"
    modules.mkdir()
    for name in ("info_panel.js", "dom.js"):
        shutil.copyfile(ROOT / "web/assets" / name, modules / name)
    (modules / "package.json").write_text('{"type":"module"}\n')
    (modules / "caps.js").write_text("export const loadMeta = () => Promise.resolve({state:'ok', data:{lens:'A12'}});\n")
    (modules / "preview.js").write_text("export const slotName=()=>'电脑配方'; export const recipeInfo=()=>null; export const KNOB_NAMES={};\n")
    (modules / "controls.js").write_text("export const fmtSigned=String;\n")
    (modules / "state.js").write_text("export const state={}; export const on=()=>{};\n")
    tags = [TAG, TAG + " *", UNKNOWN, "GXR1 0123456789 50 01", "GXR1 6a3a5a3c02 MT 22 Ac", "Alice <b>copyright</b>", None, "GXR1 bad"]
    photos = [
        {"id": str(i), "name": "R0000001", "has_jpg": True, "recipe_tag": recipe_payload(parse_recipe_tag(t)), "copyright": t}
        for i, t in enumerate(tags)
    ]
    data = tmp_path / "photos.json"
    data.write_text(json.dumps(photos))
    driver = ROOT / "tests/support/recipe_tags.mjs"
    result = subprocess.run(["node", str(driver), str(modules), str(data)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    assert rows[0]["recipe"] == "配方F-CN（28mm · 06）"
    assert rows[1]["recipe"] == "配方F-CN（28mm · 06）已微调"
    assert rows[2]["recipe"] == "配方Camera name（MOUNT · 99）（相机记录，Studio 目录里没有）"
    assert rows[3]["recipe"] == "配方GXR1 0123456789 50 01（相机记录，Studio 目录里没有）"
    assert rows[4]["recipe"] == "配方Acros（MOUNT · 22）"
    for i in (5, 6, 7):
        assert rows[i]["recipe"] == "配方未标记"
    assert "版权Alice <b>copyright</b>" in rows[5]["shot"]
    assert rows[5]["htmlElements"] == 0
    assert "版权" not in rows[6]["shot"] and "版权GXR1 bad" in rows[7]["shot"]
    assert all(row["stable"] for row in rows)
