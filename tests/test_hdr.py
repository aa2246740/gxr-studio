"""HDR：增益计算与封装、导入后在后台生成、导出时按成片重新算、RAW 与 JPG 对不齐时不做。

验收三项（research/hdr_gainmap/selfcheck.py）：主图字节保留；系统 ImageIO 识别（只在 macOS）；提亮与规格一致。
合成样本在每个平台都跑；`captures/` 里的真实样本只读，不在本机就跳过。
"""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import hdr_support as hs
from conftest import PORT
from gxr_studio import audit
from gxr_studio.app import create_app
from gxr_studio.finish.geometry import warp, warp_float
from gxr_studio.finish.spec import Geometry
from gxr_studio.hdr import container, derived
from gxr_studio.hdr import gain as g
from gxr_studio.library.store import LibraryPathRefused, sha256_file
from gxr_studio.privacy import PrivatePathError
from gxr_studio.raw import open_dng
from support.fake_card import make_fake_card
from support.waiting import wait_job

CAPTURES = Path(__file__).resolve().parents[2] / "captures"
STREET = CAPTURES / "street-g137-20261031"
A12_50 = CAPTURES / "a12-50" / "2026-10-24" / "stage2l-daily-R8051312-1473"
needs_street = pytest.mark.skipif(not (STREET / "dng").is_dir(), reason="captures/street-g137-20261031 样本不在本机")
needs_50mm = pytest.mark.skipif(not A12_50.is_dir(), reason="captures/a12-50 样本不在本机")
DONE = "已导入 5 张照片。现在可以推出存储卡了。"
ENCODED_ZERO = (
    0.1  # 增益图按 JPEG 质量 90 存：亮区边上的振铃最多几个码值（< 0.1 档）。算出来的增益是 0，见 test_frame_and_stamp_gain_is_exactly_zero
)


@pytest.fixture(scope="module")
def imageio_exe(tmp_path_factory):
    """系统 ImageIO 检查工具，每次测试运行只编译一次。不是 macOS 时为空。"""
    return hs.imageio_tool(tmp_path_factory.mktemp("imageio")) if hs.HAS_IMAGEIO else None


def _hdr(jpg: bytes, dng: bytes) -> tuple[bytes, dict]:
    return g.hdr_from_camera_jpeg(jpg, g.raw_luma(open_dng(dng)))


def _assert_boost_follows_raw(r: dict, raw: g.RawLuma) -> None:
    """规格：RAW 不过曝、JPG 发白的地方，HDR 按场景线性继续变亮（合成 JPG = 4 × RAW）。"""
    ry = g.block_mean(raw.y, 2)
    clip = g.block_mean(raw.clip.astype(np.float32), 2) > 0
    lg = r["log_gain"][: ry.shape[0], : ry.shape[1]]
    sel = (ry > 0.3) & (ry < 0.7) & ~clip
    sel[:2], sel[-2:], sel[:, :2], sel[:, -2:] = False, False, False, False
    diff = lg[sel] - hs.expected_boost(ry[sel])
    assert sel.sum() > 300
    assert abs(float(np.median(diff))) < 0.25 and float(np.percentile(np.abs(diff), 95)) < 0.35, (
        np.median(diff),
        np.percentile(np.abs(diff), 95),
    )
    assert float(r["log_gain"][ry < 0.04].max()) < 0.05  # 暗部、中间调不提亮


# ---------------------------------------------------------------- 计算与封装（每个平台）


@pytest.mark.parametrize("orientation", [1, 6, 8, 3])
def test_camera_jpeg_bytes_kept_and_boost_follows_spec(orientation):
    jpg, dng = hs.pair(0, orientation)
    out, stats = _hdr(jpg, dng)
    r = hs.check_file(jpg, out)
    assert r["ok"], r["fail"]
    assert stats["alignment"]["correlation"] > 0.9 and stats["alignment"]["shift"] == [0, 0]
    raw = g.raw_luma(open_dng(dng))
    stored = g.RawLuma(g.to_storage(raw.y, orientation), g.to_storage(raw.clip, orientation))
    _assert_boost_follows_raw(r, stored)
    assert r["meta"]["GainMapMax"] == pytest.approx(stats["gain_max_stops"], abs=1e-3)


def test_old_mpf_xmp_iso_segments_are_replaced_not_duplicated():
    jpg, dng = hs.pair(1)
    once, _ = _hdr(jpg, dng)
    twice, _ = _hdr(once, dng)  # 已经是 HDR 的文件再做一次：旧的 MPF/XMP/ISO 换掉，增益图只有一幅
    assert hs.check_file(jpg, twice)["ok"]
    assert twice.count(b"urn:iso:std:iso:ts:21496:-1") == 2


def test_container_rejects_broken_jpeg():
    with pytest.raises(container.ContainerError):
        container.jpeg_segments(b"not a jpeg")
    jpg, _ = hs.pair(0)
    with pytest.raises(container.ContainerError):
        container.jpeg_segments(jpg[: len(jpg) // 2])
    with pytest.raises(container.ContainerError):
        container.jpeg_segments(jpg[:2] + b"\xff\xe1\x00")
    with pytest.raises(container.ContainerError):
        container.app_segment(0xE1, bytes(70000))
    with pytest.raises(container.ContainerError):
        container.iso_full(container.GainMeta(gamma=-1.0))


def test_progressive_jpeg_segments_are_walked_to_the_real_eoi():
    buf = io.BytesIO()
    Image.fromarray(hs.srgb_bytes(hs.scene(64, 96))).save(buf, "JPEG", progressive=True, quality=90)
    data = buf.getvalue()
    _segs, _sos, eoi = container.jpeg_segments(data)
    assert eoi == len(data) and data.count(b"\xff\xda") > 1


def test_flat_image_without_highlights_has_no_curve():
    jpg = hs.camera_jpeg(np.full((128, 192, 3), 0.01))
    dng = hs.camera_dng(np.full((128, 192, 3), 0.01))
    with pytest.raises(g.HdrUnsupported) as err:
        _hdr(jpg, dng)
    assert err.value.reason in ("not_aligned", "no_curve")


# ---------------------------------------------------------------- 对齐


def test_dng_from_another_shot_is_not_aligned():
    raw = g.raw_luma(open_dng(hs.camera_dng(hs.scene(seed=0))))
    shifted = np.roll(hs.scene(seed=0), 140, axis=1)
    with pytest.raises(g.HdrUnsupported) as err:
        g.hdr_from_camera_jpeg(hs.camera_jpeg(shifted), raw)
    assert err.value.reason == "not_aligned"


def test_local_misalignment_is_not_aligned():
    """整体对得上、一块挪了 12 像素（机内只对 JPG 做了局部校正就是这样）：分块核对拦下来。"""
    rgb = hs.scene(seed=0)
    moved = rgb.copy()
    moved[:192, :288] = np.roll(rgb, (12, 12), axis=(0, 1))[:192, :288]
    raw = g.raw_luma(open_dng(hs.camera_dng(rgb)))
    al = g.align(g.sdr_luma_grid(g.decode_rgb(hs.camera_jpeg(moved)), 4), raw.y)
    assert al.correlation >= g.MIN_CORRELATION and al.tiles_moved >= 2
    with pytest.raises(g.HdrUnsupported, match="局部"):
        g.check_alignment(al)


def test_small_translation_is_found_and_registered():
    """JPG 的画面比 DNG 往右下错开 4 像素（增益图上 1 格）：找得到，按这个平移对齐。"""
    big = hs.scene(400, 600, seed=2)
    raw = g.raw_luma(open_dng(hs.camera_dng(big[8:392, 8:584])))
    al = g.align(g.sdr_luma_grid(g.decode_rgb(hs.camera_jpeg(big[4:388, 12:588])), 4), raw.y)
    assert al.dx == -1 and al.dy in (0, 1) and abs(al.scale - 1.0) <= 0.0101 and al.tiles_moved == 0
    assert al.correlation > 0.9
    g.check_alignment(al)


# ---------------------------------------------------------------- 真实样本（只读）


def _real(jpg_path: Path, dng_path: Path, tmp_path: Path) -> tuple[bytes, bytes, dict]:
    jpg = jpg_path.read_bytes()
    before = sha256_file(jpg_path), sha256_file(dng_path)
    out, stats = g.hdr_from_camera_jpeg(jpg, g.raw_luma(open_dng(dng_path)))
    assert (sha256_file(jpg_path), sha256_file(dng_path)) == before
    return jpg, out, stats


@needs_street
@pytest.mark.parametrize("name", ["R8051532", "R8051627", "R8051653"])
def test_real_mount_samples(name, tmp_path, imageio_exe):
    """MOUNT 单元（LensInfo 是 0/0）：三项检查 + macOS 上的系统 ImageIO。黑白、夜里灯笼、夜里招牌各一张。"""
    jpg, out, stats = _real(STREET / "jpg" / f"{name}.JPG", STREET / "dng" / f"{name}.DNG", tmp_path)
    r = hs.check_file(jpg, out)
    assert r["ok"], r["fail"]
    assert 0.3 < stats["gain_max_stops"] <= 2.5
    if not hs.HAS_IMAGEIO:
        return  # 系统 ImageIO 只在 macOS 上有
    orig, hdr = tmp_path / f"{name}.JPG", tmp_path / f"{name}_HDR.JPG"
    orig.write_bytes(jpg)
    hdr.write_bytes(out)
    sys_ = hs.imageio_check(imageio_exe, orig, hdr, r)
    assert sys_["iso_gainmap"], sys_
    assert sys_["sdr_max_abs_diff"] <= 1
    assert sys_["boost_err_p99"] < 0.15, sys_


@needs_50mm
@pytest.mark.parametrize("name", ["R8051312", "R8051435", "R8051465"])
def test_real_50mm_pairs_are_aligned(name, tmp_path):
    """A12 50mm：DNG 和 JPG 逐块都对得上（没有机内畸变校正带来的局部错位），能做 HDR。"""
    jpg, out, stats = _real(A12_50 / f"{name}.JPG", A12_50 / f"{name}.DNG", tmp_path)
    al = stats["alignment"]
    assert al["correlation"] > 0.8 and al["shift"] == [0, 0] and al["scale"] == 1.0 and al["tiles_moved"] == 0
    assert hs.check_file(jpg, out)["ok"]


@needs_50mm
def test_real_pair_with_wrong_dng_is_unsupported():
    jpg = (A12_50 / "R8051312.JPG").read_bytes()
    with pytest.raises(g.HdrUnsupported):
        g.hdr_from_camera_jpeg(jpg, g.raw_luma(open_dng(A12_50 / "R8051313.DNG")))


@hs.needs_imageio
def test_imageio_reads_synthetic_hdr(tmp_path, imageio_exe):
    jpg, dng = hs.pair(3)
    out, _ = _hdr(jpg, dng)
    r = hs.check_file(jpg, out)
    orig, hdr = tmp_path / "a.JPG", tmp_path / "a_HDR.JPG"
    orig.write_bytes(jpg)
    hdr.write_bytes(out)
    sys_ = hs.imageio_check(imageio_exe, orig, hdr, r)
    assert sys_["iso_gainmap"] and sys_["boost_err_p99"] < 0.15, sys_
    assert sys_["system_max_boost"] > 1.0
    plain = hs.imageio_check(imageio_exe, orig, orig, hs.check_file(jpg, out))
    assert not plain["iso_gainmap"]


# ---------------------------------------------------------------- 几何


@pytest.mark.parametrize(
    "geom",
    [
        Geometry(straighten=3.5),
        Geometry(vertical=20, distortion=-15),
        Geometry(rotate90=1, flip=True),
        Geometry(horizontal=-10, rotate90=3, straighten=-2),
    ],
)
def test_float_layer_follows_the_same_warp(geom):
    rng = np.random.default_rng(0)
    rgb = (rng.random((60, 90, 3)) * 255).astype(np.uint8)
    out, valid = warp(rgb, geom)
    layer = warp_float(rgb.astype(np.float32), geom)
    assert layer.shape == out.shape
    assert np.array_equal(np.isfinite(layer[..., 0]), valid)
    assert np.abs(np.clip(layer[valid] + 0.5, 0, 255).astype(np.uint8).astype(int) - out[valid].astype(int)).max() <= 1


def test_frame_and_stamp_gain_is_exactly_zero():
    """数据层跟着日期戳和边框走：那些格子的增益（编码之前）正好是 0，旁边发白的照片照常提亮。"""
    from gxr_studio.finish.hdr_export import HdrTrack, _to_grid
    from gxr_studio.finish.spec import parse_frame, parse_stamp

    jpg, dng = hs.pair(0)
    rgb = np.asarray(Image.open(io.BytesIO(jpg)).convert("RGB"))
    track = HdrTrack(None)
    raw = g.raw_luma(open_dng(dng))
    y, clip = g.register(raw, rgb.shape[:2], g.Alignment(1.0, 0, 0, 1.0), g.grid_shape(rgb.shape, 4))
    track.layer = np.dstack([y, clip.astype(np.float32)])
    stamp = parse_stamp({"enabled": True, "content": "date", "date_override": "2026-10-31", "corner": "tr", "size": "large", "glow": True})
    frame = parse_frame({"style": "leica", "color": "white"})
    from gxr_studio.finish.frame import apply_frame
    from gxr_studio.finish.stamp import apply_stamp

    out = apply_frame(apply_stamp(rgb, stamp, None, None), frame, {"lens": "A12 50mm"})
    track.stamp(stamp, None, None)
    track.frame(frame, {"lens": "A12 50mm"})
    assert track.layer is not None and track.layer.shape[:2] == out.shape[:2]
    raw_y, raw_clip = _to_grid(track.layer, (out.shape[1], out.shape[0]), 4)
    res = g.compute_gain(g.sdr_luma_grid(out, 4), raw_y, raw_clip, g.Params())
    photo = np.isfinite(raw_y)
    assert not photo[-3:].any()  # 徕卡边框的下边条
    assert float(res.delta[~photo].max()) == 0.0
    assert float(res.delta[photo].max()) > 1.0


def test_track_without_raw_or_with_unknown_frame_layout():
    from gxr_studio.finish.hdr_export import NO_RAW, HdrTrack, finish
    from gxr_studio.finish.spec import parse_frame

    track = HdrTrack(None)
    track.begin(np.zeros((8, 8, 3), np.uint8))
    assert track.problem == NO_RAW and not track.active
    assert finish(b"x", track, "jpeg") == (b"x", {"hdr": False, "reason": NO_RAW})
    track.layer = np.zeros((8, 8, 2), np.float32)
    import gxr_studio.finish.hdr_export as he

    real = he.apply_frame
    # 不像任何边框：照片的位置认不出来
    he.apply_frame = lambda rgb, spec, meta, crop_preset=None: np.zeros((20, 20, 3), np.uint8)
    try:
        track.frame(parse_frame({"style": "gallery"}), {})
    finally:
        he.apply_frame = real
    assert track.layer is None and track.problem == "frame_layout"


def test_unreadable_dng_on_export_is_unsupported(tmp_path):
    from gxr_studio.finish.hdr_export import HdrTrack

    bad = tmp_path / "x.DNG"
    bad.write_bytes(b"II*\x00junk")
    track = HdrTrack(bad)
    track.begin(np.zeros((64, 64, 3), np.uint8))
    assert track.problem == "raw_unreadable"


# ---------------------------------------------------------------- 导入后生成、导出


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        yield c


def _hdr_job(client):
    """导入任务结束之前就已经登记了 HDR 任务（或者决定不做），所以这里不用等它出现。"""
    jobs = [j for j in client.get("/api/jobs").json() if j["kind"] == "hdr"]
    return wait_job(client, jobs[0]["id"]) if jobs else None


@pytest.fixture
def hdr_card(volumes):
    """一对能做 HDR 的 RAW+JPG（HDR0001）、一对 DNG 来自别的画面（HDR0002）、加上假卡自带的随机字节对和单 JPG。"""
    card = make_fake_card(volumes, pairs=1, jpg_only=1)
    folder = card.root / "DCIM" / "100RICOH"
    hs.write_pair(folder, "HDR0001", seed=0)
    hs.write_pair(folder, "HDR0002", seed=1)
    (folder / "HDR0002.JPG").write_bytes(hs.camera_jpeg(np.roll(hs.scene(seed=1), 140, axis=1)))
    (folder / "ONLYJPG.JPG").write_bytes(hs.camera_jpeg(hs.scene(seed=5)))
    return card


def _import(client):
    from test_api import import_all

    job = import_all(client)
    return job, _hdr_job(client)


def _photo(client, name):
    return next(p for p in client.get("/api/photos").json() if p["name"] == name)


def test_import_makes_hdr_versions_without_touching_originals(client, hdr_card, settings):
    folder = hdr_card.root / "DCIM" / "100RICOH"
    card_before = {p.name: sha256_file(p) for p in folder.iterdir()}
    job, hdr_job = _import(client)
    assert job["progress"]["state"] == "done"
    assert job["progress"]["message"] == DONE  # 和不做 HDR 时一样
    assert hdr_job is not None and hdr_job["error"] is None
    assert hdr_job["progress"] == {"state": "done", "done": 3, "ready": 1, "unsupported": 2, "failed": 0}
    ok, other = _photo(client, "HDR0001"), _photo(client, "HDR0002")
    assert ok["hdr"] == "ready" and other["hdr"] == "unsupported"
    assert _photo(client, "ONLYJPG")["hdr"] == "none"
    lib = client.app.state.ctx.lib
    made = derived.hdr_path(lib, ok["id"])
    assert made.parent.parent == settings.library_dir / ".studio" / "hdr"
    shot = lib.get_shot(ok["id"])
    for kind in ("jpg", "dng"):
        assert sha256_file(lib.abspath(shot["files"][kind]["relpath"])) == shot["files"][kind]["sha256"]
    assert hs.check_file(lib.abspath(shot["files"]["jpg"]["relpath"]).read_bytes(), made.read_bytes())["ok"]
    reason = json.loads(derived.status_path(lib, other["id"]).read_text(encoding="utf-8"))
    assert reason == {"status": "unsupported", "reason": "not_aligned", "version": derived.VERSION}
    assert {p.name: sha256_file(p) for p in folder.iterdir()} == card_before
    # 重建索引不会把派生文件当成照片
    assert client.post("/api/library/rebuild").status_code == 200
    assert {p["name"] for p in client.get("/api/photos").json()} == {"HDR0001", "HDR0002", "ONLYJPG", "R0010001", "R0010002"}
    # 再插一次卡：都做过了，不再重做
    _job2, again = _import(client)
    assert again is None or again["progress"]["done"] == 0


def test_hdr_failure_is_logged_and_does_not_touch_the_import(client, hdr_card, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("坏了")

    monkeypatch.setattr(derived, "generate", boom)
    job, hdr_job = _import(client)
    assert job["progress"]["state"] == "done" and job["progress"]["message"] == DONE
    assert hdr_job["error"] is None and hdr_job["progress"]["failed"] == 3
    assert client.get("/api/alerts").json()["alerts"] == []  # 每张的失败只记日志，任务本身不算失败
    assert _photo(client, "HDR0001")["hdr"] == "pending"


def test_flag_turns_import_hdr_off(client, hdr_card, settings):
    settings.home_dir.mkdir(parents=True, exist_ok=True)
    (settings.home_dir / "flags.json").write_text(json.dumps({"hdr_on_import": False}), encoding="utf-8")
    _job, hdr_job = _import(client)
    assert hdr_job is None


def test_hdr_is_not_written_through_a_link_to_the_card(client, hdr_card, settings):
    """`.studio/hdr` 被换成指到卡上的链接：一个字节都不写到卡上，记为失败。"""
    target = hdr_card.root / "MISC"
    target.mkdir()
    link = settings.library_dir / ".studio" / "hdr"
    link.symlink_to(target, target_is_directory=True)
    _job, hdr_job = _import(client)
    assert hdr_job["progress"]["failed"] == 3 and hdr_job["progress"]["ready"] == 0
    assert list(target.iterdir()) == []


def test_private_dir_refuses_card_roots(lib, volumes):
    card = volumes / "CARD"
    (card / "DCIM").mkdir(parents=True)
    audit.protect(card)
    try:
        lib.card_roots = [card]
        (lib.root / ".studio" / "hdr").symlink_to(card, target_is_directory=True)
        jpg, dng = hs.pair(0)
        folder = lib.root / "2026" / "2026-10-31"
        folder.mkdir(parents=True)
        (folder / "A.JPG").write_bytes(jpg)
        (folder / "A.DNG").write_bytes(dng)
        shot = {"shot_id": "ab" * 32, "files": {"jpg": {"relpath": "2026/2026-10-31/A.JPG"}, "dng": {"relpath": "2026/2026-10-31/A.DNG"}}}
        with pytest.raises((LibraryPathRefused, PrivatePathError)):
            derived.generate(lib, shot)
        assert sorted(p.name for p in card.iterdir()) == ["DCIM"]
    finally:
        audit.unprotect(card)


def test_run_pending_stops_when_cancelled(lib):
    cancel = threading.Event()
    cancel.set()
    assert derived.run_pending(lib, derived.HdrProgress(), cancel).state == "cancelled"


def _export(client, photo, **body):
    r = client.post(f"/api/photos/{photo['id']}/export", json={"quality": 92, **body})
    assert r.status_code == 200, r.text
    return r.json()


def test_export_hdr_unedited_keeps_the_sdr_export_bytes(client, hdr_card):
    _import(client)
    photo = _photo(client, "HDR0001")
    sdr = _export(client, photo)
    hdr = _export(client, photo, hdr=True)
    assert sdr["hdr"] is None and hdr["hdr"] is True and hdr["hdr_note"] is None
    plain, made = Path(sdr["path"]).read_bytes(), Path(hdr["path"]).read_bytes()
    r = hs.check_file(plain, made)  # 主图就是不带 HDR 时会导出的那份，一个字节都不差
    assert r["ok"], r["fail"]
    assert r["meta"]["GainMapMax"] > 1.0


def _grid_luma(jpeg: bytes) -> np.ndarray:
    return g.sdr_luma_grid(np.asarray(Image.open(io.BytesIO(jpeg)).convert("RGB")), 4)


def test_export_hdr_follows_rotation_straighten_crop_and_frame(client, hdr_card):
    _import(client)
    photo = _photo(client, "HDR0001")
    edit = {
        "geometry": {"rotate90": 1, "straighten": 2.0},
        "frame": {"style": "gallery", "color": "white", "weight": "thick", "output_ratio": "original"},
    }
    assert client.put(f"/api/photos/{photo['id']}/edit", json=edit).status_code == 200
    assert client.put(f"/api/photos/{photo['id']}/crop", json={"preset": "free", "rect": [0.1, 0.05, 0.8, 0.9]}).status_code == 200
    plain = Path(_export(client, photo)["path"]).read_bytes()
    res = _export(client, photo, hdr=True)
    assert res["hdr"] is True
    r = hs.check_file(plain, Path(res["path"]).read_bytes())
    assert r["ok"], r["fail"]
    lg, sdr_y = r["log_gain"], _grid_luma(plain)
    # 白色边框（SDR 发白，但不是照片）不提亮：外圈 m/4 − 1 格
    hh, ww = np.asarray(Image.open(io.BytesIO(plain))).shape[:2]
    edge = int(max(hh, ww) * 0.07 / 1.14 / 4) - 1
    assert edge >= 2
    for strip in (lg[:edge], lg[-edge:], lg[:, :edge], lg[:, -edge:]):
        assert float(strip.max()) < ENCODED_ZERO and float(strip.mean()) < 0.01
    # 照片里：SDR 发白的地方提亮（数据层跟着转了、裁了），暗的地方不动
    k = edge + 3  # 再往里 3 格：边框和照片交界的那一圈不算
    inner, inner_y = lg[k:-k, k:-k], sdr_y[k:-k, k:-k]
    assert float(inner[inner_y > 0.95].mean()) > 0.5
    assert float(inner[inner_y < 0.15].max()) < 0.05


@pytest.mark.parametrize("ratio", ("3:4", "16:9"))
def test_fixed_output_ratio_hdr_keeps_padding_dark(client, hdr_card, ratio):
    _import(client)
    photo = _photo(client, "HDR0001")
    edit = {"geometry": {"rotate90": 1, "straighten": 2.0}, "frame": {"style": "gallery", "weight": "thick", "output_ratio": ratio}}
    assert client.put(f"/api/photos/{photo['id']}/edit", json=edit).status_code == 200
    assert client.put(f"/api/photos/{photo['id']}/crop", json={"preset": "free", "rect": [0.1, 0.05, 0.8, 0.9]}).status_code == 200
    plain = Path(_export(client, photo)["path"]).read_bytes()
    made = _export(client, photo, hdr=True)
    assert made["hdr"] is True
    result = hs.check_file(plain, Path(made["path"]).read_bytes())
    assert result["ok"], result["fail"]
    hh, ww = np.asarray(Image.open(io.BytesIO(plain))).shape[:2]
    size = client.post(f"/api/photos/{photo['id']}/output-size", json={}).json()
    pw, ph = size["photo"]
    rw, rh = map(int, ratio.split(":"))
    assert size["sizes"]["original"] == [ww, hh] and ww * rh == hh * rw
    x, y = (ww - pw) // 2, (hh - ph) // 2
    gain = result["log_gain"]
    # 每格 4 像素，避开照片边界的混合格；新增留白不能有 HDR 增益。
    for strip in (gain[: y // 4 - 1], gain[(y + ph + 3) // 4 + 1 :], gain[:, : x // 4 - 1], gain[:, (x + pw + 3) // 4 + 1 :]):
        assert float(strip.max()) < ENCODED_ZERO and float(strip.mean()) < 0.01
    rows, cols = slice((y + 3) // 4 + 3, (y + ph) // 4 - 3), slice((x + 3) // 4 + 3, (x + pw) // 4 - 3)
    inner, luma = gain[rows, cols], _grid_luma(plain)[rows, cols]
    assert float(inner[luma > 0.95].mean()) > 0.5


def test_export_hdr_leaves_the_date_stamp_alone(client, hdr_card):
    _import(client)
    photo = _photo(client, "HDR0001")
    stamp = {
        "enabled": True,
        "content": "date",
        "date_override": "2026-10-31",
        "corner": "tr",
        "color": "orange",
        "size": "large",
        "glow": False,
    }
    assert client.put(f"/api/photos/{photo['id']}/edit", json={"stamp": stamp}).status_code == 200
    plain = Path(_export(client, photo)["path"]).read_bytes()
    res = _export(client, photo, hdr=True)
    r = hs.check_file(plain, Path(res["path"]).read_bytes())
    assert res["hdr"] is True and r["ok"], r["fail"]
    sdr = np.asarray(Image.open(io.BytesIO(plain)).convert("RGB")).astype(int)
    orange = (sdr[..., 0] > 200) & (sdr[..., 2] < 80) & (sdr[..., 1] < 160)
    ys, xs = np.nonzero(orange)
    assert ys.size > 50 and xs.mean() > sdr.shape[1] * 0.6  # 日期戳在右上角，盖在发白的亮区上
    lg = r["log_gain"]
    assert float(lg[ys // 4, xs // 4].max()) < ENCODED_ZERO and float(lg[ys // 4, xs // 4].mean()) < 0.01
    below = lg[ys.max() // 4 + 3 : ys.max() // 4 + 12, int(sdr.shape[1] * 0.82) // 4 : int(sdr.shape[1] * 0.95) // 4]
    assert below.size and float(below.mean()) > 0.5  # 日期戳下面同样发白的地方照常提亮


def test_export_hdr_without_raw_or_as_png_says_why(client, hdr_card):
    _import(client)
    only_jpg = _photo(client, "ONLYJPG")
    res = _export(client, only_jpg, hdr=True)
    assert res["hdr"] is False and res["hdr_note"] == "HDR 需要 RAW 文件"
    png = _export(client, _photo(client, "HDR0001"), hdr=True, format="png")
    assert png["hdr"] is False and png["hdr_note"] == "HDR 需要 JPEG 格式"
    other = _export(client, _photo(client, "HDR0002"), hdr=True)
    assert other["hdr"] is False and other["hdr_note"] == "这张照片不支持 HDR"


def test_batch_export_with_hdr(client, hdr_card):
    _import(client)
    ids = [_photo(client, n)["id"] for n in ("HDR0001", "ONLYJPG")]
    job = client.post("/api/export", json={"ids": ids, "quality": 90, "hdr": True}).json()
    results = {r["id"]: r for r in wait_job(client, job["id"])["progress"]["results"]}
    assert results[ids[0]]["hdr"] is True
    assert results[ids[1]]["hdr"] is False and results[ids[1]]["hdr_note"] == "HDR 需要 RAW 文件"


def test_raw_only_export_can_be_hdr(client, volumes):
    """只有 RAW：底图是 RAW 渲染，和 RAW 天然对齐。"""
    card = make_fake_card(volumes, pairs=0, jpg_only=0)
    _jpg, dng = hs.pair(4)
    (card.root / "DCIM" / "100RICOH" / "RAWONLY.DNG").write_bytes(dng)
    _import(client)
    photo = _photo(client, "RAWONLY")
    assert photo["hdr"] == "none"
    res = _export(client, photo, hdr=True)
    assert res["hdr"] is True, res


def test_export_form_has_the_hdr_switch():
    """导出面板：开关叫"HDR"（苹果"照片"的叫法），选中的照片都没有 RAW 时不可用并写明原因，请求里带上 hdr。"""
    js = (Path(__file__).resolve().parents[1] / "web" / "assets" / "export.js").read_text(encoding="utf-8")
    assert "toggle('HDR', hdr" in js and "{ disabled: !ok }" in js
    assert "HDR_NO_RAW = 'HDR 需要 RAW 文件'" in js
    assert "precise, hdr }" in js
    assert "r.hdr === false" in js and "r.hdr_note" in js


def test_hdr_export_is_named_gxr_hdr_and_only_when_it_was_made(client, hdr_card):
    """HDR 导出叫「原名_gxr_hdr.jpg」，重名加 -2；没做成 HDR 的（只有 JPG、PNG、不支持）仍是普通名字。"""
    _import(client)
    photo = _photo(client, "HDR0001")
    assert Path(_export(client, photo)["path"]).name == "HDR0001_gxr.jpg"
    first = _export(client, photo, hdr=True)
    assert first["hdr"] is True and Path(first["path"]).name == "HDR0001_gxr_hdr.jpg"
    again = _export(client, photo, hdr=True)
    assert Path(again["path"]).name == "HDR0001_gxr_hdr-2.jpg"
    assert Path(_export(client, photo)["path"]).name == "HDR0001_gxr-2.jpg"  # 普通名字和 HDR 名字各数各的
    only_jpg = _export(client, _photo(client, "ONLYJPG"), hdr=True)
    assert only_jpg["hdr"] is False and Path(only_jpg["path"]).name == "ONLYJPG_gxr.jpg"
    png = _export(client, _photo(client, "HDR0001"), hdr=True, format="png")
    assert png["hdr"] is False and Path(png["path"]).name == "HDR0001_gxr.png"
    other = _export(client, _photo(client, "HDR0002"), hdr=True)
    assert other["hdr"] is False and Path(other["path"]).name == "HDR0002_gxr.jpg"
