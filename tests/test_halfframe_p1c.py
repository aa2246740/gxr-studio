"""halfframe-p1c 合成与单元测试：pair RAW luma、衍生 HDR、导出 HDR（diptych / singles / both）、损坏半格与纯 JPG 第 1 张拒绝。"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

import hdr_support as hs
from gxr_studio import halfframe_compose as hfc
from gxr_studio import halfframe_export as hfe
from gxr_studio.api.routes import HDR_NOTES, HDR_UNSUPPORTED
from gxr_studio.finish import serve as fserve
from gxr_studio.finish.hdr_export import HdrTrack
from gxr_studio.finish.spec import parse_frame
from gxr_studio.hdr import derived
from gxr_studio.hdr import gain as g
from gxr_studio.library.store import Library
from gxr_studio.raw import open_dng
from gxr_studio.render.params import EditState
from gxr_studio.render.service import RenderService
from raw_support import bayer_scene, build_dng

SYNTH_H = 384
SYNTH_W = 576


def _synth_dng(rgb: np.ndarray, *, orientation: int = 1, when: str = "2026:10:31 10:00:00") -> bytes:
    cam = rgb * np.array(hs.NEUTRAL)
    return build_dng(
        bayer_scene(*rgb.shape[:2], cam),
        bps=16,
        white=4095,
        neutral=hs.NEUTRAL,
        orientation=orientation,
        when=when,
        root_extra=[(272, "ASCII", "GXR")],
    )


def _camera_jpeg(rgb: np.ndarray, *, orientation: int = 1, model: str = "GXR", full_size: bool = False) -> bytes:
    exif = Image.Exif()
    exif[274] = orientation
    exif[271] = "RICOH"
    exif[272] = model
    im = Image.fromarray(hs.srgb_bytes(rgb * hs.EXPOSURE), "RGB")
    if full_size and im.size != (hfc.FULL_WIDTH, hfc.FULL_HEIGHT):
        im = im.resize((hfc.FULL_WIDTH, hfc.FULL_HEIGHT), Image.Resampling.BILINEAR)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92, exif=exif)
    return buf.getvalue()


def _synth_pair_bytes(
    *,
    orientation: int = 1,
    first_half: str = "left",
    seed1: int = 10,
    seed2: int = 20,
    dng1_rgb: np.ndarray | None = None,
    dng2_rgb: np.ndarray | None = None,
    jpg1_model: str = "GXR",
    jpg2_model: str = "GXR",
    full_camera_jpg: bool = False,
) -> tuple[bytes, bytes, bytes, bytes]:
    """返回 (jpg1, dng1, diptych_jpg2, dng2)，两半各自带独立高亮余量与相机型号标签。"""
    geo = hfc.proxy_geometry(SYNTH_W)
    x0, x1 = geo.strip_x0, geo.strip_x0 + geo.half_w
    s1 = hs.scene(SYNTH_H, geo.half_w, seed=seed1 + 100)
    s2 = hs.scene(SYNTH_H, geo.half_w, seed=seed2 + 100)
    rgb1 = np.empty((SYNTH_H, SYNTH_W, 3), dtype=np.float64)
    rgb2 = np.empty((SYNTH_H, SYNTH_W, 3), dtype=np.float64)
    for dst, strip in ((rgb1, s1), (rgb2, s2)):
        dst[:, x0:x1] = strip
        dst[:, :x0] = strip[:, :1]
        dst[:, x1:] = strip[:, -1:]
    dip = np.zeros((SYNTH_H, SYNTH_W, 3), dtype=np.float64)
    if first_half == "left":
        dip[:, : geo.half_w] = s1
        dip[:, geo.right_x0 :] = s2
    else:
        dip[:, : geo.half_w] = s2
        dip[:, geo.right_x0 :] = s1
    jpg1 = _camera_jpeg(rgb1, orientation=orientation, model=jpg1_model, full_size=full_camera_jpg)
    jpg2 = _camera_jpeg(dip, orientation=orientation, model=jpg2_model, full_size=full_camera_jpg)
    dng1 = _synth_dng(rgb1 if dng1_rgb is None else dng1_rgb, orientation=orientation)
    dng2 = _synth_dng(rgb2 if dng2_rgb is None else dng2_rgb, orientation=orientation)
    return jpg1, dng1, jpg2, dng2


def _stage_pair_shot(
    lib: Library,
    *,
    tag: str,
    orientation: int = 1,
    first_half: str = "left",
    first_has_dng: bool = True,
    dng1_bytes: bytes | None = None,
    dng2_bytes: bytes | None = None,
    dng1_rgb: np.ndarray | None = None,
    dng2_rgb: np.ndarray | None = None,
    full_camera_jpg: bool = False,
) -> tuple[dict[str, Any], bytes, bytes]:
    jpg1, dng1, jpg2, dng2 = _synth_pair_bytes(
        orientation=orientation,
        first_half=first_half,
        dng1_rgb=dng1_rgb,
        dng2_rgb=dng2_rgb,
        full_camera_jpg=full_camera_jpg,
    )
    if dng1_bytes is not None:
        dng1 = dng1_bytes
    if dng2_bytes is not None:
        dng2 = dng2_bytes

    folder = lib.root / "2026" / f"2026-10-{tag}"
    folder.mkdir(parents=True, exist_ok=True)
    rel_j1 = f"2026/2026-10-{tag}/P0001.JPG"
    rel_d1 = f"2026/2026-10-{tag}/P0001.DNG"
    rel_j2 = f"2026/2026-10-{tag}/P0002.JPG"
    rel_d2 = f"2026/2026-10-{tag}/P0002.DNG"
    lib.abspath(rel_j1).write_bytes(jpg1)
    lib.abspath(rel_j2).write_bytes(jpg2)
    lib.abspath(rel_d2).write_bytes(dng2)
    f1: dict[str, Any] = {"jpg": {"kind": "jpg", "relpath": rel_j1, "sha256": f"j1{tag}" * 10}}
    if first_has_dng:
        lib.abspath(rel_d1).write_bytes(dng1)
        f1["dng"] = {"kind": "dng", "relpath": rel_d1, "sha256": f"d1{tag}" * 10}
    f2: dict[str, Any] = {
        "jpg": {"kind": "jpg", "relpath": rel_j2, "sha256": f"j2{tag}" * 10},
        "dng": {"kind": "dng", "relpath": rel_d2, "sha256": f"d2{tag}" * 10},
    }
    base_w, base_h = (hfc.FULL_WIDTH, hfc.FULL_HEIGHT) if full_camera_jpg else (SYNTH_W, SYNTH_H)
    w, h = (base_h, base_w) if orientation in (6, 8) else (base_w, base_h)
    sid1 = f"11{tag}" * 16
    sid2 = f"22{tag}" * 16
    src1 = {"shot_id": sid1, "name": f"P{tag}01", "orientation": orientation, "width": w, "height": h, "all_files": f1}
    src2 = {"shot_id": sid2, "name": f"P{tag}02", "orientation": orientation, "width": w, "height": h, "all_files": f2}
    shot2: dict[str, Any] = {
        "shot_id": sid2,
        "name": f"P{tag}02",
        "orientation": orientation,
        "width": w,
        "height": h,
        "diptych": True,
        "has_raw": 1 if first_has_dng else 0,
        "files": {"jpg": dict(f2["jpg"])},  # P0 守则：第 2 张 files 不暴露自有 dng
        "all_files": f2,
        "pair": {"partner_id": sid1, "role": "second", "first_half": first_half, "flat": False, "kind": "g250"},
        "pair_sources": {"first": src1, "second": src2},
    }
    return shot2, jpg1, jpg2


def _seam_slice(arr: np.ndarray, orientation: int) -> np.ndarray:
    """按共享几何取出已转正数组（2D 或 3D）上的接缝区域。"""
    inv = 8 if orientation == 6 else (6 if orientation == 8 else orientation)
    sensor = hfc.orient_array(arr, inv)
    geo = hfc.proxy_geometry(sensor.shape[1])
    return sensor[:, geo.seam_x0 : geo.right_x0]


@pytest.mark.parametrize("orientation", [1, 3, 6, 8])
@pytest.mark.parametrize("first_half", ["left", "right"])
def test_pair_raw_luma_geometry_seam_nan_and_derived_hdr(orientation: int, first_half: str) -> None:
    """pair_raw_luma 复用 halfframe_compose 几何：接缝为 NaN（非 0），转正后接缝增益严格 0 档，高光有明显增益。"""
    jpg1, dng1, jpg2, dng2 = _synth_pair_bytes(orientation=orientation, first_half=first_half)
    raw = g.pair_raw_luma(
        open_dng(dng1),
        open_dng(dng2),
        first_half=first_half,
        orientation=orientation,
        first_jpeg=jpg1,
    )
    seam_y = _seam_slice(raw.y, orientation)
    seam_c = _seam_slice(raw.clip, orientation)
    assert np.isnan(seam_y).all(), "接缝列必须全部标为 NaN，不能填 0"
    assert not seam_c.any()
    h1, h2 = hfc.split_diptych_halves(raw.y, first_half=first_half, orientation=orientation)
    assert np.isfinite(h1).all() and np.isfinite(h2).all()

    out, stats = g.hdr_from_camera_jpeg(jpg2, raw)
    assert stats["alignment"]["shift"] == [0, 0]
    assert stats["alignment"]["correlation"] > 0.9
    assert stats["gain_max_stops"] >= 1.0
    checked = hs.check_file(jpg2, out)
    assert checked["ok"], checked["fail"]

    # 编码前 compute_gain 的接缝增益严格为 0 档（转正后核对）
    rgb = g.decode_rgb(jpg2)
    sdr_y = g.sdr_luma_grid(rgb, g.Params().map_factor)
    stored = g.RawLuma(g.to_storage(raw.y, orientation), g.to_storage(raw.clip, orientation))
    al = g.align(sdr_y, stored.y)
    g.check_alignment(al)
    ry, rc = g.register(stored, sdr_y.shape, al, sdr_y.shape)
    gain_res = g.compute_gain(sdr_y, ry, rc, g.Params())
    oriented_delta = hfc.orient_array(gain_res.delta, orientation)
    assert float(_seam_slice(oriented_delta, orientation).max()) == 0.0
    assert float(gain_res.delta.max()) >= 1.0


def test_pair_raw_luma_checks_each_half_capture_metadata() -> None:
    """两半分别用 check_capture 核对各自 DNG 与各自 JPG；任一半拍摄信息不符即报 capture_mismatch。"""
    jpg1_bad, dng1, jpg2, dng2 = _synth_pair_bytes(jpg1_model="OTHER")
    with pytest.raises(g.HdrUnsupported) as err1:
        g.pair_raw_luma(open_dng(dng1), open_dng(dng2), first_half="left", orientation=1, first_jpeg=jpg1_bad)
    assert err1.value.reason == "capture_mismatch"

    jpg1, dng1, jpg2_bad, dng2 = _synth_pair_bytes(jpg2_model="OTHER")
    raw = g.pair_raw_luma(open_dng(dng1), open_dng(dng2), first_half="left", orientation=1, first_jpeg=jpg1)
    with pytest.raises(g.HdrUnsupported) as err2:
        g.hdr_from_camera_jpeg(jpg2_bad, raw)
    assert err2.value.reason == "capture_mismatch"


def test_derived_hdr_accepts_pair_with_pair_raw_and_refuses_jpeg_only_photo1(lib: Library) -> None:
    """derived.eligible / generate 接受带双 DNG 的配对第 2 张，拒绝第 1 张缺 DNG 的配对。"""
    shot_ok, _j1, jpg2 = _stage_pair_shot(lib, tag="01", orientation=8, first_half="right", first_has_dng=True)
    assert derived.eligible(shot_ok) is True
    assert derived.status_of(lib, shot_ok) == derived.PENDING
    assert derived.generate(lib, shot_ok) == derived.READY
    assert derived.status_of(lib, shot_ok) == derived.READY
    hdr_bytes = derived.hdr_path(lib, shot_ok["shot_id"], pair=True).read_bytes()
    checked = hs.check_file(jpg2, hdr_bytes)
    assert checked["ok"], checked["fail"]
    assert checked["meta"]["GainMapMax"] >= 1.0

    shot_no_raw, _j1b, _j2b = _stage_pair_shot(lib, tag="02", orientation=1, first_half="left", first_has_dng=False)
    assert derived.eligible(shot_no_raw) is False
    assert derived.status_of(lib, shot_no_raw) == derived.NONE


@pytest.mark.parametrize("orientation,first_half", [(1, "left"), (6, "left"), (8, "right")])
def test_export_hdr_diptych_singles_both_and_gain_slice_parity(
    settings: Any,
    lib: Library,
    tmp_path: Path,
    orientation: int,
    first_half: str,
) -> None:
    """diptych / singles / both 导出 HDR：主图与 hdr=False 逐字节一致，单格增益图等于双联对应半格切片。"""
    shot2, _j1, _j2 = _stage_pair_shot(lib, tag=f"{orientation}{first_half[0]}", orientation=orientation, first_half=first_half)
    render = RenderService(settings.home_dir, lib)

    from gxr_studio.render.params import Adjust

    edit = EditState(adjust=Adjust(exposure_ev=1.5))

    # 1) diptych (export_finish)
    sdr_dip = fserve.export_finish(
        render, lib, shot2, edit, {}, tmp_path / "dip_sdr", [], quality=92, long_edge=None, fmt="jpeg", base="neutral", hdr=False
    )
    hdr_dip = fserve.export_finish(
        render, lib, shot2, edit, {}, tmp_path / "dip_hdr", [], quality=92, long_edge=None, fmt="jpeg", base="neutral", hdr=True
    )
    assert hdr_dip.hdr is True and hdr_dip.hdr_reason is None
    assert hdr_dip.path.name.endswith("_hdr.jpg")
    dip_check = hs.check_file(sdr_dip.path.read_bytes(), hdr_dip.path.read_bytes())
    assert dip_check["ok"], dip_check["fail"]
    assert dip_check["meta"]["GainMapMax"] >= 0.5

    # 2) singles (export_pair_finish mode="singles")
    sdr_singles = hfe.export_pair_finish(
        render, lib, shot2, edit, {}, tmp_path / "s_sdr", [], quality=92, long_edge=None, fmt="jpeg", base="neutral", hdr=False, mode="singles"
    )
    hdr_singles = hfe.export_pair_finish(
        render, lib, shot2, edit, {}, tmp_path / "s_hdr", [], quality=92, long_edge=None, fmt="jpeg", base="neutral", hdr=True, mode="singles"
    )
    assert len(hdr_singles) == 2
    s_checks: dict[str, dict[str, Any]] = {}
    for (_s1, role_s, r_sdr), (_s2, role_h, r_hdr) in zip(sdr_singles, hdr_singles, strict=True):
        assert role_s == role_h
        assert r_hdr.hdr is True and r_hdr.hdr_reason is None
        assert r_hdr.path.name.endswith("_half_hdr.jpg")
        chk = hs.check_file(r_sdr.path.read_bytes(), r_hdr.path.read_bytes())
        assert chk["ok"], chk["fail"]
        s_checks[role_h] = chk

    # 单格增益图应与双联增益图对应半格切片一致（避开接缝边缘 2 格的平滑/JPEG 振铃）
    dip_h1, dip_h2 = hfc.split_diptych_halves(dip_check["log_gain"], first_half=first_half, orientation=orientation)
    for role, dip_slice in (("first", dip_h1), ("second", dip_h2)):
        s_gain = s_checks[role]["log_gain"]
        assert s_gain.shape == dip_slice.shape
        inner_s = s_gain[2:-2, 2:-2]
        inner_d = dip_slice[2:-2, 2:-2]
        diff = np.abs(inner_s - inner_d)
        assert float(np.median(diff)) < 0.08 and float(np.percentile(diff, 95)) < 0.25, (
            role,
            float(np.median(diff)),
            float(np.percentile(diff, 95)),
        )

    # 3) both (export_pair_finish mode="both")
    sdr_both = hfe.export_pair_finish(
        render, lib, shot2, edit, {}, tmp_path / "b_sdr", [], quality=92, long_edge=None, fmt="jpeg", base="neutral", hdr=False, mode="both"
    )
    hdr_both = hfe.export_pair_finish(
        render, lib, shot2, edit, {}, tmp_path / "b_hdr", [], quality=92, long_edge=None, fmt="jpeg", base="neutral", hdr=True, mode="both"
    )
    assert len(hdr_both) == 3
    for (_s1, role_s, r_sdr), (_s2, role_h, r_hdr) in zip(sdr_both, hdr_both, strict=True):
        assert role_s == role_h
        assert r_hdr.hdr is True and r_hdr.hdr_reason is None
        assert r_hdr.path.name.endswith("_hdr.jpg")
        chk = hs.check_file(r_sdr.path.read_bytes(), r_hdr.path.read_bytes())
        assert chk["ok"], chk["fail"]


@pytest.mark.parametrize("bad_half", ["first", "second"])
def test_corrupted_half_dng_luma_falls_back_to_sdr_without_gainmap(
    settings: Any,
    lib: Library,
    tmp_path: Path,
    bad_half: str,
) -> None:
    """验收 3（负例）：损坏任一半 DNG 亮度（错位画面），衍生 HDR 标 unsupported、导出回退 SDR 且不写增益图。"""
    wrong = np.roll(hs.scene(SYNTH_H, SYNTH_W, seed=99), 120, axis=1)
    shot2, _j1, _j2 = _stage_pair_shot(
        lib,
        tag=f"bad_{bad_half[0]}",
        orientation=1,
        first_half="left",
        dng1_rgb=wrong if bad_half == "first" else None,
        dng2_rgb=wrong if bad_half == "second" else None,
        full_camera_jpg=True,
    )
    # 1) 衍生 HDR -> unsupported (not_aligned)，绝不写 .jpg
    assert derived.generate(lib, shot2) == derived.UNSUPPORTED
    assert not derived.hdr_path(lib, shot2["shot_id"], pair=True).exists()
    reason = json.loads(derived.status_path(lib, shot2["shot_id"], pair=True).read_text(encoding="utf-8"))
    assert reason["status"] == derived.UNSUPPORTED and reason["reason"] == "not_aligned" and reason.get("pair") is True

    # 2) 导出 HDR (diptych / singles / both) -> 全部回退 SDR，带原因，无增益图，字节与 hdr=False 一致
    render = RenderService(settings.home_dir, lib)
    sdr_dip = fserve.export_finish(
        render,
        lib,
        shot2,
        EditState(),
        {},
        tmp_path / f"{bad_half}_dip_sdr",
        [],
        quality=92,
        long_edge=None,
        fmt="jpeg",
        base="camera-jpg",
        hdr=False,
    )
    hdr_dip = fserve.export_finish(
        render,
        lib,
        shot2,
        EditState(),
        {},
        tmp_path / f"{bad_half}_dip_hdr",
        [],
        quality=92,
        long_edge=None,
        fmt="jpeg",
        base="camera-jpg",
        hdr=True,
    )
    assert hdr_dip.hdr is False and hdr_dip.hdr_reason == "not_aligned"
    assert not hdr_dip.path.name.endswith("_hdr.jpg")
    assert hdr_dip.path.read_bytes() == sdr_dip.path.read_bytes()
    assert b"urn:iso:std:iso:ts:21496:-1" not in hdr_dip.path.read_bytes()
    dip_dict = hfe.export_result_dict(shot2["shot_id"], hdr_dip, hdr_notes=HDR_NOTES, hdr_unsupported=HDR_UNSUPPORTED)
    assert dip_dict["hdr"] is False and dip_dict["hdr_note"] == HDR_UNSUPPORTED

    for mode in ("singles", "both"):
        sdr_entries = hfe.export_pair_finish(
            render,
            lib,
            shot2,
            EditState(),
            {},
            tmp_path / f"{bad_half}_{mode}_sdr",
            [],
            quality=92,
            long_edge=None,
            fmt="jpeg",
            base="camera-jpg",
            hdr=False,
            mode=mode,
        )
        hdr_entries = hfe.export_pair_finish(
            render,
            lib,
            shot2,
            EditState(),
            {},
            tmp_path / f"{bad_half}_{mode}_hdr",
            [],
            quality=92,
            long_edge=None,
            fmt="jpeg",
            base="camera-jpg",
            hdr=True,
            mode=mode,
        )
        for (_a, _ra, rs), (_b, _rb, rh) in zip(sdr_entries, hdr_entries, strict=True):
            assert rh.hdr is False and rh.hdr_reason == "not_aligned"
            assert not rh.path.name.endswith("_hdr.jpg")
            assert rh.path.read_bytes() == rs.path.read_bytes()
            assert b"urn:iso:std:iso:ts:21496:-1" not in rh.path.read_bytes()


def test_jpeg_only_photo1_export_hdr_refused_with_no_raw(settings: Any, lib: Library, tmp_path: Path) -> None:
    """方法 4：第 1 张只有 JPG（无 pair RAW）时，diptych / singles / both 导出 HDR 均以 no_raw 拒绝。"""
    shot2, _j1, _j2 = _stage_pair_shot(lib, tag="noraw", orientation=1, first_half="left", first_has_dng=False, full_camera_jpg=True)
    render = RenderService(settings.home_dir, lib)

    dip = fserve.export_finish(
        render, lib, shot2, EditState(), {}, tmp_path / "nr_dip", [], quality=92, long_edge=None, fmt="jpeg", base="auto", hdr=True
    )
    assert dip.hdr is False and dip.hdr_reason == "no_raw"
    dip_dict = hfe.export_result_dict(shot2["shot_id"], dip, hdr_notes=HDR_NOTES, hdr_unsupported=HDR_UNSUPPORTED)
    assert dip_dict["hdr_note"] == "HDR 需要 RAW 文件"
    assert b"urn:iso:std:iso:ts:21496:-1" not in dip.path.read_bytes()

    for mode in ("singles", "both"):
        entries = hfe.export_pair_finish(
            render,
            lib,
            shot2,
            EditState(),
            {},
            tmp_path / f"nr_{mode}",
            [],
            quality=92,
            long_edge=None,
            fmt="jpeg",
            base="auto",
            hdr=True,
            mode=mode,
        )
        for sid, role, res in entries:
            assert res.hdr is False and res.hdr_reason == "no_raw"
            item = hfe.export_result_dict(sid, res, hdr_notes=HDR_NOTES, hdr_unsupported=HDR_UNSUPPORTED, role=role)
            assert item["hdr_note"] == "HDR 需要 RAW 文件"
            assert b"urn:iso:std:iso:ts:21496:-1" not in res.path.read_bytes()


def test_export_pair_both_hdr_with_diptych_frame_keeps_singles_unframed_and_valid(settings: Any, lib: Library, tmp_path: Path) -> None:
    """both 模式下双联带边框、单格不带边框：三张各自产出合法 HDR，双联边框增益为 0。"""
    shot2, _j1, _j2 = _stage_pair_shot(lib, tag="frm", orientation=1, first_half="left")
    render = RenderService(settings.home_dir, lib)
    edit = EditState(frame=parse_frame({"style": "gallery", "color": "white", "weight": "thick", "output_ratio": "original"}))
    sdr_both = hfe.export_pair_finish(
        render, lib, shot2, edit, {}, tmp_path / "bf_sdr", [], quality=92, long_edge=None, fmt="jpeg", base="neutral", hdr=False, mode="both"
    )
    hdr_both = hfe.export_pair_finish(
        render, lib, shot2, edit, {}, tmp_path / "bf_hdr", [], quality=92, long_edge=None, fmt="jpeg", base="neutral", hdr=True, mode="both"
    )
    assert len(hdr_both) == 3
    for (_a, _ra, rs), (_b, _rb, rh) in zip(sdr_both, hdr_both, strict=True):
        assert rh.hdr is True
        chk = hs.check_file(rs.path.read_bytes(), rh.path.read_bytes())
        assert chk["ok"], chk["fail"]
    # 确认 HdrTrack.for_shot 对无 pair RAW 的 shot 返回 dng=None
    shot_nr, _, _ = _stage_pair_shot(lib, tag="fnr", first_has_dng=False)
    tr = HdrTrack.for_shot(lib, shot_nr)
    assert tr.dng is None
    tr.begin(np.zeros((32, 32, 3), np.uint8))
    assert tr.problem == "no_raw"


def test_stale_v2_unsupported_does_not_block_pair_and_pair_unsupported_is_respected(lib: Library) -> None:
    """P2-1：旧版本写下的单图 `-v2.json` 不阻塞半格对生成衍生 HDR，而半格对自己的 `unsupported` 仍生效。"""
    assert derived.VERSION == 2
    shot_ok, _j1, jpg2 = _stage_pair_shot(lib, tag="s1", orientation=1, first_half="left", first_has_dng=True)
    sid_ok = shot_ok["shot_id"]
    stale = derived.status_path(lib, sid_ok)
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text(json.dumps({"status": derived.UNSUPPORTED, "reason": "not_aligned", "version": derived.VERSION}), encoding="utf-8")

    # 忽略旧单图 `-v2.json`：状态为 pending，generate 产出 pair 独立文件并变 ready
    assert derived.status_of(lib, shot_ok) == derived.PENDING
    assert derived.generate(lib, shot_ok) == derived.READY
    assert derived.status_of(lib, shot_ok) == derived.READY
    pair_hdr = derived.hdr_path(lib, sid_ok, pair=True)
    assert pair_hdr.is_file()
    assert not derived.hdr_path(lib, sid_ok).exists()
    checked = hs.check_file(jpg2, pair_hdr.read_bytes())
    assert checked["ok"], checked["fail"]

    # 半格对自身的 unsupported 状态（`-pair-v1.json`）仍被尊重
    wrong = np.roll(hs.scene(SYNTH_H, SYNTH_W, seed=99), 120, axis=1)
    shot_bad, _jb1, _jb2 = _stage_pair_shot(lib, tag="s2", orientation=1, first_half="left", dng1_rgb=wrong, full_camera_jpg=True)
    sid_bad = shot_bad["shot_id"]
    assert derived.generate(lib, shot_bad) == derived.UNSUPPORTED
    assert derived.status_of(lib, shot_bad) == derived.UNSUPPORTED
    assert derived.status_path(lib, sid_bad, pair=True).is_file()
    assert not derived.status_path(lib, sid_bad).exists()
    assert derived.pending(lib, [shot_bad]) == []


def test_wrong_half_dng_with_dark_first_half_and_skipped_first_jpeg_refused() -> None:
    """P3-1：即使跳过第 1 张 JPG 拍摄核对、且第 1 半格低反差、第 2 半格高纹理，换错第 1 半 DNG 也必须报 not_aligned。"""
    from scipy import ndimage

    geo = hfc.proxy_geometry(SYNTH_W)
    x0, x1 = geo.strip_x0, geo.strip_x0 + geo.half_w

    def _low_contrast(seed: int) -> np.ndarray:
        tex = ndimage.gaussian_filter(np.random.default_rng(seed).random((SYNTH_H, geo.half_w)), 6.0)
        tex = (tex - float(tex.min())) / max(float(tex.max() - tex.min()), 1e-6) - 0.5
        y = np.clip(0.20 + 0.03 * tex, 0.12, 0.28)
        return np.repeat(y[..., None], 3, axis=2).astype(np.float64)

    # 第 1 半低反差（高通标准差低于 TILE_TEXTURE 且亮度残差低于 0.125 档）、第 2 半高纹理：
    # 整图对齐会被第 2 半拉过门槛，唯有逐半独立核对相关系数/位移才能拦住错配 DNG。
    s1 = _low_contrast(301)
    s1_wrong = _low_contrast(409)
    s2 = hs.scene(SYNTH_H, geo.half_w, seed=302)
    rgb1_wrong = np.empty((SYNTH_H, SYNTH_W, 3), dtype=np.float64)
    rgb2 = np.empty((SYNTH_H, SYNTH_W, 3), dtype=np.float64)
    for dst, strip in ((rgb1_wrong, s1_wrong), (rgb2, s2)):
        dst[:, x0:x1] = strip
        dst[:, :x0] = strip[:, :1]
        dst[:, x1:] = strip[:, -1:]
    dip = np.zeros((SYNTH_H, SYNTH_W, 3), dtype=np.float64)
    dip[:, : geo.half_w] = s1
    dip[:, geo.right_x0 :] = s2
    jpg2 = _camera_jpeg(dip, orientation=1)
    dng1_wrong = _synth_dng(rgb1_wrong, orientation=1)
    dng2 = _synth_dng(rgb2, orientation=1)

    raw = g.pair_raw_luma(open_dng(dng1_wrong), open_dng(dng2), first_half="left", orientation=1, first_jpeg=None)
    with pytest.raises(g.HdrUnsupported) as err:
        g.hdr_from_camera_jpeg(jpg2, raw)
    assert err.value.reason == "not_aligned"


def test_dead_halfframe_pair_hdr_note_removed() -> None:
    """P3-2：已废弃的 halfframe_pair 文案应从 HDR_NOTES 删除。"""
    assert "halfframe_pair" not in HDR_NOTES
