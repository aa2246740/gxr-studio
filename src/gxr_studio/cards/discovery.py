"""发现存储卡、列出卡上的照片和录像、把 DNG 和 JPG 配成一对。

这个模块只做"读"：`os.scandir` / `stat`，不打开任何文件，更不写。
被发现的卡根目录会登记进审计钩子的保护表，之后任何写入都会被拒绝。

`scan_card` 给导入用；`scan_card_strict` 给照片核对用（设计 §4.4）：不跟随链接，任何错误都让结果不完整，
DCIM 里的每一项都列出来（包括点文件和非照片文件）。
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .. import audit, osplat

KINDS = {".dng": "dng", ".jpg": "jpg"}
# GXR 的录像：固件字符串里有 ".AVI" 和 RIFF 的 "avih"（research/color_injection/jlaunch3-strings.txt）。
# 原样复制，不解码。还没在真卡上见过录像文件，别的扩展名不猜。
VIDEO_KINDS = {".avi": "avi"}
# 卡根这一层可以不管的系统项（只认这些名字，只在卡根）。DCIM 里面没有任何例外。
ROOT_SYSTEM_ITEMS = frozenset({".fseventsd", ".Spotlight-V100", ".Trashes", ".TemporaryItems", ".DS_Store", ".metadata_never_index"})
# macOS 在访达里看过卡之后留下的元数据：DCIM 里出现它们不导入，也不挡"都已导入"。
# 这些目录里的一切、`._*`（AppleDouble）、下面列明的文件名，以及不在点目录里、扩展名不是照片/视频的点文件。
MAC_SYSTEM_DIRS = frozenset({".Spotlight-V100", ".Trashes", ".fseventsd", ".TemporaryItems"})
MAC_SYSTEM_FILES = frozenset({".DS_Store", ".metadata_never_index", ".localized", ".apdisk", ".VolumeIcon.icns"})
# 名字像系统文件还不够（复审 A1a P1-04）：`._*` 必须真是 AppleDouble/AppleSingle；能认出是照片或视频的内容一律不算
APPLEDOUBLE_MAGIC = (b"\x00\x05\x16\x07", b"\x00\x05\x16\x00")
HEAD_BYTES = 16
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


@dataclass(frozen=True)
class Card:
    root: Path
    label: str

    @property
    def key(self) -> str:
        """挂载目录名的哈希。两张卡复用同一个挂载目录名时它相同，所以只当提示用：决定先只算哈希还是直接复制，
        从不用来跳过读取，也不出现在接口里（接口用挂载会话号，见 `session.py`）。"""
        return hashlib.sha256(self.label.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class CardFile:
    relpath: str  # 相对卡根，用 / 分隔，保留卡上的大小写
    path: Path
    size: int
    mtime_ns: int
    kind: str  # dng | jpg | avi

    @property
    def stem(self) -> str:
        return Path(self.relpath).stem


@dataclass
class CardShot:
    """卡上一张照片（可能是 DNG+JPG 一对，也可能只有一个）。"""

    key: str  # 小写的 目录/主名
    directory: str
    stem: str
    files: dict[str, CardFile] = field(default_factory=dict[Any, Any])

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files.values())


class CardReader:
    """唯一允许碰卡文件的入口，只提供读。测试里可以继承它来模拟拔卡、读错。
    没有目录 fd 的平台（Windows）上构造出来的是 `PathCardReader`，接口相同。"""

    def __new__(cls, *args: Any, **kwargs: Any) -> "CardReader":
        if cls is CardReader and osplat.USE_PATH_IO:
            return super().__new__(PathCardReader)
        return super().__new__(cls)

    def scandir(self, path: Path) -> Any:
        return os.scandir(path)

    def open_rb(self, path: Path) -> Any:
        """只读打开；最后一级是链接时拒绝（O_NOFOLLOW），不会顺着链接读到卡外面去。"""
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        return os.fdopen(fd, "rb", buffering=0)

    def exists(self, path: Path) -> bool:
        return os.path.isdir(path)

    # 严格扫描用：按目录 fd 往下走，不跟随链接
    def open_dir(self, name: str | Path, dir_fd: int | None = None) -> int:
        return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)

    def fstat(self, fd: int) -> os.stat_result:
        return os.fstat(fd)

    def listdir_fd(self, fd: int) -> list[str]:
        return os.listdir(fd)

    def stat_at(self, name: str, dir_fd: int) -> os.stat_result:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)

    def read_head(self, name: str, dir_fd: int, n: int = HEAD_BYTES) -> bytes:
        """只读、不跟随链接地读文件开头几个字节（判断它到底是不是系统文件）。"""
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
        try:
            return os.read(fd, n)
        finally:
            os.close(fd)

    def close(self, fd: int) -> None:
        os.close(fd)


class PathCardReader(CardReader):
    """按路径读卡（Windows 没有 dir_fd）。严格扫描用的「目录 fd」是本对象里的句柄号，对应一个已核对的目录路径；
    每打开一级都先 lstat，链接和重解析点（目录联接等）一律拒绝，与 POSIX 的 O_NOFOLLOW 效果相同。"""

    def __init__(self) -> None:
        self._dirs: dict[int, Path] = {}
        self._next = 1 << 20  # 不和真实 fd 重号

    def open_rb(self, path: Path) -> Any:
        if osplat.is_link(os.lstat(path)):
            raise OSError(errno.ELOOP, "链接", str(path))
        return open(path, "rb", buffering=0)

    def open_dir(self, name: str | Path, dir_fd: int | None = None) -> int:
        path = Path(name) if dir_fd is None else self._dirs[dir_fd] / str(name)
        st = os.lstat(path)
        if osplat.is_link(st):
            raise OSError(errno.ELOOP, "链接", str(path))
        if not stat.S_ISDIR(st.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "不是目录", str(path))
        handle = self._next
        self._next += 1
        self._dirs[handle] = path
        return handle

    def fstat(self, fd: int) -> os.stat_result:
        return os.stat(self._dirs[fd]) if fd in self._dirs else os.fstat(fd)

    def listdir_fd(self, fd: int) -> list[str]:
        return os.listdir(self._dirs[fd])

    def stat_at(self, name: str, dir_fd: int) -> os.stat_result:
        return os.lstat(self._dirs[dir_fd] / name)

    def read_head(self, name: str, dir_fd: int, n: int = HEAD_BYTES) -> bytes:
        path = self._dirs[dir_fd] / name
        if osplat.is_link(os.lstat(path)):
            raise OSError(errno.ELOOP, "链接", str(path))
        with open(path, "rb", buffering=0) as fh:
            return fh.read(n)

    def close(self, fd: int) -> None:
        if self._dirs.pop(fd, None) is None:
            os.close(fd)


def find_cards(volumes_roots: Iterable[Path], reader: CardReader | None = None, denied: list[str] | None = None) -> list[Card]:
    """`denied` 不为空时，把"系统不让读"（PermissionError，macOS 没给「可移除的卷」权限）的卷名记在里面。"""
    reader = reader or CardReader()
    cards: list[Card] = []
    for base in volumes_roots:
        try:
            with reader.scandir(Path(base)) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            if denied is not None and isinstance(exc, PermissionError):
                denied.append(str(base))
            continue
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            root = Path(entry.path)
            if _find_dcim(root, reader, denied, entry.name) is not None:
                audit.protect(root)
                cards.append(Card(root=root, label=entry.name))
    return cards


def _find_dcim(root: Path, reader: CardReader, denied: list[str] | None = None, name: str | None = None) -> Path | None:
    """DCIM 必须是真目录：链接不算（不跟随链接）。"""
    try:
        with reader.scandir(root) as it:
            for e in it:
                if e.name.upper() == "DCIM" and e.is_dir(follow_symlinks=False):
                    return Path(e.path)
    except OSError as exc:
        if denied is not None and isinstance(exc, PermissionError):
            denied.append(name or root.name)
        return None
    return None


def scan_card(
    card: Card, reader: CardReader | None = None, errors: list[str] | None = None, videos: list[CardFile] | None = None
) -> list[CardShot]:
    """列出 DCIM 下所有子目录里的 .DNG/.JPG（不分大小写），按 目录+主名 配对。

    以 `.` 开头的文件（macOS 的 `._xxx`、`.Spotlight`）一律跳过。
    `errors` 不为空时，读不了的文件夹/文件（相对卡根的路径）记在里面，免得悄悄漏掉。
    `videos` 不为空时，录像（.AVI）按路径顺序放进去；录像不和照片配对。
    """
    reader = reader or CardReader()
    dcim = _find_dcim(card.root, reader)
    if dcim is None:
        return []
    shots: dict[str, CardShot] = {}

    def note(path: Path, is_dir: bool = False) -> None:
        if errors is not None:
            try:
                rel = Path(path).relative_to(card.root).as_posix()
            except ValueError:
                rel = str(path)
            errors.append(rel + ("/" if is_dir else ""))

    def walk(directory: Path) -> None:
        try:
            with reader.scandir(directory) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            note(directory, True)
            return
        for e in entries:
            if e.name.startswith("."):
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    walk(Path(e.path))
                    continue
                if not e.is_file(follow_symlinks=False):
                    continue
                ext = os.path.splitext(e.name)[1].lower()
                kind = KINDS.get(ext)
                video = VIDEO_KINDS.get(ext) if videos is not None else None
                if kind is None and video is None:
                    continue
                st = e.stat(follow_symlinks=False)
            except OSError:
                note(Path(e.path))
                continue
            rel = Path(e.path).relative_to(card.root).as_posix()
            if kind is None:
                if videos is not None and video is not None:
                    videos.append(CardFile(relpath=rel, path=Path(e.path), size=st.st_size, mtime_ns=st.st_mtime_ns, kind=video))
                continue
            cf = CardFile(relpath=rel, path=Path(e.path), size=st.st_size, mtime_ns=st.st_mtime_ns, kind=kind)
            directory_rel = Path(rel).parent.as_posix()
            key = f"{directory_rel.lower()}/{Path(rel).stem.lower()}"
            shot = shots.setdefault(key, CardShot(key=key, directory=directory_rel, stem=Path(rel).stem))
            shot.files.setdefault(kind, cf)  # 同一主名下出现两个同类型文件时只认第一个

    walk(dcim)
    return sorted(shots.values(), key=lambda s: (s.directory.lower(), s.stem.lower()))


# ---- 严格扫描（照片核对用） -------------------------------------------------


@dataclass(frozen=True)
class StrictEntry:
    relpath: str  # 相对卡根，用 / 分隔
    type: str  # dir | file | link | other
    size: int
    mtime_ns: int
    ino: int = 0
    ctime_ns: int = 0


@dataclass
class StrictScan:
    complete: bool = True
    errors: list[str] = field(default_factory=list)
    dcim: str = "absent"  # present | absent | not_dir | link
    entries: list[StrictEntry] = field(default_factory=list)  # DCIM 里的全部条目（含 DCIM 自己以外的每一层）
    photos: dict[str, list[StrictEntry]] = field(default_factory=lambda: {"dng": [], "jpg": []})
    videos: list[StrictEntry] = field(default_factory=list)
    other_files: list[StrictEntry] = field(default_factory=list)  # DCIM 里导入不会收的文件、链接等
    system_files: list[StrictEntry] = field(default_factory=list)  # DCIM 里 macOS 留下的元数据（`is_system_file`）
    root_entries: list[StrictEntry] = field(default_factory=list)  # 卡根这一层除系统项以外的条目

    def fail(self, where: str, exc: BaseException | str) -> None:
        self.complete = False
        code = errno.errorcode.get(exc.errno, str(exc.errno)) if isinstance(exc, OSError) and exc.errno else str(exc)
        self.errors.append(f"{where}: {code}")

    @property
    def importable(self) -> list[StrictEntry]:
        """导入会收的文件：照片和录像。"""
        return sorted(self.photos["dng"] + self.photos["jpg"] + self.videos, key=lambda e: e.relpath)


def _entry_type(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISLNK(mode):
        return "link"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def looks_like_media(head: bytes) -> bool:
    """开头像 JPEG、TIFF/DNG 或 AVI。"""
    return head[:3] == b"\xff\xd8\xff" or head[:4] in (b"II*\x00", b"MM\x00*") or (head[:4] == b"RIFF" and head[8:12] == b"AVI ")


def _system_name(relpath: str) -> bool:
    parts = relpath.split("/")
    name, dirs = parts[-1], parts[:-1]
    if any(d in MAC_SYSTEM_DIRS for d in dirs) or name.startswith("._") or name in MAC_SYSTEM_FILES:
        return True
    in_dot_dir = any(d.startswith(".") for d in dirs)
    return name.startswith(".") and not in_dot_dir


def is_system_file(relpath: str, head: bytes = b"") -> bool:
    """macOS 自动生成的元数据文件（只对普通文件判断，`head` 是文件开头的字节）。

    名字要对得上（见 MAC_SYSTEM_DIRS / MAC_SYSTEM_FILES / `._*` / 不在点目录里的点文件），而且：
    `._*` 的内容必须是 AppleDouble/AppleSingle；其余的扩展名不能是照片/视频，内容也不能像照片/视频。
    能认出是照片或视频的文件，不管叫什么、放在哪，都不算系统文件（它们是"不会被导入"，要让用户知道）。"""
    if not _system_name(relpath):
        return False
    name = relpath.rsplit("/", 1)[-1]
    if name.startswith("._"):
        return head[:4] in APPLEDOUBLE_MAGIC
    ext = os.path.splitext(name)[1].lower()
    return ext not in KINDS and ext not in VIDEO_KINDS and not looks_like_media(head)


def importable_kind(relpath: str) -> str | None:
    """`scan_card` 会导入这个文件吗：路径里任何一级以点开头都不导入；按扩展名分照片和录像。"""
    parts = relpath.split("/")
    if any(p.startswith(".") for p in parts):
        return None
    ext = os.path.splitext(parts[-1])[1].lower()
    return KINDS.get(ext) or VIDEO_KINDS.get(ext)


def scan_card_strict(root: Path, reader: CardReader | None = None, expect: tuple[int, int] | None = None) -> StrictScan:
    """严格扫描（设计 §4.4）：卡根用 O_DIRECTORY|O_NOFOLLOW 打开，fstat 核对 `expect`（会话的 dev/ino），
    之后一律按目录 fd 往下走、不跟随链接。任何一次打开、列目录、stat 出错都记进 errors，结果标 complete=False；
    这时照片数为 0 不代表没有照片。"""
    reader = reader or CardReader()
    out = StrictScan()
    try:
        root_fd = reader.open_dir(str(root))
    except OSError as exc:
        out.fail(".", exc)
        return out
    try:
        try:
            st = reader.fstat(root_fd)
        except OSError as exc:
            out.fail(".", exc)
            return out
        if expect is not None and (st.st_dev, st.st_ino) != expect:
            out.fail(".", "root_changed")
            return out
        try:
            names = sorted(reader.listdir_fd(root_fd))
        except OSError as exc:
            out.fail(".", exc)
            return out
        dcim_names: list[tuple[str, os.stat_result]] = []
        for name in names:
            try:
                est = reader.stat_at(name, root_fd)
            except OSError as exc:
                out.fail(name, exc)
                continue
            if name.upper() == "DCIM":
                dcim_names.append((name, est))
            elif name in ROOT_SYSTEM_ITEMS:
                continue
            out.root_entries.append(StrictEntry(name, _entry_type(est.st_mode), est.st_size, est.st_mtime_ns))
        if len(dcim_names) > 1:
            out.fail("DCIM", "duplicate_dcim")
        if dcim_names:
            name, est = dcim_names[0]
            kind = _entry_type(est.st_mode)
            out.dcim = {"dir": "present", "link": "link"}.get(kind, "not_dir")
            out.root_entries.append(StrictEntry(name, kind, est.st_size, est.st_mtime_ns))
            if out.dcim == "present":
                _walk_strict(reader, root_fd, name, name, (est.st_dev, est.st_ino), out)
    finally:
        try:
            reader.close(root_fd)
        except OSError as exc:
            out.fail(".", exc)
    return out


def _walk_strict(reader: CardReader, parent_fd: int, name: str, rel: str, expect: tuple[int, int], out: StrictScan) -> None:
    try:
        fd = reader.open_dir(name, parent_fd)
    except OSError as exc:
        out.fail(rel + "/", exc)
        return
    try:
        try:
            st = reader.fstat(fd)
            if (st.st_dev, st.st_ino) != expect:  # 列完父目录以后被换成了别的东西
                out.fail(rel + "/", "changed")
                return
            names = sorted(reader.listdir_fd(fd))
        except OSError as exc:
            out.fail(rel + "/", exc)
            return
        for child in names:
            crel = f"{rel}/{child}"
            try:
                est = reader.stat_at(child, fd)
            except OSError as exc:
                out.fail(crel, exc)
                continue
            kind = _entry_type(est.st_mode)
            entry = StrictEntry(crel, kind, est.st_size, est.st_mtime_ns, est.st_ino, est.st_ctime_ns)
            out.entries.append(entry)
            if kind == "dir":
                _walk_strict(reader, fd, child, crel, (est.st_dev, est.st_ino), out)
                continue
            media = importable_kind(crel) if kind == "file" else None
            if kind == "file" and media is None and _system_name(crel) and _is_system(reader, fd, child, crel, out):
                out.system_files.append(entry)
            elif media in ("dng", "jpg"):
                out.photos[media].append(entry)
            elif media is not None:
                out.videos.append(entry)
            else:
                out.other_files.append(entry)
    finally:
        try:
            reader.close(fd)
        except OSError as exc:
            out.fail(rel + "/", exc)


def _is_system(reader: CardReader, dir_fd: int, name: str, rel: str, out: StrictScan) -> bool:
    """读开头几个字节判断。读不了：扫描不完整，这个文件不算系统文件。"""
    try:
        head = reader.read_head(name, dir_fd)
    except OSError as exc:
        out.fail(rel, exc)
        return False
    return is_system_file(rel, head)
