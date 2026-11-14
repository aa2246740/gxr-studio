"""「导出给 iPhone」：校准数据打成不压缩的 zip，放进导出文件夹。

全部用临时的应用数据目录和导出文件夹；校准包是合成的（tests/support/mobile_ui_calib.py）。
"""

from __future__ import annotations

import hashlib
import json
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import PORT
from gxr_studio.app import create_app
from gxr_studio.camdata import calib, calib_export
from support.color_fixtures import entry
from support.mobile_ui_calib import pack_files, write_pack

STOCK = {"jlaunch": entry(words=0), "qlaunch": entry(words=1), "l08firm": entry(words=2)}
NOON = 1763175600.0  # 2025-11-15 12:00 +09:00 左右；文件名里只用本地日期


def _pack(home: Path) -> dict[str, bytes]:
    files = pack_files(STOCK)
    write_pack(calib.calib_dir(home), files)
    return files


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        yield c


def test_the_zip_is_stored_named_by_date_and_holds_exactly_the_pack(tmp_path: Path) -> None:
    home = tmp_path / "app"
    files = _pack(home)
    dest = tmp_path / "out"
    path = calib_export.export_for_iphone(home, dest, [], now=NOON)
    day = time.strftime("%Y-%m-%d", time.localtime(NOON))
    assert path == dest / f"GXR 校准数据 {day}.zip"
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        assert [i.filename for i in infos][0] == "manifest.json"
        assert sorted(i.filename for i in infos) == sorted(files)
        assert all(i.compress_type == zipfile.ZIP_STORED for i in infos)
        for info in infos:
            assert archive.read(info) == files[info.filename]
    again = calib_export.export_for_iphone(home, dest, [], now=NOON)
    assert again.name == f"GXR 校准数据 {day}-2.zip" and path.is_file()


def test_missing_pack_is_refused_without_writing(tmp_path: Path) -> None:
    dest = tmp_path / "out"
    with pytest.raises(calib_export.CalibExportError) as err:
        calib_export.export_for_iphone(tmp_path / "app", dest, [])
    assert err.value.code == "calibration_missing" and err.value.message == "缺少校准数据"
    assert not dest.exists() or not any(dest.iterdir())


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        ("changed", "校准数据被改动，请重新导入"),
        ("deleted", "校准数据文件缺失，请重新导入"),
        ("schema", "校准数据版本已更新，请重新导入"),
        ("escape", "校准数据的文件清单不正确"),
        ("model", "校准数据的文件清单不正确"),
        ("garbage", "校准数据读不出来，请重新导入"),
        ("symlink", "校准数据的文件清单不正确"),
    ],
)
def test_invalid_pack_is_refused_with_the_desktop_wording(tmp_path: Path, damage: str, message: str) -> None:
    home = tmp_path / "app"
    files = _pack(home)
    folder = calib.calib_dir(home)
    manifest = json.loads(files["manifest.json"])
    if damage == "changed":
        (folder / "ctx_jlaunch_200.npz").write_bytes(b"x" + files["ctx_jlaunch_200.npz"][1:])
    elif damage == "deleted":
        (folder / "ring_qlaunch_1600.json").unlink()
    elif damage == "schema":
        manifest["schema"] = "gxr-studio/calib/1"
    elif damage == "escape":
        manifest["files"]["../secret"] = {"size": 1, "sha256": "0" * 64}
    elif damage == "model":
        manifest["units"]["jlaunch"]["models"]["1600"]["ctx"] = "nope.npz"
    elif damage == "garbage":
        (folder / "manifest.json").write_bytes(b"\xff\xfe not json")
    elif damage == "symlink":
        target = tmp_path / "elsewhere.npz"
        target.write_bytes(files["ctx_l08firm_200.npz"])
        (folder / "ctx_l08firm_200.npz").unlink()
        (folder / "ctx_l08firm_200.npz").symlink_to(target)
    if damage in ("schema", "escape", "model"):
        (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    dest = tmp_path / "out"
    with pytest.raises(calib_export.CalibExportError) as err:
        calib_export.export_for_iphone(home, dest, [])
    assert err.value.message == message
    assert not dest.exists() or not any(dest.iterdir())


def test_forbidden_and_repository_destinations_are_refused(tmp_path: Path) -> None:
    home = tmp_path / "app"
    _pack(home)
    card = tmp_path / "volumes" / "CARD"
    card.mkdir(parents=True)
    with pytest.raises(calib_export.CalibExportError) as err:
        calib_export.export_for_iphone(home, card / "DCIM", [card])
    assert err.value.code == "bad_destination" and err.value.message == "无法导出到这个位置。请换一个文件夹。"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(calib_export.CalibExportError) as err:
        calib_export.export_for_iphone(home, repo / "out", [])
    assert err.value.code == "bad_destination"
    with pytest.raises(calib_export.CalibExportError) as err:
        calib_export.export_for_iphone(home, Path("relative/out"), [])
    assert err.value.code == "bad_destination"
    assert not (card / "DCIM").exists() and not (repo / "out").exists()


def test_api_writes_into_the_export_folder_and_can_reveal(client, settings, monkeypatch) -> None:
    files = _pack(settings.home_dir)
    r = client.post("/api/calibration/export-iphone", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    path = Path(body["path"])
    assert path.parent == settings.export_dir.resolve() or path.parent == settings.export_dir
    assert body["name"].startswith("GXR 校准数据 ") and body["name"].endswith(".zip")
    assert body["message"] == "用隔空投送发到 iPhone，存到「文件」里，再在手机上导入。"
    with zipfile.ZipFile(path) as archive:
        got = {i.filename: hashlib.sha256(archive.read(i)).hexdigest() for i in archive.infolist()}
    assert got == {name: hashlib.sha256(blob).hexdigest() for name, blob in files.items()}
    opened: list[list[str]] = []
    monkeypatch.setattr("gxr_studio.export._host_is_macos", lambda: True)
    monkeypatch.setattr("gxr_studio.export.subprocess.run", lambda cmd, **kw: opened.append(cmd))
    shown = client.post(f"/api/exports/{body['export_id']}/reveal")
    assert shown.status_code == 200, shown.text
    assert opened and opened[0][:2] == ["open", "-R"] and opened[0][2].endswith(".zip")


def test_api_refuses_missing_pack_and_card_destination(client, settings, volumes) -> None:
    r = client.post("/api/calibration/export-iphone", json={})
    assert r.status_code == 409 and r.json()["message"] == "缺少校准数据"
    _pack(settings.home_dir)
    r = client.post("/api/calibration/export-iphone", json={"dest_dir": str(volumes / "CARD")})
    assert r.status_code == 422 and r.json()["error"] == "bad_destination"
    r = client.post("/api/calibration/export-iphone", json={"dest_dir": str(settings.library_dir / "x")})
    assert r.status_code == 422 and r.json()["error"] == "bad_destination"
