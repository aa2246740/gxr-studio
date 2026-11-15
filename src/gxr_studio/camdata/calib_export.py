"""把本机的校准数据打成一个 zip，给 iPhone 版导入（「导出给 iPhone」）。

只打 `calib_dir(home)` 里的 `manifest.json` 和它列出的文件，不加别的。写之前逐个核对大小和 SHA-256；
包缺了、改过或清单不对就拒绝，文案和 `calib.model_for` / `unit_status` 一样。zip 不压缩（stored），
手机上不用解压依赖就能读。目标文件夹走导出的同一套检查（卡卷、图库），并且不能在 Git 工作区里。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
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
            # import_calibration 总是写 ctx 和 ring 两项，导出时两个分支的模型都要建：少一项就用不了。
            if not isinstance(pair, dict) or not all(isinstance(pair.get(key), str) for key in ("ctx", "ring")):
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


def zip_bytes(files: dict[str, bytes], when: tuple[int, int, int, int, int, int]) -> bytes:
    """整个 zip 先在内存里做好（校准包只有几十 KB），再一次写进已核对的目录句柄。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in ["manifest.json", *sorted(n for n in files if n != "manifest.json")]:
            info = zipfile.ZipInfo(name, date_time=when)
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o600 << 16
            archive.writestr(info, files[name])
    return buffer.getvalue()


def check_models(home: Path, manifest: dict[str, Any], files: dict[str, bytes]) -> None:
    """照 `model_for` 校验**这份快照**：包里实际有的每个单元，原厂 04 指纹要对上当前的相机数据（每个单元只读一次），
    两个 ISO 分支都要能从快照里的字节建出模型。之后写进 zip 的也是这份快照，中间不再重读文件（Sol r2）。
    只有部分单元的包照样能导出。文案照 `unit_status`。"""
    from . import store

    for unit in sorted(manifest["units"]):
        meta = manifest["units"][unit]
        try:
            stock04 = store.load_stock04(home, unit)
        except FileNotFoundError as exc:
            raise _invalid(FILES_MISSING, unit) from exc
        if meta.get("camera_stock_sha256") != calib.stock_fingerprint(stock04):
            raise _invalid("校准数据与这个单元的原厂 04 不匹配，请重新导入", unit)
        for iso in calib.MODEL_ISOS:
            pair = meta["models"][str(iso)]
            try:
                calib.build_model(stock04, files[pair["ctx"]], files[pair["ring"]])
            except Exception as exc:  # noqa: BLE001  数组、ring 或模型参数坏了，和 unit_status 一样归为读不出来
                raise _invalid(UNREADABLE, f"{unit}/{iso}") from exc


def _write_claimed(folder: privacy.PrivateDir, day: str, data: bytes) -> str:
    """在已核对的目录句柄里排他地占一个不重名的文件名并写入：`GXR 校准数据 日期.zip`、`… 日期-2.zip`…
    写到一半失败，只按这个句柄删掉自己的文件。"""
    for n in range(1, 1000):
        name = zip_name(day) if n == 1 else f"{ZIP_PREFIX} {day}-{n}.zip"
        if folder.exists(name):
            continue
        try:
            folder.write(name, data)
        except FileExistsError:
            continue
        except privacy.PrivatePathError:
            raise  # 目录在写的时候被移走：write 已把写进去的字节清空，不再对移走的目录做删除
        except BaseException:
            folder.remove(name)
            raise
        return name
    raise CalibExportError(422, "too_many_names", "无法导出。同名文件太多了。")


def export_for_iphone(
    home: Path,
    dest_dir: Path,
    forbidden: Iterable[Path],
    *,
    now: float | None = None,
) -> Path:
    """核对后写 `GXR 校准数据 YYYY-MM-DD.zip` 到 `dest_dir`，返回它的路径。

    目标核对之后立刻钉成目录句柄（`privacy.PrivateDir`，不跟随链接）：已存在的目标钉它本身，不存在的钉最近的上级，
    再通过句柄逐级建出来。之后写入、占名字和失败清理都通过这个句柄；核对之后路径被换成指向别处（哪怕是另一个允许的
    文件夹）的链接，或者写的时候目录被移走，都不会写到别的地方去。"""
    from ..export import ExportError, check_destination

    blocked = list(forbidden)
    dest = Path(dest_dir).expanduser()
    if not dest.is_absolute():
        raise CalibExportError(422, "bad_destination", "无法导出。请填写导出到的完整路径。")
    dest = Path(os.path.abspath(dest))
    # 第一次检查就在句柄上做（Sol r3）：目标已存在就打开它本身，不存在就打开最近的已存在上级。`PrivateDir` 取一次
    # realpath、核对，再从根逐级不跟随链接地打开；这一步之后路径哪一级被换成链接，打开就失败，不会被带到别处。
    # 禁止目录的检查也对句柄的实际位置做，之后建目录、写入、清理都只通过句柄，不再按路径重新解释。
    anchor: Path = dest
    missing: list[str] = []
    while not os.path.lexists(anchor) and anchor.parent != anchor:
        missing.insert(0, anchor.name)
        anchor = anchor.parent
    local = time.localtime(time.time() if now is None else now)
    try:
        with privacy.PrivateDir(anchor, "calibration_export", blocked) as base:
            try:
                check_destination(base.real.joinpath(*missing), blocked)
            except ExportError as exc:
                raise CalibExportError(422, exc.code, exc.message, exc.detail) from exc
            base.verify()
            manifest, files = collect(home)
            check_models(home, manifest, files)
            data = zip_bytes(files, (max(1980, local.tm_year), local.tm_mon, local.tm_mday, local.tm_hour, local.tm_min, local.tm_sec))
            base.verify()
            with contextlib.ExitStack() as stack:
                folder = base
                for part in missing:
                    folder = stack.enter_context(folder.child(part))
                name = _write_claimed(folder, time.strftime("%Y-%m-%d", local), data)
                return folder.real / name
    except privacy.PrivatePathError as exc:
        raise CalibExportError(422, "bad_destination", "无法导出到这个位置。请换一个文件夹。", exc.detail) from exc
    except OSError as exc:
        raise CalibExportError(422, "export_failed", "无法导出。请检查导出文件夹能不能写入。") from exc
