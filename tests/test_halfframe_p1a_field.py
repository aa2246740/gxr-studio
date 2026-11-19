"""halfframe-p1a 实拍验收（本地 `GXR_STUDIO_TEST_PARENT`，缺失时自动跳过；日志与断言只用目录和序号，不输出照片名/时间/EXIF）。

覆盖：
- 验收 1：4 个实拍目录检测 + 负对照（非相邻首格 MAD > 8、对调半格拒绝）；
- 验收 2：card250 全部 3 对全分辨率重算（neutral 与拍摄配方）相位相关 (0,0)、亮度相关 ≥ 0.98、黑缝位置与第 1 张匹配；
- 验收 3：端到端（导入 g250 相机数据与开发标定）：/render?stage=final、默认导出、用户配方、Log LUT、neutral、
  LUT 预览与 edited_jpeg 缩略图、diptych / singles / both 导出与记忆偏好、尺寸档、按日期批量去重（pairs: 3）、HDR 拒绝；
- 验收 4：card250 第 1 张（单独导出）、第 7 张、第 11 张（XPan）、card249 普通 3:2（第 5 张）与旧双联（第 2 张）
  默认导出字节与 `1e8e9ac` 逐字节一致。
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image, ImageOps

from conftest import PORT
from gxr_studio import export as expmod
from gxr_studio import halfframe as hf
from gxr_studio import halfframe_compose as hfc
from gxr_studio import halfframe_pairs as hfp
from gxr_studio.app import create_app
from gxr_studio.cards.discovery import find_cards
from gxr_studio.cards.importer import ImportOptions, run_import
from gxr_studio.hdr import derived as hdr_derived
from gxr_studio.library.store import Library
from gxr_studio.render.params import EditState
from gxr_studio.render.service import RenderService
from test_halfframe_p0 import (
    REPO,
    _clear_desktop_cache,
    _field_folder,
    _setup_g250_calibrated_home,
    _sorted_jpgs,
    _sorted_pairs,
    _stage_volume_pairs,
)
from test_lut import cube_bytes

BASE_COMMIT = "1e8e9ac"


def _phase_shift_and_corr(a: np.ndarray, b: np.ndarray) -> tuple[tuple[int, int], float]:
    """计算两张同尺寸灰度/RGB 图的相位相关位移 (dy, dx) 与像素亮度相关系数。"""
    assert a.shape == b.shape, (a.shape, b.shape)
    la = (0.299 * a[:, :, 0] + 0.587 * a[:, :, 1] + 0.114 * a[:, :, 2]).astype(np.float64) if a.ndim == 3 else a.astype(np.float64)
    lb = (0.299 * b[:, :, 0] + 0.587 * b[:, :, 1] + 0.114 * b[:, :, 2]).astype(np.float64) if b.ndim == 3 else b.astype(np.float64)
    da = la - float(la.mean())
    db = lb - float(lb.mean())
    denom = float(np.sqrt(np.sum(da * da) * np.sum(db * db)))
    corr = float(np.sum(da * db) / denom) if denom > 1e-12 else 0.0

    # 用中心 1024×1024（或短边裁剪）做 FFT 相位相关，避免边角并保持 O(N log N) 快速
    h, w = da.shape
    ch = min(h, 1024)
    cw = min(w, 1024)
    y0, x0 = (h - ch) // 2, (w - cw) // 2
    fa = np.fft.rfft2(da[y0 : y0 + ch, x0 : x0 + cw])
    fb = np.fft.rfft2(db[y0 : y0 + ch, x0 : x0 + cw])
    cross = fa * np.conj(fb)
    mag = np.abs(cross)
    peak = np.fft.irfft2(cross / np.where(mag > 1e-12, mag, 1.0), s=(ch, cw))
    idx = int(np.argmax(peak))
    py, px = divmod(idx, cw)
    if py > ch // 2:
        py -= ch
    if px > cw // 2:
        px -= cw
    return (int(py), int(px)), corr


def _seam_and_halves(arr: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """按长边拆分已转正双联图的 (半格 A, 黑缝区, 半格 B)。"""
    h, w = arr.shape[:2]
    if w >= h:
        hw = round(2088 * w / 4288)
        seam_w = w - 2 * hw
        return arr[:, :hw], arr[:, hw : hw + seam_w], arr[:, hw + seam_w :]
    hw = round(2088 * h / 4288)
    seam_w = h - 2 * hw
    return arr[:hw, :], arr[hw : hw + seam_w, :], arr[hw + seam_w :, :]


def _seam_core(arr: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """拆分半格并返回去掉边缘 3px（防缩图插值与 JPEG DCT 振铃）后的黑缝内核灰度。"""
    ha, seam, hb = _seam_and_halves(arr)
    h, w = arr.shape[:2]
    if w >= h:
        margin = min(3, max(0, (seam.shape[1] - 2) // 2))
        core = seam[:, margin : seam.shape[1] - margin] if margin else seam
    else:
        margin = min(3, max(0, (seam.shape[0] - 2) // 2))
        core = seam[margin : seam.shape[0] - margin, :] if margin else seam
    core_luma = (0.299 * core[:, :, 0] + 0.587 * core[:, :, 1] + 0.114 * core[:, :, 2]).astype(np.uint8)
    return ha, core_luma, hb


def _poll_job(client: TestClient, job_id: str, timeout: float = 120.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get(f"/api/jobs/{job_id}")
        assert r.status_code == 200, r.text
        data: dict[str, Any] = r.json()
        if data["progress"]["state"] in ("done", "error", "cancelled"):
            return data
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} timed out")


def _index_shots_by_jpg_sha(lib: Library, pairs: list[tuple[Path, Path]]) -> dict[int, dict[str, Any]]:
    by_sha = {s["files"]["jpg"]["sha256"]: s for s in lib.list_shots(limit=1000)}
    out: dict[int, dict[str, Any]] = {}
    for idx, (_dng, jpg) in enumerate(pairs, start=1):
        sha = hashlib.sha256(jpg.read_bytes()).hexdigest()
        if sha in by_sha:
            out[idx] = by_sha[sha]
    return out


# --------------------------------------------------------------------------- 验收 1：实拍目录检测 + 负对照


def test_field_detection_all_four_folders_and_negative_controls(
    settings: Any,
    tmp_path: Path,
) -> None:
    _clear_desktop_cache()

    # 1) card250：(1,2) left、(3,4) right、(5,6) left，7..10 half_first，11 none
    card250_pairs = _sorted_pairs(_field_folder("card250"))
    assert len(card250_pairs) == 11
    vol250 = tmp_path / "vol_card250"
    _stage_volume_pairs(vol250, card250_pairs)
    lib250 = Library(tmp_path / "lib_card250").open()
    try:
        run_import(lib250, find_cards((vol250,))[0], ImportOptions(min_free_bytes=0))
        by_idx = _index_shots_by_jpg_sha(lib250, card250_pairs)
        assert len(by_idx) == 11

        expected_pairs = [(1, 2, "left"), (3, 4, "right"), (5, 6, "left")]
        for i1, i2, want_half in expected_pairs:
            s1, s2 = by_idx[i1], by_idx[i2]
            assert s1.get("pair") is not None, f"card250:{i1} missing pair"
            assert s2.get("pair") is not None, f"card250:{i2} missing pair"
            assert s1["pair"]["role"] == "first" and s1["pair"]["partner_id"] == s2["shot_id"]
            assert s2["pair"]["role"] == "second" and s2["pair"]["partner_id"] == s1["shot_id"]
            assert s2["pair"]["first_half"] == want_half, f"card250:({i1},{i2}) first_half={s2['pair']['first_half']}"
            assert s2["pair"]["kind"] == "g250"
            assert s2["diptych"] is True and s2["has_raw"] == 1
            assert "dng" not in s2["files"]  # P0 关口保持：photo 2 不把自有 DNG 暴露在 files["dng"]

        for idx in (7, 8, 9, 10):
            s = by_idx[idx]
            assert s.get("half_first") is True, f"card250:{idx} expected half_first"
            assert "pair" not in s, f"card250:{idx} unexpectedly paired"

        s11 = by_idx[11]
        assert s11["diptych"] is False and "pair" not in s11 and not s11.get("half_first")
    finally:
        lib250.close()

    # 2) card249, vertical, probe251：0 对、0 half_first，双联保持 JPG-only
    legacy_diptychs = {"card249": {2, 4}, "vertical": {2, 4, 6}, "probe251": {2, 4, 6}}
    for folder_name, dip_indices in legacy_diptychs.items():
        fpairs = _sorted_pairs(_field_folder(folder_name))
        vdir = tmp_path / f"vol_{folder_name}"
        _stage_volume_pairs(vdir, fpairs)
        flib = Library(tmp_path / f"lib_{folder_name}").open()
        try:
            run_import(flib, find_cards((vdir,))[0], ImportOptions(min_free_bytes=0))
            by_idx = _index_shots_by_jpg_sha(flib, fpairs)
            for idx, shot in by_idx.items():
                assert "pair" not in shot, f"{folder_name}:{idx} unexpectedly paired"
                assert not shot.get("half_first"), f"{folder_name}:{idx} unexpectedly half_first"
                if idx in dip_indices:
                    assert shot["diptych"] is True and shot["has_raw"] == 0 and "dng" not in shot["files"]
        finally:
            flib.close()

    # 3) 负对照：每个 card250 双联与所有非相邻首格配对均被拒且 MAD > 8；对调半格/反序均被拒
    jpgs250 = _sorted_jpgs(_field_folder("card250"))
    drafts: dict[int, np.ndarray] = {}
    for idx in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10):
        ok, luma = hf.decode_draft_luma(jpgs250[idx - 1])
        assert ok and luma is not None, f"card250:{idx} draft decode failed"
        drafts[idx] = luma

    true_partners = {2: (1, "left"), 4: (3, "right"), 6: (5, "left")}
    first_candidates = (1, 3, 5, 7, 8, 9, 10)
    for dip_idx, (partner_idx, true_side) in true_partners.items():
        # 非相邻首格：指纹两侧 MAD 均 > 8 且 decide_pair 拒绝
        for f_idx in first_candidates:
            if f_idx == partner_idx:
                continue
            metrics = hfp.fingerprint_metrics(drafts[f_idx], drafts[dip_idx])
            assert metrics is not None
            (mad_l, mad_r), corr = metrics
            assert mad_l > 8.0 and mad_r > 8.0, f"non-adjacent ({f_idx},{dip_idx}) MAD too low: {(mad_l, mad_r)}"
            assert hfp.decide_pair((mad_l, mad_r), corr, orientation=1) is None

        # 对调半格 / 反序：另一半 MAD > 8，反序 (dip, first) 拒绝
        true_metrics = hfp.fingerprint_metrics(drafts[partner_idx], drafts[dip_idx])
        assert true_metrics is not None
        (ml, mr), _ = true_metrics
        other_mad = mr if true_side == "left" else ml
        assert other_mad > 8.0, f"card250:({partner_idx},{dip_idx}) other half MAD={other_mad}"
        assert hfp.detect_first_like(jpgs250[dip_idx - 1]) is False
        assert hfp.evaluate_candidate(jpgs250[dip_idx - 1], jpgs250[partner_idx - 1], orientation_a=1, orientation_b=1) is None
        # 把第 2 张自身半格当第 1 张条带，或对调两半检查原侧：被拒
        swapped = drafts[dip_idx].copy()
        hw = hfp.DRAFT_HALF_W
        rx0 = hfp.DRAFT_RIGHT_X0
        swapped[:, :hw], swapped[:, rx0 : rx0 + hw] = drafts[dip_idx][:, rx0 : rx0 + hw].copy(), drafts[dip_idx][:, :hw].copy()
        sw_metrics = hfp.fingerprint_metrics(drafts[partner_idx], swapped)
        assert sw_metrics is not None
        (sw_ml, sw_mr), _ = sw_metrics
        orig_side_mad = sw_ml if true_side == "left" else sw_mr
        assert orig_side_mad > 8.0, f"swapped halves ({partner_idx},{dip_idx}) orig side MAD={orig_side_mad}"


# --------------------------------------------------------------------------- 验收 2：全分辨率重算（neutral + 拍摄配方）


def test_full_res_rerender_all_three_card250_pairs(settings: Any, lib: Library, volumes: Path) -> None:
    _clear_desktop_cache()
    card250_pairs = _sorted_pairs(_field_folder("card250"))
    _stage_volume_pairs(volumes, card250_pairs[:6])
    _setup_g250_calibrated_home(settings)
    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))

    render_svc = RenderService(settings.home_dir, lib)
    by_idx = _index_shots_by_jpg_sha(lib, card250_pairs[:6])

    for i1, i2, want_half in ((1, 2, "left"), (3, 4, "right"), (5, 6, "left")):
        shot2 = by_idx[i2]
        assert shot2["pair"]["first_half"] == want_half
        cam_dip = np.asarray(ImageOps.exif_transpose(Image.open(card250_pairs[i2 - 1][1])).convert("RGB"), dtype=np.uint8)
        cam_p1_raw = np.asarray(Image.open(card250_pairs[i1 - 1][1]).convert("RGB"), dtype=np.uint8)
        ori = int(shot2.get("orientation") or 1)
        cam_p1_strip = hfc.orient_half(cam_p1_raw[:, hfc.FULL_STRIP_X0_JPG : hfc.FULL_STRIP_X0_JPG + hfc.FULL_HALF_W], ori)

        for mode_label, edit in (("neutral", EditState(recipe="neutral")), ("shooting_recipe", EditState(recipe=None))):
            out_rgb, _labels = hfc.render_pair_rgb(
                render_svc,
                lib,
                shot2,
                edit,
                full=True,
                neutral=(mode_label == "neutral"),
            )
            assert out_rgb.shape == cam_dip.shape, f"pair ({i1},{i2}) {mode_label} shape {out_rgb.shape} != {cam_dip.shape}"
            out_a, out_seam, out_b = _seam_and_halves(out_rgb)
            cam_a, cam_seam, cam_b = _seam_and_halves(cam_dip)
            assert int(out_seam.max()) == 0, f"pair ({i1},{i2}) {mode_label} seam not pure black"
            assert int(cam_seam.max()) <= 12

            shift_a, corr_a = _phase_shift_and_corr(out_a, cam_a)
            shift_b, corr_b = _phase_shift_and_corr(out_b, cam_b)
            assert shift_a == (0, 0), f"pair ({i1},{i2}) {mode_label} half A shift={shift_a}"
            assert shift_b == (0, 0), f"pair ({i1},{i2}) {mode_label} half B shift={shift_b}"
            assert corr_a >= 0.98, f"pair ({i1},{i2}) {mode_label} half A corr={corr_a:.5f}"
            assert corr_b >= 0.98, f"pair ({i1},{i2}) {mode_label} half B corr={corr_b:.5f}"

            # 核对第 1 张所在半格与第 1 张主 JPG 中央条带相关性 ≥ 0.98 且位移 (0, 0)
            p1_half = out_a if ((ori in (1, 6) and want_half == "left") or (ori in (3, 8) and want_half == "right")) else out_b
            shift_p1, corr_p1 = _phase_shift_and_corr(p1_half, cam_p1_strip)
            assert shift_p1 == (0, 0), f"pair ({i1},{i2}) {mode_label} p1 shift={shift_p1}"
            assert corr_p1 >= 0.98, f"pair ({i1},{i2}) {mode_label} p1 corr={corr_p1:.5f}"


# --------------------------------------------------------------------------- 验收 3：端到端（渲染、配方/LUT/neutral、导出模式、批量、HDR 拒绝）


def test_e2e_pair_raw_composite_modes_prefs_batch_and_hdr_refusal(
    settings: Any,
    lib: Library,
    volumes: Path,
    tmp_path: Path,
) -> None:
    _clear_desktop_cache()
    card250_pairs = _sorted_pairs(_field_folder("card250"))
    _stage_volume_pairs(volumes, card250_pairs[:6])
    _setup_g250_calibrated_home(settings)
    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    by_idx = _index_shots_by_jpg_sha(lib, card250_pairs[:6])
    assert len(by_idx) == 6

    # HDR 导入衍生任务跳过全部第 2 张
    pending_ids = {s["shot_id"] for s in hdr_derived.pending(lib, lib.list_shots(limit=100))}
    for i2 in (2, 4, 6):
        assert by_idx[i2]["shot_id"] not in pending_ids
    lib.close()

    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        # 1) 每对第 2 张：/render?stage=final 与默认导出都是 RAW 合成（有黑缝、几何正确、与相机 JPG 不同字节）
        for i1, i2, expected_size in ((1, 2, (4288, 2848)), (3, 4, (2848, 4288)), (5, 6, (2848, 4288))):
            sid2 = by_idx[i2]["shot_id"]
            r_final = client.get(f"/api/photos/{sid2}/render?stage=final&size=preview")
            assert r_final.status_code == 200, r_final.text
            with Image.open(io.BytesIO(r_final.content)) as im:
                arr = np.asarray(im.convert("RGB"), dtype=np.uint8)
                ha, seam_core, hb = _seam_core(arr)
                assert int(seam_core.max()) <= hf.LUMA_THRESHOLD and int(ha.max()) > 30 and int(hb.max()) > 30

            exp_dir = tmp_path / f"default_{i2}"
            exp_dir.mkdir()
            r_exp = client.post(
                f"/api/photos/{sid2}/export",
                json={"size": "original", "quality_tier": "high", "dest_dir": str(exp_dir)},
            )
            assert r_exp.status_code == 200, r_exp.text
            out_path = Path(r_exp.json()["path"])
            assert out_path.read_bytes() != card250_pairs[i2 - 1][1].read_bytes()
            with Image.open(out_path) as im:
                assert im.size == expected_size
                arr = np.asarray(im.convert("RGB"), dtype=np.uint8)
                ha, seam_core, hb = _seam_core(arr)
                assert int(seam_core.max()) <= 6 and int(ha.max()) > 30 and int(hb.max()) > 30
                cam_dip = np.asarray(ImageOps.exif_transpose(Image.open(card250_pairs[i2 - 1][1])).convert("RGB"), dtype=np.uint8)
                ca, _, cb = _seam_and_halves(cam_dip)
                sa, corra = _phase_shift_and_corr(ha, ca)
                sb, corrb = _phase_shift_and_corr(hb, cb)
                assert sa == (0, 0) and sb == (0, 0) and corra >= 0.98 and corrb >= 0.98

        # 2) 用户自选配方、Log LUT、neutral、LUT 预览、edited_jpeg 缩略图、配方卡 POST /render 都走 RAW 合成
        sid2 = by_idx[2]["shot_id"]
        default_prev = np.asarray(
            Image.open(io.BytesIO(client.get(f"/api/photos/{sid2}/render?stage=final&size=preview").content)).convert("RGB"),
            dtype=np.float32,
        )
        photo_unit = client.get(f"/api/photos/{sid2}/gpu").json().get("unit") or "qlaunch"
        user_recipe = {"kind": "builtin", "unit": photo_unit, "slot": 4, "levels": {}}
        r_user = client.post(f"/api/photos/{sid2}/render?stage=final&size=preview", json={"recipe": user_recipe})
        assert r_user.status_code == 200, r_user.text
        with Image.open(io.BytesIO(r_user.content)) as im:
            user_arr = np.asarray(im.convert("RGB"), dtype=np.uint8)
            ha, seam_core, hb = _seam_core(user_arr)
            assert int(seam_core.max()) <= hf.LUMA_THRESHOLD and int(ha.max()) > 30 and int(hb.max()) > 30
            assert user_arr.shape == default_prev.shape
            assert float(np.mean(np.abs(user_arr.astype(np.float32) - default_prev))) > 3.0

        r_neu = client.post(
            f"/api/photos/{sid2}/export",
            json={"source": "neutral", "size": "small", "dest_dir": str(tmp_path / "neu")},
        )
        assert r_neu.status_code == 200, r_neu.text
        with Image.open(r_neu.json()["path"]) as im:
            assert max(im.size) == 1600
            ha, seam_core, hb = _seam_core(np.asarray(im.convert("RGB"), dtype=np.uint8))
            assert int(seam_core.max()) <= hf.LUMA_THRESHOLD and int(ha.max()) > 30 and int(hb.max()) > 30

        up = client.post(
            "/api/lut/upload",
            content=cube_bytes(lambda x: x, size=2),
            headers={"X-GXR-Filename": "log_identity.cube"},
        )
        assert up.status_code == 200, up.text
        cube_id = up.json()["cube_id"]
        assert client.patch(f"/api/luts/{cube_id}", json={"input_space": "llog"}).status_code == 200

        r_log_render = client.post(f"/api/photos/{sid2}/render?stage=final&size=preview", json={"lut": cube_id})
        assert r_log_render.status_code == 200, r_log_render.text
        with Image.open(io.BytesIO(r_log_render.content)) as im:
            ha, seam_core, hb = _seam_core(np.asarray(im.convert("RGB"), dtype=np.uint8))
            assert int(seam_core.max()) <= hf.LUMA_THRESHOLD and int(ha.max()) > 30 and int(hb.max()) > 30

        r_lut_prev = client.get(f"/api/lut/{cube_id}/preview", params={"which": "lut", "photo": sid2})
        assert r_lut_prev.status_code == 200, r_lut_prev.text
        with Image.open(io.BytesIO(r_lut_prev.content)) as im:
            ha, seam_core, hb = _seam_core(np.asarray(im.convert("RGB"), dtype=np.uint8))
            assert int(seam_core.max()) <= hf.LUMA_THRESHOLD and int(ha.max()) > 25 and int(hb.max()) > 25

        assert client.put(f"/api/photos/{sid2}/edit", json={"recipe": user_recipe, "lut": None}).status_code == 200
        r_thumb = client.get(f"/api/photos/{sid2}/thumb")
        assert r_thumb.status_code == 200
        with Image.open(io.BytesIO(r_thumb.content)) as im:
            ha, seam_core, hb = _seam_core(np.asarray(im.convert("RGB"), dtype=np.uint8))
            assert int(seam_core.max()) <= hf.LUMA_THRESHOLD and int(ha.max()) > 20 and int(hb.max()) > 20
        assert client.put(f"/api/photos/{sid2}/edit", json={"recipe": None}).status_code == 200

        # 3) 导出模式：singles（2 个 _gxr_half，2088×2848 / 2848×2088 + camera-jpg 来源）、both（3 个）、记忆偏好与尺寸档
        r_singles = client.post(
            f"/api/photos/{sid2}/export",
            json={"halfframe": "singles", "size": "original", "dest_dir": str(tmp_path / "singles_land")},
        )
        assert r_singles.status_code == 200, r_singles.text
        s_files = r_singles.json()["files"]
        assert len(s_files) == 2
        single_stems = set()
        for item in s_files:
            p = Path(item["path"])
            assert p.name.endswith("_gxr_half.jpg")
            single_stems.add(p.stem)
            with Image.open(p) as im:
                assert im.size == (2088, 2848)
                assert len(im.getexif()) > 0
        assert len(single_stems) == 2

        # 竖构图 pair (3,4) singles 应为 2848×2088，小档 small 长边 1600
        sid4 = by_idx[4]["shot_id"]
        r_singles_port = client.post(
            f"/api/photos/{sid4}/export",
            json={"halfframe": "singles", "size": "original", "dest_dir": str(tmp_path / "singles_port")},
        )
        assert r_singles_port.status_code == 200, r_singles_port.text
        for item in r_singles_port.json()["files"]:
            with Image.open(item["path"]) as im:
                assert im.size == (2848, 2088)

        r_singles_cam = client.post(
            f"/api/photos/{sid2}/export",
            json={"source": "camera-jpg", "halfframe": "singles", "size": "medium", "dest_dir": str(tmp_path / "singles_cam")},
        )
        assert r_singles_cam.status_code == 200, r_singles_cam.text
        assert len(r_singles_cam.json()["files"]) == 2
        for item in r_singles_cam.json()["files"]:
            with Image.open(item["path"]) as im:
                assert max(im.size) == 2048

        r_both = client.post(
            f"/api/photos/{sid2}/export",
            json={"halfframe": "both", "size": "small", "dest_dir": str(tmp_path / "both_small")},
        )
        assert r_both.status_code == 200, r_both.text
        b_files = r_both.json()["files"]
        assert len(b_files) == 3
        for item in b_files:
            with Image.open(item["path"]) as im:
                assert max(im.size) == 1600

        # 记忆偏好：PUT /api/prefs export_halfframe="singles" 后，不传 halfframe 时自动走 singles
        assert client.put("/api/prefs", json={"export_halfframe": "singles"}).status_code == 200
        r_pref = client.post(
            f"/api/photos/{sid2}/export",
            json={"size": "small", "dest_dir": str(tmp_path / "pref_singles")},
        )
        assert r_pref.status_code == 200, r_pref.text
        assert len(r_pref.json()["files"]) == 2
        assert client.put("/api/prefs", json={"export_halfframe": "diptych"}).status_code == 200

        # 4) 按日期批量导出：3 对共 6 张 → 去重第 1 张，只导 3 对并上报 pairs: 3
        batch_dir = tmp_path / "batch_diptych"
        batch_dir.mkdir()
        job = client.post(
            "/api/export",
            json={"date": by_idx[2]["date"], "size": "small", "dest_dir": str(batch_dir)},
        )
        assert job.status_code == 202, job.text
        done = _poll_job(client, job.json()["id"])
        prog = done["progress"]
        assert prog["state"] == "done", done
        assert prog["pairs"] == 3
        assert prog["done"] == 3 and prog["total"] == 3
        exported_ids = {item["id"] for item in prog["results"]}
        assert exported_ids == {by_idx[2]["shot_id"], by_idx[4]["shot_id"], by_idx[6]["shot_id"]}

        # 5) HDR 导出：半格一对退回 SDR 并附带指定提示
        r_hdr = client.post(
            f"/api/photos/{sid2}/export",
            json={"hdr": True, "size": "small", "dest_dir": str(tmp_path / "hdr_refuse")},
        )
        assert r_hdr.status_code == 200, r_hdr.text
        hdr_body = r_hdr.json()
        assert hdr_body["hdr"] is False
        assert hdr_body["hdr_note"] == "半格一对暂时做不了 HDR"


# --------------------------------------------------------------------------- 验收 4：非半格第 2 张导出与 1e8e9ac 逐字节一致


def test_non_pair_second_exports_byte_identical_to_base_1e8e9ac(
    settings: Any,
    lib: Library,
    volumes: Path,
    tmp_path: Path,
) -> None:
    _clear_desktop_cache()
    pairs250 = _sorted_pairs(_field_folder("card250"))
    pairs249 = _sorted_pairs(_field_folder("card249"))
    selected = [
        ("card250_1", pairs250[0]),
        ("card250_3", pairs250[2]),
        ("card250_5", pairs250[4]),
        ("card250_7", pairs250[6]),
        ("card250_11_xpan", pairs250[10]),
        ("card249_5_ordinary", pairs249[4]),
        ("card249_2_legacy_diptych", pairs249[1]),
    ]
    _stage_volume_pairs(volumes, [p for _, p in selected])
    _setup_g250_calibrated_home(settings)
    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    by_sha = {s["files"]["jpg"]["sha256"]: s for s in lib.list_shots(limit=100)}
    assert len(by_sha) == len(selected)
    lib.close()

    icc_bytes = expmod.srgb_icc()
    icc_path = tmp_path / "srgb.icc"
    icc_path.write_bytes(icc_bytes)
    head_exports: dict[str, bytes] = {}
    shot_ids: dict[str, str] = {}

    orig_icc = expmod._ICC
    expmod._ICC = icc_bytes
    try:
        with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
            for label, (_dng, jpg_path) in selected:
                sha = hashlib.sha256(jpg_path.read_bytes()).hexdigest()
                sid = by_sha[sha]["shot_id"]
                shot_ids[label] = sid
                exp_dir = tmp_path / f"head_{label}"
                exp_dir.mkdir()
                resp = client.post(
                    f"/api/photos/{sid}/export",
                    json={"size": "original", "quality_tier": "high", "dest_dir": str(exp_dir)},
                )
                assert resp.status_code == 200, f"{label}: {resp.text}"
                head_exports[label] = Path(resp.json()["path"]).read_bytes()
    finally:
        expmod._ICC = orig_icc

    has_base = subprocess.run(["git", "cat-file", "-e", f"{BASE_COMMIT}^{{commit}}"], cwd=REPO, check=False, capture_output=True)
    assert has_base.returncode == 0, f"git commit {BASE_COMMIT} must be reachable"
    base_dir = tmp_path / f"base_{BASE_COMMIT}"
    base_dir.mkdir()
    tar = subprocess.run(["git", "archive", BASE_COMMIT, "src"], cwd=REPO, capture_output=True, check=True)
    subprocess.run(["tar", "-xf", "-", "-C", str(base_dir)], input=tar.stdout, check=True)

    worker = tmp_path / "export_base.py"
    worker.write_text(
        """
import json
import sys
from pathlib import Path

src_root = Path(sys.argv[1])
home_dir = Path(sys.argv[2])
lib_dir = Path(sys.argv[3])
export_dir = Path(sys.argv[4])
vol_dir = Path(sys.argv[5])
out_dir = Path(sys.argv[6])
icc_bytes = Path(sys.argv[7]).read_bytes()
port = int(sys.argv[8])
token = sys.argv[9]
shot_ids = json.loads(sys.argv[10])

sys.meta_path = [f for f in sys.meta_path if "__editable" not in repr(f) and "gxr_studio" not in repr(f)]
sys.path.insert(0, str(src_root))

import gxr_studio
assert Path(gxr_studio.__file__).resolve().is_relative_to(src_root.resolve()), gxr_studio.__file__

from fastapi.testclient import TestClient
from gxr_studio import export as expmod
from gxr_studio.app import create_app
from gxr_studio.config import Settings

expmod._ICC = icc_bytes
cfg = Settings(
    home_dir=home_dir,
    library_dir=lib_dir,
    export_dir=export_dir,
    volumes_roots=(vol_dir,),
    port=port,
    token=token,
    min_free_bytes=0,
)
paths: dict[str, str] = {}
with TestClient(create_app(cfg), base_url=f"http://127.0.0.1:{port}", headers={"X-GXR-Token": token}) as client:
    for label, sid in shot_ids.items():
        d = out_dir / label
        d.mkdir(parents=True, exist_ok=True)
        resp = client.post(
            f"/api/photos/{sid}/export",
            json={"size": "original", "quality_tier": "high", "dest_dir": str(d)},
        )
        assert resp.status_code == 200, (label, resp.text)
        paths[label] = resp.json()["path"]
print(json.dumps(paths))
""".strip()
        + "\n",
        encoding="utf-8",
    )
    out_base = tmp_path / "out_base"
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            str(worker),
            str(base_dir / "src"),
            str(settings.home_dir),
            str(settings.library_dir),
            str(settings.export_dir),
            str(volumes),
            str(out_base),
            str(icc_path),
            str(PORT),
            settings.token,
            json.dumps(shot_ids),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    base_paths = json.loads(proc.stdout.splitlines()[-1])
    for label, _ in selected:
        base_bytes = Path(base_paths[label]).read_bytes()
        assert head_exports[label] == base_bytes, f"{label} exported bytes diverged from {BASE_COMMIT}"


# --------------------------------------------------------------------------- r1 修复：编辑门禁、粘贴配方+Log LUT、本机配方


def test_pair_editor_gates_paste_and_local_recipe(
    settings: Any,
    lib: Library,
    volumes: Path,
) -> None:
    from gxr_studio.render import edits as render_edits
    from gxr_studio.render.params import RecipeSel

    _clear_desktop_cache()
    pairs = _sorted_pairs(_field_folder("card250"))
    staged = [pairs[0], pairs[1], pairs[6]]  # pair (1,2) + 普通 RAW+JPG 第 7 张（作粘贴源）
    _stage_volume_pairs(volumes, staged)
    _setup_g250_calibrated_home(settings)
    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    by_idx = _index_shots_by_jpg_sha(lib, staged)
    sid2 = by_idx[2]["shot_id"]
    sid_src = by_idx[3]["shot_id"]
    assert by_idx[2].get("pair", {}).get("role") == "second"
    lib.close()

    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        # 1) API 门禁：capabilities / edit.render / gpu / PUT Log LUT
        caps = client.get(f"/api/photos/{sid2}/capabilities")
        assert caps.status_code == 200, caps.text
        assert caps.json() == {"raw": True, "recipe": "full", "reason": ""}, caps.text

        ed0 = client.get(f"/api/photos/{sid2}/edit")
        assert ed0.status_code == 200, ed0.text
        rinfo = ed0.json()["render"]
        assert rinfo["available"] is True and rinfo.get("prediction") is True and rinfo.get("reason") is None, rinfo
        unit = rinfo["unit"]
        assert isinstance(unit, str) and unit, rinfo

        gpu0 = client.get(f"/api/photos/{sid2}/gpu")
        g = gpu0.json()
        assert gpu0.status_code == 200 and g["enabled"] is True and g["raw"] is None, gpu0.text
        pair = g["pair"]
        geo = hfc.proxy_geometry(pair["window"]["width"])
        assert pair["first_half"] == by_idx[2]["pair"]["first_half"] and pair["orientation"] == by_idx[2]["orientation"]
        assert pair["window"] == {
            "width": geo.width,
            "height": pair["window"]["height"],
            "half_w": geo.half_w,
            "strip_x0": geo.strip_x0,
            "seam_x0": geo.seam_x0,
            "seam_w": geo.seam_w,
            "right_x0": geo.right_x0,
        }
        assert [s["half"] for s in pair["sources"]] == [0, 1] and all(f"raw?half={s['half']}&" in s["raw"]["url"] for s in pair["sources"])

        up = client.post(
            "/api/lut/upload",
            content=cube_bytes(lambda x: np.clip(x * np.array([1.15, 0.85, 0.72]) + 0.04, 0.0, 1.0), size=9),
            headers={"X-GXR-Filename": "pair-log.cube"},
        )
        assert up.status_code == 200, up.text
        log_lut = up.json()["cube_id"]
        assert client.patch(f"/api/luts/{log_lut}", json={"input_space": "llog"}).status_code == 200

        put_lut = client.put(f"/api/photos/{sid2}/edit", json={"lut": log_lut})
        assert put_lut.status_code == 200, put_lut.text
        assert client.put(f"/api/photos/{sid2}/edit", json={"lut": None}).status_code == 200

        # 2) 未改合成基准图
        r_base = client.get(f"/api/photos/{sid2}/render?stage=final")
        assert r_base.status_code == 200, r_base.text
        with Image.open(io.BytesIO(r_base.content)) as im:
            ha0, core0, hb0 = _seam_core(np.asarray(im.convert("RGB")))
        assert int(core0.max()) <= 3

        # 3) 从普通 RAW+JPG 源粘贴 配方 + Log LUT 到半格一对，两半均应用且画面改变
        cat = client.get("/api/recipes").json()
        unit_cat = next(u for u in cat["units"] if u["id"] == unit)
        target_slot = next(r["slot"] for r in unit_cat["recipes"] if r["slot"] != rinfo.get("slot"))
        src_recipe = {"kind": "builtin", "unit": unit, "slot": target_slot, "levels": {}}
        assert client.put(f"/api/photos/{sid_src}/edit", json={"recipe": src_recipe, "lut": log_lut}).status_code == 200

        pasted = client.post(
            "/api/edits/paste",
            json={"groups": ["recipe"], "source_id": sid_src, "targets": [{"id": sid2}]},
        )
        assert pasted.status_code == 200, pasted.text
        p_body = pasted.json()
        assert p_body["counts"]["applied"] == 1 and p_body["results"][0]["skipped"] == [], p_body

        r_after = client.get(f"/api/photos/{sid2}/render?stage=final")
        assert r_after.status_code == 200, r_after.text
        with Image.open(io.BytesIO(r_after.content)) as im:
            ha1, core1, hb1 = _seam_core(np.asarray(im.convert("RGB")))
        assert int(core1.max()) <= 3
        assert float(np.abs(ha1.astype(np.float32) - ha0.astype(np.float32)).mean()) > 2.0
        assert float(np.abs(hb1.astype(np.float32) - hb0.astype(np.float32)).mean()) > 2.0

        # 4) 完整本机配方校验接受半格一对（不因缺独立 DNG 报 local_recipe_needs_pair）
        local_sel = {"kind": "local", "unit": unit, "slot": 1, "id": "a" * 64, "levels": {}}
        r_local = client.put(f"/api/photos/{sid2}/edit", json={"recipe": local_sel})
        assert r_local.json().get("error") != "local_recipe_needs_pair", r_local.text

        class _StubRecipes:
            def resolve(self, sel: Any, *, photo_unit: str | None, iso: int) -> object:
                return object()

        ctx = client.app.state.ctx
        shot2 = ctx.lib.get_shot(sid2)
        assert shot2 is not None
        render_edits.validate_recipe(
            RecipeSel(kind="local", unit=unit, slot=1, id="a" * 64),
            shot2,
            ctx.render.menu,
            _StubRecipes(),  # type: ignore[arg-type]
        )
