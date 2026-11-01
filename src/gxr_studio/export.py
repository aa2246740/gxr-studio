"""导出：按裁切和拉直输出 JPEG 或 PNG，文件名 `原名_gxr.jpg` / `.png`。

- 有配对 JPG 时以相机 JPG 为源；只有 RAW 时用 raw/ 的渲染器出图（现在只有"中性转换"，
  文件名带 `_neutral`，EXIF 的 ImageDescription 写明；配方渲染由 S6 接入同一个 `Renderer` 接口）；
- 原文件只以 `rb` 打开；
- 带 EXIF（日期、镜头、ISO…；方向改为 1，尺寸改成输出尺寸，去掉无法搬家的 MakerNote）和 sRGB ICC；
- 同名自动加 -2、-3，不覆盖；
- 目标目录不能在存储卡上，也不能在图库里（否则会被当成新照片扫进去）。
"""

from __future__ import annotations

import io
import os
import stat
import subprocess  # nosec B404  只用来在 macOS 上执行 open -R，没有 shell
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageCms, ImageOps

from . import crop as cropmod
from .crop import Crop
from .library.store import Library
from .raw import NEUTRAL, RawDecodeError, Renderer, open_dng, render_full

SOFTWARE = "GXR Studio"
_DROP_BASE_TAGS = {274, 34665, 34853, 330, 513, 514}  # 方向、子目录指针、缩略图
_DROP_EXIF_TAGS = {37500}  # MakerNote：内部偏移搬不走
_ICC: bytes | None = None

# 尺寸档 / 质量档（ARCHITECTURE §5.5）；API 既可以直接给数字，也可以给档名
SIZE_TIERS: dict[str, int | None] = {"large": 3000, "medium": 2048, "small": 1600, "original": None}
QUALITY_TIERS: dict[str, int] = {"high": 95, "medium": 85, "low": 70}
FORMATS = {"jpeg": ("JPEG", ".jpg"), "png": ("PNG", ".png")}
RENDER_CAMERA_JPG = "camera-jpg"


class ExportError(Exception):
    def __init__(self, code: str, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


@dataclass(frozen=True)
class ExportOptions:
    long_edge: int | None = None  # None = 原始尺寸
    quality: int = 95  # PNG 无损，质量只被校验、不起作用
    fmt: str = "jpeg"  # jpeg | png

    @classmethod
    def from_tiers(cls, size: str | None = None, quality: str | None = None, fmt: str = "jpeg") -> "ExportOptions":
        if size is not None and size not in SIZE_TIERS:
            raise ExportError("bad_size", "请求无效。请刷新页面后重试。", f"尺寸：{size}")
        if quality is not None and quality not in QUALITY_TIERS:
            raise ExportError("bad_quality", "请求无效。请刷新页面后重试。", f"品质：{quality}")
        return cls(SIZE_TIERS[size] if size else None, QUALITY_TIERS[quality] if quality else 95, fmt)

    def validate(self) -> "ExportOptions":
        if self.fmt not in FORMATS:
            raise ExportError("bad_format", "请求无效。请刷新页面后重试。", "格式只能是 jpeg 或 png")
        if isinstance(self.quality, bool) or not isinstance(self.quality, int) or not 1 <= self.quality <= 100:
            raise ExportError("bad_quality", "请求无效。请刷新页面后重试。", "品质要在 1 到 100 之间。")
        if self.long_edge is not None and (
            isinstance(self.long_edge, bool) or not isinstance(self.long_edge, int) or not 64 <= self.long_edge <= 30000
        ):
            raise ExportError("bad_size", "请求无效。请刷新页面后重试。", "长边要在 64 到 30000 之间。")
        return self


class RevealError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class ExportLedger:
    """这次进程里导出过的文件。显示时只认这里的记录，不接受调用方传来的路径。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._roots: dict[str, Path] = {}
        self._paths: dict[str, list[Path]] = {}
        self._ids: dict[str, list[tuple[int, int, int]]] = {}

    def note(self, export_id: str, root: Path, path: Path) -> None:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise OSError("导出结果不能是符号链接")
        ident = (info.st_ino, info.st_size, info.st_mtime_ns)
        with self._lock:
            self._roots.setdefault(export_id, root.resolve())
            self._paths.setdefault(export_id, []).append(path)
            self._ids.setdefault(export_id, []).append(ident)

    def note_new(self, root: Path, path: Path) -> str:
        export_id = uuid.uuid4().hex[:12]
        self.note(export_id, root, path)
        return export_id

    def first(self, export_id: str) -> tuple[Path, Path, tuple[int, int, int]] | None:
        with self._lock:
            paths = self._paths.get(export_id)
            root = self._roots.get(export_id)
            idents = self._ids.get(export_id)
            if not paths or root is None or not idents:
                return None
            return root, paths[0], idents[0]


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return path.is_file()


def _host_is_macos() -> bool:
    """ "在访达中显示"只在 macOS 上可用。单独成一个函数，测试替换它来模拟别的系统，不用去改整个进程的 sys.platform。"""
    return sys.platform == "darwin"


def reveal_export(root: Path, path: Path, ident: tuple[int, int, int] | None = None) -> Path:
    """在访达中显示这次导出的文件。拒绝符号链接和被换掉的文件。"""
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise RevealError(404, "export_file_missing", "找不到导出的文件。请重新导出后再试。") from exc
    if stat.S_ISLNK(info.st_mode):
        raise RevealError(404, "export_file_missing", "无法在访达中显示。导出的文件已被替换。")
    current = (info.st_ino, info.st_size, info.st_mtime_ns)
    if ident is not None and current != ident:
        raise RevealError(404, "export_file_missing", "无法在访达中显示。导出的文件已被替换。")
    shown = path.resolve()
    if not _inside(root, shown) or shown != path.resolve():
        raise RevealError(404, "export_file_missing", "找不到导出的文件。请重新导出后再试。")
    if not _host_is_macos():
        raise RevealError(501, "finder_unavailable", "无法在访达中显示。这个功能只在 macOS 上可用。")
    try:
        subprocess.run(["open", "-R", str(shown)], check=True, timeout=15)  # nosec B603 B607  固定命令，路径是这次导出记下的文件
    except (OSError, subprocess.SubprocessError) as exc:
        raise RevealError(422, "finder_failed", "无法在访达中显示。请重试。") from exc
    return shown


@dataclass
class ExportResult:
    path: Path
    width: int
    height: int
    bytes: int
    render: str = RENDER_CAMERA_JPG  # camera-jpg | Renderer.id（例如 neutral-preview）
    render_label: str = "相机 JPG"  # 给人看的名字
    format: str = "jpeg"
    render_kind: str = "original"  # prediction | approx | original
    calibrated: bool | None = None
    detail: str = ""
    hdr: bool | None = None  # None = 没要 HDR；False = 要了但这张做不了，原因在 hdr_reason
    hdr_reason: str | None = None


def srgb_icc() -> bytes:
    global _ICC
    if _ICC is None:
        _ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    return _ICC


def render(src: Path, crop: Crop | None, opts: ExportOptions) -> tuple[bytes, tuple[int, int]]:
    """相机 JPG 作源。"""
    with Image.open(src) as im0:
        exif = im0.getexif()
        try:
            sub = dict(exif.get_ifd(0x8769))
        except Exception:
            sub = {}
        base = {k: v for k, v in exif.items() if k not in _DROP_BASE_TAGS}
        im = ImageOps.exif_transpose(im0).convert("RGB")
    return _finish(im, base, sub, crop, opts)


def _clean_text(value: Any) -> str | None:
    if isinstance(value, bytes):
        value = value.decode("ascii", "ignore")
    text = str(value).replace("\x00", "").strip() if value is not None else ""
    return text or None


def render_raw(src: Path, crop: Crop | None, opts: ExportOptions, renderer: Renderer = NEUTRAL) -> tuple[bytes, tuple[int, int]]:
    """只有 RAW：全分辨率渲染（条带式去马赛克 → 渲染器），再走与 JPG 相同的裁切/拉直/缩放/编码。

    EXIF 取自 DNG：厂商、型号、DateTime、EXIF 子目录（去掉 MakerNote）、镜头名；
    ImageDescription 写明渲染器，避免把它误当成相机 JPG。
    """
    dng = open_dng(src)
    try:
        pixels = render_full(dng, renderer)
    finally:
        dng.release()
    im = Image.fromarray(pixels, "RGB")
    base: dict[Any, Any] = {}
    sub: dict[Any, Any] = {}
    try:
        with Image.open(src) as im0:
            exif = im0.getexif()
            for tag in (271, 272, 306):
                text = _clean_text(exif.get(tag))
                if text:
                    base[tag] = text
            sub = {k: v for k, v in exif.get_ifd(0x8769).items() if k not in _DROP_EXIF_TAGS}
            if not _clean_text(sub.get(42036)):
                lens = _clean_text(exif.get(50708))  # UniqueCameraModel：GXR 的镜头单元名
                if lens:
                    sub[42036] = lens.removeprefix("RICOH ").strip()
    except Exception:  # nosec B110  元数据读不出来不挡导出
        pass
    base[270] = renderer.description or (f"GXR Studio {renderer.id}: white-balanced linear sensor data to sRGB, no recipe applied")
    return _finish(im, base, sub, crop, opts)


def crop_image(im: Image.Image, crop: Crop) -> Image.Image:
    """按裁切框和拉直角度裁出 8 位图（拉直在显示域做双三次重采样）。"""
    width, height = im.size
    x0, y0, w, h = cropmod.crop_box(crop.rect, width, height)
    x0, y0 = max(0, min(x0, width - 1)), max(0, min(y0, height - 1))
    w, h = max(1, min(w, width - x0)), max(1, min(h, height - y0))
    if abs(crop.angle_deg) < 1e-9:
        return im.crop((x0, y0, x0 + w, y0 + h))
    coeffs = cropmod.affine_coeffs((x0, y0, w, h), width, height, crop.angle_deg)
    return im.transform((w, h), Image.Transform.AFFINE, coeffs, resample=Image.Resampling.BICUBIC)


def _finish(
    im: Image.Image, base: dict[Any, Any], sub: dict[Any, Any], crop: Crop | None, opts: ExportOptions
) -> tuple[bytes, tuple[int, int]]:
    if crop is not None:
        im = crop_image(im, crop)
    if opts.long_edge and max(im.size) > opts.long_edge:
        scale = opts.long_edge / max(im.size)
        im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.Resampling.LANCZOS)

    def build_exif(full: bool) -> Image.Exif:
        out = Image.Exif()
        keep = base if full else {k: base[k] for k in (270, 271, 272, 306) if k in base}
        for k, v in keep.items():
            # ImageDescription 要能放下「电脑预测」「未校准」「近似」。Pillow 把 str 写成 ASCII，所以这里改用 UTF-8 字节。
            out[k] = v.encode("utf-8") if k == 270 and isinstance(v, str) else v
        out[274] = 1
        out[305] = SOFTWARE
        osub = out.get_ifd(0x8769)
        for k, v in sub.items():
            if k not in _DROP_EXIF_TAGS and (full or k in (36867, 36868, 34855, 42036)):
                osub[k] = v
        osub[40962], osub[40963] = im.width, im.height
        return out

    pil_format = FORMATS[opts.fmt][0]
    last: Exception | None = None
    for full in (True, False):  # 完整 EXIF 写不进去时退到只带日期/ISO/镜头
        buf = io.BytesIO()
        try:
            if pil_format == "JPEG":
                im.save(
                    buf,
                    "JPEG",
                    quality=opts.quality,
                    subsampling=0 if opts.quality >= 90 else 2,
                    optimize=True,
                    exif=build_exif(full),
                    icc_profile=srgb_icc(),
                )
            else:
                im.save(buf, "PNG", compress_level=6, exif=build_exif(full), icc_profile=srgb_icc())
            return buf.getvalue(), im.size
        except Exception as exc:
            last = exc
    raise ExportError("encode_failed", "无法导出。请重试。", str(last))


def write_with_exif(data: bytes, source: Path, note: str, *, quality: int, fmt: str) -> bytes:
    """把成片像素和源照片里能搬走的 EXIF 写在一起。先按方向转正，再写 Orientation=1。"""
    with Image.open(io.BytesIO(data)) as raw:
        im = ImageOps.exif_transpose(raw).convert("RGB")
    base: dict[Any, Any] = {}
    sub: dict[Any, Any] = {}
    try:
        with Image.open(source) as src:
            exif = src.getexif()
            for tag in (271, 272, 306):
                text = _clean_text(exif.get(tag))
                if text:
                    base[tag] = text
            sub = {k: v for k, v in exif.get_ifd(0x8769).items() if k not in _DROP_EXIF_TAGS}
            if not _clean_text(sub.get(42036)):
                lens = _clean_text(exif.get(50708))
                if lens:
                    sub[42036] = lens.removeprefix("RICOH ").strip()
    except Exception:  # nosec B110  元数据读不出来不挡导出
        pass
    base[270] = note
    payload, _size = _finish(im, base, sub, None, ExportOptions(None, quality, fmt))
    return payload


def check_destination(dest_dir: Path, forbidden: list[Path]) -> Path:
    dest = Path(os.path.abspath(dest_dir))
    real = Path(os.path.realpath(dest))
    for bad in forbidden:
        for cand in (Path(os.path.abspath(bad)), Path(os.path.realpath(bad))):
            if dest == cand or cand in dest.parents or cand in real.parents or real == cand:
                raise ExportError("bad_destination", "无法导出到这个位置。请换一个文件夹。", str(bad))
    return dest


def claim_path(dest_dir: Path, stem: str, ext: str = ".jpg", tag: str = "") -> tuple[Path, int]:
    """原子地占住一个不重名的文件名：`stem_gxr.jpg`、`stem_gxr-2.jpg`…（`tag` 例如 `_neutral` 接在 `_gxr` 后面）"""
    for n in range(1, 10000):
        name = f"{stem}_gxr{tag}{ext}" if n == 1 else f"{stem}_gxr{tag}-{n}{ext}"
        path = dest_dir / name
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        return path, fd
    raise ExportError("too_many_names", "无法导出。同名文件太多了。")


def export_shot(
    lib: Library,
    shot: dict[Any, Any],
    crop: Crop | None,
    dest_dir: Path,
    opts: ExportOptions,
    forbidden: list[Path],
    renderer: Renderer = NEUTRAL,
    use_raw: bool = False,
) -> ExportResult:
    """有配对 JPG 就用相机 JPG；只有 RAW（或 `use_raw`，例如要套配方）就用 `renderer`（默认中性转换）。
    RAW 读不出来时才报 no_jpg_source。"""
    opts.validate()
    files = shot["files"]
    if "jpg" not in files and "dng" not in files:
        raise ExportError("no_source", "无法导出。这张照片没有可用的文件。")
    dest = check_destination(dest_dir, [*forbidden, lib.root])
    if "jpg" in files and not (use_raw and "dng" in files):
        data, size = render(lib.abspath(files["jpg"]["relpath"]), crop, opts)
        kind, label, tag = RENDER_CAMERA_JPG, "相机 JPG", ""
    else:
        try:
            data, size = render_raw(lib.abspath(files["dng"]["relpath"]), crop, opts, renderer)
        except RawDecodeError as exc:
            if "jpg" in files:
                raise ExportError("raw_unreadable", "无法读取 RAW 文件。文件可能已损坏。", str(exc)) from exc
            raise ExportError("no_jpg_source", "无法读取 RAW 文件。这张照片没有相机 JPG。", str(exc)) from exc
        kind, label, tag = renderer.id, renderer.label, renderer.filename_tag
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExportError("dest_unwritable", "无法导出到这个位置。请换一个文件夹。", exc.strerror or "") from exc
    try:
        path, fd = claim_path(dest, shot["name"], FORMATS[opts.fmt][1], tag)
    except OSError as exc:
        raise ExportError("dest_unwritable", "无法导出到这个位置。请换一个文件夹。", exc.strerror or "") from exc
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return ExportResult(path=path, width=size[0], height=size[1], bytes=len(data), render=kind, render_label=label, format=opts.fmt)
