"""导入相机数据/标定包的接口：从卡（只读）、从本地文件、从标定包文件夹；缓存失效；写卡次数为 0。

理光字节不在仓库里：DAT 现场从本机固件打包目录复制到临时目录（找不到就跳过依赖它的测试），
卡是临时目录里的合成卡，测试进程禁止碰 /Volumes 下的真实卷（conftest）。
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import struct
import zlib
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import e0_support as t
from conftest import PORT
from gxr_studio import audit
from gxr_studio.app import create_app
from gxr_studio.camdata import calib, intake, store
from gxr_studio.cards.discovery import CardReader
from test_api import find, import_all

GENS = (127, 129, 131, 135)
ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DIR = ROOT / "captures/a12-50/2026-10-19/stage2c-first-run"
needs_sample = pytest.mark.skipif(not SAMPLE_DIR.is_dir(), reason="captures/a12-50 样本不在本机")


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def dat(gen: int = 127) -> bytes:
    raw = t.fixture_dat(gen)
    if raw is None:
        pytest.skip(f"g{gen} 夹具 DAT 不在本机")
    return raw


@pytest.fixture(scope="module")
def dev_sources():
    src = calib.dev_sources(t.REPO)
    if not src:
        pytest.skip("研究目录里的标定文件不在本机")
    return src


@pytest.fixture
def reader():
    return AuditedReader()


class AuditedReader(CardReader):
    """记下每一次对卡的打开；只允许 rb。"""

    def __init__(self):
        self.opened: list[str] = []

    def open_rb(self, path, **kwargs):
        self.opened.append(str(path))
        return super().open_rb(path, **kwargs)


@pytest.fixture
def client(settings, reader):
    app = create_app(settings, reader=reader)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        yield c


def card_with_dat(volumes: Path, label: str, raw: bytes, *, dcim: bool = True, name: str = "GXRCE.DAT") -> Path:
    root = volumes / label
    root.mkdir(parents=True, exist_ok=True)
    if dcim:
        (root / "DCIM" / "100RICOH").mkdir(parents=True, exist_ok=True)
    (root / name).write_bytes(raw)
    return root


def swap_dat(root: Path, data: bytes) -> None:
    """模拟用户换了一张卡/换了卡上的文件：测试自己写卡前先解除保护（应用下次发现这张卡时会重新保护）。"""
    audit.unprotect(root)
    (root / "GXRCE.DAT").write_bytes(data)


def snapshot(root: Path) -> dict:
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns, sha(p.read_bytes()) if p.is_file() else None)
        for p in sorted(root.rglob("*"))
    }


def forge_generation(raw: bytes, gen: int) -> bytes:
    b = bytearray(raw)
    struct.pack_into(">I", b, 28, gen)
    struct.pack_into(">I", b, 24, zlib.crc32(b[256:]) & 0xFFFFFFFF)
    struct.pack_into(">I", b, 20, 0)
    struct.pack_into(">I", b, 20, zlib.crc32(b[:256]) & 0xFFFFFFFF)
    return bytes(b)


def tree(root: Path) -> dict:
    return {str(p.relative_to(root)): sha(p.read_bytes()) for p in sorted(root.rglob("*")) if p.is_file()}


# ---- 状态 -----------------------------------------------------------------------


def test_status_before_any_import(client, volumes):
    s = client.get("/api/camdata/status").json()
    assert s["ready"] is False and s["next"] == "import_camera_data" and s["hint"]
    cam = s["camera_data"]
    assert cam["state"] == "missing" and cam["imported"] is False and cam["units"] is None and cam["all_units_complete"] is False
    assert s["calibration"]["state"] == "missing" and s["calibration"]["imported"] is False
    assert s["cards"] == [] and s["data_version"] == "none-none"


def test_status_lists_which_inserted_cards_have_the_dat(client, volumes):
    card_with_dat(volumes, "RICOHDCX", dat(), name="gxrce.dat")
    (volumes / "PHOTOS" / "DCIM").mkdir(parents=True)
    (volumes / "Not a card").mkdir()
    cards = {c["label"]: c for c in client.get("/api/camdata/status").json()["cards"]}
    assert set(cards) == {"RICOHDCX", "PHOTOS"}
    assert cards["RICOHDCX"]["has_dat"] and cards["RICOHDCX"]["dat_size"] == len(dat())
    assert not cards["PHOTOS"]["has_dat"] and cards["PHOTOS"]["dat_size"] is None


def test_endpoints_need_the_token(settings):
    app = create_app(settings)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as c:
        for method, url in (
            ("get", "/api/camdata/status"),
            ("post", "/api/camdata/import-card"),
            ("post", "/api/camdata/import-file"),
            ("post", "/api/camdata/import-upload"),
            ("post", "/api/calibration/import"),
        ):
            assert getattr(c, method)(url).status_code in (401, 403), url


# ---- 从卡导入 --------------------------------------------------------------------


@pytest.mark.parametrize("gen", GENS)
def test_import_from_card_is_read_only_and_extracts_everything(client, volumes, settings, reader, gen):
    raw = dat(gen)
    root = card_with_dat(volumes, "RICOHDCX", raw)
    (root / "DCIM" / "100RICOH" / "R0000001.JPG").write_bytes(b"not really a jpeg")
    before = snapshot(root)
    audit_before = client.get("/api/audit").json()
    with audit.AuditRecorder(watch=(str(volumes), str(volumes.resolve()))) as rec:
        r = client.post("/api/camdata/import-card")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] is True and body["unchanged"] is False and body["generation"] == gen and body["sha256"] == sha(raw)
    assert "RICOHDCX" in body["message"] and "校准数据" in body["message"]
    # 对卡：零写入、零改动、只以只读方式打开了 GXRCE.DAT 这一个文件
    assert rec.writes_under(volumes) == []
    assert snapshot(root) == before
    assert reader.opened == [str(root / "GXRCE.DAT")]
    # 应用数据目录里：3 个单元 × 8 个配方 + 原厂 04 + 模板
    home = settings.home_dir
    assert store.verify(home)["generation"] == gen
    assert store.load_template(home) == raw
    cam = body["status"]["camera_data"]
    assert cam["state"] == "ready" and cam["imported"] and cam["all_units_complete"] and cam["generation"] == gen
    assert [u["id"] for u in cam["units"]] == ["jlaunch", "qlaunch", "l08firm"]
    assert all(u["slots"] == list(range(1, 9)) and u["stock04"] for u in cam["units"])
    assert cam["source"] == {
        "name": "GXRCE.DAT",
        "size": len(raw),
        "sha256": sha(raw),
        "origin": {"kind": "card", "label": "RICOHDCX"},
    }
    assert body["status"]["next"] == "import_calibration" and body["status"]["calibration"]["state"] == "missing"
    # 配方列表现在带条目 SHA-256
    recipes = client.get("/api/recipes").json()
    assert recipes["units"][0]["recipes"][0]["entry_sha256"]
    # 应用自己的审计钩子：这次请求的写入只落在应用数据目录/图库，没有一条在卡上
    audit_after = client.get("/api/audit").json()
    new = audit_after["recent"][-(audit_after["write_count"] - audit_before["write_count"]) :]
    assert new and not [w for w in new if str(volumes) in w["path"] or str(volumes.resolve()) in w["path"]]
    # 按目录 fd 写入：审计记的是目录 fd 的真实路径（macOS 的 /var 会解析成 /private/var）
    assert any(str(settings.home_dir) in w["path"] or str(settings.home_dir.resolve()) in w["path"] for w in new)


def test_import_from_a_card_without_dcim_and_with_lowercase_name(client, volumes, settings):
    raw = dat()
    card_with_dat(volumes, "FLASHCARD", raw, dcim=False, name="gxrce.dat")
    assert client.get("/api/cards").json() == []  # 不是照片卡，图库导入不列它
    r = client.post("/api/camdata/import-card")
    assert r.status_code == 200 and r.json()["status"]["camera_data"]["source"]["name"] == "gxrce.dat"
    assert store.load_template(settings.home_dir) == raw


def test_importing_the_same_dat_again_changes_nothing(client, volumes, settings):
    raw = dat()
    card_with_dat(volumes, "RICOHDCX", raw)
    assert client.post("/api/camdata/import-card").json()["imported"] is True
    first = tree(settings.home_dir / "camera-data")
    manifest = store.read_manifest(settings.home_dir)
    svc = client.app.state.ctx.render
    svc._results.put(("k", 88), (b"x", 1, 1), 1)  # 假装有渲染结果缓存
    r = client.post("/api/camdata/import-card").json()
    assert r["imported"] is False and r["unchanged"] is True and "没有改动" in r["message"]
    assert r["invalidated"] == {"render_cache": False, "thumbnails_removed": 0}
    assert tree(settings.home_dir / "camera-data") == first
    assert store.read_manifest(settings.home_dir)["imported_at"] == manifest["imported_at"]
    assert len(svc._results) == 1  # 没动就不清缓存


def test_no_card_inserted(client, volumes):
    r = client.post("/api/camdata/import-card")
    assert r.status_code == 404 and r.json()["error"] == "no_card" and r.json()["message"] == "找不到存储卡。请重新插入后重试。"


def test_card_without_the_dat_file(client, card, settings):
    r = client.post("/api/camdata/import-card")
    assert r.status_code == 404 and r.json()["error"] == "dat_not_found"
    assert r.json()["message"] == "存储卡上没有相机配方文件（GXRCE.DAT）"
    assert not (settings.home_dir / "camera-data").exists()


def test_two_cards_with_the_dat_need_a_choice(client, volumes, settings):
    a, b = dat(127), dat(135)
    card_with_dat(volumes, "CARD-A", a)
    card_with_dat(volumes, "CARD-B", b)
    r = client.post("/api/camdata/import-card")
    assert r.status_code == 409 and r.json()["error"] == "multiple_cards"
    ids = {c["label"]: c["id"] for c in r.json()["cards"]}
    assert set(ids) == {"CARD-A", "CARD-B"} and not (settings.home_dir / "camera-data").exists()
    ok = client.post("/api/camdata/import-card", json={"card_id": ids["CARD-B"]})
    assert ok.status_code == 200 and ok.json()["generation"] == 135
    assert client.post("/api/camdata/import-card", json={"card_id": "../../etc"}).json()["error"] == "bad_card_id"
    assert client.post("/api/camdata/import-card", json={"card_id": "0" * 26}).json()["error"] == "card_not_found"


def test_card_id_of_a_card_without_the_dat(client, volumes, card):
    card_with_dat(volumes, "RICOHDCX", dat())
    other = next(c for c in client.get("/api/cards").json() if c["label"] == "NO NAME")
    r = client.post("/api/camdata/import-card", json={"card_id": other["id"]})
    assert r.status_code == 404 and r.json()["error"] == "dat_not_found"


def test_card_read_error_imports_nothing(settings, volumes):
    class Flaky(CardReader):
        def open_rb(self, path, **kwargs):
            raise OSError(5, "Input/output error")

    card_with_dat(volumes, "RICOHDCX", dat())
    app = create_app(settings, reader=Flaky())
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        r = c.post("/api/camdata/import-card")
    assert r.status_code == 409 and r.json()["error"] == "card_read_failed"
    assert not (settings.home_dir / "camera-data").exists()


# ---- 坏文件：代数不支持 / 损坏 ---------------------------------------------------------


def bad_variants():
    raw = dat()
    flipped = bytearray(raw)
    flipped[1000] ^= 1
    return {
        "generation_unsupported": ("generation_unsupported", forge_generation(raw, 130)),
        "generation_zero": ("generation_unsupported", forge_generation(raw, 0)),
        "bit_flip": ("dat_invalid", bytes(flipped)),
        "truncated": ("dat_invalid", raw[:-8]),
        "header_only": ("dat_invalid", raw[:256]),
        "not_a_dat": ("dat_invalid", b"hello, this is not a recipe program"),
        "empty": ("dat_invalid", b""),
        "too_big": ("dat_invalid", raw + b"\0" * 100_000),
    }


@pytest.mark.parametrize(
    "name",
    [
        "generation_unsupported",
        "generation_zero",
        "bit_flip",
        "truncated",
        "header_only",
        "not_a_dat",
        "empty",
        "too_big",
    ],
)
def test_bad_dat_from_card_is_refused_and_old_data_is_kept(client, volumes, settings, name):
    code, bad = bad_variants()[name]
    root = card_with_dat(volumes, "RICOHDCX", dat(127))
    assert client.post("/api/camdata/import-card").status_code == 200
    before = tree(settings.home_dir)
    swap_dat(root, bad)
    snap = snapshot(root)
    r = client.post("/api/camdata/import-card")
    assert r.status_code == 422 and r.json()["error"] == code, r.text
    message = r.json()["message"]
    if name == "generation_unsupported":
        assert message == "不支持这个版本的相机配方文件。"
        assert r.json()["detail"] == "第 130 代。支持第 127、129、131、135、137、138、150、152、153、154、155、156、158 代。"
    elif name == "generation_zero":
        assert message == "不支持这个版本的相机配方文件。"
        assert r.json()["detail"] == "第 0 代。支持第 127、129、131、135、137、138、150、152、153、154、155、156、158 代。"
    else:
        assert "无法导入" in message
    assert tree(settings.home_dir) == before  # 旧数据原封不动，也没有 .new 残留
    assert snapshot(root) == snap  # 卡上也没动
    s = client.get("/api/camdata/status").json()["camera_data"]
    assert s["source"]["sha256"] == sha(dat(127)) and s["generation"] == 127


def test_bad_dat_messages_are_specific(client, volumes):
    raw = dat()
    root = card_with_dat(volumes, "RICOHDCX", forge_generation(raw, 130))
    m = client.post("/api/camdata/import-card").json()["message"]
    assert m == "不支持这个版本的相机配方文件。"
    swap_dat(root, b"x" * 300)
    assert "不是 GXRCE.DAT" in client.post("/api/camdata/import-card").json()["message"]
    flipped = bytearray(raw)
    flipped[5000] ^= 0x10
    swap_dat(root, bytes(flipped))
    flipped_body = client.post("/api/camdata/import-card").json()
    assert "无法导入" in flipped_body["message"] and flipped_body.get("detail")
    swap_dat(root, raw + b"\0" * 100_000)
    assert "文件过大" in client.post("/api/camdata/import-card").json()["message"]


@pytest.mark.parametrize(
    ("generation", "message"),
    [
        (126, "不支持这个版本的相机配方文件。"),
        (139, "不支持这个版本的相机配方文件。"),
        (151, "不支持这个版本的相机配方文件。"),
        (157, "不支持这个版本的相机配方文件。"),
        (159, "这个相机配方文件需要更新版本的 GXR 工作室。"),
        (160, "这个相机配方文件需要更新版本的 GXR 工作室。"),
        (161, "这个相机配方文件需要更新版本的 GXR 工作室。"),
        (200, "这个相机配方文件需要更新版本的 GXR 工作室。"),
    ],
    ids=("g126", "g139", "g151", "g157", "g159", "g160", "g161", "g200"),
)
def test_unsupported_generation_copy_keeps_numbers_in_detail(client, generation, message):
    response = client.post("/api/camdata/import-upload", content=forge_generation(dat(), generation))
    assert response.status_code == 422
    body = response.json()
    assert body == {
        "error": "generation_unsupported",
        "message": message,
        "detail": f"第 {generation} 代。支持第 127、129、131、135、137、138、150、152、153、154、155、156、158 代。",
    }
    assert not any(character.isdigit() for character in body["message"])
    camera = client.get("/api/camdata/status").json()["camera_data"]
    assert camera["message"] == message and camera["detail"] == body["detail"]
    assert camera["generation"] is None and "supported_generations" not in camera


def test_a_dat_whose_04_is_not_the_factory_one_is_refused(monkeypatch):
    """标定要靠出厂 04：三个单元的 04 对不上登记的出厂 CRC 就不收（这里把登记值换掉来模拟）。"""
    from gxr_studio.camdata import datcodec as dc

    raw = dat()
    assert intake.inspect_dat(raw).generation == 127
    monkeypatch.setattr(dc, "STOCK_ENTRY_CRC", (0, 0, 0))
    with pytest.raises(intake.IntakeError) as ei:
        intake.inspect_dat(raw)
    assert ei.value.code == "dat_invalid" and "原厂 04" in ei.value.extra.get("detail", "")


# ---- 从本地文件导入 ----------------------------------------------------------------


def test_import_from_a_local_file(client, tmp_path, settings):
    raw = dat(131)
    p = tmp_path / "downloads" / "my copy.dat"  # 名字不一定叫 GXRCE.DAT
    p.parent.mkdir()
    p.write_bytes(raw)
    r = client.post("/api/camdata/import-file", json={"path": str(p)})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["generation"] == 131 and b["status"]["camera_data"]["source"] == {
        "name": "my copy.dat",
        "size": len(raw),
        "sha256": sha(raw),
        "origin": {"kind": "file"},
    }
    assert str(p.parent) not in json.dumps(b)  # 不把本地路径写进记录
    assert str(p.parent) not in (settings.home_dir / "camera-data/manifest.json").read_text(encoding="utf-8")
    assert store.load_template(settings.home_dir) == raw


def test_import_upload_takes_the_raw_bytes_the_browser_picked(client, settings):
    """界面的文件选择器给不出路径：原始字节直接 POST，文件名只用来显示（不带目录）。"""
    raw = dat(131)
    r = client.post(
        "/api/camdata/import-upload",
        content=raw,
        headers={"Content-Type": "application/octet-stream", "X-GXR-Filename": "/Users/someone/Downloads/my copy.dat"},
    )
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["generation"] == 131 and b["imported"] is True
    assert b["status"]["camera_data"]["source"] == {
        "name": "my copy.dat",
        "size": len(raw),
        "sha256": sha(raw),
        "origin": {"kind": "file"},
    }
    assert "/Users/someone" not in (settings.home_dir / "camera-data/manifest.json").read_text(encoding="utf-8")
    assert store.load_template(settings.home_dir) == raw
    again = client.post("/api/camdata/import-upload", content=raw, headers={"Content-Type": "application/octet-stream"})
    assert again.status_code == 200 and again.json()["unchanged"] is True


def test_import_upload_refuses_bad_bytes_and_keeps_old_data(client, settings):
    raw = dat(127)
    assert client.post("/api/camdata/import-upload", content=raw).status_code == 200
    kept = store.load_template(settings.home_dir)
    for body, code in (
        (b"", "dat_invalid"),
        (b"hello" * 10, "dat_invalid"),
        (b"\0" * (intake.MAX_DAT_BYTES + 1), "dat_invalid"),
    ):
        r = client.post("/api/camdata/import-upload", content=body)
        assert (r.status_code, r.json()["error"]) == (422, code), r.text
    assert store.load_template(settings.home_dir) == kept == raw


def test_import_from_a_folder_that_holds_the_dat(client, tmp_path):
    folder = tmp_path / "card-copy"
    folder.mkdir()
    (folder / "GXRCE.DAT").write_bytes(dat())
    assert client.post("/api/camdata/import-file", json={"path": str(folder)}).status_code == 200


def test_local_file_errors(client, tmp_path, settings):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    cases = [
        ({"path": "relative/GXRCE.DAT"}, 422, "bad_path"),
        ({"path": str(tmp_path / "nope.dat")}, 404, "file_not_found"),
        ({"path": str(empty_dir)}, 404, "dat_not_found"),
    ]
    for body, status, code in cases:
        r = client.post("/api/camdata/import-file", json=body)
        assert (r.status_code, r.json()["error"]) == (status, code), r.text
    assert client.post("/api/camdata/import-file", json={}).status_code == 422
    for name, (code, bad) in bad_variants().items():
        p = tmp_path / f"{name}.dat"
        p.write_bytes(bad)
        r = client.post("/api/camdata/import-file", json={"path": str(p)})
        assert (r.status_code, r.json()["error"]) == (422, code), (name, r.text)
    assert not (settings.home_dir / "camera-data").exists()


# ---- 标定包 ---------------------------------------------------------------------


@pytest.fixture
def with_camdata(client, tmp_path):
    p = tmp_path / "GXRCE.DAT"
    p.write_bytes(dat())
    assert client.post("/api/camdata/import-file", json={"path": str(p)}).status_code == 200


@pytest.fixture
def pack(tmp_path, dev_sources):
    folder = tmp_path / "calib-pack"
    intake.write_pack(folder, dev_sources)
    return folder


def test_calibration_needs_camera_data_first(client, pack, settings):
    r = client.post("/api/calibration/import", json={"path": str(pack)})
    assert r.status_code == 409 and r.json()["error"] == "camera_data_missing"
    assert not (settings.home_dir / "calib").exists()


def test_import_calibration_pack_makes_the_renderer_ready(client, with_camdata, pack, settings):
    assert client.get("/api/render/status").json()["ready"] is False
    before = client.get("/api/camdata/status").json()["data_version"]
    r = client.post("/api/calibration/import", json={"path": str(pack)})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["kind"] == "pack" and b["status"]["ready"] is True and b["status"]["next"] is None
    cal = b["status"]["calibration"]
    assert cal["state"] == "ready" and cal["imported"] and cal["imported_isos"] == [200, 1600] and cal["origin"]["kind"] == "pack"
    assert b["status"]["data_version"] != before
    assert client.get("/api/render/status").json()["ready"] is True
    home = settings.home_dir
    assert sorted(p.name for p in home.iterdir()) == ["calib", "camera-data"]  # 没有临时目录残留
    m = json.loads((home / "calib/manifest.json").read_text(encoding="utf-8"))
    assert m["units"]["jlaunch"]["camera_stock_sha256"] == calib.stock_fingerprint(store.load_stock04(home, "jlaunch"))
    with np.load(home / "calib/ctx_jlaunch_200.npz", allow_pickle=False) as z:
        assert sorted(z.files) == ["d0", "d1", "input_coefficients"]


@pytest.mark.parametrize("target", ["root", "research"])
def test_dev_import_from_the_research_tree(client, with_camdata, dev_sources, target):
    path = t.REPO if target == "root" else t.REPO / "research"
    r = client.post("/api/calibration/import", json={"path": str(path)})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "dev" and r.json()["status"]["ready"] is True and "开发机" in r.json()["message"]


def test_tampered_pack_file_is_refused_and_old_calibration_stays(client, with_camdata, pack, settings, tmp_path):
    assert client.post("/api/calibration/import", json={"path": str(pack)}).status_code == 200
    before = tree(settings.home_dir / "calib")
    bad = tmp_path / "bad-pack"
    shutil.copytree(pack, bad)
    f = bad / "ring_jlaunch_200.json"
    f.write_text(f.read_text(encoding="utf-8").replace("0.99", "0.98", 1) + " ", encoding="utf-8")
    r = client.post("/api/calibration/import", json={"path": str(bad)})
    assert r.status_code == 422 and r.json()["error"] == "calibration_pack_invalid"
    assert r.json()["message"] == "校准数据与相机配方文件不匹配" and "SHA-256" in r.json()["detail"]
    assert tree(settings.home_dir / "calib") == before
    assert sorted(p.name for p in settings.home_dir.iterdir()) == ["calib", "camera-data"]
    assert client.get("/api/camdata/status").json()["ready"] is True


def test_pack_with_a_different_stock_is_refused(client, with_camdata, dev_sources, tmp_path, settings):
    with np.load(io.BytesIO(dev_sources["jlaunch"][200][0][1]), allow_pickle=False) as z:
        arrays = {k: z[k] for k in z.files}
    arrays["stock"] = arrays["stock"].copy()
    arrays["stock"][3, 0] += 1
    forged = tmp_path / "data_jlaunch_200.npz"
    np.savez(forged, **arrays)
    folder = tmp_path / "forged-pack"
    intake.write_pack(
        folder, {"jlaunch": {200: (forged, dev_sources["jlaunch"][200][1]), 1600: dev_sources["jlaunch"][1600]}}
    )  # 清单 SHA 是对的，内容是错的
    r = client.post("/api/calibration/import", json={"path": str(folder)})
    assert r.status_code == 422 and r.json()["error"] == "calibration_stock_mismatch", r.text
    assert sorted(p.name for p in settings.home_dir.iterdir()) == ["camera-data"]
    assert client.get("/api/camdata/status").json()["calibration"]["state"] == "missing"


def test_malformed_packs(client, with_camdata, pack, tmp_path, settings):
    def variant(name, edit):
        d = tmp_path / name
        shutil.copytree(pack, d)
        m = json.loads((d / "calib-pack.json").read_text(encoding="utf-8"))
        edit(m, d)
        (d / "calib-pack.json").write_text(json.dumps(m), encoding="utf-8")
        return d

    def drop_1600(m, d):
        del m["units"]["jlaunch"]["1600"]

    def traversal(m, d):
        m["units"]["jlaunch"]["200"]["npz"] = "../data_200.npz"

    def schema(m, d):
        m["schema"] = "something/else"

    def no_hash(m, d):
        m["files"]["data_jlaunch_200.npz"] = {"size": 1}

    def missing_file(m, d):
        (d / "data_jlaunch_1600.npz").unlink()

    def wrong_size(m, d):
        m["files"]["data_jlaunch_200.npz"]["size"] += 1

    cases = {
        n: variant(n, f)
        for n, f in (
            ("drop_1600", drop_1600),
            ("traversal", traversal),
            ("schema", schema),
            ("no_hash", no_hash),
            ("missing_file", missing_file),
            ("wrong_size", wrong_size),
        )
    }
    bad_json = tmp_path / "bad_json"
    shutil.copytree(pack, bad_json)
    (bad_json / "calib-pack.json").write_text("{not json", encoding="utf-8")
    cases["bad_json"] = bad_json
    plain = tmp_path / "plain-folder"
    plain.mkdir()
    cases["no_manifest"] = plain
    for name, d in cases.items():
        r = client.post("/api/calibration/import", json={"path": str(d)})
        assert (r.status_code, r.json()["error"]) == (422, "calibration_pack_invalid"), (name, r.text)
    assert sorted(p.name for p in settings.home_dir.iterdir()) == ["camera-data"]
    missing = client.post("/api/calibration/import", json={"path": str(plain)}).json()
    assert missing["message"] == "校准数据与相机配方文件不匹配" and "calib-pack.json" in missing["detail"]
    for body, status, code in (
        ({"path": "relative"}, 422, "bad_path"),
        ({"path": str(tmp_path / "nope")}, 404, "folder_not_found"),
    ):
        r = client.post("/api/calibration/import", json=body)
        assert (r.status_code, r.json()["error"]) == (status, code)


def test_calibration_for_another_stock_04_is_flagged_after_camera_data_changes(client, with_camdata, pack, settings):
    """相机数据的原厂 04 换了而标定包是对旧的核对的：状态里标出来，不静默继续预测。"""
    assert client.post("/api/calibration/import", json={"path": str(pack)}).status_code == 200
    mp = settings.home_dir / "calib/manifest.json"
    m = json.loads(mp.read_text(encoding="utf-8"))
    for unit in m["units"].values():
        unit["camera_stock_sha256"] = "0" * 64
    mp.write_text(json.dumps(m, indent=2), encoding="utf-8")
    s = client.get("/api/camdata/status").json()
    assert s["ready"] is False and s["calibration"]["state"] == "invalid"
    assert "不匹配" in s["calibration"]["message"]
    assert s["next"] == "import_calibration"
    assert client.post("/api/calibration/import", json={"path": str(pack)}).json()["status"]["ready"] is True


# ---- 缓存失效 --------------------------------------------------------------------


def test_import_clears_the_render_caches(client, with_camdata, pack, tmp_path):
    svc = client.app.state.ctx.render
    svc._results.put(("k", 88), (b"x", 1, 1), 1)
    svc._bakes.put(("b",), object(), 1)
    p = tmp_path / "g135.dat"
    p.write_bytes(dat(135))
    v0 = client.get("/api/camdata/status").json()["data_version"]
    assert client.post("/api/camdata/import-file", json={"path": str(p)}).json()["invalidated"]["render_cache"] is True
    assert len(svc._results) == 0 and len(svc._bakes) == 0
    assert client.get("/api/camdata/status").json()["camera_data"]["generation"] == 135
    svc._results.put(("k", 88), (b"x", 1, 1), 1)
    assert client.post("/api/calibration/import", json={"path": str(pack)}).status_code == 200
    assert len(svc._results) == 0
    assert client.get("/api/camdata/status").json()["data_version"] not in (v0, "none-none")


def test_jpg_photos_keep_plain_urls(client, card):
    import_all(client)
    photos = client.get("/api/photos").json()
    assert photos and all("?" not in p["thumb_url"] for p in photos if p["has_jpg"])


@needs_sample
def test_import_redraws_thumbnails_and_previews_of_dng_only_photos(client, real_card, settings, pack, tmp_path):
    (real_card.root / "DCIM" / "100RICOH" / "R8051145.JPG").unlink()
    import_all(client)
    p = find(client, "R8051145")
    assert p["has_raw"] and not p["has_jpg"] and p["thumb_url"].endswith("?v=none-none")
    # 还没有相机数据：渲染 409，缩略图是中性转换
    assert client.get(f"/api/photos/{p['id']}/render").json()["error"] == "camera_data_missing"
    neutral = client.get(p["thumb_url"]).content
    thumbs_dir = Path(settings.library_dir) / ".studio/thumbs"
    assert any(x.name.endswith("-256-n.jpg") for x in thumbs_dir.rglob("*.jpg"))
    # 导入相机数据（还没有标定包）：缩略图仍是中性转换，但链接版本变了
    card_dat = tmp_path / "GXRCE.DAT"
    card_dat.write_bytes(dat())
    r = client.post("/api/camdata/import-file", json={"path": str(card_dat)}).json()
    assert r["invalidated"]["thumbnails_removed"] >= 1
    p2 = find(client, "R8051145")
    assert p2["thumb_url"] != p["thumb_url"]
    assert client.get(f"/api/photos/{p['id']}/render").json()["error"] == "calibration_missing"
    # 导入标定包：渲染可用，缩略图换成 04 电脑模拟，链接又变
    assert client.post("/api/calibration/import", json={"path": str(pack)}).status_code == 200
    p3 = find(client, "R8051145")
    assert p3["thumb_url"] not in (p["thumb_url"], p2["thumb_url"])
    simulated = client.get(p3["thumb_url"]).content
    assert simulated != neutral
    assert max(Image.open(io.BytesIO(simulated)).size) == 256
    names = [x.name for x in thumbs_dir.rglob("*.jpg")]
    assert any("-256-r" in n for n in names) and not any(n.endswith("-256-n.jpg") for n in names)
    # 预览：能渲染，同样的参数第二次命中缓存；再换一份相机数据（第 135 代）→ 缓存键变了，重新画
    first = client.get(f"/api/photos/{p['id']}/render", params={"max_edge": 400})
    assert first.status_code == 200 and first.headers["X-GXR-Cache"] == "miss"
    assert client.get(f"/api/photos/{p['id']}/render", params={"max_edge": 400}).headers["X-GXR-Cache"] == "hit"
    g135 = tmp_path / "g135.dat"
    g135.write_bytes(dat(135))
    assert client.post("/api/camdata/import-file", json={"path": str(g135)}).status_code == 200
    again = client.get(f"/api/photos/{p['id']}/render", params={"max_edge": 400})
    assert again.status_code == 200 and again.headers["X-GXR-Cache"] == "miss"
    assert again.headers["X-GXR-Render-Key"] != first.headers["X-GXR-Render-Key"]
    assert find(client, "R8051145")["thumb_url"] != p3["thumb_url"]
