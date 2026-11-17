"""UI-M4 frame item 9: framed exports in the browser, compared with the desktop.

The sample folder has four DNG+JPG pairs. Styles E, F and the JPG-only photo use copies of those
files under synthetic names, so the grid can hold every style the spec lists. Comparisons go
through the redacted helpers. The driver report stays in the test's temporary directory.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image, ImageDraw

from support import mobile_ui_export as mx
from support import mobile_ui_frame_export as fx
from support import samples
from support.export_pipeline_cases import check, note, same_redacted
from support.ink_text_cases import dehinted_font_paths, font_path
from test_mobile_ui_export_browser import Desktop, _local_dat, _prereq, run_driver

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "support" / "mobile_ui_frame_export_browser.mjs"
_JOBS = (
    ("full", "full-", "m4_full", 95, None, False),
    ("small", "small-", "m4_small", 70, 1600, False),
    ("hdr", "hdr-", "m4_hdr", 95, None, True),
    ("memSdr", "memsdr-", "m4_memsdr", 95, None, False),
    ("memHdr", "memhdr-", "m4_memhdr", 95, None, True),
    ("deletedLogo", "del-", "m4_del", 95, None, False),
)
_ROLE = {"full": "", "small": "-small", "hdr": "-hdr", "memSdr": "-mem", "memHdr": "-mem-hdr", "deletedLogo": "-deleted"}
_DEHINT_PREFIX = {"memSdr": "m4_memsdr_dh", "memHdr": "m4_memhdr_dh"}


@pytest.fixture
def pinned_icc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> bytes:
    """The export-pipeline ICC pin: only header bytes 24..36 may differ."""
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


def _logo_png(dest: Path) -> Path:
    img = Image.new("RGBA", (128, 128), (255, 255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([28, 28, 99, 99], fill=(22, 24, 28, 255))
    draw.ellipse([48, 48, 79, 79], fill=(214, 40, 31, 255))
    img.save(dest, format="PNG")
    return dest


def _no_cjk(value: object) -> bool:
    return isinstance(value, str) and all(ord(ch) < 0x2E80 for ch in value)


def _canvas(step: dict[str, Any], prefix: str, index: int, directory: Path) -> Path | None:
    path = directory / f"{prefix}-{index}.bin"
    return path if path.is_file() else None


def _phone_file(directory: Path, tag: str, fact: dict[str, Any]) -> Path | None:
    name = fact.get("result", {}).get("file_name")
    if not isinstance(name, str):
        return None
    path = directory / f"{tag}{name}"
    return path if path.is_file() else path


def _deleted_step(report: dict[str, Any]) -> dict[str, Any]:
    return report["steps"]["deletedLogo"]["export"]


def test_mobile_ui_frame_export_matches_desktop(tmp_path: Path, pinned_icc: bytes) -> None:
    """Item 9: sidecar, exact files, Gate B canvases, HDR reasons, CJK, logo fallback, and the ink trap."""
    check(len(pinned_icc) == 588, "icc pin")
    pairs = mx.real_pairs(4)
    if len(pairs) < 4:
        samples.missing(f"need four DNG+JPG pairs in {mx.SAMPLE_DIR}")
    dat = _local_dat()
    _prereq()
    desk = Desktop(tmp_path, dat)
    try:
        jpg_e = mx.copy_as(pairs[0][1], tmp_path / "R9990005.JPG")
        jpg_f = mx.copy_as(pairs[1][1], tmp_path / "R9990006.JPG")
        jpg_only = mx.copy_as(pairs[2][1], tmp_path / "R9990007.JPG")
        stems = {
            "a": pairs[0][0].stem,
            "b": pairs[1][0].stem,
            "c": pairs[2][0].stem,
            "d": pairs[3][0].stem,
            "e": jpg_e.stem,
            "f": jpg_f.stem,
            "jpgOnly": jpg_only.stem,
        }
        photos: dict[str, tuple[Path | None, Path | None]] = {pairs[i][0].stem: pairs[i] for i in range(4)}
        photos[jpg_e.stem] = (None, jpg_e)
        photos[jpg_f.stem] = (None, jpg_f)
        photos[jpg_only.stem] = (None, jpg_only)
        role_of = {
            stems[key]: label
            for key, label in zip(("a", "b", "c", "d", "e", "f", "jpgOnly"), ("A", "B", "C", "D", "E", "F", "jpg"), strict=True)
        }
        order = [stems[key] for key in ("a", "b", "c", "d", "e", "f", "jpgOnly")]
        files = [str(path) for pair in pairs for path in pair]
        files += [str(jpg_e), str(jpg_f), str(jpg_only), str(dat)]
        out_dir = tmp_path / "phone"
        canvas_dir = tmp_path / "canvas"
        out_dir.mkdir()
        canvas_dir.mkdir()
        logo = _logo_png(tmp_path / "studio_logo.png")
        dehinted_fonts = dehinted_font_paths(tmp_path / "dehint")
        report = run_driver(
            tmp_path,
            {
                "mode": "parity",
                "parityDriver": str(DRIVER),
                "files": files,
                "pack": str(desk.zip),
                "count": len(order),
                "order": order,
                "stems": stems,
                "binding": desk.binding(),
                "outDir": str(out_dir),
                "canvasDir": str(canvas_dir),
                "logoPng": str(logo),
                "dehintedFonts": {name: str(path) for name, path in dehinted_fonts.items()},
                "realFonts": {name: str(font_path(name)) for name in dehinted_fonts},
            },
            timeout=3600,
        )
        check(report["pageErrors"] == [], "no page errors")
        leica = report["steps"]["leicaOn"]
        check(leica["logo"] is True and leica["params"] is True and leica["date"] is True, "leica chips on")
        shots = {stem: _shot(desk, stem, *photos[stem]) for stem in order}
        full_facts = report["steps"]["full"]["facts"]["facts"]
        side_a = full_facts[stems["a"]]["side"]
        same_redacted(
            report["steps"]["singleSizes"]["original"],
            fx.size_line(desk.render, desk.lib, shots[stems["a"]], side_a, "original"),
            "original size line",
        )
        same_redacted(
            report["steps"]["singleSizes"]["small"], fx.size_line(desk.render, desk.lib, shots[stems["a"]], side_a, "small"), "small size line"
        )
        logo_images = _plant(desk, full_facts[stems["f"]], logo)
        rows = []
        for key, _tag, _prefix, quality, edge, hdr in _JOBS:
            step = _deleted_step(report) if key == "deletedLogo" else report["steps"][key]
            if key in ("memSdr", "memHdr"):
                fx.memory_ok(step, hdr=hdr)
            for stem, fact in step["facts"]["facts"].items():
                role = role_of[stem] + _ROLE[key]
                work = tmp_path / "cmp" / role
                work.mkdir(parents=True)
                prefix = _DEHINT_PREFIX.get(key)
                dehinted = canvas_dir / f"{prefix}-{int(fact['index'])}.bin" if prefix else None
                rows.append(
                    fx.finish_one(
                        desk,
                        work,
                        shots[stem],
                        fact,
                        _phone_file(out_dir, _tag, fact),
                        _canvas(step, _prefix, int(fact["index"]), canvas_dir),
                        logo_images if _uses_logo(fact, logo_images) else None,
                        quality=quality,
                        long_edge=edge,
                        hdr=hdr,
                        role=role,
                        dehinted_canvas=dehinted,
                    )
                )
        _cjk(report["steps"]["cjkStamp"])
        _trap(report["steps"]["inkTrap"])
        note("ui_m4_frame_summary", {"rows": len(rows), "exact": sum(1 for row in rows if row["kind"] == "exact")})
        check(len(rows) == 7 + 2 + 3 + 1 + 1 + 1, "every listed export compared")
    finally:
        desk.lib.close()


def _shot(desk: Desktop, stem: str, dng: Path | None, jpg: Path | None) -> dict[str, Any]:
    """``Desktop.shot`` links the sample in. The library will not open a link, so replace it with a copy."""
    made = desk.shot(stem, dng, jpg)
    for src in (dng, jpg):
        if src is None:
            continue
        dest = desk.folder / src.name
        if dest.is_symlink():
            dest.unlink()
            shutil.copy2(src, dest)
    return made


def _uses_logo(fact: dict[str, Any], images: dict[str, Image.Image] | None) -> bool:
    if not images:
        return False
    border = fact["side"].get("border") or {}
    return border.get("logo") in images


def _plant(desk: Desktop, fact: dict[str, Any], png: Path) -> dict[str, Image.Image]:
    logos = list(fact.get("layout", {}).get("logos") or [])
    check(len(logos) == 1, "custom logo sent once")
    image, meta = fx.plant_logo(desk.render.logos, str(logos[0]["id"]), png)
    fx.logo_stats_ok(logos[0], meta)
    check(fact["side"]["border"]["logo"] == logos[0]["id"], "sidecar logo id")
    return {str(logos[0]["id"]): image}


def _cjk(step: dict[str, Any]) -> None:
    check(step["err"] == "无法显示中文。请改用英文或数字。", "cjk line")
    check(_no_cjk(step["inputValue"]), "cjk cleared from the field")
    stamp = step["sideStamp"] or {}
    check(_no_cjk(stamp.get("text", "")), "cjk not stored")


def _trap(step: dict[str, Any]) -> None:
    first, retry = step["first"], step["retry"]
    check(len(first["failures"]) == 1 and first["failures"][0]["kind"] == "trap", "ink trap kind")
    check("内存不够，这张没做成。可以重试，或改用小一点的尺寸。" in (first["dock"] or ""), "memory line")
    check(first["workers"] == 2 and first["staged"] == 1, "next photo after the trap")
    check(retry["failures"] == [] and retry["staged"] == 2, "retry after the trap")
