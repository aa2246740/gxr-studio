"""MOUNT 单元的 DNG：装手动镜头时有理数分母为 0。

真机文件（captures/street-g137-20261031）里根 IFD 的 LensInfo 是 0/100, 0/100, 0/0, 0/0，
EXIF 的 FNumber 是 0/100、FocalLength 是 0/100。这里全部用合成的小 DNG，不含理光字节。
"""

from __future__ import annotations

import json
import math
from fractions import Fraction

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, TiffImagePlugin

from conftest import PORT
from gxr_studio.app import create_app
from gxr_studio.export import ExportOptions, export_shot
from gxr_studio.finish.meta_text import _ratio, meta_payload, photo_meta
from gxr_studio.lut import scene
from gxr_studio.meta import read_dng_meta
from gxr_studio.raw import RawDecodeError, open_dng, render_half, wb_gains
from gxr_studio.raw.tiff import Tiff
from raw_support import bayer_scene, build_dng
from test_api import find, import_all

LENS_INFO_MOUNT = (50736, "RATIONAL", [(0, 100), (0, 100), (0, 0), (0, 0)])
MOUNT_EXIF = [
    (33434, "RATIONAL", [(1, 60)]),
    (33437, "RATIONAL", [(0, 100)]),  # FNumber 0：相机不知道光圈
    (37380, "SRATIONAL", [(0, 10)]),
    (37386, "RATIONAL", [(0, 100)]),  # FocalLength 0
]


def mount_dng(*, exif=MOUNT_EXIF, root=(LENS_INFO_MOUNT,), w=32, h=24, **kw) -> bytes:
    yy, xx = np.mgrid[0:h, 0:w].astype(float)
    rgb = np.stack([0.2 + 0.5 * xx / w, 0.3 + 0.4 * yy / h, 0.6 - 0.4 * xx / w], axis=-1)
    kw.setdefault("lens", "GXR MOUNT A12")
    kw.setdefault("neutral", (0.645161, 1.0, 0.574712))
    return build_dng(
        bayer_scene(h, w, rgb, white=4095),
        white=4095,
        active=(0, 0, h, w),
        exif_entries=exif,
        root_extra=root,
        **kw,
    )


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        yield c


def _library_shot(lib, data: bytes, stem: str = "R8051530"):
    rel = f"2026/2026-10-31/{stem}.DNG"
    path = lib.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"shot_id": stem, "name": stem, "files": {"dng": {"relpath": rel}}}, path


# ---- TIFF 读取器 --------------------------------------------------------------


def test_zero_denominator_reads_as_nan_not_an_exception():
    t = Tiff(mount_dng(root=(LENS_INFO_MOUNT, (50730, "SRATIONAL", [(3, 0)]))))
    root = t.ifd(t.root)
    lens_info = root[50736]["value"]
    assert lens_info[:2] == [0.0, 0.0] and all(math.isnan(v) for v in lens_info[2:])
    assert math.isnan(root[50730]["value"][0])  # SRATIONAL 同样处理，分子非 0 也一样
    assert root[50736]["raw"][-8:] == b"\0" * 8, "原始字节保持不变"


# ---- DNG ---------------------------------------------------------------------


def test_mount_dng_opens_and_renders():
    d = open_dng(mount_dng())
    assert d.exif["f"] == 0.0 and d.exif["t"] == pytest.approx(1 / 60) and d.exif["iso"] == 200
    assert render_half(d).shape[:2] == (12, 16)
    json.dumps(d.summary(), allow_nan=False)


def test_unknown_exif_rationals_become_none_in_the_summary():
    d = open_dng(mount_dng(exif=[(33434, "RATIONAL", [(0, 0)]), (33437, "RATIONAL", [(0, 0)]), (37380, "SRATIONAL", [(0, 0)])]))
    assert d.exif["t"] is None and d.exif["f"] is None and d.exif["bias_ev"] is None
    assert "NaN" not in json.dumps(d.summary(), allow_nan=False)


@pytest.mark.parametrize(
    ("where", "entry"),
    [
        ("root", (50730, "SRATIONAL", [(0, 0)])),  # BaselineExposure
        ("root", (50727, "RATIONAL", [(1, 1), (0, 0), (1, 1)])),  # AnalogBalance
    ],
)
def test_render_critical_rationals_with_zero_denominator_are_refused(where, entry):
    with pytest.raises(RawDecodeError, match="zero denominator"):
        open_dng(mount_dng(root=(LENS_INFO_MOUNT, entry)))


def test_unknown_white_balance_is_refused_in_plain_words():
    d = open_dng(mount_dng(neutral=((0, 0), 1.0, 0.5)))
    with pytest.raises(RawDecodeError, match="白平衡"):
        wb_gains(d)
    with pytest.raises(scene.SceneError) as e:
        scene.scene_colour(d)
    assert e.value.code == "no_neutral"


def test_colour_matrix_with_zero_denominator_is_refused():
    # 复审 P1-08：标签存在但分母为 0 是"坏的"，和"没有这个标签"分开报
    bad = (50721, "SRATIONAL", [(0, 0)] * 9)
    with pytest.raises(scene.SceneError) as e:
        scene.scene_colour(open_dng(mount_dng(root=(LENS_INFO_MOUNT, bad))))
    assert e.value.code == "bad_matrix"


# ---- 元数据、显示、导出 ---------------------------------------------------------


def test_mount_meta_reads_without_error(tmp_path):
    path = tmp_path / "R8051530.DNG"
    path.write_bytes(mount_dng())
    m = read_dng_meta(path)
    assert m.error is None and m.lens == "GXR MOUNT A12" and m.iso == 200


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (0, None),
        (0.0, None),
        (-1.0, None),
        (math.nan, None),
        (math.inf, None),
        (TiffImagePlugin.IFDRational(0, 100), None),
        (TiffImagePlugin.IFDRational(0, 0), None),
        (TiffImagePlugin.IFDRational(1, 0), None),
        (TiffImagePlugin.IFDRational(5, 2), 2.5),
        (Fraction(1, 60), pytest.approx(1 / 60)),
        ("abc", None),
    ],
)
def test_ratio_treats_zero_and_non_finite_as_unknown(value, expected):
    assert _ratio(value) == expected


@pytest.mark.parametrize(("num", "den", "expected"), [(3, 0, None), (5, 2, 2.5), (0, 7, None)])
def test_ratio_with_a_bare_numerator_and_denominator(num, den, expected):
    class Bare:  # 有 numerator/denominator、但 float() 不认的对象
        numerator, denominator = num, den

    assert _ratio(Bare()) == expected


@pytest.mark.parametrize(
    ("exif", "aperture", "shutter"),
    [
        (MOUNT_EXIF, None, "1/60s"),
        ([(33434, "RATIONAL", [(0, 0)]), (33437, "RATIONAL", [(0, 0)])], None, None),
        ([(33434, "RATIONAL", [(0, 1)]), (33437, "RATIONAL", [(5, 0)])], None, None),
    ],
)
def test_mount_display_omits_unknown_aperture_and_shutter(lib, exif, aperture, shutter):
    shot, _ = _library_shot(lib, mount_dng(exif=exif))
    payload = meta_payload(lib, shot)
    assert payload["aperture"] == aperture and payload["shutter"] == shutter
    assert payload["lens"] == "GXR MOUNT A12" and payload["iso"] == 200
    info, _, _ = photo_meta(lib, shot)
    text = " ".join(info.values()).lower()
    assert "f/0" not in text and "inf" not in text and "nan" not in text
    json.dumps(payload, allow_nan=False)


def test_mount_raw_only_export_keeps_exif(lib, tmp_path):
    shot, _ = _library_shot(lib, mount_dng(exif=[*MOUNT_EXIF, (33437, "RATIONAL", [(0, 0)])]))
    res = export_shot(lib, shot, None, tmp_path / "out", ExportOptions(None, 90), [])
    sub = Image.open(res.path).getexif().get_ifd(0x8769)
    assert sub[34855] == 200 and sub[42036] == "GXR MOUNT A12"
    assert float(sub[33434]) == pytest.approx(1 / 60), "完整 EXIF 写进去了，没有退到精简版"


def test_api_meta_for_a_mount_dng(client, card):
    (card.root / "DCIM" / "100RICOH" / "R0000109.DNG").write_bytes(mount_dng(when="2026:10:19 10:00:00"))
    import_all(client)
    p = find(client, "R0000109", "2026-10-19")
    r = client.get(f"/api/photos/{p['id']}/meta")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["aperture"] is None and body["shutter"] == "1/60s" and body["lens"] == "GXR MOUNT A12"
