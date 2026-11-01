"""导入后在后台给 RAW+JPG 的照片生成 HDR 版本，作为派生文件放在图库的 `.studio/hdr/` 里。

    <图库>/.studio/hdr/<shot_id 前两位>/<shot_id>-v<版本>.jpg     HDR JPG（主图 = 相机 JPG 原样，加增益图）
    <图库>/.studio/hdr/<shot_id 前两位>/<shot_id>-v<版本>.json    做不了时的原因（对不齐、RAW 读不出来…）

和缩略图同一种约定：按内容哈希（shot_id）命名、只是缓存、删掉会重新生成。不放在日期文件夹里：那里的每个 JPG
重建索引时都会被当成一张照片。原 JPG 和 DNG 只以 `rb` 读，不改名、不改时间。

写入走和私有字节一样的路径：先按真实路径核对图库位置（`Library.check_write`），再用 `privacy.PrivateDir`
按不跟随链接的方式打开 `.studio/hdr`，之后的创建、改名都相对这个目录句柄，每次之前再核对一次；卡根一律禁止。
任何一步失败只记日志，不影响导入，也不影响"这张卡上的照片都已导入"的判断（这个任务在导入结束之后才开始）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import privacy
from ..library.store import Library, LibraryPathRefused
from ..observe import log
from ..raw import RawDecodeError, open_dng
from .container import ContainerError
from .gain import HdrUnsupported, Params, hdr_from_camera_jpeg, raw_luma

VERSION = 2  # 重新核对旧缓存的拍摄信息和局部对应关系
DIR = ".studio/hdr"
PURPOSE = "hdr"

READY = "ready"
UNSUPPORTED = "unsupported"
PENDING = "pending"
NONE = "none"


def _rel(shot_id: str, ext: str) -> str:
    return f"{shot_id[:2]}/{shot_id}-v{VERSION}{ext}"


def hdr_path(lib: Library, shot_id: str) -> Path:
    return lib.abspath(DIR) / _rel(shot_id, ".jpg")


def status_path(lib: Library, shot_id: str) -> Path:
    return lib.abspath(DIR) / _rel(shot_id, ".json")


def eligible(shot: dict[str, Any]) -> bool:
    files = shot.get("files") or {}
    return "jpg" in files and "dng" in files


def status_of(lib: Library, shot: dict[str, Any]) -> str:
    """ready / unsupported / pending（有 RAW+JPG，还没做）/ none（没有 RAW 或没有 JPG）。"""
    if not eligible(shot):
        return NONE
    sid = str(shot["shot_id"])
    if hdr_path(lib, sid).is_file():
        return READY
    if status_path(lib, sid).is_file():
        return UNSUPPORTED
    return PENDING


def _open_dir(lib: Library) -> privacy.PrivateDir:
    lib.check_write(f"{DIR}/x")
    return privacy.PrivateDir(lib.abspath(DIR), PURPOSE, lib.card_roots)


def _store(lib: Library, shot_id: str, ext: str, data: bytes) -> None:
    with _open_dir(lib) as d:
        d.replace_file(_rel(shot_id, ext), data)


def generate(lib: Library, shot: dict[str, Any], params: Params | None = None) -> str:
    """给一张照片生成 HDR 版本，返回状态。做不了的（对不齐、RAW 读不出来）记一个原因文件，下次不再试。"""
    sid = str(shot["shot_id"])
    files = shot["files"]
    try:
        jpeg = lib.abspath(files["jpg"]["relpath"]).read_bytes()
        raw = raw_luma(open_dng(lib.abspath(files["dng"]["relpath"])))
        out, stats = hdr_from_camera_jpeg(jpeg, raw, params)
    except (HdrUnsupported, RawDecodeError, ContainerError) as exc:
        reason = exc.reason if isinstance(exc, HdrUnsupported) else ("raw_unreadable" if isinstance(exc, RawDecodeError) else "jpeg_unreadable")
        status = {"status": UNSUPPORTED, "reason": reason, "version": VERSION}
        if isinstance(exc, HdrUnsupported) and exc.reason == "capture_mismatch":
            status["detail"] = exc.detail
        _store(lib, sid, ".json", json.dumps(status, ensure_ascii=False).encode())
        log.event(logging.INFO, "hdr_unsupported", shot_id=sid, reason=reason)
        return UNSUPPORTED
    _store(lib, sid, ".jpg", out)
    log.event(logging.INFO, "hdr_ready", shot_id=sid, gain_max_stops=stats.get("gain_max_stops"), bytes=len(out))
    return READY


@dataclass
class HdrProgress:
    state: str = "running"  # running done cancelled failed
    done: int = 0
    ready: int = 0
    unsupported: int = 0
    failed: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add(self, key: str) -> None:
        with self._lock:
            setattr(self, key, getattr(self, key) + 1)
            self.done += 1

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

    def finish(self, state: str) -> None:
        with self._lock:
            self.state = state


def pending(lib: Library, shots: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [s for s in shots if status_of(lib, s) == PENDING]


def _all_shots(lib: Library, cancel: threading.Event, page: int = 500) -> Iterator[dict[str, Any]]:
    offset = 0
    while not cancel.is_set():
        batch = lib.list_shots(limit=page, offset=offset)
        if not batch:
            break
        for shot in batch:
            if cancel.is_set():
                return
            yield shot
        offset += page


def run_pending(lib: Library, progress: HdrProgress, cancel: threading.Event, params: Params | None = None) -> HdrProgress:
    """把图库里所有还没做的 RAW+JPG 照片做一遍，新的在前。做完一轮再看一次（这期间又有导入进来的）。"""
    tried: set[str] = set()
    while not cancel.is_set():
        try:
            todo = [s for s in pending(lib, _all_shots(lib, cancel)) if s["shot_id"] not in tried]
        except Exception as exc:
            progress.finish("failed")
            log.event(logging.ERROR, "hdr_enumeration_failed", error=type(exc).__name__)
            raise  # 整轮失败交给任务表的 on_job_failed，进度也必须收束
        if not todo:
            break
        for shot in reversed(todo):
            if cancel.is_set():
                break
            tried.add(shot["shot_id"])
            started = time.monotonic()
            try:
                progress.add(generate(lib, shot, params))
            except (OSError, privacy.PrivatePathError, LibraryPathRefused) as exc:
                # 写不进去、图库被移走：只记日志，下次导入后再试
                log.event(logging.WARNING, "hdr_failed", shot_id=shot["shot_id"], error=type(exc).__name__)
                progress.add("failed")
            except Exception as exc:  # noqa: BLE001  一张出问题不拖垮整批，也不影响导入
                log.event(logging.ERROR, "hdr_failed", shot_id=shot["shot_id"], error=type(exc).__name__)
                progress.add("failed")
            log.event(logging.DEBUG, "hdr_timing", shot_id=shot["shot_id"], seconds=round(time.monotonic() - started, 2))
    progress.finish("cancelled" if cancel.is_set() else "done")
    return progress
