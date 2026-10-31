"""插卡导入。对卡只读；对图库的每一步都能在中断后接着做。

一张照片（shot）的流程：

1. 能不能不读内容：只有 `coverage.trusted_sha` 的四个条件都满足（默认 `SEEN_TRUST_UUID` 关着，所以每次都读）。
   以前见过的文件（seen / seen2 有记录）先只读算哈希：图库里确实有这份内容、文件还在，就不再复制；
2. 边读边算 SHA-256，写到 `.studio/staging/*.part`，fsync，再从本地读回来比对哈希；
3. 内容哈希已在图库里 → 重复，丢掉暂存文件；
4. 从暂存文件读 EXIF 日期，决定 `YYYY/YYYY-MM-DD/`（没有日期按策略退回文件时间或"未知日期"）；
5. 目标名已被别的内容占用 → 两个文件一起加 `_<sha8>` 后缀；已有同内容的残留 → 直接沿用；
6. `os.replace` 到位，写缩略图和 sidecar，最后一个 SQLite 事务提交索引 + seen + seen2。

录像（.AVI）原样复制到按文件时间决定的日期目录，记进 `videos` 表，不解码、不进照片网格。
全部做完以后用 `coverage.photo_coverage` 核对整张卡，结束提示只按核对结果说（设计 §4.5）。

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
from typing import Any, Callable

from .. import meta as metamod
from ..config import MiB
from ..library import sidecar as sc
from ..library import thumbs
from ..library.store import (
    UNKNOWN_DATE_DIR,
    FileRecord,
    Library,
    LibraryPathRefused,
    ShotRecord,
    make_shot_id,
    sha256_file,
)
from . import complete, coverage, repair
from . import videos as videos_mod
from .coverage import FileState, Identity, Proof
from .discovery import Card, CardFile, CardReader, CardShot, scan_card
from .messages import CHECK_FAILED, DONE_ALL, done_message

# DONE_ALL / CHECK_FAILED / done_message 在 messages.py 里定稿，这里转出去。
# HEAD 里的私有名字 `_done_message` 不再提供（仓库里没有调用方，复审 A1a R2-P2-02 选择不加兼容入口）。
__all__ = ["CHECK_FAILED", "DONE_ALL", "ImportOptions", "ImportProgress", "done_message", "run_import", "stage_file"]

# 导入要写的图库自身位置（暂存区、编辑记录临时目录、缩略图、日志、索引）：按真实路径都必须在图库里
LAYOUT = (".studio/staging/x", ".studio/tmp/x", ".studio/thumbs/x", ".studio/import-log.jsonl", ".studio/index.sqlite")
LAYOUT_REFUSED = "无法写入图库文件夹。请检查图库位置后重试。"


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
    failed_shots: int = 0  # 有文件没导进来的照片张数（一对里坏一个也算一张）
    completed_shots: int = 0  # 之前只导进来一半、这次把另一半补上的张数
    imported_videos: int = 0
    write_refused: list[str] = field(default_factory=list)  # 图库里要写的位置按真实路径不在图库（例如经链接指到卡上）：没写
    card_id: str | None = None  # 来源挂载会话号：任务和结果只属于这一次挂载（界面据此判断结果给哪张卡看）
    coverage: dict[str, Any] | None = None  # 导入结束后整张卡的照片核对结果（coverage.Coverage.to_dict）
    needed_bytes: int = 0
    free_bytes: int = 0
    current: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def update(self, **kw: Any) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def add(self, **kw: Any) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, getattr(self, k) + v)

    def to_dict(self) -> dict[Any, Any]:
        with self._lock:
            d = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
            d["unreadable"] = list(self.unreadable)
            d["verify_failed"] = list(self.verify_failed)
            d["scan_errors"] = list(self.scan_errors)
            d["write_refused"] = list(self.write_refused)
            d["coverage"] = dict(self.coverage) if self.coverage is not None else None
            return d

    def has_problems(self) -> bool:
        """有任何文件没读进来（读不出、校验不一致、列目录出错）。"""
        with self._lock:
            return bool(self.unreadable or self.verify_failed or self.scan_errors or self.write_refused)


@dataclass
class _Run:
    """一次导入的上下文：卷身份，以及这次实际读过内容的文件（给最后的照片核对用）。"""

    ident: Identity
    proofs: dict[str, Proof] = field(default_factory=dict)  # 绑定了读的时候文件状态的内容证明
    read: dict[str, tuple[int, int, str]] = field(default_factory=dict)  # 这次读过内容的文件：(长度, mtime, SHA)

    def prove(self, cf: CardFile, sha: str, state: FileState | None) -> None:
        self.read[cf.relpath] = (cf.size, cf.mtime_ns, sha)
        if state is not None and state[1:3] == (cf.size, cf.mtime_ns):
            self.proofs[cf.relpath] = Proof(sha, state)

    def check(self) -> None:
        """这次挂载会话还有效吗（复审 A1a P1-02）。无效：作废这次的全部证明，按"拔卡"中断。"""
        if not self.ident.still_valid():
            self.proofs.clear()
            self.read.clear()
            raise CardRemoved()

    def seen2_rows(self, files: list[CardFile]) -> list[tuple[Any, ...]]:
        """只给这次读过内容的文件，而且只在有完整 UUID 时。"""
        if not self.ident.volume_uuid:
            return []
        out = []
        for f in files:
            r = self.read.get(f.relpath)
            if r is not None and r[:2] == (f.size, f.mtime_ns):
                out.append((self.ident.volume_uuid, f.relpath, f.size, f.mtime_ns, r[2]))
        return out


@dataclass
class Staged:
    card_file: CardFile
    path: Path
    sha256: str
    size: int
    state: FileState | None = None  # 读卡上这个文件时它的状态（inode、长度、mtime、ctime）


# ---------------------------------------------------------------------------


def card_present(card: Card, reader: CardReader) -> bool:
    try:
        with reader.scandir(card.root) as it:
            return any(e.name.upper() == "DCIM" for e in it)
    except OSError:
        return False


def probe_file(reader: CardReader, card: Card, cf: CardFile, opts: ImportOptions, cancel: threading.Event) -> tuple[str, FileState | None]:
    """只读算哈希，不复制。给"以前见过"的文件用：图库里确实有这份内容就不用再写一遍。"""
    try:
        return coverage.hash_card_file(reader, cf.path, cf.size, opts.chunk, cancel)
    except InterruptedError as exc:
        raise Cancelled() from exc
    except OSError as exc:
        if not card_present(card, reader):
            raise CardRemoved() from exc
        raise CardReadError(str(exc)) from exc


def _hinted(lib: Library, card: Card, cf: CardFile, run: _Run) -> bool:
    """seen / seen2 说"可能导入过"：只决定先算哈希还是直接复制，不能用来跳过读取。"""
    if run.ident.volume_uuid and lib.seen2_sha(run.ident.volume_uuid, cf.relpath, cf.size, cf.mtime_ns):
        return True
    return lib.seen_hit(card.key, cf.relpath, cf.size, cf.mtime_ns)


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
            before = coverage.file_state(src)
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
            after = coverage.file_state(src)
            dst.flush()
            if opts.fsync:
                os.fsync(dst.fileno())
        if before != after:
            raise CardReadError("读的过程中文件变了")
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
    return Staged(cf, part, digest.hexdigest(), total, before)


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


def decide_date(taken: datetime | None, mtime_ns: int, shift: timedelta, policy: str) -> tuple[str | None, str | None, str]:
    """-> (日期 YYYY-MM-DD 或 None, 拍摄时间 ISO 或 None, 来源 exif|mtime|none)"""
    if taken is not None:
        dt = taken + shift
        return dt.date().isoformat(), dt.isoformat(), "exif"
    if policy == "mtime":
        dt = datetime.fromtimestamp(mtime_ns / 1e9) + shift
        if metamod.plausible(dt):
            return dt.date().isoformat(), dt.isoformat(), "mtime"
    return None, None, "none"


def _resolve_destination(lib: Library, folder: str, stem: str, items: list[Staged], main_sha: str) -> tuple[str, dict[str, bool]]:
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
        existing = lib.current_sha(rel)  # 按磁盘上现在的内容；不在图库里的（链接到卡上）、读不准的都不算"同内容"
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
    ident: Identity | None = None,
) -> ImportProgress:
    """`ident` 来自挂载会话；不给就当作没有可用的卷 UUID（每个文件都读，不写 seen2）。"""
    opts = opts or ImportOptions()
    progress = progress or ImportProgress()
    cancel = cancel or threading.Event()
    reader = reader or CardReader()
    shift = timedelta(seconds=opts.shift_seconds)
    run = _Run(ident or Identity())
    layout_ok = True
    try:
        _run(lib, card, opts, progress, cancel, reader, shift, run)
    except CardRemoved:
        progress.update(
            state="interrupted",
            message="导入已中断。重新插入存储卡后可以继续。",
        )
    except Cancelled:
        progress.update(state="cancelled", message="导入已取消。")
    except NoSpace:
        progress.update(state="paused_no_space", message="电脑空间不够，导入已暂停。清理后可以继续。")
    except LibraryPathRefused:
        # 图库自己的暂存区/索引/日志按真实路径不在图库里（例如 .studio 被做成指到卡上的链接）：一个字节都不写
        layout_ok = False
        progress.update(state="failed", message=LAYOUT_REFUSED)
    except BaseException:
        progress.update(state="failed", message="导入失败。请重试。")
        raise
    finally:
        progress.update(current=None)
        if layout_ok:  # 图库目录不合格时不清理、不记日志：这些也是写入
            _finish_run(lib, card, opts, progress, run)
    return progress


def _finish_run(lib: Any, card: Any, opts: Any, progress: Any, run: _Run) -> None:
    """导入结束（不管成败）：清掉暂存区，追加一条运行记录。"""
    lib.clean_staging()
    lib.log(
        {
            "type": "run",
            "card_key": card.key,
            "card_label": card.label,
            "volume_uuid": run.ident.volume_uuid,
            "state": progress.state,
            "shift_seconds": opts.shift_seconds,
            "force_verify": opts.force_verify,
            **{k: v for k, v in progress.to_dict().items() if isinstance(v, int)},
            "unreadable": progress.unreadable,
            "verify_failed": progress.verify_failed,
            "scan_errors": progress.scan_errors,
            "write_refused": progress.write_refused,
            "coverage": progress.coverage,
        }
    )


def _run(lib: Any, card: Any, opts: Any, progress: Any, cancel: Any, reader: Any, shift: Any, run: _Run) -> None:
    progress.update(state="scanning", message="正在读取存储卡上的文件列表")
    for rel in LAYOUT:  # 暂存区、索引、日志、缩略图：按真实路径都得在图库里
        lib.check_write(rel)
    lib.clean_staging()
    scan_errors: list[str] = []
    videos: list[CardFile] = []
    shots = scan_card(card, reader, scan_errors, videos)
    if not shots and not videos and not card_present(card, reader):
        raise CardRemoved()
    # 进度按"项"算：一张照片（一对文件）或一段录像各算一项
    progress.update(total_shots=len(shots) + len(videos), state="running", message="", scan_errors=scan_errors)
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
        run.check()
        progress.update(current=f"{shot.directory}/{shot.stem}")
        try:
            _import_shot(lib, card, shot, opts, progress, cancel, reader, shift, run)
        except LibraryPathRefused:
            progress.write_refused.extend(f.relpath for f in shot.files.values())
            progress.add(failed_shots=1)
        if progress.state == "paused_no_space":
            return
        progress.add(done_shots=1)
        if opts.after_shot:
            opts.after_shot(shot.key)
    for video in videos:
        if cancel.is_set():
            raise Cancelled()
        run.check()
        progress.update(current=video.relpath)
        try:
            videos_mod.import_video(lib, card, video, opts, progress, cancel, reader, shift, run)
        except LibraryPathRefused:
            progress.write_refused.append(video.relpath)
        if progress.state == "paused_no_space":
            return
        progress.add(done_shots=1)
    run.check()
    progress.update(state="checking", current=None, message="正在核对存储卡上的照片…")
    cov = coverage.photo_coverage(lib, card.root, run.ident, reader, proofs=run.proofs, chunk=opts.chunk)
    run.check()  # 发布"都已导入"之前再确认一次：核对的确实还是这次挂载
    progress.update(state="done", coverage=cov.to_dict(), message=done_message(progress, cov))


def _seen_rows(card: Card, files: list[CardFile], shas: dict[str, str]) -> list[tuple[Any, ...]]:
    return [(card.key, f.relpath, f.size, f.mtime_ns, shas[f.relpath]) for f in files if f.relpath in shas]


def _library_rel(lib: Any, path: Path) -> str:
    return Path(path).relative_to(lib.root).as_posix()


def _confirm_placed(lib: Any, rel: str, s: Staged, progress: Any) -> None:
    """放到位之后读回实际字节：不是刚放进去的那份（放的过程中被改了），记成校验不一致，不算导入成功。"""
    if not lib.note_verified(rel, s.sha256):
        progress.verify_failed.append(s.card_file.relpath)


def _has_space(lib: Any, todo: list[CardFile], opts: Any, progress: Any) -> bool:
    need = sum(f.size for f in todo)
    free = opts.free_space(lib.root)
    if free - need < opts.min_free_bytes:
        progress.update(
            state="paused_no_space",
            needed_bytes=need,
            free_bytes=free,
            message="电脑空间不够，导入已暂停。清理后可以继续。",
        )
        return False
    return True


def _read_files(
    lib: Any, card: Any, todo: list[CardFile], known: dict[str, str], opts: Any, progress: Any, cancel: Any, reader: Any, run: _Run
) -> tuple[list[Staged], bool]:
    """每个文件各自处理：读得出的照常导入，读不出/校验不一致的明确记下来（不登记 seen，下次会再试）。
    以前见过的先只算哈希：图库里确实有（文件还在、长度相同）就记进 `known`，不再复制。"""
    staged: list[Staged] = []
    failed = False
    for cf in todo:
        try:
            if _hinted(lib, card, cf, run):
                sha, state = probe_file(reader, card, cf, opts, cancel)
                run.prove(cf, sha, state)
                if lib.sha_in_library(sha, cf.size):
                    known[cf.relpath] = sha
                    continue
            s = stage_file(reader, card, cf, lib, opts, cancel)
            run.prove(cf, s.sha256, s.state)
            staged.append(s)
        except CardReadError:
            progress.unreadable.append(cf.relpath)
            failed = True
        except VerifyFailed:
            progress.verify_failed.append(cf.relpath)
            failed = True
        except BaseException:
            _discard(staged)
            raise
    try:
        run.check()  # 读完这一批、动图库之前：还是同一次挂载吗
    except CardRemoved:
        _discard(staged)
        raise
    return staged, failed


def _import_shot(lib: Any, card: Any, shot: CardShot, opts: Any, progress: Any, cancel: Any, reader: Any, shift: Any, run: _Run) -> None:
    cfiles = list(shot.files.values())
    known: dict[str, str] = {}
    todo: list[CardFile] = []
    for f in cfiles:
        sha = None if opts.force_verify else coverage.trusted_sha(lib, run.ident, f.relpath, f.size, f.mtime_ns)
        if sha is None:
            todo.append(f)
        else:
            known[f.relpath] = sha
    if not todo:
        progress.add(seen_shots=1)
        return
    if not _has_space(lib, todo, opts, progress):
        return

    staged, failed = _read_files(lib, card, todo, known, opts, progress, cancel, reader, run)
    # 图库里那份坏了（不在、被替换、被截断）：用刚读到的原件放回原位置，被改过的文件留着
    restored = repair.restore_damaged(lib, staged, known, progress, {"card_key": card.key, "volume_uuid": run.ident.volume_uuid})
    if restored:
        progress.add(completed_shots=1)
    if failed:
        progress.add(failed_shots=1)
    all_shas = {**known, **{s.card_file.relpath: s.sha256 for s in staged}}
    if not staged:
        if known:  # 读内容核对过，图库里都有
            lib.add_seen(_seen_rows(card, cfiles, all_shas))
            lib.add_seen2(run.seen2_rows(cfiles))
            if not failed and not restored:
                progress.add(duplicate_shots=1)
        return

    fresh = [s for s in staged if lib.sha_present(s.sha256) is None]
    dups = [s for s in staged if s not in fresh]
    if not fresh:
        _discard(staged)
        lib.add_seen(_seen_rows(card, cfiles, all_shas))
        lib.add_seen2(run.seen2_rows(cfiles))
        lib.log(_log_shot("duplicate", card, shot, staged, opts, None, None, None, run))
        if not failed and not restored:
            progress.add(duplicate_shots=1)
        return
    _discard(dups)

    if complete.complete_existing(lib, card, shot, cfiles, known, fresh, dups, staged, all_shas, opts, progress, run):
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

    lib.check_write(folder)  # 先核对目录本身：经链接指到卡上的日期文件夹，连"同名同内容"都不去比
    stem, same = _resolve_destination(lib, folder, shot.stem, fresh, main.sha256)
    for item in fresh:
        lib.check_write(f"{folder}/{stem}{Path(item.card_file.relpath).suffix}")
    lib.check_write(_library_rel(lib, sc.sidecar_path(lib.abspath(f"{folder}/{stem}{Path(main.card_file.relpath).suffix}"))))
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
        _confirm_placed(lib, rel, s, progress)
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
            if src.get("dng_sha256") == (dng_rec.sha256 if dng_rec else None) and src.get("jpg_sha256") == (
                jpg_rec.sha256 if jpg_rec else None
            ):
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
                {
                    "shot_id": shot_id,
                    "orientation": rec.orientation,
                    "files": {r.kind: {"relpath": r.relpath} for r in records},
                },
                thumbs.THUMB_SIZE,
            )
        except Exception:
            pass  # nosec B110  缩略图是缓存，之后按需再生成
    seen = _seen_rows(card, cfiles, all_shas)
    lib.commit_shot(rec, seen, run.seen2_rows(cfiles))
    lib.log(_log_shot("imported", card, shot, staged, opts, rec, records, imported_info, run))
    progress.add(imported_shots=1)


def _log_file(card: Any, s: Staged, dest: str | None, run: _Run) -> dict[str, Any]:
    return {
        "kind": s.card_file.kind,
        "card_key": card.key,
        "volume_uuid": run.ident.volume_uuid,
        "card_relpath": s.card_file.relpath,
        "card_size": s.card_file.size,
        "card_mtime_ns": s.card_file.mtime_ns,
        "sha256": s.sha256,
        "dest": dest,
    }


def _log_shot(status: Any, card: Any, shot: Any, staged: Any, opts: Any, rec: Any, records: Any, info: Any, run: _Run) -> dict[Any, Any]:
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
        "files": [_log_file(card, s, dest.get(s.card_file.kind), run) for s in staged],
    }
