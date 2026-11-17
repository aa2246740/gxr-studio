"""The phone reads an r5 card (GXRCE.DAT + GXRFX.BIN) the same way the desktop does.

The 06d sheet shows the core's missing-library error. DAT then BIN, and DAT+BIN together,
load 24×552 entries. The listing and one resolved entry match ``CameraData``.

Export passes the same library bytes. ``export_prepare`` on WASM matches ``gxr-cli`` (and
omitting the library changes that result). One real RAW+JPG exported from the browser matches
desktop ``export_finish`` byte for byte.
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
EXPORT_DRIVER = REPO / "tests" / "support" / "mobile_r5_export_browser.mjs"
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


def _blocks(side: dict[str, Any]) -> dict[str, Any]:
    """The ``recipe`` and ``adjust`` blocks, with ``exposure_ev`` as a float."""
    out = {k: json.loads(json.dumps(side[k])) for k in ("recipe", "adjust") if k in side}
    if "exposure_ev" in out.get("adjust", {}):
        out["adjust"]["exposure_ev"] = float(out["adjust"]["exposure_ev"])
    return out


class _R5Desk:
    """Desktop home for the owner pair. Same shot/export methods as the M3 ``Desktop``, but the DAT
    is imported with its library."""

    def __init__(self, tmp: Path, raw: bytes, library: bytes) -> None:
        from gxr_studio.camdata import calib, calib_export, store
        from gxr_studio.library.store import Library
        from gxr_studio.render.service import RenderService
        from support.mobile_ui_calib import pack_files, write_pack

        self.home = tmp / "app-home"
        self.home.mkdir()
        store.import_from_dat(raw, self.home, library=library)
        _dat, entries = store.entries_from_dat(raw, library)
        names = ("jlaunch", "qlaunch", "l08firm")
        pack = pack_files({name: entries[(unit, 4)] for unit, name in enumerate(names)})
        write_pack(calib.calib_dir(self.home), pack)
        self.zip = tmp / "calib.zip"
        self.zip.write_bytes(calib_export.zip_bytes(pack, (2026, 11, 16, 9, 0, 0)))
        self.lib = Library(tmp / "library").open()
        self.render = RenderService(self.home, self.lib)
        self.folder = self.lib.root / "2026" / "m3"
        self.folder.mkdir(parents=True)
        self.export_dir = tmp / "desktop-exports"
        self.export_dir.mkdir()

    def binding(self) -> str:
        self.render._refresh()
        return str(self.render.engine_pin())

    def shot(self, stem: str, dng: Path | None, jpg: Path | None) -> dict[str, Any]:
        from test_mobile_ui_export_browser import Desktop

        return Desktop.shot(self, stem, dng, jpg)  # type: ignore[no-any-return]

    def changes(self, shot: dict[str, Any], done: dict[str, Any]) -> dict[str, Any]:
        from test_mobile_ui_export_browser import Desktop

        return Desktop.changes(self, shot, done)  # type: ignore[no-any-return]

    def sidecar(self, shot: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
        from test_mobile_ui_export_browser import Desktop

        return Desktop.sidecar(self, shot, changes)  # type: ignore[no-any-return]

    def export(self, shot: dict[str, Any], side: dict[str, Any], quality: int, long_edge: int | None) -> Any:
        from test_mobile_ui_export_browser import Desktop

        return Desktop.export(self, shot, side, quality, long_edge)

    def close(self) -> None:
        self.lib.close()


@pytest.fixture
def pinned_icc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> bytes:
    """The export-pipeline ICC pin: only header bytes 24..36 (the creation time) may differ."""
    import numpy as np
    from PIL import Image

    from gxr_studio import export as export_mod
    from gxr_studio.export import srgb_icc
    from support.core_cli import require_cli, run_json
    from support.export_pipeline_cases import cli_ok

    require_cli()
    np.save(tmp_path / "icc.npy", np.zeros((8, 8, 3), dtype=np.uint8))
    code, payload = run_json(["jpeg-encode", "--in", str(tmp_path / "icc.npy"), "--quality", "95", "--out", str(tmp_path / "icc.jpg")])
    cli_ok(code, payload, "jpeg-encode for the ICC pin")
    with Image.open(tmp_path / "icc.jpg") as im:
        rs = bytes(im.info["icc_profile"])
    py = srgb_icc()
    check(len(rs) == len(py) == 588 and rs[:24] == py[:24] and rs[36:] == py[36:], "ICC profiles differ outside bytes 24..36")
    monkeypatch.setattr(export_mod, "_ICC", rs)
    return rs


def test_mobile_r5_export_native_matches_wasm(tmp_path: Path) -> None:
    """``export_prepare`` with this card's library: WASM matches ``gxr-cli``. Without the library the result changes."""
    from types import SimpleNamespace

    from gxr_studio.camdata import calib, store
    from gxr_studio.render.params import RecipeSel
    from gxr_studio.render.service import RenderService
    from support import export_source_cases as xs
    from support.dng_writer import write_dng
    from support.wasm_check import run_area
    from test_core_parity_export_source import _mosaic

    raw, library = _owner_pair()
    home = tmp_path / "home"
    store.import_from_dat(raw, home, library=library)
    dat_path = tmp_path / "GXRCE.DAT"
    lib_path = tmp_path / "GXRFX.BIN"
    dat_path.write_bytes(raw)
    lib_path.write_bytes(library)
    calib.import_calibration(home, xs.synthetic_sources(home, ("jlaunch",)))
    view = SimpleNamespace(root=tmp_path / "library", abspath=lambda rel: Path(rel))
    render = RenderService(home, view)
    render._refresh()
    cam = render._camera
    check(cam is not None, "r5 home loaded a camera")
    entry = cam.resolve(RecipeSel("jlaunch", 1)).entry  # type: ignore[union-attr]
    check(len(entry) == 552, "r5 resolve entry is 552 bytes")
    entry_path = tmp_path / "entry.bin"
    entry_path.write_bytes(entry)
    dng = tmp_path / "R5SYN.DNG"
    dng.write_bytes(write_dng(_mosaic(48, 64, 1), white=4095, orientation=6))
    shot = tmp_path / "shot.json"
    shot.write_text(
        json.dumps(
            {
                "name": "R5SYN",
                "width": 48,
                "height": 64,
                "files": {"dng": {"relpath": str(dng)}},
                "lens": "GR LENS A12 50mm F2.5 MACRO",
                "iso": 200,
            }
        ),
        encoding="utf-8",
    )
    side = tmp_path / "side.json"
    side.write_text(json.dumps({"recipe": {"unit": "jlaunch", "slot": 1}, "adjust": {"contrast": 8}}), encoding="utf-8")
    common = {
        "kind": "prepare",
        "shot": str(shot),
        "side": str(side),
        "dat": str(dat_path),
        "entry": str(entry_path),
        "calib": str(calib.calib_dir(home)),
        "binding": str(render.engine_pin()),
        "dng": str(dng),
        "quality": 95,
        "base": "auto",
    }
    with_lib = {**common, "label": "r5-with-library", "library": str(lib_path), "out": str(tmp_path / "with")}
    without = {**common, "label": "r5-without-library", "out": str(tmp_path / "without")}
    manifest = tmp_path / "r5-prepare.json"
    manifest.write_text(json.dumps([with_lib, without]), encoding="utf-8")
    run_area("export", [manifest], tmp_path)
    with_canvas = Path(f"{with_lib['out']}.canvas.npy")
    without_canvas = Path(f"{without['out']}.canvas.npy")
    check(with_canvas.is_file() and with_canvas.stat().st_size > 128, "r5 prepare with GXRFX.BIN wrote a canvas")
    if without_canvas.is_file():
        check(with_canvas.read_bytes() != without_canvas.read_bytes(), "r5 prepare without GXRFX.BIN differs")


def test_mobile_r5_export_matches_desktop(tmp_path: Path, pinned_icc: bytes) -> None:
    """The browser export of the owner pair plus one real RAW+JPG equals desktop ``export_finish``."""
    from gxr_studio.render.params import parse_edit

    pair = mx.real_pairs(1)
    if len(pair) < 1:
        samples.missing("need one DNG+JPG pair")
    _prereq()
    check(len(pinned_icc) == 588, "ICC pin is 588 bytes")
    raw, library = _owner_pair()
    dng, jpg = pair[0]
    desk = _R5Desk(tmp_path, raw, library)
    try:
        out_dir = tmp_path / "phone"
        out_dir.mkdir()
        report_path = tmp_path / "r5-export-report.json"
        manifest = {
            "stem": dng.stem,
            "count": 1,
            "pack": str(desk.zip),
            "files": [str(dng), str(jpg), str(OWNER / "GXRCE.DAT"), str(OWNER / "GXRFX.BIN")],
            "binding": desk.binding(),
            "outDir": str(out_dir),
            "out": str(report_path),
        }
        path = tmp_path / "r5-export-manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        result = _drive(EXPORT_DRIVER, path, tmp_path, "", timeout=900)
        if result.returncode != 0 or not report_path.is_file():
            pytest.fail("r5 export browser driver failed")
        report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
        check(report.get("error") is None, "r5 export driver finished")
        check(report.get("pageErrors") == [], "no page errors")
        done = {"card": "builtin:1"}
        shot = desk.shot(dng.stem, dng, jpg)
        side = desk.sidecar(shot, desk.changes(shot, done))
        fact = report["export"]["facts"]["facts"][dng.stem]
        same_redacted(parse_edit(fact["side"]).to_json(), parse_edit(side).to_json(), "phone side parse_edit equals the desktop's")
        same_redacted(_blocks(fact["side"]), _blocks(side), "phone side blocks equal the desktop sidecar's")
        check(fact["width"] == shot["width"] and fact["height"] == shot["height"], "shot width/height equal the library's photo_meta")
        check(fact["binding"] == desk.binding(), "dev binding override reached the request")
        res = desk.export(shot, side, 95, None)
        phone = out_dir / f"r5-{res.path.name}"
        got_bytes = phone.read_bytes() if phone.is_file() else None
        same_file_redacted(got_bytes, res.path.read_bytes(), "phone file equals desktop export_finish (ICC pinned)")
        got = {k: fact["result"][k] for k in ("file_name", "render", "render_label", "render_kind", "calibrated", "detail")}
        want = {
            "file_name": res.path.name,
            "render": res.render,
            "render_label": res.render_label,
            "render_kind": res.render_kind,
            "calibrated": res.calibrated,
            "detail": res.detail,
        }
        same_redacted(got, want, "result JSON equals the desktop ExportResult")
    finally:
        desk.close()
