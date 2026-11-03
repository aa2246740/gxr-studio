"""Native Windows checks for the handle backend's remaining contracts."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from gxr_studio import osplat

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows native handle checks")


@pytest.mark.parametrize("fallback", [False, True])
def test_rename_destination_uses_the_open_parent(tmp_path, monkeypatch, fallback):
    root, saved, outside = (tmp_path / name for name in ("root", "saved", "outside"))
    root.mkdir()
    outside.mkdir()
    (root / "source.bin").write_bytes(b"own source")
    (root / "target.bin").write_bytes(b"own target")
    (outside / "target.bin").write_bytes(b"other target")
    dfd = osplat.open_dir(root)
    real_set = osplat._set_info
    calls = []

    def setting(handle, cls, data):
        calls.append((handle, cls))
        if fallback and cls == osplat._FileRenameInformationEx:
            return -1073741821  # STATUS_INVALID_INFO_CLASS
        return real_set(handle, cls, data)

    monkeypatch.setattr(osplat, "_set_info", setting)
    try:
        root.rename(saved)
        root.symlink_to(outside, target_is_directory=True)
        osplat.rename_at(dfd, "source.bin", "target.bin")
    finally:
        os.close(dfd)
    assert (saved / "target.bin").read_bytes() == b"own source"
    assert not (saved / "source.bin").exists()
    assert (outside / "target.bin").read_bytes() == b"other target"
    if fallback:
        assert [cls for _, cls in calls] == [osplat._FileRenameInformationEx, osplat._FileRenameInformation]
        assert calls[0][0] == calls[1][0]


@pytest.mark.parametrize("directory", [False, True])
def test_delete_fallback_keeps_the_same_open_object(tmp_path, monkeypatch, directory):
    root, saved, outside = (tmp_path / name for name in ("root", "saved", "outside"))
    root.mkdir()
    outside.mkdir()
    if directory:
        (root / "entry").mkdir()
        (outside / "entry").mkdir()
        (outside / "entry/keep").write_bytes(b"keep")
    else:
        (root / "entry").write_bytes(b"own")
        (outside / "entry").write_bytes(b"keep")
    dfd = osplat.open_dir(root)
    real_set = osplat._set_info
    calls = []

    def setting(handle, cls, data):
        calls.append((handle, cls))
        if cls == osplat._FileDispositionInformationEx:
            return -1073741821  # STATUS_INVALID_INFO_CLASS
        return real_set(handle, cls, data)

    monkeypatch.setattr(osplat, "_set_info", setting)
    try:
        root.rename(saved)
        root.symlink_to(outside, target_is_directory=True)
        (osplat.rmdir_at if directory else osplat.unlink_at)(dfd, "entry")
    finally:
        os.close(dfd)
    assert not (saved / "entry").exists()
    assert (outside / ("entry/keep" if directory else "entry")).read_bytes() == b"keep"
    assert [cls for _, cls in calls] == [osplat._FileDispositionInformationEx, osplat._FileDispositionInformation]
    assert calls[0][0] == calls[1][0]


def test_directory_records_span_multiple_buffers_and_restart(tmp_path):
    root = tmp_path / "records"
    root.mkdir()
    names = [f"entry_{i:04d}_长文件名_\U0001f4f7_" + "x" * 70 for i in range(700)]
    for name in names:
        (root / name).touch()
    dfd = osplat.open_dir(root)
    try:
        for _ in range(2):
            actual = osplat.listdir_fd(dfd)
            assert len(actual) == len(names)
            assert set(actual) == set(names)
    finally:
        os.close(dfd)


def test_relative_lstat_identity_matches_path_stat(tmp_path):
    (tmp_path / "folder").mkdir()
    (tmp_path / "file.bin").write_bytes(b"binary\r\n\x1a")
    (tmp_path / "link").symlink_to(tmp_path / "folder", target_is_directory=True)
    dfd = osplat.open_dir(tmp_path)
    try:
        fst, pst = os.fstat(dfd), tmp_path.stat()
        assert (fst.st_dev, fst.st_ino) == (pst.st_dev, pst.st_ino)
        for name in ("folder", "file.bin", "link"):
            actual = osplat.lstat_at(dfd, name)
            expected = (tmp_path / name).lstat()
            assert (actual.st_dev, actual.st_ino, actual.st_size) == (expected.st_dev, expected.st_ino, expected.st_size)
            assert osplat.is_link(actual) == (name == "link")
    finally:
        os.close(dfd)


def test_real_directory_junction_is_refused_and_removed_as_a_link(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_bytes(b"keep")
    root = tmp_path / "root"
    root.mkdir()
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "junction"), str(outside)], capture_output=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    dfd = osplat.open_dir(root)
    try:
        assert osplat.is_link(osplat.lstat_at(dfd, "junction"))
        with pytest.raises(OSError):
            osplat.open_dir_at(dfd, "junction")
        osplat.unlink_at(dfd, "junction")
        assert osplat.listdir_fd(dfd) == []
    finally:
        os.close(dfd)
    assert (outside / "keep").read_bytes() == b"keep"


def test_change_time_tracks_writes_when_mtime_is_restored(tmp_path):
    import ctypes

    assert ctypes.sizeof(osplat._FileBasicInfo) == 40
    assert osplat._FileBasicInfo.ChangeTime.offset == 24
    path = tmp_path / "changing.bin"
    path.write_bytes(b"old data")
    original = path.stat()
    before = osplat.lstat(path)
    assert abs(before.st_ctime_ns - time.time_ns()) < 10_000_000_000
    for index in range(50):
        path.write_bytes(bytes([index]) * 8)
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        after = osplat.lstat(path)
        if after.st_ctime_ns != before.st_ctime_ns:
            break
    assert after.st_ctime_ns > before.st_ctime_ns
    assert after.st_mtime_ns == before.st_mtime_ns
    assert after.st_ino == before.st_ino and after.st_dev == before.st_dev
    assert after.st_birthtime_ns == before.st_birthtime_ns
