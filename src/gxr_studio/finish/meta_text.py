"""边框和时间戳用的显示字符串。格式按文案规范：f/2.5、1/250s、ISO 200。"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from PIL import Image

from ..library.store import Library
from ..meta import read_dng_meta, read_jpeg_meta


def _ratio(value: Any) -> float | None:
    """EXIF 有理数 -> 正的有限值；0、负数、0/0（MOUNT 装手动镜头时 FNumber 就是 0）都是"未知"，返回 None。"""
    # Pillow 的 IFDRational(0, 0) 在 float() 时会抛 ZeroDivisionError，先看分母
    if value is None or getattr(value, "denominator", 1) == 0:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        if not hasattr(value, "numerator"):
            return None
        out = float(value.numerator) / float(value.denominator)
    return out if math.isfinite(out) and out > 0 else None


def _exif(path: Any) -> dict[str, Any]:
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            sub = dict(exif.get_ifd(0x8769))
    except (OSError, ValueError, KeyError):
        return {}
    return {"exif": exif, "sub": sub, "mtime": datetime.fromtimestamp(path.stat().st_mtime)}


def frame_date(moment: datetime) -> str:
    """边框上的日期。时间戳仍用数码管自己的 'yy mm dd。"""
    return f"{moment.year:04d}.{moment.month:02d}.{moment.day:02d}"


def photo_meta(lib: Library, shot: dict[str, Any]) -> tuple[dict[str, str], datetime | None, datetime | None]:
    files = shot["files"]
    path = lib.abspath(files["jpg"]["relpath"] if "jpg" in files else files["dng"]["relpath"])
    parsed = read_jpeg_meta(path) if "jpg" in files else read_dng_meta(path)
    raw = _exif(path)
    sub = raw.get("sub") or {}
    shutter = _ratio(sub.get(33434))
    aperture = _ratio(sub.get(33437))
    if shutter is None:
        shutter_text = ""
    elif shutter >= 1:
        shutter_text = f"{shutter:g}s"
    else:
        shutter_text = f"1/{max(1, round(1 / shutter))}s"
    lens = (parsed.lens or "").removeprefix("RICOH ").strip()
    taken = parsed.taken
    file_time = raw.get("mtime") if isinstance(raw.get("mtime"), datetime) else None
    moment = taken or file_time
    date = ""
    if moment is not None:
        date = frame_date(moment)
    iso = f"ISO {parsed.iso}" if parsed.iso else ""
    short = "A12 50mm" if "50" in lens else (lens or "")
    return (
        {
            "lens": lens,
            "lens_short": short,
            "aperture": "" if aperture is None else f"f/{aperture:g}",
            "shutter": shutter_text,
            "iso": iso,
            "date": date,
            "frame_no": str(shot.get("name") or "")[-4:],
        },
        taken,
        file_time,
    )


def meta_payload(lib: Library, shot: dict[str, Any]) -> dict[str, Any]:
    info, taken, file_time = photo_meta(lib, shot)
    moment = taken or file_time
    return {
        "captured_at": moment.isoformat(timespec="seconds") if moment else None,
        "date_source": "exif" if taken else ("mtime" if file_time else None),
        "lens": info["lens"] or None,
        "aperture": info["aperture"] or None,
        "shutter": info["shutter"] or None,
        "iso": int(info["iso"].split()[-1]) if info["iso"] else None,
        "date_note": "使用文件修改日期" if file_time and not taken else None,
        # 边框上要画的字，和导出用的是同一份字符串（浏览器预览不另外拼）
        "frame": info,
    }
