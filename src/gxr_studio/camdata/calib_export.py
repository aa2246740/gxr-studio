"""把本机的校准数据打成一个 zip，给 iPhone 版导入（「导出给 iPhone」）。

只打 `calib_dir(home)` 里的 `manifest.json` 和它列出的文件，不加别的。写之前逐个核对大小和 SHA-256；
包缺了、改过或清单不对就拒绝，文案和 `calib.model_for` / `unit_status` 一样。zip 不压缩（stored），
手机上不用解压依赖就能读。目标文件夹走导出的同一套检查（卡卷、图库），并且不能在 Git 工作区里。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import zipfile
from pathlib import Path
from typing import Any, Iterable

from .. import privacy
from . import calib

ZIP_PREFIX = "GXR 校准数据"
MAX_FILE_BYTES = 64 << 20  # 单个文件的上限，和手机上读包的上限一致
MAX_FILES = 64

MISSING = "缺少校准数据"
FILES_MISSING = "校准数据文件缺失，请重新导入"
CHANGED = "校准数据被改动，请重新导入"
BAD_LIST = "校准数据的文件清单不正确"
OUTDATED = "校准数据版本已更新，请重新导入"
UNREADABLE = "校准数据读不出来，请重新导入"


class CalibExportError(Exception):
    def __init__(self, status: int, code: str, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.detail = status, code, message, detail


def _invalid(message: str, detail: str = "") -> CalibExportError:
    return CalibExportError(422, "calibration_invalid", message, detail)


def _read_plain(folder: Path, name: str) -> bytes:
    """读校准目录里的一个普通文件。符号链接、目录和超大的文件都算包坏了。"""
    path = folder / name
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise _invalid(FILES_MISSING, name) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise _invalid(BAD_LIST, name)
    if info.st_size > MAX_FILE_BYTES:
        raise _invalid(UNREADABLE, name)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        return handle.read(MAX_FILE_BYTES + 1)


def _plain_name(name: Any) -> bool:
    if not isinstance(name, str) or not name or "\\" in name:
        return False
    return Path(name).name == name and name not in (".", "..", "manifest.json")


def collect(home: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    """核对校准包，返回 (manifest, {文件名: 字节})，字节里含 `manifest.json` 本身。"""
    folder = calib.calib_dir(home)
    if not (folder / "manifest.json").exists():
        raise CalibExportError(409, "calibration_missing", MISSING)
    raw = _read_plain(folder, "manifest.json")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _invalid(UNREADABLE, "manifest.json") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != calib.SCHEMA:
        raise _invalid(OUTDATED)
    listed = manifest.get("files")
    units = manifest.get("units")
    if not isinstance(listed, dict) or not listed or len(listed) > MAX_FILES or not isinstance(units, dict) or not units:
        raise _invalid(BAD_LIST)
    if not set(units) <= set(calib.UNIT_ISOS):
        raise _invalid(BAD_LIST)
    for unit, meta in units.items():
        models = meta.get("models") if isinstance(meta, dict) else None
        if not isinstance(models, dict):
            raise _invalid(BAD_LIST, unit)
        for branch in calib.MODEL_ISOS:
            pair = models.get(str(branch))
            if not isinstance(pair, dict) or not pair:
                raise _invalid(BAD_LIST, f"{unit}/{branch}")
            for name in pair.values():
                if not _plain_name(name) or name not in listed:
                    raise _invalid(BAD_LIST, f"{unit}/{branch}")
    files: dict[str, bytes] = {"manifest.json": raw}
    for name, meta in sorted(listed.items()):
        if not _plain_name(name) or not isinstance(meta, dict):
            raise _invalid(BAD_LIST, str(name))
        blob = _read_plain(folder, name)
        if len(blob) != meta.get("size") or hashlib.sha256(blob).hexdigest() != meta.get("sha256"):
            raise _invalid(CHANGED, name)
        files[name] = blob
    return manifest, files


def zip_name(day: str) -> str:
    return f"{ZIP_PREFIX} {day}.zip"


def _claim(dest: Path, day: str) -> tuple[Path, int]:
    """原子地占一个不重名的文件名：`GXR 校准数据 日期.zip`、`… 日期-2.zip`…"""
    for n in range(1, 1000):
        name = zip_name(day) if n == 1 else f"{ZIP_PREFIX} {day}-{n}.zip"
        path = dest / name
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            continue
        return path, fd
    raise CalibExportError(422, "too_many_names", "无法导出。同名文件太多了。")


def write_zip(files: dict[str, bytes], fd: int, when: tuple[int, int, int, int, int, int]) -> None:
    with os.fdopen(fd, "wb") as handle, zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in ["manifest.json", *sorted(n for n in files if n != "manifest.json")]:
            info = zipfile.ZipInfo(name, date_time=when)
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o600 << 16
            archive.writestr(info, files[name])


def export_for_iphone(
    home: Path,
    dest_dir: Path,
    forbidden: Iterable[Path],
    *,
    now: float | None = None,
) -> Path:
    """核对后写 `GXR 校准数据 YYYY-MM-DD.zip` 到 `dest_dir`，返回它的路径。写坏了不留半个文件。"""
    from ..export import ExportError, check_destination

    blocked = list(forbidden)
    dest = Path(dest_dir).expanduser()
    if not dest.is_absolute():
        raise CalibExportError(422, "bad_destination", "无法导出。请填写导出到的完整路径。")
    try:
        dest = check_destination(dest, blocked)
        privacy.check_private_dir(dest, "calibration_export", blocked)
    except ExportError as exc:
        raise CalibExportError(422, exc.code, exc.message, exc.detail) from exc
    except privacy.PrivatePathError as exc:
        raise CalibExportError(422, "bad_destination", "无法导出到这个位置。请换一个文件夹。", exc.detail) from exc
    _manifest, files = collect(home)
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CalibExportError(422, "export_failed", "无法导出。请检查导出文件夹能不能写入。") from exc
    local = time.localtime(time.time() if now is None else now)
    path, fd = _claim(dest, time.strftime("%Y-%m-%d", local))
    try:
        write_zip(files, fd, (max(1980, local.tm_year), local.tm_mon, local.tm_mday, local.tm_hour, local.tm_min, local.tm_sec))
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path
