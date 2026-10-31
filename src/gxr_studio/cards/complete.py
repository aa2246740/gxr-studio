"""补全一张照片：这次只新读到一对里的一半，另一半以前就导入过。从 importer 拆出来（仓库规定源文件不超过 800 行）。

图库里每个要写的位置（两个文件、编辑记录）都先按真实路径核对（`Library.check_write`），再动手。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..library import sidecar as sc
from ..library import thumbs
from ..library.store import FileRecord, ShotRecord, make_shot_id


def complete_existing(
    lib: Any,
    card: Any,
    shot: Any,
    cfiles: Any,
    known: Any,
    fresh: Any,
    dups: Any,
    staged: Any,
    all_shas: Any,
    opts: Any,
    progress: Any,
    run: Any,
) -> bool:
    """这次只新读到一对里的一半，另一半以前就导入过（上次那一半读不出来）：把新来的这半并进已有的那张照片，
    而不是另起一张同名的（两张会共用同一个 sidecar）。并进去了返回 True。

    `known` 是这一对里已证明在图库里的文件（读内容核对过，或满足可信 seen2 的四个条件）的 SHA。"""
    from . import importer as imp  # importer 在模块顶层引用本模块；这里按调用时取

    shas = list(known.values())
    shas += [s.sha256 for s in dups]
    existing = None
    for sha in shas:
        sid = lib.sha_present(sha) if sha else None
        if sid is not None:
            existing = lib.get_shot(sid)
            break
    if existing is None or any(s.card_file.kind in existing["files"] for s in fresh):
        return False
    folder, stem = existing["folder"], existing["name"]
    targets = {s.card_file.kind: f"{folder}/{stem}{Path(s.card_file.relpath).suffix}" for s in fresh}
    for rel in [*targets.values(), imp._library_rel(lib, lib.sidecar_path_for(existing))]:
        lib.check_write(rel)  # 动图库之前：按真实路径核对每个要写的位置
    # 目标位置已有文件：如果内容和新来的这半一模一样，就是上次补全时在"换位"和"登记"之间被打断留下的，接着往下合并
    landed = set()
    for s in fresh:
        dest = lib.abspath(targets[s.card_file.kind])
        if os.path.lexists(dest):
            if lib.current_sha(targets[s.card_file.kind]) != s.sha256:
                return False
            landed.add(s.card_file.kind)
    try:
        side = lib.read_sidecar_for_write(existing)
        sc.check_writable(side)
    except (sc.SidecarCorrupt, sc.SidecarTooNew):
        return False

    records = [
        FileRecord(k, v["relpath"], v["sha256"], v["size"], lib.abspath(v["relpath"]).stat().st_mtime_ns) for k, v in existing["files"].items()
    ]
    for s in fresh:
        rel = targets[s.card_file.kind]
        dest = lib.abspath(rel)
        if s.card_file.kind in landed:
            s.path.unlink(missing_ok=True)
        else:
            os.replace(s.path, dest)
            progress.add(copied_files=1, bytes_copied=s.size)
        os.utime(dest, ns=(s.card_file.mtime_ns, s.card_file.mtime_ns))
        imp._confirm_placed(lib, rel, s, progress)
        records.append(FileRecord(s.card_file.kind, rel, s.sha256, s.size, s.card_file.mtime_ns))
    imp._fsync_dir(lib.abspath(folder))
    records.sort(key=lambda r: r.kind != "dng")
    dng_rec = next((r for r in records if r.kind == "dng"), None)
    jpg_rec = next((r for r in records if r.kind == "jpg"), None)
    shot_id = make_shot_id(dng_rec.sha256 if dng_rec else None, jpg_rec.sha256 if jpg_rec else None)

    source = dict(side.get("source", {}))
    for r in records:
        source[r.kind] = Path(r.relpath).name
        source[f"{r.kind}_sha256"] = r.sha256
    imported = dict(side.get("import", {}))
    imported.update(shot_id=shot_id, sizes={r.kind: r.size for r in records})
    out = sc.write_atomic(lib.sidecar_path_for(existing), {**side, "source": source, "import": imported}, tmp_dir=lib.tmp_dir)
    rec = ShotRecord(
        shot_id=shot_id,
        name=stem,
        folder=folder,
        date=existing["date"],
        date_source=existing["date_source"],
        captured_at=existing["captured_at"],
        unit=existing["unit"],
        lens=existing["lens"],
        iso=existing["iso"],
        orientation=existing["orientation"],
        width=existing["width"],
        height=existing["height"],
        added_at=existing["added_at"],
        files=records,
        sidecar_rev=int(out.get("rev", 0)),
        crop=out.get("crop"),
    )
    if opts.make_thumbs:
        try:
            thumbs.ensure(
                lib,
                {
                    "shot_id": shot_id,
                    "orientation": rec.orientation,
                    "files": {r.kind: {"relpath": r.relpath} for r in records},
                },
                thumbs.THUMB_SIZE,
            )
        except Exception:
            pass  # nosec B110  补全失败留给这次导入的结果，不在这里吞掉照片
    lib.replace_shot(existing["shot_id"], rec, imp._seen_rows(card, cfiles, all_shas), run.seen2_rows(cfiles))
    lib.log(imp._log_shot("completed", card, shot, staged, opts, rec, records, imported, run))
    progress.add(completed_shots=1)
    return True
