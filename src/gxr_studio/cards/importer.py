"""插卡导入。对卡只读；对图库的每一步都能在中断后接着做。

一张照片（shot）的流程：

1. 用 `seen`（卡标识 + 相对路径 + 大小 + 修改时间）判断是否已经导入过——命中就一个字节都不读；
2. 边读边算 SHA-256，写到 `.studio/staging/*.part`，fsync，再从本地读回来比对哈希；
3. 内容哈希已在图库里 → 重复，丢掉暂存文件；
4. 从暂存文件读 EXIF 日期，决定 `YYYY/YYYY-MM-DD/`（没有日期按策略退回文件时间或"未知日期"）；
5. 目标名已被别的内容占用 → 两个文件一起加 `_<sha8>` 后缀；已有同内容的残留 → 直接沿用；
6. `os.replace` 到位，写缩略图和 sidecar，最后一个 SQLite 事务提交索引 + seen。

任何一步被杀掉：残留的只有 `.part`（下次启动清掉）和"已就位但还没入索引"的文件
（下次发现目标名上已经是同内容文件，直接沿用）。
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .. import meta as metamod
from ..config import MiB
from ..library import sidecar as sc
from ..library import thumbs
from ..library.store import (
    UNKNOWN_DATE_DIR,
    FileRecord,
    Library,
    ShotRecord,
    make_shot_id,
    sha256_file,
)
from .discovery import Card, CardFile, CardReader, CardShot, scan_card


class CardRemoved(Exception):
    """卡不见了（拔出或被系统弹出）。"""


class CardReadError(Exception):
    """卡上某个文件读不出来。"""


class VerifyFailed(Exception):
    """复制到本地后读回来的哈希对不上。"""


class NoSpace(Exception):
    pass


class Cancelled(Exception):
    pass


def _free_space(path: Path) -> int:
    return shutil.disk_usage(path).free


@dataclass
class ImportOptions:
    shift_seconds: int = 0
    force_verify: bool = False
    undated_policy: str = "mtime"  # mtime | unknown_folder
    min_free_bytes: int = 256 * MiB
    free_space: Callable[[Path], int] = _free_space
    chunk: int = 1 << 20
    fsync: bool = True
    make_thumbs: bool = True
    # 测试钩子：before_verify 在暂存文件写完、读回比对之前；after_shot 在一张照片提交之后
    before_verify: Callable[[Path, CardFile], None] | None = None
    after_shot: Callable[[str], None] | None = None


@dataclass
class ImportProgress:
    state: str = "idle"  # idle scanning running done paused_no_space interrupted cancelled failed
    message: str = ""
    total_shots: int = 0
    done_shots: int = 0
    imported_shots: int = 0
    duplicate_shots: int = 0
    seen_shots: int = 0
    copied_files: int = 0
    bytes_copied: int = 0
    unreadable: list[str] = field(default_factory=list)
    verify_failed: list[str] = field(default_factory=list)
    scan_errors: list[str] = field(default_factory=list)  # 列目录时读不了的文件夹/文件
    failed_shots: int = 0      # 有文件没导进来的照片张数（一对里坏一个也算一张）
    completed_shots: int = 0   # 之前只导进来一半、这次把另一半补上的张数
    needed_bytes: int = 0
    free_bytes: int = 0
    current: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def update(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def add(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, getattr(self, k) + v)

    def to_dict(self) -> dict:
        with self._lock:
            d = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
            d["unreadable"] = list(self.unreadable)
            d["verify_failed"] = list(self.verify_failed)
            d["scan_errors"] = list(self.scan_errors)
            return d

    def has_problems(self) -> bool:
        """有任何文件没读进来（读不出、校验不一致、列目录出错）。"""
        with self._lock:
            return bool(self.unreadable or self.verify_failed or self.scan_errors)


@dataclass
class Staged:
    card_file: CardFile
    path: Path
    sha256: str
    size: int


# ---------------------------------------------------------------------------


def card_present(card: Card, reader: CardReader) -> bool:
    try:
        with reader.scandir(card.root) as it:
            return any(e.name.upper() == "DCIM" for e in it)
    except OSError:
        return False


def stage_file(
    reader: CardReader,
    card: Card,
    cf: CardFile,
    lib: Library,
    opts: ImportOptions,
    cancel: threading.Event,
) -> Staged:
    part = lib.staging_dir / f"{uuid.uuid4().hex}.part"
    digest = hashlib.sha256()
    total = 0
    try:
        try:
            src = reader.open_rb(cf.path)
        except OSError as exc:
            if not card_present(card, reader):
                raise CardRemoved() from exc
            raise CardReadError(str(exc)) from exc
        with src, open(part, "wb") as dst:
            while True:
                if cancel.is_set():
                    raise Cancelled()
                try:
                    block = src.read(opts.chunk)
                except OSError as exc:
                    if not card_present(card, reader):
                        raise CardRemoved() from exc
                    raise CardReadError(str(exc)) from exc
                if not block:
                    break
                digest.update(block)
                try:
                    dst.write(block)
                except OSError as exc:
                    if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                        raise NoSpace() from exc
                    raise
                total += len(block)
            dst.flush()
            if opts.fsync:
                os.fsync(dst.fileno())
        if total != cf.size:
            if not card_present(card, reader):
                raise CardRemoved()
            raise CardReadError(f"读出 {total} 字节，目录里写的是 {cf.size} 字节")
        if opts.before_verify:
            opts.before_verify(part, cf)
        if sha256_file(part) != digest.hexdigest():
            raise VerifyFailed(cf.relpath)
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise
    return Staged(cf, part, digest.hexdigest(), total)


def _discard(staged: list[Staged]) -> None:
    for s in staged:
        try:
            s.path.unlink()
        except OSError:
            pass


def _fsync_dir(path: Path) -> None:
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


def decide_date(
    taken: datetime | None, mtime_ns: int, shift: timedelta, policy: str
) -> tuple[str | None, str | None, str]:
    """-> (日期 YYYY-MM-DD 或 None, 拍摄时间 ISO 或 None, 来源 exif|mtime|none)"""
    if taken is not None:
        dt = taken + shift
        return dt.date().isoformat(), dt.isoformat(), "exif"
    if policy == "mtime":
        dt = datetime.fromtimestamp(mtime_ns / 1e9) + shift
        if metamod.plausible(dt):
            return dt.date().isoformat(), dt.isoformat(), "mtime"
    return None, None, "none"


def _resolve_destination(
    lib: Library, folder: str, stem: str, items: list[Staged], main_sha: str
) -> tuple[str, dict[str, bool]]:
    """-> (最终主名, {kind: 目标位置上已经是同内容文件})。冲突时整对一起加 _<sha8>。"""
    conflict, same = _conflicts(lib, folder, stem, items)
    if not conflict:
        return stem, same
    for width in (8, 12, 16, 64):
        candidate = f"{stem}_{main_sha[:width]}"
        conflict, same = _conflicts(lib, folder, candidate, items)
        if not conflict:
            return candidate, same
    raise RuntimeError(f"找不到可用的文件名：{folder}/{stem}")


def _conflicts(lib: Library, folder: str, stem: str, items: list[Staged]) -> tuple[bool, dict[str, bool]]:
    same: dict[str, bool] = {}
    conflict = False
    for s in items:
        ext = Path(s.card_file.relpath).suffix
        rel = f"{folder}/{stem}{ext}"
        dest = lib.abspath(rel)
        if not dest.exists():
            same[s.card_file.kind] = False
            continue
        row = lib.file_by_relpath(rel)
        existing = row["sha256"] if row else sha256_file(dest)
        if existing == s.sha256:
            same[s.card_file.kind] = True
        else:
            conflict = True
    return conflict, same


def run_import(
    lib: Library,
    card: Card,
    opts: ImportOptions | None = None,
    progress: ImportProgress | None = None,
    cancel: threading.Event | None = None,
    reader: CardReader | None = None,
) -> ImportProgress:
    opts = opts or ImportOptions()
    progress = progress or ImportProgress()
    cancel = cancel or threading.Event()
    reader = reader or CardReader()
    shift = timedelta(seconds=opts.shift_seconds)
    try:
        _run(lib, card, opts, progress, cancel, reader, shift)
    except CardRemoved:
        progress.update(
            state="interrupted",
            message=f"导入被打断了：已处理 {progress.done_shots} 张，剩下 "
            f"{progress.total_shots - progress.done_shots} 张没有导入。插回卡可以继续。",
        )
    except Cancelled:
        progress.update(state="cancelled", message="导入已取消。")
    except NoSpace:
        progress.update(state="paused_no_space", message="电脑空间不够，导入已暂停。清理后可以继续。")
    except BaseException as exc:
        progress.update(state="failed", message=f"导入失败：{type(exc).__name__}: {exc}")
        raise
    finally:
        progress.update(current=None)
        lib.clean_staging()
        lib.log(
            {
                "type": "run",
                "card_key": card.key,
                "card_label": card.label,
                "state": progress.state,
                "shift_seconds": opts.shift_seconds,
                "force_verify": opts.force_verify,
                **{k: v for k, v in progress.to_dict().items() if isinstance(v, int)},
                "unreadable": progress.unreadable,
                "verify_failed": progress.verify_failed,
                "scan_errors": progress.scan_errors,
            }
        )
    return progress


def _run(lib, card, opts, progress, cancel, reader, shift) -> None:
    progress.update(state="scanning", message="正在读取卡上的文件列表…")
    lib.clean_staging()
    scan_errors: list[str] = []
    shots = scan_card(card, reader, scan_errors)
    if not shots and not card_present(card, reader):
        raise CardRemoved()
    progress.update(total_shots=len(shots), state="running", message="", scan_errors=scan_errors)
    lib.log(
        {
            "type": "run_start",
            "card_key": card.key,
            "card_label": card.label,
            "shots": len(shots),
            "shift_seconds": opts.shift_seconds,
            "force_verify": opts.force_verify,
            "undated_policy": opts.undated_policy,
        }
    )
    for shot in shots:
        if cancel.is_set():
            raise Cancelled()
        progress.update(current=f"{shot.directory}/{shot.stem}")
        _import_shot(lib, card, shot, opts, progress, cancel, reader, shift)
        if progress.state == "paused_no_space":
            return
        progress.add(done_shots=1)
        if opts.after_shot:
            opts.after_shot(shot.key)
    progress.update(state="done", message=_done_message(progress))


def _done_message(p: ImportProgress) -> str:
    if p.has_problems():
        bad = len(p.unreadable) + len(p.verify_failed) + len(p.scan_errors)
        got = f"新增 {p.imported_shots} 张" + (f"、补全 {p.completed_shots} 张" if p.completed_shots else "")
        lost = f"有 {p.failed_shots} 张没有完整导进来，" if p.failed_shots else ""
        return (f"导入没有全部完成：{got}。{lost}共 {bad} 个文件或文件夹没能读进来（读不出来或校验不一致），"
                "卡上的文件没有改动。请先别格式化或清空这张卡：换个读卡器或重新插拔，再点「继续导入」。")
    if p.imported_shots == 0 and not p.completed_shots:
        return "这张卡上的照片都导入过了。"
    return f"导入完成，共 {p.imported_shots + p.completed_shots} 张。现在可以安全弹出卡了。"


def _seen_rows(card: Card, files: list[CardFile], shas: dict[str, str]) -> list[tuple]:
    return [(card.key, f.relpath, f.size, f.mtime_ns, shas[f.relpath]) for f in files if f.relpath in shas]


def _complete_existing(lib, card, shot, cfiles, todo, fresh, dups, staged, all_shas, opts, progress) -> bool:
    """这次只新读到一对里的一半，另一半以前就导入过（上次那一半读不出来）：把新来的这半并进已有的那张照片，
    而不是另起一张同名的（两张会共用同一个 sidecar）。并进去了返回 True。"""
    todo_paths = {f.relpath for f in todo}
    shas = [lib.seen_sha(card.key, f.relpath, f.size, f.mtime_ns) for f in cfiles if f.relpath not in todo_paths]
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
    # 目标位置已有文件：如果内容和新来的这半一模一样，就是上次补全时在"换位"和"登记"之间被打断留下的，接着往下合并
    landed = set()
    for s in fresh:
        dest = lib.abspath(targets[s.card_file.kind])
        if dest.exists():
            if sha256_file(dest) != s.sha256:
                return False
            landed.add(s.card_file.kind)
    try:
        side = lib.read_sidecar_for_write(existing)
        sc.check_writable(side)
    except (sc.SidecarCorrupt, sc.SidecarTooNew):
        return False

    records = [
        FileRecord(k, v["relpath"], v["sha256"], v["size"], lib.abspath(v["relpath"]).stat().st_mtime_ns)
        for k, v in existing["files"].items()
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
        records.append(FileRecord(s.card_file.kind, rel, s.sha256, s.size, s.card_file.mtime_ns))
    _fsync_dir(lib.abspath(folder))
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
    out = sc.write_atomic(
        lib.sidecar_path_for(existing), {**side, "source": source, "import": imported}, tmp_dir=lib.tmp_dir
    )
    rec = ShotRecord(
        shot_id=shot_id, name=stem, folder=folder, date=existing["date"], date_source=existing["date_source"],
        captured_at=existing["captured_at"], unit=existing["unit"], lens=existing["lens"], iso=existing["iso"],
        orientation=existing["orientation"], width=existing["width"], height=existing["height"],
        added_at=existing["added_at"], files=records, sidecar_rev=int(out.get("rev", 0)), crop=out.get("crop"),
    )
    if opts.make_thumbs:
        try:
            thumbs.ensure(
                lib,
                {"shot_id": shot_id, "orientation": rec.orientation,
                 "files": {r.kind: {"relpath": r.relpath} for r in records}},
                thumbs.THUMB_SIZE,
            )
        except Exception:
            pass
    lib.replace_shot(existing["shot_id"], rec, _seen_rows(card, cfiles, all_shas))
    lib.log(_log_shot("completed", card, shot, staged, opts, rec, records, imported))
    progress.add(completed_shots=1)
    return True


def _import_shot(lib, card, shot: CardShot, opts, progress, cancel, reader, shift) -> None:
    cfiles = list(shot.files.values())
    if opts.force_verify:
        todo = cfiles
    else:
        todo = [f for f in cfiles if not lib.seen_hit(card.key, f.relpath, f.size, f.mtime_ns)]
    if not todo:
        progress.add(seen_shots=1)
        return

    need = sum(f.size for f in todo)
    free = opts.free_space(lib.root)
    if free - need < opts.min_free_bytes:
        progress.update(
            state="paused_no_space",
            needed_bytes=need,
            free_bytes=free,
            message="电脑空间不够，导入已暂停。清理后可以继续。",
        )
        return

    # 一对文件里每个文件各自处理：读得出的照常导入，读不出/校验不一致的明确记下来（不登记 seen，下次会再试）
    staged: list[Staged] = []
    failed = False
    for cf in todo:
        try:
            staged.append(stage_file(reader, card, cf, lib, opts, cancel))
        except CardReadError:
            progress.unreadable.append(cf.relpath)
            failed = True
        except VerifyFailed:
            progress.verify_failed.append(cf.relpath)
            failed = True
        except BaseException:
            _discard(staged)
            raise
    if failed:
        progress.add(failed_shots=1)
    if not staged:
        return

    # 只靠 seen 命中、这次没读的文件：它们已经在图库里，不参与本次
    all_shas = {s.card_file.relpath: s.sha256 for s in staged}
    fresh = [s for s in staged if lib.sha_present(s.sha256) is None]
    dups = [s for s in staged if s not in fresh]
    if not fresh:
        _discard(staged)
        lib.add_seen(_seen_rows(card, cfiles, all_shas))
        lib.log(_log_shot("duplicate", card, shot, staged, opts, None, None, None))
        if not failed:
            progress.add(duplicate_shots=1)
        return
    _discard(dups)

    if _complete_existing(lib, card, shot, cfiles, todo, fresh, dups, staged, all_shas, opts, progress):
        return

    by_kind = {s.card_file.kind: s for s in fresh}
    main = by_kind.get("dng") or by_kind["jpg"]
    jpg_meta = metamod.read_jpeg_meta(by_kind["jpg"].path) if "jpg" in by_kind else None
    dng_meta = metamod.read_dng_meta(by_kind["dng"].path) if "dng" in by_kind else None
    taken = next((m.taken for m in (jpg_meta, dng_meta) if m and m.taken), None)
    primary = jpg_meta if jpg_meta and not jpg_meta.error else dng_meta
    mtime_ns = (by_kind.get("jpg") or main).card_file.mtime_ns
    date, captured_at, date_source = decide_date(taken, mtime_ns, shift, opts.undated_policy)
    folder = f"{date[:4]}/{date}" if date else UNKNOWN_DATE_DIR

    stem, same = _resolve_destination(lib, folder, shot.stem, fresh, main.sha256)
    lib.abspath(folder).mkdir(parents=True, exist_ok=True)
    records: list[FileRecord] = []
    for kind in ("dng", "jpg"):
        s = by_kind.get(kind)
        if s is None:
            continue
        rel = f"{folder}/{stem}{Path(s.card_file.relpath).suffix}"
        dest = lib.abspath(rel)
        if same.get(kind):
            s.path.unlink()
        else:
            os.replace(s.path, dest)
            progress.add(copied_files=1, bytes_copied=s.size)
        os.utime(dest, ns=(s.card_file.mtime_ns, s.card_file.mtime_ns))
        records.append(FileRecord(kind, rel, s.sha256, s.size, s.card_file.mtime_ns))
    _fsync_dir(lib.abspath(folder))

    dng_rec = next((r for r in records if r.kind == "dng"), None)
    jpg_rec = next((r for r in records if r.kind == "jpg"), None)
    shot_id = make_shot_id(dng_rec.sha256 if dng_rec else None, jpg_rec.sha256 if jpg_rec else None)
    lens = next((m.lens for m in (jpg_meta, dng_meta) if m and m.lens), None)
    iso = next((m.iso for m in (jpg_meta, dng_meta) if m and m.iso), None)
    added_at = datetime.now().isoformat(timespec="seconds")
    imported_info = {
        "shot_id": shot_id,
        "card_key": card.key,
        "card_label": card.label,
        "date": date,
        "date_source": date_source,
        "captured_at": captured_at,
        "shift_seconds": opts.shift_seconds,
        "added_at": added_at,
        "sizes": {r.kind: r.size for r in records},
    }
    sc_path = sc.sidecar_path(lib.abspath(records[0].relpath))
    side = None
    if sc_path.exists():
        try:
            old = sc.read(sc_path)
            src = old.get("source", {})
            if src.get("dng_sha256") == (dng_rec.sha256 if dng_rec else None) and src.get(
                "jpg_sha256"
            ) == (jpg_rec.sha256 if jpg_rec else None):
                side = old
        except sc.SidecarCorrupt:
            pass
    if side is None:
        side = sc.write_atomic(
            sc_path,
            sc.new_sidecar(
                dng=Path(dng_rec.relpath).name if dng_rec else None,
                jpg=Path(jpg_rec.relpath).name if jpg_rec else None,
                dng_sha256=dng_rec.sha256 if dng_rec else None,
                jpg_sha256=jpg_rec.sha256 if jpg_rec else None,
                orientation=primary.orientation if primary else 1,
                width=primary.width if primary else None,
                height=primary.height if primary else None,
                imported=imported_info,
            ),
            bump=False,
            tmp_dir=lib.tmp_dir,
        )
    rec = ShotRecord(
        shot_id=shot_id,
        name=stem,
        folder=folder,
        date=date,
        date_source=date_source,
        captured_at=captured_at,
        unit=metamod.unit_from_lens(lens),
        lens=lens,
        iso=iso,
        orientation=primary.orientation if primary else 1,
        width=primary.width if primary else None,
        height=primary.height if primary else None,
        added_at=added_at,
        files=records,
        sidecar_rev=int(side.get("rev", 0)),
        crop=side.get("crop"),
    )
    if opts.make_thumbs:
        try:
            thumbs.ensure(
                lib,
                {"shot_id": shot_id, "orientation": rec.orientation,
                 "files": {r.kind: {"relpath": r.relpath} for r in records}},
                thumbs.THUMB_SIZE,
            )
        except Exception:
            pass  # 缩略图是缓存，之后按需再生成
    seen = _seen_rows(card, cfiles, all_shas)
    lib.commit_shot(rec, seen)
    lib.log(_log_shot("imported", card, shot, staged, opts, rec, records, imported_info))
    progress.add(imported_shots=1)


def _log_shot(status, card, shot, staged, opts, rec, records, info) -> dict:
    dest = {r.kind: r.relpath for r in records} if records else {}
    return {
        "type": "shot",
        "status": status,
        "shot_id": rec.shot_id if rec else None,
        "card_key": card.key,
        "card_label": card.label,
        "shift_seconds": opts.shift_seconds,
        "date": rec.date if rec else None,
        "date_source": rec.date_source if rec else None,
        "files": [
            {
                "kind": s.card_file.kind,
                "card_key": card.key,
                "card_relpath": s.card_file.relpath,
                "card_size": s.card_file.size,
                "card_mtime_ns": s.card_file.mtime_ns,
                "sha256": s.sha256,
                "dest": dest.get(s.card_file.kind),
            }
            for s in staged
        ],
    }


def card_summary(lib: Library, card: Card, reader: CardReader | None = None) -> dict:
    """轻量统计（只列目录、查 seen，不读文件内容）。"""
    shots = scan_card(card, reader)
    new = [s for s in shots if any(not lib.seen_hit(card.key, f.relpath, f.size, f.mtime_ns) for f in s.files.values())]
    return {
        "shots": len(shots),
        "new_shots": len(new),
        "dng_files": sum("dng" in s.files for s in shots),
        "jpg_files": sum("jpg" in s.files for s in shots),
        "new_bytes": sum(f.size for s in new for f in s.files.values()
                         if not lib.seen_hit(card.key, f.relpath, f.size, f.mtime_ns)),
    }
