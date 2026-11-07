"""反馈和用词：导入结果的说法、相机页的导入反馈、「未校准」的原因、HDR 文件名、两种「我的配方」、图库选择。

前端部分用 node 跑 tests/support/feedback_copy.mjs（接口是 fetch 替身，DOM 是 mini_dom）。没有 node 的机器跳过。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from gxr_studio.camdata import calib
from gxr_studio.cards import coverage as coverage_mod
from gxr_studio.cards.coverage import Coverage
from gxr_studio.cards.messages import not_importable_message

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "web" / "assets"
SCENE = Path(__file__).resolve().parent / "support" / "feedback_copy.mjs"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")


def scene(name: str) -> dict:
    res = subprocess.run(["node", str(SCENE), str(ASSETS), name], capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


# ---- 2. 存储卡面板的说法 ----------------------------------------------------------------------------


def _cov(**kw) -> Coverage:
    base = {"complete": True, "dcim": "present", "total": 19, "covered": 19}
    return Coverage(**{**base, **kw})


def test_photos_imported_is_weaker_than_all_imported_but_still_needs_proof():
    extras = ["DCIM/100RICOH/Thumbs.db", "DCIM/NOTE.TXT"]
    cov = _cov(not_importable=extras)
    assert cov.all_imported is False and cov.photos_imported is True
    assert cov.to_dict()["photos_imported"] is True and cov.to_dict()["all_imported"] is False
    assert _cov().all_imported is True and _cov().photos_imported is True
    # 照片没证明、核对不完整、DCIM 不是真目录、一张照片都没有：都不能说
    assert _cov(not_importable=extras, uncovered=["DCIM/100RICOH/R0000105.JPG"], covered=18).photos_imported is False
    assert _cov(not_importable=extras, complete=False).photos_imported is False
    assert _cov(not_importable=extras, dcim="link").photos_imported is False
    assert _cov(not_importable=extras, total=0, covered=0).photos_imported is False
    # 名字像照片或视频、却没被收进来的文件（废纸篓里的 JPG、.MOV）：不能说「照片都已导入」
    assert _cov(not_importable=[".Trashes/501/R0090003.JPG"]).photos_imported is False
    assert _cov(not_importable=["DCIM/100RICOH/R0000200.MOV"]).photos_imported is False
    assert coverage_mod.MEDIA_SUFFIXES >= {".jpg", ".dng", ".avi"}
    # 没有后缀、后缀不认识：可能是改过名的照片，证明不了不是，就不说
    assert _cov(not_importable=["DCIM/100RICOH/R0000300"]).photos_imported is False
    assert _cov(not_importable=["DCIM/100RICOH/R0000300.XYZ"]).photos_imported is False
    # 开头字节像照片（读过开头的文件，例如废纸篓里没有后缀的 JPEG）：不管叫什么都不放过
    trash = "DCIM/.Trashes/501/IMG0001"
    assert _cov(not_importable=[trash], media_like=[trash]).photos_imported is False
    assert _cov(not_importable=["DCIM/NOTE.TXT"], media_like=["DCIM/NOTE.TXT"]).photos_imported is False
    assert _cov(not_importable=["DCIM/NOTE.TXT"]).photos_imported is True


def test_strict_scan_remembers_files_whose_bytes_look_like_photos(tmp_path):
    # 名字像系统文件、开头却是 JPEG 的文件：扫描记下它，核对就不会说"照片都已导入"
    from gxr_studio.cards.discovery import CardReader, scan_card_strict

    root = tmp_path / "card"
    (root / "DCIM" / "100RICOH").mkdir(parents=True)
    (root / "DCIM" / ".Trashes" / "501").mkdir(parents=True)
    (root / "DCIM" / ".Trashes" / "501" / "IMG0001").write_bytes(b"\xff\xd8\xff\xe0" + b"\0" * 60)
    (root / "DCIM" / "Thumbs.db").write_bytes(b"\xd0\xcf\x11\xe0" + b"\0" * 60)
    st = root.lstat()
    scan = scan_card_strict(root, CardReader(), (st.st_dev, st.st_ino))
    others = [e.relpath for e in scan.other_files]
    assert "DCIM/.Trashes/501/IMG0001" in others
    assert scan.media_like == ["DCIM/.Trashes/501/IMG0001"]


def test_not_importable_message_only_claims_what_the_proof_supports():
    assert not_importable_message("", 2, True) == "照片都已导入；另有 2 个其他文件不会导入"
    assert not_importable_message("已导入 3 张照片", 2, True) == "已导入 3 张照片，卡上的照片都已在图库里；另有 2 个其他文件不会导入"
    assert not_importable_message("", 2, False) == "存储卡上有 2 个文件不会被导入"
    assert not_importable_message("已导入 3 张照片", 2, False) == "已导入 3 张照片。存储卡上有 2 个文件不会被导入"
    for text in (not_importable_message("", 2, True), not_importable_message("", 2, False)):
        assert "格式化" not in text and "推出" not in text and "弹出" not in text


@needs_node
def test_summary_wording_follows_the_coverage_proof():
    out = scene("summary")
    proven = out["provenSummary"]
    assert proven["text"] == "照片都已导入；另有 2 个其他文件不会导入"
    assert "Thumbs.db" in proven["detail"] and "NOTE.TXT" in proven["detail"]
    assert out["provenEject"] is False  # 卡上还有别的文件：不说可以推出
    assert out["unprovenSummary"]["text"] == "存储卡上有 2 个文件不会被导入"
    assert out["legacySummary"]["text"] == "存储卡上有 2 个文件不会被导入"  # 后端没给证明就按没有证明说
    assert out["freshSummary"]["text"] == "已导入 3 张照片，卡上的照片都已在图库里；另有 2 个其他文件不会导入"
    assert "格式化" not in out["allText"].replace("请勿格式化", "")


@needs_node
def test_import_toast_says_what_happened_and_never_overclaims():
    out = scene("summary")
    assert out["cleanToast"] == {"kind": "ok", "text": "这张存储卡上的照片都已在图库里，没有新的可导入"}
    assert out["cleanEject"] is True
    assert out["provenToast"]["text"] == "这张存储卡上的照片都已在图库里，没有新的可导入；另有 2 个其他文件不会导入"
    assert out["unprovenToast"]["text"] == "存储卡上有 2 个文件不会被导入" and "在图库里" not in out["unprovenToast"]["text"]
    assert out["badToast"]["kind"] == "error" and "在图库里" not in out["badToast"]["text"] and "请勿格式化存储卡" in out["badToast"]["text"]
    assert out["badEject"] is False
    assert out["checkFailedToast"]["kind"] == "error" and "在图库里" not in out["checkFailedToast"]["text"]
    scanning, running, checking = out["lines"]
    assert scanning == {"text": "正在读取存储卡…", "fraction": None}
    assert running["text"] == "正在导入 3/19" and 0 < running["fraction"] < 1
    assert checking == {"text": "正在核对存储卡上的照片…", "fraction": None}


# ---- 1. 相机页的「导入照片」 -------------------------------------------------------------------------


@needs_node
@pytest.mark.parametrize(
    ("name", "toast"),
    [
        ("camera-import", "这张存储卡上的照片都已在图库里，没有新的可导入"),
        ("camera-import-extras", "这张存储卡上的照片都已在图库里，没有新的可导入；另有 2 个其他文件不会导入"),
    ],
)
def test_camera_page_import_shows_progress_and_the_outcome(name, toast):
    out = scene(name)
    assert out["beforeButton"] is True and "已导入 19 张照片" in out["beforeResult"]  # 插卡时自动导入的结果留在页面上
    assert out["progressText"] and "取消导入" in out["progressText"] and out["cancelButton"] is True
    assert out["importButtonWhileRunning"] is False
    assert "正在导入 3/19" in out["progressText"] and "正在导入 12/19" in out["progressLater"]
    assert toast in out["toast"]
    assert out["progressAfter"] is False and out["buttonAfter"] is True
    assert out["resultAfter"]
    assert out["posts"] == 2  # 自动一次，手动一次；结束时不会再自己发起
    assert out["leftPage"] == "GALLERY"


# ---- 5. 「未校准」的提示 ----------------------------------------------------------------------------


@needs_node
def test_uncalibrated_tooltip_names_the_real_reason():
    out = scene("calibration-note")
    assert tuple(out["isos"]) == calib.CALIBRATED_ISOS
    assert out["iso400"].startswith("这张照片是 ISO 400，颜色校准只做了 ISO 200 和 ISO 1600")
    assert "ISO 500" in out["iso500"]
    assert "读不到这张照片的 ISO" in out["noIso"]
    assert "28mm" in out["q28"] and "只做了 A12 50mm" in out["q28"] and "借用 50mm 的颜色模型" in out["q28"]
    assert "MOUNT" in out["mount"] and "借用 50mm 的颜色模型" in out["mount"]
    assert out["unknown"].startswith("认不出这张照片用的镜头单元")
    assert out["lut"].startswith("套用了 LUT")
    assert "28mm 单元" in out["cross"]
    assert out["other"].startswith("这张照片没有对应的颜色校准")
    # 50mm 的 ISO 400 不再被说成「使用 A12 50mm 的颜色模型」，而且不出现机器值
    for key in ("iso400", "iso500", "q28", "unknown", "lut"):
        assert "使用 A12 50mm 的颜色模型" not in out[key]
        assert not any(w in out[key] for w in ("jlaunch", "qlaunch", "l08firm", "prediction"))


# ---- 4. HDR 文件名 ---------------------------------------------------------------------------------


@needs_node
def test_export_dialog_previews_the_real_file_name():
    out = scene("export-name")
    assert out["plain"] == "R8051146_gxr.jpg" and out["hdr"] == "R8051146_gxr_hdr.jpg"
    assert out["neutral"] == "R8051145_gxr_neutral.jpg" and out["neutralHdr"] == "R8051145_gxr_neutral_hdr.jpg"
    assert out["jpgHdr"] == "R8051147_gxr.jpg"  # 只有 JPG 做不了 HDR：还是普通名字
    assert out["textPair"] == "R8051146_gxr.jpg"
    # 图库还没试过这张能不能做成 HDR：写做成时的名字，括号里写做不成时的名字（例如 RAW 读不出来，后端会退回普通名字）
    assert out["textPairHdr"] == "R8051146_gxr_hdr.jpg（做不成 HDR 时是 R8051146_gxr.jpg）"
    assert out["textPairReady"] == "R8051146_gxr_hdr.jpg"  # 图库里已经做成过 HDR
    assert out["textPairUnsupported"] == "R8051146_gxr.jpg"  # 图库已经知道这张做不成
    assert out["textRawNoDataHdr"] == "R8051145_gxr_neutral_hdr.jpg（做不成 HDR 时是 R8051145_gxr_neutral.jpg）"
    assert "原名_gxr_neutral.jpg" in out["textManyNoData"]
    assert "原名_gxr_neutral_hdr.jpg" in out["textManyNoDataHdr"] and "原名_gxr_neutral.jpg" in out["textManyNoDataHdr"]
    assert out["textRawNoData"] == "R8051145_gxr_neutral.jpg"  # 没导入相机配方文件、只有 RAW：中性转换
    assert out["textRawWithData"] == "R8051145_gxr.jpg"
    assert out["textMany"] == "原名_gxr.jpg" and out["textManyHdr"].startswith("原名_gxr_hdr.jpg")
    assert "-2" in out["note"] and "-3" in out["note"]
    assert "R8051146_gxr.jpg" in out["rowBefore"] and "-2" in out["rowBefore"]
    assert "R8051146_gxr_hdr.jpg" in out["rowAfter"]


# ---- 3. 两种「我的配方」 -----------------------------------------------------------------------------


@needs_node
def test_editor_recipe_tab_lists_both_my_recipes_with_their_source():
    out = scene("recipes-tab")
    assert out["redraws"] >= 1
    assert out["secs"].count("我的配方") == 1  # 总名字只出现一次
    assert out["sources"] == ["相机里", "电脑上 · 用 LUT 生成"]
    assert "FCC-001" in out["text"] and "Kodak 风" in out["text"]
    assert out["hasOtherUnit"] is False  # 别的单元的配方不列在这张照片的配方标签里
    assert out["cameraTitle"] == "相机里的配方"
    assert out["mineTitle"] == "电脑上 · 来自 LUT「Portra 400」"
    assert out["pressedBefore"] == "false" and out["pressedAfter"] == "true"
    # 套用 = 换成它的底子和档位
    assert out["applied"]["kind"] == "builtin" and out["applied"]["slot"] == 2
    assert out["applied"]["levels"] == {"amount": 50, "contrast": 1}
    assert out["disabled"] is None and out["puts"] == 1


@needs_node
def test_computer_recipe_is_not_applied_to_a_jpg_only_photo():
    # 只有 JPG：内置配方是近似、不带旋钮。电脑上的配方（底子加档位）套上去，预览会丢掉档位，导出会报 approx_knobs
    out = scene("recipes-tab-approx")
    assert "Kodak 风" in out["text"]
    assert out["disabled"] == "true"
    assert "需要 RAW" in out["mineTitle"]
    assert out["applied"] is None and out["puts"] == 0


def test_recipes_page_says_where_the_recipes_live():
    lut = (ASSETS / "lut.js").read_text(encoding="utf-8")
    assert "我的配方 · 电脑上" in lut and "sourceText(r)" in lut
    assert "相机里自己存的配方不在这一页" in lut
    recipes = (ASSETS / "recipes.js").read_text(encoding="utf-8")
    assert "CAMERA_SOURCE" in recipes and "电脑上 · 用 LUT 生成" in recipes


# ---- 6. 文档和界面用同一个名字 ------------------------------------------------------------------------


def test_docs_use_the_ui_name_for_calibration_data():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "颜色标定（开发用）" not in readme and "导入颜色标定（开发用）" not in readme
    camdata = (ASSETS / "camdata.js").read_text(encoding="utf-8")
    assert "导入校准数据" in camdata and "校准数据文件夹" in camdata
    assert "「校准数据」" in readme and "导入校准数据" in readme
    for doc in (ROOT / "docs").glob("*.md"):
        assert "颜色标定（开发用）" not in doc.read_text(encoding="utf-8"), doc.name


# ---- 7、8. 图库 -----------------------------------------------------------------------------------


def test_gallery_selection_shortcuts_and_no_text_selection():
    onboarding = (ASSETS / "onboarding.js").read_text(encoding="utf-8")
    assert "`${mod}点击`" in onboarding and "加选 / 取消一张" in onboarding
    gallery = (ASSETS / "gallery.js").read_text(encoding="utf-8")
    assert "e.metaKey || e.ctrlKey || state.selection.size" in gallery  # ⌘ / Ctrl 点击切换一张；点击仍然是打开
    css = (ASSETS / "app.css").read_text(encoding="utf-8")
    assert ".library { user-select: none" in css
    assert ".library input, .library textarea, .library select { user-select: text" in css
    assert ".grid.single-day .day > h3" in css and "single-day" in gallery
