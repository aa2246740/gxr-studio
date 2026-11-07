"""复制 / 粘贴调整在编辑器里的逻辑（node 里跑真正的 viewer.js、preview.js、saves.js、paste_*.js，接口用替身）。

脚本在 tests/support/paste_edits.mjs。图库里的批量粘贴、快捷键在输入框里不触发，在真 Chrome 里测（test_paste_browser.py）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_edit_route import SUPPORT, prepare

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过前端状态测试")


def run(tmp_path: Path, scenario: str) -> dict:
    folder = prepare(tmp_path)
    shutil.copy(SUPPORT / "paste_edits.mjs", tmp_path / "paste_edits.mjs")
    res = subprocess.run(  # noqa: S603
        ["node", str(tmp_path / "paste_edits.mjs"), str(folder), scenario],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        encoding="utf-8",
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])  # type: ignore[no-any-return]


def test_copy_takes_recipe_and_adjust_by_default_and_says_so(tmp_path):
    out = run(tmp_path, "copy")
    assert out["clipBefore"] is None and out["copied"] is True
    assert out["groups"] == ["recipe", "adjust"]
    assert out["sourceKeys"] == ["adjust", "curve", "lut", "recipe"], "几何、裁剪、边框、时间戳默认不带"
    assert out["exposure"] == 0.5 and out["recipeSlot"] == 2
    assert out["from"] == {"id": "a0", "name": "R0010001.DNG"}
    assert out["toast"].startswith("已复制：配方 / 风格、调整") and out["hasChange"], "toast 里能直接改选项"
    assert out["requests"] == 0, "复制只是记下来，不请求服务器"
    assert "复制调整" in out["titlebar"] and "粘贴调整" in out["titlebar"], "按钮在顶栏，不用记快捷键"
    assert "⇧⌘V" in out["pasteButtonTitle"] and "R0010001.DNG" in out["pasteButtonTitle"], "按钮的提示说明粘贴的是哪张的什么"


def test_copy_options_are_remembered_and_empty_groups_are_not_copied(tmp_path):
    out = run(tmp_path, "copy-options")
    assert out["initial"] == ["recipe", "adjust"]
    assert "拉直、透视、畸变（不含旋转和翻转）" in out["dialogText"] and "这张没有设置" in out["dialogText"]
    assert out["remembered"] == ["adjust", "geometry"] == out["chosen"]
    assert out["groups"] == ["adjust"], "几何这张没有设置，不进剪贴板"
    assert out["toast"] == "已复制：调整。几何这张没有设置，没有复制。"
    assert out["dialogClosed"]
    assert "没有勾选任何项目" in out["noneToast"] and out["clipStill"] == ["adjust"]


def test_paste_in_the_editor_is_queued_with_the_latest_rev_and_is_one_undo_step(tmp_path):
    out = run(tmp_path, "paste-is-one-undo-step")
    assert out["pasteBefore"] is True
    assert out["pastePosts"] == [{"targets": [{"id": "b0", "expected_rev": 5}], "groups": ["recipe", "adjust"], "include": True}]
    assert (out["adjAfter"], out["recipeAfter"]) == (7, 2) and out["curveAfter"] == [[0, 0], [128, 120], [255, 255]]
    assert out["undoAfter"] == out["undoBefore"] + 1, "撤销记录里只多一步"
    assert out["status"] == "saved" and out["recRev"] == out["viewRev"] == 6
    assert out["toast"] == "已套用：配方 / 风格、调整" and out["hasUndoAction"]
    assert out["putsBeforeUndo"] == 0, "界面是用服务器写完的内容更新的，不会再多写一遍"
    # 撤销一次：回到粘贴之前
    assert out["adjUndone"] == 0 and out["recipeUndone"] is None
    assert out["undoPut"] == ["adjust", "curve", "expected_rev", "recipe"]
    assert out["undoLeft"] == out["undoBefore"] and out["redoLeft"] == 1


def test_paste_waits_for_the_saves_already_queued(tmp_path):
    out = run(tmp_path, "paste-waits-for-saves")
    assert out["postsWhileHeld"] == 0 and out["statusWhileHeld"] == "saving"
    assert out["pastes"] == [[{"id": "b0", "expected_rev": 6}]], "带的是前一笔写完之后的 rev"
    assert out["order"] == ["PUT /api/photos/b0/edit", "POST /api/edits/paste"]
    assert out["finalRev"] == 7


def test_paste_is_refused_with_a_reason_and_sends_nothing(tmp_path):
    out = run(tmp_path, "paste-refused")
    assert "先复制一张照片的调整" in out["noClip"] and "⇧⌘C" in out["noClip"]
    assert "就是复制来源" in out["sameToast"]
    assert "有修改没有保存" in out["failedToast"] and "重试" in out["failedToast"]
    assert out["posts"] == 0


def test_a_partly_applied_paste_names_what_was_skipped_and_a_conflict_changes_nothing(tmp_path):
    out = run(tmp_path, "paste-skipped-and-conflict")
    assert out["partialToast"] == "已套用：调整；这张只有 JPG，没有套用配方"
    assert out["adj"] == 7 and out["recipe"] is None
    assert out["conflictToast"] == "这张照片已在别处修改，没有粘贴。" and out["undoUnchanged"]
