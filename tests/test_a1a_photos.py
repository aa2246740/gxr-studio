"""A1a：严格扫描和照片核对（设计 §4.4、§4.5；§13.3 DC02；§13.4 PH01–PH03、PH05）。

卡全是 tests/support/fake_card.py 现场生成的合成卡。录像按用户决定导入（原样复制，不解码），
所以 PH03 里 `.AVI` 被导入、其他非照片文件进 `not_importable`。
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from datetime import datetime
from pathlib import Path

import pytest

from gxr_studio import audit
from gxr_studio.cards import coverage
from gxr_studio.cards.coverage import Identity, photo_coverage
from gxr_studio.cards.discovery import CardReader, find_cards, scan_card_strict
from gxr_studio.cards.importer import CHECK_FAILED, DONE_ALL, ImportOptions, run_import
from gxr_studio.cards.session import SessionRegistry
from support.fake_card import FakeProbe, avi_bytes, fake_dat, jpeg_bytes, make_fake_card, stamp

UUID_A = "AAAAAAAA-1111-2222-3333-444444444444"
APPLEDOUBLE = b"\x00\x05\x16\x07\x00\x02\x00\x00" + bytes(24)
OPTS = dict(min_free_bytes=0, make_thumbs=False)


def one(volumes):
    (c,) = find_cards([volumes])
    return c


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class CountingReader(CardReader):
    def __init__(self):
        self.opened: list[str] = []

    def open_rb(self, path):
        self.opened.append(Path(path).name)
        return super().open_rb(path)


class FailingReader(CountingReader):
    """在严格扫描的某一步注入错误：where = root_open | root_list | dcim_open | sub_list | file_stat。"""

    def __init__(self, where: str, code: int):
        super().__init__()
        self.where, self.code, self.names = where, code, {}

    def _fail(self):
        raise OSError(self.code, os.strerror(self.code))

    def open_dir(self, name, dir_fd=None):
        if self.where == "root_open" and dir_fd is None:
            self._fail()
        if self.where == "dcim_open" and str(name).upper() == "DCIM":
            self._fail()
        fd = super().open_dir(name, dir_fd)
        self.names[fd] = Path(str(name)).name
        return fd

    def listdir_fd(self, fd):
        name = self.names.get(fd, "")
        if self.where == "root_list" and name not in ("DCIM", "100RICOH"):
            self._fail()
        if self.where == "sub_list" and name == "100RICOH":
            self._fail()
        return super().listdir_fd(fd)

    def stat_at(self, name, dir_fd):
        if self.where == "file_stat" and name == "R0010002.JPG":
            self._fail()
        return super().stat_at(name, dir_fd)

    def close(self, fd):
        self.names.pop(fd, None)
        super().close(fd)


# ---- DC02 -------------------------------------------------------------------


@pytest.mark.parametrize("where", ["root_open", "root_list", "dcim_open", "sub_list", "file_stat"])
@pytest.mark.parametrize("code", [errno.EACCES, errno.EIO, errno.ENXIO])
def test_dcim_scan_error_is_not_empty_card(volumes, lib, where, code):
    """DC02：卡根、DCIM、任意子目录或文件 stat 返回 EACCES/EIO/ENXIO：状态是扫描失败，不是空卡、照片 0 张或"都已导入"。"""
    card = make_fake_card(volumes, pairs=1, jpg_only=1)
    c = one(volumes)
    run_import(lib, c, ImportOptions(**OPTS))  # 先全部导入：如果把扫描失败当空卡，这里就会说"都已导入"
    reader = FailingReader(where, code)
    scan = scan_card_strict(c.root, reader)
    assert scan.complete is False and scan.errors
    assert errno.errorcode[code] in " ".join(scan.errors)
    cov = photo_coverage(lib, c.root, Identity(), reader, scan=scan)
    assert cov.complete is False and cov.all_imported is False
    with audit.AuditRecorder(watch=(str(card.root),)) as rec:
        p = run_import(lib, c, ImportOptions(**OPTS), reader=reader)
    assert rec.writes_under(card.root) == []
    assert p.state == "done" and p.coverage["complete"] is False and p.coverage["all_imported"] is False
    assert p.message == CHECK_FAILED and "都已导入" not in p.message and "推出" not in p.message


def test_unreadable_subfolder_with_zero_visible_photos(volumes, lib):
    """DC02：照片数为 0 不代表没有照片。唯一的子目录读不了（真实的 EACCES）：不是空卡。"""
    card = make_fake_card(volumes, pairs=1, jpg_only=0)
    sub = card.root / "DCIM" / "100RICOH"
    os.chmod(sub, 0)
    c = one(volumes)
    try:
        if os.access(sub, os.R_OK):
            pytest.skip("以 root 运行，权限位挡不住读取")
        scan = scan_card_strict(c.root)
        assert scan.complete is False and scan.photos == {"dng": [], "jpg": []} and scan.dcim == "present"
        p = run_import(lib, c, ImportOptions(**OPTS))
        assert p.coverage["total"] == 0 and p.coverage["complete"] is False
        assert "请勿格式化" in p.message and "都已导入" not in p.message
    finally:
        audit.unprotect(card.root)
        os.chmod(sub, stat.S_IRWXU)


def test_dcim_link_or_file_is_not_followed(volumes, tmp_path, lib):
    """严格扫描不跟随链接：DCIM 是链接时不算相机卡，也不顺着链接去数照片；DCIM 是普通文件同样不算。"""
    elsewhere = make_fake_card(tmp_path / "elsewhere", pairs=1)
    root = volumes / "LINKED"
    root.mkdir()
    (root / "DCIM").symlink_to(elsewhere.root / "DCIM")
    assert find_cards([volumes]) == [], "DCIM 是链接：不当成卡"
    scan = scan_card_strict(root)
    assert scan.dcim == "link" and scan.importable == [] and not scan.entries
    cov = photo_coverage(lib, root, Identity(), scan=scan)
    assert cov.all_imported is False
    plain = volumes / "PLAIN"
    plain.mkdir()
    (plain / "DCIM").write_bytes(b"not a folder")
    assert scan_card_strict(plain).dcim == "not_dir"


def test_strict_scan_lists_everything_in_dcim(volumes):
    """DCIM 里没有任何例外：点文件、非照片文件、嵌套目录、链接都列出来；卡根只例外列明的系统项。"""
    card = make_fake_card(
        volumes,
        dcim_extra={"100RICOH/._R0010001.JPG": APPLEDOUBLE, "100RICOH/sub/deeper/R0010099.JPG": jpeg_bytes(99), ".DS_Store": b"y"},
        root_extra={"MISC/AUTPRINT.MRK": b"m", ".Spotlight-V100/Store": b"s", ".fseventsd/x": b"f", "UNKNOWN.BIN": b"u"},
    )
    (card.root / "DCIM" / "100RICOH" / "link.JPG").symlink_to(card.root / "MISC" / "AUTPRINT.MRK")
    scan = scan_card_strict(card.root)
    assert scan.complete and scan.dcim == "present"
    assert "DCIM/100RICOH/sub/deeper/R0010099.JPG" in {e.relpath for e in scan.photos["jpg"]}
    assert {e.relpath for e in scan.other_files} == {"DCIM/100RICOH/link.JPG"}
    assert {e.relpath for e in scan.system_files} == {"DCIM/100RICOH/._R0010001.JPG", "DCIM/.DS_Store"}
    assert {e.relpath for e in scan.root_entries} == {"DCIM", "MISC", "UNKNOWN.BIN"}
    link = next(e for e in scan.entries if e.relpath.endswith("link.JPG"))
    assert link.type == "link"


def test_strict_scan_detects_swapped_root(volumes):
    card = make_fake_card(volumes)
    st = os.stat(card.root)
    assert scan_card_strict(card.root, expect=(st.st_dev, st.st_ino)).complete
    other = scan_card_strict(card.root, expect=(st.st_dev, st.st_ino + 1))
    assert other.complete is False and "root_changed" in other.errors[0]


# ---- PH01 -------------------------------------------------------------------


def test_done_message_requires_content_proof(volumes, lib):
    """PH01：开关关着时第二次导入同一张卡：每个文件都读了内容；只有 SHA 全在图库、文件都在，才说"都已导入"。"""
    card = make_fake_card(volumes, pairs=2, jpg_only=1, videos=1)
    c = one(volumes)
    p1 = run_import(lib, c, ImportOptions(**OPTS), ident=Identity(UUID_A))
    assert p1.message == "已导入 3 张照片和 1 个视频。现在可以推出存储卡了。"
    assert coverage.SEEN_TRUST_UUID is False
    reader = CountingReader()
    p2 = run_import(lib, c, ImportOptions(**OPTS), reader=reader, ident=Identity(UUID_A))
    assert sorted(reader.opened) == sorted(Path(r).name for r in card.files), "每个文件正好读一次内容"
    assert p2.copied_files == 0 and p2.seen_shots == 0
    assert p2.coverage["proof"] == {"content": 6, "trusted_seen": 0} and p2.coverage["covered"] == 6
    assert p2.message == DONE_ALL
    # 图库里的文件不在了（索引还在）：同样读到了内容，但不能算"已在图库"
    victim = lib.root / lib.db.execute("SELECT relpath FROM files WHERE relpath LIKE '%R0010003.JPG'").fetchone()[0]
    victim.rename(victim.with_suffix(".moved"))
    cov = photo_coverage(lib, c.root, Identity(UUID_A))
    assert cov.uncovered == ["DCIM/100RICOH/R0010003.JPG"] and cov.all_imported is False


# ---- PH02 -------------------------------------------------------------------


def test_library_deleted_file_not_covered(volumes, lib):
    """PH02：导入后在图库里删掉一张，再插卡：这一张算 uncovered，会重新导入，不说"都已导入"。"""
    card = make_fake_card(volumes, pairs=2, jpg_only=1)
    c = one(volumes)
    run_import(lib, c, ImportOptions(**OPTS))
    rels = {r["relpath"] for r in lib.db.execute("SELECT relpath FROM files")}
    gone = [lib.root / r for r in rels if r.endswith(("R0010003.JPG", "R0010001.JPG"))]
    for p in gone:
        p.unlink()
    cov = photo_coverage(lib, c.root, Identity())
    assert sorted(cov.uncovered) == ["DCIM/100RICOH/R0010001.JPG", "DCIM/100RICOH/R0010003.JPG"] and not cov.all_imported
    p = run_import(lib, c, ImportOptions(**OPTS))
    # 删掉的两个文件放回原位（同一张照片记录，复审 A1a P2-01 的修复路径），不会多出重复的照片
    assert p.imported_shots + p.completed_shots == 2 and p.copied_files == 2 and lib.count() == 3, p
    assert p.message == "已导入 2 张照片。现在可以推出存储卡了。" and "都已导入" not in p.message
    assert all(g.read_bytes() == card.files[f"DCIM/100RICOH/{g.name}"] for g in gone)
    assert run_import(lib, c, ImportOptions(**OPTS)).message == DONE_ALL


# ---- PH03 -------------------------------------------------------------------

EXTRAS = {
    "100RICOH/R0010009.MOV": b"mov",
    "100RICOH/R0010010.THM": b"thm",
    "100RICOH/.R0010012.JPG": jpeg_bytes(12),  # 点开头的照片：不是系统文件，仍然"不会被导入"
    "100RICOH/.hidden/R0010011.JPG": jpeg_bytes(11),  # 点目录里的照片：同上
    "NOTE.TXT": b"note",
}


def test_not_importable_files_block_all_imported_claim(volumes, lib):
    """PH03：DCIM 里的非照片文件进 not_importable，提示"存储卡上有 N 个文件不会被导入"，不说"都已导入"。
    .AVI 按用户决定导入（原样复制），不在 not_importable 里；卡根的文件不算。"""
    make_fake_card(volumes, videos=1, dcim_extra=EXTRAS, root_extra={"MISC/AUTPRINT.MRK": b"m", "GXRCE.DAT": fake_dat(1)})
    c = one(volumes)
    p1 = run_import(lib, c, ImportOptions(**OPTS))
    assert sorted(p1.coverage["not_importable"]) == sorted(f"DCIM/{r}" for r in EXTRAS)
    assert p1.message == "已导入 3 张照片和 1 个视频。存储卡上有 5 个文件不会被导入"
    p2 = run_import(lib, c, ImportOptions(**OPTS))
    assert p2.message == "存储卡上有 5 个文件不会被导入" and p2.coverage["all_imported"] is False
    assert p2.coverage["covered"] == p2.coverage["total"] == 6


MAC_METADATA = {
    "100RICOH/._R0001.JPG": APPLEDOUBLE,
    "100RICOH/._R0010001.JPG": APPLEDOUBLE,
    ".DS_Store": b"Bud1",
    "100RICOH/.DS_Store": b"Bud1",
    ".Spotlight-V100/Store-V2/store.db": b"s",
    ".Trashes/501/.DS_Store": b"Bud1",
    ".fseventsd/0000000000a1b2c3": b"f",
    ".TemporaryItems/folders.501/x": b"t",
    "100RICOH/.metadata_never_index": b"",
}


def test_macos_metadata_does_not_block_all_imported(volumes, lib):
    """在访达里看过卡以后 macOS 留下的元数据：不导入，单独归为系统文件，照片全部导入后仍然说"都已导入"。"""
    make_fake_card(volumes, videos=1, dcim_extra=MAC_METADATA)
    c = one(volumes)
    p1 = run_import(lib, c, ImportOptions(**OPTS))
    assert p1.message == "已导入 3 张照片和 1 个视频。现在可以推出存储卡了。"
    assert p1.coverage["not_importable"] == [] and sorted(p1.coverage["system_files"]) == sorted(f"DCIM/{r}" for r in MAC_METADATA)
    assert lib.count() == 3 and not list(lib.root.rglob("._*"))
    p2 = run_import(lib, c, ImportOptions(**OPTS))
    assert p2.message == DONE_ALL and p2.coverage["all_imported"] is True
    # 照片本身的核对不放松：图库里少了一张，系统文件再多也不说"都已导入"
    victim = lib.root / lib.db.execute("SELECT relpath FROM files WHERE relpath LIKE '%R0010002.JPG'").fetchone()[0]
    victim.unlink()
    cov = photo_coverage(lib, c.root, Identity())
    assert cov.uncovered == ["DCIM/100RICOH/R0010002.JPG"] and cov.all_imported is False


def test_system_file_rule_only_covers_macos_names():
    from gxr_studio.cards.discovery import is_system_file

    for rel, head in (
        ("DCIM/._R0001.JPG", APPLEDOUBLE),
        ("DCIM/100RICOH/.DS_Store", b"Bud1"),
        ("DCIM/.Trashes/501/.DS_Store", b"Bud1"),
        ("DCIM/.fseventsd/x", b"f"),
        ("DCIM/100RICOH/.localized", b""),
    ):
        assert is_system_file(rel, head), rel
    for rel, head in (
        ("DCIM/._R0001.JPG", b"not appledouble"),  # 名字对、内容不对
        ("DCIM/._R0001.JPG", jpeg_bytes(1)[:16]),
        ("DCIM/.Trashes/501/R0010001.JPG", b""),  # 照片扩展名：不管在哪个目录都不算系统文件（复审 P1-04）
        ("DCIM/.fseventsd/x", jpeg_bytes(1)[:16]),  # 内容是 JPEG
        ("DCIM/.Spotlight-V100/x", avi_bytes(1)[:16]),  # 内容是 AVI
        ("DCIM/100RICOH/.R0010001.JPG", b""),
        ("DCIM/.hidden/R0010001.JPG", b""),
        ("DCIM/.hidden/.foo", b""),
        ("DCIM/100RICOH/Thumbs.db", b""),
        ("DCIM/100RICOH/R0010001.AVI", b""),
    ):
        assert not is_system_file(rel, head), rel


def test_videos_are_copied_byte_for_byte_and_survive_rebuild(volumes, lib):
    """录像原样复制进图库（不解码、不进照片网格），能证明"已导入"；重建索引后照样认得。"""
    card = make_fake_card(volumes, pairs=1, jpg_only=0, videos=2)
    c = one(volumes)
    p = run_import(lib, c, ImportOptions(**OPTS))
    assert p.imported_videos == 2 and p.imported_shots == 1 and p.message == "已导入 1 张照片和 2 个视频。现在可以推出存储卡了。"
    vids = {r["relpath"]: r["sha256"] for r in lib.db.execute("SELECT relpath, sha256 FROM videos")}
    assert sorted(Path(r).name for r in vids) == ["R0010002.AVI", "R0010003.AVI"]
    for rel, digest in vids.items():
        data = (lib.root / rel).read_bytes()
        assert digest == sha(data) and data == card.files[f"DCIM/100RICOH/{Path(rel).name}"]
        assert rel.startswith("2026/2026-10-20/"), "录像按文件时间放进日期目录"
    assert lib.count() == 1, "录像不进照片网格"
    rep = lib.rebuild()
    assert rep.videos == 2 and lib.video_count() == 2
    assert run_import(lib, c, ImportOptions(**OPTS)).message == DONE_ALL


def test_same_name_video_from_another_card_gets_a_suffix(volumes, tmp_path, lib):
    make_fake_card(volumes, pairs=0, jpg_only=0, videos=1, seed="A")
    run_import(lib, one(volumes), ImportOptions(**OPTS))
    v2 = tmp_path / "v2"
    make_fake_card(v2, pairs=0, jpg_only=0, videos=1, seed="B")
    p = run_import(lib, find_cards([v2])[0], ImportOptions(**OPTS))
    assert p.imported_videos == 1
    names = sorted(Path(r["relpath"]).name for r in lib.db.execute("SELECT relpath FROM videos"))
    assert names[0] == "R0010001.AVI" and names[1].startswith("R0010001_") and names[1].endswith(".AVI")


def test_unreadable_video_is_a_problem(volumes, lib):
    make_fake_card(volumes, pairs=1, jpg_only=0, videos=1)

    class Bad(CardReader):
        def open_rb(self, path):
            if Path(path).suffix == ".AVI":
                raise OSError(errno.EIO, "I/O error")
            return super().open_rb(path)

    p = run_import(lib, one(volumes), ImportOptions(**OPTS), reader=Bad())
    assert p.unreadable == ["DCIM/100RICOH/R0010002.AVI"] and "请勿格式化" in p.message
    assert p.coverage["uncovered"] == ["DCIM/100RICOH/R0010002.AVI"]


# ---- PH05 -------------------------------------------------------------------


@pytest.fixture
def trusted_setup(volumes, lib, monkeypatch):
    monkeypatch.setattr(coverage, "SEEN_TRUST_UUID", True)
    card = make_fake_card(volumes, pairs=1, jpg_only=1, videos=1)
    c = one(volumes)
    run_import(lib, c, ImportOptions(**OPTS), ident=Identity(UUID_A))
    return card, c


def test_trusted_seen_skips_reading_only_when_all_four_hold(trusted_setup, lib):
    """PH05 基线：开关开、UUID 完整唯一、seen2 命中、图库文件在且长度相同：不读内容，证明是 trusted_seen。"""
    card, c = trusted_setup
    reader = CountingReader()
    p = run_import(lib, c, ImportOptions(**OPTS), reader=reader, ident=Identity(UUID_A))
    assert reader.opened == [] and p.coverage["proof"] == {"content": 0, "trusted_seen": 4}
    assert p.message == DONE_ALL


@pytest.mark.parametrize("drop", ["flag_off", "uuid_ambiguous", "uuid_missing", "seen2_miss", "library_missing", "library_length"])
def test_trusted_seen_requires_flag_unique_uuid_and_library_file(trusted_setup, lib, volumes, monkeypatch, drop):
    """PH05：四个条件逐一去掉一个，每一种都退回读内容。"""
    card, c = trusted_setup
    ident = Identity(UUID_A)
    target = "R0010001.JPG"
    if drop == "flag_off":
        monkeypatch.setattr(coverage, "SEEN_TRUST_UUID", False)
    elif drop == "uuid_ambiguous":
        probe = FakeProbe()
        twin = make_fake_card(volumes, "TWIN", pairs=0, jpg_only=1, seed="twin")
        probe.set_uuid(card.root, UUID_A)
        probe.set_uuid(twin.root, UUID_A)
        sessions = {s.label: s for s in SessionRegistry(probe).attach_all(find_cards([volumes]))}
        assert sessions["NO NAME"].uuid_state == "ambiguous" and sessions["NO NAME"].history_uuid is None
        ident = Identity(sessions["NO NAME"].history_uuid)
    elif drop == "uuid_missing":
        ident = Identity(None)
    elif drop == "seen2_miss":
        audit.unprotect(card.root)  # 模拟相机改了这个文件的时间
        stamp(card.path(f"DCIM/100RICOH/{target}"), datetime(2026, 10, 21, 8, 0, 0))
        audit.protect(card.root)
    else:
        rel = lib.db.execute("SELECT relpath FROM files WHERE relpath LIKE ?", (f"%{target}",)).fetchone()[0]
        if drop == "library_missing":
            (lib.root / rel).unlink()
        else:
            with open(lib.root / rel, "ab") as fh:
                fh.write(b"\0")
    reader = CountingReader()
    p = run_import(lib, c, ImportOptions(**OPTS), reader=reader, ident=ident)
    if drop in ("flag_off", "uuid_ambiguous", "uuid_missing"):
        assert sorted(reader.opened) == sorted(Path(r).name for r in card.files), "整张卡都要读内容"
        assert p.coverage["proof"]["trusted_seen"] == 0
    else:
        assert target in reader.opened, "去掉条件的那个文件必须读内容"
        assert p.coverage["proof"]["content"] >= (0 if drop == "library_length" else 1)
    if drop == "library_length":
        # 图库里那份长度不对（复审 A1a P2-01）：重读卡上原件放回原位，被改过的文件留着，这一轮如实说"已导入 1 张"
        rel = lib.db.execute("SELECT relpath FROM files WHERE relpath LIKE ?", (f"%{target}",)).fetchone()[0]
        assert (lib.root / rel).read_bytes() == card.files[f"DCIM/100RICOH/{target}"]
        assert list((lib.root / rel).parent.glob(f"{target}.modified-*"))
        assert p.coverage["all_imported"] is True and p.message == "已导入 1 张照片。现在可以推出存储卡了。"
    else:
        assert p.message in (DONE_ALL, "已导入 1 张照片。现在可以推出存储卡了。"), p.message


def test_trusted_sha_unit_conditions(lib, volumes):
    card = make_fake_card(volumes, pairs=0, jpg_only=1, videos=0)
    c = one(volumes)
    run_import(lib, c, ImportOptions(**OPTS), ident=Identity(UUID_A))
    rel = "DCIM/100RICOH/R0010001.JPG"
    st = os.stat(card.path(rel))
    args = (lib, Identity(UUID_A), rel, st.st_size, st.st_mtime_ns)
    assert coverage.trusted_sha(*args, trust=True) == sha(card.files[rel])
    assert coverage.trusted_sha(*args, trust=False) is None
    assert coverage.trusted_sha(*args) is None, "默认跟着 SEEN_TRUST_UUID（关）"
    assert coverage.trusted_sha(lib, Identity(None), rel, st.st_size, st.st_mtime_ns, trust=True) is None
    assert coverage.trusted_sha(lib, Identity("CCCCCCCC-1111-2222-3333-444444444444"), rel, st.st_size, st.st_mtime_ns, trust=True) is None


def test_coverage_backfills_seen2_and_never_writes_card(volumes, lib):
    card = make_fake_card(volumes, pairs=1, jpg_only=0, videos=1)
    c = one(volumes)
    run_import(lib, c, ImportOptions(**OPTS))  # 没有 UUID：不写 seen2
    assert lib.db.execute("SELECT COUNT(*) FROM seen2").fetchone()[0] == 0
    with audit.AuditRecorder(watch=(str(card.root),), record_reads=True) as rec:
        cov = photo_coverage(lib, c.root, Identity(UUID_A))
    assert rec.writes_under(card.root) == [] and rec.reads_under(card.root)
    assert cov.all_imported and lib.db.execute("SELECT COUNT(*) FROM seen2").fetchone()[0] == 3


def test_summary_never_claims_imported_without_proof(volumes, lib):
    """摘要不读内容：开关关着时每张都算"要核对"，界面据此去导入，而不是直接说"都已导入"。"""
    make_fake_card(volumes, pairs=1, jpg_only=1, videos=1)
    c = one(volumes)
    run_import(lib, c, ImportOptions(**OPTS), ident=Identity(UUID_A))
    s = coverage.card_summary(lib, c, ident=Identity(UUID_A))
    assert (s["shots"], s["new_shots"], s["videos"], s["new_videos"], s["verified"]) == (2, 2, 1, 1, False)


def test_card_reader_does_not_follow_a_final_symlink(volumes, tmp_path):
    secret = tmp_path / "outside.bin"
    secret.write_bytes(avi_bytes(1))
    card = make_fake_card(volumes, pairs=0, jpg_only=0)
    link = card.root / "DCIM" / "100RICOH" / "R0010001.AVI"
    link.symlink_to(secret)
    with pytest.raises(OSError):
        CardReader().open_rb(link)
