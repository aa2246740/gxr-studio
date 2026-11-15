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
from gxr_studio import privacy
from gxr_studio.app import create_app
from gxr_studio.camdata import calib, calib_export, store
from support.color_fixtures import entry
from support.mobile_ui_calib import pack_files, write_pack

STOCK = {"jlaunch": entry(words=0), "qlaunch": entry(words=1), "l08firm": entry(words=2)}
NOON = 1763175600.0  # 2025-11-15 12:00 +09:00 左右；文件名里只用本地日期


def _camera(home: Path, stock: dict[str, bytes]) -> None:
    """`model_for` 只从相机数据里读每个单元的原厂 04：测试直接放合成条目。"""
    for unit, blob in stock.items():
        path = store.camera_data_dir(home) / "units" / unit / "stock04.entry"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)


def _pack(home: Path, units: tuple[str, ...] = ("jlaunch", "qlaunch", "l08firm")) -> dict[str, bytes]:
    files = pack_files({unit: STOCK[unit] for unit in units})
    write_pack(calib.calib_dir(home), files)
    _camera(home, STOCK)
    return files


def _relist(home: Path, name: str, blob: bytes) -> None:
    """改一个包里的文件，同时把清单里的大小和 SHA-256 改对（只剩模型本身坏了）。"""
    folder = calib.calib_dir(home)
    (folder / name).write_bytes(blob)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"][name] = {"size": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


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


def test_a_partial_pack_still_exports(tmp_path: Path) -> None:
    home = tmp_path / "app"
    files = _pack(home, ("jlaunch",))
    path = calib_export.export_for_iphone(home, tmp_path / "out", [], now=NOON)
    with zipfile.ZipFile(path) as archive:
        assert sorted(archive.namelist()) == sorted(files) and len(files) == 5


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        ("stock", "校准数据与这个单元的原厂 04 不匹配，请重新导入"),
        ("no-camera", "校准数据文件缺失，请重新导入"),
        ("ring", "校准数据读不出来，请重新导入"),
        ("ctx", "校准数据读不出来，请重新导入"),
    ],
)
def test_a_pack_the_desktop_cannot_use_is_not_exported(tmp_path: Path, damage: str, message: str) -> None:
    """清单和 SHA-256 都对，但 `model_for` 用不了（Sol r1 P2）：照 `unit_status` 的文案拒绝，一个字节都不写。"""
    home = tmp_path / "app"
    _pack(home)
    if damage == "stock":
        _camera(home, {"qlaunch": entry(words=4)})
    elif damage == "no-camera":
        (store.camera_data_dir(home) / "units" / "l08firm" / "stock04.entry").unlink()
    elif damage == "ring":
        _relist(home, "ring_jlaunch_1600.json", b'{"schema": "gxr-sector-model/2"}')
    else:
        _relist(home, "ctx_qlaunch_200.npz", b"PK not an npz")
    dest = tmp_path / "out"
    with pytest.raises(calib_export.CalibExportError) as err:
        calib_export.export_for_iphone(home, dest, [])
    assert err.value.message == message
    assert not dest.exists() or not any(dest.iterdir())


def test_a_destination_swapped_for_a_link_after_the_check_is_not_written(tmp_path: Path, monkeypatch) -> None:
    """检查之后、写之前，选定的目录被换成指向禁止目录的链接（Sol r1 P1）：不写进去，原目录也没有文件。"""
    home = tmp_path / "app"
    _pack(home)
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    banned = tmp_path / "banned"
    (banned / ".git").mkdir(parents=True)
    real_collect = calib_export.collect

    def swap(target: Path):
        out = real_collect(target)
        chosen.rename(tmp_path / "chosen-was")
        chosen.symlink_to(banned, target_is_directory=True)
        return out

    monkeypatch.setattr(calib_export, "collect", swap)
    with pytest.raises(calib_export.CalibExportError) as err:
        calib_export.export_for_iphone(home, chosen, [])
    assert err.value.code == "bad_destination"
    assert sorted(p.name for p in banned.iterdir()) == [".git"]
    assert not any((tmp_path / "chosen-was").iterdir())

    # 链接到禁止列表里的目录（卡根），也一样。
    card = tmp_path / "volumes" / "CARD"
    card.mkdir(parents=True)
    chosen.unlink()
    chosen.mkdir()

    def swap_to_card(target: Path):
        out = real_collect(target)
        chosen.rmdir()
        chosen.symlink_to(card, target_is_directory=True)
        return out

    monkeypatch.setattr(calib_export, "collect", swap_to_card)
    with pytest.raises(calib_export.CalibExportError) as err:
        calib_export.export_for_iphone(home, chosen, [card])
    assert err.value.code == "bad_destination" and not any(card.iterdir())


def test_a_destination_moved_while_writing_is_not_cleaned_up_by_path(tmp_path: Path, monkeypatch) -> None:
    """写的时候整个目录被移进 Git 工作区（Sol r1 P1）：报错；写进去的字节被清空，不按路径去别处删东西。"""
    home = tmp_path / "app"
    _pack(home)
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    real_write = privacy.PrivateDir.write

    def moving(self, rel: str, data: bytes) -> None:
        chosen.rename(repo / "moved")
        real_write(self, rel, data)

    monkeypatch.setattr(privacy.PrivateDir, "write", moving)
    with pytest.raises(calib_export.CalibExportError) as err:
        calib_export.export_for_iphone(home, chosen, [])
    assert err.value.code == "bad_destination"
    left = [p for p in (repo / "moved").iterdir()]
    assert all(p.stat().st_size == 0 for p in left), left
    assert not chosen.exists()


def test_a_destination_swapped_for_a_link_to_another_allowed_folder_is_not_written(tmp_path: Path, monkeypatch) -> None:
    """检查之后把 A 换成指向另一个普通允许目录 B 的链接（Sol r2）：不写进 B，A 原来那个目录里也没有文件。"""
    home = tmp_path / "app"
    _pack(home, ("jlaunch",))
    chosen = tmp_path / "A"
    chosen.mkdir()
    other = tmp_path / "B"
    other.mkdir()
    real_collect = calib_export.collect

    def swap(target: Path):
        out = real_collect(target)
        chosen.rename(tmp_path / "A-was")
        chosen.symlink_to(other, target_is_directory=True)
        return out

    monkeypatch.setattr(calib_export, "collect", swap)
    with pytest.raises(calib_export.CalibExportError) as err:
        calib_export.export_for_iphone(home, chosen, [])
    assert err.value.code == "bad_destination"
    assert not any(other.iterdir()) and not any((tmp_path / "A-was").iterdir())


def test_a_missing_destination_is_created_through_the_pinned_parent(tmp_path: Path, monkeypatch) -> None:
    """目标还不存在：检查之后把它的上级换成指向禁止目录的链接（Sol r2），不在禁止目录里建任何东西；不换时正常建出来。"""
    home = tmp_path / "app"
    _pack(home, ("jlaunch",))
    parent = tmp_path / "parent"
    parent.mkdir()
    banned = tmp_path / "banned"
    (banned / ".git").mkdir(parents=True)
    real_collect = calib_export.collect

    def swap(target: Path):
        out = real_collect(target)
        parent.rename(tmp_path / "parent-was")
        parent.symlink_to(banned, target_is_directory=True)
        return out

    monkeypatch.setattr(calib_export, "collect", swap)
    with pytest.raises(calib_export.CalibExportError) as err:
        calib_export.export_for_iphone(home, parent / "new-export" / "deeper", [])
    assert err.value.code == "bad_destination"
    assert sorted(p.name for p in banned.iterdir()) == [".git"]
    assert not any((tmp_path / "parent-was").iterdir())
    monkeypatch.setattr(calib_export, "collect", real_collect)
    made = calib_export.export_for_iphone(home, tmp_path / "fresh" / "a" / "b", [], now=NOON)
    assert made.parent == (tmp_path / "fresh" / "a" / "b").resolve() and made.is_file()


def test_the_validated_snapshot_is_what_gets_written(tmp_path: Path, monkeypatch) -> None:
    """收集到的是坏模型，收集之后本机的包被换成好的（Sol r2）：校验的是收集到的那份，所以拒绝，不写 zip。
    不换时，zip 里的每个文件就是校验过的那份。"""
    home = tmp_path / "app"
    good = _pack(home, ("jlaunch",))
    _relist(home, "ring_jlaunch_200.json", b'{"schema": "gxr-sector-model/2"}')
    real_collect = calib_export.collect

    def swap(target: Path):
        out = real_collect(target)
        write_pack(calib.calib_dir(home), good)  # 本机的包现在又是好的
        return out

    monkeypatch.setattr(calib_export, "collect", swap)
    dest = tmp_path / "out"
    with pytest.raises(calib_export.CalibExportError) as err:
        calib_export.export_for_iphone(home, dest, [])
    assert err.value.message == "校准数据读不出来，请重新导入"
    assert not dest.exists() or not any(dest.iterdir())

    seen: dict[str, bytes] = {}

    def keep(target: Path):
        manifest, files = real_collect(target)
        seen.update(files)
        return manifest, files

    monkeypatch.setattr(calib_export, "collect", keep)
    path = calib_export.export_for_iphone(home, dest, [], now=NOON)
    with zipfile.ZipFile(path) as archive:
        assert {name: archive.read(name) for name in archive.namelist()} == seen == good
        ring = json.loads(archive.read("ring_jlaunch_200.json"))
    assert "nodes_unwrapped_deg" in ring  # 写进去的模型是好的那份


@pytest.mark.parametrize("missing", [False, True])
def test_a_swap_right_after_the_first_check_cannot_redirect(tmp_path: Path, monkeypatch, missing: bool) -> None:
    """Sol r3：第一次检查刚通过、句柄还没打开时，把目标（或缺失目标的上级）换成指向另一个允许目录 B 的链接：
    句柄打不开，拒绝；B 里什么都没有，原来那个目录也没有文件。"""
    home = tmp_path / "app"
    _pack(home, ("jlaunch",))
    chosen = tmp_path / "A"
    chosen.mkdir()
    other = tmp_path / "B"
    other.mkdir()
    real_check = privacy.check_private_dir
    swapped: list[bool] = []

    def check_then_swap(path, purpose, forbidden=()):
        out = real_check(path, purpose, forbidden)
        if purpose == "calibration_export" and not swapped:
            swapped.append(True)
            chosen.rename(tmp_path / "A-was")
            chosen.symlink_to(other, target_is_directory=True)
        return out

    monkeypatch.setattr(privacy, "check_private_dir", check_then_swap)
    target = chosen / "new-export" if missing else chosen
    with pytest.raises(calib_export.CalibExportError) as err:
        calib_export.export_for_iphone(home, target, [])
    assert swapped and err.value.code == "bad_destination"
    assert not any(other.iterdir()) and not any((tmp_path / "A-was").iterdir())
