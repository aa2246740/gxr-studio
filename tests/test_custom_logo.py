"""自定义相机标志（R6-B / DESIGN.md §10）：存储守卫、上传拒绝、去白底参考一致性、八款样式×颜色×大小、删除回退与预览导出一致。"""

from __future__ import annotations

import importlib
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFilter, PngImagePlugin

from conftest import PORT
from gxr_studio import privacy
from gxr_studio.app import create_app
from gxr_studio.finish.catalog import frames_document
from gxr_studio.finish.fonts import FontMetrics
from gxr_studio.finish.frame import apply_frame
from gxr_studio.finish.layout import frame_layout
from gxr_studio.finish.logo_math import LogoInfo, fit_logo, pick_logo_mode
from gxr_studio.finish.logo_store import (
    ERR_BAD_NAME,
    ERR_LIMIT,
    ERR_TOO_BIG,
    ERR_TOO_SMALL,
    ERR_UNREADABLE,
    MAX_BYTES,
    WARN_OPAQUE,
    LogoStore,
    has_white_background,
    key_white,
    trim_ink,
)
from gxr_studio.finish.spec import LOGO_STYLES, FrameSpec
from gxr_studio.library import sidecar as sc
from test_api import find, import_all

LOGO_KEY_JS = Path(__file__).resolve().parents[1] / "web" / "assets" / "logo_key.js"
REFERENCE = Path(os.environ.get("GXR_FRAME_REFERENCE", "/Volumes/Workspace/Scratch/gxr-studio-hillclimb/round-3/frame-design"))


@pytest.fixture
def client(settings: Any) -> Any:
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        yield c


def _encode(img: Image.Image, fmt: str = "PNG", **kwargs: Any) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **kwargs)
    return buf.getvalue()


def _transparent_mark_bytes(
    size: tuple[int, int] = (240, 100),
    color: tuple[int, int, int, int] = (180, 28, 36, 255),
    fmt: str = "PNG",
) -> bytes:
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad_x = max(8, size[0] // 10)
    pad_y = max(8, size[1] // 8)
    draw.rounded_rectangle((pad_x, pad_y, size[0] - pad_x, size[1] - pad_y), radius=14, fill=color)
    draw.ellipse((size[0] // 2 - 20, size[1] // 2 - 20, size[0] // 2 + 20, size[1] // 2 + 20), fill=(0, 0, 0, 0))
    return _encode(img, fmt=fmt)


def _ring_on_white_image(bg: tuple[int, int, int] = (255, 255, 255)) -> Image.Image:
    """白底上的深色圆环，圆环内部保留纯白圆岛（验证边缘泛洪不会抠掉字内留白），并带柔化过渡边缘。"""
    hi = Image.new("RGB", (360, 360), bg)
    draw = ImageDraw.Draw(hi)
    draw.ellipse((48, 48, 312, 312), fill=(26, 34, 58))
    draw.ellipse((116, 116, 244, 244), fill=(255, 255, 255))
    return hi.resize((180, 180), Image.Resampling.BILINEAR)


def _ref_key_white(img: Image.Image, tol: int = 30) -> Image.Image:
    """独立可移植参考实现（逐行对应 DESIGN.md §10.4 / gxr_logo.py）。"""
    rgb = np.asarray(img.convert("RGB")).astype(np.int16)
    height, width, _ = rgb.shape
    near = (255 - rgb.min(axis=2)) <= tol
    seen = np.zeros((height, width), dtype=bool)
    stack = (
        [(0, x) for x in range(width)]
        + [(height - 1, x) for x in range(width)]
        + [(y, 0) for y in range(height)]
        + [(y, width - 1) for y in range(height)]
    )
    while stack:
        y, x = stack.pop()
        if y < 0 or x < 0 or y >= height or x >= width or seen[y, x] or not near[y, x]:
            continue
        seen[y, x] = True
        stack.extend(((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)))
    alpha = np.where(seen, 0, 255).astype(np.uint8)
    a_img = Image.fromarray(alpha, "L")
    ring = np.asarray(a_img.filter(ImageFilter.MaxFilter(3))) != np.asarray(a_img.filter(ImageFilter.MinFilter(3)))
    white_frac = (255 - rgb.min(axis=2)).clip(0, 255)
    soft = np.where(ring & ~seen, np.clip(white_frac * 255 // 160, 0, 255), alpha).astype(np.uint8)
    return Image.fromarray(np.dstack([rgb.astype(np.uint8), soft]), "RGBA")


def _upload(client: Any, content: bytes, **params: Any) -> Any:
    return client.post("/api/logos", params=params, content=content)


def test_storage_privacy_restart_trim_scale_and_metadata_strip(settings: Any) -> None:
    # 1) 禁止写进受保护目录或工作区
    bad_store = LogoStore(settings.library_dir, forbidden=(settings.library_dir,))
    with pytest.raises(privacy.PrivatePathError):
        bad_store.list_logos()

    # 2) 带 PNG 文本块与超长边（1600x800 墨迹框）的图片：保存后长边缩到 <= 1200，元数据完全剥离，重启后保留
    big = Image.new("RGBA", (1800, 1000), (0, 0, 0, 0))
    ImageDraw.Draw(big).rectangle((100, 100, 1700, 900), fill=(30, 60, 120, 255))
    png_info = PngImagePlugin.PngInfo()
    png_info.add_text("Author", "Secret Studio Owner")
    png_info.add_text("Comment", "GPS-or-serial-leak")
    raw_png = _encode(big, fmt="PNG", pnginfo=png_info)
    assert b"Secret Studio Owner" in raw_png

    store = LogoStore(settings.home_dir, forbidden=(settings.library_dir,))
    added = store.add_logo(raw_png, name="  超长名字会被截断到二十个字符以内ABCDEFG  ", filename="mark.png")
    entry = added["logo"]
    assert len(entry["name"]) == 20
    assert entry["w"] == 1200 and entry["h"] == 600
    assert (settings.home_dir / "logos" / "index.json").is_file()
    stored_bytes = store.read_png(entry["id"])
    assert stored_bytes != raw_png
    assert b"Secret Studio Owner" not in stored_bytes and b"GPS-or-serial-leak" not in stored_bytes
    with Image.open(io.BytesIO(stored_bytes)) as reopened:
        assert reopened.format == "PNG"
        assert reopened.info.get("Author") is None
        assert not reopened.getexif()

    # JPEG 带 EXIF 字段同样剥离且重编码成 PNG；同时接收 WebP
    jpg_src = _ring_on_white_image()
    exif = Image.Exif()
    exif[0x010F] = "PrivateCameraMaker"
    raw_jpg = _encode(jpg_src, fmt="JPEG", quality=92, exif=exif.tobytes())
    assert b"PrivateCameraMaker" in raw_jpg
    jpg_added = store.add_logo(raw_jpg, filename="ring.jpg")
    jpg_png = store.read_png(jpg_added["logo"]["id"])
    assert jpg_png.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"PrivateCameraMaker" not in jpg_png

    webp_bytes = _encode(Image.fromarray(np.asarray(jpg_src), "RGB"), fmt="WEBP", quality=90)
    webp_added = store.add_logo(webp_bytes, filename="ring.webp")
    assert webp_added["logo"]["keyed"] is True

    # 重启（新建实例）后全部条目与图片都在
    restarted = LogoStore(settings.home_dir, forbidden=(settings.library_dir,))
    ids = [item["id"] for item in restarted.list_logos()]
    assert ids == [entry["id"], jpg_added["logo"]["id"], webp_added["logo"]["id"]]
    assert entry["id"] in restarted.info_map()


def test_upload_security_rejections_and_auth_guards(client: Any, settings: Any) -> None:
    # 未带令牌或伪造 Host 一律拦截
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}") as no_token:
        assert no_token.get("/api/logos").status_code in (401, 403)
        assert no_token.post("/api/logos", content=_transparent_mark_bytes()).status_code in (401, 403)
    with TestClient(create_app(settings), base_url="http://evil.example:8765", headers={"X-GXR-Token": settings.token}) as bad_host:
        assert bad_host.get("/api/logos").status_code in (400, 403)
        assert bad_host.post("/api/logos", content=_transparent_mark_bytes()).status_code in (400, 403)

    # 1) 超过 5 MB 拒绝
    over_5mb = b"\x89PNG\r\n\x1a\n" + b"0" * MAX_BYTES
    r_big = _upload(client, over_5mb, filename="huge.png")
    assert r_big.status_code == 422 and r_big.json()["error"] == "logo_too_big"
    assert r_big.json()["message"] == ERR_TOO_BIG

    # 2) SVG 改名为 .png、GIF、HTML、坏 PNG 头、空文件：按文件头魔数和解码器拒绝
    svg_as_png = b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200"><script>alert(1)</script></svg>'
    gif_bytes = _encode(Image.new("RGB", (100, 100), (20, 20, 20)), fmt="GIF")
    for payload, fname in (
        (svg_as_png, "sneaky.png"),
        (gif_bytes, "anim.png"),
        (b"<html>not an image</html>", "page.jpg"),
        (b"\x89PNG\r\n\x1a\ntruncated-corrupt-body", "broken.png"),
        (b"", "empty.webp"),
    ):
        resp = _upload(client, payload, filename=fname)
        assert resp.status_code == 422, (fname, resp.text)
        assert resp.json()["error"] == "logo_unreadable"
        assert resp.json()["message"] == ERR_UNREADABLE

    # 3) 解压炸弹：压缩后很小（几万字节），但总像素 6000x5000 = 30,000,000 > MAX_PIXELS，或单边 > 16384
    bomb_pixels = _encode(Image.new("RGB", (6000, 5000), (12, 34, 56)), fmt="PNG", compress_level=9)
    assert len(bomb_pixels) < MAX_BYTES
    r_bomb = _upload(client, bomb_pixels, filename="bomb.png")
    assert r_bomb.status_code == 422 and r_bomb.json()["error"] == "logo_unreadable"

    bomb_dim = _encode(Image.new("RGB", (16500, 80), (12, 34, 56)), fmt="PNG", compress_level=9)
    assert len(bomb_dim) < MAX_BYTES
    r_dim = _upload(client, bomb_dim, filename="wide_bomb.png")
    assert r_dim.status_code == 422 and r_dim.json()["error"] == "logo_unreadable"

    # 4) 裁边后短边 < 64 px，或整张白图抠完没有墨迹
    tiny_ink = Image.new("RGBA", (220, 220), (0, 0, 0, 0))
    ImageDraw.Draw(tiny_ink).rectangle((40, 80, 180, 130), fill=(20, 20, 20, 255))  # 高只有 50 px
    r_small = _upload(client, _encode(tiny_ink), filename="thin.png")
    assert r_small.status_code == 422 and r_small.json()["error"] == "logo_too_small"
    assert r_small.json()["message"] == ERR_TOO_SMALL

    all_white = _encode(Image.new("RGB", (160, 160), (255, 255, 255)), fmt="PNG")
    r_blank = _upload(client, all_white, filename="blank.png", key_white="true", tol=30)
    assert r_blank.status_code == 422 and r_blank.json()["error"] == "logo_too_small"

    # 5) 非法容差与非法 key_white 参数
    valid = _transparent_mark_bytes()
    assert _upload(client, valid, tol=61).status_code == 422
    assert _upload(client, valid, tol=-1).status_code == 422
    assert _upload(client, valid, key_white="invalid").status_code == 422

    # 6) 深色不透明背景（非白底）保留不透明并返回提示语
    opaque_navy = Image.new("RGB", (140, 90), (24, 32, 68))
    ImageDraw.Draw(opaque_navy).rectangle((25, 20, 115, 70), fill=(230, 185, 65))
    r_opaque = _upload(client, _encode(opaque_navy, fmt="JPEG", quality=92), filename="badge.jpg")
    assert r_opaque.status_code == 200
    body = r_opaque.json()
    assert body["logo"]["opaque"] is True and body["logo"]["keyed"] is False
    assert body["warning"] == WARN_OPAQUE


def test_limit_of_twelve_rename_defaults_delete_and_fallback_to_ricoh(client: Any, card: Any) -> None:
    import_all(client)
    shot_a = find(client, "R0000102")["id"]
    shot_b = find(client, "R0000103")["id"]
    shot_c = find(client, "R0000104")["id"]

    # 连续上传 12 个成功，第 13 个报错
    created_ids: list[str] = []
    for idx in range(12):
        res = _upload(client, _transparent_mark_bytes(color=(30 + idx * 10, 40, 90, 255)), name=f"标{idx + 1}")
        assert res.status_code == 200, res.text
        created_ids.append(res.json()["logo"]["id"])
    assert len(client.get("/api/logos").json()["logos"]) == 12

    r_13 = _upload(client, _transparent_mark_bytes(), name="第13个")
    assert r_13.status_code == 422 and r_13.json()["error"] == "logo_limit_reached"
    assert r_13.json()["message"] == ERR_LIMIT

    # 重命名：合法改名 + 空名/超长名拒绝 + 不存在 ID 404
    target_id = created_ids[0]
    ren = client.put(f"/api/logos/{target_id}", json={"name": "光圈圆标"})
    assert ren.status_code == 200 and ren.json()["logo"]["name"] == "光圈圆标"
    bad_empty = client.put(f"/api/logos/{target_id}", json={"name": "   "})
    assert bad_empty.status_code == 422 and bad_empty.json()["message"] == ERR_BAD_NAME
    bad_long = client.put(f"/api/logos/{target_id}", json={"name": "一" * 21})
    assert bad_long.status_code == 422 and bad_long.json()["message"] == ERR_BAD_NAME
    assert client.put("/api/logos/u_nonexistent", json={"name": "好名字"}).status_code == 404
    assert client.delete("/api/logos/u_nonexistent").status_code == 404
    assert client.get("/api/logos/ricoh/image").status_code == 404

    # 在照片 sidecar 与全局默认边框（设为默认）中同时使用 target_id
    frame_payload = {
        "style": "leica",
        "color": "black",
        "output_ratio": "4:5",
        "logo": target_id,
        "logo_mode": "auto",
        "logo_size": "large",
        "model": False,
        "show_params": True,
        "show_date": True,
        "text": "",
    }
    assert client.put(f"/api/photos/{shot_a}/edit", json={"frame": frame_payload}).status_code == 200
    assert client.put("/api/defaults/finish", json={"frame": frame_payload, "stamp": {"enabled": False}}).status_code == 200

    # 未单独改过边框的照片 shot_c：sidecar 自身 frame 为 None，/api/defaults/finish 与 /output-size 均继承「设为默认」里的自定义标志
    edit_c = client.get(f"/api/photos/{shot_c}/edit").json()
    assert edit_c["frame"] is None
    inherited = client.get("/api/defaults/finish").json()["frame"]
    assert inherited["logo"] == target_id and inherited["logo_size"] == "large" and inherited["model"] is False
    c_sizes = client.post(f"/api/photos/{shot_c}/output-size", json={}).json()
    assert c_sizes["frame"]["logo"] == target_id and c_sizes["sizes"]["original"][0] > 0

    # 删除该标志：自动把照片 sidecar 与默认边框都改回 ricoh，并可再次上传新标志
    deleted = client.delete(f"/api/logos/{target_id}")
    assert deleted.status_code == 200
    d_json = deleted.json()
    assert d_json["reverted_photos"] == 1 and d_json["reverted_defaults"] is True
    assert "光圈圆标" in d_json["message"] and "RICOH" in d_json["message"]
    assert client.get(f"/api/photos/{shot_a}/edit").json()["frame"]["logo"] == "ricoh"
    assert client.get("/api/defaults/finish").json()["frame"]["logo"] == "ricoh"
    assert client.get(f"/api/photos/{shot_c}/edit").json()["frame"] is None
    assert client.post(f"/api/photos/{shot_c}/output-size", json={}).json()["frame"]["logo"] == "ricoh"
    assert _upload(client, _transparent_mark_bytes(), name="补位新标").status_code == 200

    # 即使外部手动把未知标志写进 sidecar，读取、算尺寸、导出都不会崩，自动退回 RICOH
    lib = client.app.state.ctx.lib
    shot_obj = lib.get_shot(shot_b)
    assert shot_obj is not None
    with lib.lock:
        sc_path = lib.sidecar_path_for(shot_obj)
        side, _ = lib.read_sidecar_or_default(shot_obj)
        sc.write_atomic(sc_path, {**side, "border": {**frame_payload, "logo": "u_missing_ghost"}}, tmp_dir=lib.tmp_dir)
    size_resp = client.post(f"/api/photos/{shot_b}/output-size", json={})
    assert size_resp.status_code == 200 and size_resp.json()["sizes"]["original"][0] > 0
    exp_resp = client.post(f"/api/photos/{shot_b}/export", json={"quality": 85})
    assert exp_resp.status_code == 200 and Path(exp_resp.json()["path"]).is_file()


def test_key_white_and_sizing_match_reference_and_js_port_across_tolerances() -> None:
    for bg in ((255, 255, 255), (247, 245, 240)):
        sample = _ring_on_white_image(bg=bg)
        for tol in (0, 15, 30, 60):
            expected = np.asarray(_ref_key_white(sample, tol=tol))
            actual = np.asarray(key_white(sample, tol=tol))
            assert np.array_equal(actual, expected), f"Python key_white mismatch at bg={bg}, tol={tol}"
        keyed_30 = np.asarray(key_white(sample, tol=30))
        assert int(keyed_30[0, 0, 3]) == 0 and int(keyed_30[-1, -1, 3]) == 0
        assert int(keyed_30[90, 90, 3]) == 255 and int(keyed_30[90, 90, 0]) == 255
        semi = (keyed_30[:, :, 3] > 0) & (keyed_30[:, :, 3] < 255)
        assert int(semi.sum()) > 20

    # 本机存在 DESIGN 参考目录时，直接导入 gxr_logo.py 逐像素、逐尺寸对照
    if (REFERENCE / "gxr_logo.py").is_file():
        sys.path.insert(0, str(REFERENCE))
        try:
            ref_mod = importlib.import_module("gxr_logo")
            for bg in ((255, 255, 255), (246, 244, 239)):
                sample = _ring_on_white_image(bg=bg)
                assert has_white_background(sample) == ref_mod.has_white_background(sample)
                for tol in (0, 20, 30, 55):
                    ref_keyed = ref_mod.key_white(sample, tol=tol)
                    our_keyed = key_white(sample, tol=tol)
                    assert np.array_equal(np.asarray(our_keyed), np.asarray(ref_keyed))
                our_trim, ok = trim_ink(key_white(sample, tol=30))
                ref_trim = ref_mod.trim(ref_mod.key_white(sample, tol=30))
                assert ok and np.array_equal(np.asarray(our_trim), np.asarray(ref_trim))
                ref_logo = ref_mod.Logo(key="r", name="r", img=ref_trim)
                our_info = LogoInfo(
                    id="r",
                    name="r",
                    w=our_trim.width,
                    h=our_trim.height,
                    coverage=ref_logo.coverage,
                    opaque=ref_logo.opaque,
                    lum=ref_logo.lum,
                )
                for cap in (14, 24, 42):
                    for sz in ("small", "medium", "large"):
                        assert fit_logo(our_info, cap, max_h=cap * 3, max_w=cap * 9, size=sz) == ref_mod.fit(
                            ref_logo, cap, max_h=cap * 3, max_w=cap * 9, size=sz
                        )
        finally:
            sys.path.pop(0)

    if shutil.which("node") is not None:
        sample = _ring_on_white_image(bg=(250, 248, 245))
        rgba_in = np.asarray(sample.convert("RGBA"), dtype=np.uint8)
        h, w, _ = rgba_in.shape
        script = (
            f"import {{ keyWhiteImageData }} from {json.dumps(LOGO_KEY_JS.as_uri())};"
            "const chunks=[]; for await (const c of process.stdin) chunks.push(c);"
            "const raw = new Uint8ClampedArray(Buffer.concat(chunks));"
            f"const out = keyWhiteImageData({{ width: {w}, height: {h}, data: raw }}, 30);"
            "process.stdout.write(Buffer.from(out.data));"
        )
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            input=rgba_in.tobytes(),
            capture_output=True,
            timeout=20,
            check=True,
        )
        js_rgba = np.frombuffer(proc.stdout, dtype=np.uint8).reshape((h, w, 4))
        py_rgba = np.asarray(key_white(sample, tol=30), dtype=np.uint8)
        assert np.array_equal(js_rgba, py_rgba)


def test_styles_modes_sizes_opaque_guard_and_pixel_rendering() -> None:
    # 只有 8 款带标志位的样式暴露 logo 选项，其余 4 款有框样式不暴露
    doc = frames_document()
    assert "logo_mode" in doc["options"] and "logo_size" in doc["options"]
    cat = {entry["id"]: entry["options"] for entry in doc["styles"]}
    for st in LOGO_STYLES:
        assert "logo" in cat[st]
    for st in ("gallery", "film", "instant", "darkroom"):
        assert "logo" not in cat[st]

    metrics = FontMetrics()
    crimson_img = Image.new("RGBA", (240, 80), (0, 0, 0, 0))
    ImageDraw.Draw(crimson_img).rounded_rectangle((0, 0, 239, 79), radius=16, fill=(150, 20, 24, 255))
    crimson_info = LogoInfo(id="u_crimson", name="深红标", w=240, h=80, coverage=0.96, opaque=False, lum=55.0)
    logos_info = {"u_crimson": crimson_info}
    logos_img = {"u_crimson": crimson_img}
    photo_dark = np.full((450, 600, 3), (18, 22, 28), dtype=np.uint8)
    photo_bright = np.full((450, 600, 3), (236, 234, 228), dtype=np.uint8)
    meta = {"lens_short": "A12 50mm", "aperture": "f/2.8", "shutter": "1/250s", "iso": "ISO 200", "date": "2026.10.19"}

    for style in LOGO_STYLES:
        for mode in ("auto", "orig", "mono"):
            color = (
                "black"
                if style in ("leica", "hasselblad", "thin_keyline", "exhibition_label", "slide_mount") and mode == "auto"
                else "cream"
                if style == "slide_mount"
                else "white"
            )
            spec = FrameSpec(style=style, color=color, output_ratio="4:5", logo="u_crimson", logo_mode=mode, logo_size="medium")
            lay = frame_layout(600, 450, spec, meta, metrics, logos=logos_info)
            assert lay is not None
            clogos = [it for it in lay.items if it["k"] == "clogo"]
            assert len(clogos) == 1, (style, mode)
            rendered = apply_frame(photo_dark, spec, meta, logos=logos_info, logo_images=logos_img)
            assert rendered.shape == (lay.height, lay.width, 3)

    # 黑色 leica auto -> 白色墨迹；白色 leica auto -> 深红原色；白色 leica mono -> 深黑墨迹
    spec_dark_auto = FrameSpec(style="leica", color="black", output_ratio="4:5", logo="u_crimson", logo_mode="auto")
    lay_da = frame_layout(600, 450, spec_dark_auto, meta, metrics, logos=logos_info)
    assert lay_da is not None
    box_da = next(it for it in lay_da.items if it["k"] == "clogo")
    out_da = apply_frame(photo_dark, spec_dark_auto, meta, logos=logos_info, logo_images=logos_img)
    patch_da = out_da[box_da["y"] + 4 : box_da["y"] + box_da["h"] - 4, box_da["x"] + 8 : box_da["x"] + box_da["w"] - 8]
    assert patch_da.mean() > 220 and abs(float(patch_da[:, :, 0].mean()) - float(patch_da[:, :, 2].mean())) < 5

    spec_light_auto = FrameSpec(style="leica", color="white", output_ratio="4:5", logo="u_crimson", logo_mode="auto")
    lay_la = frame_layout(600, 450, spec_light_auto, meta, metrics, logos=logos_info)
    assert lay_la is not None
    box_la = next(it for it in lay_la.items if it["k"] == "clogo")
    out_la = apply_frame(photo_dark, spec_light_auto, meta, logos=logos_info, logo_images=logos_img)
    patch_la = out_la[box_la["y"] + 4 : box_la["y"] + box_la["h"] - 4, box_la["x"] + 8 : box_la["x"] + box_la["w"] - 8]
    assert float(patch_la[:, :, 0].mean()) - float(patch_la[:, :, 2].mean()) > 90

    spec_light_mono = FrameSpec(style="leica", color="white", output_ratio="4:5", logo="u_crimson", logo_mode="mono")
    out_lm = apply_frame(photo_dark, spec_light_mono, meta, logos=logos_info, logo_images=logos_img)
    patch_lm = out_lm[box_la["y"] + 4 : box_la["y"] + box_la["h"] - 4, box_la["x"] + 8 : box_la["x"] + box_la["w"] - 8]
    assert patch_lm.mean() < 40 and abs(float(patch_lm[:, :, 0].mean()) - float(patch_lm[:, :, 2].mean())) < 6

    # 模糊背景：暗底照片把深色标志自动翻白，亮底照片保持深红原色
    spec_blur = FrameSpec(style="blur_fill", output_ratio="4:5", logo="u_crimson", logo_mode="auto")
    lay_blur = frame_layout(600, 450, spec_blur, meta, metrics, logos=logos_info)
    assert lay_blur is not None
    box_b = next(it for it in lay_blur.items if it["k"] == "clogo")
    blur_dark = apply_frame(photo_dark, spec_blur, meta, logos=logos_info, logo_images=logos_img)
    blur_bright = apply_frame(photo_bright, spec_blur, meta, logos=logos_info, logo_images=logos_img)
    p_bd = blur_dark[box_b["y"] + 4 : box_b["y"] + box_b["h"] - 4, box_b["x"] + 8 : box_b["x"] + box_b["w"] - 8]
    p_bb = blur_bright[box_b["y"] + 4 : box_b["y"] + box_b["h"] - 4, box_b["x"] + 8 : box_b["x"] + box_b["w"] - 8]
    assert p_bd.mean() > 210
    assert float(p_bb[:, :, 0].mean()) - float(p_bb[:, :, 2].mean()) > 80

    # 不透明图片绝不被重着色（即使选了 mono 或深色底 auto）
    opaque_badge = Image.new("RGBA", (180, 90), (210, 45, 35, 255))
    opaque_info = LogoInfo(id="u_op", name="实心章", w=180, h=90, coverage=1.0, opaque=True, lum=95.0)
    assert pick_logo_mode(opaque_info, "mono", 18.0) == "orig"
    assert pick_logo_mode(opaque_info, "auto", 18.0) == "orig"
    spec_op = FrameSpec(style="cinema", output_ratio="16:9", logo="u_op", logo_mode="mono")
    lay_op = frame_layout(600, 450, spec_op, meta, metrics, logos={"u_op": opaque_info})
    assert lay_op is not None
    box_op = next(it for it in lay_op.items if it["k"] == "clogo")
    out_op = apply_frame(photo_dark, spec_op, meta, logos={"u_op": opaque_info}, logo_images={"u_op": opaque_badge})
    p_op = out_op[box_op["y"] + 2 : box_op["y"] + box_op["h"] - 2, box_op["x"] + 2 : box_op["x"] + box_op["w"] - 2]
    assert float(p_op[:, :, 0].mean()) - float(p_op[:, :, 2].mean()) > 120

    # 大小档位（小 < 中 < 大）与徕卡型号文字「GXR」开关
    heights = []
    for sz in ("small", "medium", "large"):
        lay_sz = frame_layout(
            600,
            450,
            FrameSpec(style="hasselblad", output_ratio="4:5", logo="u_crimson", logo_size=sz),
            meta,
            metrics,
            logos=logos_info,
        )
        assert lay_sz is not None
        heights.append(next(it["h"] for it in lay_sz.items if it["k"] == "clogo"))
    assert heights[0] < heights[1] < heights[2]
    no_model = frame_layout(600, 450, FrameSpec(style="leica", logo="u_crimson", model=False), meta, metrics, logos=logos_info)
    with_model = frame_layout(600, 450, FrameSpec(style="leica", logo="u_crimson", model=True), meta, metrics, logos=logos_info)
    assert no_model is not None and with_model is not None
    assert not any(it["k"] == "text" and it.get("text") == "GXR" for it in no_model.items)
    assert any(it["k"] == "text" and it.get("text") == "GXR" for it in with_model.items)


def test_full_res_export_equals_preview_layout_and_copy_paste_undo(client: Any, card: Any) -> None:
    import_all(client)
    shot1 = find(client, "R0000102")["id"]
    shot2 = find(client, "R0000103")["id"]
    up = _upload(client, _transparent_mark_bytes(size=(360, 120), color=(190, 30, 40, 255)), name="工作室章").json()["logo"]
    custom_frame = {
        "style": "leica",
        "color": "black",
        "output_ratio": "4:5",
        "logo": up["id"],
        "logo_mode": "auto",
        "logo_size": "large",
        "model": False,
        "show_params": True,
        "show_date": True,
        "text": "TOKYO",
    }
    before_edit = client.get(f"/api/photos/{shot1}/edit").json()["frame"]
    assert before_edit is None
    saved1 = client.put(f"/api/photos/{shot1}/edit", json={"frame": custom_frame}).json()["frame"]
    assert saved1["logo"] == up["id"] and saved1["logo_size"] == "large" and saved1["model"] is False

    # 复制到另一张照片（真实 /api/edits/paste）
    pasted = client.post(
        "/api/edits/paste",
        json={
            "source_id": shot1,
            "groups": ["frame"],
            "targets": [{"id": shot2}],
            "batch_id": "logo-paste-batch-1",
            "include_state": True,
        },
    )
    assert pasted.status_code == 200, pasted.text
    pasted_frame = pasted.json()["results"][0]["edit"]["frame"]
    assert pasted_frame["logo"] == up["id"] and pasted_frame["logo_size"] == "large" and pasted_frame["model"] is False

    # 原尺寸 PNG 与 JPEG 导出：成品宽高与 /output-size 完全相等，且 clogo 槽位在全尺寸下真实画出白色反相墨迹
    sized = client.post(f"/api/photos/{shot2}/output-size", json={}).json()
    want_w, want_h = sized["sizes"]["original"]
    photo_w, photo_h = sized["photo"]
    ctx = client.app.state.ctx
    lay = frame_layout(
        photo_w,
        photo_h,
        FrameSpec(**{k: v for k, v in custom_frame.items()}),
        sized["meta"],
        FontMetrics(),
        logos=ctx.render.logos.info_map(),
    )
    assert lay is not None and (lay.width, lay.height) == (want_w, want_h)
    clogo_box = next(it for it in lay.items if it["k"] == "clogo")

    for fmt in ("png", "jpeg"):
        exported = client.post(f"/api/photos/{shot2}/export", json={"format": fmt, "quality": 95}).json()
        assert (exported["width"], exported["height"]) == (want_w, want_h)
        with Image.open(exported["path"]) as exp_im:
            assert exp_im.size == (want_w, want_h)
            arr = np.asarray(exp_im.convert("RGB"))
            slot = arr[clogo_box["y"] : clogo_box["y"] + clogo_box["h"], clogo_box["x"] : clogo_box["x"] + clogo_box["w"]]
            # 黑底（亮度 ~14）上深红圆角框自动翻白，槽位里有明显高亮度白色像素，同时圆心镂空处仍保持黑底
            assert float(slot.max()) > 235 and float(slot.mean()) > 90
            cy, cx = slot.shape[0] // 2, slot.shape[1] // 2
            assert float(slot[cy - 2 : cy + 2, cx - 2 : cx + 2].mean()) < 30

    undone_batch = client.post("/api/edits/paste/undo", json={"batch_id": "logo-paste-batch-1"})
    assert undone_batch.status_code == 200 and undone_batch.json()["counts"]["restored"] == 1
    assert client.get(f"/api/photos/{shot2}/edit").json()["frame"] is None

    reverted = client.put(f"/api/photos/{shot1}/edit", json={"frame": {"style": "none"}}).json()["frame"]
    assert reverted["style"] == "none" and reverted["logo"] == "ricoh"
