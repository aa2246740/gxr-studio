"""Helpers for ``tests/test_core_parity_export_source.py``.

A desktop home is built in the test's temporary folder: the camera data imported from a DAT
the parent project already has (``GXR_STUDIO_TEST_PARENT``; never copied into the repo) and a
**synthetic** calibration pack made by ``calib.import_calibration`` from generated arrays
(``support.color_fixtures``). The real ``RenderService`` and ``finish.serve.color_image`` run on
it; ``gxr-cli source-*`` gets the same files. Nothing here prints calibration contents.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

import e0_support as e0
from gxr_studio.camdata import calib, datcodec, store
from gxr_studio.finish.compose import load_jpeg
from gxr_studio.raw.dng import RawDecodeError
from gxr_studio.render.params import Adjust, Curves, EditError, EditState, RecipeSel
from gxr_studio.render.service import NoRaw, RenderService, RenderUnavailable
from support import color_fixtures as cf

PARENT = Path(os.environ.get("GXR_STUDIO_TEST_PARENT", str(Path(__file__).resolve().parents[2]))).resolve()
CAPTURES = PARENT / "captures"


def real_dat() -> bytes | None:
    """The g127 DAT of the parent project, if this machine has it (sha256-checked)."""
    return e0.fixture_dat(127)


def synthetic_sources(home: Path, units: tuple[str, ...]) -> dict[str, Any]:
    """`import_calibration` sources for `units`: generated d0/d1/coefficients and rings."""
    sources: dict[str, Any] = {}
    for n, unit in enumerate(units):
        words = calib.stock_words_of_entry(store.load_stock04(home, unit))
        per: dict[int, Any] = {}
        for iso, rows in ((200, 4), (1600, 10)):
            d0, d1, coef = cf.arrays(rows, seed=iso + 7 * n)
            ring = json.dumps(cf.ring(kernel="hard" if iso == 1600 else "linear")).encode()
            per[iso] = ((f"src{unit}{iso}.npz", cf.source_npz(words, d0, d1, coef)), (f"ring{unit}{iso}.json", ring))
        sources[unit] = per
    return sources


@dataclass
class Home:
    home: Path
    dat: Path | None
    render: RenderService
    lib: Any

    @property
    def calib(self) -> Path:
        return calib.calib_dir(self.home)

    def binding(self) -> str:
        self.render._refresh()
        return self.render.engine_pin()

    def entry(self, sel: RecipeSel) -> bytes | None:
        """`CameraData.resolve(sel).entry`, the resolve unit's output for this selection."""
        self.render._refresh()
        cam = self.render._camera
        return None if cam is None else cam.resolve(sel).entry


def make_home(tmp: Path, dat: bytes | None, *, units: tuple[str, ...] | None = datcodec.NAMES, real_calib: bool = False) -> Home:
    """A home with the DAT imported (or none) and a calibration pack for `units` (or none)."""
    home = tmp / "home"
    home.mkdir(parents=True, exist_ok=True)
    dat_path = None
    if dat is not None:
        store.import_from_dat(dat, home)
        dat_path = tmp / "GXRCE.DAT"
        dat_path.write_bytes(dat)
        if real_calib:
            calib.import_calibration(home, calib.dev_sources(e0.REPO))
        elif units:
            calib.import_calibration(home, synthetic_sources(home, units))
    lib = SimpleNamespace(root=tmp / "library", abspath=lambda rel: Path(rel))
    return Home(home, dat_path, RenderService(home, lib), lib)


def shot_for(dng: Path | None, jpg: Path | None, *, lens: str | None, iso: int | None) -> dict[str, Any]:
    files: dict[str, Any] = {}
    if dng is not None:
        files["dng"] = {"relpath": str(dng)}
    if jpg is not None:
        files["jpg"] = {"relpath": str(jpg)}
    return {"files": files, "lens": lens, "iso": iso}


def edit_json(edit: EditState) -> dict[str, Any]:
    """The edit as `source-*` reads it: `RecipeSel.to_json()`, the LUT id, and the tone fields."""
    a = edit.adjust
    adjust = {k: getattr(a, k) for k in ("exposure_ev", "warm", "tint", "contrast", "highlight", "shadow")}
    curves = None
    if edit.curve is not None:
        curves = {k: (None if getattr(edit.curve, k) is None else [list(p) for p in getattr(edit.curve, k)]) for k in ("master", "r", "g", "b")}
    return {"recipe": edit.recipe.to_json() if edit.recipe else None, "lut": edit.lut, "adjust": adjust, "curves": curves}


def adjust_json(adjust: Adjust, curves: Curves | None) -> dict[str, Any]:
    e = edit_json(EditState(adjust=adjust, curve=curves))
    return {"adjust": e["adjust"], "curves": e["curves"]}


def write_json(path: Path, value: Any) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def python_error(exc: BaseException, has_jpg: bool) -> dict[str, Any]:
    """What `source-color-image` reports for a Python exception from `color_image`.

    `RawDecodeError` gets `export_finish`'s mapping (serve.py:446-450)."""
    if isinstance(exc, RawDecodeError):
        detail = f"没有全尺寸传感器数据，也没有更小的预览。{exc}"
        if has_jpg:
            return {"kind": "raw_unreadable", "message": "无法读取 RAW 文件。文件可能已损坏。", "detail": detail}
        return {"kind": "no_jpg_source", "message": "无法读取 RAW 文件。这张照片没有相机 JPG。", "detail": detail}
    if isinstance(exc, RenderUnavailable):
        return {"kind": exc.code, "message": exc.message, "state": exc.state}
    if isinstance(exc, NoRaw):
        return {"kind": "no_raw", "message": str(exc)}
    if isinstance(exc, EditError):
        return {"kind": exc.code, "message": exc.message, "detail": exc.detail}
    raise exc


def save_rgb(path: Path, rgb: np.ndarray | None) -> Path | None:
    if rgb is None:
        return None
    np.save(path, np.ascontiguousarray(rgb, np.uint8))
    return path


def decoded_jpg(path: Path) -> np.ndarray:
    """`load_jpeg(path, None)`: what the browser's `createImageBitmap` gives the phone."""
    return load_jpeg(path, None)


def real_pairs(cap: int) -> list[tuple[Path, Path | None]]:
    """DNGs under captures/ (no backups), spread by sorted path, each with its JPG if present."""
    if not CAPTURES.is_dir():
        return []
    found = sorted(
        (p for p in CAPTURES.rglob("*") if p.is_file() and p.suffix.upper() == ".DNG" and "backup" not in p.as_posix().lower()),
        key=lambda p: p.as_posix(),
    )
    picked = found if len(found) <= cap else [found[(i * len(found)) // cap] for i in range(cap)]
    out = []
    for p in picked:
        jpg = next((q for q in (p.with_suffix(".JPG"), p.with_suffix(".jpg")) if q.is_file()), None)
        out.append((p, jpg))
    return out
