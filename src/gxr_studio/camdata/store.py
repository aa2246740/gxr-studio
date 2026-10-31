"""把用户自己卡上 `GXRCE.DAT` 里的理光深层行放进应用数据目录（`camera-data/`），并读回校验。

目录结构（ARCHITECTURE §4.3）::

    camera-data/
      manifest.json                 来源（文件名、大小、SHA-256）、代数、每个文件的 SHA-256、校验结果
      units/<unit>/stock04.entry    原厂 04（552 B）
      units/<unit>/recipes/NN.entry NN = 01..08（04 与 stock04 相同）
      dat-template/GXRCE.DAT        写回用的模板（用户卡上 DAT 的原样副本）

理光字节只写到 `home`（应用数据目录，永远不进仓库、不随应用分发）。写入先写临时目录、读回比对，再整体替换。
写临时目录之前和整体替换之前，都用 `privacy.check_private_dir` 核对目标不在任何 Git 工作区里（设计 §5.6）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Iterable, cast

from .. import privacy
from . import datcodec

SCHEMA = "gxr-studio/camera-data/1"
UNITS = datcodec.NAMES
SLOTS = tuple(range(1, 9))


class CamdataError(ValueError):
    pass


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


CAMERA_DATA = "camera-data"


def camera_data_dir(home: Path) -> Path:
    return Path(home) / CAMERA_DATA


def entries_from_dat(raw: bytes) -> tuple[Any, ...]:
    """解析 DAT，返回 (dat, {(unit, slot): 552 B})，unit 0..2，slot 1..8。"""
    dat = datcodec.parse(raw)
    out = {}
    for unit in range(3):
        for slot in SLOTS:
            e = dat.entry_of(unit, slot)
            if len(e) != datcodec.ENTRY_BYTES:
                raise CamdataError("条目长度不合格")
            out[(unit, slot)] = e
    return dat, out


def import_from_dat(
    raw: bytes,
    home: Path,
    *,
    source_name: str = "GXRCE.DAT",
    origin: dict[Any, Any] | None = None,
    forbidden: Iterable[Path] = (),
) -> dict[Any, Any]:
    """从 DAT 字节提取三个单元 × 8 个配方（含原厂 04）到 `home/camera-data/`，返回 manifest。

    `origin`（可选）记下来源：{"kind": "card", "label": 卷标} 或 {"kind": "file"}，写进 manifest 的 source.origin。
    `forbidden`：卷根、卡根、图库等不许落盘的目录（`privacy.forbidden_for`）。所有写入都相对已核对的目录 fd。
    """
    privacy.check_private_dir(home, "camera_data", forbidden)  # 解析之前先拒绝不合格的目录
    dat, entries = entries_from_dat(raw)
    files: dict[str, bytes] = {"dat-template/GXRCE.DAT": bytes(raw)}
    for (unit, slot), e in entries.items():
        files[f"units/{UNITS[unit]}/recipes/{slot:02d}.entry"] = e
    for unit in range(3):
        files[f"units/{UNITS[unit]}/stock04.entry"] = entries[(unit, 4)]
    manifest = {
        "schema": SCHEMA,
        "generation": dat.generation,
        "source": {"name": source_name, "size": len(raw), "sha256": _sha(raw)},
        "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "checks": {
            "dat_crc": "ok",
            "generation_registered": dat.generation,
            "pin_crc32": "ok",
            "stock04_crc": "ok",
            "round_trip_identical": True,
        },
        "files": {rel: {"size": len(b), "sha256": _sha(b)} for rel, b in sorted(files.items())},
    }
    if origin:
        manifest["source"]["origin"] = origin
    files["manifest.json"] = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode()
    write_tree(home, CAMERA_DATA, files, manifest["files"], "camera_data", forbidden)
    return manifest


def write_tree(home: Path, name: str, files: dict[str, bytes], expect: dict[str, Any], purpose: str, forbidden: Iterable[Path] = ()) -> None:
    """把 `files` 写成 `home/<name>.new/`，按 `expect` 读回比对，再整体换掉 `home/<name>`（相机数据、标定包共用）。
    全程相对已核对的应用数据目录 fd；失败时删掉自己的 `.new`，原来的 `<name>` 不动。"""
    tmp = name + ".new"
    with privacy.PrivateDir(home, purpose, forbidden) as d:
        d.remove(tmp)
        try:
            for rel, b in files.items():
                d.write(f"{tmp}/{rel}", b)
            for rel, meta in expect.items():  # 读回比对后才替换
                if _sha(d.read(f"{tmp}/{rel}")) != meta["sha256"]:
                    raise CamdataError(f"读回比对失败：{rel}")
            d.swap_in(tmp, name)
        except BaseException:
            try:
                d.remove(tmp)
            except (OSError, privacy.PrivatePathError):
                pass  # 目录已经不合格时不再碰它；残留的 .new 下次写入前会先删掉
            raise


def swap_in(new: Path, root: Path, forbidden: Iterable[Path] = ()) -> None:
    """用 `new` 整体换掉 `root`（两者同一个上级目录）。按已核对的上级目录 fd 进行，见 `privacy.PrivateDir.swap_in`：
    优先原子对调；不支持时退回"旧的改名 .old → 新的改名 root"，两步之间被打断由 `recover_interrupted_swaps` 恢复。
    上级目录不合格时删掉 `new`，不留半个输出。"""
    new, root = Path(new), Path(root)
    if new.parent != root.parent:
        raise CamdataError("swap_in 只能在同一个目录里对调")
    try:
        with privacy.PrivateDir(root.parent, "swap", forbidden) as d:
            d.swap_in(new.name, root.name)
    except privacy.PrivatePathError:
        shutil.rmtree(new, ignore_errors=True)
        raise


def recover_interrupted_swaps(home: Path, forbidden: Iterable[Path] = ()) -> list[str]:
    """启动时调用。发现 `<目录>.old`：root 不在就把 .old 改回去（换到一半被打断），root 在就说明换完了，.old 是残渣，删掉。
    返回做了什么（给日志/测试看）。应用数据目录不存在或不合格时什么都不做（不建目录，不碰它）。"""
    if not Path(home).is_dir():
        return []
    done = []
    try:
        with privacy.PrivateDir(home, "recover", forbidden) as d:
            for name in (CAMERA_DATA, "calib"):
                old = name + ".old"
                if not d.exists(old):
                    continue
                if d.exists(name):
                    d.remove(old)
                    done.append(f"{name}: removed stale .old")
                else:
                    os.rename(old, name, src_dir_fd=d.fd, dst_dir_fd=d.fd)
                    done.append(f"{name}: restored from .old")
    except privacy.PrivatePathError:
        return done
    return done


def import_from_dat_file(path: Path, home: Path) -> dict[Any, Any]:
    path = Path(path)
    return import_from_dat(path.read_bytes(), home, source_name=path.name)


def read_manifest(home: Path) -> dict[Any, Any]:
    p = camera_data_dir(home) / "manifest.json"
    if not p.is_file():
        raise CamdataError("还没有导入相机配方文件")
    m = cast(dict[str, Any], json.loads(p.read_text()))
    if m.get("schema") != SCHEMA:
        raise CamdataError("相机配方文件的版本不认识")
    return m


def verify(home: Path) -> dict[Any, Any]:
    """逐文件核对 SHA-256，并核对每个条目自带的 CRC。有任何不一致就抛 CamdataError。"""
    m = read_manifest(home)
    root = camera_data_dir(home)
    for rel, meta in m["files"].items():
        p = root / rel
        if not p.is_file() or _sha(p.read_bytes()) != meta["sha256"]:
            raise CamdataError(f"相机配方文件缺失或被改动：{rel}")
    for unit in UNITS:
        for slot in SLOTS:
            check_entry(load_entry(home, unit, slot))
    return m


def check_entry(e: bytes) -> None:
    import zlib

    if len(e) != datcodec.ENTRY_BYTES:
        raise CamdataError("条目长度不是 552")
    for off, size, crc_off in datcodec.CRC_FIELDS:
        if int.from_bytes(e[crc_off : crc_off + 4], "big") != zlib.crc32(e[off : off + size]) & 0xFFFFFFFF:
            raise CamdataError(f"条目 CRC 对不上（偏移 {crc_off}）")


def load_entry(home: Path, unit: str | int, slot: int) -> bytes:
    name = UNITS[unit] if isinstance(unit, int) else unit
    if name not in UNITS or slot not in SLOTS:
        raise CamdataError("单元或槽位不对")
    return (camera_data_dir(home) / "units" / name / "recipes" / f"{slot:02d}.entry").read_bytes()


def load_stock04(home: Path, unit: str | int) -> bytes:
    name = UNITS[unit] if isinstance(unit, int) else unit
    return (camera_data_dir(home) / "units" / name / "stock04.entry").read_bytes()


def load_template(home: Path) -> bytes:
    return (camera_data_dir(home) / "dat-template" / "GXRCE.DAT").read_bytes()
