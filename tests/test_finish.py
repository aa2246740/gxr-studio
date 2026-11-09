"""成片：纠正方向、边框名称、时间戳撇号、缓存键里的品质、只有 JPEG 时的调整。"""

from __future__ import annotations

import io

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import PORT
from gxr_studio.app import create_app
from gxr_studio.finish.catalog import frames_document
from gxr_studio.finish.geometry import clamp_norm_rect, header_value, max_area_rect, normalized_valid_rect, warp
from gxr_studio.finish.pixels import apply_base, apply_extra
from gxr_studio.finish.spec import Geometry, StampSpec
from gxr_studio.finish.stamp import format_stamp
from gxr_studio.render.params import Adjust, parse_edit


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        yield c


def _grid(w: int = 80, h: int = 60) -> np.ndarray:
    img = np.full((h, w, 3), 40, np.uint8)
    img[h // 2 - 1 : h // 2 + 1, :] = 220
    img[:, w // 2 - 1 : w // 2 + 1] = 220
    return img


def test_positive_vertical_widens_the_top() -> None:
    rgb, valid = warp(_grid(), Geometry(vertical=80))
    ys, xs = np.nonzero(valid)
    top = xs[ys < rgb.shape[0] * 0.2]
    bot = xs[ys > rgb.shape[0] * 0.8]
    assert top.max() - top.min() > bot.max() - bot.min()


def test_positive_horizontal_widens_the_right() -> None:
    rgb, valid = warp(_grid(90, 70), Geometry(horizontal=80))
    ys, xs = np.nonzero(valid)
    left = ys[xs < rgb.shape[1] * 0.25]
    right = ys[xs > rgb.shape[1] * 0.75]
    assert right.max() - right.min() > left.max() - left.min()


def test_positive_distortion_pulls_corners_in() -> None:
    src = _grid(64, 48)
    _out, valid = warp(src, Geometry(distortion=70))
    assert valid.mean() < 1.0
    assert valid[valid.shape[0] // 2, valid.shape[1] // 2]


def test_full_mask_valid_rect_is_the_whole_frame() -> None:
    mask = np.ones((20, 30), dtype=bool)
    assert max_area_rect(mask) == (0, 0, 30, 20)
    assert normalized_valid_rect(mask) == (0.0, 0.0, 1.0, 1.0)
    assert header_value((0, 0, 1, 1)) == "0.0000,0.0000,1.0000,1.0000"


def test_clamp_keeps_a_rect_that_is_already_inside() -> None:
    mask = np.ones((40, 40), dtype=bool)
    mask[:5, :] = False
    rect = (0.2, 0.2, 0.4, 0.4)
    assert clamp_norm_rect(mask, rect) == rect


def test_leica_bar_right_aligns_both_lines() -> None:
    from gxr_studio.finish.frame import apply_frame
    from gxr_studio.finish.spec import parse_frame

    photo = np.full((120, 640, 3), 180, np.uint8)
    meta = {
        "lens": "GR LENS A12 50mm F2.5 MACRO",
        "aperture": "f/2.5",
        "shutter": "1/80s",
        "iso": "ISO 800",
        "date": "26 10 23",
    }
    frame = parse_frame({"style": "leica", "show_logo": True, "show_params": True, "show_date": True, "output_ratio": "original"})
    assert frame is not None
    out = apply_frame(photo, frame, meta)
    dark = np.any(out < 80, axis=2)
    bar = dark[photo.shape[0] :]
    rows = [np.nonzero(bar[i])[0] for i in range(bar.shape[0]) if bar[i].any()]
    rights = [int(cols.max()) for cols in rows if cols.max() > out.shape[1] * 0.55]
    assert max(rights) - min(rights) <= 8
    assert max(rights) > out.shape[1] - 40


def test_frame_catalog_uses_the_copy_names() -> None:
    doc = frames_document()
    names = {item["id"]: item["name"] for item in doc["styles"]}
    assert names == {
        "none": "无",
        "gallery": "白边",
        "leica": "徕卡水印",
        "hasselblad": "哈苏水印",
        "film": "胶片",
        "instant": "拍立得",
    }
    assert doc["stamp"]["text_max_length"] == 24
    assert doc["stamp"]["fonts"][0]["name"] == "数码管"
    assert doc["defaults"]["frame"]["style"] == "none"
    assert doc["stamp"]["defaults"]["enabled"] is False


def test_frame_date_uses_a_full_year() -> None:
    from datetime import datetime

    from gxr_studio.finish.meta_text import frame_date

    assert frame_date(datetime(2026, 10, 23, 17, 41)) == "2026.10.23"


def test_seg7_uses_the_digit_wide_blank_between_groups() -> None:
    from datetime import datetime

    from gxr_studio.finish.fonts import FontMetrics
    from gxr_studio.finish.layout import advance
    from gxr_studio.finish.stamp import apply_stamp

    metrics = FontMetrics()
    assert abs(advance(metrics, "dseg", 80, "!") - advance(metrics, "dseg", 80, "0")) < 1
    # 1500×1026 的白底照片，「大」号日期戳的字号正好是 80 像素
    spec = StampSpec(enabled=True, font="seg7", size="large", color="orange", glow=False, corner="tl")
    canvas = apply_stamp(np.full((1026, 1500, 3), 255, np.uint8), spec, datetime(2026, 10, 23, 17, 41), None)
    ink = (canvas[:, :, 0] > 180) & (canvas[:, :, 1] < 140)
    cols = np.nonzero(ink.any(axis=0))[0]
    splits = np.nonzero(np.diff(cols) > 4)[0]
    starts = [int(cols[0]), *[int(cols[i + 1]) for i in splits]]
    ends = [int(cols[i]) for i in splits] + [int(cols[-1])]
    widths = [end - start for start, end in zip(starts, ends)]
    gaps = [starts[i + 1] - ends[i] for i in range(len(starts) - 1)]
    one = widths.index(min(widths))
    cell = advance(metrics, "dseg", 80, "0")
    assert gaps[one] < cell * 0.4
    before_one = gaps[one - 1]
    after_ten = gaps[one + 1]
    assert before_one > after_ten
    assert abs((before_one - after_ten) - cell / 2) < cell * 0.25


def test_seg7_apostrophe_is_a_slanted_tick() -> None:
    from datetime import datetime

    from PIL import Image, ImageDraw

    from gxr_studio.finish.layout import tick_item

    spec = StampSpec(enabled=True, font="seg7", glow=False, size="small")
    text = format_stamp(spec, datetime(2026, 10, 23, 17, 41), None)
    assert text.startswith("'")
    canvas = Image.new("RGB", (80, 80), (255, 255, 255))
    ImageDraw.Draw(canvas).polygon([tuple(p) for p in tick_item(8, 8, 48, (255, 80, 0))["pts"]], fill=(255, 80, 0))
    ink = (np.asarray(canvas)[:, :, 0] > 200) & (np.asarray(canvas)[:, :, 1] < 140)
    ys, xs = np.nonzero(ink)
    top = xs[ys <= np.percentile(ys, 30)].mean()
    bot = xs[ys >= np.percentile(ys, 70)].mean()
    assert top > bot + 0.5
    assert ink[int(ys.mean()), int(xs.mean())]


def test_jpg_exposure_and_extra_knobs_change_pixels() -> None:
    src = np.full((24, 24, 3), 80, np.uint8)
    brighter = apply_base(src, Adjust(exposure_ev=1.0), None)
    assert brighter.mean() > src.mean()
    crushed = apply_extra(src, Adjust(black_point=80))
    vivid = apply_extra(np.array([[[40, 80, 40]]], np.uint8), Adjust(saturation=80))
    assert crushed.mean() < src.mean()
    assert vivid[0, 0, 1] >= 80


def test_parse_edit_reads_legacy_angle_as_straighten() -> None:
    edit = parse_edit({"crop": {"preset": "free", "rect": [0, 0, 1, 1], "angle_deg": 3.5}})
    assert edit.geometry.straighten == 3.5


def test_render_key_includes_quality(tmp_path) -> None:
    from gxr_studio.library.store import Library
    from gxr_studio.render.params import EditState
    from gxr_studio.render.service import RenderService

    lib = Library(tmp_path).open()
    try:
        svc = RenderService(tmp_path / "home", lib)
        svc._camera = type("Cam", (), {"table_sha256": "t" * 64, "manifest_sha256": "m" * 64})()
        svc._calib_manifest_sha = "c" * 64
        shot = {"files": {"dng": {"sha256": "abc"}}, "orientation": 1, "iso": 200, "lens": "50mm"}

        class Plan:
            unit = "jlaunch"
            entry_sha256 = "e"
            model_iso = 200
            calibrated = True

        edit = EditState()
        a = svc.render_key(shot, edit, Plan(), max_edge=100, exact=False, crop=None, quality=88)  # type: ignore[arg-type]
        b = svc.render_key(shot, edit, Plan(), max_edge=100, exact=False, crop=None, quality=40)  # type: ignore[arg-type]
        assert a != b
    finally:
        lib.close()


def _ratio_ok(width: int, height: int, numer: int, denom: int) -> bool:
    if numer == denom:
        return width == height and width >= 16
    return abs(height * numer - width * denom) <= max(numer, denom) and width >= 16 and height >= 16


def _glyph_boxes(image: np.ndarray, spec: StampSpec):
    from datetime import datetime
    from unittest.mock import patch

    from gxr_studio.finish import stamp

    boxes = []
    original = stamp._paint

    def observed(draw, layout, fonts, fill):
        for op in layout.ops:
            if op["k"] == "text":
                boxes.append(draw.textbbox((op["x"], op["y"]), op["text"], font=fonts.face(op["font"], op["px"], op["cjk"])))
        return original(draw, layout, fonts, fill)

    with patch.object(stamp, "_paint", observed):
        stamp.apply_stamp(image, spec, datetime(2026, 10, 31, 14, 5), None)
    return boxes


def test_stamp_keeps_date_time_and_all_24_characters() -> None:
    from datetime import datetime
    from pathlib import Path

    from gxr_studio.finish import stamp

    text = "ABCDEFGHIJKLMNOPQRSTUVWX"
    spec = StampSpec(enabled=True, content="date+text", text=text, date_format="yyyy.mm.dd", show_time=True, font="sans", glow=False)
    got = format_stamp(spec, datetime(2026, 10, 31, 14, 5), None)
    assert got.endswith(text) and "2026.10.31" in got and "14:05" in got
    cjk = "上海北京" * 6
    cjk_spec = StampSpec(enabled=True, content="date+text", text=cjk, date_format="yyyy.mm.dd", show_time=True, font="sans", glow=False)
    fixture = Path(__file__).resolve().parent / "fixtures" / "NotoSansSC-Shanghai.otf"
    saved = stamp._CJK_FONTS
    stamp._CJK_FONTS = (fixture,)
    try:
        for width, height in ((192, 256), (256, 192), (480, 640)):
            for item in (spec, cjk_spec):
                boxes = _glyph_boxes(np.full((height, width, 3), 30, np.uint8), item)
                assert boxes, (width, height, item.text)
                for left, top, right, bottom in boxes:
                    assert 0 <= left and 0 <= top and right <= width and bottom <= height, (width, height, item.text, boxes)
    finally:
        stamp._CJK_FONTS = saved


def test_output_size_uses_the_real_canvas_past_1200() -> None:
    from gxr_studio.finish.geometry import output_size

    for geom in (Geometry(vertical=80), Geometry(distortion=70), Geometry(straighten=12, vertical=40)):
        _rgb, valid = warp(np.zeros((1200, 1600, 3), np.uint8), geom)
        assert output_size(1600, 1200, geom) == (valid.shape[1], valid.shape[0])


def _portrait_jpeg(path, token: int, dng: bytes | None = None) -> None:
    """存储像素是 640×480，方向标记 6。左上角一块红，转正后落到右上。token 让两份文件内容不同。"""
    rgb = np.full((480, 640, 3), (30, 40, 50), np.uint8)
    rgb[:90, :110] = (240, 20, 20)
    rgb[-80:, :90] = (20, 210, 40)
    rgb[180:220, 500:560] = (token, 15, 230)
    image = Image.fromarray(rgb)
    exif = Image.Exif()
    exif[274] = 6
    image.save(path, quality=95, exif=exif)
    if dng is not None:
        path.with_suffix(".DNG").write_bytes(dng)


def _red_centroid(pixels: np.ndarray) -> tuple[float, float]:
    red = (pixels[:, :, 0] > 180) & (pixels[:, :, 1] < 60) & (pixels[:, :, 2] < 60)
    ys, xs = np.nonzero(red)
    assert xs.size > 40
    return float(xs.mean()), float(ys.mean())


def _reference_upright(path, edge: int | None) -> np.ndarray:
    from PIL import ImageOps

    with Image.open(path) as raw:
        upright = ImageOps.exif_transpose(raw).convert("RGB")
    arr = np.asarray(upright)
    if edge and max(arr.shape[:2]) > edge:
        height, width = arr.shape[:2]
        scale = edge / max(height, width)
        resized = Image.fromarray(arr).resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.LANCZOS)
        arr = np.asarray(resized)
    return arr


def test_sized_export_keeps_portrait_orientation(client, card):
    """零编辑的竖拍 JPG，原始尺寸和指定长边都先转正再缩放。JPG 与 RAW+JPG 同一套。"""
    from test_api import find, import_all
    from test_raw_export import scene_dng

    folder = card.root / "DCIM" / "100RICOH"
    _portrait_jpeg(folder / "PORTRAIT.JPG", 40)
    _portrait_jpeg(folder / "PAIR.JPG", 210, scene_dng())
    import_all(client)
    for name in ("PORTRAIT", "PAIR"):
        photo = find(client, name)
        assert photo["has_jpg"] is True
        if name == "PAIR":
            assert photo["has_raw"] is True
        source = folder / f"{name}.JPG"
        for edge in (None, 512, 2048, 4096):
            body: dict[str, object] = {"quality": 95, "source": "auto"}
            if edge is not None:
                body["long_edge"] = edge
            saved = client.post(f"/api/photos/{photo['id']}/export", json=body)
            assert saved.status_code == 200, (name, edge, saved.text)
            with Image.open(saved.json()["path"]) as out:
                assert out.getexif().get(274) == 1
                got = np.asarray(out.convert("RGB"))
            ref = _reference_upright(source, edge)
            assert got.shape == ref.shape, (name, edge, got.shape, ref.shape)
            assert got.shape[1] < got.shape[0]
            rx, ry = _red_centroid(ref)
            gx, gy = _red_centroid(got)
            assert abs(rx - gx) < 8 and abs(ry - gy) < 8, (name, edge, (gx, gy), (rx, ry))
            # Orientation 6 把存储图顺时针转 90°：左上角的红块落到转正后的右上角。
            assert gx > got.shape[1] * 0.65 and gy < got.shape[0] * 0.35


def test_thumbnail_preview_and_export_keep_the_stamp_suffix(client, card):
    """同样长度、后缀不同的两段文字，缩略图、预览和导出都要能看出差别。"""
    from pathlib import Path

    from gxr_studio.finish import stamp
    from test_api import find, import_all

    Image.fromarray(np.full((480, 640, 3), 36, np.uint8)).save(card.root / "DCIM" / "100RICOH" / "STAMP.JPG", quality=95)
    import_all(client)
    photo = find(client, "STAMP")
    fixture = Path(__file__).resolve().parent / "fixtures" / "NotoSansSC-Shanghai.otf"
    saved = stamp._CJK_FONTS
    stamp._CJK_FONTS = (fixture,)
    try:
        shots = []
        for text in ("上海北京" * 6, ("上海北京" * 6)[:-2] + "上海"):
            assert len(text) == 24
            put = client.put(
                f"/api/photos/{photo['id']}/edit",
                json={
                    "stamp": {
                        "enabled": True,
                        "content": "date+text",
                        "date_format": "yyyy.mm.dd",
                        "show_time": True,
                        "date_override": "2026-10-31",
                        "text": text,
                        "font": "sans",
                        "glow": False,
                    }
                },
            )
            assert put.status_code == 200, put.text
            current = find(client, "STAMP")
            thumb = Image.open(io.BytesIO(client.get(current["thumb_url"]).content)).convert("RGB")
            preview = Image.open(
                io.BytesIO(client.get(f"/api/photos/{photo['id']}/render", params={"stage": "final", "size": "preview"}).content)
            ).convert("RGB")
            exported = client.post(f"/api/photos/{photo['id']}/export", json={"quality": 95})
            assert exported.status_code == 200, exported.text
            export = Image.open(exported.json()["path"]).convert("RGB")
            shots.append({"thumb": np.asarray(thumb), "preview": np.asarray(preview), "export": np.asarray(export)})
        for key in ("thumb", "preview", "export"):
            assert shots[0][key].shape == shots[1][key].shape
            assert not np.array_equal(shots[0][key], shots[1][key]), key
    finally:
        stamp._CJK_FONTS = saved


def test_system_cjk_font_draws_on_macos() -> None:
    import sys
    from pathlib import Path

    import pytest

    from gxr_studio.finish import stamp
    from gxr_studio.finish.frame import apply_frame
    from gxr_studio.finish.spec import parse_frame

    if sys.platform != "darwin":
        pytest.skip("系统中文字体只在 macOS 上检查")
    system = (
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
        Path("/System/Library/Fonts/STHeiti Medium.ttc"),
        Path("/System/Library/Fonts/STHeiti Light.ttc"),
    )
    if not any(path.is_file() for path in system):
        pytest.skip("这台 Mac 没有系统中文字体")
    saved = stamp._CJK_FONTS
    stamp._CJK_FONTS = system
    try:
        face = stamp.cjk_font(32)
        assert face is not None
        shanghai = apply_frame(np.full((80, 120, 3), 180, np.uint8), parse_frame({"style": "instant", "text": "上海"}), {})
        beijing = apply_frame(np.full((80, 120, 3), 180, np.uint8), parse_frame({"style": "instant", "text": "北京"}), {})
        assert shanghai is not None and beijing is not None and not np.array_equal(shanghai, beijing)
    finally:
        stamp._CJK_FONTS = saved


def test_preset_ratio_stays_within_one_pixel_inside_the_valid_region() -> None:
    """拉直、垂直、畸变之后，按比例取整数再收进有效区域。1:1 宽高相等。"""
    from gxr_studio.crop import crop_box, default_crop, target_ratio
    from gxr_studio.finish.compose import finish_array
    from gxr_studio.finish.geometry import FILL, fitted_crop
    from gxr_studio.finish.spec import factory_frame, factory_stamp
    from gxr_studio.render.params import EditState

    image = np.full((480, 640, 3), 180, np.uint8)
    fill = np.array(FILL, np.uint8)
    presets = ("1:1", "4:5", "3:2", "xpan-65-24")
    geometries = (Geometry(straighten=15), Geometry(vertical=80), Geometry(distortion=70))
    for geom in geometries:
        warped, _valid = warp(image, geom)
        canvas_h, canvas_w = warped.shape[:2]
        for preset in presets:
            ratio = target_ratio(preset, canvas_w, canvas_h)
            assert ratio is not None
            asked = default_crop(preset, canvas_w, canvas_h)
            proxy = fitted_crop(geom, asked.rect)
            rect = proxy if proxy is not None else asked.rect
            out, _header = finish_array(
                image,
                EditState(geometry=geom),
                stage="final",
                frame=factory_frame(),
                stamp=factory_stamp(),
                crop_rect=rect,
                meta={},
                taken=None,
                file_time=None,
                from_raw=False,
                crop_preset=preset,
            )
            assert _ratio_ok(out.shape[1], out.shape[0], ratio.numerator, ratio.denominator), (
                geom,
                preset,
                out.shape[1],
                out.shape[0],
            )
            assert not np.any(np.all(out == fill, axis=-1))
            saved = fitted_crop(geom, asked.rect, preset=preset, width=image.shape[1], height=image.shape[0])
            assert saved is not None
            _x, _y, pw, ph = crop_box(saved, canvas_w, canvas_h)
            assert _ratio_ok(pw, ph, ratio.numerator, ratio.denominator), (geom, preset, pw, ph)
