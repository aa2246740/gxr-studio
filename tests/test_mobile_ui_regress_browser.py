"""复审 r1 的回归：每个 case 一个新的无头 Chrome，开发服务器整个模块共用一个。

故障只在页面里注入（延迟或改写 Worker 回复、藏起 navigator.gpu），真实样本照常解码。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import REAL_HOME
from test_mobile_ui_browser import MODULES, PKG, RUNTIME, SAMPLES, _free_port, _need, _pairs, _stop

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "support" / "mobile_ui_regress.mjs"


def _prerequisites(files: list[Path]) -> None:
    _need(len(files) >= 2, f"need a DNG+JPG pair in {SAMPLES}")
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    files = _pairs()
    _prerequisites(files)
    tmp = tmp_path_factory.mktemp("mobile-ui-regress")
    dngs = [str(path) for path in files if path.suffix.upper() == ".DNG"]
    jpgs = [str(path) for path in files if path.suffix.upper() == ".JPG"]
    manifest = tmp / "files.json"
    listing = {"pairs": [str(path) for path in files], "dngs": dngs, "jpgs": jpgs, "jpg": jpgs[0], "dng": dngs[0]}
    manifest.write_text(json.dumps(listing), encoding="utf-8")
    port = _free_port()
    env = os.environ.copy()
    env["GXR_MOBILE_SAMPLES"] = str(SAMPLES)
    log = tmp / "vite.log"
    vite: subprocess.Popen[str] | None = None
    try:
        with log.open("w", encoding="utf-8") as stream:
            vite = subprocess.Popen(
                ["node", str(MODULES / "vite" / "bin" / "vite.js"), "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
                cwd=REPO / "mobile",
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if vite.poll() is not None:
                pytest.fail(f"vite exited {vite.returncode}\n{log.read_text(encoding='utf-8', errors='replace')[-2000:]}")
            probe = subprocess.run(
                [
                    "node",
                    "-e",
                    "fetch(process.argv[1]).then((r)=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))",
                    f"http://127.0.0.1:{port}/",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if probe.returncode == 0:
                break
            time.sleep(0.4)
        else:
            pytest.fail("vite did not answer")
        yield {"base": f"http://127.0.0.1:{port}", "manifest": manifest, "log": log}
    finally:
        _stop(vite)


def run_case(server: dict[str, Any], name: str) -> dict[str, Any]:
    result = subprocess.run(
        ["node", str(DRIVER), str(RUNTIME), server["base"], str(server["manifest"]), name],
        cwd=REPO,
        env={**os.environ, "HOME": str(REAL_HOME)},
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    out: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["ok"], out.get("error")
    data: dict[str, Any] = out["result"]
    assert data.get("errors", []) == [], data["errors"]
    return data


def test_equal_capture_times_share_one_order(server: dict[str, Any]) -> None:
    data = run_case(server, "equal-time-order")
    for order, expected in (("newest", ["r9700003", "r9700002", "r9700001"]), ("oldest", ["r9700001", "r9700002", "r9700003"])):
        run = data["runs"][order]
        assert run["ids"] == expected, order
        titles = [step["title"].lower() for step in run["seen"]]
        assert titles == expected, (order, run["seen"])
        assert [step["index"].split(" · ")[0] for step in run["seen"]] == ["1 / 3", "2 / 3", "3 / 3"], (order, run["seen"])


def test_bad_dngs_reach_unreadable_without_a_gpu(server: dict[str, Any]) -> None:
    data = run_case(server, "no-gpu-bad-dng")
    assert data["gpu"] == "0"
    # Worker 的结构检查直接标坏：坏头、截断的 IFD。
    assert "err" in data["classes"]["r9990002"].split()
    assert "err" in data["classes"]["r9990003"].split()
    # 结构对、但没有原始数据：网格里先当「只有 RAW」，点开后 WASM 描述失败，同样是 06b。
    assert "err" not in data["classes"]["r9990004"].split()
    for name in ("r9990002", "r9990003", "r9990004"):
        opened = data["opened"][name]
        assert "读不了" in opened["unreadable"], (name, opened)
        assert "从这次去掉" in opened["unreadable"], (name, opened)
    real = data["opened"]["real"]
    assert real["screen"] == "editor" and real["unreadable"] == "", real
    assert "这个浏览器现在用不了显卡预览" in real["label"], real
    assert len(data["left"]) == 1


def test_full_dng_reads_happen_in_the_worker(server: dict[str, Any]) -> None:
    data = run_case(server, "full-read-in-worker")
    assert data["gpu"] == "1"
    assert data["first"]["title"] != data["second"]["title"]
    # 打开一张、预读下一张、滑过去：主线程一次 File.arrayBuffer 都没有。
    assert data["mainReads"] == [], data["mainReads"]


def test_late_old_results_do_not_overwrite_a_repicked_photo(server: dict[str, Any]) -> None:
    data = run_case(server, "repick-stale")
    for label, row in data["results"].items():
        fresh, after = row["fresh"], row["after"]
        assert row["released"] == 1, label
        assert fresh["canvas"] and "err" not in fresh["cls"].split(), (label, fresh)
        # 旧回复放出来以后：格子、拍摄时间、副标题都还是新文件的，没有「读不了」也没有「拔卡」。
        assert after == fresh, (label, fresh, after)
        assert after["unreadable"] == 0 and after["card"] == 0, (label, after)


def test_card_removed_keeps_cached_photos_and_counts_unread(server: dict[str, Any]) -> None:
    data = run_case(server, "card-removed")
    # 一张已在显卡里，一张读不了：读进来 1 张，没读完 1 张。
    assert "已经读进来的 1 张" in data["dialog"], data["dialog"]
    assert "还有 1 张没读完" in data["dialog"], data["dialog"]
    assert data["subtitle"] == "存储卡已拔出 · 1 张没读完"
    assert data["subtitleAfterRefresh"] == "存储卡已拔出 · 1 张没读完"
    assert "gone" in data["bClass"].split()
    assert data["againDialog"] == 1
    # 缓存的那张：进了编辑器、滑块可用、曝光改了，没有再读卡，显卡里也没多出一份源。
    assert data["reopened"]["screen"] == "editor" and not data["reopened"]["disabled"], data["reopened"]
    assert data["reopened"]["exposure"] not in ("0.0", ""), data["reopened"]
    assert data["aName"] not in data["readsAfterCard"], data["readsAfterCard"]
    assert data["after"]["sources"] == data["before"]["sources"], (data["before"], data["after"])
    assert data["after"]["sourceBytes"] == data["before"]["sourceBytes"], (data["before"], data["after"])
    # 重新选：状态清掉，那张能开。
    assert "存储卡已拔出" not in data["subtitleRepicked"], data["subtitleRepicked"]
    assert "gone" not in data["bClassRepicked"].split()
    assert data["repickedTitle"].lower() == data["b"]


def test_recipe_cards_show_the_real_source(server: dict[str, Any]) -> None:
    data = run_case(server, "recipe-cards")
    gpu = data["gpuRun"]
    assert set(gpu["labels"]) == {"中性转换"}
    # 卡片就是这张 RAW 的中性小图（同一份显卡输出，只差缩放）。
    assert gpu["diff"] <= 2.0, gpu["diff"]
    # 第一张一次、第二张一次，滑回第一张不再请显卡出图。
    assert gpu["callsAfterFirst"] == 1, gpu
    assert gpu["calls"] == 2, gpu
    fallback = data["fallback"]["out"]
    dng_only = data["fallback"]["dngOnly"]
    for photo_id, row in fallback.items():
        want = "内嵌预览" if photo_id == dng_only else "相机 JPG"
        assert set(row["labels"]) == {want}, (photo_id, row)
        assert "中性转换" not in row["labels"]
        assert row["drawn"] == len(row["labels"]), (photo_id, row)


def test_histogram_updates_on_content_and_rests_when_idle(server: dict[str, Any]) -> None:
    data = run_case(server, "histogram")
    # 第一帧就画了，不用先拖滑块。
    assert data["first"]["version"] is not None and data["first"]["painted"] > 1000, data["first"]
    # 静止 2 秒：一次显卡读回都没有。
    assert data["idleCalls"] == 0, data
    # 拖到最亮：重画了、高光三角亮了；松手以后又停下来。
    assert data["dragged"]["version"] != data["first"]["version"], data
    assert data["dragged"]["painted"] > 1000 and data["dragged"]["high"] is True, data["dragged"]
    assert data["dragCalls"] >= 1, data
    assert data["settledCalls"] == 0, data
    assert data["exposure"].startswith("+"), data["exposure"]
    # 换到下一张：新的编辑器里也画出来了。
    assert data["next"]["version"] is not None and data["next"]["painted"] > 1000, data["next"]


def test_later_tabs_keep_the_design_layout_disabled(server: dict[str, Any]) -> None:
    data = run_case(server, "later-tabs")
    want = {
        "裁剪": ["原始", "XPan 65:24", "小红书 3:4", "横向", "竖向", "参考线：三等分", "拉直", "0.0°"],
        "边框": [
            "无",
            "徕卡水印",
            "模糊背景",
            "白边",
            "哈苏水印",
            "拍立得",
            "胶片",
            "极简细线",
            "颜色",
            "细",
            "中",
            "粗",
            "标志",
            "参数",
            "日期",
            "时间戳",
        ],
        "LUT": ["不用 LUT", "胶片暖调", "青橙电影", "黑金", "输入色彩空间", "导入 LUT（.cube 文件）", "不写入相机"],
        "曲线": ["主曲线", "红", "绿", "蓝", "重置此通道"],
    }
    for tab, words in want.items():
        panel = data["tabs"][tab]
        assert panel is not None, tab
        assert "这项下一步做" in panel["text"], tab
        for word in words:
            assert word in panel["text"], (tab, word)
        # 有真控件，而且一个都不能点。
        assert panel["controls"] >= 4 and panel["enabled"] == 0, (tab, panel)
    assert data["tabs"]["曲线"]["svg"] == 1
    assert data["tabs"]["裁剪"]["inputs"] == 1
    assert data["tabs"]["边框"]["inputs"] == 1


def test_old_gpu_upload_cannot_replace_a_newer_version(server: dict[str, Any]) -> None:
    data = run_case(server, "upload-race")
    assert data["released"] == 1, data
    after = data["after"]
    # 两次上传都做完了；装着的是第二次（新版本）的纹理，正在显示的也是它，只有一份源。
    assert after["uploads"] == 2, after
    assert after["installedFrom"] == 1, after
    assert after["currentMatches"] is True, after
    assert after["sources"] == 1, after


def test_card_removed_counts_only_cached_sources_as_usable(server: dict[str, Any]) -> None:
    data = run_case(server, "card-removed-three")
    assert len(data["ids"]) == 3
    # 一张在显卡里，两张只有缩略图：能接着用 1 张，没读完 2 张。
    assert "已经读进来的 1 张" in data["dialog"], data["dialog"]
    assert "还有 2 张没读完" in data["dialog"], data["dialog"]
    assert data["subtitle"] == "存储卡已拔出 · 2 张没读完"
    assert "gone" not in data["classes"]["a"].split()
    assert "gone" in data["classes"]["b"].split() and "gone" in data["classes"]["c"].split(), data["classes"]
    # 只有缩略图的那张点开：不进编辑器，弹同样的提示和数字。
    assert data["screenAfterC"] == "grid"
    assert "已经读进来的 1 张" in data["againDialog"] and "还有 2 张没读完" in data["againDialog"]
    assert data["reopened"] == "editor"


def test_late_neutral_thumbnail_after_clear_is_dropped(server: dict[str, Any]) -> None:
    data = run_case(server, "neutral-after-clear")
    assert data["screen"] == "start"
    # 清空之后才回来的中性小图：没有留下一个没 close 的位图。
    assert data["after"]["open"] == 0, data["after"]
