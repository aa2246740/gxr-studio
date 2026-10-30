"""把用户自己卡上 `GXRCE.DAT` 里的理光深层行放进应用数据目录（`camera-data/`），并读回校验。

目录结构（ARCHITECTURE §4.3）::

    camera-data/
      manifest.json                 来源（文件名、大小、SHA-256）、代数、每个文件的 SHA-256、校验结果
      units/<unit>/stock04.entry    原厂 04（552 B）
      units/<unit>/recipes/NN.entry NN = 01..08（04 与 stock04 相同）
      dat-template/GXRCE.DAT        写回用的模板（用户卡上 DAT 的原样副本）

理光字节只写到 `home`（应用数据目录，永远不进仓库、不随应用分发）。写入先写临时目录、读回比对，再整体替换。
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

from . import datcodec

SCHEMA = "gxr-studio/camera-data/1"
UNITS = datcodec.NAMES
SLOTS = tuple(range(1, 9))


class CamdataError(ValueError):
    pass


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def camera_data_dir(home: Path) -> Path:
    return Path(home) / "camera-data"


def entries_from_dat(raw: bytes):
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


def import_from_dat(raw: bytes, home: Path, *, source_name: str = "GXRCE.DAT", origin: dict | None = None) -> dict:
    """从 DAT 字节提取三个单元 × 8 个配方（含原厂 04）到 `home/camera-data/`，返回 manifest。

    `origin`（可选）记下来源：{"kind": "card", "label": 卷标} 或 {"kind": "file"}，写进 manifest 的 source.origin。
    """
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
            "dat_crc": "ok", "generation_registered": dat.generation, "pin_crc32": "ok",
            "stock04_crc": "ok", "round_trip_identical": True,
        },
        "files": {rel: {"size": len(b), "sha256": _sha(b)} for rel, b in sorted(files.items())},
    }
    root = camera_data_dir(home)
    tmp = root.with_name(root.name + ".new")
    if tmp.exists():
        shutil.rmtree(tmp)
    for rel, b in files.items():
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            f.write(b)
            f.flush()
            os.fsync(f.fileno())
    if origin:
        manifest["source"]["origin"] = origin
    (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    # 读回比对后才替换
    for rel, meta in manifest["files"].items():
        if _sha((tmp / rel).read_bytes()) != meta["sha256"]:
            shutil.rmtree(tmp)
            raise CamdataError(f"读回比对失败：{rel}")
    swap_in(tmp, root)
    return manifest


def _atomic_exchange(a: Path, b: Path) -> bool:
    """macOS：一次系统调用把两个目录对调（renamex_np + RENAME_SWAP），任何时刻 b 这个名字下都有完整的一份。
    不支持（不是 macOS、文件系统不认）返回 False。"""
    if sys.platform != "darwin":
        return False
    try:
        fn = ctypes.CDLL(None, use_errno=True).renamex_np
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
    except (OSError, AttributeError):
        return False
    return fn(os.fsencode(a), os.fsencode(b), 2) == 0


def swap_in(new: Path, root: Path) -> None:
    """用 `new` 整体换掉 `root`（相机数据、标定包共用）。

    优先用原子对调：断电或被杀也只会是"旧的"或"新的"，不会出现没有 root 的状态。
    不支持原子对调时退回"旧的改名 .old → 新的改名 root"，这两步之间被打断会留下 `.old`，
    下次启动时 `recover_interrupted_swaps` 自动恢复。"""
    new, root = Path(new), Path(root)
    if not root.exists():
        root.parent.mkdir(parents=True, exist_ok=True)
        os.replace(new, root)
        return
    if _atomic_exchange(new, root):
        shutil.rmtree(new, ignore_errors=True)       # 对调之后 new 这个名字下是旧的那份
        return
    old = root.with_name(root.name + ".old")
    if old.exists():
        shutil.rmtree(old)
    os.replace(root, old)
    os.replace(new, root)
    shutil.rmtree(old, ignore_errors=True)


def recover_interrupted_swaps(home: Path) -> list[str]:
    """启动时调用。发现 `<目录>.old`：root 不在就把 .old 改回去（换到一半被打断），root 在就说明换完了，.old 是残渣，删掉。
    返回做了什么（给日志/测试看）。"""
    done = []
    for name in ("camera-data", "calib"):
        root = Path(home) / name
        old = root.with_name(name + ".old")
        if not old.exists():
            continue
        if root.exists():
            shutil.rmtree(old, ignore_errors=True)
            done.append(f"{name}: removed stale .old")
        else:
            os.replace(old, root)
            done.append(f"{name}: restored from .old")
    return done


def import_from_dat_file(path: Path, home: Path) -> dict:
    path = Path(path)
    return import_from_dat(path.read_bytes(), home, source_name=path.name)


def read_manifest(home: Path) -> dict:
    p = camera_data_dir(home) / "manifest.json"
    if not p.is_file():
        raise CamdataError("还没有导入相机数据")
    m = json.loads(p.read_text())
    if m.get("schema") != SCHEMA:
        raise CamdataError("相机数据版本不认识")
    return m


def verify(home: Path) -> dict:
    """逐文件核对 SHA-256，并核对每个条目自带的 CRC。有任何不一致就抛 CamdataError。"""
    m = read_manifest(home)
    root = camera_data_dir(home)
    for rel, meta in m["files"].items():
        p = root / rel
        if not p.is_file() or _sha(p.read_bytes()) != meta["sha256"]:
            raise CamdataError(f"相机数据文件缺失或被改动：{rel}")
    for unit in UNITS:
        for slot in SLOTS:
            check_entry(load_entry(home, unit, slot))
    return m


def check_entry(e: bytes) -> None:
    import zlib

    if len(e) != datcodec.ENTRY_BYTES:
        raise CamdataError("条目长度不是 552")
    for off, size, crc_off in datcodec.CRC_FIELDS:
        if int.from_bytes(e[crc_off:crc_off + 4], "big") != zlib.crc32(e[off:off + size]) & 0xFFFFFFFF:
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
