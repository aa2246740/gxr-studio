"""手机竖屏配方微调时，「配方 | LUT」切换键不能挡住参数片和滑块。

在 390×844 和 440×956 上，每个微调控件中心的 elementFromPoint 必须落在该控件上。
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from gxr_studio.camdata import calib_export, store
from support.mobile_ui_calib import pack_files
from test_g250 import _need_g185_or_g250
from test_mobile_camera_recipe_browser import _card250_pair
from test_mobile_ui_browser import RUNTIME, _drive, _need

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "support" / "mobile_lut_switch_finetune.mjs"


def _redact(text: str) -> str:
    return re.sub(r"/(?:Volumes|Users|private|var/folders|tmp)/\S+", "[path]", text)[-500:]


def test_phone_finetune_controls_are_not_under_the_mode_switch(tmp_path: Path) -> None:
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")

    raw250, lib250, dat250, fx250 = _need_g185_or_g250(250)
    dng, jpg = _card250_pair(3)
    _parsed, entries = store.entries_from_dat(raw250, library=lib250)
    pack = pack_files({name: entries[(unit, 4)] for unit, name in enumerate(("jlaunch", "qlaunch", "l08firm"))})
    pack_zip = tmp_path / "calib.zip"
    pack_zip.write_bytes(calib_export.zip_bytes(pack, (2026, 11, 21, 9, 0, 0)))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"files": [str(dng), str(jpg), str(dat250), str(fx250)], "pack": str(pack_zip)}),
        encoding="utf-8",
    )

    proc = _drive(DRIVER, manifest, tmp_path, "", timeout=240)
    raw = [line for line in proc.stdout.splitlines() if line.startswith("{")]
    assert raw, _redact((proc.stdout or "")[-400:])
    got: dict[str, Any] = json.loads(raw[-1])
    assert got.get("error") is None, got.get("error")
    assert [view["label"] for view in got["views"]] == ["390x844", "440x956"]
    for view in got["views"]:
        assert view["fine"] is True, view
        assert view["chipsTested"] >= 2, view
        assert view["slidersTested"] >= 1, view
        assert view["tested"] == view["chipsTested"] + view["slidersTested"], view
        assert view["misses"] == [], view
        assert view["switchHidden"] is True, view
