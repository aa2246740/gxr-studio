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
# 直方图面板：viewer.js 要的是 { el, attach }，通用替身只给一个空 div。它要 MutationObserver 和定时器，这里的假页面没有也不需要；
# 它自己的行为在 tests/test_histogram_browser.py 里用真浏览器测
HISTOGRAM_STUB = (
    "export function createHistogram() { return { el: document.createElement('div'), attach() {}, toggleClip() { return false; } }; }\n"
    "export function histogramState() { return null; }\n"
    "export function clipTitle() { return ''; }\n"
)


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


def prepare(tmp_path: Path, real: tuple[str, ...] = ()) -> Path:
    """把前端模块拷到临时目录，和保存与路由无关的面板换成空实现；api.js 换成转给 globalThis 替身的薄壳。"""
    folder = tmp_path / "modules"
    folder.mkdir()
    for src in ASSETS.glob("*.js"):
        text = src.read_text(encoding="utf-8")
        stub = src.name in STUBBED and src.name not in real
        if src.name == "histogram.js" and src.name not in real:
            text = HISTOGRAM_STUB
        (folder / src.name).write_text(_stub(text) if stub else text, encoding="utf-8")
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
    return folder


def run(tmp_path: Path, scenario: str, real: tuple[str, ...] = ()) -> dict:
    folder = prepare(tmp_path, real)
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


# ---- PR #30 独立评审（F01–F11）的回归 ---------------------------------------------------


def test_retry_is_one_queued_write_and_newer_edits_win(tmp_path):
    """F01：重试的 GET 还没回来时排进新修改、再 flush：flush 等重试和新修改都写完才放行；新修改最后写，服务器上是新值。"""
    out = run(tmp_path, "retry-overlap")
    assert out["saved"] is False
    assert out["statusWhileRetrying"] == "saving" and out["flushedEarly"] is False
    assert out["retry"] is True and out["newer"] is True
    assert out["putValues"] == [1, 1, 2]  # 失败的一笔、重试补写、新修改
    assert out["trace"] == ["flush@3"]
    assert out["status"] == "saved" and out["server"] == 2


def test_retry_does_not_replay_a_value_a_newer_save_replaced(tmp_path):
    """F01：失败后同一项又改并写成功：失败记录随之清掉，重试什么都不写，不拿旧值盖回去。"""
    out = run(tmp_path, "newer-edit-supersedes-failed")
    assert out["newer"] is True and out["statusAfterNewer"] == "saved"
    assert out["retry"] is True
    assert out["putValues"] == [1, 2]
    assert out["server"] == 2


def test_retry_after_reopening_shows_what_was_written(tmp_path):
    """F03：切走再回来后点底栏「重试」：写进去以后面板显示写进去的值，rev 跟服务器一致，下一笔照常写进去。"""
    out = run(tmp_path, "retry-after-reopen")
    assert out["reopenedExposure"] == 0 and out["statusReopened"] == "failed"
    assert out["status"] == "saved"
    assert out["viewExposure"] == 1
    assert out["viewRev"] == out["serverRev"]
    assert out["nextOk"] is True


def test_reload_does_not_roll_back_a_newer_save(tmp_path):
    """F04：重读编辑记录途中写进去一笔新修改：重读完 rev 和内容都是新的，下一笔不会 409。"""
    out = run(tmp_path, "reload-race")
    assert out["saved"] is True
    assert out["viewRev"] == out["serverRev"] == 2
    assert out["viewExposure"] == 2
    assert out["nextOk"] is True and out["conflicts"] == 0


def test_whole_day_export_waits_for_every_edited_photo_of_that_day(tmp_path):
    """F02：B 的修改没写进去时，从大图换成「这一天的全部」或在图库导出这一天，都不发导出请求；只导出 A 这一张照常。"""
    out = run(tmp_path, "date-export-barrier")
    assert out["dayPostsFromViewer"] == 0 and "有修改没有保存" in out["dayFormText"]
    assert out["singlePosts"] == 1
    assert out["dayPostsFromGallery"] == 0 and "有修改没有保存" in out["galleryFormText"]


def test_reset_locks_its_button_until_both_writes_finish(tmp_path):
    """F09：复原点了马上锁住，连点不会多排写入；写完解锁。"""
    out = run(tmp_path, "reset-double-click")
    assert out["lockedAtOnce"] is True
    assert out["puts"] == ["edit", "crop"]
    assert out["unlocked"] is True


def test_reset_partial_failure_keeps_the_draft_and_retries_only_the_failed_part(tmp_path):
    """F09：复原时编辑没写进去：底栏标成没保存。裁剪是按复原后的几何定的，几何没写成功就不写，留在失败记录里；重试先写编辑、再写裁剪。"""
    out = run(tmp_path, "reset-partial-failure")
    assert out["status"] == "failed" and "有修改没有保存" in out["saveLine"]
    assert out["retry"] is True and out["statusAfter"] == "saved"
    assert out["retryPuts"] == ["edit", "crop"]


def test_before_image_keeps_only_the_latest_response(tmp_path):
    """F08：对比图两次请求乱序回来：缓存留最新几何的那张，晚到的旧结果不装、不建地址、不重画。"""
    out = run(tmp_path, "before-out-of-order")
    assert out["requests"] == 2
    assert '"straighten":5' in out["key"]
    assert out["urlsCreated"] == 1 and out["repaints"] == 1


def test_viewer_finishes_a_partial_photo_list(tmp_path):
    """F07：图库只取了第一页就打开照片：大图把列表取完，胶片条和序号用完整的列表。"""
    out = run(tmp_path, "partial-list")
    assert out["listLen"] == 2 and out["complete"] is True
    assert out["title"] == "1 / 2"
    assert out["prevDisabled"] == "true" and out["nextEnabled"] is True


def test_info_panel_names_looks_and_camera_recipes_by_their_own_name(tmp_path):
    """F11：Looks 和相机里的自定义配方写自己的名字；找不到时写类别，不冒用原厂底子的名字。"""
    out = run(tmp_path, "info-names")
    assert out["look"] == "NIGHT" and out["user"] == "MY BW"
    assert out["missingLook"] == "Look"
    assert "原厂标准" in out["builtin"]


def test_camera_import_finishing_after_navigation_does_not_redraw_the_camera_page(tmp_path):
    """F05：相机配方导入完成时已经回到图库：页面留在图库（相机数据照常更新）；没离开时照常重画相机页。"""
    out = run(tmp_path, "camera-import-after-leave")
    assert out["page"] == "GALLERY" and out["camdataReady"] is True
    assert out["stayedPage"] is True and out["importedShown"] is True


def test_export_dialog_keeps_focus_while_busy(tmp_path):
    """F06：导出途中按钮用 aria-disabled 锁住（不 disabled，焦点不丢）；焦点被丢到 body 时 Tab 带回对话框；对话框开着时背后 inert。"""
    out = run(tmp_path, "modal-focus-guard")
    assert out["appInertOpen"] is True
    assert out["submitDisabled"] is False and out["submitAriaDisabled"] == "true"
    assert out["focusOnSubmit"] is True
    assert out["exportPosts"] == 1
    assert out["focusBackInDialog"] is True and out["tabPrevented"] is True
    assert out["unlocked"] is True
    assert out["appInertClosed"] is False


def test_changing_scope_while_waiting_for_saves_exports_what_was_submitted(tmp_path):
    """N01：等保存的过程中切到「这一天的全部」，这一次导出的仍是点导出时选的那一张；B 没保存，不能发出整天的导出。"""
    out = run(tmp_path, "scope-change-during-barrier")
    assert out["failedB"] == "failed"
    assert out["posts"] == [{"url": "/api/photos/a0/export", "date": None}]


def test_retry_updates_the_panel_from_the_write_without_reading_again(tmp_path):
    """N02：重试写进去以后，面板直接用写入的回应；不另读，读失败也不会留下旧值配新 rev。"""
    out = run(tmp_path, "retry-no-reread")
    assert out["reopenedExposure"] == 0
    assert out["retry"] is True and out["readsAfterRetry"] == 0
    assert out["viewExposure"] == 1 and out["status"] == "saved"
    assert out["next"] is True
    assert out["serverExposure"] == 1 and out["serverContrast"] == 5


def test_retry_does_not_overwrite_a_newer_failed_draft(tmp_path):
    """N02：重试途中又改了曝光且这笔写失败：面板保留新值、标成没保存；之后的调整写进去的是新值。"""
    out = run(tmp_path, "retry-keeps-newer-draft")
    assert out["retry"] is True and out["newer"] is False
    assert out["viewExposure"] == 2 and out["status"] == "failed"
    assert out["next"] is True and out["serverExposure"] == 2
    assert out["statusAfter"] == "saved"


def test_returning_to_a_cached_comparison_drops_the_request_in_flight(tmp_path):
    """N03：改回已经缓存的几何时，还在路上的别的几何的对比图作废，晚到也不换掉缓存。"""
    out = run(tmp_path, "before-cache-return")
    assert out["requests"] == 2
    assert out["returnedCached"] is True and out["keptCached"] is True and out["keyIs0"] is True


def test_single_export_result_reaches_a_toast_even_during_the_close_animation(tmp_path):
    """N04：关掉对话框后 160ms 退场期间回来的结果（成功和失败）也用提示告诉用户。"""
    out = run(tmp_path, "single-close-fast-result")
    assert "已导出 1 张" in out["okToast"]
    assert "导出目录不能写" in out["failToast"]


def test_rebuild_failure_after_closing_settings_is_a_toast_with_retry(tmp_path):
    """N04：关掉设置后重建失败，用带「重试」的提示告诉用户；从提示重试会再发一次。"""
    out = run(tmp_path, "settings-rebuild-closed-failure")
    assert "照片库读不出来" in out["toast"]
    assert out["hasRetry"] is True
    assert out["postsBeforeRelease"] == 2, "连点提示里的重试只能再发一次"
    assert out["rebuildPosts"] == 2
    assert out["doneToasts"] == 1


def test_retry_keeps_the_saved_exposure_when_only_the_curve_changed_meanwhile(tmp_path):
    """V01：重试排着时只改了曲线：曲线以界面为准，补写进去的曝光照样显示，之后的调整不会把它写回 0。"""
    out = run(tmp_path, "retry-other-field")
    assert out["reopenedExposure"] == 0
    assert out["retry"] is True and out["newer"] is True
    assert out["viewExposure"] == 1 and out["viewCurve"] == [[0, 0], [128, 110], [255, 255]]
    assert out["status"] == "saved" and out["next"] is True
    assert out["serverExposure"] == 1 and out["serverCurve"] == {"master": [[0, 0], [128, 110], [255, 255]]}


def test_partial_retry_shows_the_edit_that_was_written(tmp_path):
    """V02：重试时编辑写进去、裁剪又失败：编辑马上显示；第二次重试补上裁剪；之后的调整保留曝光 1。"""
    out = run(tmp_path, "retry-partial-two-kinds")
    assert out["reopenedExposure"] == 0
    assert out["firstRetry"] is False
    assert out["afterPartial"] == {"view": 1, "server": 1, "status": "failed"}
    assert out["secondRetry"] is True and out["after"]["view"] == 1 and out["after"]["status"] == "saved"
    assert out["next"] is True and out["serverAfterNext"] == 1


def test_batch_result_reaches_a_toast_during_the_close_animation(tmp_path):
    """V04：批量导出的结果在对话框退场的 160ms 里回来，也用提示告诉用户。"""
    out = run(tmp_path, "batch-close-fast")
    assert "已导出 2 张" in out["toast"]


def test_closed_export_offers_retry_save_in_the_toast(tmp_path):
    """V05：关掉对话框后保存关卡才查到没写进去的修改：提示里有「重试保存」，点了补写；不导出。"""
    out = run(tmp_path, "closed-save-retry")
    assert "有修改没有保存" in out["toast"]
    assert out["hasRetrySave"] is True
    assert out["statusB"] == "saved" and "修改已保存" in out["toastAfter"]
    assert out["posts"] == 0


def test_retry_landing_mid_gesture_does_not_rebuild_the_panel(tmp_path):
    """W01：重试写进去时正在拖曲线：面板不重建（拖着的控件和松手时的保存还在）；这一步结束后再刷新，曲线和曝光都写进去。"""
    out = run(tmp_path, "retry-during-gesture")
    assert out["tab"] == "adjust" and out["hadPane"] is True
    assert out["retry"] is True
    assert out["sameDuringGesture"] is True and out["exposureDuringGesture"] == 1
    assert out["rebuiltAfter"] is True
    assert out["serverCurve"] == {"master": [[0, 0], [133, 148], [255, 255]]}
    assert out["serverExposure"] == 1 and out["status"] == "saved"


def test_undoing_another_field_after_retry_keeps_the_recovered_value(tmp_path):
    """W02：重试补写的曝光装进来以后撤销曲线：只撤曲线，曝光不回到切回来时读到的旧值；重做放回曲线。"""
    out = run(tmp_path, "retry-undo-cross-field")
    assert out["retry"] is True
    assert out["beforeUndo"] == {"view": 1, "server": 1}
    assert out["afterUndo"]["view"] == 1 and out["afterUndo"]["server"] == 1
    assert out["afterUndo"]["curve"] is None and out["afterUndo"]["viewCurve"] is None
    assert out["afterRedo"] == {"view": 1, "server": 1, "curve": {"master": [[0, 0], [128, 110], [255, 255]]}}


def test_undo_after_retry_still_reverts_your_own_change(tmp_path):
    """W02：曝光是你自己改的（写失败、重试成功），撤销照样回到原来的值。"""
    out = run(tmp_path, "retry-undo-own-history")
    assert out["retry"] is True
    assert out["afterUndo"] == {"view": 0, "server": 0}


def test_slider_press_without_change_ends_the_step_and_the_retry_refreshes_the_panel(tmp_path):
    """X01：在真的调整面板上按下又松开曝光滑块、值没变：这一步结束（不留 pending）；重试写进的曝光 1 显示在滑块上。"""
    out = run(tmp_path, "no-op-slider", real=("adjust.js",))
    assert out["sliderFound"] is True
    assert out["pendingAfterRelease"] is False
    assert out["retry"] is True
    assert out["pending"] is False and out["panesStale"] is False
    assert out["sliderValue"] == "1" and out["modelExposure"] == 1
    assert out["undoLength"] == 0
    assert out["status"] == "saved"


def test_retry_keeps_history_the_user_made_even_when_values_match(tmp_path):
    """X02：曝光在这里改过（2、0、1 失败），重试时又拖到 0 没松手：撤销记录不动；松手后撤销两次依次回到 1、0。"""
    out = run(tmp_path, "retry-owned-history")
    assert out["retry"] is True
    assert out["undoBefore"] == out["undoAfterRetry"] == [0, 2, 0]
    assert out["undos"] == [{"view": 1, "server": 1}, {"view": 0, "server": 0}]


def test_undoing_a_curve_after_an_in_place_refresh_keeps_the_retried_exposure(tmp_path):
    """Y01：曝光改过几次、最后一笔失败，原地刷新后又改曲线；重试补写曝光 1 后撤销曲线：只撤曲线，曝光仍是 1。"""
    out = run(tmp_path, "refresh-mixed-history")
    assert out["afterRefresh"] == 0
    assert out["retry"] is True and out["afterRetry"] == {"view": 1, "server": 1}
    assert out["afterUndo"] == {"view": 1, "server": 1, "curve": None}
    assert out["afterRedo"] == {"view": 1, "server": 1, "curve": {"master": [[0, 0], [133, 148], [255, 255]]}}


def test_undo_after_an_in_place_refresh_still_reverts_your_own_exposure(tmp_path):
    """Y01：曝光 0→1 失败，原地刷新、重试补写 1 后撤销：回到你原来的 0，重做回到 1。"""
    out = run(tmp_path, "refresh-owned-history")
    assert out["retry"] is True and out["afterRetry"] == {"view": 1, "server": 1}
    assert out["afterUndo"]["view"] == 0 and out["afterUndo"]["server"] == 0
    assert out["afterRedo"]["view"] == 1 and out["afterRedo"]["server"] == 1


def test_dragging_back_to_the_start_still_saves_the_release(tmp_path):
    """Y02：重试回来的时候正在拖同一个滑块，最后拖回起点松手（浏览器不发 change）：松手时的值照样保存，只存一次。"""
    out = run(tmp_path, "drag-back-to-start", real=("adjust.js",))
    assert out["retry"] is True and out["serverMid"] == 1
    assert out["final"] == {"view": 0, "server": 0, "pending": False, "status": "saved"}
    assert out["edits"] == 3, "失败的一笔、重试一笔、松手一笔"


def test_ruler_key_held_when_focus_leaves_is_saved_once(tmp_path):
    """Y02：拉直刻度按着方向键时焦点离开：照样保存，这一步结束；再离开一次不会再存。"""
    out = run(tmp_path, "ruler-blur", real=("correct.js",))
    assert out["found"] is True and out["v2"] is True
    assert out["pending"] is False
    assert out["straighten"] == 0.1 and out["geometryWrites"] == 1


def test_undoing_the_frame_keeps_the_failed_stamp_for_retry(tmp_path):
    """Z01：印记那笔失败、原地刷新后改边框又撤销：撤销只写 frame，印记的失败还在，重试补写回来。"""
    out = run(tmp_path, "decoration-undo-other-group")
    assert out["stampAfterRefresh"] is False and out["frameSaved"] == {"style": "white"}
    assert out["undoKeys"] == [["frame"]]
    assert out["statusAfterUndo"] == "failed"
    assert out["retry"] is True
    assert out["final"] == {"frame": None, "stamp": True, "status": "saved"}


def test_undoing_the_frame_during_a_stamp_retry_does_not_overwrite_it(tmp_path):
    """Z01：印记的重试已经发出、还没回来时撤销边框：补写回来的印记不会被撤销冲掉。"""
    out = run(tmp_path, "decoration-undo-during-retry")
    assert out["retry"] is True
    # 重试那笔在撤销之前就发出去了（被压住）；撤销只写 frame，不带 stamp 键
    assert out["undoKeys"] == [["frame"]]
    assert out["final"] == {"frame": None, "stamp": True, "status": "saved"}


def test_legacy_angle_only_undo_and_redo_write_the_angle(tmp_path):
    """Z02：旧接口只拉直了 5° 的一步：撤销写回 0°，重做写回 5°，框不动。"""
    out = run(tmp_path, "legacy-angle-undo", real=("correct.js", "crop.js"))
    assert out["v2"] is False
    assert out["afterAction"] == 5
    assert out["afterUndo"]["angle"] == 0
    assert out["afterUndo"]["bodies"] == [{"angle_deg": 0, "rect": [0.1, 0.2, 0.5, 0.4], "preset": "free"}]
    assert out["afterRedo"]["angle"] == 5
    assert out["afterRedo"]["bodies"] == [{"angle_deg": 5, "rect": [0.1, 0.2, 0.5, 0.4], "preset": "free"}]


def test_arrow_keys_pressed_while_the_next_photo_loads_are_not_dropped(tmp_path):
    """B1：→ 去 B、B 的编辑记录还在读时按 ←：快捷键还在，从 B 算起回到 A，不被吃掉。"""
    out = run(tmp_path, "arrow-burst")
    # hashchange 还没处理（屏幕上还是 A，地址栏已经是 B）：← 从 B 算回 A；撤销、空格不作用在 A 上
    assert out["preRight"] is True and out["preHash"] == "#/photo/b0" and out["preShown"] == "a0"
    assert out["preUndo"] is False and out["preSpace"] is False
    assert out["preLeft"] is True and out["preHashAfterLeft"] == "#/photo/a0"
    assert out["right"] is True and out["hashAfterRight"] == "#/photo/b0"
    assert out["shownWhileLoading"] == "b0" and out["keysWhileLoading"] is True
    assert out["left"] is True and out["hashAfterLeft"] == "#/photo/a0"
    assert out["final"] == {"hash": "#/photo/a0", "shown": "a0"}


def test_toolbar_redo_button(tmp_path):
    """B2：顶栏的重做按钮：没东西可重做时置灰，撤销后可用，点了放回那一步，新的改动后又置灰。"""
    out = run(tmp_path, "toolbar-redo")
    assert out["found"] is True
    assert out["initial"] == {"undo": True, "redo": True}
    assert out["afterEdit"] == {"undo": False, "redo": True}
    assert out["afterUndo"] == {"undo": True, "redo": False, "value": 0, "server": 0}
    assert out["afterRedo"] == {"undo": False, "redo": True, "value": 1, "server": 1}
    assert out["afterNewEdit"] == {"redo": True}


def test_export_dialog_says_close_once_the_export_has_started(tmp_path):
    """导出开始以后，对话框的「取消」改叫「关闭」：关掉对话框不会停下导出，叫「取消」会误导。"""
    out = run(tmp_path, "export-close-label")
    assert out == {"before": "取消", "after": "关闭", "exported": 1}


def _near(rect, x, y, w, h, eps=1e-3):
    return all(abs(rect[k] - v) < eps for k, v in {"x": x, "y": y, "w": w, "h": h}.items())


def test_ratio_click_changes_the_box_before_the_save_returns(tmp_path):
    """裁切里点比例：框马上变（本地算），保存在后台；回应回来换成后端的准框；连点时过时的回应不盖掉新框；
    保存失败框留着、标成未保存，重试补上；撤销只写回裁切。"""
    out = run(tmp_path, "crop-click-local-first", real=("crop.js",))
    third = 1 / 3000
    # 1) 保存还挂着，框已经是 1:1 的居中最大框，面板和画面都重画过
    assert out["right"]["preset"] == "1:1" and out["right"]["orientation"] == "landscape"
    assert _near(out["right"]["rect"], 1 / 6, 0, 2 / 3, 1)
    assert out["redraws"] == {"box": 1, "side": 1}
    assert out["heldBody"] == [{"preset": "1:1", "rect": None, "orientation": "landscape"}]
    assert out["stillLocal"] == out["right"]
    assert _near(out["afterServer"]["rect"], 500 / 3000, 0, 2000 / 3000, 1, third)
    # 2) 连点：16:9 的回应晚到，不能把 4:5 的框换掉；最后换成 4:5 的准框
    assert out["double"]["preset"] == "4:5" and out["double"]["orientation"] == "portrait"
    assert out["afterStale"] == out["double"]
    assert _near(out["afterBoth"]["rect"], 700 / 3000, 0, 1600 / 3000, 1, third)
    # 3) 撤销：只有裁切的写入
    assert out["undo"]["urls"] == ["crop"]
    # 连点两下是两步：撤销先回到 16:9，不是一口气回到 1:1
    assert out["undo"]["body"]["preset"] == "16:9"
    assert out["undo"]["now"]["preset"] == "16:9"
    # 4) 失败：框留着（XPan 的本地框），标成未保存；重试成功后换成后端的框
    assert out["failed"]["status"] == "failed" and out["failed"]["preset"] == "xpan-65-24"
    assert _near(out["failed"]["rect"], 0, 0.2231, 1, 0.5538, 2e-3)
    assert out["retry"] is True
    assert out["retried"]["status"] == "saved"
    assert _near(out["retried"]["rect"], 0, 446 / 2000, 1, 1108 / 2000, third)


def test_zoom_100_is_the_clicked_box_and_dragging_does_not_shrink_it(tmp_path):
    """PR #39 复审（Grok 第二轮）：拉直以后滑块的 100% 原来按有效矩形算，比点比例时的框小（1:1、4° 小 109 像素），
    拖一下就存成小框。现在 100% 就是点击框，60% 以框的中心缩放，拖出界只挪回来、不缩小。"""
    out = run(tmp_path, "crop-zoom-matches-click", real=("crop.js",))
    assert len(out["cases"]) == 6
    for case in out["cases"]:
        assert case["zoom100Off"] < 1e-9, case
        assert case["clickKept"] is True, case
        assert abs(case["zoom60"]["ratio"] - 0.6) < 1e-9 and case["zoom60"]["centerOff"] < 1e-9, case
        assert case["nudgedSizeOff"] < 1e-9, case


def test_one_user_action_is_one_undo_step_before_the_server_answers(tmp_path):
    """2026-11-07：曲线上点一下，要按三次 ⌘Z 点才消失——撤销记录原来要等保存回来才写，保存慢的时候前两次撤掉的是别的步骤。
    现在每个操作在结束的那一刻就记一步（同步部分已经多一条），等保存写完也不会多出第二条。"""
    out = run(tmp_path, "one-action-one-undo", real=("adjust.js", "crop.js", "correct.js"))
    for name, got in out["steps"].items():
        assert got == {"sync": 1, "final": 1, "pending": False}, (name, got)
    assert set(out["steps"]) == {"slider", "slider-double-click-reset", "curve-click", "preset", "rotate", "flip", "reset-corrections"}
    # 保存还挂着时按一次撤销：撤掉的就是刚点的那个点
    assert out["curveClicked"] != out["curveBefore"]
    assert out["curveAfterUndo"] == out["curveBefore"]


def test_rotate_after_auto_with_perspective_ends_saved_and_consistent(tmp_path):
    """2026-11-07：小红书 3:4 → 缩放 → 拉直 → 自动（带透视）→ 向左旋转，服务器 422，底栏「有修改没有保存」，面板成了骨架。
    原因：服务器写透视时把框收进新画面并换了画布尺寸，界面的框和 frame 不跟；旋转又拿落后的框去写。"""
    out = run(tmp_path, "rotate-after-refit", real=("crop.js", "correct.js"))
    # 自动以后：界面的框和画布尺寸就是服务器收好的
    assert out["afterAuto"] == {"rect": out["serverAfterAuto"]["rect"], "frame": out["serverAfterAuto"]["frame"]}
    assert out["serverAfterAuto"]["frame"] == [3300, 2400]
    # 转的时候画布不清空；转完保存成功，界面和服务器一致，没有被拒的写入
    assert out["canvasWhileTurning"] is True
    assert out["status"] in ("saved", "idle")
    assert out["afterRotate"] == {"rect": out["serverAfterRotate"]["rect"], "frame": out["serverAfterRotate"]["frame"]}
    assert out["serverAfterRotate"]["frame"] == [2400, 3300]
    # 写入只有两笔：点比例（让服务器算框），和转完以后那一个框（没有被拒、没有重写）
    assert [p["rect"] is None for p in out["cropPuts"]] == [True, False]
    # 界面上的框万一落后于服务器的画布：转好的框是目标（fit），服务器收成离它最近的合格框，一笔写完，不用重算
    assert out["afterDrift"]["status"] in ("saved", "idle")
    assert len(out["afterDrift"]["puts"]) == 1 and out["afterDrift"]["puts"][0] is not None


def test_auto_shows_busy_and_says_what_it_changed(tmp_path):
    """「自动」要一两秒：按钮锁住、转圈、写「正在分析…」；之后一条提示说清楚改了什么（可以撤销）；没把握时仍是原来的提示。"""
    out = run(tmp_path, "auto-feedback", real=("crop.js", "correct.js"))
    assert out["before"] == {"text": "自动", "busy": None}
    assert out["during"] == {"text": "正在分析…", "busy": "true", "disabled": "true"}
    assert out["postsWhileBusy"] == 1
    assert out["after"] == {"text": "自动", "busy": None}
    assert any("已自动拉直 +4.0°、垂直 +8（可以撤销）" in t for t in out["doneToast"]), out["doneToast"]
    assert out["geometry"] == {"straighten": 4, "vertical": 8}
    assert out["undoSteps"] == 1
    assert any("未检测到明显的水平线" in t for t in out["unsureToast"]) and out["undoStepsAfterUnsure"] == 1
    assert any("没有改动" in t for t in out["sameToast"]) and out["sameWrites"] == 0


@pytest.mark.parametrize("scenario", ["undo-auto-restores-box", "undo-auto-while-reread"])
def test_undoing_auto_brings_back_the_old_box_with_the_slider(tmp_path, scenario):
    """评审 P1：自动（透视）以后服务器把框收小、换了画布。撤销只还原了垂直，框还是收小的。
    几何的一步现在也管框；撤销落在「重读裁剪」的回应还没到的时候，晚到的旧回应也不能把收小的框放回来。"""
    out = run(tmp_path, scenario, real=("crop.js", "correct.js"))
    assert out["start"] == [0.35, 0.2, 0.3, 0.6]
    assert out["fitted"] != out["start"], "服务器应该把框收小了，不然这个场景什么也没测"
    assert out["after"] == {"rect": out["start"], "frame": [3000, 2000], "vertical": 0}
    assert out["server"] == {"rect": out["start"], "frame": [3000, 2000]}
    if scenario == "undo-auto-while-reread":
        assert out["heldReread"] is True, "撤销时重读裁剪的请求应该还在路上"
    # 保存的回应还没到时，界面上的框和画布就已经是撤销以后的样子，「W × H」读数没有错到回应回来
    assert out["undoNow"]["rect"] == out["start"] and out["undoNow"]["frame"] == [3000, 2000]
    assert out["undoNow"]["pixels"] == "900 × 1200"
    # 重做：当场回到收小的框和 3300 × 2400 的画布，写进去的就是收小的框，不留「有修改没有保存」。
    # 撤销落在重读还在路上的时候，重做记下的框原来是重读完成之前的：写进去被服务器拒绝，最后变成服务器编的居中框
    assert out["redoNow"] == {"rect": [0.35, 0.2, 0.3, 0.55], "frame": [3300, 2400], "vertical": 40}
    assert out["redoAfter"] == {"rect": [0.35, 0.2, 0.3, 0.55], "frame": [3300, 2400]}
    assert out["redoPuts"] == [[0.35, 0.2, 0.3, 0.55]]
    assert out["redoStatus"] in ("saved", "idle")


def test_rotate_retries_with_the_turned_box_after_a_failed_crop_write(tmp_path):
    """评审 P1：转了以后写框失败（500 / 断网）。照常提示，不重算、不换成居中的最大框，重试用的是转好的框。"""
    out = run(tmp_path, "rotate-save-fails", real=("crop.js", "correct.js"))
    a = out["a"]
    assert len(a["puts"]) == 1 and a["puts"][0] is not None, "500 以后不该再发 rect: null"
    assert a["status"] == "failed" and a["toast"] is True
    assert a["retried"] and a["retried"][0] is not None
    assert [round(x, 4) for x in a["retried"][0]] == a["localRect"]
    assert a["after"] in ("saved", "idle")


def test_a_ratio_click_during_a_rotate_is_not_overwritten_by_the_turned_box(tmp_path):
    """评审 P2：转的几何还没写完时点了比例，比例的写入排在前面；转的那一笔不能再把落后的本地框排在后面盖掉它。"""
    out = run(tmp_path, "rotate-then-ratio-click", real=("crop.js", "correct.js"))
    # 队里的顺序就是操作的顺序：转的几何、转好的框、点比例的框（让服务器算）；最后一笔是点比例的，服务器和界面都以它为准
    assert [p[:2] for p in out["puts"]] == [["edit"], ["crop", "4:3"], ["crop", "1:1"]] and out["puts"][2][2] is None
    assert out["server"]["preset"] == "1:1" and out["client"]["preset"] == "1:1"
    assert [round(x, 4) for x in out["client"]["rect"]] == out["server"]["rect"]


def test_undoing_a_turn_restores_the_frame_and_canvas_at_once(tmp_path):
    """评审 P2：转 90° 时 frame 和预览画布的宽高对调了，撤销快照里没有它们，撤销后读数还是转过的尺寸。"""
    out = run(tmp_path, "undo-rotate-restores-frame", real=("crop.js", "correct.js"))
    assert out["turned"]["frame"] == [2000, 3000] and out["turned"]["full"] == {"w": 2000, "h": 3000}
    assert out["undone"]["frame"] == [3000, 2000]
    assert out["undone"]["full"] == {"w": 3000, "h": 2000} and out["undone"]["canvas"] == {"w": 3000, "h": 2000}
    assert out["undone"]["rotate90"] == 0


@pytest.mark.parametrize(
    ("scenario", "geometry", "final_rect"),
    [
        ("double-turn-held", {"rotate90": 2, "flip": False}, [0.6, 0.3, 0.3, 0.6]),
        ("rotate-then-flip-held", {"rotate90": 1, "flip": True}, [0.3, 0.6, 0.6, 0.3]),
    ],
)
def test_two_turns_while_the_geometry_write_is_pending_store_the_last_box(tmp_path, scenario, geometry, final_rect):
    """评审 P1：几何的写入还挂着时又转了一次（或翻转）。每次转的几何和框在点下去的时候一起排队：
    几何 1、框 1、几何 2、框 2。框写的时候画布还是它定下来时的样子，不会落在后一笔几何后面被拒、被服务器编成居中框。"""
    out = run(tmp_path, scenario, real=("crop.js", "correct.js"))
    assert out["screenBeforeRelease"] == final_rect
    assert out["geometry"] == geometry
    assert out["server"]["rect"] == final_rect
    assert out["client"]["rect"] == final_rect
    kinds = [p[0] for p in out["puts"]]
    assert kinds == ["edit", "crop", "edit", "crop"], out["puts"]
    assert out["puts"][3][1] == final_rect and all(p[1] is not None for p in out["puts"] if p[0] == "crop")
    assert out["status"] in ("saved", "idle") and out["toasts"] == []


@pytest.mark.parametrize("scenario", ["failed-turn-retry", "lone-failed-turn"])
def test_retry_after_a_failed_turn_writes_the_turned_box_too(tmp_path, scenario):
    """评审 P1：转的几何没写进去（500）。后面的框是按那次几何定的，几何没写成功就先不写，留在失败记录里；
    重试先写几何、再写框，服务器和界面最后一致（原来重试只补几何，服务器上还是转之前的框，导出用的是服务器的）。"""
    out = run(tmp_path, scenario, real=("crop.js", "correct.js"))
    fail = out["afterFail"]
    twice = scenario == "failed-turn-retry"
    turned = [0.6, 0.3, 0.3, 0.6] if twice else [0.1, 0.6, 0.6, 0.3]
    assert fail["status"] == "failed" and fail["screen"] == turned
    # 连转两次：第一次的几何和框都写进去了，第二次的几何失败、它的框没写；只转一次：几何失败、框没写
    assert fail["cropPuts"] == (1 if twice else 0)
    assert fail["serverRect"] == ([0.1, 0.6, 0.6, 0.3] if twice else [0.1, 0.1, 0.3, 0.6])
    retry = out["afterRetry"]
    rotate = 2 if twice else 1
    assert out["retry"] is True and retry["status"] in ("saved", "idle")
    assert retry["serverRotate"] == rotate
    assert retry["puts"] == [["edit", rotate], ["crop", turned]], "先写几何，再写框"
    assert retry["serverRect"] == turned and retry["screen"] == turned


def test_a_second_undo_during_the_reread_still_redoes_the_fitted_box(tmp_path):
    """评审 P2：撤销落在重读还没回来的时候，又撤销了一步（把「自动」那一步埋到下面）。
    重读回来以后，重做「自动」写的是服务器收小的框，不是重读开始之前的框（原来写旧框被拒，最后变成居中框）。"""
    out = run(tmp_path, "undo-twice-while-reread", real=("crop.js", "correct.js"))
    assert out["heldReread"] is True
    assert out["crops"][-1] == [0.35, 0.2, 0.3, 0.55], out["crops"]
    assert None not in out["crops"]
    assert out["screen"] == {"rect": [0.35, 0.2, 0.3, 0.55], "frame": [3300, 2400], "vertical": 40}
    assert out["server"] == {"rect": [0.35, 0.2, 0.3, 0.55], "frame": [3300, 2400]}
    assert out["status"] in ("saved", "idle")


@pytest.mark.parametrize("scenario", ["failed-turn-then-perspective", "failed-turn-then-perspective-queued"])
def test_retry_after_a_failed_turn_and_a_later_perspective_keeps_the_box(tmp_path, scenario):
    """评审 P1（第四轮）：转的几何没写进去，之后拖垂直写成功了（带着旋转），服务器已经按新几何收过框。
    重试（或垂直的写入还排着时点重试）不能拿过时的框去被拒、被服务器编成居中框：转好的框是目标（fit），位置保留。"""
    out = run(tmp_path, scenario, real=("crop.js", "correct.js"))
    assert out["afterTurn"]["screen"] == [0.1, 0.6, 0.6, 0.3] and out["afterTurn"]["status"] == "failed"
    after = out["after"]
    assert out["retry"] is True and after["status"] in ("saved", "idle")
    assert after["geometry"] == {"rotate90": 1, "vertical": 40}
    assert after["screen"] == after["server"] == [0.1, 0.6, 0.6, 0.3], "转好的框的位置保留，不是居中的最大框"
    assert after["frame"] == after["serverFrame"]
    crop_puts = [p for p in after["puts"] if p[0] == "crop"]
    assert crop_puts and all(p[1] is not None and p[2] is True for p in crop_puts), after["puts"]


def test_retrying_a_rejected_drag_keeps_the_users_box(tmp_path):
    """评审 P1（第五轮）：拖的框被服务器拒了（形状不对），重试时不能悄悄换成 fit 收小的框：
    还是原来的写入、还是被拒，提示出来，界面上的框原样留着；服务器认了（条件变了）才写进去。"""
    out = run(tmp_path, "retry-rejected-drag", real=("crop.js", "correct.js"))
    mine = [0.2, 0.15, 0.3, 0.6]
    assert out["first"]["puts"] == [["crop", mine, False]] and out["first"]["status"] == "failed" and out["first"]["screen"] == mine
    assert out["retry"] is False
    assert out["afterRetry"]["puts"] == [["crop", mine, False]], "重试发的还是原来的写入，没有 fit"
    assert out["afterRetry"]["status"] == "failed" and out["afterRetry"]["screen"] == mine


def test_retry_fits_a_box_only_right_after_writing_the_geometry_it_was_placed_against(tmp_path):
    """评审 P1（第五轮）的另一面：拖的框没写进去（普通的写入），接着垂直的几何也没写进去。重试先写几何；
    几何刚写进去，这个框是按旧几何摆的，当目标（fit）写：服务器收得下就原样收，收不下才收成最近的合格框。"""
    out = run(tmp_path, "retry-geometry-then-plain-box", real=("crop.js", "correct.js"))
    assert out["skipped"]["status"] == "failed"
    after = out["afterRetry"]
    assert out["retry"] is True and after["status"] in ("saved", "idle")
    mine = [0.2, 0.15, 0.3, 0.6]
    assert after["puts"] == [["edit"], ["crop", mine, True]], after["puts"]  # 几何刚写进去：框直接当目标写，一笔写完
    assert after["server"] == mine, "位置保留"


def test_a_failed_box_is_retried_as_a_target_after_a_later_geometry_write_succeeds(tmp_path):
    """模糊测试 1405 / 1532：框的写入失败（500），之后几何写成功了，服务器按新几何收过框，界面因为有没保存的框没换成服务器的。
    重试拿着按旧几何摆的框去写，永远被拒（crop_outside_image）。现在几何一写成功，没写进去的框就改成目标（fit），重试一笔写完。"""
    out = run(tmp_path, "failed-box-then-geometry", real=("crop.js", "correct.js"))
    mine = [0.2, 0.15, 0.3, 0.6]
    assert out["failedBox"] == {"status": "failed", "screen": mine}
    assert out["afterGeometry"]["status"] == "failed" and out["afterGeometry"]["screen"] == mine
    after = out["afterRetry"]
    assert out["retry"] is True and after["status"] in ("saved", "idle")
    assert after["puts"] == [["crop", mine, True]], after["puts"]
    assert after["server"] == mine and after["screen"] == mine


def test_a_failed_reread_after_a_geometry_write_is_not_saved_and_retry_reads_again(tmp_path):
    """评审 P2（第七轮）：几何写进去了，读回服务器收过的框失败。先隔一会儿再读（两次退避），还不行就记成失败：
    状态是「有修改没有保存」而不是「已保存」，导出被拒，提示里有「重试」；重试就是再读一次，读到了才算完，界面的框换成服务器的。"""
    out = run(tmp_path, "reread-fails-then-retry", real=("crop.js", "correct.js"))
    stuck = out["stuck"]
    assert stuck["gets"] >= 4, "第一次读加两次退避重读，一共三次失败，之后不再读"  # 进页面时的那一次也算
    assert stuck["status"] == "failed" and stuck["stale"] is True
    assert stuck["retryButton"] is True and out["exportBlocked"] is True
    assert stuck["screen"] != stuck["server"], "这个场景里界面确实没跟上服务器"
    after = out["afterRetry"]
    assert out["retry"] is True and after["status"] in ("saved", "idle") and after["stale"] is False
    assert after["gets"] >= 1 and after["puts"] == []
    assert after["screen"] == after["server"] and after["frame"] == [3300, 2400]


def test_a_box_write_carries_fit_only_while_the_server_box_has_not_been_read_back(tmp_path):
    """评审 P1（第七轮）：fit 只在刚写过几何、服务器收过的框还没读回来（boxStale）的时候带；普通的拖动、缩放不带，让服务器照常校验。"""
    out = run(tmp_path, "cropbody-fit-flag", real=("crop.js", "correct.js"))
    assert out["plain"] is True and out["stale"] is True and out["nullRect"] is True
    assert out["afterGeometry"] == {"boxStale": False, "fit": True}


@pytest.mark.parametrize("code", ["ratio_mismatch", "crop_outside_image", "too_small", "bad_rect"])
def test_a_box_the_server_called_wrong_stays_rejected_after_a_later_geometry_write(tmp_path, code):
    """评审第八轮 P1：框被服务器说「本身不对」才没写进去，之后几何写成功了。不能把它改成 fit：重试仍是原来的普通写入、
    仍然被拒，提示出来，界面上的框原样留着；记录还在时的下一次拖动也不带 fit（loadCrop 不再让这个记录把 boxStale 一直开着）。"""
    out = run(tmp_path, f"shape-rejected-then-geometry:{code}", real=("crop.js", "correct.js"))
    mine = [0.2, 0.15, 0.3, 0.6]
    assert out["rejected"] == {"status": "failed", "screen": mine, "shape": True}
    assert out["afterGeometry"] == {"status": "failed", "screen": mine, "boxStale": False, "failedFit": False}
    after = out["afterRetry"]
    assert out["retry"] is False and after["status"] == "failed" and after["screen"] == mine
    assert after["puts"] == [["crop", mine, False]], "重试发的还是原来的普通写入"
    assert out["laterDrag"]["puts"] == [["crop", [0.25, 0.2, 0.3, 0.6], False]], "之后的拖动不带 fit"


def test_a_later_successful_reread_clears_the_failed_reread(tmp_path):
    """评审第八轮 P2：读不回来记成失败；之后又改了一次，这次读到了，界面的框就是服务器的：状态回到已保存，导出不再被拒。"""
    out = run(tmp_path, "stale-cleared-by-later-sync", real=("crop.js", "correct.js"))
    assert out["stuck"] == {"status": "failed", "stale": True}
    later = out["later"]
    assert later["status"] in ("saved", "idle") and later["record"] is None
    assert later["screen"] == later["server"]
    assert later["exportBlocked"] is False and later["flushed"] is False
