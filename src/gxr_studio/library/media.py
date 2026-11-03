"""只读打开图库里登记过的照片文件，并按已打开的文件描述符流式发送（显卡预览的 /raw、/camera-jpg 和 RAW 布局共用）。

打开规则：
- `relpath` 必须是图库根下面的相对路径，不能有空段、`.`、`..`，也不能是绝对路径；
- 图库根取 realpath 以后，从文件系统根开始逐级不跟随链接地打开（`osplat.open_dir`：根目录本身和它的每一级祖先都算在内），
  任何一级在这之间被换成链接都失败；
- 位置核对作用在已经打开的根目录句柄上：句柄的真实位置必须就是那个 realpath，而且在图库允许的位置（不在卡上、
  不在受保护目录里，`Library.is_local`）。核对和之后的读取用的是同一个句柄，没有"按路径检查、再按路径打开"的空隙；
- 再相对这个句柄逐级不跟随链接地打开子目录和文件（`osplat.open_dir_at` / `open_file_at`）；打开的必须是普通文件。
两个平台同一套实现：POSIX 是 `dir_fd` + `O_NOFOLLOW`，Windows 是以父目录句柄为根的相对打开 + 拒绝重解析点。

发送时只用这个 fd：支持单段 Range（206 / 416），数字最多 20 位，按块读，结束或构造失败时关闭。
"""

from __future__ import annotations

import errno
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from .. import osplat

CHUNK = 1 << 20
RANGE_DIGITS = 20  # 远超任何文件大小；更长的数字不转换（复审 r2 P2-01：5,000 位数字让 int() 抛错）
_RANGE = re.compile(r"bytes=(\d{0,%d})-(\d{0,%d})$" % (RANGE_DIGITS, RANGE_DIGITS))


class MediaRefused(Exception):
    """这个文件不能从图库读：路径不对、是链接、不在图库里、不是普通文件。"""


def _parts(relpath: str) -> list[str]:
    if not relpath or "\\" in relpath or "\0" in relpath:
        raise MediaRefused("bad_path")
    pure = PurePosixPath(relpath)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in relpath.split("/")):
        raise MediaRefused("bad_path")
    return list(pure.parts)


def _same_place(fd: int, root: Path) -> Path | None:
    now = osplat.fd_path(fd)
    if now is None:
        return None
    real = Path(os.path.realpath(now))
    return real if os.path.normcase(str(real)) == os.path.normcase(str(root)) else None


def open_media(lib: Any, relpath: str) -> int:
    """返回只读 fd（调用方负责关闭）。不合规时抛 MediaRefused；文件不存在等抛 OSError。"""
    parts = _parts(relpath)
    root = Path(os.path.realpath(lib.root))
    try:
        dfd = osplat.open_dir(root)  # 根目录和它的每一级祖先都不跟随链接
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise MediaRefused("link") from exc
        raise
    fd = -1
    try:
        here = _same_place(dfd, root)
        if here is None or not lib.is_local(here):  # 核对的是已经打开的这个根目录
            raise MediaRefused("outside")
        for part in parts[:-1]:
            nfd = osplat.open_dir_at(dfd, part)
            os.close(dfd)
            dfd = nfd
        fd = osplat.open_file_at(dfd, parts[-1])
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise MediaRefused("not_a_file")
    except OSError as exc:
        if fd >= 0:
            os.close(fd)
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR):  # 路径里某一级是链接，或不是普通文件
            raise MediaRefused("link" if exc.errno != errno.EISDIR else "not_a_file") from exc
        raise
    except BaseException:
        if fd >= 0:
            os.close(fd)
        raise
    finally:
        os.close(dfd)
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
    """按已打开的 fd 发送文件内容。拥有这个 fd：构造失败时立刻关闭，否则发送结束（或中途出错）时关闭。"""

    def __init__(self, fd: int, *, media_type: str, headers: dict[str, str], range_header: str | None) -> None:
        self.fd = fd
        try:
            self._prepare(media_type, headers, range_header)
        except BaseException:
            os.close(fd)  # 构造失败时 __call__ 不会运行：fd 在这里还掉（复审 r2 P2-01）
            raise

    def _prepare(self, media_type: str, headers: dict[str, str], range_header: str | None) -> None:
        size = os.fstat(self.fd).st_size
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
