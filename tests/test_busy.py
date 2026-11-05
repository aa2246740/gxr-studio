"""加载态与过渡：分档阈值、按钮锁、乐观更新回滚、轮询不重叠、图库多选、撤销/重做。

真正的 busy.js、poll.js、gallery.js、preview.js 在 node 里跑（tests/support/busy_scene.mjs），接口用替身。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "web" / "assets"
SUPPORT = Path(__file__).parent / "support"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过前端状态测试")


def run(tmp_path: Path, scenario: str) -> dict:
    folder = tmp_path / "modules"
    folder.mkdir()
    for src in ASSETS.glob("*.js"):
        shutil.copy(src, folder / src.name)
    (folder / "gpu").mkdir()
    (folder / "gpu" / "engine.js").write_text(
        "export class Engine { static async create() { throw new Error('no gpu'); } }\n", encoding="utf-8"
    )
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
    shutil.copy(SUPPORT / "busy_scene.mjs", tmp_path / "busy_scene.mjs")
    res = subprocess.run(
        ["node", str(tmp_path / "busy_scene.mjs"), str(folder), scenario],
        capture_output=True,
        text=True,
        timeout=60,
        encoding="utf-8",
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])  # type: ignore[no-any-return]


def test_feedback_tiers_stay_quiet_under_150ms(tmp_path):
    """150ms 以内结束的操作什么都不显示；到 150ms 才出现原地反馈。档位是 150ms / 1s / 5s。"""
    out = run(tmp_path, "thresholds")
    assert out["tiers"] == [150, 1000, 5000]
    assert out["fastShown"] == 0 and out["fastFired"] is False
    assert out["before150"] == 0 and out["at150"] == 1 and out["slowFired"] is True


def test_busy_button_locks_at_once_spins_late_and_offers_retry(tmp_path):
    """点了马上锁住（再点不重发），150ms 后才转圈并换成「正在…」；结束恢复原文字；失败在旁边写原因并给「重试」。"""
    out = run(tmp_path, "button-lock")
    assert out["lockedAtOnce"] is True and out["spinAtOnce"] is None
    assert out["secondResult"] is True and out["callsWhileLocked"] == 1
    assert out["spinAfter150"] == "true" and out["textWhileBusy"] == "正在重建…"
    assert out["result"] == "ok" and out["unlocked"] is True and out["textAfter"] == "重建索引"
    assert out["failedResult"] is True
    assert out["inlineError"].startswith("图库读不了") and out["inlineError"].endswith("重试")
    assert out["errorRole"] == "alert"
    assert out["retryRan"] is True and out["errorCleared"] is True


def test_optimistic_update_rolls_back_when_the_save_fails(tmp_path):
    """乐观更新：界面先变，再保存；保存失败界面变回去，错误交给调用方提示。"""
    out = run(tmp_path, "optimistic")
    assert out["okResult"] == "saved" and out["okState"] is True
    assert out["badResult"] is True and out["badState"] is True and out["reported"] == "没存上"
    assert out["log"] == ["apply", "commit:true", "apply", "commit:false", "rollback"]


def test_polling_never_overlaps_and_pauses_in_the_background(tmp_path):
    """U15：上一个请求没回来不发下一个；页面在后台不发，回到前台马上补一次；stop() 以后不再发。"""
    out = run(tmp_path, "poll")
    assert out["runsWhileSlow"] == 1
    assert out["runsAfterRelease"] >= 2
    assert out["runsWhileHidden"] == 0
    assert out["catchUpOnVisible"] == 1
    assert out["runsAfterStop"] == 0
    assert out["maxInFlight"] == 1


def test_gallery_shift_range_and_toggle_selection(tmp_path):
    """U12：⇧ 点选按网格顺序选一段（正反两个方向都行），⌘ 点选切换单张，不改传入的集合。"""
    out = run(tmp_path, "gallery-select")
    assert out["forward"] == ["b", "c", "d"] and out["backward"] == ["b", "c", "d"]
    assert out["keepBase"] == ["a", "d", "e"]
    assert out["noAnchor"] == ["c"] and out["unknownTarget"] == []
    assert out["toggleOn"] == ["a", "b"] and out["toggleOff"] == ["a"]
    assert out["inputUntouched"] == ["a"]


def test_redo_puts_back_what_undo_took_and_a_new_change_clears_it(tmp_path):
    """⇧⌘Z / Ctrl+Y 重做：撤销掉的一步能放回来；没有可重做的时候不动；新的改动让重做作废。"""
    out = run(tmp_path, "redo")
    assert out["start"] == 2 and out["afterUndo"] == 1 and out["afterUndo2"] == 0
    assert out["afterRedo"] == 1 and out["afterRedo2"] == 2 and out["redoEmptyNoop"] == 2
    assert out["redoClearedByNewChange"] == 0 and out["afterNew"] == 5


def test_lut_folder_import_reports_n_of_n(tmp_path):
    """LUT 文件夹导入：每传完一个报一次 n/N，跳过的系统文件不算进总数；不传回调照常工作。"""
    out = run(tmp_path, "lut-import-progress")
    assert out["calls"] == [[0, 2], [1, 2], [2, 2]]
    assert out["status"] == ["imported", "error"]
    assert out["noCallback"] == 1


def test_progress_bars_scale_instead_of_changing_width():
    """进度条用 transform 缩放（--f），不改 width：动起来不触发重排。"""
    for name in ("export.js", "import.js", "lut.js"):
        text = (ASSETS / name).read_text(encoding="utf-8")
        assert "style.width" not in text, name
        assert "setFraction(" in text, name
    css = (ASSETS / "app.css").read_text(encoding="utf-8")
    assert "transform: scaleX(var(--f, 0))" in css


MOTION_PROPS = {
    "transform",
    "opacity",
    "background",
    "background-color",
    "color",
    "border-color",
    "box-shadow",
    "filter",
    "outline-color",
    "none",
}


def test_motion_respects_reduced_motion_and_never_animates_layout():
    """动效只用 transform / opacity，时长和缓动都走 token；系统要求减少动态时关掉动画。"""
    css = (ASSETS / "app.css").read_text(encoding="utf-8")
    assert "@media (prefers-reduced-motion: reduce)" in css
    for m in re.finditer(r"transition:\s*([^;]+);", css):
        value = m.group(1)
        for prop in re.findall(r"(?:^|,)\s*([a-z-]+)\s", value):
            assert prop in MOTION_PROPS, value
        assert re.search(r"\d+m?s\b", value) is None, f"时长要用 token：{value}"


def test_recipe_slots_fall_back_to_gr_names_and_never_name_other_brands(tmp_path):
    """U7：/api/recipes 给了名字就用；没给时 1–4 是 GR 正片 / GR 负片 / GR 高对比黑白 / 原厂标准，5–8 是「空位 N」。"""
    out = run(tmp_path, "recipe-names")
    assert out["names"] == [
        "01 GR 正片",
        "02 GR 负片",
        "03 GR 高对比黑白",
        "04 原厂标准",
        "05 我的街拍",
        "06 空位 6",
        "07 空位 7",
        "08 空位 8",
    ]
    assert out["unknownUnit"] == "03 GR 高对比黑白"
    for brand in ("富士", "徕卡", "Fuji", "Leica", "Classic", "Velvia", "Provia", "Acros"):
        assert not any(brand in n for n in out["base"]), brand


def test_gpu_preview_is_on_unless_this_browser_turned_it_off(tmp_path):
    """U10：显卡实时预览默认用；设置里关掉只记在这台电脑的浏览器里，开关一变当前照片马上重画（发事件）。"""
    out = run(tmp_path, "gpu-pref")
    assert out["defaultOn"] is True
    assert out["afterOff"] is False and out["stored"] == "off"
    assert out["afterOn"] is True
    assert out["events"] == [False, True]


def test_generic_server_errors_are_replaced_by_what_the_user_was_doing(tmp_path):
    """U17：后端只给了通用的「无法完成操作」时，提示换成这个操作自己的说法；后端有具体说明时照用。"""
    out = run(tmp_path, "error-text")
    assert out["generic"] == "没有导出这个 LUT。请重试。"
    assert out["specific"] == "照片已经不在图库里了"
    assert out["noFallback"] == "无法完成操作。请重试。"
    # 前端自己不再写这句通用的话（只剩 api.js 给后端没说明时兜底）
    for src in ASSETS.glob("*.js"):
        if src.name != "api.js":
            assert "无法完成操作" not in src.read_text(encoding="utf-8"), src.name
