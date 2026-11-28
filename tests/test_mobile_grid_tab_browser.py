"""电脑上网格只占一个 Tab 位（roving tabindex，2026-11-29 backlog-4b，`browser_serial`）。

- 电脑（没有触摸点、指针精确）1440×900、30 张照片：从顶栏「选择」按一次 Tab 落到一格，再按一次就出网格；Shift-Tab 回来还是
  那一格。方向键走过以后，Tab 出去再 Shift-Tab 回来落在走到的那一格；Enter 照旧打开照片。
- 有触摸的手机尺寸不变：每格都能 Tab 到，从「选择」按两次 Tab 还在网格里。

真实照片：本机拍摄文件夹里的 30 张相机 JPG。报告里只有布尔和计数，没有文件名、拍摄时间或 EXIF。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from support import samples
from support.export_pipeline_cases import check
from test_mobile_camera_recipe_browser import _prereq
from test_mobile_ui_browser import _drive

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "support" / "mobile_grid_tab_browser.mjs"
FOLDERS = ("r6-field-20261118/card250", "r6-field-20261118/kubik_r6", "r6-field-20261118/vertical", "r6-field-20261118/card249")
COUNT = 30


def _jpgs() -> list[Path]:
    out: list[Path] = []
    for name in FOLDERS:
        folder = samples.captures() / name
        if not folder.is_dir():
            samples.missing(f"captures/{folder.name} 不在本机")
        out += sorted(p for p in folder.iterdir() if p.suffix == ".JPG")
    if len(out) < COUNT:
        samples.missing(f"need {COUNT} camera JPGs, found {len(out)}")
    return out[:COUNT]


def _run(tmp_path: Path) -> dict[str, Any]:
    _prereq()
    report_path = tmp_path / "report.json"
    manifest = {"files": [str(p) for p in _jpgs()], "count": COUNT, "reportPath": str(report_path)}
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = _drive(DRIVER, manifest_path, tmp_path, "", timeout=900)
    check(report_path.is_file(), "driver wrote its report")
    report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    check(result.returncode == 0 and report.get("error") is None, f"driver completed without error: {report.get('error')}")
    check(report["pageErrors"] == [], f"no page errors: {report['pageErrors']}")
    return report["steps"]


def test_mobile_grid_tab(tmp_path: Path) -> None:
    steps = _run(tmp_path)
    pc = steps["computer"]
    check(pc["computer"] is True and pc["cells"] >= 9, f"computer context with a full grid ({pc['computer']}, {pc['cells']} cells)")
    check(pc["tabbable"] == 1, f"computer: exactly one grid cell is in the Tab order ({pc['tabbable']} of {pc['cells']})")
    check(pc["first"]["cell"] is True and pc["first"]["index"] == 0, f"one Tab from the toolbar reaches the first cell ({pc['first']})")
    check(pc["second"]["inGrid"] is False, f"the next Tab leaves the grid ({pc['second']})")
    check(pc["back"]["id"] == pc["first"]["id"], "Shift-Tab comes back to the same cell")
    moved = pc["moved"]
    check(moved["cell"] is True and moved["index"] > 0, f"arrow keys still move between cells ({moved})")
    check(pc["out"]["inGrid"] is False and pc["again"]["id"] == moved["id"], "Tab out and back lands on the cell the arrows reached")
    check(pc["tabbableAfter"] == 1, f"still one Tab stop after the arrows ({pc['tabbableAfter']})")
    check(pc.get("enterOpens") is True, "Enter still opens the photo")

    ph = steps["phone"]
    check(ph["computer"] is False, "phone context has touch")
    check(ph["tabbable"] == ph["cells"], f"phone: every cell stays in the Tab order ({ph['tabbable']} of {ph['cells']})")
    check(ph["first"]["cell"] is True and ph["second"]["cell"] is True, f"phone: Tab walks cell to cell as before ({ph['second']})")
    check(ph["second"]["index"] == ph["first"]["index"] + 1, "phone: the second Tab is the next cell")
