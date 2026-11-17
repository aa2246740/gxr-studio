"""UI-M4 frame item 9: framed exports in the browser, compared with the desktop.

The sample folder has four DNG+JPG pairs. Styles E and F are full copies of two of those pairs
under synthetic names; the JPG-only photo copies just the JPEG. Two more pairs are portrait:
the first portrait DNG+JPG files under the captures root, in sorted path order, or an orientation
rewrite when none can be read. Comparisons go through the redacted helpers. The driver report
stays in the test's temporary directory. Nothing here prints a photo name, a capture time, or EXIF.
"""

from __future__ import annotations

import shutil
import struct
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
    ("portraitFrame", "pframe-", "m4_pframe", 95, None, False),
    ("portraitInstant", "pinst-", "m4_pinst", 95, None, False),
)
_ROLE = {
    "full": "",
    "small": "-small",
    "hdr": "-hdr",
    "memSdr": "-mem",
    "memHdr": "-mem-hdr",
    "deletedLogo": "-deleted",
    "portraitFrame": "",
    "portraitInstant": "",
}
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


def _display_size(path: Path) -> tuple[int, int] | None:
    """Upright JPEG size. Failures stay empty: the path and EXIF are not reported."""
    try:
        with Image.open(path) as im:
            orient = int(im.getexif().get(274, 1) or 1)
            width, height = im.size
    except Exception:
        return None
    if orient in (5, 6, 7, 8):
        return height, width
    return width, height


def _portrait_pairs(limit: int) -> list[tuple[Path, Path]]:
    """First portrait DNG+JPG pairs under captures, in sorted path order."""
    root = samples.captures()
    if not root.is_dir():
        return []
    found: list[tuple[Path, Path]] = []
    dngs = sorted(path for path in root.rglob("*") if path.is_file() and path.suffix.upper() == ".DNG")
    for dng in dngs:
        jpg = next((dng.with_suffix(ext) for ext in (".JPG", ".jpg") if dng.with_suffix(ext).is_file()), None)
        if jpg is None:
            continue
        size = _display_size(jpg)
        if size is not None and size[1] > size[0]:
            found.append((dng, jpg))
        if len(found) >= limit:
            break
    return found


def _copy_pair(pair: tuple[Path, Path], folder: Path, stem: str) -> tuple[Path, Path]:
    dng = mx.copy_as(pair[0], folder / f"{stem}.DNG")
    jpg = mx.copy_as(pair[1], folder / f"{stem}.JPG")
    return dng, jpg


def _patch_tiff_orientation(path: Path, value: int) -> bool:
    """Rewrite IFD0 Orientation in place. Returns false when the tag is absent."""
    with path.open("r+b") as handle:
        header = handle.read(8)
        if len(header) < 8 or header[:2] not in (b"II", b"MM"):
            return False
        end = "<" if header[:2] == b"II" else ">"
        if struct.unpack(end + "H", header[2:4])[0] != 42:
            return False
        ifd = struct.unpack(end + "I", header[4:8])[0]
        handle.seek(ifd)
        count_raw = handle.read(2)
        if len(count_raw) < 2:
            return False
        for _ in range(min(struct.unpack(end + "H", count_raw)[0], 80)):
            entry = handle.read(12)
            if len(entry) < 12:
                return False
            tag, kind = struct.unpack(end + "HH", entry[:4])
            if tag == 0x0112 and kind == 3:
                handle.seek(-4, 1)
                handle.write(struct.pack(end + "HH", value, 0))
                return True
    return False


def _rewritten_portrait(pair: tuple[Path, Path], folder: Path, stem: str) -> tuple[Path, Path] | None:
    dng = mx.copy_as(pair[0], folder / f"{stem}.DNG")
    jpg = folder / f"{stem}.JPG"
    if not mx.set_orientation(pair[1], jpg, 6):
        return None
    _patch_tiff_orientation(dng, 6)
    size = _display_size(jpg)
    if size is None or size[1] <= size[0]:
        return None
    return dng, jpg


def _portrait_copies(pairs: list[tuple[Path, Path]], folder: Path) -> list[tuple[Path, Path]]:
    real = _portrait_pairs(2)
    if len(real) >= 2:
        return [_copy_pair(real[0], folder, "R9990008"), _copy_pair(real[1], folder, "R9990009")]
    made: list[tuple[Path, Path]] = []
    for stem, pair in zip(("R9990008", "R9990009"), pairs[:2], strict=True):
        rewritten = _rewritten_portrait(pair, folder, stem)
        check(rewritten is not None, "portrait target")
        if rewritten is not None:
            made.append(rewritten)
    return made


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
        dng_e, jpg_e = _copy_pair(pairs[0], tmp_path, "R9990005")
        dng_f, jpg_f = _copy_pair(pairs[1], tmp_path, "R9990006")
        jpg_only = mx.copy_as(pairs[2][1], tmp_path / "R9990007.JPG")
        portraits = _portrait_copies(pairs, tmp_path)
        check(len(portraits) == 2, "two portrait pairs")
        for _dng, jpg in portraits:
            size = _display_size(jpg)
            check(size is not None and size[1] > size[0], "portrait target before export")
        stems = {
            "a": pairs[0][0].stem,
            "b": pairs[1][0].stem,
            "c": pairs[2][0].stem,
            "d": pairs[3][0].stem,
            "e": jpg_e.stem,
            "f": jpg_f.stem,
            "jpgOnly": jpg_only.stem,
            "pFrame": portraits[0][0].stem,
            "pInstant": portraits[1][0].stem,
        }
        photos: dict[str, tuple[Path | None, Path | None]] = {pairs[i][0].stem: pairs[i] for i in range(4)}
        photos[jpg_e.stem] = (dng_e, jpg_e)
        photos[jpg_f.stem] = (dng_f, jpg_f)
        photos[jpg_only.stem] = (None, jpg_only)
        photos[portraits[0][0].stem] = portraits[0]
        photos[portraits[1][0].stem] = portraits[1]
        labels = ("A", "B", "C", "D", "E", "F", "jpg", "P-frame", "P-instant")
        keys = ("a", "b", "c", "d", "e", "f", "jpgOnly", "pFrame", "pInstant")
        role_of = {stems[key]: label for key, label in zip(keys, labels, strict=True)}
        order = [stems[key] for key in ("a", "b", "c", "d", "e", "f", "jpgOnly")]
        files = [str(path) for pair in pairs for path in pair]
        files += [str(dng_e), str(jpg_e), str(dng_f), str(jpg_f), str(jpg_only), str(dat)]
        files += [str(path) for pair in portraits for path in pair]
        out_dir = tmp_path / "phone"
        canvas_dir = tmp_path / "canvas"
        out_dir.mkdir()
        canvas_dir.mkdir()
        logo = _logo_png(tmp_path / "studio_logo.png")
        dehint_dir = tmp_path / "dehint"
        dehint_dir.mkdir()
        dehinted_fonts = dehinted_font_paths(dehint_dir)
        fonts = fx.Fonts(real={name: font_path(name) for name in dehinted_fonts}, dehint=dehinted_fonts)
        report = run_driver(
            tmp_path,
            {
                "mode": "parity",
                "parityDriver": str(DRIVER),
                "files": files,
                "pack": str(desk.zip),
                "count": len(order) + len(portraits),
                "order": order,
                "stems": stems,
                "binding": desk.binding(),
                "outDir": str(out_dir),
                "canvasDir": str(canvas_dir),
                "logoPng": str(logo),
                "dehintedFonts": {name: str(path) for name, path in fonts.dehint.items()},
                "realFonts": {name: str(path) for name, path in fonts.real.items()},
            },
            timeout=3600,
        )
        check(report["pageErrors"] == [], "no page errors")
        leica = report["steps"]["leicaOn"]
        check(leica["logo"] is True and leica["params"] is True and leica["date"] is True, "leica chips on")
        shots = {stem: _shot(desk, stem, *photos[stem]) for stem in (*order, stems["pFrame"], stems["pInstant"])}
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
                        fonts=fonts,
                        dehinted_canvas=dehinted,
                    )
                )
        _cjk(report["steps"]["cjkStamp"])
        _trap(report["steps"]["inkTrap"])
        note("ui_m4_frame_summary", {"rows": len(rows), "exact": sum(1 for row in rows if row["kind"] == "exact")})
        check(len(rows) == 7 + 2 + 3 + 1 + 1 + 1 + 2, "every listed export compared")
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
