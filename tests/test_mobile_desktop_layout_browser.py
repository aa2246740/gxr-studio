"""电脑浏览器版式回归（2026-11-26，`browser_serial`，生产 mobile/dist）。

L1：没有 WebGPU（WebKit、去掉 navigator.gpu 的 Chromium）时，相机 JPG 的后备画布在 iPad/电脑版式里按 contain 落在舞台里，
和 Chromium Metal 画出来的照片矩形相差不超过 2 px；裁剪框贴着照片。iPad（有触摸）和手机尺寸不动。

只用本地 card250 实拍照片和 g250 相机配方；报告里只有矩形和计数，没有文件名、拍摄时间或 EXIF。
`GXR_DESKTOP_LAYOUT_DIST` 可以指到别的构建（比如旧 head 的 dist），用来证明回归在旧代码上失败。
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

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DIST = Path(os.environ.get("GXR_DESKTOP_LAYOUT_DIST", "") or REPO / "mobile" / "dist")
DRIVER = REPO / "tests" / "support" / "mobile_desktop_layout_browser.mjs"
TOL = 2.0

Rect = dict[str, float]


def check(ok: bool, label: str) -> None:
    """报告里只有矩形和计数（没有照片信息），失败时原样打出来。"""
    assert ok, label


def _run(tmp_path: Path, groups: list[str]) -> dict[str, Any]:
    _prereq()
    _need((DIST / "index.html").is_file(), "mobile/dist is missing; run make mobile-build")
    _raw250, _lib250, dat250, fx250 = _need_g185_or_g250(250)
    dng, jpg = _card250_pair(1)
    out = tmp_path / "report.json"
    manifest = {
        "distDir": str(DIST),
        "files": [str(dng), str(jpg), str(dat250), str(fx250)],
        "stem": dng.stem,
        "groups": groups,
        "out": str(out),
        "shotsDir": os.environ.get("GXR_DESKTOP_LAYOUT_SHOTS", ""),
        "shotsPrefix": os.environ.get("GXR_DESKTOP_LAYOUT_SHOTS_PREFIX", "after"),
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
        timeout=1500,
    )
    assert proc.returncode == 0, f"driver failed ({proc.returncode}): {proc.stderr[-1500:]}"
    report: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
    dump = os.environ.get("GXR_DESKTOP_LAYOUT_REPORT", "")
    if dump:
        Path(dump).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name in groups:
        row = report["groups"][name]
        assert "error" not in row, f"{name}: {row['error']}"
    return report["groups"]


def _inside(inner: Rect, outer: Rect, what: str) -> None:
    check(inner["x"] >= outer["x"] - 1 and inner["y"] >= outer["y"] - 1, f"{what}: photo starts inside the stage ({inner} in {outer})")
    check(
        inner["x"] + inner["w"] <= outer["x"] + outer["w"] + 1 and inner["y"] + inner["h"] <= outer["y"] + outer["h"] + 1,
        f"{what}: photo ends inside the stage ({inner} in {outer})",
    )
    touches_w = abs(inner["w"] - outer["w"]) <= TOL
    touches_h = abs(inner["h"] - outer["h"]) <= TOL
    check(touches_w or touches_h, f"{what}: photo is letterboxed (fills one side of the stage) ({inner} in {outer})")


def _near(a: Rect, b: Rect, what: str) -> None:
    worst = max(abs(a[k] - b[k]) for k in ("x", "y", "w", "h"))
    check(worst <= TOL, f"{what}: rectangles differ by {worst:.1f} px ({a} vs {b})")


def test_mobile_desktop_no_gpu_photo_fit(tmp_path: Path) -> None:
    groups = _run(tmp_path, ["fit"])["fit"]
    metal = groups["metal"]
    check(
        metal.get("adapter") is not None and metal["adapter"]["fallback"] is False, f"Metal Chromium has a real adapter: {metal.get('adapter')}"
    )
    check(metal["errors"] == [], f"Metal: no page errors: {metal['errors']}")
    ref = {row["size"]: row for row in metal["rows"]}
    for row in metal["rows"]:
        check(row["view"]["kind"] == "gpu", f"metal {row['size']}: GPU canvas shows")
        _inside(row["view"]["photo"], row["view"]["pv"], f"metal {row['size']}")
        _near(row["crop"]["cbox"], row["crop"]["photo"], f"metal {row['size']} crop frame")
    for engine in ("nogpu", "webkit"):
        res = groups[engine]
        if res.get("skipped"):
            pytest.skip(res["skipped"])
        check(res["errors"] == [], f"{engine}: no page errors: {res['errors']}")
        check(len(res["rows"]) == 4, f"{engine}: four sizes measured")
        for row in res["rows"]:
            size = row["size"]
            view, crop = row["view"], row["crop"]
            check(view["layout"].startswith("ipad"), f"{engine} {size}: iPad/desktop layout ({view['layout']})")
            check(view["kind"] == "still", f"{engine} {size}: camera-JPG fallback shows")
            _inside(view["photo"], view["pv"], f"{engine} {size}")
            _near(view["photo"], ref[size]["view"]["photo"], f"{engine} {size} vs Metal")
            _inside(crop["photo"], crop["pv"], f"{engine} {size} crop")
            _near(crop["cbox"], crop["photo"], f"{engine} {size} crop frame on the photo")
            _near(crop["cbox"], ref[size]["crop"]["cbox"], f"{engine} {size} crop frame vs Metal")


def test_mobile_desktop_touch_fit_unchanged(tmp_path: Path) -> None:
    """iPad（Mac UA + 触摸）和手机尺寸：照片仍在舞台里按 contain 摆放，裁剪框贴着照片。"""
    groups = _run(tmp_path, ["fitTouch"])["fitTouch"]
    if groups.get("skipped"):
        pytest.skip(groups["skipped"])
    want = {"1180x820": "ipad-landscape", "1024x1366": "ipad-portrait", "390x844": "phone-portrait", "844x390": "phone-landscape"}
    for kind in ("ipad", "phone"):
        res = groups[kind]
        check(res["errors"] == [], f"{kind}: no page errors: {res['errors']}")
        for row in res["rows"]:
            size = row["size"]
            check(row["view"]["layout"] == want[size], f"{kind} {size}: layout {row['view']['layout']}")
            _inside(row["view"]["photo"], row["view"]["pv"], f"{kind} {size}")
            _near(row["crop"]["cbox"], row["crop"]["photo"], f"{kind} {size} crop frame")
