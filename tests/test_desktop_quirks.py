"""Two desktop render bugs the phone port used to copy (unit desktop-quirks, fixed 2026-11-16).

- Base ``neutral`` with camera data and calibration rendered the unit's stock 04 (computer
  prediction) under the 中性转换 label, with ``calibrated=1`` and a stock-04 EXIF description. The
  export API's ``source: "neutral"`` is a neutral conversion with no recipe (``api/routes.py``
  ``ExportRequest.source``). It now always renders ``NEUTRAL``.
- ``standard`` (auto, DNG only) falls back to ``NEUTRAL`` when there is no camera data or
  calibration, but reported ``from_raw=True``, so the finish applied only the four extra
  adjustments and silently dropped exposure, white balance, tone and curves. ``from_raw`` is now
  false whenever the picture is neutral, and the finish applies every adjustment as for a JPG.

``core/crates/gxr-export/src/source/select.rs`` follows the same rule; the export-source parity
tests compare both. Synthetic DNGs only. The neutral-base test needs the parent project's camera
data (``GXR_STUDIO_TEST_PARENT``) and a synthetic calibration pack; nothing here prints camera or
calibration data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from gxr_studio.finish import serve
from gxr_studio.raw import NEUTRAL, open_dng, render_full
from gxr_studio.render.params import Adjust, EditState
from support import export_source_cases as xs
from support import samples
from support.dng_writer import write_dng

LENS = "GR LENS A12 50mm F2.5 MACRO"


def _dng_shot(tmp_path: Path) -> dict[str, Any]:
    rng = np.random.default_rng(1)
    path = tmp_path / "R0000001.DNG"
    path.write_bytes(write_dng(rng.integers(200, 3800, (48, 64), dtype=np.uint16), white=4095, orientation=6))
    shot = xs.shot_for(path, None, lens=LENS, iso=200)
    shot.update(name=path.stem, shot_id="q", width=64, height=48)
    shot["files"]["dng"]["sha256"] = "0" * 64
    return shot


def _neutral(shot: dict[str, Any]) -> np.ndarray:
    dng = open_dng(Path(shot["files"]["dng"]["relpath"]))
    try:
        return render_full(dng, NEUTRAL)
    finally:
        dng.release()


def test_neutral_base_is_neutral_with_calibration(tmp_path: Path) -> None:
    dat = xs.real_dat()
    samples.need_sample(dat is not None, "相机数据夹具（g127 DAT）不在本机")
    if dat is None:
        pytest.skip("相机数据夹具（g127 DAT）不在本机")
    h = xs.make_home(tmp_path / "all", dat)
    shot = _dng_shot(tmp_path)
    rgb, from_raw, labels = serve.color_image(h.render, shot, EditState(), max_edge=None, quality=95, lib=h.lib, base="neutral", full=True)
    assert labels["text"] == "中性转换" and labels["tag"] == "_neutral"
    assert from_raw is False, "the neutral picture has no adjustment baked in"
    assert labels["calibrated"] == "0", "a neutral conversion is not a calibrated prediction"
    assert labels["description"].startswith("GXR Studio neutral-preview"), "EXIF 270 must not describe a stock 04 prediction"
    np.testing.assert_array_equal(rgb, _neutral(shot), err_msg="pixels must be the neutral conversion, not stock 04")


def test_standard_neutral_fallback_keeps_adjustments(tmp_path: Path) -> None:
    h = xs.make_home(tmp_path / "nodat", None)
    shot = _dng_shot(tmp_path)

    def final(edit: EditState, base: str) -> tuple[np.ndarray, dict[str, str]]:
        return serve.render_stage_rgb(h.render, h.lib, shot, edit, {}, stage="final", max_edge=None, quality=95, base=base)

    for base in ("auto", "neutral"):
        plain, labels = final(EditState(), base)
        assert labels["id"] == "neutral-preview", (base, labels)
        brighter, _ = final(EditState(adjust=Adjust(exposure_ev=1.0)), base)
        assert brighter.mean() > plain.mean() + 1.0, f"{base}: exposure +1 EV must brighten the exported neutral conversion"
