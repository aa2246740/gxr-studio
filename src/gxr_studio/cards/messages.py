"""导入结束的提示（设计 §4.5；文案已由编排者定稿）。

只有核对完整、没有未证明的文件、也没有不会被导入的文件，才说"都已导入"。永远不说"可以格式化"。
"""

from __future__ import annotations

from typing import Any

DONE_ALL = "这张存储卡上的照片都已导入"
CHECK_FAILED = "无法核对存储卡上的照片，请勿格式化存储卡。"


def done_message(p: Any, cov: Any = None) -> str:
    """`p` 是 ImportProgress，`cov` 是 coverage.Coverage（没有核对结果时为 None）。"""
    if p.has_problems():
        bad = len(p.unreadable) + len(p.verify_failed) + len(p.scan_errors) + len(getattr(p, "write_refused", []))
        return f"有 {bad} 个文件未能导入。请勿格式化存储卡，重新插入后再试一次。"
    if cov is None or not cov.complete or cov.dcim != "present":
        return CHECK_FAILED
    if cov.uncovered:
        return f"存储卡上有 {len(cov.uncovered)} 个文件未导入。请勿格式化存储卡，重新插入后再试一次。"
    got = got_text(p)
    if cov.not_importable:
        return not_importable_message(got, len(cov.not_importable), cov.photos_imported)
    if not got:
        return DONE_ALL
    return f"{got}。现在可以推出存储卡了。"


def not_importable_message(got: str, extra: int, photos_imported: bool) -> str:
    """卡上有工作室不会导入的文件时怎么说。"照片都已导入"只在核对证明了每张照片都在图库里时才说；永远不说"可以格式化"。
    前端 web/assets/import_summary.js 的 summarizeImport 写同一句话。"""
    note = f"另有 {extra} 个其他文件不会导入"
    if photos_imported:
        return f"{got}，卡上的照片都已在图库里；{note}" if got else f"照片都已导入；{note}"
    return f"{got}。存储卡上有 {extra} 个文件不会被导入" if got else f"存储卡上有 {extra} 个文件不会被导入"


def got_text(p: Any) -> str:
    photos = p.imported_shots + p.completed_shots
    if p.imported_videos:
        return f"已导入 {photos} 张照片和 {p.imported_videos} 个视频" if photos else f"已导入 {p.imported_videos} 个视频"
    return f"已导入 {photos} 张照片" if photos else ""
