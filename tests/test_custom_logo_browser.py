"""真实浏览器（固定 Playwright 运行环境，无 WebGPU、开启 reducedMotion）验证自定义标志端到端交互与预览导出一致。"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import uvicorn
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from conftest import PORT, REAL_HOME
from gxr_studio.app import create_app
from gxr_studio.finish.fonts import FontMetrics
from gxr_studio.finish.layout import frame_layout
from gxr_studio.finish.logo_math import LogoInfo
from gxr_studio.finish.spec import parse_frame
from test_api import find, import_all

WEB = Path(__file__).resolve().parents[1] / "web"
DRIVER = Path(__file__).parent / "support" / "custom_logo_browser.mjs"
ENV_RUNTIME = os.environ.get("GXR_PLAYWRIGHT_RUNTIME")
REQUIRE_PIN = bool(ENV_RUNTIME) or os.environ.get("GXR_REQUIRE_GPU_TEST") == "1"
RUNTIME = Path(ENV_RUNTIME) if ENV_RUNTIME else REAL_HOME / ".codex" / "playwright-runtime" / "runtime.mjs"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or (not REQUIRE_PIN and not RUNTIME.is_file()),
    reason="需要固定的 Playwright 运行环境",
)


@pytest.fixture
def live(settings: Any, card: Any) -> Any:
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        import_all(client)
        photo1 = find(client, "R0000102")["id"]
        photo2 = find(client, "R0000103")["id"]
        photo3 = find(client, "R0000104")["id"]
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(settings.with_(port=port, web_dir=WEB)), log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        yield {
            "port": port,
            "token": settings.token,
            "photo1": photo1,
            "photo2": photo2,
            "photo3": photo3,
            "settings": settings,
        }
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        sock.close()


def _build_white_ring_png(dest: Path) -> None:
    """生成一张白底深色开口圆角环（含连通圆心镂空与抗锯齿边缘）的测试 PNG，绝不提交静态图片资产。"""
    hi = Image.new("RGB", (360, 200), (255, 255, 255))
    draw = ImageDraw.Draw(hi)
    draw.rounded_rectangle((36, 28, 324, 172), radius=48, fill=(24, 28, 42))
    draw.ellipse((140, 60, 220, 140), fill=(255, 255, 255))
    draw.rectangle((168, 0, 192, 80), fill=(255, 255, 255))
    hi.resize((180, 100), Image.Resampling.BILINEAR).save(dest, format="PNG")


def test_custom_logo_upload_preview_export_defaults_copy_paste_undo_and_manage_in_pinned_browser(
    live: Any,
    tmp_path: Path,
) -> None:
    assert RUNTIME.is_file(), f"固定的 Playwright 运行时缺失（禁止回退系统 Chrome）：{RUNTIME}"
    files_dir = tmp_path / "logo-inputs"
    files_dir.mkdir()
    _build_white_ring_png(files_dir / "white_ring.png")
    transparent = Image.new("RGBA", (160, 128), (12, 34, 56, 0))
    ImageDraw.Draw(transparent).ellipse((16, 16, 144, 112), fill=(50, 20, 70, 255))
    transparent.save(files_dir / "transparent_mark.png", format="PNG")
    (files_dir / "sneaky_svg.png").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80"><rect width="120" height="80"/></svg>',
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "node",
            str(DRIVER),
            str(RUNTIME),
            str(live["port"]),
            live["token"],
            live["photo1"],
            live["photo2"],
            live["photo3"],
            str(files_dir),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        encoding="utf-8",
        env={**os.environ, "HOME": str(REAL_HOME)},
    )
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    out = json.loads(result.stdout.strip().splitlines()[-1])

    assert out["envCheck"] == {"hasWebGpu": False, "reducedMotion": True}
    if os.environ.get("GXR_CHROME"):
        assert out["pinnedExecutable"] == os.environ["GXR_CHROME"]
    assert out["hiddenLogoStyles"] == {"gallery": 0, "film": 0, "instant": 0, "darkroom": 0}
    assert out["renderRequestsDuringFrameClicks"] == 0
    assert out["pastedStyleOnPhoto2"] == "leica"
    assert out["inheritedLogoOnPhoto3"] == out["uploadedLogoId"]
    assert out["reloadStageLogoMaxAlpha"] > 200
    assert out["errors"] == []
    assert out["transparentKeyDisabled"] is True and out["transparentKeyChecked"] is False

    # 校验面板尺寸行、舞台 .pv、样式卡片与导出的原尺寸 PNG / JPEG 完全一致（清单第 8 项）
    m = re.match(r"^成品 (\d+) × (\d+) 像素", out["sizeLine"])
    assert m is not None, out["sizeLine"]
    expected_w, expected_h = int(m.group(1)), int(m.group(2))
    expected_aspect = expected_w / expected_h
    assert out["stagePvAspect"] == pytest.approx(expected_aspect, abs=0.008)
    assert out["cardCssAspect"] == pytest.approx(expected_aspect, abs=0.01)
    assert abs(out["cardBitmap"][0] - expected_aspect * out["cardBitmap"][1]) <= max(1.0, expected_aspect)
    # 舞台与卡片在黑色徕卡底上都画出了自动反白的标志墨迹，切原色时舞台墨迹变暗（抗锯齿边缘最高约 80，远低于反白 255）
    assert out["stageSlotAutoWhiteMax"] > 235 and out["stageSlotAutoWhiteLuma"] > 75
    assert out["stageSlotOrigDarkMax"] < 120 and out["stageSlotOrigDarkLuma"] < 45
    assert out["cardSlotAutoWhiteMax"] > 190 and out["cardChangedFromRicoh"] is True

    sized = out["outputSizeInfo"]
    assert sized["sizes"]["original"] == [expected_w, expected_h]
    spec = parse_frame(sized["frame"])
    assert spec is not None
    logos_map = {
        k: LogoInfo(
            id=v["id"],
            name=v["name"],
            w=int(v["w"]),
            h=int(v["h"]),
            coverage=float(v["coverage"]),
            opaque=bool(v["opaque"]),
            lum=float(v["lum"]),
        )
        for k, v in out["logoInfoMap"].items()
    }
    lay = frame_layout(sized["photo"][0], sized["photo"][1], spec, sized["meta"], FontMetrics(), logos=logos_map)
    assert lay is not None and (lay.width, lay.height) == (expected_w, expected_h)
    clogo = next(it for it in lay.items if it["k"] == "clogo")

    for exported in (out["exportedPng"], out["exportedJpeg"]):
        exp_path = Path(exported["path"])
        assert exp_path.is_file()
        with Image.open(exp_path) as exp_img:
            assert exp_img.size == (expected_w, expected_h)
            arr = np.asarray(exp_img.convert("RGB"))
            slot = arr[clogo["y"] : clogo["y"] + clogo["h"], clogo["x"] : clogo["x"] + clogo["w"]]
            assert float(slot.max()) > 235 and float(slot.mean()) > 65
            cy, cx = slot.shape[0] // 2, slot.shape[1] // 2
            assert float(slot[cy - 2 : cy + 2, cx - 2 : cx + 2].mean()) < 35

    # 删除标志后，照片编辑与默认边框均改回 ricoh，且标志库为空
    del_resp = out["deleteResponse"]
    assert "新墨环印" in del_resp["message"] and "RICOH" in del_resp["message"]
    assert del_resp["reverted_defaults"] is True and del_resp["reverted_photos"] >= 1
    with TestClient(
        create_app(live["settings"]),
        base_url=f"http://127.0.0.1:{PORT}",
        headers={"X-GXR-Token": live["token"]},
    ) as check_client:
        assert check_client.get("/api/logos").json()["logos"] == []
        assert check_client.get("/api/defaults/finish").json()["frame"]["logo"] == "ricoh"
        assert check_client.get(f"/api/photos/{live['photo1']}/edit").json()["frame"]["logo"] == "ricoh"
        assert check_client.get(f"/api/photos/{live['photo2']}/edit").json()["frame"]["style"] == "none"
        edit3 = check_client.get(f"/api/photos/{live['photo3']}/edit").json()
        assert edit3["frame"] is None
        assert check_client.post(f"/api/photos/{live['photo3']}/output-size", json={}).json()["frame"]["logo"] == "ricoh"


def test_missing_logo_preview_and_export_fall_back_together(live: Any, tmp_path: Path) -> None:
    """打开页面并画出自定义标志后删掉 PNG，重画和导出都改回 RICOH。"""
    assert RUNTIME.is_file(), f"固定的 Playwright 运行时缺失（禁止回退系统 Chrome）：{RUNTIME}"
    mark = Image.new("RGBA", (1600, 80), (255, 0, 255, 255))
    png_path = tmp_path / "magenta-wordmark.png"
    mark.save(png_path, format="PNG")
    result = subprocess.run(
        [
            "node",
            str(Path(__file__).parent / "support" / "custom_logo_missing.mjs"),
            str(RUNTIME),
            str(live["port"]),
            live["token"],
            live["photo1"],
            str(png_path),
            str(live["settings"].home_dir),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        encoding="utf-8",
        env={**os.environ, "HOME": str(REAL_HOME)},
    )
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["agreed"] is True
    assert out["preview"]["message"] is True
    assert out["preview"]["catalogHasLogo"] is False
    assert all(item["k"] != "clogo" for item in out["preview"]["marks"])
    assert any(item["k"] == "logo" for item in out["preview"]["marks"])
    exported = Path(out["exported"]["path"])
    assert exported.is_file()
    with Image.open(exported) as exp_img:
        arr = np.asarray(exp_img.convert("RGB"))
    magenta = int(((arr[:, :, 0] > 240) & (arr[:, :, 1] < 20) & (arr[:, :, 2] > 240)).sum())
    assert magenta < 30, magenta
    sized = out["preview"]["sized"]
    spec = parse_frame(sized["frame"])
    assert spec is not None
    lay = frame_layout(sized["photo"][0], sized["photo"][1], spec, sized["meta"], FontMetrics(), logos={})
    assert lay is not None
    py_marks = [
        {"k": item["k"], "id": item.get("id"), "x": item["x"], "y": item["y"], "w": item["w"], "h": item["h"]}
        for item in lay.items
        if item.get("k") in ("clogo", "logo")
    ]
    assert py_marks == out["preview"]["marks"]


def test_transient_logo_image_failure_keeps_the_selection(live: Any, tmp_path: Path) -> None:
    """图片请求短暂失败时，文件还在，就不能把用户的标志改回 RICOH。"""
    assert RUNTIME.is_file(), f"固定的 Playwright 运行时缺失（禁止回退系统 Chrome）：{RUNTIME}"
    mark = Image.new("RGBA", (180, 80), (0, 0, 0, 0))
    ImageDraw.Draw(mark).rounded_rectangle((8, 8, 172, 72), radius=12, fill=(20, 90, 200, 255))
    png_path = tmp_path / "retry-mark.png"
    mark.save(png_path, format="PNG")
    result = subprocess.run(
        [
            "node",
            str(Path(__file__).parent / "support" / "custom_logo_image_retry.mjs"),
            str(RUNTIME),
            str(live["port"]),
            live["token"],
            live["photo1"],
            str(png_path),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        encoding="utf-8",
        env={**os.environ, "HOME": str(REAL_HOME)},
    )
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
