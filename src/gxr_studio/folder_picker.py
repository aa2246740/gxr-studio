"""仅在用户请求时打开 macOS 文件夹选择器，不列出目录，也不写文件。"""

from __future__ import annotations

import subprocess  # nosec B404  固定的系统 osascript，不使用 shell
import sys
import threading
from pathlib import Path

_CHOOSER_LOCK = threading.Lock()
_SCRIPT = """
on run argv
    set startingFolder to missing value
    try
        set startingFolder to (POSIX file (item 1 of argv)) as alias
    end try
    try
        with timeout of 120 seconds
            if startingFolder is missing value then
                set chosenFolder to choose folder with prompt "选择导出文件夹"
            else
                set chosenFolder to choose folder with prompt "选择导出文件夹" default location startingFolder
            end if
        end timeout
        return POSIX path of chosenFolder
    on error number -128
        return ""
    end try
end run
"""


class FolderPickerError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _host_is_macos() -> bool:
    return sys.platform == "darwin"


def choose_export_folder(default: Path) -> Path | None:
    """路径只经 argv 传入固定脚本；取消返回 None，同一进程最多打开一个选择器。"""
    if not _host_is_macos():
        raise FolderPickerError(501, "folder_picker_unavailable", "文件夹选择器只在 macOS 上可用。请粘贴文件夹的完整路径。")
    if not _CHOOSER_LOCK.acquire(blocking=False):
        raise FolderPickerError(409, "folder_picker_busy", "文件夹选择器已经打开。请先选择文件夹或取消。")
    try:
        try:
            result = subprocess.run(  # nosec B603  固定的系统程序，路径是单独的 argv，不执行路径内容
                ["/usr/bin/osascript", "-e", _SCRIPT, str(default)],
                check=True,
                capture_output=True,
                text=True,
                timeout=125,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise FolderPickerError(503, "folder_picker_failed", "无法打开文件夹选择器。请粘贴文件夹的完整路径。") from exc
        value = result.stdout.removesuffix("\n")
        if not value:
            return None
        chosen = Path(value)
        if not chosen.is_absolute():
            raise FolderPickerError(503, "folder_picker_failed", "没有取得文件夹的完整路径。请在输入框中粘贴路径。")
        return chosen
    finally:
        _CHOOSER_LOCK.release()
