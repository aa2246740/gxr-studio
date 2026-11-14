"""导出只编码一次：成片像素（编码之前的 uint8 数组）直接写进最终文件，HDR 增益也在这份数组上拟合。

旧的顺序冻结在 `support/export_once_legacy.py`：预览字节、没有编辑的相机 JPG 原尺寸导出，都要和旧代码逐字节相同。
真实样本只从 `GXR_STUDIO_TEST_PARENT` 读，不复制进仓库。
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image, JpegImagePlugin

import hdr_support as hs
from gxr_studio.export import ExportError, ExportOptions, _finish, source_exif, write_rgb_with_exif
from gxr_studio.finish import hdr_export
from gxr_studio.finish.hdr_export import HdrTrack, _to_grid
from gxr_studio.finish.serve import _fit, export_finish, render_stage, render_stage_rgb
from gxr_studio.finish.spec import FrameSpec, StampSpec
from gxr_studio.hdr.gain import sdr_luma_grid
from gxr_studio.raw import Renderer
from gxr_studio.render.params import Adjust, EditState, RecipeSel
from gxr_studio.render.service import RenderService
from support.export_once_legacy import legacy_export_payload, legacy_render_stage

STUDIO = Path(__file__).resolve().parents[1]
LONG_EDGE = 200
CROP = {"crop": {"rect": [0.05, 0.08, 0.85, 0.8], "preset": "free"}}


def _recipe_display(codes: np.ndarray) -> np.ndarray:
    """测试用的"配方"：逐像素、和位置无关，和中性转换不一样（更亮、偏暖）。"""
    v = np.clip(codes / np.float32(4095.0) * np.float32(1.6), 0.0, 1.0) ** np.float32(1 / 2.2)
    return np.rint(v * np.array([255.0, 240.0, 215.0], np.float32)).astype(np.uint8)


TEST_RECIPE = Renderer("recipe-test", "测试配方", "_recipe", True, _recipe_display, description="GXR Studio recipe-test")


@pytest.fixture
def studio(lib, settings, monkeypatch):
    """图库里一对 RAW+JPG、一对方向 6 要转正的、一张只有 RAW 的；配方渲染换成 `TEST_RECIPE`（不需要相机数据）。"""
    folder = lib.root / "2026" / "2026-10-31"
    folder.mkdir(parents=True)
    for stem, seed, orientation in (("PAIR0001", 0, 1), ("TURN0001", 7, 6)):  # 这两个种子的合成画面对得齐
        jpg, dng = hs.pair(seed, orientation=orientation)
        (folder / f"{stem}.JPG").write_bytes(jpg)
        (folder / f"{stem}.DNG").write_bytes(dng)
    _jpg, raw_only = hs.pair(5)
    (folder / "RAW00001.DNG").write_bytes(raw_only)
    rel = "2026/2026-10-31/"
    shots = {
        "pair": {
            "shot_id": "pair",
            "name": "PAIR0001",
            "width": 576,
            "height": 384,
            "files": {"jpg": {"relpath": rel + "PAIR0001.JPG"}, "dng": {"relpath": rel + "PAIR0001.DNG"}},
        },
        "turned": {
            "shot_id": "turned",
            "name": "TURN0001",
            "width": 384,
            "height": 576,
            "files": {"jpg": {"relpath": rel + "TURN0001.JPG"}, "dng": {"relpath": rel + "TURN0001.DNG"}},
        },
        "raw": {"shot_id": "raw", "name": "RAW00001", "width": 576, "height": 384, "files": {"dng": {"relpath": rel + "RAW00001.DNG"}}},
    }
    monkeypatch.setattr(RenderService, "renderer", lambda self, shot, edit, precise=False: TEST_RECIPE)
    return RenderService(settings.home_dir, lib), lib, shots, settings.export_dir


EDITED = EditState(
    adjust=Adjust(exposure_ev=0.3, contrast=12),
    frame=FrameSpec(style="gallery", output_ratio="original"),
    stamp=StampSpec(enabled=True, date_override="2026-10-31"),
)
# (名字, 照片, 编辑, sidecar, 底图)
CASES = [
    ("raw-recipe", "pair", EditState(recipe=RecipeSel("jlaunch", 1), adjust=Adjust(contrast=8)), CROP, "auto"),
    ("raw-neutral", "raw", EditState(adjust=Adjust(exposure_ev=-0.2)), CROP, "neutral"),
    ("raw-standard", "raw", EditState(), {}, "auto"),
    ("jpeg-edited", "pair", EDITED, CROP, "auto"),
    ("camera-untouched", "pair", EditState(), {}, "auto"),
    ("turned-edited", "turned", EDITED, CROP, "auto"),
    ("turned-untouched", "turned", EditState(), {}, "auto"),
]

IDS = [c[0] for c in CASES]


def _case(studio, name):
    render, lib, shots, out = studio
    _n, which, edit, side, base = next(c for c in CASES if c[0] == name)
    return render, lib, shots[which], edit, side, base, out


def _export(studio, name, *, long_edge=None, fmt="jpeg", hdr=False, quality=95):
    render, lib, shot, edit, side, base, out = _case(studio, name)
    res = export_finish(render, lib, shot, edit, side, out, [], quality=quality, long_edge=long_edge, fmt=fmt, base=base, hdr=hdr)
    return res, res.path.read_bytes()


def _expected(studio, name, *, long_edge=None, quality=95, track=None):
    """规格的写法：`_fit(render_stage_rgb(...)[0], long_edge)`，以及要写进 EXIF 的说明。"""
    render, lib, shot, edit, side, base, _out = _case(studio, name)
    rgb, labels = render_stage_rgb(render, lib, shot, edit, side, stage="final", max_edge=None, quality=quality, base=base, track=track)
    note = labels.get("description") or labels.get("text") or "原图"
    files = shot["files"]
    source = lib.abspath(files["jpg" if "jpg" in files else "dng"]["relpath"])
    return _fit(rgb, long_edge), note, source


class _Saves:
    """数 `PIL.Image.Image.save` 里格式是 JPEG 的调用：照片（RGB）和增益图（L）分开数。"""

    def __init__(self, monkeypatch):
        self.rgb = 0
        self.gain = 0
        real = Image.Image.save

        def save(im, fp, format=None, **params):  # noqa: A002  和 Pillow 的参数名一致
            fmt = (format or "").upper()
            if fmt == "JPEG":
                if im.mode == "L":
                    self.gain += 1
                else:
                    self.rgb += 1
            return real(im, fp, format, **params)

        monkeypatch.setattr(Image.Image, "save", save)


# ---------------------------------------------------------------- 1. 每次导出只编码一次 JPEG


@pytest.mark.parametrize("long_edge", [None, LONG_EDGE])
@pytest.mark.parametrize("name", IDS)
def test_each_export_encodes_the_photo_exactly_once(studio, monkeypatch, name, long_edge):
    render, lib, shot, edit, side, base, _out = _case(studio, name)
    saves = _Saves(monkeypatch)
    legacy_export_payload(render, lib, shot, edit, side, quality=95, long_edge=long_edge, fmt="jpeg", base=base)
    old = saves.rgb
    saves.rgb = 0
    _export(studio, name, long_edge=long_edge)
    assert (saves.rgb, saves.gain) == (1, 0)
    # 计数器本身是对的：旧顺序在有编辑或有长边时会多编码
    assert old == (1 if name.endswith("-untouched") else 2) + (1 if long_edge else 0)


@pytest.mark.parametrize("long_edge", [None, LONG_EDGE])
@pytest.mark.parametrize("name", ["jpeg-edited", "camera-untouched", "raw-neutral", "turned-edited"])
def test_hdr_export_encodes_the_photo_once_and_the_gain_map_once(studio, monkeypatch, name, long_edge):
    saves = _Saves(monkeypatch)
    res, _data = _export(studio, name, long_edge=long_edge, hdr=True)
    assert res.hdr is True, res.hdr_reason
    assert (saves.rgb, saves.gain) == (1, 1)


def test_png_export_never_encodes_a_jpeg(studio, monkeypatch):
    saves = _Saves(monkeypatch)
    for name in IDS:
        _export(studio, name, fmt="png", long_edge=LONG_EDGE)
    assert (saves.rgb, saves.gain) == (0, 0)


# ---------------------------------------------------------------- 2. 字节和 `_finish(编码之前的数组)` 相同


@pytest.mark.parametrize("fmt", ["jpeg", "png"])
@pytest.mark.parametrize("quality", [95, 85])
@pytest.mark.parametrize("long_edge", [None, LONG_EDGE])
@pytest.mark.parametrize("name", IDS)
def test_export_bytes_are_finish_of_the_pre_encode_array(studio, name, long_edge, quality, fmt):
    res, data = _export(studio, name, long_edge=long_edge, quality=quality, fmt=fmt)
    rgb, note, source = _expected(studio, name, long_edge=long_edge, quality=quality)
    base, sub = source_exif(source, note)
    want, size = _finish(Image.fromarray(rgb, "RGB"), base, sub, None, ExportOptions(None, quality, fmt))
    assert data == want
    assert (res.width, res.height) == size == (rgb.shape[1], rgb.shape[0])
    assert data == write_rgb_with_exif(rgb, source, note, quality=quality, fmt=fmt)
    with Image.open(io.BytesIO(data)) as im:
        exif = im.getexif()
        assert exif[274] == 1 and exif[305] == "GXR Studio"
        assert exif.get_ifd(0x8769)[40962] == rgb.shape[1] and exif.get_ifd(0x8769)[40963] == rgb.shape[0]
        if fmt == "jpeg":
            assert im.info.get("progressive", 0) == 0
            # 4:4:4 在质量 ≥ 90，否则 4:2:0；和 `_finish` 一样
            assert JpegImagePlugin.get_sampling(im) == (0 if quality >= 90 else 2)


def test_long_edge_is_lanczos_on_the_array(studio):
    rgb, _note, _src = _expected(studio, "jpeg-edited")
    fitted, _note, _src = _expected(studio, "jpeg-edited", long_edge=LONG_EDGE)
    assert max(fitted.shape[:2]) == LONG_EDGE
    h, w = rgb.shape[:2]
    from gxr_studio.finish.layout import fit_size

    want = np.asarray(Image.fromarray(rgb, "RGB").resize(fit_size(w, h, LONG_EDGE), Image.Resampling.LANCZOS))
    assert np.array_equal(fitted, want)


# ---------------------------------------------------------------- 3. 回归：预览字节、相机 JPG 原样导出、PNG


def _outcome(fn, *args, **kw):
    """(JPEG 字节, labels)，或者抛出的错误（类型和文字）。只有 RAW 的小预览要相机数据，测试环境里两边都抛同一个错。"""
    try:
        return fn(*args, **kw)
    except Exception as exc:  # noqa: BLE001  两边抛的错也要一样
        return type(exc), str(exc)


@pytest.mark.parametrize("stage", ["final", "uncropped", "original"])
@pytest.mark.parametrize("max_edge", [None, 1600, 240, 64])
@pytest.mark.parametrize("name", IDS)
def test_preview_bytes_are_unchanged(studio, name, max_edge, stage):
    render, lib, shot, edit, side, base, _out = _case(studio, name)
    kw: dict[str, Any] = {"stage": stage, "max_edge": max_edge, "quality": 88, "base": base}
    new = _outcome(render_stage, render, lib, shot, edit, side, **kw)
    old = _outcome(legacy_render_stage, render, lib, shot, edit, side, **kw)
    assert new == old
    # 只有 RAW 的小预览（以及配方的小预览）走渲染服务，要相机数据；其余必须真出了图
    if max_edge is None or (base == "auto" and "jpg" in shot["files"] and edit.recipe is None):
        assert isinstance(new[0], bytes) and new[0][:2] == b"\xff\xd8"


def test_preview_with_an_hdr_track_is_unchanged(studio):
    render, lib, shot, edit, side, base, _out = _case(studio, "jpeg-edited")
    tracks = [HdrTrack(lib.abspath(shot["files"]["dng"]["relpath"])) for _ in range(2)]
    kw: dict[str, Any] = {"stage": "final", "max_edge": None, "quality": 95, "base": base}
    assert render_stage(render, lib, shot, edit, side, track=tracks[0], **kw) == legacy_render_stage(
        render, lib, shot, edit, side, track=tracks[1], **kw
    )
    assert tracks[0].layer is not None and np.array_equal(tracks[0].layer, tracks[1].layer, equal_nan=True)


@pytest.mark.parametrize("quality", [95, 85, 70])
@pytest.mark.parametrize("fmt", ["jpeg", "png"])
@pytest.mark.parametrize("name", ["camera-untouched", "turned-untouched"])
def test_untouched_camera_jpg_export_is_byte_identical(studio, fmt, quality, name):
    render, lib, shot, edit, side, base, _out = _case(studio, name)
    res, data = _export(studio, name, fmt=fmt, quality=quality)
    old, labels, _hdr = legacy_export_payload(render, lib, shot, edit, side, quality=quality, long_edge=None, fmt=fmt, base=base)
    assert data == old
    assert res.render == "camera-jpg" and labels["id"] == "camera-jpg"
    assert (res.width, res.height) == (shot["width"], shot["height"])  # 转正以后的尺寸，方向 6 的是竖的


@pytest.mark.parametrize("long_edge", [None, LONG_EDGE])
@pytest.mark.parametrize("name", IDS)
def test_png_decodes_to_the_array_exactly(studio, name, long_edge):
    _res, data = _export(studio, name, fmt="png", long_edge=long_edge)
    rgb, _note, _src = _expected(studio, name, long_edge=long_edge)
    with Image.open(io.BytesIO(data)) as im:
        assert im.format == "PNG"
        assert np.array_equal(np.asarray(im.convert("RGB")), rgb)


@pytest.mark.parametrize("name", ["jpeg-edited", "raw-recipe", "turned-edited"])
def test_the_single_encode_is_closer_to_the_edit_than_the_old_chain(studio, name):
    """旧顺序先 4:2:0 再 4:4:4；新的只有一次 4:4:4。新文件解码后离编辑出来的像素更近。"""
    render, lib, shot, edit, side, base, _out = _case(studio, name)
    rgb, _note, _src = _expected(studio, name)
    _res, data = _export(studio, name)
    old, _labels, _hdr = legacy_export_payload(render, lib, shot, edit, side, quality=95, long_edge=None, fmt="jpeg", base=base)

    def err(blob):
        with Image.open(io.BytesIO(blob)) as im:
            return np.abs(np.asarray(im.convert("RGB")).astype(int) - rgb.astype(int))

    new_err, old_err = err(data), err(old)
    assert new_err.mean() < old_err.mean()
    assert new_err.max() <= old_err.max()


# ---------------------------------------------------------------- 4. HDR：增益在数组上拟合


@pytest.mark.parametrize("long_edge", [None, LONG_EDGE])
@pytest.mark.parametrize("name", ["jpeg-edited", "camera-untouched", "raw-neutral", "raw-recipe", "turned-edited", "turned-untouched"])
def test_hdr_gain_is_fitted_on_the_pre_encode_array(studio, monkeypatch, name, long_edge):
    seen: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
    real = hdr_export.compute_gain

    def spy(sdr_y, raw_y, clip, p):
        seen.append((sdr_y, raw_y, clip))
        return real(sdr_y, raw_y, clip, p)

    monkeypatch.setattr(hdr_export, "compute_gain", spy)
    res, data = _export(studio, name, long_edge=long_edge, hdr=True)
    assert res.hdr is True, res.hdr_reason
    render, lib, shot, _edit, _side, _base, _out = _case(studio, name)
    files = shot["files"]
    track = HdrTrack(lib.abspath(files["dng"]["relpath"]), jpeg=lib.abspath(files["jpg"]["relpath"]) if "jpg" in files else None)
    rgb, _note, _src = _expected(studio, name, long_edge=long_edge, track=track)
    assert track.layer is not None
    factor = track.params.map_factor
    want_raw_y, want_clip = _to_grid(track.layer, (rgb.shape[1], rgb.shape[0]), factor)
    [(sdr_y, raw_y, clip)] = seen
    assert np.array_equal(sdr_y, sdr_luma_grid(rgb, factor))
    assert np.array_equal(raw_y, want_raw_y, equal_nan=True) and np.array_equal(clip, want_clip)
    # 主图就是普通导出的那份字节
    plain = write_rgb_with_exif(rgb, _src, _note, quality=95, fmt="jpeg")
    with Image.open(io.BytesIO(data)) as im:
        decoded = np.asarray(im.convert("RGB"))
    with Image.open(io.BytesIO(plain)) as im:
        assert np.array_equal(decoded, np.asarray(im.convert("RGB")))
    assert not np.array_equal(sdr_luma_grid(decoded, factor), sdr_y)  # 以前拟合的是这个


@pytest.mark.parametrize("fmt", ["jpeg", "png"])
@pytest.mark.parametrize("long_edge", [None, LONG_EDGE])
@pytest.mark.parametrize("name", IDS)
def test_hdr_outcome_and_reasons_are_the_same_as_before(studio, name, long_edge, fmt):
    render, lib, shot, edit, side, base, _out = _case(studio, name)
    res, _data = _export(studio, name, long_edge=long_edge, fmt=fmt, hdr=True)
    _old, _labels, info = legacy_export_payload(render, lib, shot, edit, side, quality=95, long_edge=long_edge, fmt=fmt, base=base, hdr=True)
    assert (res.hdr, res.hdr_reason) == (info["hdr"], info.get("reason"))
    if fmt == "png":
        assert res.hdr is False and res.hdr_reason == "not_jpeg"
    else:
        assert res.hdr is True and res.path.name.endswith("_hdr.jpg")


def test_hdr_reasons_without_raw_and_with_a_bad_frame_are_unchanged(studio, monkeypatch):
    render, lib, shots, out = studio
    folder = lib.root / "2026" / "2026-10-31"
    jpg, _dng = hs.pair(6)
    (folder / "ONLYJPG1.JPG").write_bytes(jpg)
    only = {"shot_id": "only", "name": "ONLYJPG1", "width": 576, "height": 384, "files": {"jpg": {"relpath": "2026/2026-10-31/ONLYJPG1.JPG"}}}
    res = export_finish(render, lib, only, EDITED, CROP, out, [], quality=95, long_edge=None, fmt="jpeg", hdr=True)
    assert (res.hdr, res.hdr_reason) == (False, "no_raw") and res.path.name == "ONLYJPG1_gxr.jpg"
    monkeypatch.setattr(hdr_export, "apply_frame", lambda rgb, spec, meta, logos=None, logo_images=None: np.zeros((20, 20, 3), np.uint8))
    res = export_finish(render, lib, shots["pair"], EDITED, CROP, out, [], quality=95, long_edge=None, fmt="jpeg", hdr=True)
    assert (res.hdr, res.hdr_reason) == (False, "frame_layout") and res.path.name == "PAIR0001_gxr.jpg"


# ---------------------------------------------------------------- 真实样本（GXR_STUDIO_TEST_PARENT）


def _real_pairs(limit: int) -> list[Path]:
    parent = Path(os.environ.get("GXR_STUDIO_TEST_PARENT", STUDIO.parent))
    root = parent / "captures" / "a12-50"
    if not root.is_dir():
        return []
    jpgs = sorted(p for p in root.rglob("*.JPG") if p.with_suffix(".DNG").is_file())
    return jpgs[:: max(1, len(jpgs) // limit)][:limit] if jpgs else []


@pytest.fixture(scope="module")
def real_pairs():
    pairs = _real_pairs(2)
    if not pairs:
        pytest.skip("没有现场 a12-50 样本")
    return pairs


@pytest.mark.parametrize("index", [0, 1])
def test_real_photo_preview_and_untouched_export_are_unchanged(lib, settings, real_pairs, index):
    import shutil

    if index >= len(real_pairs):
        pytest.skip("样本不够")
    src = real_pairs[index]
    folder = lib.root / "real"
    folder.mkdir()
    shutil.copyfile(src, folder / "REAL.JPG")
    shot = {"shot_id": "real", "name": "REAL", "files": {"jpg": {"relpath": "real/REAL.JPG"}}}
    with Image.open(folder / "REAL.JPG") as im:
        w, h = im.size
    shot.update(width=w, height=h)
    render = RenderService(settings.home_dir, lib)
    plain = EditState()
    for edit, side in ((plain, {}), (EDITED, CROP)):
        for max_edge in (1600, 240):
            kw: dict[str, Any] = {"stage": "final", "max_edge": max_edge, "quality": 88}
            assert render_stage(render, lib, shot, edit, side, **kw) == legacy_render_stage(render, lib, shot, edit, side, **kw)
    res = export_finish(render, lib, shot, plain, {}, settings.export_dir, [], quality=95, long_edge=None, fmt="jpeg")
    old, _labels, _info = legacy_export_payload(render, lib, shot, plain, {}, quality=95, long_edge=None, fmt="jpeg")
    assert res.path.read_bytes() == old


# ---------------------------------------------------------------- 错误先后不变（Sol desktop-export r1 P2）


@pytest.mark.parametrize("fmt", ["jpeg", "png"])
def test_broken_untouched_jpg_and_bad_destination_still_report_the_destination(studio, fmt):
    """没编辑的相机 JPG 坏了、目标目录又不能用：和以前一样先报目录（API 是 422 bad_destination），不是解码异常。"""
    render, lib, shot, edit, side, base, _out = _case(studio, "camera-untouched")
    lib.abspath(shot["files"]["jpg"]["relpath"]).write_bytes(b"not a jpeg")
    with pytest.raises(ExportError) as err:
        export_finish(render, lib, shot, edit, side, lib.root, [], quality=95, long_edge=None, fmt=fmt, base=base, hdr=False)
    assert err.value.code == "bad_destination"


def test_broken_untouched_jpg_with_a_good_destination_still_fails_to_decode(studio):
    """目录没问题时，坏 JPG 照旧在解码时失败，而且不留下文件。"""
    render, lib, shot, edit, side, base, out = _case(studio, "camera-untouched")
    lib.abspath(shot["files"]["jpg"]["relpath"]).write_bytes(b"not a jpeg")
    with pytest.raises(Exception) as err:
        export_finish(render, lib, shot, edit, side, out, [], quality=95, long_edge=None, fmt="jpeg", base=base, hdr=False)
    assert not isinstance(err.value, ExportError) or err.value.code != "bad_destination"
    assert not out.exists() or not any(out.glob("PAIR0001*"))
