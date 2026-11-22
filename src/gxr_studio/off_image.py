"""关机图（GXROFF.JPG）编码、重封装、严格校验与安全落盘。

相机自 g260 固件起在关机时读取存储卡根目录的 ``GXROFF.JPG``，格式要求严格：
- 640×480 基线 JPEG，总字节数不超过 32,768 B；
- 段顺序唯一：SOI → 单个 DQT（130 B，8 位表 0 和表 1）→ SOF0（640×480，Y 2×1，Cb/Cr 1×1）
  → 单个 DHT（标准 Annex K.3，顺序 DC0、DC1、AC0、AC1）→ DRI 40 → 标准 SOS → 扫描数据 → EOI；
- 不含 APP、COM、EXIF；
- 扫描数据包含 59 个按顺序循环的 RST0–7（60 组 × 40 MCU），末尾紧跟 EOI。

纯 Python + Pillow 实现，无外部二进制，在 macOS 与 Windows 上一致运行。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

from . import privacy
from .export import ExportError, check_destination, srgb_icc

WIDTH = 640
HEIGHT = 480
MAX_BYTES = 32768
HEADER_BYTES = 595
DRI_VALUE = WIDTH // 16  # 40 MCUs per row
RESTARTS = HEIGHT // 8 - 1  # 59 RST markers -> 60 intervals
QUALITIES: tuple[int, ...] = (92, 88, 84, 80, 75, 70, 65, 60, 55, 50, 45, 40, 35, 30, 25, 20, 15, 10)
OFF_IMAGE_FILENAME = "GXROFF.JPG"

# ITU-T T.81 Annex K.3 标准霍夫曼表（固定顺序：DC0、DC1、AC0、AC1）
ANNEX_K: tuple[tuple[int, bytes, bytes], ...] = (
    (0x00, bytes([0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0]), bytes(range(12))),
    (0x01, bytes([0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0]), bytes(range(12))),
    (
        0x10,
        bytes([0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7D]),
        bytes.fromhex(
            "01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a"
            "161718191a25262728292a3435363738393a434445464748494a535455565758595a6364656667"
            "68696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3"
            "b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4"
            "f5f6f7f8f9fa"
        ),
    ),
    (
        0x11,
        bytes([0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 0x77]),
        bytes.fromhex(
            "000102031104052131061241510761711322328108144291a1b1c109233352f0156272d10a16"
            "2434e125f11718191a262728292a35363738393a434445464748494a535455565758595a636465"
            "666768696a737475767778797a82838485868788898a92939495969798999aa2a3a4a5a6a7a8a9"
            "aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3"
            "f4f5f6f7f8f9fa"
        ),
    ),
)

_EXPECTED_DHT_ORDER: tuple[int, ...] = (0x00, 0x01, 0x10, 0x11)
_EXPECTED_SOS_BODY = bytes([3, 1, 0x00, 2, 0x11, 3, 0x11, 0, 63, 0])
_EXPECTED_SOF0_BODY = struct.pack(">BHHB", 8, HEIGHT, WIDTH, 3) + bytes([1, 0x21, 0, 2, 0x11, 1, 3, 0x11, 1])


@dataclass(frozen=True)
class CheckIssue:
    code: str
    message: str


@dataclass(frozen=True)
class OffImageCheck:
    ok: bool
    dat_state: int
    errors: tuple[CheckIssue, ...]


@dataclass(frozen=True)
class OffImageResult:
    data: bytes
    bytes: int
    quality: int
    width: int
    height: int
    check: OffImageCheck
    sha256: str


def _segment(marker: int, body: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(body) + 2) + body


def fixed_tail() -> bytes:
    """合法关机图的固定头尾段（偏移 136..595）：SOF0、DHT、DRI、SOS。"""
    dht = b"".join(bytes([tc]) + bits + vals for tc, bits, vals in ANNEX_K)
    dri = struct.pack(">H", DRI_VALUE)
    out = _segment(0xC0, _EXPECTED_SOF0_BODY) + _segment(0xC4, dht) + _segment(0xDD, dri) + _segment(0xDA, _EXPECTED_SOS_BODY)
    return out


_FIXED_TAIL = fixed_tail()


def header_crc() -> int:
    return zlib.crc32(_FIXED_TAIL) & 0xFFFFFFFF


class _Bits:
    """严格位流读取器（与固件 off_walk 一致：间隔内 0xFF 必须紧跟 0x00）。"""

    def __init__(self, data: bytes, pos: int, end: int) -> None:
        self.data = data
        self.pos = pos
        self.end = end
        self.acc = 0
        self.n = 0

    def bit(self) -> int:
        if not self.n:
            if self.pos >= self.end:
                return -1
            c = self.data[self.pos]
            if c == 0xFF:
                if self.pos + 1 >= self.end or self.data[self.pos + 1]:
                    return -1
                self.pos += 1
            self.pos += 1
            self.acc, self.n = c, 8
        self.n -= 1
        return (self.acc >> self.n) & 1

    def bits(self, k: int) -> int:
        v = 0
        for _ in range(k):
            x = self.bit()
            if x < 0:
                return -1
            v = (v << 1) | x
        return v

    def sym(self, table: bytes) -> int:
        code = first = idx = 0
        for length in range(16):
            x = self.bit()
            if x < 0:
                return -1
            code |= x
            cnt = table[length]
            if code - first < cnt:
                return table[16 + idx + code - first]
            idx += cnt
            first = (first + cnt) << 1
            code <<= 1
        return -1


def _walk_entropy(data: bytes, end: int) -> bool:
    """严格解码 60 个重启间隔（每组 40 MCU，Y Y Cb Cr），确认无越界系数、1-bit 补齐、RST0..7 连续且正好到 end。"""
    dc = (data[160:205], data[189:234])
    ac = (data[218:413], data[397:575])
    b = _Bits(data, HEADER_BYTES, end)
    for iv in range(RESTARTS + 1):
        b.n = 0
        for m in range(160):
            comp = (m & 3) >> 1
            s = b.sym(dc[comp])
            if s < 0 or b.bits(s) < 0:
                return False
            k = 1
            while k < 64:
                rs = b.sym(ac[comp])
                if rs < 0:
                    return False
                if not (rs & 15):
                    if rs != 0xF0:
                        break
                    k += 16
                    continue
                k += rs >> 4
                if b.bits(rs & 15) < 0:
                    return False
                k += 1
            if k > 64:
                return False
        if b.n and (b.acc & ((1 << b.n) - 1)) != (1 << b.n) - 1:
            return False
        if iv < RESTARTS:
            if b.pos + 1 >= b.end or data[b.pos] != 0xFF or data[b.pos + 1] != (0xD0 | (iv & 7)):
                return False
            b.pos += 2
    return b.pos == b.end


def _parse_header_segments(data: bytes) -> tuple[list[tuple[int, bytes]], int | None, str | None]:
    """从偏移 2 走到 SOS，返回 (段列表, 扫描起始偏移, 截断错误)。"""
    segs: list[tuple[int, bytes]] = []
    pos = 2
    n = len(data)
    while pos < n:
        if data[pos] != 0xFF:
            return segs, None, f"偏移 {pos} 处缺少段标记"
        while pos < n and data[pos] == 0xFF:
            pos += 1
        if pos >= n:
            return segs, None, "文件在段标记处截断"
        marker = data[pos]
        pos += 1
        if marker == 0xD9:
            return segs, None, "遇到 EOI 之前没有找到 SOS"
        if pos + 2 > n:
            return segs, None, f"段 FF{marker:02X} 缺少长度字段"
        length = struct.unpack(">H", data[pos : pos + 2])[0]
        if length < 2 or pos + length > n:
            return segs, None, f"段 FF{marker:02X} 长度越界"
        body = data[pos + 2 : pos + length]
        segs.append((marker, body))
        pos += length
        if marker == 0xDA:
            return segs, pos, None
    return segs, None, "缺少扫描头 SOS"


def _diagnose_dqt(dqts: list[bytes], issues: list[CheckIssue]) -> None:
    if not dqts:
        issues.append(CheckIssue("missing_dqt", "缺少量化表段 DQT。"))
    elif len(dqts) > 1:
        issues.append(CheckIssue("multiple_dqt", f"存在 {len(dqts)} 个 DQT 段，必须合并为 1 个 DQT 段（表 0 和表 1）。"))
    elif len(dqts[0]) != 130 or dqts[0][0] != 0 or dqts[0][65] != 1:
        issues.append(CheckIssue("bad_dqt", "DQT 段格式不对（需要 130 B，包含 8 位量化表 0 和 1）。"))


def _diagnose_sof(segs: list[tuple[int, bytes]], issues: list[CheckIssue]) -> None:
    sof0s = [body for m, body in segs if m == 0xC0]
    other_sofs = [m for m, _ in segs if m in (0xC1, 0xC2, 0xC3)]
    if other_sofs or not sof0s:
        issues.append(CheckIssue("missing_sof0", "缺少基线帧头 SOF0（仅支持基线 JPEG）。"))
        return
    if len(sof0s) > 1 or len(sof0s[0]) < 6:
        issues.append(CheckIssue("bad_sof0", "SOF0 帧头不完整。"))
        return
    body = sof0s[0]
    precision, height, width, ncomp = struct.unpack(">BHHB", body[:6])
    if (width, height) != (WIDTH, HEIGHT):
        issues.append(CheckIssue("bad_dimensions", f"尺寸为 {width}×{height}，必须为 {WIDTH}×{HEIGHT}。"))
    if ncomp != 3 or len(body) != 6 + 9:
        issues.append(CheckIssue("bad_subsampling", "必须为 3 分量 4:2:2 采样（Y 2×1，Cb/Cr 1×1）。"))
        return
    comps = [(body[6 + 3 * i], body[7 + 3 * i], body[8 + 3 * i]) for i in range(3)]
    samplings = (comps[0][1], comps[1][1], comps[2][1])
    if samplings != (0x21, 0x11, 0x11):
        if samplings == (0x22, 0x11, 0x11):
            issues.append(CheckIssue("bad_subsampling", "色度采样为 4:2:0，必须为 4:2:2（Y 2×1，Cb/Cr 1×1）。"))
        else:
            issues.append(CheckIssue("bad_subsampling", "色度采样不是 4:2:2（Y 2×1，Cb/Cr 1×1）。"))
    elif precision != 8 or body != _EXPECTED_SOF0_BODY:
        issues.append(CheckIssue("bad_sof0", "SOF0 帧头参数不符合要求。"))


def _diagnose_dht(dhts: list[bytes], issues: list[CheckIssue]) -> None:
    if not dhts:
        issues.append(CheckIssue("missing_dht", "缺少霍夫曼表段 DHT。"))
        return
    if len(dhts) > 1:
        issues.append(CheckIssue("multiple_dht", f"存在 {len(dhts)} 个 DHT 段，必须合并为 1 个 DHT 段。"))
        return
    rem = dhts[0]
    parsed: list[tuple[int, bytes, bytes]] = []
    while rem:
        if len(rem) < 17:
            issues.append(CheckIssue("bad_dht", "DHT 段截断。"))
            return
        tc = rem[0]
        bits = rem[1:17]
        count = sum(bits)
        size = 17 + count
        if len(rem) < size:
            issues.append(CheckIssue("bad_dht", "DHT 表项长度越界。"))
            return
        parsed.append((tc, bits, rem[17:size]))
        rem = rem[size:]
    order = tuple(tc for tc, _, _ in parsed)
    if order != _EXPECTED_DHT_ORDER:
        issues.append(CheckIssue("bad_dht_order", "DHT 表顺序错误（必须为 DC0、DC1、AC0、AC1）。"))
        return
    if tuple(parsed) != ANNEX_K:
        issues.append(CheckIssue("bad_dht", "DHT 不是标准 Annex K.3 霍夫曼表。"))


def _diagnose_dri_sos_order(segs: list[tuple[int, bytes]], issues: list[CheckIssue]) -> None:
    dris = [body for m, body in segs if m == 0xDD]
    if not dris:
        issues.append(CheckIssue("missing_dri", f"缺少重启间隔段 DRI（必须为 {DRI_VALUE}）。"))
    elif len(dris) > 1 or len(dris[0]) != 2:
        issues.append(CheckIssue("bad_dri", f"DRI 段格式不对（必须为 1 个且值为 {DRI_VALUE}）。"))
    else:
        val = struct.unpack(">H", dris[0])[0]
        if val != DRI_VALUE:
            issues.append(CheckIssue("bad_dri", f"DRI 重启间隔为 {val}，必须为 {DRI_VALUE}。"))

    soss = [body for m, body in segs if m == 0xDA]
    if not soss or soss[0] != _EXPECTED_SOS_BODY:
        issues.append(CheckIssue("bad_sos", "SOS 扫描头不符合标准 3 分量基线布局。"))

    markers = [m for m, _ in segs]
    if not issues and markers != [0xDB, 0xC0, 0xC4, 0xDD, 0xDA]:
        issues.append(CheckIssue("bad_segment_order", "段顺序不符合要求（必须依次为 SOI、DQT、SOF0、DHT、DRI、SOS）。"))


def _diagnose_scan_and_eoi(data: bytes, scan_start: int, issues: list[CheckIssue]) -> int | None:
    """扫描熵数据，检查 EOI、尾部多余字节与 59 个 RST0–7 标记；返回 EOI 前填充字节边界。"""
    n = len(data)
    eoi_pos: int | None = None
    restarts = 0
    bad_marker = False
    i = scan_start
    while i < n:
        if data[i] == 0xFF:
            j = i + 1
            while j < n and data[j] == 0xFF:
                j += 1
            if j >= n:
                issues.append(CheckIssue("missing_eoi", "文件末尾缺少 EOI 标记。"))
                return None
            code = data[j]
            if code == 0x00:
                if j > i + 1:
                    issues.append(CheckIssue("unexpected_marker", f"偏移 {i} 处出现非法连续 FF 填充字节。"))
                    bad_marker = True
                    break
                i = j + 1
                continue
            if code == 0xD9:
                eoi_pos = j - 1
                if j + 1 < n:
                    extra = n - (j + 1)
                    issues.append(CheckIssue("trailing_bytes", f"EOI 之后有多余的 {extra} 字节。"))
                break
            if 0xD0 <= code <= 0xD7:
                if j > i + 1:
                    issues.append(CheckIssue("unexpected_marker", f"偏移 {i} 处 RST 标记前有非法填充字节。"))
                    bad_marker = True
                    break
                expected = 0xD0 | (restarts & 7)
                if code != expected:
                    issues.append(
                        CheckIssue(
                            "bad_rst_order",
                            f"重启标记顺序错误：第 {restarts + 1} 个应为 RST{restarts & 7}，实际为 RST{code & 7}。",
                        )
                    )
                    bad_marker = True
                    break
                restarts += 1
                i = j + 1
                continue
            issues.append(CheckIssue("unexpected_marker", f"扫描数据中出现意外标记 FF{code:02X}。"))
            bad_marker = True
            break
        i += 1

    if eoi_pos is None and not bad_marker:
        issues.append(CheckIssue("missing_eoi", "文件末尾缺少 EOI 标记。"))
        return None
    if not bad_marker and restarts != RESTARTS:
        issues.append(CheckIssue("missing_rst", f"重启标记数量为 {restarts}，必须为 {RESTARTS} 个（RST0–7 循环）。"))
    if eoi_pos is None:
        return None
    end = eoi_pos
    while end > scan_start and data[end - 1] == 0xFF:
        end -= 1
    return end


def check_off_image(data: bytes) -> OffImageCheck:
    """DAT 260（OFF_IMG_STRICT）完整校验：逐段诊断 + 严格头部与熵流解码。"""
    issues: list[CheckIssue] = []
    n = len(data)
    if n == 0:
        return OffImageCheck(ok=False, dat_state=1, errors=(CheckIssue("empty", "文件为空。"),))
    if n > MAX_BYTES:
        issues.append(CheckIssue("over_max_bytes", f"{n} B 超过上限 {MAX_BYTES} B。"))
    if n < 2 or data[:2] != b"\xff\xd8":
        issues.append(CheckIssue("missing_soi", "缺少 JPEG 起始标记 SOI。"))
        state = 1 if n > MAX_BYTES else 2
        return OffImageCheck(ok=False, dat_state=state, errors=tuple(issues))

    segs, scan_start, parse_err = _parse_header_segments(data)
    for m, _ in segs:
        if 0xE0 <= m <= 0xEF:
            issues.append(CheckIssue("forbidden_segment", f"包含禁止的段 APP{m - 0xE0}（不能含 APP/COM/EXIF）。"))
        elif m == 0xFE:
            issues.append(CheckIssue("forbidden_segment", "包含禁止的注释段 COM（不能含 APP/COM/EXIF）。"))
    _diagnose_dqt([b for m, b in segs if m == 0xDB], issues)
    _diagnose_sof(segs, issues)
    _diagnose_dht([b for m, b in segs if m == 0xC4], issues)
    _diagnose_dri_sos_order(segs, issues)
    if parse_err and not issues:
        issues.append(CheckIssue("bad_header", parse_err))

    end: int | None = None
    if scan_start is not None:
        end = _diagnose_scan_and_eoi(data, scan_start, issues)

    # 与固件 DAT 260 逐字节对齐的兜底核对（绝不放过任何未诊断到的头偏移偏差）
    if not issues:
        if n < HEADER_BYTES + 2:
            issues.append(CheckIssue("too_short", f"文件仅 {n} B，短于固定头长度。"))
        elif data[:7] != b"\xff\xd8\xff\xdb\x00\x84\x00" or data[71] != 1 or data[136:HEADER_BYTES] != _FIXED_TAIL:
            issues.append(CheckIssue("bad_header", "文件头与相机要求的固定段布局不一致。"))
        elif data[-2:] != b"\xff\xd9":
            issues.append(CheckIssue("missing_eoi", "文件末尾缺少 EOI 标记。"))

    if not issues and end is not None and not _walk_entropy(data, end):
        issues.append(CheckIssue("bad_scan_data", "扫描数据无法按 60 组 × 40 MCU 严格解码。"))

    if not issues:
        return OffImageCheck(ok=True, dat_state=7, errors=())
    if n > MAX_BYTES:
        return OffImageCheck(ok=False, dat_state=1, errors=tuple(issues))
    if len(issues) == 1 and issues[0].code == "bad_scan_data":
        return OffImageCheck(ok=False, dat_state=8, errors=tuple(issues))
    return OffImageCheck(ok=False, dat_state=2, errors=tuple(issues))


def remux_off_image(data: bytes) -> bytes:
    """将标准基线 4:2:2 + DRI 40 JPEG 重封装为相机布局：去掉 APP/COM，合并 DQT 与 DHT 并排为 DC0/DC1/AC0/AC1。"""
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise ExportError("encode_failed", "无法生成关机图。", "JPEG 缺少 SOI")
    segs, scan_start, parse_err = _parse_header_segments(data)
    if parse_err is not None or scan_start is None:
        raise ExportError("encode_failed", "无法生成关机图。", parse_err or "JPEG 缺少 SOS")
    parts: dict[int, list[bytes]] = {}
    for marker, body in segs:
        parts.setdefault(marker, []).append(body)
    for req in (0xDB, 0xC0, 0xC4, 0xDD, 0xDA):
        if req not in parts:
            raise ExportError("encode_failed", "无法生成关机图。", f"编码结果缺少段 FF{req:02X}")

    dht_raw = b"".join(parts[0xC4])
    tables: dict[int, bytes] = {}
    while dht_raw:
        if len(dht_raw) < 17:
            raise ExportError("encode_failed", "无法生成关机图。", "DHT 表截断")
        size = 17 + sum(dht_raw[1:17])
        if len(dht_raw) < size:
            raise ExportError("encode_failed", "无法生成关机图。", "DHT 表长度越界")
        tables[dht_raw[0]] = dht_raw[:size]
        dht_raw = dht_raw[size:]
    if any(tc not in tables for tc in _EXPECTED_DHT_ORDER):
        raise ExportError("encode_failed", "无法生成关机图。", "缺少标准霍夫曼表")

    out = (
        b"\xff\xd8"
        + _segment(0xDB, b"".join(parts[0xDB]))
        + _segment(0xC0, parts[0xC0][0])
        + _segment(0xC4, b"".join(tables[tc] for tc in _EXPECTED_DHT_ORDER))
        + _segment(0xDD, parts[0xDD][0])
        + _segment(0xDA, parts[0xDA][0])
        + data[scan_start:]
    )
    return out


def cover_size(width: int, height: int) -> tuple[int, int]:
    """按整数运算求铺满 640×480 的缩放后尺寸，保证 Python 与 Rust 逐像素一致。"""
    if width <= 0 or height <= 0:
        raise ExportError("bad_image", "图像尺寸无效。")
    if width == WIDTH and height == HEIGHT:
        return WIDTH, HEIGHT
    if width * HEIGHT >= height * WIDTH:
        nw = max(WIDTH, (width * HEIGHT + height // 2) // height)
        return nw, HEIGHT
    nh = max(HEIGHT, (height * WIDTH + width // 2) // width)
    return WIDTH, nh


def cover_crop_rgb(rgb: np.ndarray) -> np.ndarray:
    """将转正后的 RGB 图像按 Lanczos 缩放铺满 640×480 并居中裁切。"""
    if rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.shape[0] <= 0 or rgb.shape[1] <= 0:
        raise ExportError("bad_image", "图像数据格式不正确。")
    height, width = int(rgb.shape[0]), int(rgb.shape[1])
    arr = np.ascontiguousarray(rgb, dtype=np.uint8)
    if width == WIDTH and height == HEIGHT:
        return arr
    nw, nh = cover_size(width, height)
    im = Image.fromarray(arr, "RGB")
    if (nw, nh) != (width, height):
        im = im.resize((nw, nh), Image.Resampling.LANCZOS)
    x0 = (nw - WIDTH) // 2
    y0 = (nh - HEIGHT) // 2
    cropped = im.crop((x0, y0, x0 + WIDTH, y0 + HEIGHT))
    return np.ascontiguousarray(np.asarray(cropped, dtype=np.uint8))


def _to_srgb(im: Image.Image) -> Image.Image:
    """若有非 sRGB 的内嵌 ICC 配置文件则转到 sRGB；无 ICC 或转换失败时保留原 RGB。"""
    icc = im.info.get("icc_profile")
    if not icc:
        return im
    try:
        src_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        dst_profile = ImageCms.ImageCmsProfile(io.BytesIO(srgb_icc()))
        # sRGB 内嵌图不重复做 CMS 转换，避免无谓舍入偏差
        desc = (ImageCms.getProfileDescription(src_profile) or "").lower()
        if "srgb" in desc:
            return im
        converted = ImageCms.profileToProfile(im, src_profile, dst_profile, outputMode="RGB")
        return converted if converted is not None else im
    except Exception:
        # 损坏的 ICC 不阻断生成，回退原像素
        return im


# 先看声明的尺寸再解码：超过 Pillow 的炸弹阈值（约 8,900 万像素）就不解码，免得一张小文件吃掉几 GB 内存。
MAX_SOURCE_PIXELS = Image.MAX_IMAGE_PIXELS or 89_478_485
ERR_TOO_MANY_PIXELS = "图片像素太多，请选择 8,900 万像素以内的图片。"


def prepare_image_bytes(raw: bytes) -> np.ndarray:
    """解码图片字节：按 EXIF 方向转正、透明底叠纯黑、转换到 sRGB RGB uint8。"""
    if not raw:
        raise ExportError("bad_image", "图片文件为空。")
    try:
        with Image.open(io.BytesIO(raw)) as im0:
            if im0.width * im0.height > MAX_SOURCE_PIXELS:
                raise ExportError("image_too_big", ERR_TOO_MANY_PIXELS, f"{im0.width}x{im0.height}")
            im0.load()
            turned = ImageOps.exif_transpose(im0)
            im = turned if turned is not None else im0.copy()
            has_alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
            if has_alpha:
                rgba = im.convert("RGBA")
                bg = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
                im = Image.alpha_composite(bg, rgba).convert("RGB")
                if "icc_profile" in im0.info:
                    im.info["icc_profile"] = im0.info["icc_profile"]
            elif im.mode != "RGB":
                im = im.convert("RGB")
                if "icc_profile" in im0.info:
                    im.info["icc_profile"] = im0.info["icc_profile"]
            im = _to_srgb(im)
            return np.ascontiguousarray(np.asarray(im, dtype=np.uint8))
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ExportError("image_too_big", ERR_TOO_MANY_PIXELS, str(exc)) from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ExportError("unsupported_image", "无法读取这张图片。请选择 JPEG 或 PNG 图片。", str(exc)) from exc


def encode_off_image_rgb(rgb: np.ndarray) -> OffImageResult:
    """将转正后的 RGB 编码为 640×480 关机图，品质从 92 递减直到 <= 32,768 B，并执行校验。"""
    frame = cover_crop_rgb(rgb)
    im = Image.fromarray(frame, "RGB")
    chosen_data = b""
    chosen_q = QUALITIES[-1]
    for q in QUALITIES:
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=q, subsampling="4:2:2", restart_marker_rows=1, optimize=False)
        remuxed = remux_off_image(buf.getvalue())
        chosen_data = remuxed
        chosen_q = q
        if len(remuxed) <= MAX_BYTES:
            break
    chk = check_off_image(chosen_data)
    return OffImageResult(
        data=chosen_data,
        bytes=len(chosen_data),
        quality=chosen_q,
        width=WIDTH,
        height=HEIGHT,
        check=chk,
        sha256=hashlib.sha256(chosen_data).hexdigest(),
    )


def encode_off_image_bytes(raw: bytes) -> OffImageResult:
    """从图片文件字节生成关机图（EXIF 转正 → 铺满居中裁切 640×480 → 编码重封装 → 校验）。"""
    return encode_off_image_rgb(prepare_image_bytes(raw))


def save_off_image(data: bytes, dest_dir: Path, forbidden: Iterable[Path]) -> Path:
    """校验通过后原子写出 ``GXROFF.JPG``，复用私有/禁写守卫拒绝存储卡、卷根、图库与 Git 目录。"""
    chk = check_off_image(data)
    if not chk.ok:
        detail = "；".join(issue.message for issue in chk.errors)
        raise ExportError("off_image_check_failed", "关机图校验未通过，未写入文件。", detail)

    blocked = list(forbidden)
    dest = Path(dest_dir).expanduser()
    if not dest.is_absolute():
        raise ExportError("bad_destination", "无法导出。请填写导出到的完整路径。")
    dest = Path(os.path.abspath(dest))

    anchor: Path = dest
    missing: list[str] = []
    while not os.path.lexists(anchor) and anchor.parent != anchor:
        missing.insert(0, anchor.name)
        anchor = anchor.parent
    try:
        with privacy.PrivateDir(anchor, "off_image", blocked) as base:
            check_destination(base.real.joinpath(*missing), blocked)
            base.verify()
            with contextlib.ExitStack() as stack:
                folder = base
                for part in missing:
                    folder = stack.enter_context(folder.child(part))
                folder.replace_file(OFF_IMAGE_FILENAME, data)
                return folder.real / OFF_IMAGE_FILENAME
    except ExportError:
        raise
    except privacy.PrivatePathError as exc:
        raise ExportError("bad_destination", "无法导出到这个位置。请换一个文件夹。", exc.detail) from exc
    except OSError as exc:
        raise ExportError("dest_unwritable", "无法导出到这个位置。请换一个文件夹。", exc.strerror or "") from exc
