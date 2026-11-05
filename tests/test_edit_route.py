"""编辑保存与路由的正确性（R03、R04、R05、R11；R16 要求真正执行模块）。

真正的 viewer.js、preview.js、router.js、export.js、dom.js 在 node 里跑，接口用替身，
哪个请求什么时候返回由脚本控制（tests/support/edit_route.mjs）。断言落在实际发出的请求和页面状态上。
和这些行为无关的面板（配方、调整、裁剪、边框、时间戳、画面）换成只返回空节点的替身。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "web" / "assets"
SUPPORT = Path(__file__).parent / "support"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过前端状态测试")

# 只是被 viewer.js 引进来、和保存与路由无关的模块：按真文件导出的名字生成空实现
STUBBED = ("recipes.js", "adjust.js", "crop.js", "correct.js", "frame_panel.js", "stamp_panel.js", "stage.js", "gallery.js", "lut_library.js")
EXPORT_RE = re.compile(r"^export\s+(?:async\s+)?(function\*?|const|let|class)\s+(\w+)", re.M)


def _stub(source: str) -> str:
    lines = []
    for kind, name in EXPORT_RE.findall(source):
        if kind.startswith("function"):
            lines.append(f"export function {name}() {{ return document.createElement('div'); }}")
        elif kind == "class":
            lines.append(f"export class {name} {{}}")
        else:
            lines.append(f"export const {name} = {{}};")
    return "\n".join(lines) + "\n"


def run(tmp_path: Path, scenario: str) -> dict:
    folder = tmp_path / "modules"
    folder.mkdir()
    for src in ASSETS.glob("*.js"):
        text = src.read_text(encoding="utf-8")
        (folder / src.name).write_text(_stub(text) if src.name in STUBBED else text, encoding="utf-8")
    (folder / "gpu").mkdir()
    engine = "export class Engine { static async create() { throw new Error('no gpu'); } }\n"
    (folder / "gpu" / "engine.js").write_text(engine, encoding="utf-8")
    (folder / "api.js").write_text(
        "export class ApiError extends Error {\n"
        "  constructor(status, code, message, data = null) {\n"
        "    super(message); this.status = status; this.code = code; this.data = data;\n"
        "  }\n"
        "}\n"
        "export const api = (...a) => globalThis.apiStub(...a);\n"
        "export const apiBlob = (...a) => globalThis.apiBlobStub(...a);\n",
        encoding="utf-8",
    )
    (folder / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
    shutil.copy(SUPPORT / "mini_dom.mjs", tmp_path / "mini_dom.mjs")
    shutil.copy(SUPPORT / "edit_route.mjs", tmp_path / "edit_route.mjs")
    res = subprocess.run(
        ["node", str(tmp_path / "edit_route.mjs"), str(folder), scenario],
        capture_output=True,
        text=True,
        timeout=60,
        encoding="utf-8",
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])  # type: ignore[no-any-return]


def test_r03_reset_on_a_then_switch_to_b_never_writes_b(tmp_path):
    """R03：A 上点「复原」，A 的编辑保存在 B 打开之后才返回：B 没有任何写请求；A 的裁剪写回整张、0°，用的是 A 的 rev。"""
    out = run(tmp_path, "reset-then-switch")
    assert out["hadReset"]
    assert out["shownBeforeRelease"] == "b0" and out["shownAfter"] == "b0"
    assert out["putsB"] == []
    assert [p["url"] for p in out["putsA"]] == ["/api/photos/a0/edit", "/api/photos/a0/crop"]
    edit, crop = out["putsA"]
    assert edit["rev"] == 1 and crop["rev"] == 2  # 裁剪接在 A 的编辑之后，用 A 的新 rev
    assert crop["angle"] == 0 and crop["rect"] == [0, 0, 1, 1]
    assert out["pageHasB"] and not out["pageHasA"]


def test_r03_reset_then_back_to_gallery_finishes_on_a_only(tmp_path):
    """R03：复原等待中回到图库：A 的两笔照常写完，没有别的写入，页面停在图库。"""
    out = run(tmp_path, "reset-then-leave")
    assert out["putsA"] == ["/api/photos/a0/edit", "/api/photos/a0/crop"]
    assert out["putsB"] == []
    assert out["page"] == "GALLERY"


def test_r04_queued_save_is_written_after_switching_photos(tmp_path):
    """R04：第一笔在路上、第二笔排队时切到 B：第二笔照样写进 A（rev 接第一笔）；切回 A 时读编辑要等写完。"""
    out = run(tmp_path, "queued-save-survives-switch")
    assert out["results"] == [True, True]
    assert out["putsA"] == [
        {"url": "/api/photos/a0/edit", "keys": ["adjust", "expected_rev"], "rev": 1},
        {"url": "/api/photos/a0/edit", "keys": ["curve", "expected_rev"], "rev": 2},
    ]
    assert out["putsB"] == 0
    assert out["statusWhileSaving"] == "saving" and out["statusAfter"] == "saved"
    # 切回 A 时，在 A 的写入放行之前不读编辑记录（否则读到旧的 rev，之后的保存会 409）
    assert out["editReadsBeforeRelease"] == 1
    assert out["editReadsAfter"] == 2
    assert out["reopenedRev"] == out["serverRev"] == 3


def test_r05_failed_save_stops_export_and_offers_retry(tmp_path):
    """R05：保存失败后 flushEdits() 拒绝；点导出不发导出请求，表单里有「重试保存」；重试成功后才导出。"""
    out = run(tmp_path, "failed-save-blocks-export")
    assert out["saved"] is False and out["status"] == "failed"
    assert "有修改没有保存" in out["saveLine"] and "重试" in out["saveLine"]  # 大图里也有重试入口
    assert out["flush"] == "unsaved" and "磁盘已满" in out["flushMessage"]
    assert out["exportPostsAfterFailure"] == 0
    assert "有修改没有保存" in out["formText"] and "导出已停止" in out["formText"]
    assert out["hasRetry"]
    assert out["statusAfterRetry"] == "saved"
    # 第一次失败的 PUT，再加上重试的那一次：重试带着原来的修改和服务端现在的 rev
    assert out["retryPut"][-1] == {"keys": ["adjust", "expected_rev"], "rev": 1}
    assert out["exportPostsAfterRetry"] == 1


def test_r11_slow_photo_response_does_not_replace_the_newer_route(tmp_path):
    """R11：A 的照片请求比 B 晚回来：页面和快捷键都留在 B，A 的编辑记录不再去读。"""
    out = run(tmp_path, "stale-route-reversed")
    assert out["shownBefore"] == "b0" and out["shownAfter"] == "b0"
    assert out["aStillCurrent"] is False
    assert out["pageHasB"] and not out["pageHasA"]
    assert out["keysFor"] == "b0"
    assert out["aEditReads"] == 0


def test_r11_slow_photo_response_after_returning_to_gallery(tmp_path):
    """R11：打开 A 马上回图库，A 的响应才到：页面停在图库，没有装上大图的快捷键。"""
    out = run(tmp_path, "stale-route-back-to-gallery")
    assert out["page"] == "GALLERY"
    assert out["shown"] is None and out["keys"] is None
    assert out["aStillCurrent"] is False and out["aEditReads"] == 0


def test_r11_opening_from_the_gallery_is_immediate_and_a_late_refresh_is_ignored(tmp_path):
    """从图库打开：照片信息已经在列表里，页面立刻出来（不等 /api/photos/<id>）；
    后台那次刷新晚于切到 B 才回来时，页面和快捷键都留在 B。"""
    out = run(tmp_path, "cached-open-stale-refresh")
    assert out["shownFirst"] == "a0" and out["pageHadA"]
    assert out["shownAfter"] == "b0" and out["pageHasB"] and not out["pageHasStale"]
    assert out["keysFor"] == "b0"
