"""The phone reads an r5 card (GXRCE.DAT + GXRFX.BIN) the same way the desktop does.

The 06d sheet shows the core's missing-library error. DAT then BIN, and DAT+BIN together,
load 24×552 entries. The listing and one resolved entry match ``CameraData``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from g178_support import DAT_SHA256
from gxr_studio.camdata import r5, store
from gxr_studio.render.params import parse_recipe
from gxr_studio.render.recipes import CameraData
from support import mobile_ui_export as mx
from support import samples
from support.export_pipeline_cases import check, same_file_redacted, same_redacted
from test_mobile_ui_browser import MODULES, PKG, RUNTIME, _drive, _need

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "support" / "mobile_r5_dat_browser.mjs"
OWNER = Path("/Volumes/Workspace/Scratch/gxr-studio-try-real/home/camera-data/dat-template")
RECIPE = {"kind": "builtin", "unit": "jlaunch", "slot": 1, "levels": {}}


def _prereq() -> None:
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")


def _owner_pair() -> tuple[bytes, bytes]:
    dat, lib = OWNER / "GXRCE.DAT", OWNER / "GXRFX.BIN"
    if not dat.is_file() or not lib.is_file():
        samples.missing("owner r5 pair is not on this machine")
    raw, library = dat.read_bytes(), lib.read_bytes()
    if hashlib.sha256(raw).hexdigest() != DAT_SHA256[178]:
        pytest.fail("owner GXRCE.DAT is not the registered g178 file")
    if hashlib.sha256(library).hexdigest() != r5.LIBRARY_SHA256[178]:
        pytest.fail("owner GXRFX.BIN is not the registered g178 library")
    return raw, library


def _same_read(phone: dict[str, Any], cam: CameraData, library_len: int) -> None:
    assert phone["entries"] == 24 * 552
    assert phone["generation"] == 178
    assert phone["library"] == library_len
    same_redacted(phone["listing"], cam.listing(), "phone listing equals desktop listing")
    sel = parse_recipe(RECIPE)
    assert sel is not None
    resolved = cam.resolve(sel)
    same_file_redacted(base64.b64decode(phone["entry"]), resolved.entry, "phone resolve equals desktop resolve")
    same_redacted(phone["levels"], resolved.levels, "phone resolve levels equal desktop levels")
    same_redacted(phone["sha"], resolved.entry_sha256, "phone resolve sha equals desktop sha")


def test_mobile_r5_dat_sheet_listing_and_resolve(tmp_path: Path) -> None:
    """DAT alone shows the core error. Either pick order loads, and the read matches the desktop."""
    pair = mx.real_pairs(1)
    if len(pair) < 1:
        samples.missing("need one DNG+JPG pair")
    _prereq()
    raw, library = _owner_pair()
    dng, jpg = pair[0]
    home = tmp_path / "home"
    store.import_from_dat(raw, home, library=library)
    cam = CameraData(home)
    report_path = tmp_path / "r5-report.json"
    manifest = {
        "dng": str(dng),
        "jpg": str(jpg),
        "dat": str(OWNER / "GXRCE.DAT"),
        "bin": str(OWNER / "GXRFX.BIN"),
        "files": [str(dng), str(jpg), str(OWNER / "GXRCE.DAT"), str(OWNER / "GXRFX.BIN")],
        "out": str(report_path),
    }
    path = tmp_path / "r5-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    result = _drive(DRIVER, path, tmp_path, "", timeout=420)
    if result.returncode != 0 or not report_path.is_file():
        pytest.fail("r5 browser driver failed")
    report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    check(report.get("error") is None, "r5 driver finished")
    check(report.get("pageErrors") == [], "no page errors")

    sheet = report["sheet"]["text"]
    assert "缺少 GXRFX.BIN" in sheet
    assert "缺少 GXRFX.BIN" in report["sheet"]["panel"]
    assert "GXRFX.BIN（新卡才有）" in sheet
    assert "在「文件」里选 GXRCE.DAT 和 GXRFX.BIN" in sheet
    assert report["sheet"]["hidden"] is False
    _same_read(report["second"], cam, len(library))
    _same_read(report["loaded"], cam, len(library))
