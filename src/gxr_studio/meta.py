"""照片元数据：拍摄时间、尺寸、方向、ISO、镜头。只读文件头，不解码像素。

JPG 走 Pillow 的 EXIF；DNG 是 TIFF：IFD0 里有 160×120 缩略图、DateTime、方向，
EXIF 子目录里有 DateTimeOriginal/ISO/镜头，主图在 SubIFD（NewSubfileType=0），
另一个 SubIFD 是 640×480 的 JPEG 预览（已在真实样本上核实）。
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, TiffImagePlugin

EXIF_IFD = 0x8769
_DT_FORMATS = ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y/%m/%d %H:%M:%S")
MIN_YEAR = 1995


@dataclass
class PhotoMeta:
    width: int | None = None  # 按方向转正之后的尺寸
    height: int | None = None
    orientation: int = 1
    taken: datetime | None = None  # 相机时钟，没有时区
    iso: int | None = None
    lens: str | None = None
    model: str | None = None
    error: str | None = None


def parse_exif_datetime(value) -> datetime | None:
    """解析 EXIF 日期；空白、全零、乱码、不合理的年份一律当作没有。"""
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("ascii", "ignore")
    text = str(value).replace("\x00", "").strip()
    if not text or text.startswith("0000") or not re.search(r"\d{4}", text):
        return None
    for fmt in _DT_FORMATS:
        try:
            dt = datetime.strptime(text[:19] if len(text) >= 19 else text, fmt)
        except ValueError:
            continue
        return dt if plausible(dt) else None
    return None


def plausible(dt: datetime, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    return MIN_YEAR <= dt.year <= now.year + 1


def unit_from_lens(lens: str | None) -> str | None:
    """镜头名 -> 单元（粗略：只认焦距；与 DECISIONS 一致，未在 28mm/MOUNT 真机文件上核实）。"""
    if not lens:
        return None
    m = re.search(r"(\d+)\s*mm", lens)
    return f"{m.group(1)}mm" if m else None


def _clean(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("ascii", "ignore")
    text = str(value).replace("\x00", "").strip()
    return text or None


def _iso(value) -> int | None:
    if isinstance(value, (tuple, list)):
        value = value[0] if value else None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _oriented(w: int, h: int, orientation: int) -> tuple[int, int]:
    return (h, w) if orientation in (5, 6, 7, 8) else (w, h)


def _orientation(value) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return 1
    return v if 1 <= v <= 8 else 1


def read_jpeg_meta(path: Path) -> PhotoMeta:
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            try:
                sub = exif.get_ifd(EXIF_IFD)
            except Exception:
                sub = {}
            orientation = _orientation(exif.get(274, 1))
            w, h = _oriented(im.width, im.height, orientation)
            taken = parse_exif_datetime(sub.get(36867)) or parse_exif_datetime(exif.get(306))
            return PhotoMeta(
                width=w,
                height=h,
                orientation=orientation,
                taken=taken,
                iso=_iso(sub.get(34855)),
                lens=_clean(sub.get(42036)),
                model=_clean(exif.get(272)),
            )
    except Exception as exc:  # 元数据读不出来不能挡住导入
        return PhotoMeta(error=f"{type(exc).__name__}: {exc}")


def _load_subifd(im: Image.ImageFile.ImageFile, offset: int):
    little = im.tag_v2._endian == "<"  # type: ignore[attr-defined]
    ifh = b"II*\x00\x08\x00\x00\x00" if little else b"MM\x00*\x00\x00\x00\x08"
    ifd = TiffImagePlugin.ImageFileDirectory_v2(ifh=ifh)
    im.fp.seek(offset)  # type: ignore[union-attr]
    ifd.load(im.fp)  # type: ignore[arg-type]
    return ifd


def read_dng_meta(path: Path) -> PhotoMeta:
    try:
        with Image.open(path) as im:
            tags = im.tag_v2  # type: ignore[attr-defined]
            orientation = _orientation(tags.get(274, 1))
            width, height = im.width, im.height
            for off in tags.get(330, ()) or ():
                try:
                    ifd = _load_subifd(im, int(off))
                except Exception:
                    continue  # nosec B112  这个子 IFD 读不出来就看下一个
                if ifd.get(254, 0) == 0 and ifd.get(256) and ifd.get(257):
                    crop = ifd.get(50720)
                    if crop and len(crop) == 2:
                        width, height = int(crop[0]), int(crop[1])
                    else:
                        width, height = int(ifd[256]), int(ifd[257])
                    break
            try:
                sub = im.getexif().get_ifd(EXIF_IFD)
            except Exception:
                sub = {}
            w, h = _oriented(width, height, orientation)
            taken = parse_exif_datetime(sub.get(36867)) or parse_exif_datetime(tags.get(306))
            lens = _clean(sub.get(42036))
            if not lens:
                unique = _clean(tags.get(50708))
                lens = re.sub(r"^RICOH\s+", "", unique) if unique else None
            return PhotoMeta(
                width=w,
                height=h,
                orientation=orientation,
                taken=taken,
                iso=_iso(sub.get(34855)),
                lens=lens,
                model=_clean(tags.get(272)),
            )
    except Exception as exc:
        return PhotoMeta(error=f"{type(exc).__name__}: {exc}")


def dng_preview_image(path: Path) -> Image.Image | None:
    """DNG 里内嵌的最大一张 JPEG 预览（GXR：640×480）；没有就退回 IFD0 的小缩略图。"""
    try:
        with Image.open(path) as im:
            best: bytes | None = None
            best_area = 0
            for off in im.tag_v2.get(330, ()) or ():  # type: ignore[attr-defined]
                try:
                    ifd = _load_subifd(im, int(off))
                except Exception:
                    continue  # nosec B112  预览 IFD 读不出来就看下一个
                if ifd.get(259) != 7 or not ifd.get(273) or not ifd.get(279):
                    continue
                start, count = int(ifd[273][0]), int(ifd[279][0])
                area = int(ifd.get(256, 0)) * int(ifd.get(257, 0))
                if count <= 0 or count > 32 * 1024 * 1024 or area <= best_area:
                    continue
                im.fp.seek(start)  # type: ignore[union-attr]
                data = im.fp.read(count)  # type: ignore[union-attr]
                if len(data) == count:
                    best, best_area = data, area
            if best is not None:
                try:
                    pv = Image.open(io.BytesIO(best))
                    pv.load()
                    return pv.convert("RGB")
                except Exception:
                    pass  # nosec B110  内嵌预览解不开就继续走主图
            im.load()
            return im.convert("RGB")
    except Exception:
        return None
