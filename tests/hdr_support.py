"""HDR 测试的公共部分：合成的 RAW+JPG、独立写的 HDR 文件检查（不复用 `gxr_studio.hdr.container`），系统 ImageIO 对照。

检查项来自 research/hdr_gainmap/selfcheck.py：
 1 主图解码像素与原 JPG 逐像素相同；原 JPG 的段和 SOS…EOI 字节原样出现在主图里（没有重新压缩）；
 2 MPF 两条记录、偏移和长度对得上；主图 XMP 的 Container:Directory；ISO 版本段 + 增益图完整 ISO 段与 XMP 数值一致；
   第二幅能单独解码，尺寸 = 主图 1/4（向上取整），灰度；
 3 按规格公式重建 HDR（权重 1）：暗部与中间调不提亮；三通道同乘一个增益（色相不变）；
 4 （macOS）系统 ImageIO 认出 ISO 增益图，系统给的提亮与按规格算的一致。
"""

from __future__ import annotations

import io
import json
import math
import os
import shutil
import struct
import subprocess  # nosec B404  只在 macOS 上编译和运行仓库里的 swift 检查脚本
import xml.etree.ElementTree as ET  # nosec B405  解析测试自己生成的 XMP
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image
from scipy import ndimage

from raw_support import bayer_scene, build_dng

NS = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "hdrgm": "http://ns.adobe.com/hdr-gain-map/1.0/",
    "Container": "http://ns.google.com/photos/1.0/container/",
    "Item": "http://ns.google.com/photos/1.0/container/item/",
}
XMP_SIG = b"http://ns.adobe.com/xap/1.0/\x00"
ISO_SIG = b"urn:iso:std:iso:ts:21496:-1\x00"
SWIFT = Path(__file__).parent / "support" / "check_imageio.swift"
ON_MACOS = os.uname().sysname == "Darwin"  # 真实宿主，与 privacy 的判断一致
HAS_IMAGEIO = ON_MACOS and shutil.which("swiftc") is not None
needs_imageio = pytest.mark.skipif(not HAS_IMAGEIO, reason="只在 macOS 上运行（系统 ImageIO）")
EXPOSURE = 4.0  # 合成的"相机 JPG"比 RAW 亮 2 档：RAW 在 JPG 发白的地方还有余量
NEUTRAL = (0.5, 1.0, 0.6)


# ---------------------------------------------------------------- 合成的 RAW+JPG


def scene(h: int = 384, w: int = 576, seed: int = 0) -> np.ndarray:
    """白平衡后的场景线性 RGB（0..1）：从左到右连续变亮（2^-7 → 2^-0.3，带纹理），右边三成在 JPG 里发白、RAW 不过曝；
    左下角一小块 RAW 也过曝。"""
    rng = np.random.default_rng(seed)
    tex = ndimage.gaussian_filter(rng.random((h, w)), 6.0)
    tex = (tex - tex.min()) / (tex.max() - tex.min()) - 0.5
    u = np.linspace(0, 1, w)[None, :] + 0.3 * tex
    y = np.exp2(-7 + 6.7 * np.clip(u, 0, 1))
    y[h - h // 6 :, : w // 6] = 1.2  # 过曝（夹到白电平）
    rgb = np.clip(y[..., None] * np.array([1.0, 0.97, 0.92]), 0, 1)
    return rgb.astype(np.float64)


def expected_boost(raw: np.ndarray, max_stops: float = 2.5) -> np.ndarray:
    """规格（REPORT §3.1 第 4 步）：拐点以上按场景线性继续变亮。合成 JPG = 4 × RAW，所以 HDR = 4 × RAW，SDR 发白处 ≈ 1。"""
    return max_stops * np.tanh(np.clip(np.log2(EXPOSURE * raw), 0, None) / max_stops)


def srgb_bytes(lin: np.ndarray) -> np.ndarray:
    v = np.clip(lin, 0, 1)
    return np.rint(np.where(v <= 0.0031308, v * 12.92, 1.055 * v ** (1 / 2.4) - 0.055) * 255).astype(np.uint8)


def camera_jpeg(rgb: np.ndarray, orientation: int = 1, when: str = "2026:10:31 10:00:00") -> bytes:
    exif = Image.Exif()
    exif[274] = orientation
    exif[306] = when
    exif[271] = "RICOH"
    exif[272] = "GXR"
    buf = io.BytesIO()
    Image.fromarray(srgb_bytes(rgb * EXPOSURE), "RGB").save(buf, "JPEG", quality=95, exif=exif)
    return buf.getvalue()


def camera_dng(rgb: np.ndarray, orientation: int = 1) -> bytes:
    cam = rgb * np.array(NEUTRAL)  # 白平衡之前的传感器值：除以 AsShotNeutral 正好回到场景
    return build_dng(
        bayer_scene(*rgb.shape[:2], cam),
        bps=16,
        white=4095,
        neutral=NEUTRAL,
        orientation=orientation,
        when="2026:10:31 10:00:00",
        root_extra=[(272, "ASCII", "GXR")],
    )


def pair(seed: int = 0, orientation: int = 1, h: int = 384, w: int = 576) -> tuple[bytes, bytes]:
    """(相机 JPG, DNG)，存储方向相同（横的），方向标记都是 `orientation`。"""
    rgb = scene(h, w, seed)
    return camera_jpeg(rgb, orientation), camera_dng(rgb, orientation)


def write_pair(folder: Path, stem: str, seed: int = 0, when: datetime | None = None) -> tuple[Path, Path]:
    jpg, dng = pair(seed)
    folder.mkdir(parents=True, exist_ok=True)
    j, d = folder / f"{stem}.JPG", folder / f"{stem}.DNG"
    j.write_bytes(jpg)
    d.write_bytes(dng)
    if when is not None:
        ts = when.timestamp()
        for p in (j, d):
            os.utime(p, (ts, ts))
    return j, d


# ---------------------------------------------------------------- 独立的文件检查


def walk(d: bytes, start: int) -> list[tuple[Any, int, int]]:
    """从 start 的 SOI 起列出段 [(marker, 段起点, 段终点)]，最后一项是 ('SOS', sos, eoi_end)。基线 JPEG。"""
    assert d[start : start + 2] == b"\xff\xd8", "缺 SOI"
    i, out = start + 2, []
    while True:
        assert d[i] == 0xFF, f"段错位 @{i}"
        m = d[i + 1]
        n = struct.unpack(">H", d[i + 2 : i + 4])[0]
        if m == 0xDA:
            j = d.index(b"\xff", i + 2 + n)
            while d[j + 1] != 0xD9:
                j = d.index(b"\xff", j + 1)
            out.append(("SOS", i, j + 2))
            return out
        out.append((m, i, i + 2 + n))
        i += 2 + n


def _num(v: str | None) -> float:
    return float(v) if v is not None else float("nan")


def _mpf_entries(d: bytes, segs: list[tuple[Any, int, int]]) -> tuple[int, list[tuple[int, ...]], bytes | None, int | None]:
    mpf = [(a, b) for m, a, b in segs if m == 0xE2 and d[a + 4 : a + 8] == b"MPF\x00"]
    assert len(mpf) == 1, "主图 MPF 段数 ≠ 1"
    t = mpf[0][0] + 8
    e = ">" if d[t : t + 2] == b"MM" else "<"
    ifd = t + struct.unpack(e + "I", d[t + 4 : t + 8])[0]
    entries: list[tuple[int, ...]] = []
    version, count = None, None
    for k in range(struct.unpack(e + "H", d[ifd : ifd + 2])[0]):
        tag, _typ, cnt = struct.unpack(e + "HHI", d[ifd + 2 + 12 * k : ifd + 10 + 12 * k])
        val = d[ifd + 10 + 12 * k : ifd + 14 + 12 * k]
        if tag == 0xB000:
            version = val
        elif tag == 0xB001:
            count = struct.unpack(e + "I", val)[0]
        elif tag == 0xB002:
            off = t + struct.unpack(e + "I", val)[0]
            entries += [struct.unpack(e + "IIIHH", d[off + 16 * n : off + 16 * n + 16]) for n in range(cnt // 16)]
    return t, entries, version, count


def check_file(orig: bytes, d: bytes, *, preserved: bool = True) -> dict[str, Any]:
    """`preserved`：主图应当就是 `orig`（导入时的相机 JPG）。导出时 `orig` 是不带 HDR 的同一份成片。"""
    r: dict[str, Any] = {"fail": []}

    def need(cond: bool, what: str) -> None:
        if not cond:
            r["fail"].append(what)

    segs = walk(d, 0)
    prim_end = segs[-1][2]
    if preserved:
        body = d[:prim_end]
        osegs = walk(orig, 0)
        for m, a, b in osegs:
            if m == 0xE2 and orig[a + 4 : a + 8] == b"MPF\x00":
                continue
            need(orig[a:b] in body, f"原段 {m} 未原样保留")
        o_sos = osegs[-1]
        need(orig[o_sos[1] : o_sos[2]] == d[segs[-1][1] : segs[-1][2]], "SOS…EOI 熵编码数据不同")
    a = np.asarray(Image.open(io.BytesIO(orig)).convert("RGB"))
    b = np.asarray(Image.open(io.BytesIO(d)).convert("RGB"))
    r["pixels_identical"] = bool(a.shape == b.shape and np.array_equal(a, b))
    need(r["pixels_identical"], "主图像素不同")

    t, entries, version, count = _mpf_entries(d, segs)
    need(version == b"0100" and count == 2 and len(entries) == 2, "MPF 版本/数量错误")
    need(entries[0][1] == prim_end and entries[0][2] == 0, "MPF 主图大小/偏移错误")
    g0 = t + entries[1][2]
    need(g0 == prim_end and d[g0 : g0 + 2] == b"\xff\xd8", "MPF 增益图偏移没有指向主图之后的 FFD8")
    need(g0 + entries[1][1] == len(d), "增益图大小与文件末尾不符")
    gm = d[g0:]

    xmp = [d[s + 4 + len(XMP_SIG) : e] for m, s, e in segs if m == 0xE1 and d[s + 4 : s + 4 + len(XMP_SIG)] == XMP_SIG]
    need(len(xmp) == 1, "主图 XMP 段数 ≠ 1")
    desc = ET.fromstring(xmp[0]).find("rdf:RDF/rdf:Description", NS)  # nosec B314
    assert desc is not None
    need(desc.get(f"{{{NS['hdrgm']}}}Version") == "1.0", "主图 hdrgm:Version 缺失")
    items = desc.findall("Container:Directory/rdf:Seq/rdf:li/Container:Item", NS)
    sem = [(i.get(f"{{{NS['Item']}}}Semantic"), i.get(f"{{{NS['Item']}}}Length")) for i in items]
    need(len(sem) == 2 and sem[0][0] == "Primary" and sem[1][0] == "GainMap", "Container:Directory 结构错误")
    need(sem[1][1] is not None and int(sem[1][1] or 0) == len(gm), "GainMap Item:Length ≠ 实际长度")

    iso_p = [d[s + 4 : e] for m, s, e in segs if m == 0xE2 and d[s + 4 : s + 4 + len(ISO_SIG)] == ISO_SIG]
    need(len(iso_p) == 1 and iso_p[0] == ISO_SIG + b"\x00\x00\x00\x00", "主图 ISO 版本段错误")
    gsegs = walk(gm, 0)
    need(gsegs[-1][2] == len(gm), "增益图 EOI 不在文件末尾")
    gx = [gm[s + 4 + len(XMP_SIG) : e] for m, s, e in gsegs if m == 0xE1 and gm[s + 4 : s + 4 + len(XMP_SIG)] == XMP_SIG]
    gi = [gm[s + 4 + len(ISO_SIG) : e] for m, s, e in gsegs if m == 0xE2 and gm[s + 4 : s + 4 + len(ISO_SIG)] == ISO_SIG]
    need(len(gx) == 1 and len(gi) == 1, "增益图缺 XMP 或 ISO 段")
    gd = ET.fromstring(gx[0]).find("rdf:RDF/rdf:Description", NS)  # nosec B314
    assert gd is not None
    keys = ("GainMapMin", "GainMapMax", "Gamma", "OffsetSDR", "OffsetHDR", "HDRCapacityMin", "HDRCapacityMax")
    xm = {k: _num(gd.get(f"{{{NS['hdrgm']}}}{k}")) for k in keys}
    need(gd.get(f"{{{NS['hdrgm']}}}BaseRenditionIsHDR") == "False", "增益图 XMP BaseRenditionIsHDR 错误")
    ib = gi[0]
    minv, _wv, flags = struct.unpack(">HHB", ib[:5])
    hb = struct.unpack(">IIII", ib[5:21])
    v = struct.unpack(">iIiIIIiIiI", ib[21:61])
    iso = {"HDRCapacityMin": hb[0] / hb[1], "HDRCapacityMax": hb[2] / hb[3], "GainMapMin": v[0] / v[1], "GainMapMax": v[2] / v[3]}
    iso.update({"Gamma": v[4] / v[5], "OffsetSDR": v[6] / v[7], "OffsetHDR": v[8] / v[9]})
    need(minv == 0 and not flags & 0x80 and bool(flags & 0x40) and len(ib) == 61, "ISO 段头错误")
    for k in keys:
        need(abs(xm[k] - iso[k]) < 1e-4, f"XMP {k} 与 ISO 不一致")
    r["meta"] = xm

    gimg = Image.open(io.BytesIO(gm))
    gimg.load()
    hh, ww = b.shape[:2]
    need(gimg.size == (math.ceil(ww / 4), math.ceil(hh / 4)) and gimg.mode == "L", "增益图尺寸/通道错误")

    # 3 按规格重建（libultrahdr applyGainMap 同式，权重 1）
    rec = np.asarray(gimg, np.float32) / 255.0
    logg = xm["GainMapMin"] + (xm["GainMapMax"] - xm["GainMapMin"]) * rec ** (1 / xm["Gamma"])
    r["log_gain"] = logg
    up = np.repeat(np.repeat(np.exp2(logg), 4, 0), 4, 1)[:hh, :ww]
    sdr = a.astype(np.float32) / 255.0
    sdr = np.where(sdr <= 0.04045, sdr / 12.92, ((sdr + 0.055) / 1.055) ** 2.4)
    hdr = (sdr + xm["OffsetSDR"]) * up[..., None] - xm["OffsetHDR"]
    y = sdr @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    st = np.log2(up[y < 0.18])
    r["midtone_le_0p05_frac"] = float((st <= 0.05).mean()) if st.size else 1.0
    need(r["midtone_le_0p05_frac"] >= 0.99, "中间调/暗部有 >1% 像素被提亮超过 0.05 档")
    s_o, h_o = sdr + xm["OffsetSDR"], hdr + xm["OffsetHDR"]
    ratio_err = float(np.abs(h_o / h_o.sum(-1, keepdims=True) - s_o / s_o.sum(-1, keepdims=True)).max())
    need(ratio_err < 1e-5, "三通道比例变了")
    r["sdr_lin"], r["hdr_lin"], r["sdr_y"] = sdr, hdr, y
    r["ok"] = not r["fail"]
    return r


# ---------------------------------------------------------------- 系统 ImageIO（macOS）


def imageio_tool(tmp: Path) -> Path:
    exe = tmp / "check_imageio"
    if not exe.exists():
        subprocess.run(["swiftc", "-O", "-o", str(exe), str(SWIFT)], check=True, capture_output=True, timeout=300)  # nosec B603 B607
    return exe


def imageio_check(exe: Path, orig: Path, hdr: Path, r: dict[str, Any]) -> dict[str, Any]:
    """系统 ImageIO：认出 ISO 增益图；SDR 与原图差 ≤1；系统的提亮（4×4 块）与按规格算的差（p99，档）。"""
    tmp = hdr.parent
    run = subprocess.run([str(exe), str(hdr)], check=True, capture_output=True, text=True, timeout=120)  # nosec B603
    info = json.loads(run.stdout.splitlines()[0])
    f32 = tmp / f"{hdr.stem}.f32"
    run = subprocess.run([str(exe), "--compare", str(orig), str(hdr), str(f32)], check=True, capture_output=True, text=True, timeout=120)  # nosec B603
    cmp_ = json.loads(run.stdout)
    hh, ww = r["sdr_y"].shape
    gh, gw = math.ceil(hh / 4), math.ceil(ww / 4)
    arr = np.fromfile(f32, np.float32)
    ls, lh = arr[: gh * gw].reshape(gh, gw), arr[gh * gw :].reshape(gh, gw)
    hy = r["hdr_lin"] @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    pad = ((0, gh * 4 - hh), (0, gw * 4 - ww))
    pred = np.pad(hy, pad, mode="edge").reshape(gh, 4, gw, 4).mean(axis=(1, 3))
    spad = np.pad(r["sdr_y"], pad, mode="edge").reshape(gh, 4, gw, 4).mean(axis=(1, 3))
    ok = (ls > 0.01) & (spad > 0.01)
    err = np.abs(np.log2(lh[ok] / ls[ok]) - np.log2(pred[ok] / spad[ok]))
    return {
        "iso_gainmap": bool(info.get("isoGainMap")),
        "headroom": info.get("hdrHeadroom"),
        "sdr_max_abs_diff": cmp_.get("sdrMaxAbsDiff"),
        "boost_err_p99": float(np.percentile(err, 99)),
        "system_max_boost": float(np.log2(lh[ok] / ls[ok]).max()),
    }
