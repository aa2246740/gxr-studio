"""27 寸大屏版式检查（机主 2026-11-28，`browser_serial`，生产 mobile/dist，Chromium Metal）。

网页窗口宽 >= 2000、高 >= 1100 才放大：2560×1440（2 倍屏）上网格每格 200–240、右边栏 440–480，角标和拉直尺的字比 10 px 大一点，
网格缩略图和配方卡小图的位图不小于显示尺寸 × 设备像素比，什么都不溢出。1440×900 和 1280×800 和原来一模一样。

只用本地 card250 实拍照片和 g250 相机配方；报告里只有盒子、字号和位图尺寸，没有文件名、拍摄时间或 EXIF。
`GXR_BIG_SCREEN_DIST` 可以指到别的构建（比如旧 head 的 dist），用来证明检查在旧代码上失败。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from conftest import REAL_HOME
from test_g250 import _need_g185_or_g250
from test_mobile_camera_recipe_browser import _card250_pair, _prereq
from test_mobile_ui_browser import RUNTIME, _need

pytestmark = [pytest.mark.browser_serial, pytest.mark.browser_alone]

REPO = Path(__file__).resolve().parents[1]
DIST = Path(os.environ.get("GXR_BIG_SCREEN_DIST", "") or REPO / "mobile" / "dist")
DRIVER = REPO / "tests" / "support" / "mobile_big_screen_browser.mjs"
CASES = [(2560, 1440, 2), (1440, 900, 2), (1280, 800, 2)]
# 小窗口原来的样子（origin/main 0f9d404d）：右边栏 13" 392 / 11" 360，角标 10 px 高 17，尺子度数 10 px，
# 网格缩略图解 360 宽，配方卡小图长边 192，网格每格约 160 分列。
SMALL_PANEL = {"1440x900@2": 392.0, "1280x800@2": 360.0}


def check(ok: bool, label: str) -> None:
    """报告里只有盒子和尺寸（没有照片信息），失败时原样打出来。"""
    assert ok, label


def _run(tmp_path: Path) -> list[dict[str, Any]]:
    _prereq()
    _need((DIST / "index.html").is_file(), "mobile/dist is missing; run make mobile-build")
    _raw250, _lib250, dat250, fx250 = _need_g185_or_g250(250)
    pairs = [_card250_pair(i) for i in (1, 2, 3)]
    solo_jpg = _card250_pair(4)[1]  # 只有 JPG 的一张：网格上挂「JPG」角标
    out = tmp_path / "report.json"
    manifest = {
        "distDir": str(DIST),
        "files": [str(p) for pair in pairs for p in pair] + [str(solo_jpg), str(dat250), str(fx250)],
        "stem": pairs[0][0].stem,
        "cases": [list(c) for c in CASES],
        "out": str(out),
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(DRIVER), str(RUNTIME), str(manifest_path)],
        cwd=REPO,
        env={**os.environ, "HOME": str(REAL_HOME)},
        check=False,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert proc.returncode == 0, f"driver failed ({proc.returncode}): {proc.stderr[-1500:]}"
    rows: list[dict[str, Any]] = json.loads(out.read_text(encoding="utf-8"))["rows"]
    dump = os.environ.get("GXR_BIG_SCREEN_REPORT", "")
    if dump:
        Path(dump).write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for row in rows:
        assert "error" not in row, f"{row['size']}: {row['error']}"
    return rows


def _sharp(items: list[dict[str, float]], dpr: float, what: str) -> None:
    """object-fit: cover 画进盒子：两边都要 位图 >= 显示 × 设备像素比，才不是放大的。"""
    for it in items:
        scale = max(it["bw"] / it["cw"], it["bh"] / it["ch"]) * dpr
        check(scale <= 1.01, f"{what}: bitmap {it['cw']}x{it['ch']} upscaled to {it['bw']}x{it['bh']} css px at {dpr}x ({scale:.2f})")


def _not_scaled(text: dict[str, Any] | None, what: str) -> None:
    check(text is not None, f"{what}: visible")
    assert text is not None
    check(text["scaleX"] == 1 and text["scaleY"] == 1 and str(text["zoom"]) in ("1", "normal"), f"{what}: drawn at its own size ({text})")


def _no_overflow(row: dict[str, Any]) -> None:
    size, grid, editor, ruler = row["size"], row["grid"], row["editor"], row["ruler"]
    check(row["errors"] == [], f"{size}: no page errors: {row['errors']}")
    check(grid["pageScroll"] is False and grid["gridOverflowX"] is False, f"{size}: grid does not overflow ({grid})")
    check(editor["pageScroll"] is False and editor["outside"] == [], f"{size}: editor fits the window ({editor['outside']})")
    check(editor["bandOverflowX"] is False and ruler["bandOverflowX"] is False, f"{size}: side panel does not overflow sideways")


def test_mobile_big_screen_layout(tmp_path: Path) -> None:
    rows = {row["size"]: row for row in _run(tmp_path)}
    for row in rows.values():
        _no_overflow(row)
        check(row["grid"]["layout"] == "ipad-landscape" and row["editor"]["layout"] == "ipad-landscape", f"{row['size']}: desktop layout")

    big = rows["2560x1440@2"]
    grid, editor, ruler = big["grid"], big["editor"], big["ruler"]
    check(grid["big"] is True, "2560x1440: the big-screen media query matches")
    check(200 <= grid["cellW"] <= 240, f"2560x1440: grid thumbnails 200–240 px ({grid['cellW']}, {grid['cols']} columns)")
    check(440 <= editor["panelW"] <= 480, f"2560x1440: side panel 440–480 px ({editor['panelW']})")
    check(grid["badge"] is not None and 10 < grid["badge"]["font"] <= 13, f"2560x1440: badge text slightly larger ({grid['badge']})")
    check(ruler["label"] is not None and 10 < ruler["label"]["font"] <= 13, f"2560x1440: ruler text slightly larger ({ruler['label']})")
    _not_scaled(grid["badge"], "2560x1440 badge")
    _not_scaled(ruler["label"], "2560x1440 ruler label")
    check(len(grid["thumbs"]) >= 4, f"2560x1440: grid thumbnails drawn ({len(grid['thumbs'])})")
    _sharp(grid["thumbs"], big["dpr"], "2560x1440 grid thumbnail")
    check(len(editor["cards"]) >= 3, f"2560x1440: predicted recipe cards drawn ({len(editor['cards'])})")
    _sharp(editor["cards"], big["dpr"], "2560x1440 recipe card")

    for size, panel in SMALL_PANEL.items():
        row = rows[size]
        grid, editor, ruler = row["grid"], row["editor"], row["ruler"]
        check(grid["big"] is False, f"{size}: not a big screen")
        cols = max(4, (grid["scrollerW"] + 2) // 160)
        want_cell = (grid["scrollerW"] - (cols - 1) * 2) / cols
        check(
            grid["cols"] == cols and abs(grid["cellW"] - want_cell) <= 0.6, f"{size}: grid unchanged ({grid['cols']} cols, {grid['cellW']} px)"
        )
        check(editor["panelW"] == panel, f"{size}: side panel unchanged ({editor['panelW']})")
        check(
            grid["badge"] is not None and grid["badge"]["font"] == 10 and grid["badge"]["h"] == 17, f"{size}: badge unchanged ({grid['badge']})"
        )
        check(ruler["label"] is not None and ruler["label"]["font"] == 10, f"{size}: ruler text unchanged ({ruler['label']})")
        check(
            grid["thumbs"] and all(t["cw"] == 360 for t in grid["thumbs"]),
            f"{size}: grid thumbnails still decoded 360 wide ({grid['thumbs'][:2]})",
        )
        check(
            editor["cards"] and all(max(c["cw"], c["ch"]) == 192 for c in editor["cards"]),
            f"{size}: recipe cards still baked at 192 ({editor['cards'][:2]})",
        )
