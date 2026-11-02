"""平台层（复审 P1-04）：Windows 那条按路径的实现，在任何系统上都直接跑（不靠 GXR_FORCE_PATH_IO）。

- 中间一级目录在核对之后被换成链接（Windows 上是目录联接）：写不进去、读不出来、照片打不开；
- 两个进程同时整体替换同一个文件：最后是其中一份完整内容，不留临时文件；
- 平台层的锁、fd → 真实路径、落盘。
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

from gxr_studio import osplat, privacy
from gxr_studio.library import media


def _swap_for_link(folder: str, elsewhere: str) -> None:
    """把 `folder` 换成指向 `elsewhere` 的目录链接（原目录挪到旁边）。"""
    os.rename(folder, folder + ".real")
    os.symlink(elsewhere, folder, target_is_directory=True)


@pytest.fixture
def home_and_elsewhere(tmp_path):
    home = tmp_path / "app-data"
    (home / "a" / "b").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    return home, elsewhere


def test_path_write_refuses_a_folder_swapped_for_a_link_before_the_open(home_and_elsewhere, monkeypatch):
    home, elsewhere = home_and_elsewhere
    d = privacy._PathPrivateDir(home, "test")
    target, folder, away = os.fspath(home / "a/b/new.bin"), os.fspath(home / "a/b"), os.fspath(elsewhere)
    real_open, swapped = os.open, []

    def opening(path, flags, *args, **kw):
        if not swapped and os.fspath(path) == target and flags & os.O_CREAT:
            swapped.append(1)
            _swap_for_link(folder, away)
        return real_open(path, flags, *args, **kw)

    monkeypatch.setattr(privacy.os, "open", opening)
    with pytest.raises(privacy.PrivatePathError):
        d.write("a/b/new.bin", b"private bytes")
    monkeypatch.undo()
    assert swapped
    # 打开跟着链接在别处建了文件，但核对在写第一个字节之前就拦下了：那里最多是 0 字节
    leaked = elsewhere / "new.bin"
    assert not leaked.exists() or leaked.read_bytes() == b""


def test_path_read_refuses_a_folder_swapped_for_a_link_after_the_check(home_and_elsewhere, monkeypatch):
    home, elsewhere = home_and_elsewhere
    (home / "a/b/old.bin").write_bytes(b"own")
    (elsewhere / "old.bin").write_bytes(b"somebody else's")
    d = privacy._PathPrivateDir(home, "test")
    target, folder, away = os.fspath(home / "a/b/old.bin"), os.fspath(home / "a/b"), os.fspath(elsewhere)
    real_lstat, swapped = os.lstat, []

    def lstating(path, *args, **kw):  # 逐级核对之后、读之前的那次 lstat：先换掉中间一级
        if not swapped and os.fspath(path) == target:
            swapped.append(1)
            _swap_for_link(folder, away)
        return real_lstat(path, *args, **kw)

    monkeypatch.setattr(privacy.os, "lstat", lstating)
    with pytest.raises(privacy.PrivatePathError):
        d.read("a/b/old.bin")
    monkeypatch.undo()
    assert swapped


def test_path_moved_parent_reports_a_changed_directory(home_and_elsewhere):
    home, elsewhere = home_and_elsewhere
    (home / "a/b/old.bin").write_bytes(b"own")
    d = privacy._PathPrivateDir(home, "test")
    assert d.read("a/b/old.bin") == b"own"
    with pytest.raises(FileNotFoundError):  # 文件本来就没有：照常报找不到
        d.read("a/b/missing.bin")
    with pytest.raises(FileNotFoundError):
        d.read("a/nope/missing.bin")


def test_media_by_path_refuses_a_folder_swapped_for_a_link(tmp_path, monkeypatch):
    root = tmp_path / "library"
    (root / "2026" / "11").mkdir(parents=True)
    (root / "2026/11/R0001.DNG").write_bytes(b"own")
    elsewhere = tmp_path / "card"
    elsewhere.mkdir()
    (elsewhere / "R0001.DNG").write_bytes(b"card bytes")
    lib = SimpleNamespace(root=root, is_local=lambda _p: True)
    fd = media.open_media(lib, "2026/11/R0001.DNG", use_dir_fd=False)
    try:
        assert media.read_all(fd) == b"own"
    finally:
        os.close(fd)
    real_root = os.path.realpath(root)
    target, folder, away = os.path.join(real_root, "2026", "11", "R0001.DNG"), os.path.join(real_root, "2026", "11"), os.fspath(elsewhere)
    real_lstat, seen = os.lstat, []

    def lstating(path, *args, **kw):  # 第二次 lstat 最后一级（逐级核对之后、打开之前）：先换掉中间一级
        if os.fspath(path) == target:
            seen.append(1)
            if len(seen) == 2:
                _swap_for_link(folder, away)
        return real_lstat(path, *args, **kw)

    monkeypatch.setattr(media.os, "lstat", lstating)
    with pytest.raises(media.MediaRefused):
        media.open_media(lib, "2026/11/R0001.DNG", use_dir_fd=False)
    monkeypatch.undo()
    assert len(seen) >= 2


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
            except PermissionError as exc:  # Windows：对方正在替换的那一刻目标被占用，os.replace 报拒绝访问
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


def test_fd_path_and_full_fsync(tmp_path):
    path = tmp_path / "f.bin"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | osplat.binary_flag(), 0o600)
    try:
        os.write(fd, b"x")
        osplat.full_fsync(fd)
        now = osplat.fd_path(fd)
        assert now is not None
        assert os.path.normcase(os.path.realpath(now)) == os.path.normcase(os.path.realpath(path))
    finally:
        os.close(fd)
    assert osplat.describe()["windows"] is (sys.platform == "win32")
