"""PR #123 修复轮：极宽标志不越出槽位、ICC 转到 sRGB、符号链接错误不泄漏路径。"""

from __future__ import annotations

import io
import itertools
import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageCms

from conftest import PORT
from gxr_studio.app import create_app
from gxr_studio.finish.fonts import FONT_FILES, FontMetrics, table_dict
from gxr_studio.finish.layout import frame_layout
from gxr_studio.finish.logo_math import LogoInfo, fit_logo
from gxr_studio.finish.logo_store import LogoError, LogoStore, prepare_uploaded_logo
from gxr_studio.finish.spec import LOGO_STYLES, OUTPUT_RATIOS, FrameSpec

WEB = Path(__file__).resolve().parents[1]
P3_PROFILE = Path("/System/Library/ColorSync/Profiles/Display P3.icc")
ERR_BAD_PROFILE = "这张图的颜色配置文件读不了。请换一张 PNG、JPG 或 WebP。"
ERR_LOGO_UNAVAILABLE = "标志库暂时不能用。请检查应用数据文件夹。"
ERR_LOGO_FILE = "这个标志的文件读不出来。请重新上传。"


@pytest.fixture
def client(settings: Any) -> Any:
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        yield c


def _encode(img: Image.Image, **kwargs: Any) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", **kwargs)
    return buf.getvalue()


def _solid(size: tuple[int, int], color: tuple[int, int, int, int]) -> bytes:
    return _encode(Image.new("RGBA", size, color))


def _inside(layout: Any) -> list[dict[str, Any]]:
    bad: list[dict[str, Any]] = []
    if layout is None:
        return bad
    for item in layout.items:
        if item.get("k") != "clogo":
            continue
        x, y, w, h = int(item["x"]), int(item["y"]), int(item["w"]), int(item["h"])
        if w > layout.width or h > layout.height or x < 0 or y < 0 or x + w > layout.width or y + h > layout.height:
            bad.append({"canvas": [layout.width, layout.height], "logo": dict(item)})
    return bad


def test_extreme_aspect_logo_stays_inside_every_style_and_ratio() -> None:
    """极宽、极高标志在全部有标志位的样式和成品比例上都不能画出槽位或画布。"""
    cap_failures: list[str] = []
    for width, height, cap, max_h, max_w, size in itertools.product(
        (1200, 8, 64),
        (5, 1200, 64),
        (14, 21, 42),
        (8, 40, 100),
        (8, 40, 300),
        ("small", "medium", "large"),
    ):
        if width == height:
            continue
        logo = LogoInfo("extreme", "extreme", width, height, 0.35, False, 40.0)
        fitted_w, fitted_h = fit_logo(logo, cap, max_h, max_w, size)
        if fitted_w > max_w or fitted_h > max_h or fitted_w < 1 or fitted_h < 1:
            cap_failures.append(f"{width}x{height} cap={cap} max={max_w}x{max_h} {size} -> {fitted_w}x{fitted_h}")

    logos = [
        LogoInfo("wide", "Wide", 1200, 5, 0.35, False, 40.0),
        LogoInfo("tall", "Tall", 8, 1200, 0.35, False, 40.0),
        LogoInfo("word", "Word", 960, 128, 0.35, False, 25.0),
    ]
    logo_map = {item.id: item for item in logos}
    metrics = FontMetrics()
    meta = {"lens_short": "A12 50mm", "aperture": "f/2.8", "shutter": "1/250s", "iso": "ISO 200", "date": "2026.11.11"}
    ratios = [*OUTPUT_RATIOS, "original"]
    cases: list[dict[str, Any]] = []
    specs: list[FrameSpec] = []
    outside: list[dict[str, Any]] = []
    for style, ratio, shape, logo, size in itertools.product(
        sorted(LOGO_STYLES),
        ratios,
        ((900, 600), (600, 900)),
        logos,
        ("small", "medium", "large"),
    ):
        spec = FrameSpec(style=style, output_ratio=ratio, logo=logo.id, logo_mode="orig", logo_size=size, color="white")
        layout = frame_layout(shape[0], shape[1], spec, meta, metrics, logo_map)
        specs.append(spec)
        cases.append({"w": shape[0], "h": shape[1], "spec": spec.to_json(), "meta": meta})
        outside.extend({"style": style, "ratio": ratio, "logo": logo.id, "size": size, **item} for item in _inside(layout))

    node_note = "node 未跑"
    mismatches: list[dict[str, Any]] = []
    if not cap_failures and not outside and shutil.which("node"):
        payload = {
            "cases": cases,
            "logos": [
                {"id": item.id, "name": item.name, "w": item.w, "h": item.h, "coverage": item.coverage, "opaque": item.opaque, "lum": item.lum}
                for item in logos
            ],
            "tables": {font: table_dict(font) for font in FONT_FILES},
            "fits": [
                {"logo": {"w": 1200, "h": 5, "coverage": 0.35}, "cap": 21, "maxH": 100, "maxW": 300, "size": "medium"},
                {"logo": {"w": 8, "h": 1200, "coverage": 0.2}, "cap": 21, "maxH": 80, "maxW": 40, "size": "large"},
                {"logo": {"w": 960, "h": 128, "coverage": 0.35}, "cap": 24, "maxH": 72, "maxW": 216, "size": "small"},
            ],
        }
        script = """
import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
const root = process.argv[1];
const data = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const {frameLayout} = await import(pathToFileURL(root + '/web/assets/finish_layout.js'));
const {useTable, metrics} = await import(pathToFileURL(root + '/web/assets/finish_fonts.js'));
const {useLogos, fitLogo} = await import(pathToFileURL(root + '/web/assets/finish_frame_core.js'));
for (const [font, table] of Object.entries(data.tables)) useTable(font, table);
useLogos(data.logos);
const mark = (item) => ({k: item.k, id: item.id ?? null, x: item.x, y: item.y, w: item.w, h: item.h});
const layouts = data.cases.map((c) => {
  const layout = frameLayout(c.w, c.h, c.spec, c.meta, metrics);
  return {width: layout.width, height: layout.height, logos: layout.items.filter((item) => item.k === 'clogo' || item.k === 'logo').map(mark)};
});
const fits = data.fits.map((c) => fitLogo({...c.logo, opaque: false, lum: 40}, c.cap, c.maxH, c.maxW, c.size));
console.log(JSON.stringify({layouts, fits}));
"""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(payload, handle)
            payload_path = handle.name
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", script, str(WEB), payload_path],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        Path(payload_path).unlink(missing_ok=True)
        assert proc.returncode == 0, proc.stderr[-2000:]
        actual = json.loads(proc.stdout)
        py_fits = []
        for item in payload["fits"]:
            info = LogoInfo("f", "f", item["logo"]["w"], item["logo"]["h"], item["logo"]["coverage"], False, 40.0)
            py_fits.append(list(fit_logo(info, item["cap"], item["maxH"], item["maxW"], item["size"])))
        if py_fits != actual["fits"]:
            mismatches.append({"fits": {"python": py_fits, "javascript": actual["fits"]}})
        for index, (case, spec, js_layout) in enumerate(zip(cases, specs, actual["layouts"], strict=True)):
            py_layout = frame_layout(case["w"], case["h"], spec, case["meta"], metrics, logo_map)
            assert py_layout is not None
            py_logos = [
                {"k": item["k"], "id": item.get("id"), "x": item["x"], "y": item["y"], "w": item["w"], "h": item["h"]}
                for item in py_layout.items
                if item.get("k") in ("clogo", "logo")
            ]
            if py_layout.width != js_layout["width"] or py_layout.height != js_layout["height"] or py_logos != js_layout["logos"]:
                mismatches.append({"index": index, "python": py_logos, "javascript": js_layout["logos"]})
                if len(mismatches) >= 3:
                    break
        node_note = f"js_cases={len(actual['layouts'])}"

    if cap_failures or outside or mismatches:
        pytest.fail(
            f"cap_failures={len(cap_failures)} outside={len(outside)} mismatches={len(mismatches)} "
            f"first_cap={cap_failures[:2]} first_outside={outside[:1]} first_mismatch={mismatches[:1]} node={node_note}"
        )
    assert node_note.startswith("js_cases="), node_note


def test_display_p3_upload_converts_to_srgb_and_keeps_alpha() -> None:
    assert P3_PROFILE.is_file(), P3_PROFILE
    profile = P3_PROFILE.read_bytes()
    source = Image.new("RGBA", (96, 96), (200, 80, 50, 180))
    expected = ImageCms.profileToProfile(
        source.convert("RGB"),
        ImageCms.ImageCmsProfile(io.BytesIO(profile)),
        ImageCms.createProfile("sRGB"),
        outputMode="RGB",
    )
    stored, _meta = prepare_uploaded_logo(_encode(source, icc_profile=profile), key_mode=False)
    with Image.open(io.BytesIO(stored)) as saved:
        saved.load()
        pixel = saved.getpixel((48, 48))
        assert saved.info.get("icc_profile") in (None, b"")
        assert pixel[:3] == expected.getpixel((48, 48))
        assert pixel[3] == 180

    plain = Image.new("RGBA", (96, 96), (200, 80, 50, 255))
    untouched, _meta = prepare_uploaded_logo(_encode(plain), key_mode=False)
    with Image.open(io.BytesIO(untouched)) as saved:
        assert saved.getpixel((48, 48)) == (200, 80, 50, 255)

    broken = Image.new("RGB", (80, 80), (200, 80, 50))
    with pytest.raises(LogoError) as caught:
        prepare_uploaded_logo(_encode(broken, icc_profile=b"not-a-profile"), key_mode=False)
    assert caught.value.message == ERR_BAD_PROFILE
    assert "/" not in caught.value.message and "not-a-profile" not in caught.value.message


def test_symlinked_logo_directory_response_has_no_path(client: Any, settings: Any, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    blocked = tmp_path / "pretend-repo"
    blocked.mkdir()
    (blocked / ".git").mkdir()
    settings.home_dir.mkdir(parents=True, exist_ok=True)
    root = settings.home_dir / "logos"
    if root.is_symlink():
        root.unlink()
    elif root.exists():
        shutil.rmtree(root)
    root.symlink_to(blocked, target_is_directory=True)
    caplog.set_level(logging.INFO, logger="gxr_studio")
    response = client.get("/api/logos")
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["message"] == ERR_LOGO_UNAVAILABLE
    assert "detail" not in body
    assert str(blocked) not in response.text
    assert settings.token not in caplog.text
    assert "logo_path_refused" in caplog.text
    assert sorted(path.name for path in blocked.iterdir()) == [".git"]


def test_symlinked_logo_file_response_is_chinese(client: Any, settings: Any, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    uploaded = client.post("/api/logos", params={"name": "洋红", "key_white": "false"}, content=_solid((160, 80), (255, 0, 255, 255)))
    assert uploaded.status_code == 200, uploaded.text
    logo_id = uploaded.json()["logo"]["id"]
    external = tmp_path / "outside-logo.png"
    external.write_bytes(b"do-not-follow")
    stored = settings.home_dir / "logos" / f"{logo_id}.png"
    stored.unlink()
    stored.symlink_to(external)
    caplog.set_level(logging.INFO, logger="gxr_studio")
    response = client.get(f"/api/logos/{logo_id}/image")
    assert external.read_bytes() == b"do-not-follow"
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["message"] == ERR_LOGO_FILE
    assert "detail" not in body
    assert "Internal Server Error" not in response.text
    assert str(external) not in response.text and str(stored) not in response.text
    assert settings.token not in caplog.text
    assert "logo_file_unreadable" in caplog.text


_PROFILE_DIR = Path("/System/Library/ColorSync/Profiles")


def _need_profile(name: str) -> bytes:
    path = _PROFILE_DIR / name
    if not path.is_file():
        pytest.skip(f"系统色彩配置不在本机：{path}")
    return path.read_bytes()


def _reference_srgb(raw: bytes, in_mode: str) -> tuple[Image.Image, Image.Image]:
    with Image.open(io.BytesIO(raw)) as src:
        src.load()
        alpha = src.convert("RGBA").getchannel("A")
        icc = src.info["icc_profile"]
        expected = ImageCms.profileToProfile(
            src.convert(in_mode),
            ImageCms.ImageCmsProfile(io.BytesIO(icc)),
            ImageCms.createProfile("sRGB"),
            outputMode="RGB",
        )
        assert expected is not None
        return expected, alpha


def test_gray_cmyk_and_palette_profiles_keep_colour_and_alpha() -> None:
    gray_icc = _need_profile("Generic Gray Gamma 2.2 Profile.icc")
    gray = Image.new("L", (64, 64), 140)
    gray_raw = _encode(gray, icc_profile=gray_icc)
    expected_gray, _gray_alpha = _reference_srgb(gray_raw, "L")
    stored_gray, _meta = prepare_uploaded_logo(gray_raw, key_mode=False)
    with Image.open(io.BytesIO(stored_gray)) as saved:
        pixel = saved.getpixel((32, 32))
        assert pixel[0] == pixel[1] == pixel[2]
        assert pixel[:3] == expected_gray.getpixel((32, 32))

    cmyk_icc = _need_profile("Generic CMYK Profile.icc")
    cmyk = Image.new("CMYK", (64, 64), (20, 40, 60, 10))
    cmyk_buf = io.BytesIO()
    cmyk.save(cmyk_buf, format="JPEG", quality=95, icc_profile=cmyk_icc)
    cmyk_raw = cmyk_buf.getvalue()
    expected_cmyk, _cmyk_alpha = _reference_srgb(cmyk_raw, "CMYK")
    stored_cmyk, _meta = prepare_uploaded_logo(cmyk_raw, key_mode=False)
    with Image.open(io.BytesIO(stored_cmyk)) as saved:
        got = saved.getpixel((32, 32))[:3]
        want = expected_cmyk.getpixel((32, 32))
        assert all(abs(a - b) <= 1 for a, b in zip(got, want, strict=True)), (got, want)

    p3 = _need_profile("Display P3.icc")
    indexed = Image.new("P", (64, 64))
    indexed.putpalette([0, 0, 0, 200, 80, 50] + [0] * 762)
    indexed.putdata([0] + [1] * (64 * 64 - 1))
    indexed_raw = _encode(indexed, transparency=0, icc_profile=p3)
    expected_indexed, indexed_alpha = _reference_srgb(indexed_raw, "RGB")
    stored_indexed, _meta = prepare_uploaded_logo(indexed_raw, key_mode=False)
    with Image.open(io.BytesIO(stored_indexed)) as saved:
        assert saved.getpixel((0, 0))[3] == 0
        assert indexed_alpha.getpixel((0, 0)) == 0
        assert saved.getpixel((16, 16))[:3] == expected_indexed.getpixel((16, 16))
        assert saved.getpixel((16, 16))[3] == 255


def test_unreadable_logo_file_is_omitted_like_a_missing_one(settings: Any, tmp_path: Path) -> None:
    store = LogoStore(settings.home_dir, forbidden=(settings.library_dir,))
    added = store.add_logo(_solid((96, 96), (20, 40, 200, 255)), name="坏链", key_mode=False)
    logo_id = added["logo"]["id"]
    external = tmp_path / "outside.png"
    external.write_bytes(b"do-not-read")
    stored = settings.home_dir / "logos" / f"{logo_id}.png"
    stored.unlink()
    stored.symlink_to(external)
    assert logo_id not in {item["id"] for item in store.list_logos()}
    assert logo_id not in store.info_map()
    assert store.image_map(logo_id) == {}
    assert external.read_bytes() == b"do-not-read"


def test_disposed_views_release_logo_catalog_listeners() -> None:
    script = """
import { pathToFileURL } from 'node:url';
const root = process.argv[1];
const cat = await import(pathToFileURL(root + '/web/assets/logo_catalog.js'));
const preview = await import(pathToFileURL(root + '/web/assets/preview.js'));
const views = [];
for (let i = 0; i < 8; i += 1) {
  const view = { ed: null };
  view.logoCatalogWatch = cat.onLogoCatalog(() => {});
  views.push(view);
}
if (cat.catalogListenerCount() !== 8) throw new Error('open ' + cat.catalogListenerCount());
for (const view of views) preview.disposeEdit(view);
if (cat.catalogListenerCount() !== 0) throw new Error('disposed ' + cat.catalogListenerCount());
console.log('ok');
"""
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(WEB)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
