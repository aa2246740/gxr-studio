"""halfframe-p1a 合成与单元测试（不依赖实拍照片，可直接在 Git CI 跑）。

覆盖验收 6：
- 合成器纯几何：EXIF 1/3/6/8（拒绝 2/4/5/7）、接缝位置、条带顺序、输出尺寸、代理宽度取整与相机 JPG 单格切分；
- 检测规则表：合成特征向量与合成 JPEG/MPO（含平场 `flat=True`、真场景黑条未过指纹、方向不匹配、非 g250 接缝、非相邻等）；
- 配对存储 `PairStore`：版本守卫、只读 `.studio`（API 仍返回 200 且不泄露路径）、`Library.rebuild` 与缺失伙伴降级。
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import struct
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import PORT
from gxr_studio import app as appmod
from gxr_studio import halfframe as hf
from gxr_studio import halfframe_compose as hfc
from gxr_studio import halfframe_export as hfe
from gxr_studio import halfframe_library as hfl
from gxr_studio import halfframe_pairs as hfp
from gxr_studio.app import create_app
from gxr_studio.cards.discovery import find_cards
from gxr_studio.cards.importer import ImportOptions, run_import
from gxr_studio.library import location as lib_loc
from gxr_studio.library.store import Library
from gxr_studio.raw import Renderer
from gxr_studio.render.params import EditState, RecipeSel
from test_lut import cube_bytes

# --------------------------------------------------------------------------- 合成 JPEG / MPO 辅助


def _encode_jpeg_bytes(arr: np.ndarray, *, exif_orientation: int | None = None, quality: int = 96) -> bytes:
    im = Image.fromarray(arr, mode="RGB")
    buf = io.BytesIO()
    kwargs: dict[str, Any] = {"format": "JPEG", "quality": quality, "subsampling": 0}
    if exif_orientation is not None:
        exif = Image.Exif()
        exif[274] = exif_orientation
        kwargs["exif"] = exif.tobytes()
    im.save(buf, **kwargs)
    return buf.getvalue()


def _make_mpo_bytes(primary_jpg: bytes, preview_jpg: bytes) -> bytes:
    """把两张标准 JPEG 拼成带 APP2 MPF 头（2 帧）的最小合法 MPO，Pillow `n_frames==2` 可直接 seek(1)。"""
    assert primary_jpg.startswith(b"\xff\xd8")
    assert preview_jpg.startswith(b"\xff\xd8")
    # TIFF header (big-endian "MM") + IFD0 (3 tags: MPFVersion, NumberOfImages, MPEntry) + 2*16 byte entries
    # APP2 payload = b"MPF\x00" + tiff_data
    # tiff offsets are relative to the start of "MM" (4 bytes after APP2 length+id)
    # Construct payload with placeholder offset for image 2, then patch exact offset.
    ifd_offset = 8
    num_entries = 3
    entries_offset = ifd_offset + 2 + num_entries * 12 + 4  # 8 + 2 + 36 + 4 = 50
    mp_entry_bytes = 2 * 16  # 32
    tiff_len = entries_offset + mp_entry_bytes  # 82
    app2_payload_len = 4 + tiff_len  # 86 ("MPF\x00" + tiff)
    app2_segment_len = 2 + 2 + app2_payload_len  # FF E2 + len(2) + payload = 90
    # APP2 is inserted right after primary's SOI (offset 2).
    # TIFF "MM" starts at offset 2 (SOI) + 2 (FF E2) + 2 (len) + 4 ("MPF\x00") = 10 in the MPO file.
    tiff_file_start = 10
    img1_size = len(primary_jpg) + app2_segment_len
    img2_offset_rel = img1_size - tiff_file_start

    tiff = bytearray()
    tiff += b"MM" + struct.pack(">HI", 42, ifd_offset)
    tiff += struct.pack(">H", num_entries)
    # Tag 0xB000 MPFVersion (UNDEFINED, count 4, "0100")
    tiff += struct.pack(">HHII", 0xB000, 7, 4, int.from_bytes(b"0100", "big"))
    # Tag 0xB001 NumberOfImages (LONG, count 1, 2)
    tiff += struct.pack(">HHII", 0xB001, 4, 1, 2)
    # Tag 0xB002 MPEntry (UNDEFINED, count 32, offset=entries_offset)
    tiff += struct.pack(">HHII", 0xB002, 7, mp_entry_bytes, entries_offset)
    tiff += struct.pack(">I", 0)  # next IFD
    # Entry 0 (primary): attr=0x030000, size=img1_size, offset=0, dep=0
    tiff += struct.pack(">IIIHH", 0x030000, img1_size, 0, 0, 0)
    # Entry 1 (MP preview): attr=0x010002, size=len(preview_jpg), offset=img2_offset_rel, dep=0
    tiff += struct.pack(">IIIHH", 0x010002, len(preview_jpg), img2_offset_rel, 0, 0)
    assert len(tiff) == tiff_len

    app2 = b"\xff\xe2" + struct.pack(">H", 2 + app2_payload_len) + b"MPF\x00" + bytes(tiff)
    return primary_jpg[:2] + app2 + primary_jpg[2:] + preview_jpg


def _textured_half(height: int, width: int, *, seed: int) -> np.ndarray:
    """生成带平滑高频纹理的合成半格图像（RGB，1/8 下采样仍保持丰富梯度且 JPEG 压缩低失真）。"""
    yy = np.linspace(0.0, 1.0, height, dtype=np.float64)[:, None]
    xx = np.linspace(0.0, 1.0, width, dtype=np.float64)[None, :]
    p = float(seed) * 0.7
    r = 120.0 + 75.0 * np.sin(2.0 * np.pi * (xx * 2.5 + p)) * np.cos(2.0 * np.pi * (yy * 1.5))
    g = 115.0 + 70.0 * np.cos(2.0 * np.pi * (xx * 1.8 - yy * 2.2 + p))
    b = 125.0 + 65.0 * np.sin(2.0 * np.pi * (yy * 3.1 + xx * 0.7 + p * 0.5))
    return np.clip(np.stack([r, g, b], axis=-1), 30, 235).astype(np.uint8)


def _make_g250_first_mpo(
    path: Path,
    strip: np.ndarray,
    *,
    exif_orientation: int = 1,
    preview_black_bars: bool = True,
) -> Path:
    """写一张 4288×2848 合成第 1 张（主图列 1100..3187 放 strip，MPF 640×424 预览两侧黑边可选）。"""
    canvas = np.full((hfc.FULL_HEIGHT, hfc.FULL_WIDTH, 3), 128, dtype=np.uint8)
    canvas[:, hfc.FULL_STRIP_X0_JPG : hfc.FULL_STRIP_X0_JPG + hfc.FULL_HALF_W] = strip
    prev = np.full((hfp.MPF_HEIGHT, hfp.MPF_WIDTH, 3), 140, dtype=np.uint8)
    if preview_black_bars:
        prev[:, : hfp.MPF_LEFT_MAX + 1] = 0
        prev[:, hfp.MPF_RIGHT_MIN :] = 0
    mpo = _make_mpo_bytes(
        _encode_jpeg_bytes(canvas, exif_orientation=exif_orientation),
        _encode_jpeg_bytes(prev, quality=92),
    )
    path.write_bytes(mpo)
    return path


def _make_g250_diptych_jpg(
    path: Path,
    left_strip: np.ndarray,
    right_strip: np.ndarray,
    *,
    exif_orientation: int = 1,
    seam_range: tuple[int, int] = (hfc.FULL_SEAM_X0, hfc.FULL_SEAM_X1),
) -> Path:
    canvas = np.full((hfc.FULL_HEIGHT, hfc.FULL_WIDTH, 3), 140, dtype=np.uint8)
    canvas[:, : hfc.FULL_HALF_W] = left_strip
    canvas[:, hfc.FULL_RIGHT_X0 :] = right_strip
    x0, x1 = seam_range
    canvas[:, x0 : x1 + 1] = 0
    path.write_bytes(_encode_jpeg_bytes(canvas, exif_orientation=exif_orientation))
    return path


# --------------------------------------------------------------------------- 1. 合成器纯几何（EXIF 1/3/6/8、拒绝 2/4/5/7、代理取整）


@pytest.mark.parametrize("orientation", (1, 3, 6, 8))
@pytest.mark.parametrize("first_half", ("left", "right"))
def test_composer_geometry_orientations_seam_order_and_sizes(orientation: int, first_half: str) -> None:
    g = hfc.full_geometry()
    assert (g.width, g.half_w, g.strip_x0, g.seam_x0, g.seam_w, g.right_x0) == (4288, 2088, 1099, 2088, 112, 2200)

    # 用缩放网格加速测试，保持精确比例 2088:112:2088 与 2848 高度结构
    s1 = np.full((2848, 2088, 3), (200, 40, 40), dtype=np.uint8)
    s2 = np.full((2848, 2088, 3), (40, 40, 200), dtype=np.uint8)
    # 在条带四角打唯一标记，核对 EXIF 旋转后的方向和条带顺序
    s1[0, 0] = (255, 1, 2)
    s2[-1, -1] = (3, 4, 255)

    out = hfc.compose_strips(s1, s2, first_half=first_half, seam_w=g.seam_w, orientation=orientation)
    h1 = hfc.orient_half(s1, orientation)
    h2 = hfc.orient_half(s2, orientation)

    if orientation in (1, 3):
        assert out.shape == (2848, 4288, 3)
        assert h1.shape == (2848, 2088, 3) and h2.shape == (2848, 2088, 3)
        # 接缝列 2088..2199 全为 RGB 0（ori=3 旋转 180° 后 4288-2200=2088 仍对称）
        assert int(out[:, 2088:2200].max()) == 0
        left_out, right_out = out[:, :2088], out[:, 2200:]
        if (orientation == 1 and first_half == "left") or (orientation == 3 and first_half == "right"):
            assert np.array_equal(left_out, h1)
            assert np.array_equal(right_out, h2)
        else:
            assert np.array_equal(left_out, h2)
            assert np.array_equal(right_out, h1)
    else:
        assert out.shape == (4288, 2848, 3)
        assert h1.shape == (2088, 2848, 3) and h2.shape == (2088, 2848, 3)
        # 接缝行 2088..2199 全为 RGB 0
        assert int(out[2088:2200, :].max()) == 0
        top_out, bot_out = out[:2088, :], out[2200:, :]
        # ori=6（顺时针 90°）：传感器左半→上半；ori=8（逆时针 90°）：传感器左半→下半
        if (orientation == 6 and first_half == "left") or (orientation == 8 and first_half == "right"):
            assert np.array_equal(top_out, h1)
            assert np.array_equal(bot_out, h2)
        else:
            assert np.array_equal(top_out, h2)
            assert np.array_equal(bot_out, h1)


@pytest.mark.parametrize("bad_ori", (0, 2, 4, 5, 7, 9))
def test_composer_rejects_mirrored_and_invalid_orientations(bad_ori: int) -> None:
    s = np.zeros((16, 16, 3), dtype=np.uint8)
    with pytest.raises(ValueError, match="unsupported orientation"):
        hfc.validate_orientation(bad_ori)
    with pytest.raises(ValueError, match="unsupported orientation"):
        hfc.compose_strips(s, s, first_half="left", seam_w=4, orientation=bad_ori)
    with pytest.raises(ValueError, match="unsupported orientation"):
        hfc.orient_half(s, bad_ori)


@pytest.mark.parametrize("proxy_w", (2144, 1600, 1200, 800, 640, 536))
def test_composer_proxy_geometry_rounding_formula(proxy_w: int) -> None:
    g = hfc.proxy_geometry(proxy_w)
    want_half = round(2088 * proxy_w / 4288)
    want_x0 = (1099 * proxy_w) // 4288
    want_seam = proxy_w - 2 * want_half
    assert g.width == proxy_w
    assert g.half_w == want_half
    assert g.strip_x0 == want_x0
    assert g.seam_x0 == want_half
    assert g.seam_w == want_seam
    assert g.right_x0 == want_half + want_seam
    assert 2 * g.half_w + g.seam_w == proxy_w

    h = max(4, round(2848 * proxy_w / 4288))
    s1 = np.full((h, g.half_w, 3), 180, dtype=np.uint8)
    s2 = np.full((h, g.half_w, 3), 90, dtype=np.uint8)
    comp = hfc.compose_strips(s1, s2, first_half="left", seam_w=g.seam_w, orientation=1)
    assert comp.shape == (h, proxy_w, 3)
    assert int(comp[:, g.seam_x0 : g.right_x0].max()) == 0
    assert int(comp[:, : g.half_w].min()) == 180
    assert int(comp[:, g.right_x0 :].min()) == 90


@pytest.mark.parametrize("orientation", (1, 3, 6, 8))
@pytest.mark.parametrize("first_half", ("left", "right"))
def test_camera_jpg_singles_crop_columns_1100_and_diptych_half(tmp_path: Path, orientation: int, first_half: str) -> None:
    # 标定边界列：主图列 1099 为红色（不得进入单格 1），列 1100..3187 为绿色，列 3188 为蓝色（不得进入单格 1）
    img1 = np.full((hfc.FULL_HEIGHT, hfc.FULL_WIDTH, 3), (20, 20, 20), dtype=np.uint8)
    img1[:, 1099] = (250, 0, 0)
    img1[:, 1100:3188] = (20, 200, 20)
    img1[:, 3188] = (0, 0, 250)
    p1 = tmp_path / f"first_{orientation}_{first_half}.jpg"
    p1.write_bytes(_encode_jpeg_bytes(img1, exif_orientation=orientation, quality=98))

    left_fill = np.full((hfc.FULL_HEIGHT, hfc.FULL_HALF_W, 3), (20, 200, 20), dtype=np.uint8)
    right_fill = np.full((hfc.FULL_HEIGHT, hfc.FULL_HALF_W, 3), (20, 40, 210), dtype=np.uint8)
    if first_half == "right":
        left_fill, right_fill = right_fill, left_fill
    p2 = _make_g250_diptych_jpg(tmp_path / f"dip_{orientation}_{first_half}.jpg", left_fill, right_fill, exif_orientation=orientation)

    h1, h2 = hfc.camera_jpg_singles(p1, p2, first_half=first_half, orientation=orientation)
    want_shape = (2848, 2088, 3) if orientation in (1, 3) else (2088, 2848, 3)
    assert h1.shape == want_shape and h2.shape == want_shape
    # 单格 1 是绿块（R 很低、G 很高），单格 2 是蓝块（B 很高、G 很低）
    assert float(h1[:, :, 1].mean()) > 180.0 and float(h1[:, :, 0].mean()) < 40.0
    assert float(h2[:, :, 2].mean()) > 180.0 and float(h2[:, :, 1].mean()) < 60.0


# --------------------------------------------------------------------------- 2. 检测规则表（合成特征向量 + 合成 JPEG/MPO）


def test_decide_pair_decision_table_vectors() -> None:
    # 明确命中左半 / 右半
    d_left = hfp.decide_pair((1.2, 22.0), (0.996, 0.10), orientation=1)
    assert d_left is not None and d_left.first_half == "left" and d_left.flat is False

    d_right = hfp.decide_pair((18.5, 1.8), (0.05, 0.994), orientation=8)
    assert d_right is not None and d_right.first_half == "right" and d_right.flat is False

    # 平场（两侧同时过线）：按 EXIF 方向决定 first_half，flat=True
    for ori, want_half in ((1, "left"), (6, "left"), (3, "right"), (8, "right")):
        d_flat = hfp.decide_pair((0.8, 1.1), (0.995, 0.993), orientation=ori)
        assert d_flat is not None and d_flat.first_half == want_half and d_flat.flat is True

    # 边界与拒绝项：
    # 1) 另一侧 MAD ≤ 8（歧义，未过线但也未拉开）→ 拒
    assert hfp.decide_pair((1.0, 7.9), (0.995, 0.50), orientation=1) is None
    # 2) 两边都不满足 MAD ≤ 3 且 corr ≥ 0.99 → 拒
    assert hfp.decide_pair((3.1, 25.0), (0.995, 0.10), orientation=1) is None
    assert hfp.decide_pair((1.0, 25.0), (0.989, 0.10), orientation=1) is None
    # 3) 候选前置不满足：非相邻、非 first_like、非 g250 接缝（legacy48）、方向不一致、镜像方向
    assert hfp.decide_pair((0.5, 25.0), (0.999, 0.0), orientation=1, adjacent=False) is None
    assert hfp.decide_pair((0.5, 25.0), (0.999, 0.0), orientation=1, a_first_like=False) is None
    assert hfp.decide_pair((0.5, 25.0), (0.999, 0.0), orientation=1, b_seam_kind=hf.SEAM_LEGACY48) is None
    assert hfp.decide_pair((0.5, 25.0), (0.999, 0.0), orientation=1, same_orientation=False) is None
    for bad_ori in (2, 4, 5, 7):
        assert hfp.decide_pair((0.5, 25.0), (0.999, 0.0), orientation=bad_ori) is None


def _seam_kind_file(path: Path) -> str | None:
    ok, luma = hf.decode_draft_luma(path)
    assert ok and luma is not None
    return hf.seam_kind_luma(luma)


def test_synthetic_mpo_and_jpeg_detection_normal_flat_and_black_stripe_rejection(tmp_path: Path) -> None:
    assert not hasattr(hf, "seam_kind_of")
    s1 = _textured_half(hfc.FULL_HEIGHT, hfc.FULL_HALF_W, seed=1)
    s2 = _textured_half(hfc.FULL_HEIGHT, hfc.FULL_HALF_W, seed=4)
    s_other = _textured_half(hfc.FULL_HEIGHT, hfc.FULL_HALF_W, seed=9)

    # 1) 正常 g250 第 1 张（带 MPF 黑边预览）+ 正常左/右双联
    first_left = _make_g250_first_mpo(tmp_path / "f1.jpg", s1, exif_orientation=1)
    dip_left = _make_g250_diptych_jpg(tmp_path / "d1.jpg", s1, s2, exif_orientation=1)
    assert hfp.detect_first_like(first_left) is True
    assert _seam_kind_file(dip_left) == hf.SEAM_G250
    res_l = hfp.evaluate_candidate(first_left, dip_left, orientation_a=1, orientation_b=1, a_first_like=True)
    assert res_l is not None and res_l.first_half == "left" and res_l.flat is False

    first_right = _make_g250_first_mpo(tmp_path / "f2.jpg", s1, exif_orientation=8)
    dip_right = _make_g250_diptych_jpg(tmp_path / "d2.jpg", s2, s1, exif_orientation=8)
    res_r = hfp.evaluate_candidate(first_right, dip_right, orientation_a=8, orientation_b=8, a_first_like=True)
    assert res_r is not None and res_r.first_half == "right" and res_r.flat is False

    # 普通 3:2（MPF 预览无两侧黑边）→ first_like 必须 False
    plain_mpo = _make_g250_first_mpo(tmp_path / "plain.jpg", s1, exif_orientation=1, preview_black_bars=False)
    assert hfp.detect_first_like(plain_mpo) is False

    # 2) 平场场景（两边都一样纯灰）：仍按方向判定，flat=True
    flat_half = np.full((hfc.FULL_HEIGHT, hfc.FULL_HALF_W, 3), 135, dtype=np.uint8)
    first_flat = _make_g250_first_mpo(tmp_path / "f_flat.jpg", flat_half, exif_orientation=3)
    dip_flat = _make_g250_diptych_jpg(tmp_path / "d_flat.jpg", flat_half, flat_half, exif_orientation=3)
    res_flat = hfp.evaluate_candidate(first_flat, dip_flat, orientation_a=3, orientation_b=3, a_first_like=True)
    assert res_flat is not None and res_flat.first_half == "right" and res_flat.flat is True

    # 3) 真场景中恰好中央有黑条（过黑缝形状检查），但像素指纹不匹配第 1 张 → 拒绝
    black_stripe_scene = _make_g250_diptych_jpg(tmp_path / "scene_black_stripe.jpg", s_other, s2, exif_orientation=1)
    assert _seam_kind_file(black_stripe_scene) == hf.SEAM_G250
    assert hfp.evaluate_candidate(first_left, black_stripe_scene, orientation_a=1, orientation_b=1, a_first_like=True) is None

    # 4) legacy 48px 双联接缝 → 拒绝配对
    legacy_dip = _make_g250_diptych_jpg(tmp_path / "legacy48.jpg", s1, s2, exif_orientation=1, seam_range=(2120, 2167))
    assert _seam_kind_file(legacy_dip) == hf.SEAM_LEGACY48
    assert hfp.evaluate_candidate(first_left, legacy_dip, orientation_a=1, orientation_b=1, a_first_like=True) is None


# --------------------------------------------------------------------------- 3. PairStore：版本守卫、只读 .studio、rebuild


def test_pair_store_version_guard_and_roundtrip(tmp_path: Path) -> None:
    assert hfp.PAIRS_REL in lib_loc.STUDIO_PATHS
    p = tmp_path / hfp.PAIRS_FILE
    store = hfp.PairStore(p)
    sha1 = "1" * 64
    sha2 = "2" * 64
    rec = hfp.make_pair_record(
        sha1,
        sha2,
        hfp.PairDecision(first_half="left", flat=False, mad=(1.1, 24.5), corr=(0.997, 0.12)),
    )
    store.put_first_like(sha1, True, save=False)
    store.put_pair(rec, save=True)
    assert p.is_file()

    reloaded = hfp.PairStore(p)
    reloaded.load()
    assert reloaded.get_first_like(sha1) is True
    got = reloaded.pair_for_second(sha2)
    assert got is not None and got["first_sha"] == sha1 and got["first_half"] == "left"
    assert reloaded.pair_for_first(sha1) == got

    # 未来版本守卫：version=99 不覆盖写盘，读出为空且不抛错
    future_raw = json.dumps({"version": 99, "first_like": {sha1: True}, "pairs": {"future": True}})
    p.write_text(future_raw, encoding="utf-8")
    guarded = hfp.PairStore(p)
    guarded.load()
    assert guarded.get_first_like(sha1) is None
    assert guarded.pair_for_second(sha2) is None
    guarded.put_first_like("3" * 64, True, save=True)
    guarded.put_pair(rec, save=True)
    assert p.read_text(encoding="utf-8") == future_raw


def test_pair_store_readonly_studio_returns_200_and_rebuild_survives(
    settings: Any,
    lib: Library,
    volumes: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s1 = _textured_half(hfc.FULL_HEIGHT, hfc.FULL_HALF_W, seed=2)
    s2 = _textured_half(hfc.FULL_HEIGHT, hfc.FULL_HALF_W, seed=5)
    dcim = volumes / "CARD" / "DCIM" / "100RICOH"
    dcim.mkdir(parents=True, exist_ok=True)
    p1 = _make_g250_first_mpo(dcim / "R0001001.JPG", s1, exif_orientation=1)
    p2 = _make_g250_diptych_jpg(dcim / "R0001002.JPG", s1, s2, exif_orientation=1)
    # 同时写第 3 张无伙伴的 first_like，测 half_first 标记
    p3 = _make_g250_first_mpo(dcim / "R0001005.JPG", s2, exif_orientation=1)
    sha1 = hashlib.sha256(p1.read_bytes()).hexdigest()
    sha2 = hashlib.sha256(p2.read_bytes()).hexdigest()
    sha3 = hashlib.sha256(p3.read_bytes()).hexdigest()

    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    pairs_path = lib.abspath(hfp.PAIRS_REL)
    assert pairs_path.is_file()
    stored = json.loads(pairs_path.read_text(encoding="utf-8"))
    assert stored["version"] == 1
    assert stored["first_like"][sha1] is True
    assert stored["first_like"][sha3] is True
    assert stored["pairs"][sha2]["first_sha"] == sha1
    assert stored["pairs"][sha2]["first_half"] == "left"

    shots = {s["files"]["jpg"]["sha256"]: s for s in lib.list_shots()}
    assert shots[sha1]["pair"]["role"] == "first"
    assert shots[sha1]["pair"]["partner_id"] == shots[sha2]["shot_id"]
    assert shots[sha2]["pair"]["role"] == "second"
    assert shots[sha2]["pair"]["partner_id"] == shots[sha1]["shot_id"]
    assert shots[sha2]["diptych"] is True
    assert shots[sha3].get("half_first") is True
    assert "pair" not in shots[sha3]

    # 删除 pairs 文件后执行 Library.rebuild()：重新算回并写盘
    pairs_path.unlink()
    lib._pairs.load()
    lib.rebuild()
    assert pairs_path.is_file()
    rebuilt = json.loads(pairs_path.read_text(encoding="utf-8"))
    assert rebuilt["pairs"][sha2]["first_sha"] == sha1
    assert rebuilt["first_like"][sha3] is True

    # 只读 `.studio`（0o555）+ 清掉 pairs / verdicts 文件：API 仍返回 200 且日志不带图库路径
    pairs_path.unlink()
    lib.abspath(hf.HALFFRAME_REL).unlink(missing_ok=True)
    lib.close()
    monkeypatch.setattr(appmod, "start_halfframe_job", lambda _ctx: None)
    logger = logging.getLogger("gxr_studio")
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    monkeypatch.setattr(logger, "propagate", False)
    caplog.set_level(logging.INFO, logger="gxr_studio")

    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        studio_dir = settings.library_dir / ".studio"
        os.chmod(studio_dir, 0o555)
        try:
            res_list = client.get("/api/photos")
            res_dates = client.get("/api/dates")
            res_one = client.get(f"/api/photos/{shots[sha2]['shot_id']}")
            assert res_list.status_code == 200 and len(res_list.json()) == 3
            assert res_dates.status_code == 200
            assert res_one.status_code == 200
            assert res_one.json()["diptych"] is True
        finally:
            os.chmod(studio_dir, 0o755)

    for rec_log in caplog.records:
        assert str(settings.library_dir) not in rec_log.message


# --------------------------------------------------------------------------- 4. Fix round 1 合成回归（P2-1 / P2-2 / P3-2 / P3-3 / P3-6）


def test_pair_with_jpeg_only_photo1_exports_camera_jpg_and_rejects_raw_edits_with_409(
    settings: Any,
    lib: Library,
    volumes: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2-1：photo 1 只有 JPG 的配对在 singles/both 切相机 JPG；需 RAW 的编辑返回 409 no_raw，批量导出不标 failed。"""
    s1 = _textured_half(hfc.FULL_HEIGHT, hfc.FULL_HALF_W, seed=11)
    s2 = _textured_half(hfc.FULL_HEIGHT, hfc.FULL_HALF_W, seed=12)
    dcim = volumes / "CARD" / "DCIM" / "100RICOH"
    dcim.mkdir(parents=True, exist_ok=True)
    _make_g250_first_mpo(dcim / "R0002001.JPG", s1, exif_orientation=1)
    p2 = _make_g250_diptych_jpg(dcim / "R0002002.JPG", s1, s2, exif_orientation=1)
    sha2 = hashlib.sha256(p2.read_bytes()).hexdigest()

    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    by_sha = {s["files"]["jpg"]["sha256"]: s for s in lib.list_shots()}
    sid2 = by_sha[sha2]["shot_id"]
    shot2 = lib.get_shot(sid2)
    assert shot2 is not None and hfp.is_pair_second(shot2) and not hfp.has_pair_raw(shot2)
    lib.close()

    # 让 render.renderer 在错误尝试走 RAW 时返回哑渲染器（否则 52cd005 在无相机数据时会被 RenderUnavailable 掩盖）
    dummy_renderer = Renderer("recipe-prediction", "配方", "_gxr", True, lambda codes: np.zeros((*codes.shape[:2], 3), np.uint8))

    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        render_svc = client.app.state.ctx.render  # type: ignore[union-attr]
        monkeypatch.setattr(render_svc, "renderer", lambda *_a, **_kw: dummy_renderer)

        up_disp = client.post(
            "/api/lut/upload",
            content=cube_bytes(lambda x: np.clip(x * 0.9 + 0.05, 0, 1), size=9),
            headers={"X-GXR-Filename": "disp.cube"},
        )
        assert up_disp.status_code == 200
        disp_lut = up_disp.json()["cube_id"]
        client.patch(f"/api/luts/{disp_lut}", json={"input_space": "srgb"})

        up_log = client.post(
            "/api/lut/upload",
            content=cube_bytes(lambda x: np.clip(x * 0.8 + 0.1, 0, 1), size=9),
            headers={"X-GXR-Filename": "log.cube"},
        )
        assert up_log.status_code == 200
        log_lut = up_log.json()["cube_id"]
        client.patch(f"/api/luts/{log_lut}", json={"input_space": "llog"})

        # 1) 未编辑：diptych / singles / both 都走相机 JPG 成功导出
        for mode, expect_files in (("diptych", None), ("singles", 2), ("both", 3)):
            out_dir = tmp_path / f"exp_{mode}"
            res = client.post(f"/api/photos/{sid2}/export", json={"halfframe": mode, "dest_dir": str(out_dir)})
            assert res.status_code == 200, (mode, res.text)
            body = res.json()
            if expect_files is None:
                assert body.get("files") is None
            else:
                assert len(body["files"]) == expect_files
                for entry in body["files"]:
                    assert Path(entry["path"]).is_file()
                    if entry["role"] in ("first", "second"):
                        assert (entry["width"], entry["height"]) == (hfc.FULL_HALF_W, hfc.FULL_HEIGHT)

        # 2) Display LUT 在 singles / both 上照常切相机 JPG 后套用
        assert client.put(f"/api/photos/{sid2}/edit", json={"lut": disp_lut}).status_code == 200
        res_disp = client.post(f"/api/photos/{sid2}/export", json={"halfframe": "singles", "dest_dir": str(tmp_path / "exp_disp")})
        assert res_disp.status_code == 200 and len(res_disp.json()["files"]) == 2
        assert client.delete(f"/api/photos/{sid2}/edit").status_code == 200

        # 3) 批量导出（singles / both）：永远不标 failed
        for mode in ("singles", "both"):
            job = client.post(
                "/api/export",
                json={"ids": [sid2], "halfframe": mode, "dest_dir": str(tmp_path / f"batch_{mode}")},
            ).json()
            for _ in range(100):
                st = client.get(f"/api/jobs/{job['id']}").json()
                if st["progress"]["state"] != "running":
                    break
                time.sleep(0.05)
            assert st["progress"]["state"] == "done"
            assert all("error" not in item for item in st["progress"]["results"]), st["progress"]["results"]

        # 4) 显式需要 RAW 的编辑（配方或 Log LUT）→ 409 no_raw，绝非 500
        lib_live = client.app.state.ctx.lib  # type: ignore[union-attr]
        shot_live = lib_live.get_shot(sid2)
        assert shot_live is not None
        sc_path = lib_live.sidecar_path_for(shot_live)
        base_side = json.loads(sc_path.read_text(encoding="utf-8"))

        sc_path.write_text(json.dumps({**base_side, "recipe": {"unit": "qlaunch", "slot": 2}}), encoding="utf-8")
        for mode in ("diptych", "singles", "both"):
            r_rec = client.post(f"/api/photos/{sid2}/export", json={"halfframe": mode, "dest_dir": str(tmp_path / f"rec_{mode}")})
            assert r_rec.status_code == 409 and r_rec.json()["error"] == "no_raw", (mode, r_rec.status_code, r_rec.text)

        sc_path.write_text(json.dumps({**base_side, "lut": log_lut}), encoding="utf-8")
        for mode in ("diptych", "singles", "both"):
            r_log = client.post(f"/api/photos/{sid2}/export", json={"halfframe": mode, "dest_dir": str(tmp_path / f"log_{mode}")})
            assert r_log.status_code == 409 and r_log.json()["error"] == "no_raw", (mode, r_log.status_code, r_log.text)

        # 批量导出遇到需 RAW 的编辑时也记 no_raw，不记 failed
        job_nr = client.post("/api/export", json={"ids": [sid2], "halfframe": "singles", "dest_dir": str(tmp_path / "batch_nr")}).json()
        for _ in range(100):
            st_nr = client.get(f"/api/jobs/{job_nr['id']}").json()
            if st_nr["progress"]["state"] != "running":
                break
            time.sleep(0.05)
        assert st_nr["progress"]["results"][0]["error"] == "no_raw"


def test_pair_neighbor_names_and_single_shot_read_avoids_full_scan(lib: Library, volumes: Path) -> None:
    """P2-2：已存配对读单张只走一次 files_sha 索引短路；未配对只查 ±1 邻居名，不扫全表。"""
    assert hfp.neighbor_names("R0010002", -1) == ["R0010001", "R10001"]
    assert hfp.neighbor_names("R0010002", 1) == ["R0010003", "R10003"]
    assert hfp.neighbor_names("R1000", -1) == ["R0999", "R999"]
    assert hfp.neighbor_names("R999", 1) == ["R1000"]
    assert hfp.neighbor_names("R0000", -1) == []
    assert hfp.neighbor_names("plain", -1) == []

    s1 = _textured_half(hfc.FULL_HEIGHT, hfc.FULL_HALF_W, seed=21)
    s2 = _textured_half(hfc.FULL_HEIGHT, hfc.FULL_HALF_W, seed=22)
    s3 = _textured_half(hfc.FULL_HEIGHT, hfc.FULL_HALF_W, seed=23)
    dcim = volumes / "CARD" / "DCIM" / "100RICOH"
    dcim.mkdir(parents=True, exist_ok=True)
    _make_g250_first_mpo(dcim / "R0003001.JPG", s1, exif_orientation=1)
    p2 = _make_g250_diptych_jpg(dcim / "R0003002.JPG", s1, s2, exif_orientation=1)
    p_unpaired = _make_g250_diptych_jpg(dcim / "R0003010.JPG", s2, s3, exif_orientation=1)
    sha2 = hashlib.sha256(p2.read_bytes()).hexdigest()
    sha_u = hashlib.sha256(p_unpaired.read_bytes()).hexdigest()
    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))

    by_sha = {s["files"]["jpg"]["sha256"]: s for s in lib.list_shots()}
    sid2 = by_sha[sha2]["shot_id"]
    sid_u = by_sha[sha_u]["shot_id"]

    qlog = lib.trace_queries()
    got2 = lib.get_shot(sid2)
    assert got2 is not None and got2["pair"]["role"] == "second"
    assert not any("s.width = 4288" in q for q in qlog), qlog
    assert not any("s.name IN" in q for q in qlog), qlog
    assert sum("f.sha256 IN" in q for q in qlog) == 1, qlog

    qlog.clear()
    got_u = lib.get_shot(sid_u)
    assert got_u is not None and got_u["diptych"] is True and "pair" not in got_u
    assert not any("s.width = 4288" in q for q in qlog), qlog
    assert any("s.name IN" in q for q in qlog), qlog


def test_pair_singles_metadata_matches_each_half_rendered_recipe(
    settings: Any,
    lib: Library,
    volumes: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P3-2：每张单格的导出元数据（render_label / recipe_binding / 文件名后缀 / EXIF 说明）准确对应各自实际渲染的配方。"""
    s1 = _textured_half(hfc.FULL_HEIGHT, hfc.FULL_HALF_W, seed=31)
    s2 = _textured_half(hfc.FULL_HEIGHT, hfc.FULL_HALF_W, seed=32)
    dcim = volumes / "CARD" / "DCIM" / "100RICOH"
    dcim.mkdir(parents=True, exist_ok=True)
    _make_g250_first_mpo(dcim / "R0004001.JPG", s1, exif_orientation=1)
    p2 = _make_g250_diptych_jpg(dcim / "R0004002.JPG", s1, s2, exif_orientation=1)
    sha2 = hashlib.sha256(p2.read_bytes()).hexdigest()
    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))

    by_sha = {s["files"]["jpg"]["sha256"]: s for s in lib.list_shots()}
    shot2 = lib.get_shot(by_sha[sha2]["shot_id"])
    assert shot2 is not None
    # 给两半挂上虚拟 DNG 路径以便进入 RAW 单格导出分支，条带渲染用 stub
    for key in ("first", "second"):
        src = shot2["pair_sources"][key]
        src["all_files"]["dng"] = {"kind": "dng", "relpath": src["all_files"]["jpg"]["relpath"], "sha256": key * 16}

    def _mk_renderer(label: str, tag: str, desc: str, sel: str) -> Renderer:
        return Renderer(
            "recipe-prediction",
            label,
            tag,
            True,
            lambda codes: np.zeros((*codes.shape[:2], 3), np.uint8),
            description=desc,
            recipe_binding={
                "record_sha256": "a" * 64,
                "entry_sha256": "b" * 64,
                "binding_sha256": "c" * 64,
                "selection_id": sel,
                "quality_state": "passed",
                "assessed_entry": True,
            },
        )

    r_first = _mk_renderer("配方一", "_r1", "GXR Studio recipe-first", "builtin:1")
    r_second = _mk_renderer("配方二", "_r2", "GXR Studio recipe-second", "builtin:2")
    r_explicit = _mk_renderer("配方五", "_r5", "GXR Studio recipe-explicit-5", "builtin:5")

    class _StubRender:
        home = settings.home_dir

        def recipe_menu(self) -> Any:
            return None

        def renderer(self, half_shot: dict[str, Any], edit: EditState, *, precise: bool = False) -> Renderer:
            if edit.recipe is not None and edit.recipe.slot == 5:
                return r_explicit
            return r_first if half_shot["shot_id"] == shot2["pair_sources"]["first"]["shot_id"] else r_second

    geo = hfc.full_geometry()
    monkeypatch.setattr(hfc, "_render_one_dng_strip", lambda _p, _r, *, full: (np.full((64, 48, 3), 120, np.uint8), geo))

    # 1) 未显式改配方：两张单格分别带上各自拍摄配方元数据
    out_auto = hfe.export_pair_finish(
        _StubRender(),
        lib,
        shot2,
        EditState(),
        {},
        tmp_path / "p3_2_auto",
        [],
        quality=90,
        long_edge=None,
        fmt="jpeg",
        mode="both",
    )
    by_role = {role: res for _sid, role, res in out_auto}
    assert by_role["first"].render_label == "配方一"
    assert by_role["first"].recipe_binding["selection_id"] == "builtin:1"
    assert by_role["first"].path.name.endswith("_r1_half.jpg")
    assert by_role["second"].render_label == "配方二"
    assert by_role["second"].recipe_binding["selection_id"] == "builtin:2"
    assert by_role["second"].path.name.endswith("_r2_half.jpg")
    with Image.open(by_role["first"].path) as im1, Image.open(by_role["second"].path) as im2:
        assert "recipe-first" in str(im1.getexif().get(270, ""))
        assert "recipe-second" in str(im2.getexif().get(270, ""))

    # 2) 显式改配方（slot 5）：两张单格都带上第 2 张选中的配方元数据
    out_exp = hfe.export_pair_finish(
        _StubRender(),
        lib,
        shot2,
        EditState(recipe=RecipeSel("qlaunch", 5)),
        {},
        tmp_path / "p3_2_explicit",
        [],
        quality=90,
        long_edge=None,
        fmt="jpeg",
        mode="singles",
    )
    by_role_e = {role: res for _sid, role, res in out_exp}
    for role in ("first", "second"):
        assert by_role_e[role].render_label == "配方五"
        assert by_role_e[role].recipe_binding["selection_id"] == "builtin:5"
        assert by_role_e[role].path.name.endswith("_r5_half.jpg")
        with Image.open(by_role_e[role].path) as im:
            assert "recipe-explicit-5" in str(im.getexif().get(270, ""))


def test_rejected_pair_candidates_persisted_across_restart_and_version_guard(settings: Any, lib: Library, volumes: Path) -> None:
    """P3-3：已检验但未配对的候选以纯 SHA 持久化，重启图库后回填不重复跑；未来版本守卫保持有效。"""
    s1 = _textured_half(hfc.FULL_HEIGHT, hfc.FULL_HALF_W, seed=41)
    s_other = _textured_half(hfc.FULL_HEIGHT, hfc.FULL_HALF_W, seed=42)
    s2 = _textured_half(hfc.FULL_HEIGHT, hfc.FULL_HALF_W, seed=43)
    dcim = volumes / "CARD" / "DCIM" / "100RICOH"
    dcim.mkdir(parents=True, exist_ok=True)
    p1 = _make_g250_first_mpo(dcim / "R0005001.JPG", s1, exif_orientation=1)
    p2 = _make_g250_diptych_jpg(dcim / "R0005002.JPG", s_other, s2, exif_orientation=1)
    sha1 = hashlib.sha256(p1.read_bytes()).hexdigest()
    sha2 = hashlib.sha256(p2.read_bytes()).hexdigest()
    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))

    pairs_path = lib.abspath(hfp.PAIRS_REL)
    pairs_path.unlink(missing_ok=True)
    lib.close()

    lib2 = Library(settings.library_dir)
    lib2.open()
    try:
        todo = lib2.halfframe_pair_pending()
        assert len(todo) >= 1
        hfl.backfill_pairs(lib2, todo, hf.HalfframeProgress(total=len(todo)), threading.Event())
        stored = json.loads(pairs_path.read_text(encoding="utf-8"))
        assert stored["rejected"] == {sha2: [sha1]}
        assert "R0005001" not in pairs_path.read_text(encoding="utf-8")
        assert lib2.halfframe_pair_pending() == []
    finally:
        lib2.close()

    # 重启图库后待回填队列必须为空
    lib3 = Library(settings.library_dir)
    lib3.open()
    try:
        assert lib3._pairs.is_rejected(sha1, sha2) is True
        assert lib3.halfframe_pair_pending() == []
    finally:
        lib3.close()

    # 未来版本守卫：version=99 不被 mark_rejected 或 put_batch 覆盖
    future_raw = json.dumps({"version": 99, "first_like": {}, "pairs": {}, "rejected": {sha2: [sha1]}})
    pairs_path.write_text(future_raw, encoding="utf-8")
    guarded = hfp.PairStore(pairs_path)
    guarded.load()
    guarded.mark_rejected("a" * 64, "b" * 64, save=True)
    guarded.put_batch({}, {}, rejected={("a" * 64, "b" * 64)}, save=True)
    assert pairs_path.read_text(encoding="utf-8") == future_raw


def test_routes_and_finish_serve_line_budget() -> None:
    """P3-6：api/routes.py 与 finish/serve.py 各自不超过 720 行。"""
    src_root = Path(__file__).resolve().parents[1] / "src" / "gxr_studio"
    routes_lines = len((src_root / "api" / "routes.py").read_text(encoding="utf-8").splitlines())
    serve_lines = len((src_root / "finish" / "serve.py").read_text(encoding="utf-8").splitlines())
    assert routes_lines <= 720, f"api/routes.py is {routes_lines} lines (> 720)"
    assert serve_lines <= 720, f"finish/serve.py is {serve_lines} lines (> 720)"
