"""A1a：卡的身份（设计 §4.1–§4.3，§13.2 ID01–ID03、ID08，§13.11 test_card_uid_distinct_same_label）。

全部用 tests/support/fake_card.py 现场生成的合成卡和合成卷信息；不碰 /Volumes 下的真实卷（conftest）。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from conftest import PORT
from gxr_studio import audit
from gxr_studio.app import create_app
from gxr_studio.cards import coverage
from gxr_studio.cards.coverage import Identity
from gxr_studio.cards.discovery import CardReader, find_cards
from gxr_studio.cards.importer import ImportOptions, run_import
from gxr_studio.cards.session import (
    DISKUTIL,
    DISKUTIL_TIMEOUT,
    SESSION_ID_RE,
    SessionError,
    SessionRegistry,
    VolumeProbe,
    new_session_id,
)
from gxr_studio.library.store import Library
from support.fake_card import FakeProbe, make_fake_card, replace_card

UUID_A = "AAAAAAAA-1111-2222-3333-444444444444"
UUID_B = "BBBBBBBB-1111-2222-3333-444444444444"
OPTS = dict(min_free_bytes=0, make_thumbs=False)


def client_for(settings, probe, **kw):
    app = create_app(settings, sessions=SessionRegistry(probe), **kw)
    return TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token})


def wait_job(client, job_id):
    for _ in range(600):
        job = client.get(f"/api/jobs/{job_id}").json()
        if not job["running"]:
            return job
        time.sleep(0.02)
    raise AssertionError("导入任务没有结束")


def import_card(client, card_id):
    r = client.post("/api/import", json={"card_id": card_id})
    assert r.status_code == 202, r.text
    return wait_job(client, r.json()["id"])["progress"]


def library_shas(lib: Library) -> set[str]:
    return {r["sha256"] for r in lib.db.execute("SELECT sha256 FROM files")}


class CountingReader(CardReader):
    def __init__(self):
        self.opened: list[str] = []

    def open_rb(self, path):
        self.opened.append(Path(path).name)
        return super().open_rb(path)


# ---- ID01 -------------------------------------------------------------------


@pytest.mark.parametrize(
    "uuid_b, trust",
    [(UUID_B, False), (UUID_A, False), (None, False), (UUID_B, True), (None, True)],
    ids=["new-uuid", "same-uuid", "uuid-missing", "new-uuid-trust-on", "uuid-missing-trust-on"],
)
def test_same_label_seen_isolation(settings, volumes, uuid_b, trust, monkeypatch):
    """ID01：两张合成卡先后复用同一挂载路径，卷标、相对路径、长度、mtime 相同，内容不同。
    导入第二张必须读内容并保存第二个 SHA，不能 seen_shots 增加后直接返回；旧卷标 seen 也不能当可信 UUID 命中。"""
    probe = FakeProbe()
    card_a = make_fake_card(volumes, seed="A")
    probe.set_uuid(card_a.root, UUID_A)
    with client_for(settings, probe) as client:
        (a,) = client.get("/api/cards").json()
        assert a["uuid_state"] == "ok"
        pa = import_card(client, a["id"])
        assert pa["imported_shots"] == 3 and pa["message"].startswith("已导入 3 张")
        lib = client.app.state.ctx.lib
        shas_a = library_shas(lib)
        # 旧版本留下的按卷标分区的 seen 记录（真实升级时就有）：不能被当成可信的 UUID 命中
        assert lib.db.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 5

        meta_a = {r: (os.stat(card_a.path(r)).st_size, os.stat(card_a.path(r)).st_mtime_ns) for r in card_a.files}
        card_b = replace_card(card_a, seed="B")
        probe.remount(card_b.root)
        probe.set_uuid(card_b.root, uuid_b)
        meta_b = {r: (os.stat(card_b.path(r)).st_size, os.stat(card_b.path(r)).st_mtime_ns) for r in card_b.files}
        assert meta_a == meta_b and all(card_a.files[r] != card_b.files[r] for r in card_a.files), "同路径、同长度、同 mtime，内容不同"
        (b,) = client.get("/api/cards").json()
        assert b["id"] != a["id"], "同一个挂载路径换了卡，会话号必须换"
        assert client.post("/api/import", json={"card_id": a["id"]}).status_code == 404, "旧会话已作废"
        # 默认（信任开关关着）一律读内容；开关打开时，旧卷标 seen 和另一张卷的 seen2 也不能让第二张卡跳过。
        # 同一个 UUID + 开关打开正是设计 §4.3 说的未核实风险（U1），所以开关默认关，见 test_trust_flag_defaults_off。
        monkeypatch.setattr(coverage, "SEEN_TRUST_UUID", trust)
        pb = import_card(client, b["id"])
        assert pb["seen_shots"] == 0 and pb["imported_shots"] == 3, pb
        shas_b = library_shas(lib) - shas_a
        assert shas_b == {hashlib.sha256(d).hexdigest() for d in card_b.files.values()}
        assert pb["coverage"]["all_imported"] is True and pb["coverage"]["proof"]["trusted_seen"] == 0


def test_old_label_seen_rows_are_not_trusted(volumes, lib, monkeypatch):
    """ID01 后半：旧卷标 seen 记录不能直接迁移为可信 UUID 命中（开关打开、UUID 完整也不行）。"""
    card = make_fake_card(volumes, seed="A")
    (c,) = find_cards([volumes])
    rows = [(c.key, r, len(d), os.stat(card.path(r)).st_mtime_ns, "0" * 64) for r, d in card.files.items()]
    lib.add_seen(rows)  # 旧版本留下的记录，SHA 甚至是错的
    monkeypatch.setattr(coverage, "SEEN_TRUST_UUID", True)
    reader = CountingReader()
    p = run_import(lib, c, ImportOptions(**OPTS), reader=reader, ident=Identity(UUID_A))
    assert p.seen_shots == 0 and p.imported_shots == 3
    assert set(reader.opened) == {Path(r).name for r in card.files}, "每个文件都读了内容"
    assert lib.db.execute("SELECT COUNT(*) FROM seen2 WHERE volume_uuid=?", (UUID_A,)).fetchone()[0] == 5


def test_trust_flag_defaults_off():
    """GXR 格式化后 UUID 会不会变还没核实（§16 U1）：在此之前信任开关必须关着，导入一律读内容。"""
    assert coverage.SEEN_TRUST_UUID is False


# ---- ID02 -------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "fail", "timeout", "garbage", "wrong-mount", "not-a-uuid"])
def test_uuid_missing_is_session_bound(settings, volumes, value):
    """ID02：UUID 缺失、空值、diskutil 失败或超时：只读发现照样返回；不按卷标关联历史（不写 seen2）；
    会话只绑定本次挂载，重挂载、重启后立即失效。"""
    probe = FakeProbe()
    card = make_fake_card(volumes, seed=1)
    probe.set_uuid(card.root, value)
    with client_for(settings, probe) as client:
        (c,) = client.get("/api/cards").json()
        assert c["uuid_state"] == "missing" and c["label"] == "NO NAME"
        p = import_card(client, c["id"])
        assert p["state"] == "done" and p["imported_shots"] == 3
        lib = client.app.state.ctx.lib
        assert lib.db.execute("SELECT COUNT(*) FROM seen2").fetchone()[0] == 0, "没有完整 UUID 不关联历史"
        assert all(e.get("volume_uuid") is None for e in lib.read_log() if e["type"] == "run")
        # 同一个挂载重新挂载：旧会话号立即失效，不按卷标"认出"它
        probe.remount(card.root)
        assert client.get(f"/api/cards/{c['id']}/summary").status_code == 404
        (c2,) = client.get("/api/cards").json()
        assert c2["id"] != c["id"]
    # 重启（新的应用实例）：旧会话号同样失效
    with client_for(settings, probe) as client:
        r = client.post("/api/import", json={"card_id": c2["id"]})
        assert r.status_code == 404 and r.json()["error"] == "card_not_found"


def test_uuid_missing_session_rejects_write_resolution_after_remount(volumes):
    """ID02：只允许"本次挂载内明确选中"：会话作废后，按写入目的解析同样失败。"""
    probe = FakeProbe()
    card = make_fake_card(volumes)
    reg = SessionRegistry(probe)
    (s,) = reg.attach_all(find_cards([volumes]))
    assert s.uuid_state == "missing" and reg.resolve(s.session_id, write=True) is s
    probe.remount(card.root)
    with pytest.raises(SessionError) as e:
        reg.resolve(s.session_id, write=True)
    assert e.value.status == 404


# ---- ID03 -------------------------------------------------------------------


def test_duplicate_uuid_and_truncated_id_collision(settings, volumes, tmp_path):
    """ID03：两个挂载 UUID 相同 → 两个都标歧义，写入目的 409；只读导入可以，但不关联 seen2。
    两个卷根下同名目录（旧 card_id 是卷标哈希，会撞号）→ 会话号不同，各自只操作自己那张。"""
    probe = FakeProbe()
    a = make_fake_card(volumes, "CARD-A", seed="A")
    b = make_fake_card(volumes, "CARD-B", seed="B")
    probe.set_uuid(a.root, UUID_A)
    probe.set_uuid(b.root, UUID_A)
    with client_for(settings, probe) as client:
        cards = client.get("/api/cards").json()
        assert [c["uuid_state"] for c in cards] == ["ambiguous", "ambiguous"]
        reg = client.app.state.ctx.sessions
        for c in cards:
            with pytest.raises(SessionError) as e:
                reg.resolve(c["id"], write=True)
            assert (e.value.status, e.value.code) == (409, "identity_ambiguous")
        pb = import_card(client, next(c["id"] for c in cards if c["label"] == "CARD-B"))
        lib = client.app.state.ctx.lib
        assert pb["imported_shots"] == 3 and library_shas(lib) == {hashlib.sha256(d).hexdigest() for d in b.files.values()}
        assert lib.db.execute("SELECT COUNT(*) FROM seen2").fetchone()[0] == 0

    # 旧 card_id（卷标哈希）撞号：两个卷根下都有 "NO NAME"
    v2 = tmp_path / "volumes2"
    v2.mkdir()
    c1 = make_fake_card(volumes, "NO NAME", seed=1)
    c2 = make_fake_card(v2, "NO NAME", seed=2)
    found = find_cards([volumes, v2])
    old_keys = {c.key for c in found if c.label == "NO NAME"}
    assert len(old_keys) == 1, "旧 card_id 确实撞号"
    ids = iter(["A" * 26, "A" * 26, "B" * 26])  # 生成器给出重复号：登记表不能让两个会话共用
    reg = SessionRegistry(probe, id_factory=lambda: next(ids))
    sessions = {s.root: s for s in reg.attach_all([c for c in found if c.label == "NO NAME"])}
    assert {s.session_id for s in sessions.values()} == {"A" * 26, "B" * 26}
    assert reg.resolve("B" * 26).root == sessions[c2.root].root
    assert reg.resolve("A" * 26).root == sessions[c1.root].root


def test_card_uid_distinct_same_label(volumes, tmp_path):
    """§13.11：同卷标的两张卡同时插着，card_id（会话号）不同。"""
    v2 = tmp_path / "v2"
    v2.mkdir()
    make_fake_card(volumes, "RICOHDCX", seed=1)
    make_fake_card(v2, "RICOHDCX", seed=2)
    reg = SessionRegistry(FakeProbe())
    sessions = reg.attach_all(find_cards([volumes, v2]))
    assert len({s.session_id for s in sessions}) == 2 and all(SESSION_ID_RE.match(s.session_id) for s in sessions)


# ---- ID08 -------------------------------------------------------------------

BAD_IDS = [
    "/Volumes/RICOHDCX",
    "../../etc/passwd",
    "..%2F..%2Fetc",
    "%2e%2e%2f",
    "-rf",
    "--help",
    "; rm -rf /",
    "$(id)",
    "`id`",
    "a" * 26,  # 小写不在 Crockford 字母表里
    "I" * 26,  # I/L/O/U 不在字母表里
    "0" * 25,
    "0" * 27,
    "0" * 26 + "/x",
    "",
]


def test_card_id_path_and_command_arguments_rejected(settings, volumes):
    """ID08：card_id 是路径、../、编码分隔符、选项或命令片段时拒绝；diskutil 用参数数组和服务器登记的挂载点，带超时；
    卷消失后不 mkdir 重建挂载目录。"""
    probe = FakeProbe()
    card = make_fake_card(volumes)
    probe.set_uuid(card.root, UUID_A)
    with client_for(settings, probe) as client:
        (c,) = client.get("/api/cards").json()
        for bad in BAD_IDS:
            r = client.post("/api/import", json={"card_id": bad})
            assert r.status_code == 400 and r.json()["error"] == "bad_card_id", bad
            r = client.post("/api/camdata/import-card", json={"card_id": bad})
            assert r.status_code in (400, 404) and r.json()["error"] in ("bad_card_id", "card_not_found"), bad
            if bad and "/" not in bad:
                r = client.get(f"/api/cards/{quote(bad, safe='')}/summary")
                # 含编码分隔符的在路由层就不匹配（框架 404），其余由 resolve_session 拒绝（400）；都拿不到卡
                assert (r.status_code, r.json().get("error")) in ((400, "bad_card_id"), (404, None)) and "shots" not in r.json(), bad
        # diskutil：每次都是固定的绝对路径 + 参数数组 + 服务器从 statfs 拿到的挂载点，带 5 秒超时；从不含 card_id
        assert probe.calls, "应该调用过 diskutil"
        for args, kw in probe.calls:
            assert args == [DISKUTIL, "info", "-plist", str(card.root)]
            assert kw.get("timeout") == DISKUTIL_TIMEOUT and "shell" not in kw
        assert DISKUTIL_TIMEOUT == 5.0
        # 卷消失：会话作废，不重建挂载目录
        audit.unprotect(card.root)
        shutil.rmtree(card.root)
        for path in (f"/api/cards/{c['id']}/summary",):
            r = client.get(path)
            assert r.status_code == 404 and r.json()["error"] == "card_not_found"
        r = client.post("/api/import", json={"card_id": c["id"]})
        assert r.status_code == 404
        assert not card.root.exists(), "卷消失后不能 mkdir 重建挂载目录"
        assert client.get("/api/cards").json() == []


def test_real_probe_only_asks_diskutil_about_real_mount_points(volumes):
    """真实的探针：临时目录不是挂载点，不调用 diskutil（UUID 记为缺失），也不会把所在卷的 UUID 当成卡的。"""
    calls = []
    probe = VolumeProbe(runner=lambda *a, **k: calls.append(a))
    card = make_fake_card(volumes)
    fp = probe.fingerprint(str(card.root))
    assert fp is not None and fp.mount_path == str(card.root)
    assert probe.volume_uuid(str(card.root)) is None and calls == []
    assert probe.fingerprint(str(card.root / "missing")) is None
    link = volumes / "LINK"
    link.symlink_to(card.root)
    assert probe.fingerprint(str(link)) is None, "挂载路径是链接：不当成卡"


def test_session_ids_are_random_crockford():
    ids = {new_session_id() for _ in range(200)}
    assert len(ids) == 200 and all(SESSION_ID_RE.match(i) for i in ids)
