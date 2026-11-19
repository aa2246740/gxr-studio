"""UI-M3-GROUP：复制调整、套用到这一组、选照片…、撤销，在无头 Chrome 里用真照片走一遍。

照片只经 GXR_STUDIO_TEST_PARENT 找（support.samples）。为了让分组间隔 3 → 1 分钟时组真的变，另外在 tmp_path 里做几张副本，
只把拍摄时间改掉（同一张照片、同一个镜头）：一张只有 JPG 的（来源后 50 秒）、一对来源后 150 秒、一对来源后 30 分钟。
断言只看界面上的字、组标题的张数和编辑状态（配方 id、调整、rev），不打印照片数据和拍摄时间。
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from support import samples
from support.export_pipeline_cases import check, same_redacted
from test_mobile_ui_browser import DATS, MODULES, PKG, RUNTIME, _drive, _need

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
SAMPLES = samples.a12_50_samples()
DRIVER = REPO / "tests" / "support" / "mobile_ui_group_browser.mjs"
DATE = re.compile(rb"\d{4}:\d\d:\d\d \d\d:\d\d:\d\d")


def _ms(path: Path) -> int:
    """手机的拍摄时间：桌面 taken（到秒）加 SubSecTimeOriginal（test_mobile_ui_group_unit 核对过两边同源）。"""
    from gxr_studio.meta import read_jpeg_meta
    from test_mobile_ui_group_unit import _subsec_ms, _taken_ms

    taken = read_jpeg_meta(path).taken
    check(taken is not None, "sample has a capture time")
    assert taken is not None
    return _taken_ms(taken) + _subsec_ms(path)


def _shifted(src: Path, dst: Path, seconds: int) -> None:
    """同一张照片换一个拍摄时间：把文件里和 DateTimeOriginal 相同的日期字串都换掉（等长），别的字节不动。"""
    from gxr_studio.meta import read_jpeg_meta

    taken = read_jpeg_meta(src.with_suffix(".JPG")).taken
    assert taken is not None
    old = taken.strftime("%Y:%m:%d %H:%M:%S").encode()
    new = (taken + timedelta(seconds=seconds)).strftime("%Y:%m:%d %H:%M:%S").encode()
    data = src.read_bytes()
    check(old in data and len(DATE.findall(new)) == 1, "date string found")
    dst.write_bytes(data.replace(old, new))


def _groups(times: dict[str, int], gap_minutes: int) -> list[list[str]]:
    """lib/group.ts 的 groupByGap（从早到晚）：相差严格大于间隔，或跨了本地日期，就新开一组；同一时间按 id。"""
    out: list[list[str]] = []
    last: int | None = None
    for stem, ms in sorted(times.items(), key=lambda item: (item[1], item[0].lower())):
        day = datetime.fromtimestamp(ms / 1000).date()
        if last is None or ms - last > gap_minutes * 60_000 or day != datetime.fromtimestamp(last / 1000).date():
            out.append([])
        out[-1].append(stem)
        last = ms
    return out


def test_mobile_ui_group_copy_apply_pick_and_undo(tmp_path: Path) -> None:
    pairs = sorted(dng for dng in SAMPLES.glob("*.DNG") if dng.with_suffix(".JPG").is_file()) if SAMPLES.is_dir() else []
    dat = next((path for path in DATS if path.is_file()), None)
    samples.need_sample(len(pairs) >= 2 and dat is not None, f"need DNG+JPG pairs and a GXRCE.DAT under {samples.sample_parent()}")
    if len(pairs) < 2 or dat is None:
        pytest.skip("need DNG+JPG pairs and a local GXRCE.DAT fixture (GXR_STUDIO_TEST_PARENT)")
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")

    times = {dng.stem: _ms(dng.with_suffix(".JPG")) for dng in pairs}
    source = max(times, key=lambda stem: (times[stem], stem))
    base = next(dng for dng in pairs if dng.stem == source)
    files = [str(path) for dng in pairs for path in (dng, dng.with_suffix(".JPG"))]
    jpg_only = tmp_path / "R9990002.JPG"
    _shifted(base.with_suffix(".JPG"), jpg_only, 50)
    files.append(str(jpg_only))
    times[jpg_only.stem] = times[source] + 50_000
    for stem, seconds in (("R9990003", 150), ("R9990004", 1800)):
        for ext in (".DNG", ".JPG"):
            _shifted(base.with_suffix(ext), tmp_path / f"{stem}{ext}", seconds)
            files.append(str(tmp_path / f"{stem}{ext}"))
        times[stem] = times[source] + seconds * 1000
    expect = {str(gap): _groups(times, gap) for gap in (3, 1)}
    group3 = next(group for group in expect["3"] if source in group)
    group1 = next(group for group in expect["1"] if source in group)
    check(jpg_only.stem in group3 and "R9990003" in group3 and "R9990003" not in group1, "copies land as planned")
    check(len(expect["1"]) > len(expect["3"]), "gap 1 splits more groups")
    picks = [stem for stem in sorted(times) if stem not in group3][:2]
    check(len(picks) == 2, "two photos outside the source's group")

    manifest = tmp_path / "group.json"
    manifest.write_text(
        json.dumps(
            {
                "files": files,
                "photos": len(times),
                "dat": str(dat),
                "source": source,
                "sameGroupTarget": "R9990003",
                "sourceGroupFirst1": group1[0].lower(),
                "picks": picks,
                "expect": expect,
            }
        ),
        encoding="utf-8",
    )
    result = _drive(DRIVER, manifest, tmp_path, "", timeout=600)
    (tmp_path / "driver-stdout.json").write_text(result.stdout, encoding="utf-8")
    assert result.returncode == 0, result.stderr[-3000:] + result.stdout[-3000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["pageErrors"] == []
    steps: dict[str, Any] = report["steps"]

    loaded = steps["loaded"]
    assert loaded["photos"] == len(times) and set(loaded["revs"]) == {0}
    assert [h["cnt"] for h in loaded["heads"]] == [f"{len(g)} 张" for g in expect["3"] if len(g) > 1]
    assert {h.get("go") for h in loaded["heads"]} == {None}

    assert steps["hint"]["goButtons"] == 0

    src = steps["source"]
    assert src["on"] == "builtin:2" and src["after"]["choice"]["id"] == "builtin:2"
    assert src["after"]["knobs"]["exposure_ev"] == 0.7 and src["after"]["knobs"]["contrast"] == 12
    # 选配方、两次滑块松手：三次落定的修改。
    assert src["after"]["rev"] == src["beforeRev"] + 3
    # 桌面 memory：第一次选配方时把默认的原厂 04（档位为空）记进去；当前配方的档位不在 memory 里。
    assert src["after"]["memory"] == {"builtin:4": {}} and src["after"]["levels"] == {"builtin:2": {}}

    group = steps["group"]
    assert group["button"] == "复制 2 项"
    lead = "套用时只改勾选的项目，别的保持不变。勾选会记住，下次还是这几项。"
    for line in ("复制调整", "配方 / 风格", "配方、LUT", "调整", "几何", "裁剪", "边框", "时间戳", "（这张没有设置）", lead):
        assert line in group["sheet"], line
    targets = len(group3) - 1
    where = group["where"]
    assert "已复制：配方 / 风格、调整" in where
    assert f"来自 {source}：" in where and "曝光 +0.7，对比度 +12。要套用到哪些照片？" in where
    assert f"这张所在的一组 · {len(group3)} 张" in where and f"套用到这一组（{targets} 张）" in where
    assert "按拍摄时间自动分组：前后隔不到 3 分钟的算一组" in where and "选照片…" in where and "先不套用" in where
    assert group["confirm"]["title"] == f"套用到这一组的 {targets} 张？"
    assert f"除了这张以外的 {targets} 张会换成 {source} 的配方 / 风格和调整。别的设置不变。" in group["confirm"]["body"]
    assert "套用后可以撤销。存储卡上的照片不会变。" in group["confirm"]["body"]
    # 结果句是桌面的 pasteMessage：只有 JPG 的那张只套用了调整。
    assert group["applied"]["line"] == f"已套用到 {targets} 张；1 张只有 JPG，没有套用配方"
    assert group["applied"]["sub"].endswith(" · 配方 / 风格、调整")
    after = {row["stem"]: row for row in group["edits"]}
    for stem in group3:
        if stem == source:
            continue
        row = after[stem]
        same_redacted(row["knobs"], src["after"]["knobs"], "target knobs == source knobs")
        assert row["rev"] == 1, stem
        if stem == jpg_only.stem:
            assert row["choice"] is None
        else:
            assert row["choice"] == src["after"]["choice"], stem
            same_redacted(row["levels"], src["after"]["levels"], "target levels == source levels")
            same_redacted(row["memory"], src["after"]["memory"], "target memory == source memory")
    for stem in times:
        if stem not in group3:
            assert after[stem]["rev"] == 0 and after[stem]["choice"] is None, stem

    target = steps["target"]
    assert target == {"on": "builtin:2", "exposure": "+0.7", "contrast": "+12", "barAfterBack": 1}

    gap = steps["gap"]
    assert [h["cnt"] for h in gap["gap1"]] == [f"{len(g)} 张" for g in expect["1"] if len(g) > 1]
    assert gap["confirm"]["title"] == f"套用到这一组的 {len(group1) - 1} 张？"

    pick = steps["pick"]
    assert pick["button"] == "复制 2 项" and pick["pickTitle"] == "选要套用的照片"
    assert pick["pickBar"]["sub"] == "已选 2 张"
    for line in ("配方 / 风格、调整", f"来自 {source}", "套用到 2 张"):
        assert line in pick["pickBar"]["text"], line
    assert pick["confirm"]["title"] == "套用到 2 张？"
    assert pick["applied"]["line"] == "已套用到 2 张"
    for before, now, undone in zip(pick["before"], pick["after"], pick["restored"], strict=True):
        assert now["choice"] == src["after"]["choice"] and now["knobs"] == src["after"]["knobs"]
        assert now["rev"] == before["rev"] + 1
        assert undone["choice"] == before["choice"] and undone["knobs"] == before["knobs"]
        assert undone["levels"] == before["levels"] and undone["memory"] == before["memory"]
        assert undone["rev"] == before["rev"] + 2
    assert pick["undone"] == "已撤销 2 张"

    changed = steps["changed"]
    assert changed["confirm"]["title"] == "套用到 2 张？"
    assert changed["applied"]["line"] == "已套用到 2 张"
    assert changed["line"] == "已撤销 1 张；1 张粘贴之后又改过，没有撤销"
    # 改过的那张不动；另一张回到套用之前（选照片那一批撤销后的样子）。
    assert changed["p1"] == changed["p1Before"] and changed["p1"]["knobs"]["shadow"] == -15
    assert changed["p2"]["knobs"] == pick["restored"][1]["knobs"] and changed["p2"]["choice"] == pick["restored"][1]["choice"]

    # 04e 的铅笔：按现在的编辑状态，同组套用后这一组每一张都有（来源也有编辑）。
    assert group["pencils"] == sorted(stem.lower() for stem in group3)

    cleared = steps["cleared"]
    assert "已复制：配方 / 风格、调整" in cleared["copiedBar"] and f"来自 {source} · 点组标题上的「套用到这一组」" in cleared["copiedBar"]
    assert cleared["bars"] == 0 and cleared["goButtons"] == 0

    # Sol r1 P2：只套用配方之后，零值的「全部重置」、点两下已经是 0 的滑块、松开没动的滑块都不加 rev，撤销照样还原。
    noop = steps["noop"]
    assert noop["recipeOnly"] == "复制 1 项"
    assert noop["before"]["choice"] is None and set(noop["before"]["knobs"].values()) == {0}
    # 只有一张时桌面 pasteMessage 说得更直接。
    assert noop["applied"]["line"] == "已套用：配方 / 风格"
    assert noop["pasted"]["rev"] == noop["before"]["rev"] + 1 and noop["pasted"]["choice"] == src["after"]["choice"]
    assert noop["afterNoop"]["rev"] == noop["pasted"]["rev"]
    assert noop["line"] == "已撤销 1 张"
    assert noop["restored"]["choice"] is None and noop["restored"]["rev"] == noop["pasted"]["rev"] + 1
    target2 = picks[1].lower()
    assert target2 in noop["pencilsBeforeUndo"] and target2 not in noop["pencilsAfterUndo"]


CAPTURE_DRIVER = REPO / "tests" / "support" / "mobile_ui_group_capture_browser.mjs"


def test_mobile_ui_group_capture_time_falls_back_to_the_dng(tmp_path: Path) -> None:
    """RAW+JPG 配对、相机 JPG 里没有拍摄时间：和桌面导入一样用 DNG 的拍摄时间，不用文件日期。

    JPG 是测试里合成的（没有 EXIF），DNG 是真样本在 tmp_path 里的副本。另有一张只有合成 JPG 的照片：两边都用修改时间。"""
    import os

    from PIL import Image

    from gxr_studio.meta import read_dng_meta, read_jpeg_meta
    from test_mobile_ui_group_unit import _subsec_ms, _taken_ms

    dngs = sorted(SAMPLES.glob("*.DNG")) if SAMPLES.is_dir() else []
    samples.need_sample(bool(dngs), f"need a DNG in {SAMPLES}")
    if not dngs:
        pytest.skip(f"need a DNG in {SAMPLES}")
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")

    paired_jpg, paired_dng, lone = tmp_path / "R9990010.JPG", tmp_path / "R9990010.DNG", tmp_path / "R9990011.JPG"
    shutil.copyfile(dngs[0], paired_dng)
    for path, colour in ((paired_jpg, (120, 90, 60)), (lone, (60, 90, 120))):
        Image.new("RGB", (96, 64), colour).save(path, "JPEG", quality=90)
    stamp = 1_700_000_000  # 整秒：File.lastModified 是毫秒
    for path in (paired_jpg, paired_dng, lone):
        os.utime(path, (stamp, stamp))
    check(read_jpeg_meta(paired_jpg).taken is None and read_jpeg_meta(lone).taken is None, "synthetic JPGs have no capture time")
    dng_taken = read_dng_meta(paired_dng).taken
    check(dng_taken is not None, "the DNG has a capture time")
    assert dng_taken is not None
    # 桌面导入：next(m.taken for m in (jpg_meta, dng_meta) if m.taken)，手机再加 DNG 的 SubSecTimeOriginal。
    want = {"R9990010": _taken_ms(dng_taken) + _subsec_ms(paired_dng), "R9990011": stamp * 1000}

    manifest = tmp_path / "capture.json"
    manifest.write_text(json.dumps({"files": [str(paired_dng), str(paired_jpg), str(lone)], "photos": 2}), encoding="utf-8")
    result = _drive(CAPTURE_DRIVER, manifest, tmp_path, "", timeout=240)
    assert result.returncode == 0, result.stderr[-3000:] + result.stdout[-3000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["pageErrors"] == []
    got = {row["stem"]: row["ms"] for row in sorted(report["photos"], key=lambda row: row["stem"])}
    same_redacted(got, want, "paired JPG without a time uses the DNG's; a lone JPG uses lastModified")
    check(got["R9990010"] != stamp * 1000, "not the file date")


LEVEL_DRIVER = REPO / "tests" / "support" / "mobile_ui_group_level_browser.mjs"


def test_mobile_ui_group_recipe_knob_same_value_keeps_rev(tmp_path: Path) -> None:
    """Sol r2：配方选中、强度默认 100 没存键，同值的 change（或拖到 0 再回 100）不是修改：rev 不变，套用照样能撤销。

    对照：已存的同值档位也不加 rev；真的改了档位才加一，撤销时算「粘贴之后又改过」。合成的配方列表，不读照片。"""
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")
    manifest = tmp_path / "level.json"
    manifest.write_text("{}", encoding="utf-8")
    result = _drive(LEVEL_DRIVER, manifest, tmp_path, "", timeout=180)
    assert result.returncode == 0, result.stderr[-3000:] + result.stdout[-3000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["pageErrors"] == []
    cases = {row["action"]: row for row in report["cases"]}
    for name in ("default-same", "default-roundtrip"):
        row = cases[name]
        assert row["afterRev"] == row["beforeRev"] == 1, (name, row)
        assert row["levels"] == {"builtin:2": {}}, (name, row)
        assert row["counts"] == {"restored": 1, "changed": 0, "error": 0} and row["choiceAfterUndo"] is None, (name, row)
    stored = cases["stored-same"]
    assert stored["afterRev"] == stored["beforeRev"] == 1 and stored["counts"]["restored"] == 1
    changed = cases["changed"]
    assert changed["afterRev"] == 2 and changed["levels"] == {"builtin:2": {"amount": 50}}
    assert changed["counts"] == {"restored": 0, "changed": 1, "error": 0}
