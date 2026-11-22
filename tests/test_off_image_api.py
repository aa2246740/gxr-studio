"""桌面关机图 HTTP 接口测试（图库照片、自选文件、强制校验失败与存储卡禁写）。"""

from __future__ import annotations

import base64
import io
import struct
import zlib
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from gxr_studio import off_image
from gxr_studio.app import create_app
from gxr_studio.config import Settings

TOKEN = "off-image-api-test-token"
HEADERS = {"Host": "127.0.0.1:8765", "X-GXR-Token": TOKEN}


def _make_sample_jpg(path: Path, size: tuple[int, int] = (900, 600), orientation: int | None = None) -> bytes:
    w, h = size
    yy, xx = np.mgrid[0:h, 0:w]
    arr = np.stack([(xx * 255 // max(1, w - 1)), (yy * 255 // max(1, h - 1)), np.full_like(xx, 110)], axis=-1).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr, "RGB").save(buf, "JPEG", quality=92)
    raw = buf.getvalue()
    if orientation is not None:
        tiff = b"MM\x00\x2a" + struct.pack(">I", 8) + struct.pack(">H", 1)
        tiff += struct.pack(">HHIHH", 0x0112, 3, 1, orientation, 0) + struct.pack(">I", 0)
        body = b"Exif\x00\x00" + tiff
        raw = raw[:2] + b"\xff\xe1" + struct.pack(">H", len(body) + 2) + body + raw[2:]
    path.write_bytes(raw)
    return raw


def _setup_app(tmp_path: Path) -> tuple[Any, Settings, Path, str]:
    lib_dir = tmp_path / "library"
    home_dir = tmp_path / "home"
    export_dir = tmp_path / "export"
    volumes = tmp_path / "volumes"
    card = volumes / "FAKECARD"
    (card / "DCIM" / "100RICOH").mkdir(parents=True)
    (card / "GXRCE.DAT").write_bytes(b"GXR0" + b"\x00" * 16)
    (lib_dir / "2026-11-21").mkdir(parents=True)
    _make_sample_jpg(lib_dir / "2026-11-21" / "R0001001.JPG", (1000, 700))

    settings = Settings(
        library_dir=lib_dir,
        home_dir=home_dir,
        export_dir=export_dir,
        volumes_roots=(volumes,),
        token=TOKEN,
        web_dir=Path(__file__).resolve().parents[1] / "web",
    )
    app = create_app(settings)
    app.state.ctx.lib.rebuild()
    shots = app.state.ctx.lib.list_shots()
    assert len(shots) == 1
    return app, settings, card, str(shots[0]["shot_id"])


def test_off_image_preview_and_export_from_library_and_upload(tmp_path: Path) -> None:
    app, settings, card, shot_id = _setup_app(tmp_path)
    with TestClient(app) as client:
        # 1. 从图库照片预览（带当前编辑的 SDR 渲染）
        r = client.post(f"/api/off-image/preview?shot_id={shot_id}", headers=HEADERS)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True
        assert data["width"] == 640 and data["height"] == 480
        assert data["bytes"] <= off_image.MAX_BYTES
        assert data["dat_state"] == 7
        assert data["preview_data_url"].startswith("data:image/jpeg;base64,")
        assert data["preview_token"]
        preview_bytes = base64.b64decode(data["preview_data_url"].split(",", 1)[1])
        assert len(preview_bytes) == data["bytes"]
        assert off_image.check_off_image(preview_bytes).ok

        # 2. 导出到默认导出目录
        r_exp = client.post(
            "/api/off-image/export",
            headers=HEADERS,
            json={"preview_token": data["preview_token"], "dest_dir": str(settings.export_dir)},
        )
        assert r_exp.status_code == 200, r_exp.text
        exp = r_exp.json()
        out_path = Path(exp["path"])
        assert out_path == (settings.export_dir / "GXROFF.JPG").resolve()
        assert out_path.read_bytes() == preview_bytes
        assert exp["export_id"]

        # 3. 拒绝把关机图写到挂载存储卡目录
        r_card = client.post(
            "/api/off-image/export",
            headers=HEADERS,
            json={"preview_token": data["preview_token"], "dest_dir": str(card)},
        )
        assert r_card.status_code == 422
        assert r_card.json()["error"] == "bad_destination"
        assert not (card / "GXROFF.JPG").exists()

        # 4. 换一张图：上传任意本地图片（Orientation 6 竖图）
        picked_raw = _make_sample_jpg(tmp_path / "picked.jpg", (900, 600), orientation=6)
        r_up = client.post(
            "/api/off-image/preview",
            headers={**HEADERS, "Content-Type": "application/octet-stream"},
            content=picked_raw,
        )
        assert r_up.status_code == 200, r_up.text
        up = r_up.json()
        assert up["ok"] is True and up["dat_state"] == 7 and up["bytes"] <= off_image.MAX_BYTES
        up_bytes = base64.b64decode(up["preview_data_url"].split(",", 1)[1])
        assert off_image.check_off_image(up_bytes).ok


def test_off_image_api_blocks_export_when_checker_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, settings, _card, shot_id = _setup_app(tmp_path)
    failed = off_image.OffImageCheck(
        ok=False,
        dat_state=2,
        errors=(off_image.CheckIssue("forced_fail", "测试强制校验失败。"),),
    )
    monkeypatch.setattr(off_image, "check_off_image", lambda _data: failed)
    with TestClient(app) as client:
        r = client.post(f"/api/off-image/preview?shot_id={shot_id}", headers=HEADERS)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert body["preview_token"] is None
        assert body["errors"] and body["errors"][0]["code"] == "forced_fail"

        r_exp = client.post(
            "/api/off-image/export",
            headers=HEADERS,
            json={"shot_id": shot_id, "dest_dir": str(settings.export_dir)},
        )
        assert r_exp.status_code == 422
        assert r_exp.json()["error"] == "off_image_check_failed"
        assert not (settings.export_dir / "GXROFF.JPG").exists()


def _png_header_only(width: int, height: int) -> bytes:
    """只有文件头的 PNG：声明很大的尺寸，但文件本身只有几十字节。"""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00" * 64)) + chunk(b"IEND", b"")


@pytest.mark.parametrize("side", [10_000, 20_000])
def test_off_image_rejects_huge_images_before_decoding(tmp_path: Path, side: int) -> None:
    """1 亿像素（Pillow 警告区）和 4 亿像素（炸弹区）都要回中文 422，不能 500，也不能真去解码。"""
    app, _settings, _card, _shot_id = _setup_app(tmp_path)
    with TestClient(app) as client:
        r = client.post(
            "/api/off-image/preview",
            headers={**HEADERS, "Content-Type": "application/octet-stream"},
            content=_png_header_only(side, side),
        )
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"] == "image_too_big"
    assert "detail" not in body


def test_off_image_errors_carry_no_paths(tmp_path: Path) -> None:
    """拒绝写卡等错误只回中文说明；路径只进本地日志。"""
    app, _settings, card, shot_id = _setup_app(tmp_path)
    with TestClient(app) as client:
        r = client.post("/api/off-image/export", headers=HEADERS, json={"shot_id": shot_id, "dest_dir": str(card)})
        bad = client.post(
            "/api/off-image/preview",
            headers={**HEADERS, "Content-Type": "application/octet-stream"},
            content=b"not an image",
        )
    assert r.status_code == 422 and r.json()["error"] == "bad_destination"
    assert bad.status_code == 422 and bad.json()["error"] == "unsupported_image"
    for resp in (r, bad):
        assert "detail" not in resp.json()
        assert str(tmp_path) not in resp.text
