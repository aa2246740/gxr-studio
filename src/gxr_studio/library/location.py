"""图库的真实位置核对：在第一次写图库之前做（复审 A1a R3-P1-01）。

启动时，第一次扫卡（登记卡根写保护）之前，就已经要拿锁、建 `.studio`、清临时目录、打开索引。
如果图库根、`.studio` 或其中某个文件事先被做成指到插着的卡上的链接，这些写入就会落到卡上。所以：

1. `card_roots(volumes_roots)`：只读地找出当前插着的卡（卷根下有 DCIM 目录或卡根有 GXRCE.DAT 的目录）；
2. `check_library_location(root, cards)`：
   - 图库根解析链接以后，不能在任何卡根（含已登记写保护的目录）里面；
   - 图库自己会写的每个位置（`.studio`、索引及其日志文件、锁、导入日志、缩略图、暂存区、临时目录）解析链接以后，
     都必须还在图库根里面——经链接指到卡上或图库外面的，一律拒绝。

不合格抛 `LibraryLocationRefused`：启动明确报错，一个字节都不写。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable

from .. import audit

# 图库自己（不算照片）会写的位置
STUDIO_PATHS = (
    ".studio",
    ".studio/studio.lock",
    ".studio/index.sqlite",
    ".studio/index.sqlite-journal",
    ".studio/index.sqlite-wal",
    ".studio/index.sqlite-shm",
    ".studio/import-log.jsonl",
    ".studio/thumbs",
    ".studio/staging",
    ".studio/tmp",
)
MESSAGE = "无法打开图库。图库文件夹（或其中的 .studio）在存储卡上，或经过链接指到了图库以外的位置。"
ADVICE = "请把图库放在电脑上的普通文件夹里，再重新打开工作室。"
EXIT_CODE = 78  # EX_CONFIG：配置不对，不是"已经开着了"


class LibraryLocationRefused(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(f"{MESSAGE} {detail}")
        self.message, self.detail = MESSAGE, detail


def _real(path: str | os.PathLike[Any]) -> Path:
    return Path(os.path.realpath(os.path.abspath(os.fspath(path))))


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def card_roots(volumes_roots: Iterable[str | os.PathLike[Any]]) -> list[Path]:
    """当前插着的卡的卡根（只读：只列目录，不打开文件、不登记任何东西）。读不了的卷跳过。"""
    found: list[Path] = []
    for base in volumes_roots:
        try:
            with os.scandir(str(base)) as it:
                volumes = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        for vol in volumes:
            if vol.name.startswith("."):
                continue
            try:
                if not vol.is_dir():
                    continue
                with os.scandir(vol.path) as inner:
                    names = {e.name.upper(): e for e in inner}
                dcim = names.get("DCIM")
                is_card = (dcim is not None and dcim.is_dir(follow_symlinks=False)) or "GXRCE.DAT" in names
            except OSError:
                continue
            if is_card:
                found.append(Path(vol.path))
    return found


def check_library_location(root: str | os.PathLike[Any], cards: Iterable[str | os.PathLike[Any]] = ()) -> Path:
    """核对图库的真实位置（见模块说明）。合格返回图库根的真实路径；不合格抛 `LibraryLocationRefused`。只读。"""
    real_root = _real(root)
    for card in [*cards, *audit.protected_roots()]:
        real_card = _real(card)
        if _within(real_root, real_card):
            raise LibraryLocationRefused(f"图库 {root} 实际在存储卡 {real_card} 上")
    for rel in STUDIO_PATHS:
        real = _real(Path(root) / rel)
        if not _within(real, real_root):
            raise LibraryLocationRefused(f"{Path(root) / rel} 经过链接指到了 {real}")
    return real_root
