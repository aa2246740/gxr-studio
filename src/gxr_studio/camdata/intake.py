"""导入入口：把用户的 `GXRCE.DAT`（卡上或本地文件）和标定包放进应用数据目录。

- 对卡**只读**：只经过 `CardReader`（`scandir` / `open_rb`），被发现的卡根目录登记进审计钩子，任何写入都会被拒绝；
- 校验在写任何东西之前做完：魔数、代数（127/129/131/135/137/138 以外一律拒绝）、两层 CRC、钉子、`pack(parse(b)) == b`、
  三个单元的原厂 04 CRC。任何一项不过，应用数据目录里原来的数据原封不动；
- 标定包有两种来源：`calib-pack.json` 清单加文件（逐个核 SHA-256，先复制到临时目录再导入，核对和导入读的是同一份字节），
  或开发机上的研究目录（仓库根或 `research/`，只在开发阶段可用）。两种都要用包里的 stock 核对已导入相机数据的 04；
- 理光字节只写到应用数据目录，不进仓库。

出错时抛 `IntakeError(status, code, message)`，message 是给用户看的中文白话。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, cast

from .. import audit
from ..cards.discovery import Card, CardReader
from . import calib, datcodec, store

DAT_NAME = "GXRCE.DAT"
MAX_DAT_BYTES = max(p.max_file for p in datcodec.PROFILES.values())
PACK_SCHEMA = "gxr-studio/calib-pack/1"
PACK_FILE = "calib-pack.json"
PACK_ISOS = calib.CALIBRATED_ISOS
MAX_PACK_FILE_BYTES = 64 << 20
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")

HOW_TO_GET_DAT = "相机配方文件在存储卡最外层，和 DCIM 并排。"


class IntakeError(Exception):
    def __init__(self, status: int, code: str, message: str, extra: dict[Any, Any] | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra or {}


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ---- 卡上找 GXRCE.DAT（只读） ------------------------------------------------------


@dataclass(frozen=True)
class CardScan:
    card: Card
    has_dcim: bool
    dat_path: Path | None
    dat_size: int | None

    @property
    def has_dat(self) -> bool:
        return self.dat_path is not None


def scan_cards_for_dat(volumes_roots: Iterable[Path], reader: CardReader | None = None) -> list[CardScan]:
    """每个卷的最外层找 DCIM 和 GXRCE.DAT（不分大小写，不进子目录）。有其一就算一张卡，并登记为受保护。"""
    reader = reader or CardReader()
    out: list[CardScan] = []
    for base in volumes_roots:
        try:
            with reader.scandir(Path(base)) as it:
                volumes = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        for vol in volumes:
            if vol.name.startswith("."):
                continue
            try:
                if not vol.is_dir():
                    continue
            except OSError:
                continue
            root = Path(vol.path)
            has_dcim, dat_path, dat_size = False, None, None
            try:
                with reader.scandir(root) as it:
                    for e in it:
                        upper = e.name.upper()
                        if upper == "DCIM" and e.is_dir():
                            has_dcim = True
                        elif upper == DAT_NAME and not e.name.startswith(".") and e.is_file(follow_symlinks=False):
                            dat_path, dat_size = Path(e.path), e.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            if has_dcim or dat_path is not None:
                audit.protect(root)
                out.append(CardScan(Card(root=root, label=vol.name), has_dcim, dat_path, dat_size))
    return out


def _read_limited(fh: Any) -> bytes:
    chunks, total = [], 0
    while True:
        chunk = fh.read(1 << 16)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_DAT_BYTES:
            raise IntakeError(
                422,
                "dat_invalid",
                "无法导入相机配方文件。文件过大。",
                {"detail": f"超过 {MAX_DAT_BYTES // 1024} KB"},
            )
    return b"".join(chunks)


def read_dat_from_card(reader: CardReader, scan: CardScan) -> bytes:
    try:
        with reader.open_rb(scan.dat_path) as fh:  # type: ignore[arg-type]
            return _read_limited(fh)
    except OSError as exc:
        raise IntakeError(409, "card_read_failed", f"无法读取存储卡「{scan.card.label}」上的 GXRCE.DAT。请重新插入后重试。") from exc


def read_dat_from_path(path: Path) -> tuple[bytes, str]:
    """本地文件；给的是文件夹就在里面找 GXRCE.DAT（比如直接选整张卡的根目录）。返回 (字节, 文件名)。"""
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise IntakeError(422, "bad_path", "无法导入。请填写完整路径。")
    try:
        if path.is_dir():
            found = next((p for p in sorted(path.iterdir()) if p.name.upper() == DAT_NAME and p.is_file()), None)
            if found is None:
                raise IntakeError(404, "dat_not_found", "无法导入相机配方文件。文件夹里没有 GXRCE.DAT。", {"detail": HOW_TO_GET_DAT})
            path = found
        elif not path.is_file():
            raise IntakeError(404, "file_not_found", "找不到这个文件。请重新选择后再试。")
        with open(path, "rb") as fh:
            return _read_limited(fh), path.name
    except IntakeError:
        raise
    except OSError as exc:
        raise IntakeError(422, "file_unreadable", "无法读取这个文件。请检查权限后再试。", {"detail": exc.strerror or str(exc)}) from exc


# ---- 校验与导入 -----------------------------------------------------------------


@dataclass(frozen=True)
class DatInfo:
    generation: int
    size: int
    sha256: str


def inspect_dat(raw: bytes) -> DatInfo:
    """写任何东西之前的全部校验。通过才返回；否则抛带中文说明的 IntakeError。"""
    if not raw:
        raise IntakeError(422, "dat_invalid", "无法导入相机配方文件。文件是空的。")
    try:
        gen = datcodec.peek_generation(raw)
        dat = datcodec.parse(raw)
        if datcodec.pack(dat) != raw:
            raise datcodec.DatError("重新封装后与原文件不一致")
        for unit in range(3):
            if datcodec.crc(dat.entry_of(unit, 4)[:448]) != datcodec.STOCK_ENTRY_CRC[unit]:
                raise datcodec.DatError(f"{datcodec.NAMES[unit]} 的原厂 04 不是出厂内容")
    except datcodec.UnsupportedGeneration as exc:
        g = datcodec.peek_generation(raw)
        raise IntakeError(
            422,
            "generation_unsupported",
            f"不支持第 {g} 代相机配方文件。请使用第 127 代或更新的版本。",
        ) from exc
    except datcodec.DatError as exc:
        if raw[:8] != b"GXRCE001":
            raise IntakeError(422, "dat_invalid", "无法导入相机配方文件。这不是 GXRCE.DAT。", {"detail": HOW_TO_GET_DAT}) from exc
        raise IntakeError(
            422,
            "dat_invalid",
            "无法导入相机配方文件。请从存储卡重新复制。",
            {"detail": str(exc)},
        ) from exc
    except Exception as exc:  # noqa: BLE001  畸形输入可能触发任何解析错误，统一当作损坏
        raise IntakeError(422, "dat_invalid", "这份 GXRCE.DAT 读不懂，没有导入。请换一份相机配方文件后再试。") from exc
    return DatInfo(gen, len(raw), _sha(raw))


def existing_source_sha(home: Path) -> str | None:
    """已导入且完好的相机数据的来源 SHA-256；没有或有问题返回 None。"""
    try:
        return cast(str, store.verify(home)["source"]["sha256"])
    except (store.CamdataError, OSError, ValueError, KeyError):
        return None


def install_dat(raw: bytes, home: Path, *, source_name: str, origin: dict[Any, Any]) -> dict[Any, Any]:
    """（已经 `inspect_dat` 通过的）DAT 提取到 `home/camera-data/`，返回 manifest。写入先落临时目录、读回比对再整体替换。"""
    try:
        return store.import_from_dat(raw, home, source_name=source_name, origin=origin)
    except (store.CamdataError, datcodec.DatError, OSError) as exc:
        raise IntakeError(500, "camdata_write_failed", "相机配方文件没能存进应用数据目录。请重试。") from exc


def describe(home: Path) -> dict[Any, Any] | None:
    """已导入相机数据的概况（不重新校验）；没有返回 None。三个单元各自是否齐全按清单里的文件算。"""
    try:
        m = store.read_manifest(home)
    except (store.CamdataError, OSError, ValueError):
        return None
    files = m.get("files", {})
    from ..render.recipes import UNIT_INFO

    units = []
    for info in UNIT_INFO:
        have = [s for s in store.SLOTS if f"units/{info['id']}/recipes/{s:02d}.entry" in files]
        stock = f"units/{info['id']}/stock04.entry" in files
        units.append(
            {
                "id": info["id"],
                "label": info["label"],
                "slots": have,
                "stock04": stock,
                "complete": stock and len(have) == len(store.SLOTS),
            }
        )
    src = m.get("source", {})
    return {
        "generation": m.get("generation"),
        "imported_at": m.get("imported_at"),
        "source": {
            "name": src.get("name"),
            "size": src.get("size"),
            "sha256": src.get("sha256"),
            "origin": src.get("origin"),
        },
        "units": units,
        "all_units_complete": all(u["complete"] for u in units),
    }


def describe_calibration(home: Path) -> dict[Any, Any] | None:
    p = calib.calib_dir(home) / "manifest.json"
    try:
        m = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    return {
        "imported_at": m.get("imported_at"),
        "origin": m.get("origin"),
        "isos": sorted(int(i) for i in m.get("sources", {})),
    }


# ---- 标定包 ---------------------------------------------------------------------


def write_pack(dest: Path, sources: dict[Any, Any]) -> dict[Any, Any]:
    """把 {iso: (npz, ring_json)} 整理成标定包文件夹（calib-pack.json + 文件）。给开发机和测试用；返回清单。"""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    isos, files = {}, {}
    for iso, (npz, ring) in sorted(sources.items()):
        n_name, r_name = f"data_{iso}.npz", f"ring_model_{iso}.json"
        shutil.copyfile(npz, dest / n_name)
        shutil.copyfile(ring, dest / r_name)
        isos[str(iso)] = {"npz": n_name, "ring": r_name}
        for name in (n_name, r_name):
            b = (dest / name).read_bytes()
            files[name] = {"size": len(b), "sha256": _sha(b)}
    manifest = {"schema": PACK_SCHEMA, "isos": isos, "files": files}
    (dest / PACK_FILE).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return manifest


_PACK_MISMATCH = "校准数据与相机配方文件不匹配"


def _bad_pack(detail: str) -> IntakeError:
    return IntakeError(422, "calibration_pack_invalid", _PACK_MISMATCH, {"detail": detail})


def _snapshot_pack(folder: Path, snap: Path) -> dict[Any, Any]:
    """校验 calib-pack.json，逐个文件复制到 `snap` 并边复制边核 SHA-256。返回 {iso: (npz, ring)}（指向 snap）。"""
    try:
        pack = json.loads((folder / PACK_FILE).read_text())
    except (OSError, ValueError) as exc:
        raise _bad_pack("校准数据的清单（calib-pack.json）读不出来或不是有效的 JSON。") from exc
    if not isinstance(pack, dict) or pack.get("schema") != PACK_SCHEMA:
        raise _bad_pack("校准数据的版本不认识，没有导入。请重新选择后再试。")
    isos, files = pack.get("isos"), pack.get("files")
    if not isinstance(isos, dict) or sorted(isos) != sorted(str(i) for i in PACK_ISOS):
        raise _bad_pack(f"校准数据必须正好包含 ISO {' 和 '.join(str(i) for i in PACK_ISOS)} 两套。请重新选择后再试。")
    if not isinstance(files, dict):
        raise _bad_pack("校准数据的清单里没有文件表。请重新选择后再试。")
    wanted: dict[str, str] = {}
    sources = {}
    for iso_s, pair in isos.items():
        if not isinstance(pair, dict) or set(pair) != {"npz", "ring"}:
            raise _bad_pack(f"ISO {iso_s} 的条目要有 npz 和 ring 两项。")
        for name in (pair["npz"], pair["ring"]):
            if not isinstance(name, str) or not _NAME_RE.match(name):
                raise _bad_pack("校准数据里的文件名只能是普通文件名。请重新选择后再试。")
            meta = files.get(name)
            if not isinstance(meta, dict) or not isinstance(meta.get("sha256"), str) or len(meta["sha256"]) != 64:
                raise _bad_pack(f"清单里没有「{name}」的 SHA-256。")
            wanted[name] = meta["sha256"].lower()
        sources[int(iso_s)] = (snap / pair["npz"], snap / pair["ring"])
    snap.mkdir(parents=True)
    for name, want in sorted(wanted.items()):
        src = folder / name
        if src.is_symlink() or not src.is_file():
            raise _bad_pack(f"校准数据缺少文件「{name}」。请重新选择后再试。")
        if src.stat().st_size > MAX_PACK_FILE_BYTES:
            raise _bad_pack(f"文件「{name}」太大，不像校准数据里的文件。请重新选择后再试。")
        data = src.read_bytes()
        if _sha(data) != want:
            raise _bad_pack(f"文件「{name}」的 SHA-256 和清单对不上，可能下载不完整或被改过，没有导入。")
        declared = files[name].get("size")
        if declared is not None and declared != len(data):
            raise _bad_pack(f"文件「{name}」的大小和清单对不上，没有导入。")
        (snap / name).write_bytes(data)
    return sources


def _dev_sources(folder: Path) -> dict[Any, Any] | None:
    """开发机：folder 是仓库根（里面有 research/）或 research/ 本身。找全两套文件才算。"""
    for root in (folder, folder.parent):
        src = calib.dev_sources(root)
        if all(p.is_file() for pair in src.values() for p in pair):
            return src
    return None


def import_calibration_folder(home: Path, folder: Path) -> tuple[dict[Any, Any], str]:
    """返回 (manifest, kind)；kind = pack | dev。要先导入相机数据（用它的原厂 04 核对包里的 stock）。"""
    folder = Path(folder).expanduser()
    if not folder.is_absolute():
        raise IntakeError(422, "bad_path", "无法导入。请填写完整路径。")
    try:
        store.load_stock04(home, "jlaunch")
    except (OSError, store.CamdataError) as exc:
        raise IntakeError(409, "camera_data_missing", "还没有导入相机配方文件。校准数据要用其中的原厂 04 核对，请先导入 GXRCE.DAT。") from exc
    if not folder.is_dir():
        raise IntakeError(404, "folder_not_found", "找不到这个文件夹。请重新选择后再试。")
    snap = Path(home) / "calib-intake.tmp"
    stale = calib.calib_dir(home).with_name(calib.calib_dir(home).name + ".new")
    try:
        if snap.exists():
            shutil.rmtree(snap)
        if (folder / PACK_FILE).is_file():
            sources, kind, origin = _snapshot_pack(folder, snap), "pack", {"kind": "pack", "folder": folder.name}
        else:
            sources = _dev_sources(folder)  # type: ignore[assignment]
            if sources is None:
                raise _bad_pack(
                    "这个文件夹里没有 calib-pack.json（校准数据的清单）。"
                    "开发阶段也可以选仓库根目录或 research/ 目录；别的用户需要另外拿到校准数据。"
                )
            kind, origin = "dev", {"kind": "dev", "folder": folder.name}
        try:
            return calib.import_calibration(home, sources, origin=origin), kind
        except calib.StockMismatch as exc:
            raise IntakeError(
                422,
                "calibration_stock_mismatch",
                _PACK_MISMATCH,
                {"detail": str(exc)},
            ) from exc
        except calib.CalibError as exc:
            raise _bad_pack("校准数据不合格，没有导入。请重新选择后再试。") from exc
        except (OSError, ValueError, KeyError, EOFError) as exc:
            raise _bad_pack("校准数据里的文件读不出来，没有导入。请重新选择后再试。") from exc
    finally:
        shutil.rmtree(snap, ignore_errors=True)
        shutil.rmtree(stale, ignore_errors=True)  # 失败时 import_calibration 留下的半成品
