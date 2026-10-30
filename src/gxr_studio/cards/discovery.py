"""发现存储卡、列出卡上的照片、把 DNG 和 JPG 配成一对。

这个模块只做"读"：`os.scandir` / `stat`，不打开任何文件，更不写。
被发现的卡根目录会登记进审计钩子的保护表，之后任何写入都会被拒绝。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .. import audit

KINDS = {".dng": "dng", ".jpg": "jpg"}


@dataclass(frozen=True)
class Card:
    root: Path
    label: str

    @property
    def key(self) -> str:
        """卡的标识：卷标的哈希。只用来加速"见过的文件"（seen），不作去重依据（去重靠内容哈希）。"""
        return hashlib.sha256(self.label.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class CardFile:
    relpath: str  # 相对卡根，用 / 分隔，保留卡上的大小写
    path: Path
    size: int
    mtime_ns: int
    kind: str  # dng | jpg

    @property
    def stem(self) -> str:
        return Path(self.relpath).stem


@dataclass
class CardShot:
    """卡上一张照片（可能是 DNG+JPG 一对，也可能只有一个）。"""

    key: str  # 小写的 目录/主名
    directory: str
    stem: str
    files: dict[str, CardFile] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files.values())


class CardReader:
    """唯一允许碰卡文件的入口，只提供读。测试里可以继承它来模拟拔卡、读错。"""

    def scandir(self, path: Path):
        return os.scandir(path)

    def open_rb(self, path: Path):
        return open(path, "rb", buffering=0)

    def exists(self, path: Path) -> bool:
        return os.path.isdir(path)


def find_cards(
    volumes_roots: Iterable[Path], reader: CardReader | None = None, denied: list[str] | None = None
) -> list[Card]:
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
    try:
        with reader.scandir(root) as it:
            for e in it:
                if e.name.upper() == "DCIM" and e.is_dir():
                    return Path(e.path)
    except OSError as exc:
        if denied is not None and isinstance(exc, PermissionError):
            denied.append(name or root.name)
        return None
    return None


def scan_card(card: Card, reader: CardReader | None = None, errors: list[str] | None = None) -> list[CardShot]:
    """列出 DCIM 下所有子目录里的 .DNG/.JPG（不分大小写），按 目录+主名 配对。

    以 `.` 开头的文件（macOS 的 `._xxx`、`.Spotlight`）一律跳过。
    `errors` 不为空时，读不了的文件夹/文件（相对卡根的路径）记在里面，免得悄悄漏掉。
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
                kind = KINDS.get(os.path.splitext(e.name)[1].lower())
                if kind is None:
                    continue
                st = e.stat(follow_symlinks=False)
            except OSError:
                note(Path(e.path))
                continue
            rel = Path(e.path).relative_to(card.root).as_posix()
            cf = CardFile(relpath=rel, path=Path(e.path), size=st.st_size, mtime_ns=st.st_mtime_ns, kind=kind)
            directory_rel = Path(rel).parent.as_posix()
            key = f"{directory_rel.lower()}/{Path(rel).stem.lower()}"
            shot = shots.setdefault(key, CardShot(key=key, directory=directory_rel, stem=Path(rel).stem))
            shot.files.setdefault(kind, cf)  # 同一主名下出现两个同类型文件时只认第一个

    walk(dcim)
    return sorted(shots.values(), key=lambda s: (s.directory.lower(), s.stem.lower()))
