"""带增益图的 HDR JPEG 的封装：ISO 21496-1 + Adobe/Ultra HDR（XMP `hdrgm`）+ MPF，主图压缩数据一个字节都不动。

来源：research/hdr_gainmap/gxr_hdr.py 的 JPEG 段结构、元数据和 `assemble`（REPORT.md §2、§3.2）。规格出处：

- Ultra HDR v1.1：https://developer.android.com/media/platform/hdr-image-format
- ISO 21496-1 的字节布局按 libultrahdr `gainmapmetadata.cpp`，MPF 按 `multipictureformat.cpp`、`jpegr.cpp appendGainMap`

主图 = SOI、原有 APP 段（去掉旧的 MPF/XMP/ISO）、新 XMP、ISO 版本段、新 MPF、原 DQT/SOF/DHT…、原 SOS…EOI。
第二幅 = SOI、增益图 XMP、增益图 ISO、增益图 JPEG。原文件 MPF 带的第二幅图（GXR 的 640×424 预览）不保留：
libultrahdr 把第二幅 JPEG 当增益图，严格模式要求正好两幅。
"""

from __future__ import annotations

import io
import struct
from dataclasses import dataclass
from fractions import Fraction

import numpy as np
from PIL import Image

XMP_NS = b"http://ns.adobe.com/xap/1.0/\x00"
ISO_NS = b"urn:iso:std:iso:ts:21496:-1\x00"
MPF_SIG = b"MPF\x00"
OFFSET = 1.0 / 64.0  # Ultra HDR 推荐的 OffsetSDR / OffsetHDR
MPF_SEG_LEN = 4 + 4 + 8 + 2 + 36 + 4 + 32  # FF E2 LL LL + "MPF\0" + TIFF 头 + IFD + 两条记录
_XMPTK = "GXR Studio"


class ContainerError(ValueError):
    """JPEG 段结构读不懂，或者元数据放不进去。"""


@dataclass(frozen=True)
class Segment:
    marker: int
    start: int  # FF 的位置
    end: int  # 段结束（下一个 FF 的位置）

    def payload(self, data: bytes) -> bytes:
        return data[self.start + 4 : self.end]


@dataclass(frozen=True)
class GainMeta:
    """增益图元数据，都是 log2 值（`gamma` 除外）。"""

    gain_min: float = 0.0
    gain_max: float = 1.0
    gamma: float = 1.0
    offset_sdr: float = OFFSET
    offset_hdr: float = OFFSET
    cap_min: float = 0.0  # HDRCapacityMin
    cap_max: float = 1.0  # HDRCapacityMax


def _u16(data: bytes, pos: int) -> int:
    if pos + 2 > len(data):
        raise ContainerError("JPEG 在段中间结束")
    return int(struct.unpack_from(">H", data, pos)[0])


def jpeg_segments(data: bytes, start: int = 0) -> tuple[list[Segment], int, int]:
    """返回 (SOS 之前的段, 第一个 SOS 的位置, EOI 之后的位置)。

    熵编码数据里的 FF 后面只会是 00 或 RSTn；渐进式的后续 SOS、扫描之间的 DHT 等按段长度跳过。"""
    if data[start : start + 2] != b"\xff\xd8":
        raise ContainerError("不是 JPEG（缺 SOI）")
    i = start + 2
    segs: list[Segment] = []
    while True:
        while i + 1 < len(data) and data[i] == 0xFF and data[i + 1] == 0xFF:
            i += 1
        if i + 1 >= len(data) or data[i] != 0xFF:
            raise ContainerError(f"段结构错误 @ {i}")
        marker = data[i + 1]
        if marker == 0xDA:
            sos = i
            break
        length = _u16(data, i + 2)
        if length < 2 or i + 2 + length > len(data):
            raise ContainerError(f"段长度错误 @ {i}")
        segs.append(Segment(marker, i, i + 2 + length))
        i += 2 + length
    j = sos + 2 + _u16(data, sos + 2)
    while True:
        j = data.find(b"\xff", j)
        if j < 0 or j + 1 >= len(data):
            raise ContainerError("JPEG 缺 EOI")
        nxt = data[j + 1]
        if nxt == 0xD9:
            return segs, sos, j + 2
        if nxt == 0x00 or 0xD0 <= nxt <= 0xD7:
            j += 2
        elif nxt == 0xFF:
            j += 1
        else:  # 扫描之间的段（渐进式的 SOS、DHT…）：按长度跳过
            j += 2 + _u16(data, j + 2)


def app_segment(marker: int, payload: bytes) -> bytes:
    if len(payload) + 2 > 0xFFFF:
        raise ContainerError("APP 段超过 64 KB")
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


def _fmt(v: float) -> str:
    return f"{v:.7g}"


def xmp_primary(gain_len: int) -> bytes:
    s = (
        f'<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="{_XMPTK}">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about=""\n'
        '    xmlns:Container="http://ns.google.com/photos/1.0/container/"\n'
        '    xmlns:Item="http://ns.google.com/photos/1.0/container/item/"\n'
        '    xmlns:hdrgm="http://ns.adobe.com/hdr-gain-map/1.0/"\n'
        '   hdrgm:Version="1.0">\n'
        "   <Container:Directory>\n"
        "    <rdf:Seq>\n"
        '     <rdf:li rdf:parseType="Resource">\n'
        '      <Container:Item Item:Semantic="Primary" Item:Mime="image/jpeg"/>\n'
        "     </rdf:li>\n"
        '     <rdf:li rdf:parseType="Resource">\n'
        f'      <Container:Item Item:Semantic="GainMap" Item:Mime="image/jpeg" Item:Length="{gain_len}"/>\n'
        "     </rdf:li>\n"
        "    </rdf:Seq>\n"
        "   </Container:Directory>\n"
        "  </rdf:Description>\n"
        " </rdf:RDF>\n"
        "</x:xmpmeta>"
    )
    return s.encode()


def xmp_gainmap(m: GainMeta) -> bytes:
    s = (
        f'<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="{_XMPTK}">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about=""\n'
        '    xmlns:hdrgm="http://ns.adobe.com/hdr-gain-map/1.0/"\n'
        '   hdrgm:Version="1.0"\n'
        f'   hdrgm:GainMapMin="{_fmt(m.gain_min)}"\n'
        f'   hdrgm:GainMapMax="{_fmt(m.gain_max)}"\n'
        f'   hdrgm:Gamma="{_fmt(m.gamma)}"\n'
        f'   hdrgm:OffsetSDR="{_fmt(m.offset_sdr)}"\n'
        f'   hdrgm:OffsetHDR="{_fmt(m.offset_hdr)}"\n'
        f'   hdrgm:HDRCapacityMin="{_fmt(m.cap_min)}"\n'
        f'   hdrgm:HDRCapacityMax="{_fmt(m.cap_max)}"\n'
        '   hdrgm:BaseRenditionIsHDR="False"/>\n'
        " </rdf:RDF>\n"
        "</x:xmpmeta>"
    )
    return s.encode()


def _frac(v: float, signed: bool) -> tuple[int, int]:
    f = Fraction(v).limit_denominator(1 << 16)
    if not signed and f < 0:
        raise ContainerError("无符号字段不能为负")
    return f.numerator, f.denominator


def iso_version_only() -> bytes:
    """主图里的 ISO 21496-1 段：只有 minimum_version / writer_version（libultrahdr 同样写法）。"""
    return ISO_NS + struct.pack(">HH", 0, 0)


def iso_full(m: GainMeta) -> bytes:
    """增益图里的 ISO 21496-1 段：单通道、用底图色彩空间（flags 0x40），大端分数。"""
    out = bytearray(ISO_NS)
    out += struct.pack(">HHB", 0, 0, 0x40)
    for v in (m.cap_min, m.cap_max):  # baseHdrHeadroom / alternateHdrHeadroom，log2，无符号
        out += struct.pack(">II", *_frac(v, False))
    gmin, gmax, gam = _frac(m.gain_min, True), _frac(m.gain_max, True), _frac(m.gamma, False)
    bo, ao = _frac(m.offset_sdr, True), _frac(m.offset_hdr, True)
    out += struct.pack(">iIiIIIiIiI", gmin[0], gmin[1], gmax[0], gmax[1], gam[0], gam[1], bo[0], bo[1], ao[0], ao[1])
    return bytes(out)


def mpf_segment(primary_size: int, gain_size: int, gain_offset: int) -> bytes:
    """两幅图的 MPF（大端），与 libultrahdr generateMpf 同布局：主图属性 0x030000（基线主图），增益图 0。
    第二幅的偏移相对 MPF 段里 TIFF 头的位置。"""
    t = bytearray(b"MM\x00\x2a") + struct.pack(">I", 8)
    t += struct.pack(">H", 3)
    t += struct.pack(">HHI", 0xB000, 7, 4) + b"0100"
    t += struct.pack(">HHII", 0xB001, 4, 1, 2)
    entry_off = 8 + 2 + 3 * 12 + 4
    t += struct.pack(">HHII", 0xB002, 7, 32, entry_off)
    t += struct.pack(">I", 0)  # 下一个 IFD
    t += struct.pack(">IIIHH", 0x030000, primary_size, 0, 0, 0)
    t += struct.pack(">IIIHH", 0x000000, gain_size, gain_offset, 0, 0)
    return app_segment(0xE2, MPF_SIG + bytes(t))


def encode_gainmap(delta: np.ndarray, meta: GainMeta, quality: int) -> bytes:
    """log2 增益 → 规格的 8 位编码 recovery = ((log2g − min)/(max − min))^gamma → 灰度 JPEG。"""
    span = meta.gain_max - meta.gain_min
    r = np.clip((delta - meta.gain_min) / span, 0, 1) ** meta.gamma
    img = Image.fromarray(np.floor(r * 255.0 + 0.5).astype(np.uint8), "L")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _is_replaced(seg: Segment, data: bytes) -> bool:
    """旧的 MPF、XMP、ISO 段：换成新的。"""
    p = seg.payload(data)
    return (seg.marker == 0xE2 and (p.startswith(MPF_SIG) or p.startswith(ISO_NS))) or (seg.marker == 0xE1 and p.startswith(XMP_NS))


def assemble(jpg: bytes, gain_jpeg: bytes, meta: GainMeta) -> bytes:
    """原 JPG 字节 + 增益图 JPEG → HDR JPG 字节。除被替换的元数据段以外，主图的每个字节都从原文件原样复制。"""
    segs, sos, eoi = jpeg_segments(jpg)
    keep_app: list[bytes] = []
    keep_rest: list[bytes] = []
    for s in segs:
        if _is_replaced(s, jpg):
            continue
        (keep_app if 0xE0 <= s.marker <= 0xEF else keep_rest).append(jpg[s.start : s.end])
    gsegs, gsos, geoi = jpeg_segments(gain_jpeg)
    gplain = b"".join(gain_jpeg[s.start : s.end] for s in gsegs if s.marker not in (0xE1, 0xE2)) + gain_jpeg[gsos:geoi]
    secondary = b"\xff\xd8" + app_segment(0xE1, XMP_NS + xmp_gainmap(meta)) + app_segment(0xE2, iso_full(meta)) + gplain
    head = b"\xff\xd8" + b"".join(keep_app) + app_segment(0xE1, XMP_NS + xmp_primary(len(secondary))) + app_segment(0xE2, iso_version_only())
    rest = b"".join(keep_rest)
    primary_size = len(head) + MPF_SEG_LEN + len(rest) + (eoi - sos)
    tiff_pos = len(head) + 8
    mpf = mpf_segment(primary_size, len(secondary), primary_size - tiff_pos)
    out = head + mpf + rest + jpg[sos:eoi] + secondary
    if len(mpf) != MPF_SEG_LEN or len(out) != primary_size + len(secondary):
        raise ContainerError("MPF 长度不一致")
    return out
