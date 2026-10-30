"""只有 RAW 的照片也能导出（中性转换）；JPEG/PNG、尺寸档/质量档、EXIF、sRGB ICC、裁切与拉直。（S4）"""

from __future__ import annotations

import io
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from gxr_studio.app import create_app
from gxr_studio.crop import Crop, default_crop
from gxr_studio.export import (
    QUALITY_TIERS,
    SIZE_TIERS,
    ExportError,
    ExportOptions,
    export_shot,
    render_raw,
)
from gxr_studio.library.store import sha256_file
from gxr_studio.raw import NEUTRAL, Renderer, open_dng, render_full

from conftest import PORT
from raw_support import bayer_scene, build_dng
from test_api import find, import_all

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DIR = ROOT / "captures/a12-50/2026-10-19/stage2c-first-run"


def scene_dng(w=96, h=64, **kw) -> bytes:
    yy, xx = np.mgrid[0:h, 0:w].astype(float)
    scene = np.stack([0.1 + 0.6 * xx / w, 0.2 + 0.5 * yy / h, 0.7 - 0.5 * xx / w], axis=-1)
    kw.setdefault("neutral", (0.645161, 1.0, 0.574712))
    return build_dng(bayer_scene(h, w, scene, white=4095), white=4095, active=(0, 0, h, w), **kw)


@pytest.fixture
def raw_only(tmp_path, lib):
    """库里一张只有 DNG 的照片；返回 (shot 字典, 导出目录, DNG 路径)。"""
    rel = "2026/2026-10-16/R0000201.DNG"
    path = lib.root / rel
    path.parent.mkdir(parents=True)
    path.write_bytes(scene_dng())
    shot = {"shot_id": "s201", "name": "R0000201", "files": {"dng": {"relpath": rel}}}
    return shot, tmp_path / "out", path


def test_raw_only_exports_jpeg_with_exif_icc_and_neutral_mark(lib, raw_only):
    shot, out, dng = raw_only
    before = sha256_file(dng)
    res = export_shot(lib, shot, None, out, ExportOptions(None, 90), [])
    assert res.path.name == "R0000201_gxr_neutral.jpg" and res.render == "neutral-preview"
    assert res.render_label == "中性转换" and res.format == "jpeg"
    im = Image.open(res.path)
    assert im.size == (96, 64) == (res.width, res.height) and im.format == "JPEG"
    assert im.info.get("icc_profile")
    ex = im.getexif()
    sub = ex.get_ifd(0x8769)
    assert ex[271] == "RICOH" and ex[272] == "GXR A12" and ex[305] == "GXR Studio" and ex[274] == 1
    assert "neutral-preview" in ex[270] and "no recipe" in ex[270]
    assert sub[36867] == "2026:10:16 18:51:53" and sub[34855] == 200
    assert sub[42036] == "GR LENS A12 50mm F2.5 MACRO"  # 取自 UniqueCameraModel，去掉 "RICOH "
    assert 37500 not in sub, "MakerNote 搬不走"
    assert (sub[40962], sub[40963]) == (96, 64)
    assert sha256_file(dng) == before, "导出不能改动原文件"
    # 内容就是中性渲染（JPEG 有损，给点容差）
    ref = render_full(open_dng(dng), NEUTRAL)
    assert np.abs(np.asarray(im).astype(int) - ref.astype(int)).mean() < 3


def test_png_has_exif_icc_and_is_lossless(lib, raw_only):
    shot, out, dng = raw_only
    res = export_shot(lib, shot, None, out, ExportOptions(None, 95, "png"), [])
    assert res.path.name == "R0000201_gxr_neutral.png" and res.format == "png"
    im = Image.open(res.path)
    assert im.format == "PNG" and im.info.get("icc_profile")
    assert "neutral-preview" in im.getexif()[270]
    assert np.array_equal(np.asarray(im.convert("RGB")), render_full(open_dng(dng), NEUTRAL))
    second = export_shot(lib, shot, None, out, ExportOptions(None, 95, "png"), [])
    assert second.path.name == "R0000201_gxr_neutral-2.png"


def test_size_and_quality_tiers(lib, raw_only):
    shot, out, _ = raw_only
    assert SIZE_TIERS == {"large": 3000, "medium": 2048, "small": 1600, "original": None}
    assert QUALITY_TIERS == {"high": 95, "medium": 85, "low": 70}
    opts = ExportOptions.from_tiers("small", "low", "png")
    assert (opts.long_edge, opts.quality, opts.fmt) == (1600, 70, "png")
    assert ExportOptions.from_tiers("original", "high").long_edge is None
    res = export_shot(lib, shot, None, out, ExportOptions(64, 80), [])
    assert (res.width, res.height) == (64, 43)
    q_hi = export_shot(lib, shot, None, out, ExportOptions(None, 95), [])
    q_lo = export_shot(lib, shot, None, out, ExportOptions(None, 30), [])
    assert q_lo.bytes < q_hi.bytes
    for bad, code in ((ExportOptions(None, 0), "bad_quality"), (ExportOptions(5, 90), "bad_size"),
                      (ExportOptions(None, 90, "gif"), "bad_format")):
        with pytest.raises(ExportError) as e:
            bad.validate()
        assert e.value.code == code
    with pytest.raises(ExportError):
        ExportOptions.from_tiers("huge")
    with pytest.raises(ExportError):
        ExportOptions.from_tiers(None, "ultra")


def test_crop_and_straighten_dimensions_on_raw(lib, raw_only):
    shot, out, _ = raw_only
    xpan = default_crop("xpan-65-24", 96, 64)
    res = export_shot(lib, shot, xpan, out, ExportOptions(None, 90), [])
    assert (res.width, res.height) == (96, 35)  # 96 * 24 / 65 = 35.4
    tilted = default_crop("3:2", 96, 64, angle_deg=4.0)
    res2 = export_shot(lib, shot, tilted, out, ExportOptions(None, 90, "png"), [])
    x = tilted.rect
    assert (res2.width, res2.height) == (round(x[2] * 96), round(x[3] * 64))
    # 拉直确实动了像素：同一个框，0° 和 4° 不一样
    flat = Crop("3:2", tilted.rect, 0.0)
    a, _ = render_raw(shot_path(lib, shot), flat, ExportOptions(None, 90, "png"))
    b, _ = render_raw(shot_path(lib, shot), tilted, ExportOptions(None, 90, "png"))
    assert Image.open(io.BytesIO(a)).size == Image.open(io.BytesIO(b)).size and a != b


def shot_path(lib, shot) -> Path:
    return lib.abspath(shot["files"]["dng"]["relpath"])


def test_oriented_raw_exports_upright_dimensions(lib, tmp_path):
    rel = "2026/2026-10-16/R0000202.DNG"
    path = lib.root / rel
    path.parent.mkdir(parents=True)
    path.write_bytes(scene_dng(orientation=6))
    shot = {"shot_id": "s202", "name": "R0000202", "files": {"dng": {"relpath": rel}}}
    res = export_shot(lib, shot, None, tmp_path / "o", ExportOptions(None, 90), [])
    assert (res.width, res.height) == (64, 96)


def test_camera_jpg_is_preferred_when_present(lib, tmp_path):
    jrel = "2026/2026-10-16/R0000203.JPG"
    jpath = lib.root / jrel
    jpath.parent.mkdir(parents=True)
    Image.new("RGB", (80, 60), (10, 200, 30)).save(jpath, "JPEG")
    drel = "2026/2026-10-16/R0000203.DNG"
    (lib.root / drel).write_bytes(scene_dng())
    shot = {"shot_id": "s203", "name": "R0000203", "files": {"jpg": {"relpath": jrel}, "dng": {"relpath": drel}}}
    res = export_shot(lib, shot, None, tmp_path / "o", ExportOptions(None, 90), [])
    assert res.render == "camera-jpg" and res.path.name == "R0000203_gxr.jpg" and (res.width, res.height) == (80, 60)
    png = export_shot(lib, shot, None, tmp_path / "o", ExportOptions(None, 90, "png"), [])
    assert png.path.name == "R0000203_gxr.png" and Image.open(png.path).format == "PNG"


def test_unreadable_raw_without_jpg_keeps_the_old_error(lib, tmp_path):
    rel = "2026/2026-10-16/R0000204.DNG"
    (lib.root / rel).parent.mkdir(parents=True)
    (lib.root / rel).write_bytes(b"II*\x00not really a dng")
    shot = {"shot_id": "s204", "name": "R0000204", "files": {"dng": {"relpath": rel}}}
    with pytest.raises(ExportError) as e:
        export_shot(lib, shot, None, tmp_path / "o", ExportOptions(), [])
    assert e.value.code == "no_jpg_source" and "RAW" in e.value.message
    assert not (tmp_path / "o").exists() or not any((tmp_path / "o").iterdir())


def test_renderer_plugs_into_export(lib, raw_only):
    shot, out, _ = raw_only
    red = Renderer("recipe-test", "测试配方", "", True, lambda c: np.full(c.shape, (200, 10, 10), np.uint8))
    res = export_shot(lib, shot, None, out, ExportOptions(None, 95, "png"), [], renderer=red)
    assert res.render == "recipe-test" and res.render_label == "测试配方" and res.path.name == "R0000201_gxr.png"
    assert np.asarray(Image.open(res.path).convert("RGB"))[5, 5].tolist() == [200, 10, 10]


# ---- HTTP -------------------------------------------------------------------


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        yield c


def wait_job(client, job):
    for _ in range(600):
        job = client.get(f"/api/jobs/{job['id']}").json()
        if not job["running"]:
            return job
        time.sleep(0.05)
    raise AssertionError("任务超时")


def test_api_raw_only_exports_instead_of_409(client, card, settings):
    for stem, hour in (("R0000109", 10), ("R0000110", 11)):  # 假卡里只有 DNG 的两张：换成真的带 CFA 的 DNG（内容要不同，否则会被去重）
        (card.root / "DCIM" / "100RICOH" / f"{stem}.DNG").write_bytes(scene_dng(when=f"2026:10:19 {hour}:00:00"))
    import_all(client)
    p = find(client, "R0000109", "2026-10-19")
    assert p["has_raw"] and not p["has_jpg"]
    r = client.post(f"/api/photos/{p['id']}/export", json={"long_edge": 64, "quality": 80})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["render"] == "neutral-preview" and body["render_label"] == "中性转换" and body["format"] == "jpeg"
    assert body["path"].endswith("R0000109_gxr_neutral.jpg") and (body["width"], body["height"]) == (64, 43)
    png = client.post(f"/api/photos/{p['id']}/export", json={"format": "png", "size": "original"}).json()
    assert png["format"] == "png" and (png["width"], png["height"]) == (96, 64)
    tiers = client.post(f"/api/photos/{p['id']}/export", json={"size": "small", "quality_tier": "low"}).json()
    assert (tiers["width"], tiers["height"]) == (96, 64)  # 比 1600 小，不放大
    assert client.post(f"/api/photos/{p['id']}/export", json={"format": "gif"}).json()["error"] == "bad_format"
    assert client.post(f"/api/photos/{p['id']}/export", json={"size": "huge"}).json()["error"] == "bad_size"
    # 批量：两张换成真 DNG 的 + 两张有 JPG 的成功；第三张只有 DNG（假的、读不出来，在 101RICOH）仍是 no_jpg_source
    job = wait_job(client, client.post("/api/export", json={"date": "2026-10-19", "long_edge": 200}).json())
    res = job["progress"]["results"]
    assert job["progress"]["state"] == "done"
    ok = [x for x in res if "path" in x]
    bad = [x for x in res if "error" in x]
    assert len(ok) == 4 and len(bad) == 1 and bad[0]["error"] == "no_jpg_source"
    assert sum(x["render"] == "neutral-preview" for x in ok) == 2  # R0000109、R0000110
    assert sum(x["render"] == "camera-jpg" for x in ok) == 2


# ---- 真实样本 ---------------------------------------------------------------

needs_sample = pytest.mark.skipif(not SAMPLE_DIR.is_dir(), reason="captures/a12-50 样本不在本机")


@needs_sample
def test_real_raw_only_xpan_export_jpeg_and_png(lib, tmp_path):
    rel = "2026/2026-10-16/R8051144.DNG"
    (lib.root / rel).parent.mkdir(parents=True)
    (lib.root / rel).write_bytes((SAMPLE_DIR / "R8051144.DNG").read_bytes())
    shot = {"shot_id": "r1", "name": "R8051144", "files": {"dng": {"relpath": rel}}}
    xpan = default_crop("xpan-65-24", 4288, 2848)
    t = time.time()
    res = export_shot(lib, shot, xpan, tmp_path / "o", ExportOptions(None, 90), [])
    assert (res.width, res.height) == (4288, 1583) and time.time() - t < 15
    im = Image.open(res.path)
    assert im.size == (4288, 1583) and im.info.get("icc_profile")
    assert im.getexif().get_ifd(0x8769)[34855] == 400
    png = export_shot(lib, shot, default_crop("3:2", 4288, 2848, angle_deg=3.0), tmp_path / "o",
                      ExportOptions.from_tiers("medium", fmt="png"), [])
    assert max(png.width, png.height) == 2048 and Image.open(png.path).format == "PNG"
    # 粗检：中性转换不黑不白，且比相机 JPG 暗（相机 JPG 烤进了色调曲线，中性转换没有；实测约 114 对 162）
    cam = Image.open(SAMPLE_DIR / "R8051144.JPG").convert("L").resize((200, 133))
    mine = Image.open(export_shot(lib, shot, None, tmp_path / "o", ExportOptions(800, 90), []).path).convert("L")
    mean_mine, mean_cam = np.asarray(mine.resize((200, 133))).mean(), np.asarray(cam).mean()
    assert 60 < mean_mine < mean_cam


@needs_sample
def test_api_real_raw_only_export(volumes, real_card, client):
    """真实样本去掉 JPG，只剩 DNG：原来 409，现在导出中性转换。"""
    (real_card.root / "DCIM" / "100RICOH" / "R8051145.JPG").unlink()
    import_all(client)
    p = find(client, "R8051145")
    assert p["has_raw"] and not p["has_jpg"]
    client.put(f"/api/photos/{p['id']}/crop", json={"preset": "xpan-65-24"})
    out = client.post(f"/api/photos/{p['id']}/export", json={}).json()
    assert (out["width"], out["height"]) == (4288, 1583) and out["render"] == "neutral-preview"
    assert out["path"].endswith("R8051145_gxr_neutral.jpg")
