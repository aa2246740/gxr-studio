"""导入：只读卡、配对、去重、日期、幂等、拔卡续传、空间不足、校验。（M1 验收 1–5）"""

from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from gxr_studio import audit
from gxr_studio import meta as metamod
from gxr_studio.cards import importer
from gxr_studio.cards.discovery import CardReader, find_cards, scan_card
from gxr_studio.cards.importer import ImportOptions, run_import
from gxr_studio.devtools.synthetic_card import FALLBACK_MTIME, tree_snapshot
from gxr_studio.library.store import Library, sha256_file

OPTS = dict(min_free_bytes=0)


def one_card(volumes):
    (c,) = find_cards([volumes])
    return c


def library_media(lib: Library):
    return sorted(p for p in lib.root.rglob("*") if p.is_file() and p.suffix.lower() in (".dng", ".jpg") and ".studio" not in p.parts)


def card_hashes(card) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for s in card.shots:
        for k in s.kinds:
            p = card.path(s, k)
            out.setdefault(sha256_file(p), set()).add(p.name)
    return out


# ---- 验收 1：只读卡 ---------------------------------------------------------


def test_card_is_never_written(volumes, card, lib):
    before = tree_snapshot(card.root)
    c = one_card(volumes)
    with audit.AuditRecorder(watch=(str(card.root),), record_reads=True) as rec:
        p = run_import(lib, c, ImportOptions(**OPTS))
        run_import(lib, c, ImportOptions(**OPTS))  # 第二遍也一样
    assert p.state == "done"
    assert rec.writes_under(card.root) == [], "对卡的写入次数必须为 0（含被拦截的尝试）"
    assert rec.reads_under(card.root), "探针本身要能看到读取，否则上面的 0 没有意义"
    assert tree_snapshot(card.root) == before, "导入前后整棵卡目录（内容、大小、mtime）必须完全一致"


def test_progress_distinguishes_checking_from_copying(volumes, card, lib, monkeypatch):
    progress = importer.ImportProgress()
    calls = []
    stage, probe = importer.stage_file, importer.probe_file

    def copy(*args, **kw):
        calls.append(("copy", progress.phase))
        return stage(*args, **kw)

    def check(*args, **kw):
        calls.append(("check", progress.phase))
        return probe(*args, **kw)

    monkeypatch.setattr(importer, "stage_file", copy)
    monkeypatch.setattr(importer, "probe_file", check)
    c = one_card(volumes)
    run_import(lib, c, ImportOptions(**OPTS), progress=progress)
    assert calls and set(calls) == {("copy", "copying")}
    calls.clear()
    progress = importer.ImportProgress()
    run_import(lib, c, ImportOptions(**OPTS), progress=progress)
    assert calls and set(calls) == {("check", "checking")}
    assert progress.copied_files == 0 and progress.coverage["photos_imported"] is True


def test_writes_to_card_are_blocked_and_recorded(volumes, card):
    one_card(volumes)  # 发现卡就会把它登记为只读
    with audit.AuditRecorder(watch=(str(card.root),)) as rec:
        with pytest.raises(PermissionError):
            open(card.root / "DCIM" / "evil.txt", "wb")
        with pytest.raises(PermissionError):
            os.remove(card.root / "DCIM" / "100RICOH" / "R0000101.JPG")
    assert len(rec.writes_under(card.root)) == 2 and all(w.blocked for w in rec.writes)
    assert (card.root / "DCIM" / "100RICOH" / "R0000101.JPG").exists()


# ---- 配对、垃圾文件、目录 ---------------------------------------------------


def test_pairing_and_junk(volumes, card):
    c = one_card(volumes)
    shots = scan_card(c)
    assert len(shots) == len(card.shots)
    by = {(s.directory, s.stem): set(s.files) for s in shots}
    assert by[("DCIM/100RICOH", "R0000101")] == {"dng", "jpg"}
    assert by[("DCIM/100RICOH", "R0000106")] == {"jpg"}
    assert by[("DCIM/100RICOH", "R0000109")] == {"dng"}
    assert ("DCIM/101RICOH", "R0000111") in by, "DCIM 下所有子目录都要扫"
    names = {f.relpath for s in shots for f in s.files.values()}
    assert not any("/._" in n or "Thumbs" in n or "MISC" in n or "Spotlight" in n or n.endswith(".TXT") for n in names)


def test_pair_key_is_case_insensitive(volumes, tmp_path):
    root = volumes / "CARD" / "DCIM" / "100RICOH"
    root.mkdir(parents=True)
    (root / "r0000001.jpg").write_bytes(b"a")
    (root / "R0000001.DNG").write_bytes(b"b")
    (shot,) = scan_card(one_card(volumes))
    assert set(shot.files) == {"dng", "jpg"}


def test_no_dcim_no_card(volumes):
    (volumes / "USBSTICK").mkdir()
    (volumes / "USBSTICK" / "readme.txt").write_text("x", encoding="utf-8")
    assert find_cards([volumes]) == []


# ---- 去重、同名 -------------------------------------------------------------


def test_import_counts_dedup_and_same_name(volumes, card, lib):
    c = one_card(volumes)
    p = run_import(lib, c, ImportOptions(**OPTS))
    unique = len(card.unique_shots)
    assert (p.state, p.total_shots, p.imported_shots, p.duplicate_shots) == ("done", len(card.shots), unique, 1)
    assert lib.count() == unique
    # 去重：R0000199 与 R0000106 逐字节相同，库里只能有一份
    assert not list(lib.root.rglob("R0000199*"))
    # 同名不同内容：101RICOH/R0000101.JPG 与 100RICOH/R0000101(.DNG/.JPG) 同一天，必须并存，后者加 _sha8
    same_day = sorted(p.name for p in (lib.root / "2026" / "2026-10-16").glob("R0000101*") if not p.name.endswith(".json"))
    suffixed = [n for n in same_day if "_" in n]
    assert "R0000101.DNG" in same_day and "R0000101.JPG" in same_day and len(suffixed) == 1
    assert (lib.root / "2026" / "2026-10-16" / suffixed[0].replace(".JPG", ".gxrs.json")).exists()
    # 库里每个文件的哈希都对应卡上某个文件，卡上每份内容都在库里
    src = card_hashes(card)
    got = {sha256_file(p) for p in library_media(lib)}
    assert got == set(src)


def test_dedup_across_cards_by_content(volumes, card, lib):
    run_import(lib, one_card(volumes), ImportOptions(**OPTS))
    n = lib.count()
    # 另一张卡（不同卷标）上有同一批内容：按内容哈希全部判为重复
    other = volumes / "OTHER" / "DCIM" / "100RICOH"
    other.mkdir(parents=True)
    src = card.root / "DCIM" / "100RICOH"
    for name in ("R0000102.JPG", "R0000102.DNG", "R0000107.JPG"):
        (other / name).write_bytes((src / name).read_bytes())
    cards = {c.label: c for c in find_cards([volumes])}
    p = run_import(lib, cards["OTHER"], ImportOptions(**OPTS))
    assert (p.imported_shots, p.duplicate_shots, lib.count()) == (0, 2, n)
    assert p.copied_files == 0


# ---- 验收 4：日期 -----------------------------------------------------------


def test_dates_folders_and_fallback(volumes, card, lib):
    c = one_card(volumes)
    run_import(lib, c, ImportOptions(**OPTS))
    for s in card.unique_shots:
        shots = [x for x in lib.list_shots(limit=1000) if x["name"].split("_")[0] == s.stem and x["folder"].endswith(s.expected_date)]
        assert shots, f"{s.stem} 应落在 {s.expected_date}"
        sh = shots[0]
        assert sh["date"] == s.expected_date and sh["folder"] == f"{s.expected_date[:4]}/{s.expected_date}"
        assert sh["date_source"] == ("exif" if s.taken else "mtime")
    assert FALLBACK_MTIME.date().isoformat() == "2026-10-20"


def test_undated_goes_to_unknown_folder_when_configured(volumes, card, lib):
    run_import(lib, one_card(volumes), ImportOptions(undated_policy="unknown_folder", **OPTS))
    unknown = lib.list_shots("unknown")
    assert sorted(s["name"] for s in unknown) == ["R0000112", "R0000113", "R0000114"]
    assert all(s["folder"] == "未知日期" and s["date"] is None for s in unknown)
    assert (lib.root / "未知日期" / "R0000112.JPG").exists()


def test_batch_shift_moves_dates_and_is_logged(volumes, card, lib):
    # R0000103 拍于 10-17 23:59:59，整批 +2 秒 → 跨过午夜到 10-18
    run_import(lib, one_card(volumes), ImportOptions(shift_seconds=2, **OPTS))
    r = next(s for s in lib.list_shots(limit=1000) if s["name"] == "R0000103")
    assert r["date"] == "2026-10-18" and r["captured_at"].startswith("2026-10-18T00:00:01")
    runs = [e for e in lib.read_log() if e["type"] in ("run_start", "run")]
    assert {e["shift_seconds"] for e in runs} == {2}
    assert all(e["shift_seconds"] == 2 for e in lib.read_log() if e["type"] == "shot")


def test_bad_exif_dates_are_rejected():
    assert metamod.parse_exif_datetime("0000:00:00 00:00:00") is None
    assert metamod.parse_exif_datetime("    :  :     :  :  ") is None
    assert metamod.parse_exif_datetime("2099:01:01 00:00:00") is None
    assert metamod.parse_exif_datetime("1970:01:01 00:00:00") is None
    assert str(metamod.parse_exif_datetime("2026:10:16 18:51:53\x00")) == "2026-10-16 18:51:53"


# ---- 验收 2：幂等 -----------------------------------------------------------


def test_second_import_copies_nothing_but_reads_every_card_file(volumes, card, lib):
    # 设计 §4.3（P1-01）：seen 不再用来跳过读取。第二遍每个文件都读内容核对，但一个都不复制。
    c = one_card(volumes)
    run_import(lib, c, ImportOptions(**OPTS))
    with audit.AuditRecorder(watch=(str(card.root),), record_reads=True) as rec:
        p = run_import(lib, c, ImportOptions(**OPTS))
    assert (p.copied_files, p.imported_shots, p.seen_shots) == (0, 0, 0)
    assert p.duplicate_shots == len(card.shots)
    opened = {r for r in rec.reads_under(card.root) if os.path.isfile(r)}
    expected = {str(card.path(s, k)) for s in card.shots for k in s.kinds}
    assert expected <= opened, "第二遍也要读每个照片文件的内容"
    assert not list(lib.staging_dir.iterdir()), "核对过的文件不再复制到暂存区"
    # 假卡 DCIM 里的 ._R0000101.JPG 是 macOS 系统文件（忽略）；Thumbs.db、NOTE.TXT 不会被导入，所以不能说"都已导入"
    assert p.coverage["all_imported"] is False and p.coverage["not_importable"] == ["DCIM/100RICOH/Thumbs.db", "DCIM/NOTE.TXT"]
    assert p.coverage["system_files"] == ["DCIM/100RICOH/._R0000101.JPG"]
    assert p.message == "照片都已导入；另有 2 个其他文件不会导入" and p.coverage["photos_imported"] is True


def test_force_verify_reads_everything_but_adds_nothing(volumes, card, lib):
    c = one_card(volumes)
    run_import(lib, c, ImportOptions(**OPTS))
    n, files = lib.count(), len(library_media(lib))
    with audit.AuditRecorder(watch=(str(card.root),), record_reads=True) as rec:
        p = run_import(lib, c, ImportOptions(force_verify=True, **OPTS))
    assert (p.imported_shots, p.copied_files, lib.count(), len(library_media(lib))) == (0, 0, n, files)
    assert len([r for r in rec.reads_under(card.root) if os.path.isfile(r)]) >= len(card.unique_shots)
    assert p.duplicate_shots == len(card.shots)


def test_seen_survives_index_rebuild(volumes, card, lib):
    # seen 只是"可能导入过"的提示：重建后照样恢复，但第二遍仍然读内容核对，不复制
    c = one_card(volumes)
    run_import(lib, c, ImportOptions(**OPTS))
    rep = lib.rebuild()
    assert rep.shots == len(card.unique_shots) and rep.seen_restored > 0
    with audit.AuditRecorder(watch=(str(card.root),), record_reads=True) as rec:
        p = run_import(lib, c, ImportOptions(**OPTS))
    assert p.copied_files == 0 and [r for r in rec.reads_under(card.root) if os.path.isfile(r)]
    assert p.coverage["covered"] == p.coverage["total"]


# ---- 验收 3：中断可恢复 -----------------------------------------------------


class UnpluggedReader(CardReader):
    """读到第 N 个文件之后"拔卡"：目录消失，读操作报 ENODEV。"""

    def __init__(self, card_root: Path, after_files: int):
        self.card_root, self.after, self.count, self.gone = card_root, after_files, 0, False

    def open_rb(self, path, **kwargs):
        if self.gone:
            raise FileNotFoundError(errno.ENOENT, "card gone")
        self.count += 1
        if self.count > self.after:
            self.gone = True
            raise OSError(errno.ENODEV, "device not configured")
        return super().open_rb(path, **kwargs)

    def scandir(self, path):
        if self.gone and Path(path) == self.card_root:
            raise FileNotFoundError(errno.ENOENT, "card gone")
        return super().scandir(path)


def assert_library_consistent(lib: Library, card):
    assert not list(lib.staging_dir.glob("*")), "不能留下 .part"
    src = card_hashes(card)
    for p in library_media(lib):
        assert sha256_file(p) in src, f"库里有残缺或来路不明的文件：{p}"
    for s in lib.list_shots(limit=100000):
        for f in s["files"].values():
            assert sha256_file(lib.abspath(f["relpath"])) == f["sha256"]


def test_unplug_midway_then_resume(volumes, card, lib):
    c = one_card(volumes)
    reader = UnpluggedReader(c.root, after_files=9)
    p = run_import(lib, c, ImportOptions(**OPTS), reader=reader)
    assert p.state == "interrupted" and "重新插入存储卡后可以继续" in p.message
    assert 0 < lib.count() < len(card.unique_shots)
    assert_library_consistent(lib, card)
    done_before = lib.count()
    # 插回：只补缺的
    p2 = run_import(lib, c, ImportOptions(**OPTS))
    assert p2.state == "done" and lib.count() == len(card.unique_shots)
    assert p2.imported_shots == len(card.unique_shots) - done_before
    assert_library_consistent(lib, card)


def test_unreadable_file_is_skipped_not_fatal(volumes, card, lib):
    c = one_card(volumes)

    class Bad(CardReader):
        def open_rb(self, path, **kwargs):
            if Path(path).name == "R0000104.JPG":
                raise OSError(errno.EIO, "I/O error")
            return super().open_rb(path, **kwargs)

    p = run_import(lib, c, ImportOptions(**OPTS), reader=Bad())
    assert p.state == "done" and p.unreadable == ["DCIM/100RICOH/R0000104.JPG"]
    # 一对里读不出的那个明确列出；读得出的 DNG 照常导入（不再整对丢掉）
    assert p.failed_shots == 1
    assert lib.count() == len(card.unique_shots)
    assert sorted(f.name for f in lib.root.rglob("R0000104*")) == ["R0000104.DNG", "R0000104.gxrs.json"]
    assert "安全弹出" not in p.message and "请勿格式化" in p.message
    assert_library_consistent(lib, card)
    # 好了以后再来一次：把 JPG 并进同一张照片，不会多出一张同名的
    p2 = run_import(lib, c, ImportOptions(**OPTS))
    assert p2.completed_shots == 1 and p2.imported_shots == 0 and not p2.unreadable
    assert lib.count() == len(card.unique_shots)
    assert sorted(f.name for f in lib.root.rglob("R0000104*")) == ["R0000104.DNG", "R0000104.JPG", "R0000104.gxrs.json"]
    (shot,) = [s for s in lib.list_shots(limit=1000) if s["name"] == "R0000104"]
    assert set(shot["files"]) == {"dng", "jpg"} and shot["has_raw"] and shot["has_jpg"]
    side = json.loads(lib.sidecar_path_for(shot).read_text(encoding="utf-8"))
    assert side["source"]["jpg"] == "R0000104.JPG" and side["source"]["jpg_sha256"] == shot["files"]["jpg"]["sha256"]
    # 假卡 DCIM 里的 ._R0000101.JPG 是 macOS 系统文件（忽略）；Thumbs.db、NOTE.TXT 不会被导入，所以不说"可以推出"，也不说"都已导入"
    assert p2.message == "已导入 1 张照片，卡上的照片都已在图库里；另有 2 个其他文件不会导入"
    assert_library_consistent(lib, card)
    p3 = run_import(lib, c, ImportOptions(**OPTS))  # 再来一次：什么都不新增
    assert p3.completed_shots == 0 and p3.imported_shots == 0 and lib.count() == len(card.unique_shots)


def test_unreadable_dng_still_imports_the_jpg(volumes, card, lib):
    c = one_card(volumes)

    class Bad(CardReader):
        def open_rb(self, path, **kwargs):
            if Path(path).name == "R0000104.DNG":
                raise OSError(errno.EIO, "I/O error")
            return super().open_rb(path, **kwargs)

    p = run_import(lib, c, ImportOptions(**OPTS), reader=Bad())
    assert p.unreadable == ["DCIM/100RICOH/R0000104.DNG"] and p.failed_shots == 1
    assert sorted(f.name for f in lib.root.rglob("R0000104*")) == ["R0000104.JPG", "R0000104.gxrs.json"]
    p2 = run_import(lib, c, ImportOptions(**OPTS))
    assert p2.completed_shots == 1 and lib.count() == len(card.unique_shots)
    (shot,) = [s for s in lib.list_shots(limit=1000) if s["name"] == "R0000104"]
    assert set(shot["files"]) == {"dng", "jpg"}


def test_completing_a_pair_keeps_the_edits(volumes, card, lib):
    """上次只导进来一半的照片，用户已经裁过了；补上另一半以后裁切还在。"""
    from gxr_studio.library import edits

    c = one_card(volumes)

    class Bad(CardReader):
        def open_rb(self, path, **kwargs):
            if Path(path).name == "R0000104.JPG":
                raise OSError(errno.EIO, "I/O error")
            return super().open_rb(path, **kwargs)

    run_import(lib, c, ImportOptions(**OPTS), reader=Bad())
    (shot,) = [s for s in lib.list_shots(limit=1000) if s["name"] == "R0000104"]
    edits.write_crop(lib, shot, preset="1:1", rect=None, angle_deg=0.0)
    run_import(lib, c, ImportOptions(**OPTS))
    (shot,) = [s for s in lib.list_shots(limit=1000) if s["name"] == "R0000104"]
    assert shot["crop"] and shot["crop"]["preset"] == "1:1"
    assert edits.read_crop(lib, shot)["preset"] == "1:1"


def test_unreadable_folder_is_reported_not_silent(volumes, card, lib):
    c = one_card(volumes)

    class Bad(CardReader):
        def scandir(self, path):
            if Path(path).name == "100RICOH":
                raise PermissionError(errno.EACCES, "denied")
            return super().scandir(path)

    p = run_import(lib, c, ImportOptions(**OPTS), reader=Bad())
    assert p.state == "done" and p.scan_errors == ["DCIM/100RICOH/"]
    assert "安全弹出" not in p.message and "请勿格式化" in p.message
    assert p.has_problems()


def test_verify_failure_is_not_committed(volumes, card, lib):
    c = one_card(volumes)

    def corrupt(part: Path, cf):
        if cf.relpath.endswith("R0000105.DNG"):
            with open(part, "r+b") as fh:
                fh.write(b"\xff")  # 模拟写盘出错

    p = run_import(lib, c, ImportOptions(before_verify=corrupt, **OPTS))
    assert p.verify_failed == ["DCIM/100RICOH/R0000105.DNG"]
    # 校验不一致的这个文件不进图库（也不登记 seen）；同一对里好的 JPG 照常导入
    assert not list(lib.root.rglob("R0000105*.DNG"))
    assert [f.name for f in lib.root.rglob("R0000105*.JPG")] == ["R0000105.JPG"]
    assert p.failed_shots == 1 and "安全弹出" not in p.message and "请勿格式化" in p.message
    assert lib.count() == len(card.unique_shots)
    assert_library_consistent(lib, card)
    p2 = run_import(lib, c, ImportOptions(**OPTS))  # 读卡器好了以后重试：DNG 补进同一张
    assert p2.completed_shots == 1 and not p2.verify_failed and lib.count() == len(card.unique_shots)


# ---- 空间不足 ---------------------------------------------------------------


def test_pause_when_no_space_then_resume(volumes, card, lib):
    c = one_card(volumes)
    budget = {"free": 400_000}  # 只够前几张

    def free(_):
        return budget["free"]

    calls = {"n": 0}

    def after(_):
        calls["n"] += 1
        budget["free"] -= 60_000

    opts = ImportOptions(free_space=free, after_shot=after, **OPTS)
    p = run_import(lib, c, opts)
    assert p.state == "paused_no_space" and "空间不够" in p.message and p.needed_bytes > 0 and p.free_bytes < p.needed_bytes + 1
    partial = lib.count()
    assert 0 < partial < len(card.unique_shots)
    assert_library_consistent(lib, card)
    budget["free"] = 10**12
    p2 = run_import(lib, c, opts)
    assert p2.state == "done" and lib.count() == len(card.unique_shots)
    assert p2.imported_shots == len(card.unique_shots) - partial


def test_enospc_while_writing_pauses(volumes, card, lib, monkeypatch):
    c = one_card(volumes)
    real_stage = importer.stage_file
    n = {"i": 0}

    def flaky(*a, **kw):
        n["i"] += 1
        if n["i"] == 6:
            raise importer.NoSpace()
        return real_stage(*a, **kw)

    monkeypatch.setattr(importer, "stage_file", flaky)
    p = run_import(lib, c, ImportOptions(**OPTS))
    assert p.state == "paused_no_space" and not list(lib.staging_dir.glob("*"))
    monkeypatch.setattr(importer, "stage_file", real_stage)
    assert run_import(lib, c, ImportOptions(**OPTS)).state == "done"
    assert lib.count() == len(card.unique_shots)


def test_cancel(volumes, card, lib):
    c = one_card(volumes)
    cancel = threading.Event()
    p = run_import(lib, c, ImportOptions(after_shot=lambda _: cancel.set(), **OPTS), cancel=cancel)
    assert p.state == "cancelled" and lib.count() == 1


# ---- 验收 3：在明确的导入阶段终止进程 20 轮 ----------------------------------

DRIVER = textwrap.dedent(
    """
    import os, sys
    from pathlib import Path
    from gxr_studio.library.store import Library
    from gxr_studio.cards.discovery import find_cards
    from gxr_studio.cards.importer import run_import, ImportOptions
    volumes, library = Path(sys.argv[1]), Path(sys.argv[2])
    lib = Library(library).open()
    (card,) = find_cards([volumes])
    stage = sys.argv[3]
    def pause(where):
        if where == stage and lib.count() > 0:
            print('ready:' + where + ':' + str(os.getpid()), flush=True)
            sys.stdin.buffer.read(1)
    run_import(lib, card, ImportOptions(min_free_bytes=0,
               before_verify=lambda *a: pause('verify'), after_shot=lambda *a: pause('committed')))
    """
)


def test_kill_the_process_20_times(volumes, card, tmp_path):
    """每轮：新图库 → 子进程导入中途 SIGKILL（隔 4 轮再杀一次）→ 检查无残缺 → 插回继续 → 总数和哈希全对。"""
    src = card_hashes(card)
    (c,) = find_cards([volumes])
    terminated_pids = []

    def kill_once(library: Path, stage: str) -> bool:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", DRIVER, str(volumes), str(library), stage], stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )
        ready, lines = threading.Event(), []

        def read_marker():
            lines.append(proc.stdout.readline())
            ready.set()

        reader = threading.Thread(target=read_marker, daemon=True)
        reader.start()
        actual_pid = None
        try:
            assert ready.wait(60), "子进程没有到达指定导入阶段"
            marker = lines[0].decode("ascii").strip().split(":")
            assert marker[:2] == ["ready", stage] and len(marker) == 3, lines
            actual_pid = int(marker[2])
            if sys.platform != "win32":
                assert actual_pid == proc.pid
            assert proc.poll() is None
        finally:
            if actual_pid is not None and sys.platform == "win32":
                os.kill(actual_pid, signal.SIGTERM)
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
            reader.join(timeout=10)
            proc.stdin.close()
            proc.stdout.close()
        assert not reader.is_alive()
        terminated_pids.append(actual_pid)
        return True

    def no_partial_files(library: Path, tag: str):
        for p in library.rglob("*"):
            if p.is_file() and p.suffix.lower() in (".dng", ".jpg") and ".studio" not in p.parts:
                assert sha256_file(p) in src, f"{tag}：库里出现残缺文件 {p}"

    killed = 0
    partial_counts = []
    for i in range(20):
        library = tmp_path / f"kill-lib-{i}"
        killed += kill_once(library, "verify" if i % 2 else "committed")
        if i % 4 == 0:
            killed += kill_once(library, "verify")
        no_partial_files(library, f"第 {i} 轮")
        lib = Library(library).open()
        assert not [p for p in library.rglob("*.tmp") if p.is_file()], "重开图库后不能留下临时文件"
        try:
            partial_counts.append(lib.count())
            p = run_import(lib, c, ImportOptions(**OPTS))
            assert p.state == "done"
            assert lib.count() == len(card.unique_shots)
            assert_library_consistent(lib, card)
            assert {sha256_file(x) for x in library_media(lib)} == set(src)
            if i == 19:  # 索引可以完全重建，且与现状一致
                before = [(s["shot_id"], s["folder"], s["name"]) for s in lib.list_shots(limit=1000)]
                lib.rebuild(verify=True)
                assert [(s["shot_id"], s["folder"], s["name"]) for s in lib.list_shots(limit=1000)] == before
        finally:
            lib.close()
    assert killed == len(terminated_pids) == 25, f"只有 {killed} 次终止了运行中的进程"
    print(json.dumps({"terminated_worker_pids": terminated_pids, "rounds": 20}))
    assert any(0 < n < len(card.unique_shots) for n in partial_counts), "至少要有几次是在导到一半时被杀"


# ---- 验收 5：真实样本 -------------------------------------------------------


def test_real_samples_four_pairs(volumes, real_card, lib):
    c = one_card(volumes)
    p = run_import(lib, c, ImportOptions(**OPTS))
    real = [s for s in real_card.shots if s.real]
    assert len(real) == 4
    shots = {s["name"]: s for s in lib.list_shots(limit=1000)}
    for s in real:
        sh = shots[s.stem]
        assert set(sh["files"]) == {"dng", "jpg"}
        assert (sh["width"], sh["height"]) == (4288, 2848) and sh["unit"] == "50mm"
        for kind in ("dng", "jpg"):
            assert sh["files"][kind]["sha256"] == sha256_file(real_card.path(s, kind))
            assert sha256_file(lib.abspath(sh["files"][kind]["relpath"])) == sh["files"][kind]["sha256"]
    assert p.state == "done" and p.verify_failed == [] and p.unreadable == []
    assert_library_consistent(lib, real_card)


# ---- 索引重建 ---------------------------------------------------------------


def test_index_is_fully_rebuildable(volumes, card, lib):
    run_import(lib, one_card(volumes), ImportOptions(**OPTS))
    before = lib.list_shots(limit=1000)
    dates_before = lib.dates()
    lib.close()
    lib.index_path.unlink()  # 整个删掉
    lib2 = Library(lib.root).open()
    try:
        assert lib2.count() == 0
        rep = lib2.rebuild()
        assert rep.shots == len(before) and rep.hashed == 0, "有 sidecar 时不需要重新读大文件"
        assert lib2.list_shots(limit=1000) == before
        assert lib2.dates() == dates_before
    finally:
        lib2.close()
        lib.open()  # 让 fixture 的 close 不出错


def test_rebuild_adopts_loose_files_and_detects_corruption(volumes, card, lib):
    run_import(lib, one_card(volumes), ImportOptions(**OPTS))
    # 用户自己丢进来一张没有 sidecar 的照片
    src = card.path(card.shots[0], "jpg")
    (lib.root / "2026" / "2026-10-16" / "MANUAL.JPG").write_bytes(src.read_bytes() + b"x")
    rep = lib.rebuild()
    assert rep.sidecars_created == 1 and lib.count() == len(card.unique_shots) + 1
    # 改坏一个文件：verify 能发现
    victim = library_media(lib)[0]
    victim.write_bytes(victim.read_bytes()[:-1] + b"\x00")
    rep2 = lib.rebuild(verify=True)
    assert rep2.mismatches
