"""平台层和按目录句柄的边界（复审 r1 P1-04，复审 r2 P1-01 / R2-P1-01 / R2-P1-02）。

私有目录、读卡、照片文件在两个平台上是同一套代码，只有 `osplat` 里的原语不同（POSIX：dir_fd + O_NOFOLLOW；
Windows：以父目录句柄为根的 NtCreateFile + 拒绝重解析点、按句柄删除/改名/列目录）。这里的竞态在 macOS/Linux 上跑 POSIX 原语，
在 CI 的 windows-latest 上跑 Windows 原语（`os.symlink` 在 runner 上可用），断言相同：

- 删除时父目录被换成链接：外部文件不会被删（复审 r2 `path_delete_race.py`）；
- 严格扫描列目录前目录被换成链接：不会读到、列出外部文件（复审 r2 `card_reader_retarget.py`）；
- 打开照片时图库根或它的上级被换成链接：拒绝，不返回外部字节（复审 r2 `media_root_race.py`）；
- 写入、读取时父目录被换成链接：写不到外面、读不到外面；
- 两个进程同时整体替换同一个文件、锁冲突、fd → 真实路径。
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from gxr_studio import osplat, privacy
from gxr_studio.cards.discovery import CardReader, scan_card_strict
from gxr_studio.library import media


def _swap_for_link(folder: str, elsewhere: str, saved: str) -> None:
    """把 `folder` 挪到 `saved`，原位置放一个指向 `elsewhere` 的目录链接。"""
    os.rename(folder, saved)
    os.symlink(elsewhere, folder, target_is_directory=True)


def _hooked(monkeypatch, primitive, names, action):
    """第一次调用平台原语 `osplat.<primitive>(dfd, name, …)` 且 `name in names` 时，先做 `action()` 再真正执行。
    挂在原语上而不是 `os.*` 上：Windows 的原语不经过 `os.open` / `os.unlink`，两个平台都要触发。"""
    real, fired = getattr(osplat, primitive), []

    def hooked(dfd, name, *args, **kw):
        if not fired and name in names:
            fired.append(1)
            action()
        return real(dfd, name, *args, **kw)

    monkeypatch.setattr(osplat, primitive, hooked)
    return fired


@pytest.fixture
def home_and_elsewhere(tmp_path):
    home = tmp_path / "app-data"
    (home / "a" / "b").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    return home, elsewhere


# ---- 私有目录 ------------------------------------------------------------------------------


def test_delete_after_the_parent_became_a_link_never_touches_the_outside(home_and_elsewhere, monkeypatch):
    """复审 r2 R2-P1-01：真正删除的那一刻把 a/b 换成指向外面的链接。删除绑定的是已经打开的父目录句柄。"""
    home, elsewhere = home_and_elsewhere
    (home / "a/b/victim.txt").write_bytes(b"OWN DATA")
    (elsewhere / "victim.txt").write_bytes(b"UNRELATED USER DATA")
    folder, saved, away = os.fspath(home / "a/b"), os.fspath(home / "a/b.saved"), os.fspath(elsewhere)
    fired = _hooked(monkeypatch, "unlink_at", ("victim.txt",), lambda: _swap_for_link(folder, away, saved))
    d = privacy.PrivateDir(home, "test")
    try:
        try:
            d.remove("a/b/victim.txt")
        except (OSError, privacy.PrivatePathError):
            pass  # 拒绝也可以；不可以的是删到外面去
    finally:
        d.close()
        monkeypatch.undo()
    assert fired
    assert (elsewhere / "victim.txt").read_bytes() == b"UNRELATED USER DATA"


def test_delete_when_the_parent_is_already_a_link_is_refused(home_and_elsewhere):
    home, elsewhere = home_and_elsewhere
    (elsewhere / "victim.txt").write_bytes(b"UNRELATED USER DATA")
    _swap_for_link(os.fspath(home / "a/b"), os.fspath(elsewhere), os.fspath(home / "a/b.saved"))
    with privacy.PrivateDir(home, "test") as d:
        with pytest.raises(OSError):
            d.remove("a/b/victim.txt")
    assert (elsewhere / "victim.txt").read_bytes() == b"UNRELATED USER DATA"


def test_rmtree_deletes_links_themselves_not_their_targets(home_and_elsewhere):
    home, elsewhere = home_and_elsewhere
    (elsewhere / "keep.txt").write_bytes(b"KEEP")
    (home / "a/b/own.txt").write_bytes(b"own")
    os.symlink(os.fspath(elsewhere), os.fspath(home / "a/b/link"), target_is_directory=True)
    with privacy.PrivateDir(home, "test") as d:
        d.remove("a")
    assert not (home / "a").exists() and (elsewhere / "keep.txt").read_bytes() == b"KEEP"


def test_write_and_read_stay_inside_when_the_parent_becomes_a_link(home_and_elsewhere, monkeypatch):
    home, elsewhere = home_and_elsewhere
    (home / "a/b/old.bin").write_bytes(b"own")
    (elsewhere / "old.bin").write_bytes(b"somebody else's")
    folder, saved, away = os.fspath(home / "a/b"), os.fspath(home / "a/b.saved"), os.fspath(elsewhere)
    fired = _hooked(monkeypatch, "open_file_at", ("old.bin", "new.bin"), lambda: _swap_for_link(folder, away, saved))
    d = privacy.PrivateDir(home, "test")
    try:
        got = d.read("a/b/old.bin")  # 句柄是换之前打开的 a/b：读到的是自己的文件
        assert got == b"own" and fired
        fired.clear()
        os.unlink(folder)
        os.rename(saved, folder)
        try:
            d.write("a/b/new.bin", b"private bytes")
        except (OSError, privacy.PrivatePathError):
            pass
    finally:
        d.close()
        monkeypatch.undo()
    assert fired
    assert not (elsewhere / "new.bin").exists()  # 写不到外面去


# ---- 读卡的严格扫描 ----------------------------------------------------------------------------


def test_strict_scan_lists_the_directory_it_opened(tmp_path):
    """复审 r2 R2-P1-02：严格扫描拿到 DCIM/100RICOH 的句柄之后，目录被换成指向外面的链接。之后的列目录、
    看属性、读开头都绑定这个句柄：不读外部字节，不把外部文件当照片。"""
    root = tmp_path / "synthetic-card"
    folder = root / "DCIM" / "100RICOH"
    folder.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (folder / "OWN.JPG").write_bytes(b"\xff\xd8\xff OWN SYNTHETIC BYTES")
    (outside / "OUTSIDE.JPG").write_bytes(b"\xff\xd8\xff OUTSIDE SYNTHETIC BYTES")
    (folder / ".DS_Store").write_bytes(b"OWN SYNTHETIC METADATA")
    (outside / ".DS_Store").write_bytes(b"OUTSIDE SYNTHETIC METADATA")
    saved = os.fspath(folder.with_name("100RICOH.saved"))

    class Replacing(CardReader):
        def __init__(self):
            self.watched, self.swapped, self.reads = None, False, []

        def open_dir(self, name, dir_fd=None):
            fd = super().open_dir(name, dir_fd)
            if str(name) == "100RICOH":
                self.watched = fd
            return fd

        def listdir_fd(self, fd):
            if fd == self.watched and not self.swapped:
                _swap_for_link(os.fspath(folder), os.fspath(outside), saved)
                self.swapped = True
            return super().listdir_fd(fd)

        def read_head(self, name, dir_fd, *args, **kwargs):
            data = super().read_head(name, dir_fd, *args, **kwargs)
            self.reads.append(data)
            return data

    reader = Replacing()
    st = root.stat()
    result = scan_card_strict(root, reader, expect=(st.st_dev, st.st_ino))
    assert reader.swapped
    assert not any(b"OUTSIDE" in r for r in reader.reads)
    names = [dataclasses.asdict(e)["relpath"] for e in result.photos["jpg"]]
    assert names == ["DCIM/100RICOH/OWN.JPG"], names


def test_card_file_open_refuses_a_linked_folder(tmp_path):
    root = tmp_path / "card"
    (root / "DCIM").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "X.JPG").write_bytes(b"outside")
    os.symlink(os.fspath(outside), os.fspath(root / "DCIM" / "100RICOH"), target_is_directory=True)
    target = root / "DCIM" / "100RICOH" / "X.JPG"
    # 父目录和文件末级的链接都必须在实际读取时拒绝。
    os.symlink(os.fspath(outside / "X.JPG"), os.fspath(root / "DCIM" / "LINK.JPG"))
    with pytest.raises(OSError):
        CardReader().open_rb(root / "DCIM" / "LINK.JPG", root=root)
    with pytest.raises(OSError):
        CardReader().open_rb(target, root=root)


# ---- 照片文件 ------------------------------------------------------------------------------


def _library(tmp_path):
    root = tmp_path / "library"
    (root / "2026" / "11").mkdir(parents=True)
    (root / "2026/11/R0001.DNG").write_bytes(b"LIBRARY BYTES")
    outside = tmp_path / "outside"
    (outside / "2026" / "11").mkdir(parents=True)
    (outside / "2026/11/R0001.DNG").write_bytes(b"SYNTHETIC OUTSIDE BYTES")
    real = Path(os.path.realpath(root))
    lib = SimpleNamespace(root=root, is_local=lambda p: Path(os.path.realpath(p)) == real)
    return root, outside, lib


@pytest.mark.parametrize("which", ["root", "parent_of_root"])
def test_media_refuses_when_the_library_root_or_an_ancestor_becomes_a_link(tmp_path, monkeypatch, which):
    """复审 r2 P1-01：路径核对之后、真正打开之前，把图库根（或它的上级）换成指向外面的链接。"""
    if which == "root":
        root, outside, lib = _library(tmp_path)
        victim, elsewhere = root, outside
    else:
        nest = tmp_path / "nest"
        root, outside, lib = _library(nest)
        victim, elsewhere = nest, tmp_path / "outside-nest"
        os.rename(os.fspath(nest), os.fspath(elsewhere))
        os.makedirs(os.fspath(nest / "library" / "2026" / "11"))
        (nest / "library/2026/11/R0001.DNG").write_bytes(b"LIBRARY BYTES")
        (elsewhere / "library/2026/11/R0001.DNG").write_bytes(b"SYNTHETIC OUTSIDE BYTES")
    name, saved = victim.name, os.fspath(victim) + ".saved"
    fired = _hooked(monkeypatch, "open_dir_at", (name,), lambda: _swap_for_link(os.fspath(victim), os.fspath(elsewhere), saved))
    with pytest.raises(media.MediaRefused):
        fd = media.open_media(lib, "2026/11/R0001.DNG")
        os.close(fd)
    monkeypatch.undo()
    assert fired


def test_media_reads_through_the_handle_it_checked(tmp_path, monkeypatch):
    """根目录句柄打开、核对之后再被换掉：之后的读取仍然绑定这个句柄，读到的是图库里的字节。"""
    root, outside, lib = _library(tmp_path)
    saved = os.fspath(root) + ".saved"
    fired = _hooked(monkeypatch, "open_dir_at", ("2026",), lambda: _swap_for_link(os.fspath(root), os.fspath(outside), saved))
    fd = media.open_media(lib, "2026/11/R0001.DNG")
    try:
        assert media.read_all(fd) == b"LIBRARY BYTES"
    finally:
        os.close(fd)
        monkeypatch.undo()
    assert fired


def test_media_refuses_a_library_root_outside_the_allowed_place(tmp_path):
    root, _outside, _lib = _library(tmp_path)
    with pytest.raises(media.MediaRefused):
        media.open_media(SimpleNamespace(root=root, is_local=lambda _p: False), "2026/11/R0001.DNG")


# ---- 并发、锁、fd ------------------------------------------------------------------------------


WRITER = textwrap.dedent(
    """
    import sys
    from gxr_studio.privacy import PrivateDir
    home, tag, rounds = sys.argv[1], sys.argv[2], int(sys.argv[3])
    with PrivateDir(home, "test") as d:
        for i in range(rounds):
            d.replace_file("shared.json", (tag * 4096).encode())
    print("done", flush=True)
    """
)


def test_two_processes_replacing_the_same_file_leave_one_whole_copy(tmp_path):
    home = tmp_path / "app-data"
    home.mkdir()
    rounds = 40
    child = subprocess.Popen([sys.executable, "-c", WRITER, str(home), "B", str(rounds)], stdout=subprocess.PIPE, text=True)
    errors: list[BaseException] = []
    with privacy.PrivateDir(home, "test") as d:
        for _ in range(rounds):
            try:
                d.replace_file("shared.json", b"A" * 4096)
            except PermissionError as exc:  # Windows：对方正在替换的那一刻目标被占用
                errors.append(exc)
    out, _ = child.communicate(timeout=120)
    assert child.returncode == 0 and out.strip() == "done"
    data = (home / "shared.json").read_bytes()
    assert data in (b"A" * 4096, b"B" * 4096)  # 完整的一份，不是两份拼起来
    assert [p.name for p in home.iterdir()] == ["shared.json"]  # 没有留下临时文件
    assert len(errors) < rounds


def test_lock_conflicts_between_two_handles_and_releases(tmp_path):
    path = tmp_path / "x.lock"
    a = os.open(path, os.O_RDWR | os.O_CREAT | osplat.binary_flag(), 0o644)
    b = os.open(path, os.O_RDWR | osplat.binary_flag())
    try:
        osplat.lock_exclusive_nb(a)
        with pytest.raises(OSError):
            osplat.lock_exclusive_nb(b)
        osplat.unlock(a)
        osplat.lock_exclusive_nb(b)
        osplat.unlock(b)
    finally:
        os.close(a)
        os.close(b)


def test_handle_primitives(tmp_path):
    """原语本身：逐级打开、不跟随链接、列目录、排他新建、改名替换、删除；名字里不能带分隔符。"""
    (tmp_path / "d").mkdir()
    (tmp_path / "elsewhere").mkdir()
    os.symlink(os.fspath(tmp_path / "elsewhere"), os.fspath(tmp_path / "d" / "link"), target_is_directory=True)
    dfd = osplat.open_dir(os.path.realpath(tmp_path / "d"))
    try:
        assert os.path.normcase(os.path.realpath(osplat.fd_path(dfd))) == os.path.normcase(os.path.realpath(tmp_path / "d"))
        with pytest.raises(OSError):
            osplat.open_dir_at(dfd, "link")  # 链接不跟随
        assert osplat.is_link(osplat.lstat_at(dfd, "link"))
        osplat.mkdir_at(dfd, "sub")
        assert osplat.is_real_dir(osplat.lstat_at(dfd, "sub"))
        fd = osplat.open_file_at(dfd, "f.bin", create=True)
        os.write(fd, b"x")
        osplat.full_fsync(fd)
        os.close(fd)
        with pytest.raises(FileExistsError):
            osplat.open_file_at(dfd, "f.bin", create=True)
        fd = osplat.open_file_at(dfd, "g.bin", create=True)
        os.write(fd, b"y")
        os.close(fd)
        osplat.rename_at(dfd, "g.bin", "f.bin")  # 目标已存在：替换
        fd = osplat.open_file_at(dfd, "f.bin")
        assert os.read(fd, 8) == b"y"
        os.close(fd)
        with pytest.raises(OSError):
            osplat.open_file_at(dfd, "sub")  # 是目录
        assert sorted(osplat.listdir_fd(dfd)) == ["f.bin", "link", "sub"]
        osplat.unlink_at(dfd, "link")  # 删的是链接本身
        osplat.unlink_at(dfd, "f.bin")
        osplat.rmdir_at(dfd, "sub")
        assert osplat.listdir_fd(dfd) == []
        for bad in ("", ".", "..", "a/b", "a\\b"):
            with pytest.raises(OSError):
                osplat.open_file_at(dfd, bad)
    finally:
        os.close(dfd)
    assert (tmp_path / "elsewhere").is_dir()
    os.symlink(os.fspath(tmp_path / "elsewhere"), os.fspath(tmp_path / "via"), target_is_directory=True)
    with pytest.raises(OSError):  # 路径里有链接：从根开始逐级打开时失败
        osplat.open_dir(Path(os.path.realpath(tmp_path)) / "via")
    assert osplat.describe()["windows"] is (sys.platform == "win32")
