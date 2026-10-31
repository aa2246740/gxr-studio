"""挂载会话：接口里的 `card_id` 就是它（设计 §4.1）。

三层身份分开用：

- **会话号**：工作室每发现一次挂载就生成一个随机号（128 位，Crockford Base32，26 个字符），只在服务器内存里，
  映射到 `{挂载路径, 根目录 dev/ino, f_fsid, f_mntfromname, 卷 UUID, 卷标}`。挂载消失或指纹变了就作废，重启也作废；
- **卷 UUID**：`diskutil info -plist <挂载点>` 的完整 `VolumeUUID`，只用来跨插拔关联历史（seen2）。
  取不到是 `missing`，两个当前会话相同是 `ambiguous`；这两种都不关联历史。它不代表哪张物理卡；
- **卷标**：只用来显示。

所有带 `card_id` 的接口都经过 `SessionRegistry.resolve()`，不再用 `next(...)` 取第一张。
"""

from __future__ import annotations

import ctypes
import os
import plistlib
import re
import secrets
import stat
import subprocess  # nosec B404  只用来以参数数组调用 /usr/sbin/diskutil，没有 shell
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import discovery

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
SESSION_ID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
UUID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")
DISKUTIL = "/usr/sbin/diskutil"
DISKUTIL_TIMEOUT = 5.0

UUID_OK = "ok"
UUID_MISSING = "missing"
UUID_AMBIGUOUS = "ambiguous"

NOT_FOUND = "找不到存储卡。请重新插入后重试。"
AMBIGUOUS = "无法区分这两张存储卡。请只插入一张。"


class SessionError(Exception):
    """解析 card_id 失败。status/code 直接对应接口的错误。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def new_session_id() -> str:
    """128 位随机数的 Crockford Base32（26 个字符，最高位补两个 0）。"""
    n = secrets.randbits(128)
    return "".join(CROCKFORD[(n >> (5 * i)) & 31] for i in reversed(range(26)))


# ---- 卷信息 ------------------------------------------------------------------


@dataclass(frozen=True)
class Fingerprint:
    """会话核对用的挂载指纹：任何一项变了，这个会话就作废。"""

    mount_path: str
    root_dev: int
    root_ino: int
    fsid: tuple[int, int]
    mntfromname: str


@dataclass(frozen=True)
class StatFs:
    fsid: tuple[int, int]
    fstype: str
    mntonname: str
    mntfromname: str


class _DarwinStatfs(ctypes.Structure):
    # <sys/mount.h> 的 64 位 inode 版 struct statfs（arm64 和 x86_64 的 statfs$INODE64 相同）
    _fields_ = [
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", ctypes.c_int32 * 2),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * 16),
        ("f_mntonname", ctypes.c_char * 1024),
        ("f_mntfromname", ctypes.c_char * 1024),
        ("f_flags_ext", ctypes.c_uint32),
        ("f_reserved", ctypes.c_uint32 * 7),
    ]


def _darwin_statfs_fn() -> Any:
    libc = ctypes.CDLL(None, use_errno=True)
    for name in ("statfs$INODE64", "statfs"):
        try:
            fn = getattr(libc, name)
        except AttributeError:
            continue
        fn.argtypes = [ctypes.c_char_p, ctypes.POINTER(_DarwinStatfs)]
        fn.restype = ctypes.c_int
        return fn
    return None


class VolumeProbe:
    """读挂载指纹和卷 UUID。测试继承它来模拟换卡、UUID 缺失或重复、diskutil 超时。"""

    def __init__(self, runner: Callable[..., Any] | None = None) -> None:
        self.runner = runner or subprocess.run
        self._statfs_fn: Any = None

    def statfs(self, path: str) -> StatFs:
        if sys.platform == "darwin":
            if self._statfs_fn is None:
                self._statfs_fn = _darwin_statfs_fn()
            if self._statfs_fn is not None:
                buf = _DarwinStatfs()
                if self._statfs_fn(os.fsencode(path), ctypes.byref(buf)) != 0:
                    err = ctypes.get_errno()
                    raise OSError(err, os.strerror(err), path)
                return StatFs(
                    (int(buf.f_fsid[0]), int(buf.f_fsid[1])),
                    buf.f_fstypename.decode("utf-8", "replace"),
                    os.fsdecode(buf.f_mntonname),
                    os.fsdecode(buf.f_mntfromname),
                )
        st = os.statvfs(path)
        fsid = int(getattr(st, "f_fsid", 0))
        return StatFs((fsid & 0xFFFFFFFF, fsid >> 32), "", "", "")

    def fingerprint(self, path: str) -> Fingerprint | None:
        """挂载路径现在的指纹；不存在、是链接或不是目录时返回 None（会话作废，不重建目录）。"""
        try:
            st = os.lstat(path)
            if not stat.S_ISDIR(st.st_mode):
                return None
            fs = self.statfs(path)
        except OSError:
            return None
        return Fingerprint(path, st.st_dev, st.st_ino, fs.fsid, fs.mntfromname)

    def volume_uuid(self, path: str) -> str | None:
        """只对真正的挂载点（statfs 的 f_mntonname 就是这个路径）调用 diskutil；参数数组，超时 5 秒。
        失败、超时、空值、格式不对、plist 里的挂载点对不上，一律返回 None（uuid_missing）。"""
        try:
            fs = self.statfs(path)
        except OSError:
            return None
        if not fs.mntonname or os.path.realpath(fs.mntonname) != os.path.realpath(path):
            return None
        try:
            out = self.runner(
                [DISKUTIL, "info", "-plist", fs.mntonname],
                capture_output=True,
                timeout=DISKUTIL_TIMEOUT,
                check=False,
            )  # nosec B603  固定的绝对路径命令，挂载点来自 statfs，不来自请求
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        if getattr(out, "returncode", 1) != 0:
            return None
        try:
            info = plistlib.loads(out.stdout)
        except (plistlib.InvalidFileException, ValueError, TypeError):
            return None
        if not isinstance(info, dict):
            return None
        mount = info.get("MountPoint")
        if not isinstance(mount, str) or os.path.realpath(mount) != os.path.realpath(path):
            return None
        value = info.get("VolumeUUID")
        if not isinstance(value, str) or not UUID_RE.match(value.strip()):
            return None
        return value.strip().upper()


# ---- 会话 --------------------------------------------------------------------


@dataclass
class MountSession:
    session_id: str
    card: discovery.Card
    fingerprint: Fingerprint
    volume_uuid: str | None
    discovered_at: str
    uuid_state: str = UUID_MISSING
    probe: VolumeProbe | None = field(default=None, repr=False, compare=False)

    def still_valid(self) -> bool:
        """完整指纹（挂载路径的 dev/ino、f_fsid、f_mntfromname）还和发现时一样吗。导入在读取边界和发布结果前都问一次。"""
        return self.probe is not None and self.probe.fingerprint(self.fingerprint.mount_path) == self.fingerprint

    @property
    def root(self) -> Path:
        return self.card.root

    @property
    def label(self) -> str:
        return self.card.label

    @property
    def history_uuid(self) -> str | None:
        """能用来关联跨插拔历史（seen2）的完整 UUID：只有状态是 ok 时才有。"""
        return self.volume_uuid if self.uuid_state == UUID_OK else None

    def public(self) -> dict[str, Any]:
        return {"id": self.session_id, "label": self.label, "uuid_state": self.uuid_state}


@dataclass
class SessionRegistry:
    probe: VolumeProbe = field(default_factory=VolumeProbe)
    id_factory: Callable[[], str] = new_session_id
    _sessions: dict[str, MountSession] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def _new_id(self) -> str:
        for _ in range(16):
            sid = self.id_factory()
            if SESSION_ID_RE.match(sid) and sid not in self._sessions:
                return sid
        raise RuntimeError("无法生成不重复的会话号")

    def attach(self, card: discovery.Card) -> MountSession | None:
        """发现一张卡：同一个挂载路径、指纹没变就沿用原会话；否则旧会话作废，建新会话（重新读 UUID）。"""
        path = str(card.root)
        fp = self.probe.fingerprint(path)
        if fp is None:
            return None
        with self._lock:
            for sid, s in list(self._sessions.items()):
                if s.fingerprint.mount_path != path:
                    continue
                if s.fingerprint == fp:
                    if s.card.label != card.label:
                        s.card = card
                    self._recompute()
                    return s
                del self._sessions[sid]
        uuid = self.probe.volume_uuid(path)  # 可能要等 diskutil，放在锁外
        with self._lock:
            for s in self._sessions.values():
                if s.fingerprint == fp:  # 并发发现了同一个挂载
                    return s
            sid = self._new_id()
            session = MountSession(sid, card, fp, uuid, datetime.now().isoformat(timespec="seconds"), probe=self.probe)
            self._sessions[sid] = session
            self._recompute()
            return session

    def attach_all(self, cards: list[discovery.Card]) -> list[MountSession]:
        self.prune()
        out = [s for s in (self.attach(c) for c in cards) if s is not None]
        with self._lock:
            self._recompute()
        return out

    def prune(self) -> None:
        """指纹对不上（卷消失、换了卡、重新挂载）的会话全部作废。"""
        with self._lock:
            items = list(self._sessions.items())
        dead = [sid for sid, s in items if self.probe.fingerprint(s.fingerprint.mount_path) != s.fingerprint]
        with self._lock:
            for sid in dead:
                self._sessions.pop(sid, None)
            self._recompute()

    def _recompute(self) -> None:
        counts: dict[str, int] = {}
        for s in self._sessions.values():
            if s.volume_uuid:
                counts[s.volume_uuid] = counts.get(s.volume_uuid, 0) + 1
        for s in self._sessions.values():
            if not s.volume_uuid:
                s.uuid_state = UUID_MISSING
            elif counts[s.volume_uuid] > 1:
                s.uuid_state = UUID_AMBIGUOUS
            else:
                s.uuid_state = UUID_OK

    def resolve(self, card_id: str, *, write: bool = False) -> MountSession:
        """所有带 card_id 的接口共用：格式不对 400，作废或不存在 404，写入时身份歧义 409。

        只读操作（识别、导入照片、备份）在歧义时仍然可以做，但不关联 seen2（`history_uuid` 为 None）。"""
        if not isinstance(card_id, str) or not SESSION_ID_RE.match(card_id):
            raise SessionError(400, "bad_card_id", NOT_FOUND)
        self.prune()
        with self._lock:
            matches = [s for sid, s in self._sessions.items() if sid == card_id]
        if not matches:
            raise SessionError(404, "card_not_found", NOT_FOUND)
        if len(matches) > 1:
            raise SessionError(409, "identity_ambiguous", AMBIGUOUS)
        session = matches[0]
        if self.probe.fingerprint(session.fingerprint.mount_path) != session.fingerprint:
            with self._lock:
                self._sessions.pop(session.session_id, None)
            raise SessionError(404, "card_not_found", NOT_FOUND)
        if write and session.uuid_state == UUID_AMBIGUOUS:
            raise SessionError(409, "identity_ambiguous", AMBIGUOUS)
        return session

    def resolve_optional(self, card_id: str | None, candidates: list[MountSession], *, write: bool = False) -> MountSession:
        """可以不带 card_id 的接口：只有一个候选时用它；多个候选没给 id 一律 409 multiple_cards。"""
        if card_id is not None:
            return self.resolve(card_id, write=write)
        if len(candidates) > 1:
            raise SessionError(409, "multiple_cards", f"已插入 {len(candidates)} 张存储卡，请选择要使用的存储卡")
        if not candidates:
            raise SessionError(404, "card_not_found", NOT_FOUND)
        return self.resolve(candidates[0].session_id, write=write)
