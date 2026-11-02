"""只读打开图库里登记过的照片文件，并按已打开的文件描述符流式发送（显卡预览的 /raw、/camera-jpg 和 RAW 布局共用）。

打开规则：
- `relpath` 必须是图库根下面的相对路径，不能有空段、`.`、`..`，也不能是绝对路径；
- 图库根按 realpath 确定，并核对它不在卡根或受保护目录里（`Library.is_local`）；
- POSIX：从图库根开始逐级 `O_DIRECTORY|O_NOFOLLOW` 打开目录，最后一级 `O_NOFOLLOW` 打开文件。任何一级是链接都失败，
  核对和打开是同一次操作，没有"核对完被换掉再按路径打开"的空隙；
- Windows（没有 dir_fd）：逐级 lstat，遇到链接或重解析点就拒绝；打开以后再用 fstat 的 (st_dev, st_ino) 与 lstat 结果比对，
  并核对 fd 的真实位置（GetFinalPathNameByHandleW）就是这个路径，中间被换掉就拒绝；
- 打开的必须是普通文件。

发送时只用这个 fd：支持单段 Range（206 / 416），按块读，结束后关闭。
"""

from __future__ import annotations

import errno
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from .. import osplat

CHUNK = 1 << 20
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)  # 打开时不被 FIFO 之类卡住；打开后再核对是不是普通文件
_RANGE = re.compile(r"bytes=(\d*)-(\d*)$")


class MediaRefused(Exception):
    """这个文件不能从图库读：路径不对、是链接、不在图库里、不是普通文件。"""


def _parts(relpath: str) -> list[str]:
    if not relpath or "\\" in relpath or "\0" in relpath:
        raise MediaRefused("bad_path")
    pure = PurePosixPath(relpath)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in relpath.split("/")):
        raise MediaRefused("bad_path")
    return list(pure.parts)


def _open_posix(root: Path, parts: list[str]) -> int:
    fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        for part in parts[:-1]:
            nfd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nfd
        return os.open(parts[-1], _FILE_FLAGS | _NONBLOCK, dir_fd=fd)
    finally:
        os.close(fd)


def _open_by_path(root: Path, parts: list[str]) -> int:
    """没有 dir_fd 的平台（Windows）：逐级 lstat 拒绝链接/重解析点，打开后核对身份没变。"""
    path = root
    for part in parts:
        path = path / part
        if osplat.is_link(os.lstat(path)):
            raise MediaRefused("link")
    before = os.lstat(path)
    fd = os.open(path, _FILE_FLAGS)
    try:
        after = os.fstat(fd)
        if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
            raise MediaRefused("replaced")
        # 中间某一级在 lstat 和打开之间被换成链接/目录联接：打开会跟着走，这里按 fd 的真实位置拦下
        now = osplat.fd_path(fd)
        if now is None or os.path.normcase(os.path.realpath(now)) != os.path.normcase(str(path)):
            raise MediaRefused("replaced")
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_media(lib: Any, relpath: str, *, use_dir_fd: bool | None = None) -> int:
    """返回只读 fd（调用方负责关闭）。不合规时抛 MediaRefused；文件不存在等抛 OSError。"""
    parts = _parts(relpath)
    root = Path(os.path.realpath(lib.root))
    if not lib.is_local(root):
        raise MediaRefused("outside")
    posix = (os.open in os.supports_dir_fd and sys.platform != "win32") if use_dir_fd is None else use_dir_fd
    try:
        fd = _open_posix(root, parts) if posix else _open_by_path(root, parts)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):  # 路径里某一级是链接（O_NOFOLLOW）
            raise MediaRefused("link") from exc
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise MediaRefused("not_a_file")
        if posix and hasattr(os, "set_blocking"):
            os.set_blocking(fd, True)
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_all(fd: int) -> bytes:
    """读完整个文件（从头），不关闭 fd。"""
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, CHUNK)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def parse_range(header: str | None, size: int) -> tuple[int, int] | None | str:
    """单段 Range → (起, 止含)；没有或看不懂 → None（发整个文件）；超出范围 → "unsatisfiable"。"""
    if not header:
        return None
    m = _RANGE.fullmatch(header.strip())
    if m is None:
        return None
    a, b = m.groups()
    if a == "" and b == "":
        return None
    if a == "":
        n = int(b)
        if n == 0:
            return "unsatisfiable"
        return max(0, size - n), size - 1
    start = int(a)
    end = size - 1 if b == "" else min(int(b), size - 1)
    if start >= size or start > end:
        return "unsatisfiable"
    return start, end


class FdResponse(Response):
    """按已打开的 fd 发送文件内容（拥有并最终关闭这个 fd）。"""

    def __init__(self, fd: int, *, media_type: str, headers: dict[str, str], range_header: str | None) -> None:
        self.fd = fd
        size = os.fstat(fd).st_size
        span = parse_range(range_header, size)
        out = {**headers, "Accept-Ranges": "bytes"}
        if span == "unsatisfiable":
            self.start, self.length = 0, 0
            super().__init__(status_code=416, headers={**out, "Content-Range": f"bytes */{size}"}, media_type=media_type)
            return
        if isinstance(span, tuple):
            self.start, self.length, status = span[0], span[1] - span[0] + 1, 206
            out["Content-Range"] = f"bytes {span[0]}-{span[1]}/{size}"
        else:
            self.start, self.length, status = 0, size, 200
        super().__init__(status_code=status, headers=out, media_type=media_type)
        self.headers["content-length"] = str(self.length)

    def _read(self, offset: int, n: int) -> bytes:
        os.lseek(self.fd, offset, os.SEEK_SET)
        return os.read(self.fd, n)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
            offset, left = self.start, self.length
            if left == 0:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
                return
            while left > 0:
                chunk = await run_in_threadpool(self._read, offset, min(CHUNK, left))
                if not chunk:
                    raise OSError("文件在发送途中变短了")
                offset += len(chunk)
                left -= len(chunk)
                await send({"type": "http.response.body", "body": chunk, "more_body": left > 0})
        finally:
            os.close(self.fd)
