"""Two desktop render quirks that the phone port copies on purpose (unit desktop-quirks, 2026-11-16).

Both are marked ``xfail(strict=True)``: each test states what the export should do, fails today,
and turns into an XPASS failure once the fix lands, so the marker has to come off with the fix.
``finish/serve.py``, ``core/crates/gxr-export/src/source/select.rs`` and
``test_core_parity_export_source.py::test_neutral_base_renders_stock_04_when_calibrated`` change
together, or the parity tests break.

- Quirk 1: base ``neutral`` with camera data and calibration renders the unit's stock 04 (computer
  prediction) but labels it 中性转换. The export API's ``source: "neutral"`` is documented as a
  neutral conversion with no recipe (``api/routes.py`` ``ExportRequest.source``).
- Quirk 2: ``standard`` (auto, DNG only) falls back to the neutral renderer when there is no camera
  data or calibration, but still reports ``from_raw=True``, so the finish applies only the four
  extra adjustments and silently drops exposure, white balance, tone and curves.

Synthetic DNGs only. Quirk 1 needs the parent project's camera data (``GXR_STUDIO_TEST_PARENT``)
and a synthetic calibration pack; nothing here prints camera or calibration data.
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
from support.dng_writer import write_dng

LENS = "GR LENS A12 50mm F2.5 MACRO"
QUIRK_1 = (
    "desktop quirk 1: base 'neutral' renders stock 04 when calibrated but labels it neutral "
    "(serve.color_image -> _raw_rgb(EditState()) -> RenderService.renderer plans stock 04)"
)
QUIRK_2 = (
    "desktop quirk 2: 'standard' falls back to NEUTRAL with from_raw=True, so exposure, white balance, "
    "tone and curves are dropped (serve.color_image returns True; compose.finish_unframed runs apply_extra only)"
)


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


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=QUIRK_1)
def test_neutral_base_is_neutral_with_calibration(tmp_path: Path) -> None:
    dat = xs.real_dat()
    if dat is None:
        pytest.skip("相机数据夹具（g127 DAT）不在本机")
    h = xs.make_home(tmp_path / "all", dat)
    shot = _dng_shot(tmp_path)
    rgb, _from_raw, labels = serve.color_image(h.render, shot, EditState(), max_edge=None, quality=95, lib=h.lib, base="neutral", full=True)
    assert labels["text"] == "中性转换" and labels["tag"] == "_neutral"
    assert labels["calibrated"] == "0", "a neutral conversion is not a calibrated prediction"
    assert labels["description"].startswith("GXR Studio neutral-preview"), "EXIF 270 must not describe a stock 04 prediction"
    np.testing.assert_array_equal(rgb, _neutral(shot), err_msg="pixels must be the neutral conversion, not stock 04")


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=QUIRK_2)
def test_standard_neutral_fallback_keeps_adjustments(tmp_path: Path) -> None:
    h = xs.make_home(tmp_path / "nodat", None)
    shot = _dng_shot(tmp_path)

    def final(edit: EditState) -> tuple[np.ndarray, dict[str, str]]:
        return serve.render_stage_rgb(h.render, h.lib, shot, edit, {}, stage="final", max_edge=None, quality=95)

    plain, labels = final(EditState())
    assert labels["id"] == "neutral-preview", labels
    brighter, _ = final(EditState(adjust=Adjust(exposure_ev=1.0)))
    assert brighter.mean() > plain.mean() + 1.0, "exposure +1 EV must brighten the exported neutral conversion"
