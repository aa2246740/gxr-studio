"""缩略图与预览：优先用配对 JPG；只有 DNG 时用半分辨率路径（2×2 合并的 RAW，不用 DNG 里 160×120 的小图）。

只有 DNG 的照片没有相机 JPG 可看，所以画出来的是"没有任何编辑时 DNG 的样子"：导入了相机数据就是该单元原厂 04 的
电脑模拟（**电脑预测**，不是相机 JPG），没有就是中性转换。图片响应头和照片列表里的 `preview_look` 都会如实标注（见 `look_of`）。
半分辨率读不出来（不是 GXR 的 DNG、文件被截断…）才退回 DNG 里内嵌的小预览。

缓存键是 shot_id（内容哈希）加变体名：JPG 的变体为空；DNG-only 的变体随相机数据变化（见 `RenderService.thumb_variant`），
所以导入相机数据之后缩略图会自己重画。删掉 `.studio/thumbs/` 会自动重建。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from .. import meta as metamod
from ..raw import NEUTRAL, half_res_codes, open_dng, orient_array
from ..raw.dng import RawDecodeError
from .store import Library

THUMB_SIZE = 256
PREVIEW_SIZE = 1600
_QUALITY = {THUMB_SIZE: 82, PREVIEW_SIZE: 88}

_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


def cache_path(lib: Library, shot_id: str, size: int, variant: str = "") -> Path:
    return lib.thumbs_dir / shot_id[:2] / f"{shot_id}-{size}{'-' + variant if variant else ''}.jpg"


def purge_variants(lib: Library) -> int:
    """删掉只有 DNG 的照片的缩略图/预览缓存（带变体名的那些，如 `<id>-256-r…`）；JPG 的缩略图不动。返回删除个数。

    相机数据或标定包换了以后旧变体不会再被用到，删掉免得越攒越多；需要时会按新数据重画。
    """
    removed = 0
    for size in (THUMB_SIZE, PREVIEW_SIZE):
        for p in lib.thumbs_dir.glob(f"*/*-{size}-*.jpg"):
            if p.stem.endswith("-" + EMBEDDED_VARIANT):
                continue  # 内嵌小预览与相机数据无关，留着（照片列表靠它认出"读不出来的 RAW"）
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def preview_kind(shot: dict) -> str:
    return "jpg" if "jpg" in shot["files"] else "dng-preview"


def _render_from_jpg(path: Path, size: int) -> Image.Image:
    with Image.open(path) as im:
        w, h = im.size
        scale = size / max(w, h)
        if scale < 1:
            im.draft("RGB", (max(1, int(w * scale)), max(1, int(h * scale))))
        out = ImageOps.exif_transpose(im).convert("RGB")
    out.thumbnail((size, size), Image.Resampling.LANCZOS)
    return out


def _render_from_dng(path: Path, size: int, orientation: int) -> Image.Image | None:
    """内嵌的小预览（半分辨率读不出来时的最后退路）。"""
    im = metamod.dng_preview_image(path)
    if im is None:
        return None
    if orientation in _TRANSPOSE:
        im = im.transpose(_TRANSPOSE[orientation])
    im.thumbnail((size, size), Image.Resampling.LANCZOS)  # 只缩小，不放大
    return im


def _render_from_dng_half(path: Path, shot: dict, size: int, render) -> Image.Image | None:
    """半分辨率路径：2×2 合并的白平衡码值 → 缩到 `size` → 原厂 04 电脑模拟（没有相机数据就中性转换）。"""
    from ..render.service import shrink_codes

    try:
        dng = open_dng(path)
        try:
            codes = orient_array(half_res_codes(dng), dng.orientation)
        finally:
            dng.release()
    except RawDecodeError:
        return None
    codes = shrink_codes(np.ascontiguousarray(codes, np.float32), size)
    rgb = render.default_look(shot, codes) if render is not None else None
    if rgb is None:
        rgb = NEUTRAL.display(codes)
    return Image.fromarray(np.ascontiguousarray(rgb), "RGB")


NEUTRAL_LABEL = "中性转换（不是相机的颜色）"
EMBEDDED_LABEL = "RAW 里内嵌的小预览（相机自己生成的，很小）"
EMBEDDED_VARIANT = "emb"


def look_of(shot: dict, render=None, lib: Library | None = None) -> dict:
    """没有任何编辑时，这张照片的缩略图/大图画的是什么，界面据此标注：
    - camera-jpg：配对的相机 JPG，就是相机拍出来的样子；
    - stock04-prediction：只有 RAW，已导入相机数据——该单元原厂 04 的**电脑模拟**，是预测，不是相机 JPG；
    - neutral：只有 RAW，没有相机数据——白平衡后的中性转换，不是相机的颜色。
    - embedded：RAW 半分辨率读不出来而退回的 RAW 内嵌小预览（相机自己生成的），不是预测。导入时就会生成缩略图，
      所以这种情况在导入之后马上就能从缓存文件认出来（传了 `lib` 才查）；图片响应头也会标 `X-GXR-Look: embedded`。"""
    if "jpg" in shot["files"]:
        return {"kind": "camera-jpg", "label": None, "prediction": False}
    if lib is not None and cache_path(lib, shot["shot_id"], THUMB_SIZE, EMBEDDED_VARIANT).exists():
        return {"kind": "embedded", "label": EMBEDDED_LABEL, "prediction": False}
    label = render.default_look_label(shot) if render is not None else None
    if label:
        return {"kind": "stock04-prediction", "label": label, "prediction": True}
    return {"kind": "neutral", "label": NEUTRAL_LABEL, "prediction": False}


def ensure(lib: Library, shot: dict, size: int, render=None) -> Path | None:
    """返回缓存文件路径；原图读不出来返回 None。`render` 是 RenderService（没有就只能画中性转换）。"""
    files = shot["files"]
    variant = "" if "jpg" in files else (render.thumb_variant(shot) if render is not None else "n")
    dest = cache_path(lib, shot["shot_id"], size, variant)
    if dest.exists():
        return dest
    if variant:
        embedded = cache_path(lib, shot["shot_id"], size, EMBEDDED_VARIANT)
        if embedded.exists():
            return embedded
    try:
        if "jpg" in files:
            img = _render_from_jpg(lib.abspath(files["jpg"]["relpath"]), size)
        else:
            path = lib.abspath(files["dng"]["relpath"])
            img = _render_from_dng_half(path, shot, size, render)  # type: ignore[assignment]
            if img is None:  # 半分辨率读不出来：退回内嵌小预览，单独存成 emb 变体，响应里不当作电脑预测
                img = _render_from_dng(path, size, shot.get("orientation") or 1)
                dest = cache_path(lib, shot["shot_id"], size, EMBEDDED_VARIANT)
    except Exception:
        return None
    if img is None:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".thumb-", suffix=".tmp", dir=lib.tmp_dir)  # 每次打开图库时清空
    try:
        with os.fdopen(fd, "wb") as fh:
            img.save(fh, "JPEG", quality=_QUALITY.get(size, 85))
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest
