"""挂载指纹不能被测试里改掉的 `sys.platform` 带偏（Linux CI 上 reveal 三个用例失败的根因，见 research/studio_a1a_r4_fix/REPORT.md）。

reveal 的测试曾用 `monkeypatch.setattr("gxr_studio.export.sys.platform", "darwin")` 钉住平台判断——这改的是整个进程的
`sys.platform`。在 Linux 上，`VolumeProbe.statfs` 于是按 macOS 的结构体去调 glibc 的 `statfs`，把 glibc 的
"空闲 inode 数"读成了 f_fsid；导入一写文件这个数就变，会话核对认为"换了挂载"，导入被中断，一张都没进图库。

这里在 macOS 上模拟那台 Linux：libc 的 `statfs` 按 glibc 的布局填结构体，`sys.platform` 被改成 "darwin"。
"""

from __future__ import annotations

import ctypes
import os
import struct
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from conftest import PORT
from gxr_studio.app import create_app
from gxr_studio.cards import session
from gxr_studio.cards.discovery import find_cards
from gxr_studio.cards.session import VolumeProbe
from test_api import find, import_all


def _glibc_statfs_like(watch: Path):
    """一个"Linux 上的 libc statfs"：按 glibc x86-64 的 struct statfs 填前 120 字节——
    f_type, f_bsize, f_blocks, f_bfree, f_bavail, f_files, f_ffree, f_fsid… 各 8 字节。
    f_ffree（偏移 48，正好是 macOS 结构体里 f_fsid 的位置）随 `watch` 下文件个数变化，就像真的空闲 inode 数。"""

    def fake(path: bytes, ref) -> int:  # 调用方传的是 ctypes.byref(buf)
        files = sum(len(fs) + len(ds) for _, ds, fs in os.walk(watch))
        raw = struct.pack("<qqQQQQQii", 0xEF53, 4096, 1 << 20, 1 << 19, 1 << 19, 1 << 16, (1 << 16) - files, 1234, 5678)
        ctypes.memmove(ctypes.addressof(ref._obj), raw, len(raw))
        return 0

    return fake


def test_patched_sys_platform_does_not_change_the_fingerprint(volumes, card, monkeypatch):
    """把 `sys.platform` 改成另一种系统：同一个挂载的指纹不变（statfs 的布局按真实宿主决定，不按 sys.platform）。"""
    (c,) = find_cards([volumes])
    probe = VolumeProbe()
    before = probe.fingerprint(str(c.root))
    monkeypatch.setattr(sys, "platform", "linux" if sys.platform == "darwin" else "darwin")
    probe2 = VolumeProbe()
    assert probe2.fingerprint(str(c.root)) == before
    assert probe.fingerprint(str(c.root)) == before


def test_linux_host_with_platform_patched_to_darwin_still_imports(settings, card, monkeypatch, tmp_path):
    """在 macOS 上复现 Linux CI：宿主不是 macOS、libc 的 statfs 是 glibc 布局、测试把 sys.platform 改成 darwin。
    导入必须照常完成（R0000102 进图库），核对结果也正常。"""
    monkeypatch.setattr(session, "HOST_IS_DARWIN", False, raising=False)  # 修复后：按真实宿主（这里模拟 Linux）选布局
    monkeypatch.setattr(session, "_darwin_statfs_fn", lambda: _glibc_statfs_like(tmp_path))
    monkeypatch.setattr(sys, "platform", "darwin")  # reveal 测试以前就是这样钉平台的
    app = create_app(settings)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        job = import_all(client)
        assert job["progress"]["state"] == "done", job["progress"]
        assert find(client, "R0000102")["name"] == "R0000102"
