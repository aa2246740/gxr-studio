"""照片核对（设计 §4.3、§4.5）：只有真正核对过内容，才说"照片都已导入"。

一张卡上的照片和录像，算"已在图库"要有证明：

- `content`：读过内容，SHA 在图库索引里，对应的文件还在，长度相同；
- `trusted_seen`：不读内容，凭 seen2 记录。四个条件缺一不可：
  1. 这次挂载有完整 UUID，不歧义、不缺失；
  2. `SEEN_TRUST_UUID` 开着；
  3. seen2 里 `(uuid, 相对路径, 大小, mtime)` 命中；
  4. 图库里有这个 SHA，文件还在，长度相同。

`SEEN_TRUST_UUID` 默认关：GXR 格式化以后卷 UUID 会不会变还没核实（设计 §16 U1）。如果每次格式化都写同一个序列号，
新照片又从 R0010001 编号，"UUID + 路径 + 大小 + mtime"就说明不了是同一个文件。核实以后由编排者改这个常量（要复审）。

核对对卡只读：只经过 `CardReader`；只有缺少证明的文件才会被读，读到的 SHA 回填进 seen2（有完整 UUID 时）。
"""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..library.store import Library
from .discovery import Card, CardFile, CardReader, StrictEntry, StrictScan, scan_card, scan_card_strict

SEEN_TRUST_UUID = False


@dataclass(frozen=True)
class Identity:
    """导入和核对需要的那一点卷身份。`volume_uuid` 只在状态 ok 时有值；None 表示不关联任何历史。"""

    volume_uuid: str | None = None
    expect: tuple[int, int] | None = None  # 会话记下的卡根 (st_dev, st_ino)
    # 这次挂载会话还有效吗（完整指纹：卡根 dev/ino、f_fsid、设备名）。导入在读取边界和发布结果之前都要问一次
    verify: Callable[[], bool] | None = field(default=None, compare=False)

    def still_valid(self) -> bool:
        return self.verify is None or self.verify()


FileState = tuple[int, int, int, int]  # (inode, 长度, mtime_ns, ctime_ns)：读内容时那个已打开文件的状态


def file_state(fh: Any) -> FileState | None:
    """已打开文件的状态（fstat）。拿不到（测试替身没有 fileno）返回 None，这时读到的内容不当作证明。"""
    try:
        st = os.fstat(fh.fileno())
    except (AttributeError, OSError, ValueError):
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


@dataclass(frozen=True)
class Proof:
    """读过内容的证明，绑定读的时候那个文件的状态。结束核对时文件的 inode、长度、mtime、ctime 任何一项不同，
    就说明读完之后被换过或改写过（哪怕长度和 mtime 改回原样，ctime 也会变），证明作废，要重新读。"""

    sha256: str
    state: FileState

    def matches(self, entry: StrictEntry) -> bool:
        return self.state == (entry.ino, entry.size, entry.mtime_ns, entry.ctime_ns)


@dataclass
class Coverage:
    complete: bool
    dcim: str
    total: int = 0
    covered: int = 0
    uncovered: list[str] = field(default_factory=list)
    not_importable: list[str] = field(default_factory=list)
    system_files: list[str] = field(default_factory=list)  # 已忽略的 macOS 元数据，不挡"都已导入"
    proof_content: int = 0
    proof_trusted: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def all_imported(self) -> bool:
        """只有这时才能说"这张存储卡上的照片都已导入"。"""
        return self.complete and self.dcim == "present" and not self.uncovered and not self.not_importable

    def to_dict(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "dcim": self.dcim,
            "total": self.total,
            "covered": self.covered,
            "uncovered": list(self.uncovered),
            "not_importable": list(self.not_importable),
            "system_files": list(self.system_files),
            "proof": {"content": self.proof_content, "trusted_seen": self.proof_trusted},
            "errors": list(self.errors),
            "all_imported": self.all_imported,
        }


def trusted_sha(lib: Library, ident: Identity, relpath: str, size: int, mtime_ns: int, *, trust: bool | None = None) -> str | None:
    """四个条件都满足时返回 seen2 里的 SHA（可以不读内容）；任何一个不满足返回 None（必须读内容）。"""
    if not (SEEN_TRUST_UUID if trust is None else trust):
        return None
    if not ident.volume_uuid:
        return None
    sha = lib.seen2_sha(ident.volume_uuid, relpath, size, mtime_ns)
    if sha is None or not lib.sha_in_library(sha, size):
        return None
    return sha


def hash_card_file(
    reader: CardReader,
    path: Path,
    size: int,
    chunk: int = 1 << 20,
    cancel: threading.Event | None = None,
    *,
    root: Path,
    expect: tuple[int, int] | None = None,
) -> tuple[str, FileState | None]:
    """只读算 SHA-256，返回 (SHA, 读的那个文件的状态)。读出的长度和目录里的不一样、或者读的过程中文件状态变了，
    就当读错。"""
    digest = hashlib.sha256()
    total = 0
    with reader.open_rb(path, root=root, expect=expect) as fh:
        before = file_state(fh)
        while True:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("cancelled")
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
            total += len(block)
        after = file_state(fh)
    if total != size:
        raise OSError(f"读出 {total} 字节，目录里写的是 {size} 字节")
    if before != after:
        raise OSError("读的过程中文件变了")
    return digest.hexdigest(), before


def photo_coverage(
    lib: Library,
    root: Path,
    ident: Identity,
    reader: CardReader | None = None,
    *,
    proofs: dict[str, Proof] | None = None,
    scan: StrictScan | None = None,
    trust: bool | None = None,
    chunk: int = 1 << 20,
) -> Coverage:
    """核对一张卡：严格扫描 + 每个照片/录像文件找证明。`proofs` 是这次导入时已经读过内容的文件。"""
    reader = reader or CardReader()
    expect = ident.expect
    if expect is None:
        try:
            st = root.lstat()
            expect = (st.st_dev, st.st_ino)
        except OSError as exc:
            return Coverage(complete=False, dcim="absent", errors=[f".: {exc}"])
    scan = scan or scan_card_strict(root, reader, expect)
    cov = Coverage(complete=scan.complete, dcim=scan.dcim, errors=list(scan.errors))
    cov.not_importable = [e.relpath for e in scan.other_files]
    cov.system_files = [e.relpath for e in scan.system_files]
    proofs = proofs or {}
    backfill: list[tuple[Any, ...]] = []
    for entry in scan.importable:
        cov.total += 1
        rel, size, mtime = entry.relpath, entry.size, entry.mtime_ns
        proof = proofs.get(rel)
        if proof is not None and proof.matches(entry):
            sha, kind = proof.sha256, "content"
        elif (trusted := trusted_sha(lib, ident, rel, size, mtime, trust=trust)) is not None:
            sha, kind = trusted, "trusted"
        else:
            kind = "content"
            try:
                sha, _ = hash_card_file(reader, root / rel, size, chunk, root=root, expect=expect)
            except OSError as exc:
                cov.complete = False
                cov.errors.append(f"{rel}: {exc}")
                cov.uncovered.append(rel)
                continue
            if ident.volume_uuid:
                backfill.append((ident.volume_uuid, rel, size, mtime, sha))
        if lib.sha_in_library(sha, size):
            cov.covered += 1
            if kind == "trusted":
                cov.proof_trusted += 1
            else:
                cov.proof_content += 1
        else:
            cov.uncovered.append(rel)
    lib.add_seen2(backfill)
    return cov


def card_summary(lib: Library, card: Card, reader: CardReader | None = None, ident: Identity | None = None) -> dict[Any, Any]:
    """轻量统计（只列目录，不读文件内容）。

    不读内容就证明不了"已导入"：`new_shots` 是缺少可信证明（`coverage.trusted_sha`）的张数。
    `SEEN_TRUST_UUID` 关着时它等于全部张数，界面据此每次插卡都去导入（导入会读内容核对）。"""
    ident = ident or Identity()
    videos: list[CardFile] = []
    shots = scan_card(card, reader, videos=videos)

    def unproven(f: CardFile) -> bool:
        return trusted_sha(lib, ident, f.relpath, f.size, f.mtime_ns) is None

    new = [s for s in shots if any(unproven(f) for f in s.files.values())]
    new_videos = [v for v in videos if unproven(v)]
    return {
        "shots": len(shots),
        "new_shots": len(new),
        "videos": len(videos),
        "new_videos": len(new_videos),
        "dng_files": sum("dng" in s.files for s in shots),
        "jpg_files": sum("jpg" in s.files for s in shots),
        "new_bytes": sum(f.size for s in new for f in s.files.values() if unproven(f)) + sum(v.size for v in new_videos),
        "verified": False,
    }
