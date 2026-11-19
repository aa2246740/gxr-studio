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
from gxr_studio import halfframe_pairs as hfp
from gxr_studio.app import create_app
from gxr_studio.cards.discovery import find_cards
from gxr_studio.cards.importer import ImportOptions, run_import
from gxr_studio.library import location as lib_loc
from gxr_studio.library.store import Library

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


def test_synthetic_mpo_and_jpeg_detection_normal_flat_and_black_stripe_rejection(tmp_path: Path) -> None:
    s1 = _textured_half(hfc.FULL_HEIGHT, hfc.FULL_HALF_W, seed=1)
    s2 = _textured_half(hfc.FULL_HEIGHT, hfc.FULL_HALF_W, seed=4)
    s_other = _textured_half(hfc.FULL_HEIGHT, hfc.FULL_HALF_W, seed=9)

    # 1) 正常 g250 第 1 张（带 MPF 黑边预览）+ 正常左/右双联
    first_left = _make_g250_first_mpo(tmp_path / "f1.jpg", s1, exif_orientation=1)
    dip_left = _make_g250_diptych_jpg(tmp_path / "d1.jpg", s1, s2, exif_orientation=1)
    assert hfp.detect_first_like(first_left) is True
    assert hf.seam_kind_of(dip_left) == hf.SEAM_G250
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
    assert hf.seam_kind_of(black_stripe_scene) == hf.SEAM_G250
    assert hfp.evaluate_candidate(first_left, black_stripe_scene, orientation_a=1, orientation_b=1, a_first_like=True) is None

    # 4) legacy 48px 双联接缝 → 拒绝配对
    legacy_dip = _make_g250_diptych_jpg(tmp_path / "legacy48.jpg", s1, s2, exif_orientation=1, seam_range=(2120, 2167))
    assert hf.seam_kind_of(legacy_dip) == hf.SEAM_LEGACY48
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
