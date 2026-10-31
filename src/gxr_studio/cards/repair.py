"""图库里的副本坏了，用卡上原件修好（复审 A1a P1-03、P2-01）。

索引说某个位置存的是这份内容，但那个文件不在了、被等长替换、被截断或被改写：这次从卡上读到、核对过的
同一份内容，放回索引记着的原位置。原位置上被改过的文件不删，改名成 `<原名>.modified-<时间>` 留在旁边。
索引不用改（SHA 本来就对），照片的编辑记录和照片 id 都不变。
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any


def restore_damaged(lib: Any, staged: list[Any], known: dict[str, str], progress: Any, log: dict[str, Any]) -> int:
    """`staged` 里内容在索引中有记录、但图库里没有一份完好副本的，放回原位置；从 `staged` 移到 `known`。
    返回修好的文件数。`log` 是导入日志里这次的公共字段。"""
    fixed = 0
    for s in list(staged):
        damaged = lib.damaged_paths(s.sha256)
        if not damaged or lib.sha_in_library(s.sha256, s.size):
            continue  # 索引里没有这份内容（是新照片），或者另有一份完好的（是重复）
        rel = damaged[0]
        lib.check_write(rel)  # 按真实路径核对：原位置经链接指到卡上时不修（调用方记成没导入）
        dest = lib.abspath(rel)
        kept = None
        if os.path.lexists(dest):
            kept = dest.with_name(f"{dest.name}.modified-{datetime.now().strftime('%Y%m%dT%H%M%S%f')}")
            os.replace(dest, kept)
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(s.path, dest)
        os.utime(dest, ns=(s.card_file.mtime_ns, s.card_file.mtime_ns))
        _sync_dir(dest.parent)
        if not lib.note_verified(rel, s.sha256):  # 放回去以后读回来不是原件：不算修好
            progress.verify_failed.append(s.card_file.relpath)
        progress.add(copied_files=1, bytes_copied=s.size)
        lib.log(
            {
                **log,
                "type": "restored",
                "card_relpath": s.card_file.relpath,
                "sha256": s.sha256,
                "dest": rel,
                "kept_modified": kept.name if kept else None,
            }
        )
        staged.remove(s)
        known[s.card_file.relpath] = s.sha256
        fixed += 1
    return fixed


def _sync_dir(path: Any) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
