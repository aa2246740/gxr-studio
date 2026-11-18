"""曲线导出和桌面 export_finish 逐字节相同。

六种编辑（主曲线 S、只动红、绿加蓝、端点拉到底、很多点、重置成恒等）落在 RAW+JPG 和只有 JPG 上。
恒等曲线不写进 sidecar，导出字节和没曲线一样。比较走脱敏函数，不打印照片。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from support import mobile_ui_export as mx
from support import samples
from support.export_pipeline_cases import check, same_file_redacted, same_redacted
from test_mobile_ui_export_browser import Desktop, _local_dat, _prereq, run_driver

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
PARITY = REPO / "tests" / "support" / "mobile_ui_curves_export_browser.mjs"

S = [[0, 0], [64, 32], [192, 220], [255, 255]]
R = [[0, 0], [128, 40], [255, 255]]
G = [[0, 0], [96, 180], [255, 255]]
B = [[0, 0], [160, 50], [255, 255]]
END = [[0, 0], [255, 0]]
MANY = [[0, 0], [36, 76], [72, 112], [108, 148], [144, 184], [180, 220], [216, 255], [255, 255]]
EXPECT = {
    "s-curve": {"master": S},
    "r-only": {"r": R},
    "g-b": {"g": G, "b": B},
    "endpoint": {"master": END},
    "many": {"master": MANY},
}


@pytest.fixture
def pinned_icc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> bytes:
    from PIL import Image as PilImage

    from gxr_studio import export as export_mod
    from gxr_studio.export import srgb_icc
    from support.core_cli import require_cli, run_json
    from support.export_pipeline_cases import cli_ok

    require_cli()
    np.save(tmp_path / "icc.npy", np.zeros((8, 8, 3), dtype=np.uint8))
    code, payload = run_json(["jpeg-encode", "--in", str(tmp_path / "icc.npy"), "--quality", "95", "--out", str(tmp_path / "icc.jpg")])
    cli_ok(code, payload, "jpeg-encode for the ICC pin")
    with PilImage.open(tmp_path / "icc.jpg") as im:
        rs = bytes(im.info["icc_profile"])
    py = srgb_icc()
    check(len(rs) == len(py) == 588 and rs[:24] == py[:24] and rs[36:] == py[36:], "ICC profiles differ outside bytes 24..36")
    monkeypatch.setattr(export_mod, "_ICC", rs)
    return rs


def _curve_of(side: dict[str, Any]) -> Any:
    from gxr_studio.render.params import parse_curve

    if "curve" not in side:
        return None
    parsed = parse_curve(side.get("curve"))
    return None if parsed is None else parsed.to_json()


def test_mobile_curve_exports_match_desktop(tmp_path: Path, pinned_icc: bytes) -> None:
    from gxr_studio.render.params import parse_edit

    pairs = mx.real_pairs(1)
    if not pairs:
        samples.missing("need a DNG+JPG pair")
    dat = _local_dat()
    _prereq()
    desk = Desktop(tmp_path, dat)
    try:
        dng, jpg = pairs[0]
        jobs = (
            ("s-curve", "pair", "R9990301"),
            ("r-only", "pair", "R9990302"),
            ("g-b", "pair", "R9990303"),
            ("endpoint", "jpg", "R9990304"),
            ("many", "jpg", "R9990305"),
            ("identity", "jpg", "R9990306"),
            ("none", "jpg", "R9990307"),
        )
        photos: dict[str, tuple[Path | None, Path | None]] = {}
        files = [str(dat)]
        manifest_jobs = []
        for kind, source, stem in jobs:
            if source == "pair":
                dng_path = mx.copy_as(dng, tmp_path / f"{stem}.DNG")
                jpg_path = mx.copy_as(jpg, tmp_path / f"{stem}.JPG")
                photos[stem] = (dng_path, jpg_path)
                files.extend((str(dng_path), str(jpg_path)))
            else:
                jpg_path = mx.copy_as(jpg, tmp_path / f"{stem}.JPG")
                photos[stem] = (None, jpg_path)
                files.append(str(jpg_path))
            manifest_jobs.append({"kind": kind, "stem": stem})
        out_dir = tmp_path / "phone"
        out_dir.mkdir()
        report = run_driver(
            tmp_path,
            {
                "mode": "parity",
                "parityDriver": str(PARITY),
                "files": files,
                "pack": str(desk.zip),
                "count": len(jobs),
                "jobs": manifest_jobs,
                "binding": desk.binding(),
                "outDir": str(out_dir),
            },
            timeout=1500,
        )
        check(report["pageErrors"] == [], "no page errors")
        edited = report["steps"]["edited"]
        check(len(edited) == len(jobs), "one curve edit per photo")
        for row, (kind, _source, _stem) in zip(edited, jobs, strict=True):
            check(row["kind"] == kind, "edit kind echoed")
            if kind in ("identity", "none"):
                check(row["curve"] is None, "editor stored no curve")
            else:
                same_redacted(row["curve"], EXPECT[kind], "editor curve matches the gesture")
        facts = report["steps"]["full"]["facts"]["facts"]
        shots = {stem: desk.shot(stem, *photos[stem]) for _kind, _source, stem in jobs}
        for kind, _source, stem in jobs:
            fact = facts.get(stem)
            check(isinstance(fact, dict), "export fact present")
            side = fact["side"]
            got = _curve_of(side)
            if kind in ("identity", "none"):
                check(got is None, "sidecar omits an identity curve")
                changes: dict[str, Any] = {}
            else:
                same_redacted(side.get("curve"), EXPECT[kind], "sidecar curve matches the gesture")
                changes = {"curve": side.get("curve")}
            shot = shots[stem]
            desktop_side = desk.sidecar(shot, changes)
            same_redacted(parse_edit(side).to_json(), parse_edit(desktop_side).to_json(), "phone side parse_edit equals the desktop sidecar")
            res = desk.export(shot, desktop_side, 95, None)
            phone = out_dir / f"curve-{res.path.name}"
            blob = phone.read_bytes() if phone.is_file() else None
            same_file_redacted(blob, res.path.read_bytes(), "phone file equals desktop export_finish")
    finally:
        desk.lib.close()
