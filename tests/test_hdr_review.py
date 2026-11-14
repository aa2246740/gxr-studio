"""HDR 复审回归：拍摄配对、局部对应关系与图库关闭。只使用合成照片。"""

from __future__ import annotations

import io
import json
import threading

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from scipy import ndimage

import hdr_support as hs
from conftest import PORT
from gxr_studio.api.routes import start_hdr_job
from gxr_studio.app import create_app
from gxr_studio.hdr import derived
from gxr_studio.hdr import gain as g
from gxr_studio.raw import open_dng
from raw_support import bayer_scene, build_dng
from support import samples
from support.fake_card import make_fake_card
from test_api import import_all

CAPTURE = {
    36867: "2000:01:01 12:00:00",
    37521: "123",
    33434: 0.01,
    33437: 2.5,
    34855: 200,
    42036: "RICOH GR LENS A12 50mm F2.5 MACRO",
    42016: "0123456789abcdef0123456789abcdef",
}


def capture_pair(jpeg_changes=None, dng_changes=None, *, rgb=None):
    rgb = hs.scene() if rgb is None else rgb
    jpg_fields = {**CAPTURE, **(jpeg_changes or {})}
    raw_fields = {**CAPTURE, **(dng_changes or {})}
    exif = Image.Exif()
    exif[271], exif[272] = "RICOH", jpg_fields.pop(272, "GXR A12")
    exif[34665] = {k: v for k, v in jpg_fields.items() if v is not None}
    buf = io.BytesIO()
    Image.fromarray(hs.srgb_bytes(rgb * hs.EXPOSURE)).save(buf, "JPEG", quality=95, exif=exif)
    rows = []
    for tag, value in raw_fields.items():
        if tag == 272:
            continue
        kind = "ASCII" if isinstance(value, str) or value is None else "SHORT" if tag == 34855 else "RATIONAL"
        rows.append((tag, kind, "" if value is None else value if kind == "ASCII" else [value]))
    raw = build_dng(
        bayer_scene(*rgb.shape[:2], rgb * np.array(hs.NEUTRAL)),
        white=4095,
        when=CAPTURE[36867],
        exif_entries=rows,
        root_extra=[(272, "ASCII", raw_fields.get(272, "GXR A12"))],
    )
    return buf.getvalue(), raw


def moving_pair(size, distance, *, multiple=False, quiet=False):
    first = hs.scene()
    if quiet:
        first[64:224, 360:568] = 0.22
    second = first.copy()
    first[96 : 96 + size, 420 : 420 + size] = 0.035
    second[96 : 96 + size, 420 + distance : 420 + distance + size] = 0.035
    if multiple:
        first[220:244, 270:294] = 0.015
        second[260:284, 300:324] = 0.015
    return hs.camera_jpeg(second), hs.camera_dng(first)


@pytest.mark.parametrize(
    "size,distance,multiple,quiet",
    [
        (8, 48, False, False),
        (16, 24, False, False),
        (24, 40, False, False),
        (32, 64, False, False),
        (40, 80, False, False),
        (64, 80, False, False),
        (20, 8, False, False),
        (40, 40, True, False),
        (16, 64, False, True),
        (40, 80, False, True),
    ],
)
def test_local_correspondence_must_be_verified(size, distance, multiple, quiet):
    jpeg, dng = moving_pair(size, distance, multiple=multiple, quiet=quiet)
    with pytest.raises(g.HdrUnsupported) as error:
        g.hdr_from_camera_jpeg(jpeg, g.raw_luma(open_dng(dng)))
    assert error.value.reason == "not_aligned"


@pytest.mark.parametrize("x,quiet", [(180, False), (180, True), (300, False), (300, True)])
def test_small_local_difference_is_not_hidden_by_the_background(x, quiet):
    first = hs.scene()
    if quiet:
        first[64:224, x - 20 : x + 140] = 0.22
    second = first.copy()
    first[96:104, x : x + 8] = 0.035
    second[96:104, x + 24 : x + 32] = 0.035
    with pytest.raises(g.HdrUnsupported) as error:
        g.hdr_from_camera_jpeg(hs.camera_jpeg(second), g.raw_luma(open_dng(hs.camera_dng(first))))
    assert error.value.reason == "not_aligned"


@pytest.mark.parametrize("size", [8, 16])
def test_short_motion_with_low_contrast_is_not_supported(size):
    first = hs.scene()
    second = first.copy()
    first[96 : 96 + size, 180 : 180 + size] = 0.035
    second[96 : 96 + size, 188 : 188 + size] = 0.035
    with pytest.raises(g.HdrUnsupported) as error:
        g.hdr_from_camera_jpeg(hs.camera_jpeg(second), g.raw_luma(open_dng(hs.camera_dng(first))))
    assert error.value.reason == "not_aligned"


def test_global_translation_uses_a_fixed_search_center():
    big = hs.scene(400, 600, seed=2)
    raw = g.raw_luma(open_dng(hs.camera_dng(big[8:392, 8:584])))
    sdr = g.sdr_luma_grid(g.decode_rgb(hs.camera_jpeg(big[4:388, 12:588])), 4)
    al = g.align(sdr, raw.y)
    assert (al.scale, al.dy, al.dx) == (1.0, 1, -1)
    g.check_alignment(al)


def test_small_radial_correction_is_not_supported():
    from test_hdr import STREET

    dng = STREET / "dng/R8051530.DNG"
    if not dng.is_file():
        samples.missing("MOUNT 样本不在本机")
    raw = g.raw_luma(open_dng(dng))
    sdr = g.sdr_luma_grid(g.decode_rgb((STREET / "jpg/R8051530.JPG").read_bytes()), 4)
    h, w = sdr.shape
    yy, xx = np.mgrid[:h, :w].astype(np.float32)
    rr = ((xx - (w - 1) / 2) / (w / 2)) ** 2 + ((yy - (h - 1) / 2) / (h / 2)) ** 2
    sx = (xx - (w - 1) / 2) * (1 + 0.002 * rr) + (w - 1) / 2
    sy = (yy - (h - 1) / 2) * (1 + 0.002 * rr) + (h - 1) / 2
    corrected = ndimage.map_coordinates(sdr, [sy, sx], order=1, mode="nearest")
    with pytest.raises(g.HdrUnsupported) as error:
        g.check_alignment(g.align(corrected, raw.y))
    assert error.value.reason == "not_aligned"


@pytest.mark.parametrize(
    "tag,value",
    [
        (36867, "2000:01:02 12:00:00"),
        (37521, "124"),
        (33434, 0.02),
        (33437, 4.0),
        (34855, 400),
        (272, "GXR OTHER"),
        (42036, "RICOH GXR MOUNT A12"),
        (42016, "abcdef0123456789abcdef0123456789"),
    ],
)
def test_capture_metadata_difference_rejects_the_pair(tag, value):
    jpeg, dng = capture_pair({tag: value})
    with pytest.raises(g.HdrUnsupported, match="不是同一次拍摄") as error:
        g.hdr_from_camera_jpeg(jpeg, g.raw_luma(open_dng(dng)))
    assert error.value.reason == "capture_mismatch"


@pytest.mark.parametrize("missing", [{37521: None}, {33437: None, 42036: None}, dict.fromkeys(CAPTURE)])
@pytest.mark.parametrize("side", ["jpeg", "dng", "both"])
def test_missing_capture_fields_still_require_and_pass_alignment(missing, side):
    jpeg, dng = capture_pair(missing if side != "dng" else None, missing if side != "jpeg" else None)
    _out, stats = g.hdr_from_camera_jpeg(jpeg, g.raw_luma(open_dng(dng)))
    assert stats["alignment"]["correlation"] > 0.9


def test_equivalent_capture_values_are_compared_after_normalization():
    jpeg, dng = capture_pair({37521: "1230", 42036: "  RICOH GR LENS A12 50mm F2.5 MACRO  "})
    _out, stats = g.hdr_from_camera_jpeg(jpeg, g.raw_luma(open_dng(dng)))
    assert stats["alignment"]["correlation"] > 0.9


def test_missing_metadata_still_rejects_local_motion():
    first = hs.scene()
    second = first.copy()
    first[96:136, 420:460] = 0.035
    second[96:136, 500:540] = 0.035
    jpeg, _ = capture_pair(dict.fromkeys(CAPTURE), rgb=second)
    _, dng = capture_pair(dng_changes=dict.fromkeys(CAPTURE), rgb=first)
    with pytest.raises(g.HdrUnsupported) as error:
        g.hdr_from_camera_jpeg(jpeg, g.raw_luma(open_dng(dng)))
    assert error.value.reason == "not_aligned"


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        yield c


@pytest.mark.parametrize("case", ["motion", "capture"])
def test_import_and_export_decline_unmatched_pair(client, volumes, case):
    card = make_fake_card(volumes, pairs=0, jpg_only=0)
    folder = card.root / "DCIM/100RICOH"
    jpeg, dng = moving_pair(40, 80) if case == "motion" else capture_pair({36867: "2000:01:02 12:00:00"})
    (folder / "SAME0001.JPG").write_bytes(jpeg)
    (folder / "SAME0001.DNG").write_bytes(dng)
    imported = import_all(client)
    assert imported["error"] is None and imported["progress"]["state"] == "done"
    ctx = client.app.state.ctx
    jobs = [job for job in ctx.jobs.all() if job.kind == "hdr"]
    assert len(jobs) == 1
    jobs[0].thread.join(timeout=10)
    assert not jobs[0].running and jobs[0].error is None
    (photo,) = client.get("/api/photos").json()
    assert photo["hdr"] == "unsupported"
    reason = json.loads(derived.status_path(ctx.lib, photo["id"]).read_text(encoding="utf-8"))
    assert reason["reason"] == ("not_aligned" if case == "motion" else "capture_mismatch")
    if case == "capture":
        assert reason["detail"] == "RAW 与 JPG 不是同一次拍摄"
    assert not derived.hdr_path(ctx.lib, photo["id"]).exists()
    result = client.post(f"/api/photos/{photo['id']}/export", json={"hdr": True}).json()
    assert result["hdr"] is False and result["hdr_note"]
    if case == "capture":
        assert result["hdr_note"] == "RAW 与 JPG 不是同一次拍摄"


def test_shutdown_cancels_hdr_and_keeps_library_open_until_exit(settings, monkeypatch):
    app = create_app(settings)
    ctx = app.state.ctx
    entered = threading.Event()
    observations = {}
    original_list = ctx.lib.list_shots

    def shots(*_a, **_kw):
        return [] if _kw.get("offset", 0) else [{"shot_id": "ab" * 32, "files": {"jpg": {}, "dng": {}}}]

    def generate(lib, _shot, _params):
        job = ctx.jobs.active("hdr")
        entered.set()
        observations["cancelled"] = job.cancel.wait(timeout=2)
        observations["library_open"] = lib._db is not None
        original_list()
        return "ready"

    monkeypatch.setattr(ctx.lib, "list_shots", shots)
    monkeypatch.setattr(derived, "generate", generate)
    with TestClient(app):
        start_hdr_job(ctx)
        (job,) = ctx.jobs.all()
        assert entered.wait(timeout=2)
    job.thread.join(timeout=5)
    assert not job.running
    assert observations == {"cancelled": True, "library_open": True}
    assert job.error is None and job.progress.state == "cancelled"
    start_hdr_job(ctx)
    assert len(ctx.jobs.all()) == 1


def test_enumeration_failure_finishes_progress_and_reports_job_failure(lib, monkeypatch):
    from gxr_studio.jobs import JobRegistry

    def unreadable(**_kw):
        raise OSError("图库暂时不可读")

    monkeypatch.setattr(lib, "list_shots", unreadable)
    registry = JobRegistry()
    notified = []
    registry.set_alert_handler(lambda job: notified.append(job.id))
    progress = derived.HdrProgress()
    job = registry.start("hdr", progress, lambda job: derived.run_pending(lib, progress, job.cancel))
    job.thread.join(timeout=2)
    assert not job.running and job.error is not None
    assert progress.state == "failed"
    assert notified == [job.id]


def test_older_hdr_cache_requires_a_new_check(lib):
    sid = "ab" * 8
    folder = lib.studio / "hdr" / sid[:2]
    folder.mkdir(parents=True)
    (folder / f"{sid}-v1.jpg").write_bytes(b"previous version")
    shot = {"shot_id": sid, "files": {"jpg": {}, "dng": {}}}
    assert derived.status_of(lib, shot) == "pending"
