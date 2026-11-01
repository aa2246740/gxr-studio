"""只比较原始 DNG/JPG 中双方都有的拍摄字段，不用文件名或文件修改时间认定配对。"""

from __future__ import annotations

import io
import math
from typing import Any

from PIL import Image

from ..raw.tiff import Tiff

Capture = dict[str, str | float]
TEXT_TAGS = {
    271: "Make",
    272: "Model",
    50708: "UniqueCameraModel",
    36867: "DateTimeOriginal",
    42036: "LensModel",
    42016: "ImageUniqueID",
}
NUMBER_TAGS = {33434: "ExposureTime", 33437: "FNumber", 34855: "ISO", 37386: "FocalLength"}
MISMATCH = "RAW 与 JPG 不是同一次拍摄"


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="replace")
    return " ".join(str(value).replace("\x00", "").split()) if value is not None else ""


def _capture(tags: dict[int, Any]) -> Capture:
    result: Capture = {}
    for tag, name in TEXT_TAGS.items():
        value = _text(tags.get(tag))
        if value and not (tag == 36867 and value.startswith("0000")):
            result[name] = value.casefold()
    subsec = _text(tags.get(37521))
    if subsec.isascii() and subsec.isdecimal():
        result["SubSecTimeOriginal"] = subsec.rstrip("0") or "0"
    for tag, name in NUMBER_TAGS.items():
        raw_value = tags.get(tag)
        if isinstance(raw_value, (tuple, list)):
            raw_value = raw_value[0] if len(raw_value) == 1 else None
        if raw_value is None:
            continue
        try:
            number = float(raw_value)
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if math.isfinite(number) and number > 0:
            result[name] = number
    return result


def from_dng(data: bytes) -> Capture:
    tiff = Tiff(data)
    root = tiff.ifd(tiff.root)
    tags = {tag: entry["value"] for tag, entry in root.items()}
    if 34665 in root:
        tags.update({tag: entry["value"] for tag, entry in tiff.ifd(root[34665]["value"][0]).items()})
    return _capture(tags)


def from_jpeg(data: bytes) -> Capture:
    with Image.open(io.BytesIO(data)) as image:
        exif = image.getexif()
        return _capture({**dict(exif), **exif.get_ifd(34665)})


def differs(raw: Capture, jpeg: Capture) -> bool:
    for name in raw.keys() & jpeg.keys():
        a, b = raw[name], jpeg[name]
        if isinstance(a, float) and isinstance(b, float):
            if not math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-9):
                return True
        elif a != b:
            return True
    return False
