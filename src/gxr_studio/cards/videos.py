"""录像（.AVI）导入：原样复制进图库的日期目录，不解码，不进照片网格（用户决定，设计 §2/§16）。

录像没有 EXIF，日期按文件时间（`undated_policy` 照样生效）。读卡、暂存、校验和照片走同一套
（`importer._read_files` → `stage_file`），所以读不出来、校验不一致同样记进 unreadable / verify_failed。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..library.store import UNKNOWN_DATE_DIR
from . import coverage, repair
from .discovery import CardFile


def _video_destination(lib: Any, folder: str, name: str, sha: str) -> tuple[str, bool]:
    """-> (目标相对路径, 目标位置上已经是同内容文件)。名字被别的内容占用时加 _<sha8>。"""
    stem, ext = os.path.splitext(name)
    for candidate in [name] + [f"{stem}_{sha[:w]}{ext}" for w in (8, 12, 16, 64)]:
        rel = f"{folder}/{candidate}"
        dest = lib.abspath(rel)
        if not dest.exists():
            return rel, False
        if lib.current_sha(rel) == sha:  # 按磁盘上现在的内容；不在图库里的（链接到卡上）不算同内容
            return rel, True
    raise RuntimeError(f"找不到可用的文件名：{folder}/{name}")


def import_video(lib: Any, card: Any, cf: CardFile, opts: Any, progress: Any, cancel: Any, reader: Any, shift: Any, run: Any) -> None:
    """录像原样复制：不解码，不进照片网格；日期按文件时间（录像没有 EXIF）。"""
    from . import importer as imp  # importer 在模块顶层引用本模块；这里按调用时取，测试替换 stage_file 也生效

    if not opts.force_verify and coverage.trusted_sha(lib, run.ident, cf.relpath, cf.size, cf.mtime_ns):
        progress.add(seen_shots=1)
        return
    if not imp._has_space(lib, [cf], opts, progress):
        return
    known: dict[str, str] = {}
    staged, failed = imp._read_files(lib, card, [cf], known, opts, progress, cancel, reader, run)
    restored = repair.restore_damaged(lib, staged, known, progress, {"card_key": card.key, "volume_uuid": run.ident.volume_uuid})
    sha = known.get(cf.relpath) or (staged[0].sha256 if staged else None)
    if failed or sha is None:
        return
    seen, seen2 = imp._seen_rows(card, [cf], {cf.relpath: sha}), run.seen2_rows([cf])
    if not staged or lib.sha_in_library(sha, cf.size):
        imp._discard(staged)
        lib.add_seen(seen)
        lib.add_seen2(seen2)
        progress.add(**({"imported_videos": 1} if restored else {"duplicate_shots": 1}))
        return
    s = staged[0]
    date, _, _ = imp.decide_date(None, cf.mtime_ns, shift, opts.undated_policy)
    folder = f"{date[:4]}/{date}" if date else UNKNOWN_DATE_DIR
    lib.check_write(folder)  # 按真实路径核对：经链接指到卡上的日期文件夹不写、也不拿卡上的文件当副本
    rel, same = _video_destination(lib, folder, Path(cf.relpath).name, sha)
    lib.check_write(rel)
    dest = lib.abspath(rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if same:
        s.path.unlink()
    else:
        os.replace(s.path, dest)
        progress.add(copied_files=1, bytes_copied=s.size)
    os.utime(dest, ns=(cf.mtime_ns, cf.mtime_ns))
    imp._confirm_placed(lib, rel, s, progress)
    imp._fsync_dir(dest.parent)
    lib.commit_video(rel, sha, s.size, cf.mtime_ns, seen, seen2)
    lib.log(
        {
            "type": "video",
            "status": "imported",
            "card_key": card.key,
            "card_label": card.label,
            "volume_uuid": run.ident.volume_uuid,
            "files": [imp._log_file(card, s, rel, run)],
        }
    )
    progress.add(imported_videos=1)
