"""用户自定义标志的存储、上传校验、去白底与删除回退（DESIGN.md §10.4、§10.6）。

所有文件只写入应用数据目录 `home_dir / "logos"`，经 `privacy.PrivateDir` 守卫（禁止落入 Git 工作区、存储卡或图库目录），
图片解码前限制字节数、魔数与像素总数（防解压炸弹），抹除全部元数据后重新编码为干净的 PNG。
"""

from __future__ import annotations

import io
import json
import re
import secrets
import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image, ImageFilter, ImageOps
from scipy import ndimage

from .. import privacy
from .logo_math import LogoInfo

MAX_BYTES = 5 * 1024 * 1024
MIN_SIDE = 64
STORE_LONG_EDGE = 1200
MAX_LOGOS = 12
NAME_MAX = 20
MAX_PIXELS = 25_000_000
MAX_DIMENSION = 16_384
ALLOWED_FORMATS = ["PNG", "JPEG", "WEBP"]
LOGO_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")

ERR_TOO_BIG = "文件太大了（超过 5 MB）。请换一张小一点的。"
ERR_UNREADABLE = "这个文件打不开。请换一张 PNG、JPG 或 WebP 图片。"
ERR_TOO_SMALL = "图片太小了：标志至少要 64 像素高。"
ERR_LIMIT = "已经保存了 12 个标志。删除一个，再上传新的。"
ERR_NOT_FOUND = "找不到这个标志。"
ERR_BAD_NAME = "请输入标志名称（最多 20 个字）。"
WARN_OPAQUE = "这张图是不透明的，标志可能带着背景。可以打开「去掉白底」。"


class LogoError(Exception):
    def __init__(self, status: int, code: str, message: str, extra: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}


def sniff_magic(data: bytes) -> str | None:
    """按文件头魔数识别 PNG / JPEG / WebP；SVG、GIF、TIFF 等一律返回 None。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    return None


def decode_safe_rgba(data: bytes) -> Image.Image:
    """安全解码上传字节，校验魔数与像素上限，剥离所有元数据并返回全新 RGBA 图像。"""
    if len(data) > MAX_BYTES:
        raise LogoError(422, "logo_too_big", ERR_TOO_BIG)
    expected_fmt = sniff_magic(data)
    if expected_fmt is None:
        raise LogoError(422, "logo_unreadable", ERR_UNREADABLE)
    try:
        with Image.open(io.BytesIO(data), formats=ALLOWED_FORMATS) as src:
            if src.format != expected_fmt:
                raise LogoError(422, "logo_unreadable", ERR_UNREADABLE)
            if (
                src.width <= 0
                or src.height <= 0
                or src.width > MAX_DIMENSION
                or src.height > MAX_DIMENSION
                or src.width * src.height > MAX_PIXELS
            ):
                raise LogoError(422, "logo_unreadable", ERR_UNREADABLE)
            src.load()
            turned = ImageOps.exif_transpose(src)
            rgba_src = (turned if turned is not None else src).convert("RGBA")
            arr = np.array(rgba_src, dtype=np.uint8, copy=True)
    except LogoError:
        raise
    except Exception as exc:
        raise LogoError(422, "logo_unreadable", ERR_UNREADABLE) from exc
    return Image.fromarray(arr, "RGBA")


def has_white_background(img: Image.Image) -> bool:
    """不透明且四角都接近白色（>= 238）时返回 True（默认建议开启「去掉白底」）。"""
    rgba = img.convert("RGBA")
    alpha = np.asarray(rgba.getchannel("A"))
    if int(alpha.min()) < 250:
        return False
    px = np.asarray(rgba.convert("RGB"))
    corners = (px[0, 0], px[0, -1], px[-1, 0], px[-1, -1])
    return all(int(c.min()) >= 238 for c in corners)


def key_white(img: Image.Image, tol: int = 30) -> Image.Image:
    """与参考实现 `gxr_logo.key_white` 完全一致的四邻域边缘漫水去白底 + 3x3 柔化边缘。"""
    clamped_tol = max(0, min(60, int(tol)))
    rgb = np.asarray(img.convert("RGB")).astype(np.int16)
    near = (255 - rgb.min(axis=2)) <= clamped_tol
    labels, _ = ndimage.label(near)
    border_ids = np.unique(np.concatenate([labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]]))
    border_ids = border_ids[border_ids != 0]
    seen = np.isin(labels, border_ids)
    alpha = np.where(seen, 0, 255).astype(np.uint8)
    a_img = Image.fromarray(alpha, "L")
    ring = np.asarray(a_img.filter(ImageFilter.MaxFilter(3))) != np.asarray(a_img.filter(ImageFilter.MinFilter(3)))
    white_frac = (255 - rgb.min(axis=2)).clip(0, 255)
    soft = np.where(ring & ~seen, np.clip(white_frac * 255 // 160, 0, 255), alpha).astype(np.uint8)
    return Image.fromarray(np.dstack([rgb.astype(np.uint8), soft]), "RGBA")


def trim_ink(img: Image.Image, thresh: int = 8) -> tuple[Image.Image, bool]:
    """裁到 alpha > thresh 的墨迹外框；整张全透明时 has_ink 为 False。"""
    rgba = img.convert("RGBA")
    box = rgba.getchannel("A").point(lambda v: 255 if v > thresh else 0).getbbox()
    if box is None:
        return rgba, False
    return rgba.crop(box), True


def _stats_of(rgba: Image.Image) -> tuple[float, bool, float]:
    alpha_u8 = np.asarray(rgba.getchannel("A"))
    opaque = int(alpha_u8.min()) >= 250
    alpha = alpha_u8.astype(np.float32) / 255.0
    coverage = float(alpha.mean())
    rgb = np.asarray(rgba.convert("RGB"), dtype=np.float32)
    luma = rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    lum = float((luma * alpha).sum() / max(1e-6, float(alpha.sum())))
    return round(coverage, 6), opaque, round(lum, 3)


def _clean_png_bytes(rgba: Image.Image) -> bytes:
    clean = Image.fromarray(np.array(rgba.convert("RGBA"), dtype=np.uint8, copy=True), "RGBA")
    buf = io.BytesIO()
    clean.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def clean_logo_name(raw_name: str, fallback: str = "") -> str:
    """清理标志名称（去首尾空白与控制字符，最多 20 个字符）。"""
    candidate = "".join(ch for ch in (raw_name or "") if ord(ch) >= 0x20).strip()
    if not candidate and fallback:
        stem = Path(fallback.replace("\\", "/").rsplit("/", 1)[-1]).stem
        candidate = "".join(ch for ch in stem if ord(ch) >= 0x20).strip()
    if not candidate:
        candidate = "我的标志"
    return candidate[:NAME_MAX]


def prepare_uploaded_logo(
    data: bytes,
    *,
    key_mode: str | bool | None = None,
    tol: int = 30,
) -> tuple[bytes, dict[str, Any]]:
    """解码、按需去白底、裁边、校验最小短边 64px、缩放到长边 <= 1200px 并重编码为干净 PNG。"""
    clamped_tol = max(0, min(60, int(tol)))
    rgba = decode_safe_rgba(data)
    white_bg = has_white_background(rgba)
    orig_opaque = int(np.asarray(rgba.getchannel("A")).min()) >= 250
    if key_mode is None or key_mode == "auto":
        use_key = white_bg
    elif isinstance(key_mode, bool):
        use_key = key_mode
    else:
        use_key = str(key_mode).lower() in ("1", "true", "yes", "on")
    use_key = use_key and orig_opaque
    if use_key:
        rgba = key_white(rgba, clamped_tol)
    trimmed, has_ink = trim_ink(rgba)
    if not has_ink or min(trimmed.width, trimmed.height) < MIN_SIDE:
        raise LogoError(422, "logo_too_small", ERR_TOO_SMALL)
    if max(trimmed.size) > STORE_LONG_EDGE:
        scale = STORE_LONG_EDGE / max(trimmed.size)
        nw = max(1, round(trimmed.width * scale))
        nh = max(1, round(trimmed.height * scale))
        trimmed = trimmed.resize((nw, nh), Image.Resampling.LANCZOS)
    png = _clean_png_bytes(trimmed)
    with Image.open(io.BytesIO(png), formats=["PNG"]) as stored:
        stored.load()
        coverage, opaque, lum = _stats_of(stored.convert("RGBA"))
        width, height = stored.width, stored.height
    warning = WARN_OPAQUE if (orig_opaque and not white_bg and not use_key) else ""
    return png, {
        "w": width,
        "h": height,
        "keyed": bool(use_key),
        "tol": clamped_tol if use_key else 0,
        "coverage": coverage,
        "opaque": opaque,
        "lum": lum,
        "warning": warning,
    }


def _valid_logo_id(logo_id: str) -> bool:
    return bool(LOGO_ID_RE.match(logo_id)) and logo_id not in ("ricoh", "none")


class LogoStore:
    """管理 `home_dir / "logos"` 下的 `index.json` 与 `<id>.png`。"""

    def __init__(self, home_dir: Path, forbidden: Iterable[Path] = ()) -> None:
        self.home_dir = Path(home_dir)
        self.root = self.home_dir / "logos"
        self.forbidden = tuple(Path(p) for p in forbidden)
        self._lock = threading.RLock()
        self._img_cache: dict[str, tuple[int, Image.Image]] = {}

    def _read_index_unlocked(self, pdir: privacy.PrivateDir) -> list[dict[str, Any]]:
        if not pdir.exists("index.json"):
            return []
        try:
            raw = json.loads(pdir.read("index.json").decode("utf-8"))
        except (OSError, ValueError):
            return []
        items = raw.get("logos") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return []
        out: list[dict[str, Any]] = []
        for entry in items:
            if not isinstance(entry, dict):
                continue
            lid = str(entry.get("id") or "")
            if not _valid_logo_id(lid) or not pdir.exists(f"{lid}.png"):
                continue
            out.append(entry)
        return out

    def _write_index_unlocked(self, pdir: privacy.PrivateDir, entries: list[dict[str, Any]]) -> None:
        payload = json.dumps({"version": 1, "logos": entries}, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        pdir.replace_file("index.json", payload)

    def list_logos(self) -> list[dict[str, Any]]:
        privacy.check_private_dir(self.root, "logos", self.forbidden)
        if not self.root.is_dir():
            return []
        with self._lock, privacy.PrivateDir(self.root, "logos", self.forbidden) as pdir:
            return [dict(item) for item in self._read_index_unlocked(pdir)]

    def info_map(self) -> dict[str, LogoInfo]:
        out: dict[str, LogoInfo] = {}
        for item in self.list_logos():
            try:
                out[item["id"]] = LogoInfo(
                    id=str(item["id"]),
                    name=str(item.get("name") or ""),
                    w=max(1, int(item["w"])),
                    h=max(1, int(item["h"])),
                    coverage=float(item.get("coverage", 0.35)),
                    opaque=bool(item.get("opaque", False)),
                    lum=float(item.get("lum", 128.0)),
                )
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def read_png(self, logo_id: str) -> bytes:
        if not _valid_logo_id(logo_id):
            raise LogoError(404, "logo_not_found", ERR_NOT_FOUND)
        privacy.check_private_dir(self.root, "logos", self.forbidden)
        if not self.root.is_dir():
            raise LogoError(404, "logo_not_found", ERR_NOT_FOUND)
        with self._lock, privacy.PrivateDir(self.root, "logos", self.forbidden) as pdir:
            if not pdir.exists(f"{logo_id}.png"):
                raise LogoError(404, "logo_not_found", ERR_NOT_FOUND)
            return pdir.read(f"{logo_id}.png")

    def image_map(self, only_id: str | None = None) -> dict[str, Image.Image]:
        """返回供导出绘制用的 RGBA Image 字典。"""
        if only_id in (None, "", "ricoh", "none"):
            return {}
        if only_id is not None and not _valid_logo_id(only_id):
            return {}
        privacy.check_private_dir(self.root, "logos", self.forbidden)
        if not self.root.is_dir():
            return {}
        with self._lock, privacy.PrivateDir(self.root, "logos", self.forbidden) as pdir:
            fname = f"{only_id}.png"
            if not pdir.exists(fname):
                return {}
            raw = pdir.read(fname)
            cached = self._img_cache.get(only_id)
            key = hash(raw)
            if cached is not None and cached[0] == key:
                return {only_id: cached[1]}
            with Image.open(io.BytesIO(raw), formats=["PNG"]) as im:
                im.load()
                rgba = Image.fromarray(np.array(im.convert("RGBA"), dtype=np.uint8, copy=True), "RGBA")
            self._img_cache[only_id] = (key, rgba)
            return {only_id: rgba}

    def add_logo(
        self,
        data: bytes,
        *,
        name: str = "",
        filename: str = "",
        key_mode: str | bool | None = None,
        tol: int = 30,
    ) -> dict[str, Any]:
        # 先检查容量，免得已满时白算一次大图；写入时在锁内再核对一次
        privacy.check_private_dir(self.root, "logos", self.forbidden)
        if self.root.is_dir():
            with self._lock, privacy.PrivateDir(self.root, "logos", self.forbidden) as pdir:
                if len(self._read_index_unlocked(pdir)) >= MAX_LOGOS:
                    raise LogoError(422, "logo_limit_reached", ERR_LIMIT)
        png, meta = prepare_uploaded_logo(data, key_mode=key_mode, tol=tol)
        clean_name = clean_logo_name(name, fallback=filename)
        warning = meta.pop("warning", "")
        with self._lock, privacy.PrivateDir(self.root, "logos", self.forbidden) as pdir:
            entries = self._read_index_unlocked(pdir)
            if len(entries) >= MAX_LOGOS:
                raise LogoError(422, "logo_limit_reached", ERR_LIMIT)
            logo_id = f"u_{secrets.token_hex(6)}"
            while any(e["id"] == logo_id for e in entries) or pdir.exists(f"{logo_id}.png"):
                logo_id = f"u_{secrets.token_hex(6)}"
            entry: dict[str, Any] = {
                "id": logo_id,
                "name": clean_name,
                "w": meta["w"],
                "h": meta["h"],
                "keyed": meta["keyed"],
                "tol": meta["tol"],
                "created": datetime.now().astimezone().isoformat(timespec="seconds"),
                "coverage": meta["coverage"],
                "opaque": meta["opaque"],
                "lum": meta["lum"],
            }
            pdir.replace_file(f"{logo_id}.png", png)
            entries.append(entry)
            self._write_index_unlocked(pdir, entries)
            return {"logo": dict(entry), "warning": warning, "logos": [dict(e) for e in entries]}

    def rename_logo(self, logo_id: str, new_name: str) -> dict[str, Any]:
        if not _valid_logo_id(logo_id):
            raise LogoError(404, "logo_not_found", ERR_NOT_FOUND)
        cleaned = "".join(ch for ch in (new_name or "") if ord(ch) >= 0x20).strip()
        if not cleaned or len(cleaned) > NAME_MAX:
            raise LogoError(422, "bad_logo_name", ERR_BAD_NAME)
        privacy.check_private_dir(self.root, "logos", self.forbidden)
        if not self.root.is_dir():
            raise LogoError(404, "logo_not_found", ERR_NOT_FOUND)
        with self._lock, privacy.PrivateDir(self.root, "logos", self.forbidden) as pdir:
            entries = self._read_index_unlocked(pdir)
            found = None
            for entry in entries:
                if entry["id"] == logo_id:
                    entry["name"] = cleaned
                    found = dict(entry)
                    break
            if found is None:
                raise LogoError(404, "logo_not_found", ERR_NOT_FOUND)
            self._write_index_unlocked(pdir, entries)
            return {"logo": found, "logos": [dict(e) for e in entries]}

    def delete_logo(self, logo_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if not _valid_logo_id(logo_id):
            raise LogoError(404, "logo_not_found", ERR_NOT_FOUND)
        privacy.check_private_dir(self.root, "logos", self.forbidden)
        if not self.root.is_dir():
            raise LogoError(404, "logo_not_found", ERR_NOT_FOUND)
        with self._lock, privacy.PrivateDir(self.root, "logos", self.forbidden) as pdir:
            entries = self._read_index_unlocked(pdir)
            removed = next((dict(e) for e in entries if e["id"] == logo_id), None)
            if removed is None:
                raise LogoError(404, "logo_not_found", ERR_NOT_FOUND)
            kept = [e for e in entries if e["id"] != logo_id]
            self._write_index_unlocked(pdir, kept)
            pdir.remove(f"{logo_id}.png")
            self._img_cache.pop(logo_id, None)
            return removed, [dict(e) for e in kept]


def revert_logo_usages(logo_id: str, lib: Any, home_dir: Path) -> tuple[dict[str, int], bool]:
    """删除标志后，把用到它的照片 sidecar（`border`）和默认边框（`finish-defaults.json`）改回 RICOH。"""
    from ..library import sidecar as sc
    from .serve import load_defaults, save_defaults
    from .spec import FrameSpec, stored_frame

    reverted_defaults = False
    frame_def, stamp_def = load_defaults(home_dir)
    if getattr(frame_def, "logo", "ricoh") == logo_id:
        fixed_def = replace(frame_def, logo="ricoh", show_logo=True)
        save_defaults(home_dir, fixed_def.to_json(), stamp_def.to_json())
        reverted_defaults = True

    reverted_revs: dict[str, int] = {}
    shots = lib.list_shots(limit=1_000_000, offset=0) if lib is not None else []
    for shot in shots:
        with lib.lock:
            sc_path = lib.sidecar_path_for(shot)
            if not sc_path.exists() or not lib.is_local(sc_path):
                continue
            try:
                side = sc.read(sc_path)
            except sc.SidecarCorrupt:
                continue
            border = side.get("border")
            if not isinstance(border, dict) or border.get("logo") != logo_id:
                continue
            try:
                sc.check_writable(side)
            except sc.SidecarTooNew:
                continue
            parsed = stored_frame(border) or FrameSpec()
            updated = {**side, "border": replace(parsed, logo="ricoh", show_logo=True).to_json()}
            out = sc.write_atomic(sc_path, updated, tmp_dir=lib.tmp_dir)
            new_rev = int(out["rev"])
            lib.set_edit_cache(shot["shot_id"], new_rev, out.get("crop"))
            reverted_revs[str(shot["shot_id"])] = new_rev
    return reverted_revs, reverted_defaults
