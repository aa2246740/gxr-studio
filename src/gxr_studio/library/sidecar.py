"""sidecar：每张照片一个 `<主名>.gxrs.json`，只存"编辑意图"，不存像素（ARCHITECTURE §4.4）。

M1 只写 `source/frame/crop/import` 四块；读到不认识的键（M2 以后的 recipe/adjust/curve）原样保留。
写入：临时文件 → fsync → os.replace；rev 单调递增；读到比自己新的 schema 时拒绝改写。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

SCHEMA = "gxr-studio/edit/1"
SUFFIX = ".gxrs.json"


class SidecarTooNew(Exception):
    """sidecar 的 schema 比本程序新：只读。"""


class SidecarCorrupt(Exception):
    pass


def sidecar_path(media: Path) -> Path:
    return media.with_name(media.stem + SUFFIX)


def new_sidecar(
    *,
    dng: str | None,
    jpg: str | None,
    dng_sha256: str | None,
    jpg_sha256: str | None,
    orientation: int,
    width: int | None,
    height: int | None,
    imported: dict,
) -> dict:
    return {
        "schema": SCHEMA,
        "rev": 0,
        "source": {"dng": dng, "jpg": jpg, "dng_sha256": dng_sha256, "jpg_sha256": jpg_sha256},
        "frame": {"orientation": orientation, "width": width, "height": height},
        "crop": None,
        "import": imported,
        "modified": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _shape_problem(data: dict) -> str | None:
    """手改出来的文件：JSON 对、schema 也对，但 rev/frame/crop 的类型不对。返回第一处问题，没有就 None。
    比本程序新的 schema 不查（字段含义可能变了，只读、不改写）。"""
    rev = data.get("rev", 0)
    if isinstance(rev, bool) or not isinstance(rev, int):
        return "rev 不是整数"
    frame = data.get("frame")
    if frame is not None:
        if not isinstance(frame, dict):
            return "frame 不是对象"
        for key in ("width", "height"):
            v = frame.get(key)
            if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
                return f"frame.{key} 不是整数"
    crop = data.get("crop")
    if crop is not None:
        if not isinstance(crop, dict):
            return "crop 不是对象"
        rect = crop.get("rect")
        if not isinstance(crop.get("preset"), str) or not isinstance(rect, list) or len(rect) != 4 or not all(map(_is_num, rect)):
            return "crop 缺字段或类型不对"
        if "angle_deg" in crop and not _is_num(crop["angle_deg"]):
            return "crop.angle_deg 不是数字"
    return None


def read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SidecarCorrupt(f"{path}: {exc}") from exc
    if not isinstance(data, dict) or "schema" not in data:
        raise SidecarCorrupt(f"{path}: 不是 sidecar")
    if _schema_version(data["schema"]) <= _schema_version(SCHEMA):
        problem = _shape_problem(data)
        if problem:
            raise SidecarCorrupt(f"{path}: {problem}")
    return data


def backup_corrupt(path: Path) -> Path:
    """坏掉的 sidecar 改名留着（`<名>.corrupt-<时间>`），不删；返回新位置。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    n = 1
    while target.exists():
        n += 1
        target = path.with_name(f"{path.name}.corrupt-{stamp}-{n}")
    os.rename(path, target)
    return target


def _schema_version(schema: str) -> int:
    try:
        return int(str(schema).rsplit("/", 1)[1])
    except (IndexError, ValueError):
        return 0


def check_writable(data: dict) -> None:
    if not str(data.get("schema", "")).startswith("gxr-studio/edit/") or _schema_version(data["schema"]) > _schema_version(SCHEMA):
        raise SidecarTooNew(data.get("schema"))


def write_atomic(path: Path, data: dict, *, bump: bool = True, tmp_dir: Path | None = None) -> dict:
    """写入并返回写入后的内容；bump 为真时 rev+1。临时文件放 tmp_dir（须与 path 同一个文件系统）。"""
    check_writable(data)
    out = dict(data)
    if bump:
        out["rev"] = int(out.get("rev", 0)) + 1
        out["modified"] = datetime.now().astimezone().isoformat(timespec="seconds")
    payload = json.dumps(out, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".sidecar-", suffix=".tmp", dir=tmp_dir or path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return out
