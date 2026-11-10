"""配方标签的图片卡片：说明表、别的单元的配方分组和原因、缓存键、点卡片写进去的编辑、键盘、「未校准」的位置。

前端部分用 node 跑 tests/support/look_cards.mjs（接口是 fetch 替身，DOM 是 mini_dom，不画图）。没有 node 的机器跳过。
画图、显卡、和舞台的一致、拖滑块不发请求在真浏览器里测：tests/test_look_cards_browser.py。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "web" / "assets"
SCENE = Path(__file__).resolve().parent / "support" / "look_cards.mjs"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")


def scene(name: str) -> dict:
    res = subprocess.run(["node", str(SCENE), str(ASSETS), name], capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


@needs_node
def test_descriptions_are_short_and_unknown_looks_get_none():
    out = scene("model")
    assert out["builtin"] == ["饱和度高", "饱和度略低", "黑白，对比高", "原厂标准色彩", None, None]  # 5–8 是用户空位，不写
    assert out["known"] == "黑白"
    assert out["unknown"] is None  # 不认识的 Look 不编说明
    assert out["camera"] == "相机里的配方"
    assert out["computerTitled"] == "来自 Portra 400"
    assert out["computerUntitled"] == "用 LUT 生成"
    assert out["longest"] <= out["max"] == 14


ALLOWED_WORDS = {"饱和度高", "饱和度低", "饱和度略低", "对比高", "对比低", "偏亮", "偏暗", "偏暖", "黑白", "原厂标准色彩"}


@needs_node
def test_descriptions_only_use_the_words_the_measurement_rule_defines():
    # look_model.js 表上面的规则只定义了这些词（和阈值）；表里别的说法就是没有依据的
    out = scene("model")
    used = {word for text in out["texts"] for word in text.split("，")}
    assert used <= ALLOWED_WORDS, used - ALLOWED_WORDS
    model = (ASSETS / "look_model.js").read_text(encoding="utf-8")
    rules = ("≥ 1.15 → 饱和度高", "0.75–0.9 → 饱和度略低", "≤ 0.75 → 饱和度低", "→ 黑白")
    rules += ("≥ 1.3 → 对比高", "≤ 0.8 → 对比低", "→ 偏亮", "→ 偏暗", "→ 偏暖")
    for rule in rules:
        assert rule in model, rule


@needs_node
def test_other_unit_recipes_are_grouped_with_the_reason():
    out = scene("model")
    # 28mm 的照片：50mm 和 MOUNT 的配方灰着列出，原因写清楚是为谁做的、这张是什么镜头拍的
    assert out["g28"]["usable"] == ["b"]
    assert out["g28"]["other"] == [["a", "为 A12 50mm 做的配方；这张是 28mm 拍的"], ["c", "为 A12 MOUNT 做的配方；这张是 28mm 拍的"]]
    assert out["gMount"]["usable"] == ["c"]
    assert out["gMount"]["other"] == ["为 A12 50mm 做的配方；这张是 MOUNT 拍的", "为 A12 28mm 做的配方；这张是 MOUNT 拍的"]
    assert out["unknownPhoto"][0] == "为 A12 28mm 做的配方；认不出这张照片的镜头"
    assert out["none"] == {"usable": [], "other": []}


@needs_node
def test_card_cache_key_changes_only_with_what_changes_the_picture():
    out = scene("model")
    assert out["same"] is True
    assert all(out["changes"].values()), out["changes"]  # 照片、相机数据、小图大小、LUT、调整、曲线、几何、裁切、档位、底子
    assert out["ignores"] is True  # 边框、时间戳、选中哪张、换标签
    assert out["lru"] == [1, None, 3, 2]  # 最近用过的留着


@needs_node
def test_cache_key_follows_the_preview_path_and_the_display_aspect():
    out = scene("shared-key")
    assert out["stable"] is True
    assert out["aspectChanges"] is True  # 透视 / 畸变的导出框到了，舞台重新拉伸，卡片也要重画
    assert out["wayChanges"] is True and out["backAgain"] is True  # 本机关掉显卡预览：显卡画的图不再用；打开又回到原来的键
    assert out["flagChanges"] is True  # 服务器开关关了


@needs_node
def test_a_gesture_aborts_the_server_card_that_is_already_on_its_way():
    out = scene("drag-aborts")
    assert out["started"] == 1 and out["signalGiven"] is True  # 请求带取消开关（327c3ea 没有）
    assert out["abortedAtOnce"] is True  # 手按下去，在路上的请求当场取消
    assert out["rendersWhileDown"] == 1 and out["paintedWhileDown"] == 0  # 按着的时候不再发新的，也什么都没画
    assert out["failedWhileDown"] is False  # 取消的不算失败
    assert out["rendersAfterRelease"] == 2  # 松手以后重来
    assert out["paintedAfter"] == 1 and out["failedAfter"] is False and out["serverCount"] == 1


@needs_node
def test_cards_are_buttons_with_names_and_the_current_one_is_marked():
    out = scene("panel")
    assert out["tag"] == "BUTTON"
    assert out["names"][:3] == ["01 GR 正片", "02 GR 负片", "04 原厂标准"]
    assert "Acros" in out["names"] and "FCC-001" in out["names"] and "50 的配方" in out["names"]
    assert out["acros"]["label"] == "Acros，黑白，当前配方" and out["acros"]["pressed"] == "true"
    assert out["odd"]["label"] == "没见过的名字" and out["odd"]["descLines"] == 0  # 没有说明就不写
    assert out["fcc"]["label"] == "FCC-001，相机里的配方" and out["fcc"]["title"] == "相机里的配方"
    assert out["mine"]["label"] == "50 的配方，来自 Portra 400"
    assert out["mine"]["title"] == "电脑上 · 来自 LUT「Portra 400」 · 质量证据未齐 · 本机草稿"
    # 只有一张卡片能用 Tab 进入：当前配方
    assert out["rovingZero"] == 1 and out["rovingOnCurrent"] == "look:acros"


@needs_node
def test_clicking_a_card_writes_the_same_edit_as_the_old_recipe_button():
    out = scene("panel")
    # 这个编辑记录里 jlaunch/2 记着上次的对比 +1；原来的按钮点下去写的就是这个（档位取这个配方上次的旋钮，当前配方的旋钮存进 memory）
    assert out["puts"] == 1
    assert out["applied"] == {"unit": "jlaunch", "kind": "builtin", "slot": 2, "levels": {"contrast": 1}}
    assert out["putBody"]["recipe"] == {
        "kind": "builtin",
        "unit": "jlaunch",
        "slot": 2,
        "levels": {"contrast": 1},
        "memory": {"jlaunch/2": {"contrast": 1}, "jlaunch/look/acros": {}},
    }
    assert out["afterOdd"] == {"unit": "jlaunch", "kind": "look", "slot": 4, "id": "odd", "levels": {}}
    assert out["memoryAfterOdd"]["jlaunch/2"] == {"contrast": 1}
    assert out["afterMine"] == {"unit": "jlaunch", "kind": "local", "slot": 2, "id": "c" * 64, "levels": {}}
    assert "质量证据未齐，当前使用本机草稿" in out["localQuality"]
    assert "当前效果未经质量评估" in out["adjustedLocalQuality"]


@needs_node
def test_full_record_cards_keep_their_names_and_distinct_identities_for_the_same_base():
    out = scene("panel")
    assert out["recordNames"] == ["Synthetic a", "Synthetic b"]
    first = {"unit": "jlaunch", "kind": "record", "slot": 2, "id": "a" * 64, "levels": {}}
    second = {**first, "id": "b" * 64}
    assert out["firstRecord"] == first and out["secondRecord"] == second
    assert [{key: body[key] for key in first} for body in out["recordBodies"]] == [first, second]


@needs_node
def test_arrow_keys_move_between_cards_and_do_not_change_photo():
    out = scene("panel")
    assert out["rightKey"] == [True, True]  # 拦下默认动作，也不让大图把左右键当成换照片
    assert out["focusAfterRight"] is True
    assert out["tabindexAfterRight"] == ["-1", "0"]
    assert out["otherKey"] == [False, False]


@needs_node
def test_uncalibrated_moves_under_the_chosen_recipe():
    out = scene("panel")
    # 50mm、ISO 200 的照片是校准过的：选了配方也没有「未校准」
    assert out["calib"] is None and "未校准" not in out["chips"]
    assert "当前：Acros" in out["cur"]
    # MOUNT 的 ISO 800 没有实拍校准：选了配方，名字下方说明 ISO 原因
    mount = scene("panel-mount")
    assert mount["calib"]["text"] == "未校准：颜色可能有些偏差"
    assert "ISO 800" in mount["calib"]["title"] and "只做了 ISO 200" in mount["calib"]["title"]
    assert "未校准" not in mount["chips"]
    # 没选配方就没有预测，这一行不写
    plain = scene("panel-mount-plain")
    assert plain["calib"] is None and plain["cur"] is None


def test_uncalibrated_chip_is_not_on_the_photo():
    preview = (ASSETS / "preview.js").read_text(encoding="utf-8")
    assert "text: '未校准'" not in preview and "uncalibratedReason" not in preview
    recipes = (ASSETS / "recipes.js").read_text(encoding="utf-8")
    assert "uncalibratedReason(view, photoUnit)" in recipes and "未校准：颜色可能有些偏差" in recipes
    assert recipes.index("if (ed.recipe) {") < recipes.index("calib-note")  # 只在选了配方时才有这一行


@needs_node
def test_other_unit_recipes_are_listed_greyed_with_a_way_forward():
    out = scene("panel-mount-plain")
    assert out["otherRows"] == [
        "50 的配方为 A12 50mm 做的配方；这张是 MOUNT 拍的",
        "28 的配方为 A12 28mm 做的配方；这张是 MOUNT 拍的",
    ]
    assert out["otherAria"] == ["true", "true"]
    assert out["hasOtherNamedCard"] is False  # 灰着的不是能点的卡片
    assert out["link"] == "#/recipes?unit=l08firm"  # 去配方页，预选这张照片的镜头单元


@needs_node
def test_jpg_only_says_why_in_one_plain_line():
    out = scene("panel-approx")
    assert out["chips"] == ["近似"] and out["approxLine"] is True
    # 电脑上的配方在 JPG 上不能用（和以前一样），点了不写
    assert out["mine"]["disabled"] == "true"
    # 内置配方照样能点
    assert out["puts"] == 1 and out["applied"]["slot"] == 2
    none = scene("panel-jpg-none")
    assert "配方需要 RAW 文件" in none["text"]
    assert out["otherKey"] == [False, False]


def test_router_accepts_a_unit_parameter_on_the_recipes_page():
    router = (ASSETS / "router.js").read_text(encoding="utf-8")
    assert "h.startsWith('#/recipes?')" in router


def test_new_modules_stay_under_the_line_cap():
    for name in ("look_cards.js", "look_model.js", "recipes.js"):
        assert len((ASSETS / name).read_text(encoding="utf-8").splitlines()) <= 400, name
