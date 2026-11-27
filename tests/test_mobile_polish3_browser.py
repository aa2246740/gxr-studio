"""polish-3 回归（browser_serial，生产 mobile/dist，固定 WebKit，没有 WebGPU）。

1. iPad 和手机横屏的无显卡说明条：只有 RAW、没有 JPG 也没有内嵌预览的那张，说「这张只有 RAW，现在预览不了」和
   「导出仍会套用配方」，和舞台中间一致；有 JPG 的照旧说「预览只显示相机 JPG…」。转竖屏再转回来不变。
2. 手机横屏 LUT 色彩空间瓷砖（844×390、667×375）：每个内置空间都只显示短名，不带「· 文件已写明」，不在词中间折行
   （只在空格和全角左括号前折行）、不被裁切；按钮 aria-label 仍是全文。WebKit 和固定 Chromium 都查。
3. 手机横屏焦点顺序：Tab 走到的（固定 Chromium）和读屏走的 DOM 顺序里，右边一列控件的顺序和画面从上到下一致；
   转竖屏、再转回横屏也对。

只用合成照片和合成 `.cube`；驱动只写脱敏 JSON。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from conftest import REAL_HOME
from test_mobile_lut_landscape_browser import _cube, _synth_pair
from test_mobile_perf_startup_browser import _lazy_photos
from test_mobile_ui_browser import MODULES, PKG, RUNTIME, _need

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DIST = REPO / "mobile" / "dist"
DRIVER = REPO / "tests" / "support" / "mobile_polish3_webkit.mjs"

NO_GPU_TITLE = "这个浏览器现在用不了显卡预览"
NO_GPU_DETAIL = "预览只显示相机 JPG，导出仍会套用配\u2060方\u2060和\u00a0LUT"
NO_PREVIEW_TEXT = "这张只有 RAW，现在预览不了"
NO_GPU_RAW_DETAIL = "导出仍会套用配方"
# lib/lut.ts 里的全部内置色彩空间：id → 短名。每个空间配一个在「# Input:」里写明它的 .cube。
SPACES_TS = REPO / "mobile" / "src" / "lib" / "lut.ts"
SPACE_LABELS = dict(re.findall(r"\{ id: '([^']+)', label: '[^']*', shortLabel: '([^']+)'", SPACES_TS.read_text(encoding="utf-8")))
CUBE_INPUTS = {
    "srgb": "sRGB",
    "rec709": "Rec.709",
    "gamma24": "Gamma 2.4",
    "flog": "F-Log",
    "flog2": "F-Log2",
    "llog": "L-Log",
    "llog709": "L-Log709",
    "vlog": "V-Log",
    "slog3": "S-Log3",
    "slog3-sgamut3": "S-Log3 S-Gamut3",
    "applelog": "Apple Log",
    "linear": "Linear",
}
CUBE_TITLES = {key: f"Tile {i:02d}" for i, key in enumerate(CUBE_INPUTS, start=1)}
LANDSCAPE_VPS = ("844x390", "667x375")


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need((DIST / "index.html").is_file(), "mobile/dist is missing; run make mobile-build")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")
    tmp = tmp_path_factory.mktemp("polish3")
    lazy = _lazy_photos(tmp / "photos")
    pair_dir = tmp / "pair"
    pair_dir.mkdir()
    dng, jpg = _synth_pair(pair_dir, "R9980301")
    cubes = {}
    for key, declared in CUBE_INPUTS.items():
        path = tmp / f"s{CUBE_TITLES[key][-2:]}.cube"
        path.write_text(_cube(CUBE_TITLES[key], 0.05).replace("# Input: sRGB", f"# Input: {declared}"), encoding="utf-8")
        cubes[key] = str(path)
    out = tmp / "report.json"
    manifest = tmp / "manifest.json"
    payload = {
        "distDir": str(DIST),
        "photos": {"raw": lazy["raw"], "jpeg": lazy["jpeg"], "pair": [str(dng), str(jpg)]},
        "cubes": cubes,
        "cubeTitles": CUBE_TITLES,
        "out": str(out),
        "shotsDir": os.environ.get("GXR_POLISH3_SHOTS_DIR", ""),
        "shotsPrefix": os.environ.get("GXR_POLISH3_SHOTS_PREFIX", "after"),
    }
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(DRIVER), str(RUNTIME), str(manifest)],
        cwd=REPO,
        env={**os.environ, "HOME": str(REAL_HOME)},
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, f"driver failed ({proc.returncode}): {proc.stderr[-1500:]}"
    data: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
    if data.get("skipped"):
        pytest.skip(data["skipped"])
    dump = os.environ.get("GXR_POLISH3_REPORT", "")
    if dump:
        Path(dump).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name, row in data["scenarios"].items():
        assert "error" not in row, f"{name}: {row['error']}"
        assert row["errors"] == [], f"{name} page errors: {row['errors']}"
    return data


def _scenario(report: dict[str, Any], name: str) -> dict[str, Any]:
    row: dict[str, Any] = report["scenarios"][name]
    return row


@pytest.mark.parametrize(
    ("name", "layout"),
    [
        ("noticeIpadLandscape", "ipad-landscape"),
        ("noticeIpadPortrait", "ipad-portrait"),
        ("noticeLandscape844", "phone-landscape"),
        ("noticeLandscape667", "phone-landscape"),
    ],
)
def test_no_gpu_notice_matches_the_stage_for_raw_only(report: dict[str, Any], name: str, layout: str) -> None:
    row = _scenario(report, name)
    raw, jpg = row["raw"], row["jpg"]
    assert raw["layout"] == layout and raw["count"] == 1 and raw["visible"], raw
    assert raw["noPreview"] == NO_PREVIEW_TEXT, f"stage does not say it cannot preview: {raw}"
    assert (raw["title"], raw["detail"]) == (NO_PREVIEW_TEXT, NO_GPU_RAW_DETAIL), raw
    assert jpg["layout"] == layout and jpg["count"] == 1 and jpg["visible"] and jpg["noPreview"] is None, jpg
    assert (jpg["title"], jpg["detail"]) == (NO_GPU_TITLE, NO_GPU_DETAIL), jpg
    if layout == "phone-landscape":
        assert row["rawPortrait"]["layout"] == "phone-portrait" and row["rawPortrait"]["count"] == 0, row["rawPortrait"]
        assert row["rawBack"] == raw, (row["rawBack"], raw)


def _words(label: str) -> list[str]:
    """短名里不许拆开的词：只能在空格处和全角左括号前折行。"""
    return [part for piece in label.split(" ") for part in re.split(r"(?=（)", piece) if part]


def _breaks_mid_word(lines: list[str], words: list[str]) -> list[str]:
    return [word for word in words if not any(word in line for line in lines)]


def test_every_built_in_space_has_a_cube() -> None:
    assert len(SPACE_LABELS) >= 12, SPACE_LABELS
    assert set(CUBE_INPUTS) == set(SPACE_LABELS)


@pytest.mark.parametrize("scenario", ["spaceTile", "spaceTileChromium"])
@pytest.mark.parametrize("key", sorted(CUBE_INPUTS))
def test_landscape_space_tile_shows_short_label_without_mid_word_breaks(report: dict[str, Any], scenario: str, key: str) -> None:
    label = SPACE_LABELS[key]
    bad = []
    for vp in LANDSCAPE_VPS:
        tile = _scenario(report, scenario)["tiles"][f"{key}-{vp}"]
        assert tile is not None and tile["layout"] == "phone-landscape", tile
        assert tile["shown"] == label, f"{vp}: narrow landscape tile should show only the short label: {tile}"
        assert "".join(tile["lines"]).replace(" ", "") == label.replace(" ", ""), tile
        if _breaks_mid_word(tile["lines"], _words(label)):
            bad.append(f"{vp}: {tile['lines']}")
        assert not tile["clipped"] and tile["inButton"], f"{vp}: label clipped or outside the tile: {tile}"
        assert tile["aria"] == f"色彩空间：{label} · 文件已写明", tile["aria"]
    assert bad == [], f"{label!r} breaks mid-word: {bad}"


def test_portrait_space_menu_keeps_the_full_text(report: dict[str, Any]) -> None:
    menu = _scenario(report, "spaceTile")["portraitMenu"]
    assert menu is not None and "文件已写明" in (menu["aria"] or "") + menu["text"], menu


def _right_column(walk: list[dict[str, Any]], stage_right: int) -> list[dict[str, Any]]:
    return [step for step in walk if step["x"] >= stage_right - 1 and step["w"] > 0]


def _visual_order(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # 同一行（顶边差 ≤ 4 px）从左到右，行与行从上到下。
    out = sorted(steps, key=lambda s: (s["y"], s["x"]))
    rows: list[list[dict[str, Any]]] = []
    for step in out:
        if rows and abs(rows[-1][0]["y"] - step["y"]) <= 4:
            rows[-1].append(step)
        else:
            rows.append([step])
    return [step for row in rows for step in sorted(row, key=lambda s: s["x"])]


@pytest.mark.parametrize("walk", ["landscape", "landscapeDom", "landscapeAgain", "landscapeAgainDom"])
def test_phone_landscape_focus_follows_the_visual_order(report: dict[str, Any], walk: str) -> None:
    row = _scenario(report, "focus")
    steps = _right_column(row[walk], row["stage"]["right"])
    assert len(steps) >= 8, row[walk]
    regions = [step["region"] for step in steps]
    assert "toolbar" in regions and "band" in regions, regions
    got = [(s["name"], s["y"]) for s in steps]
    want = [(s["name"], s["y"]) for s in _visual_order(steps)]
    assert got == want, f"Tab order {got} differs from the visual order {want}"


@pytest.mark.parametrize("walk", ["portrait", "portraitDom"])
def test_phone_portrait_focus_keeps_band_before_toolbar(report: dict[str, Any], walk: str) -> None:
    regions = [step["region"] for step in _scenario(report, "focus")[walk]]
    assert "band" in regions and "toolbar" in regions, regions
    assert regions.index("band") < regions.index("toolbar"), regions
