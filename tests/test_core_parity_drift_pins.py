"""Pinned regressions for unit parity-drift-1 (2026-11-18).

New field captures shifted the old spread-by-sorted-path samples onto three existing real pairs
where the desktop and the phone core differ. Each difference comes from macOS libm not rounding
correctly where ``gxr_math`` does (PORT-WAVE-1 platform libm rule), so each is attributed, not
fixed. The pairs are found by the SHA-256 of their path under captures/ and checked by the
SHA-256 of the DNG; no photo name is in the repo or the output.

- HDR track layer, ``tan``: the vertical-15 perspective slider (``geometry._slider_s``). macOS
  ``tan`` is 1 ulp high there, so the desktop's sample position at one layer element lands exactly
  on a float32 rounding tie and the bilinear value moves 1 ulp. The export file is unchanged.
- Neutral RAW base, ``powf``: input ``0x3eadd76b`` in ``srgb_encode`` (rawrender's known case):
  red 157 on the desktop, 158 in Rust at three pixels. Two survive the crop; after a saturation
  edit the canvas differs by 2 codes at one of them (157 vs 159) and by 1 code at the other
  (157 vs 158). The 1600-pixel resize averages them away, so the file is byte-identical.
- HDR gain from Rust's own SDR grid, ``powf`` at sRGB codes 71 and 180 (``POW_CODES``): the grid
  moves one tone-curve bin median, ``delta`` moves by float noise, and 8 gain codes cross a ``.5``
  boundary, each by exactly 1 code.
"""

from __future__ import annotations

import json
import math
import struct
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from support import hdr_parity as hp
from support import host_libm as hl
from support import samples
from support.core_cli import require_cli, run_npy
from test_core_parity_export_pipeline_raw import pinned_icc  # noqa: F401

TAN_PAIR = ("0536bd27697a6b54", "5b52663a15f669a0")
POWF_PAIR = ("78f104f493ee7d12", "fafb8232dec2e83a")
GAIN_PAIR = ("145633d1e7029726", "680c5e8211d4a769")

# The vertical-15 slider's tan argument, macOS's result and the correctly rounded one (float64 bits).
TAN_X, TAN_HOST, TAN_CR = 0x3FB41B2F769CF0DF, 0x3FB425CB88F37C5A, 0x3FB425CB88F37C59
VERTICAL_15 = {"straighten": 0.0, "vertical": 15, "horizontal": 0, "distortion": 0, "rotate90": 0, "flip": False}


@pytest.fixture(autouse=True)
def _cli() -> None:
    require_cli()


def _bits(x: float) -> int:
    return int(struct.unpack(">Q", struct.pack(">d", x))[0])


def _pair(pin: tuple[str, str]) -> tuple[Path, Path]:
    dng = samples.pinned_capture(*pin)
    jpg = dng.with_suffix(".JPG")
    if not jpg.is_file():
        samples.missing(f"pinned capture {pin[0]} has no JPG")
    return dng, jpg


def test_vertical_15_tan_is_correctly_rounded_in_rust(tmp_path: Path) -> None:
    """The ``tan`` attribution without a photo: on the HDR layer's size (4288×2848) and vertical 15,
    Rust's ``warp_float`` equals the desktop's ``warp_float`` run with a correctly rounded ``tan``
    bit for bit (Gate A), and differs from the desktop's own at pixel (2699, 3411) only, both
    channels, where macOS's sample position sits on a float32 tie (negative control)."""
    from gxr_studio.finish.geometry import warp_float
    from gxr_studio.finish.spec import Geometry

    x = (VERTICAL_15["vertical"] / 100.0) * math.radians(30.0)
    assert _bits(x) == TAN_X
    assert _bits(hl.cr_tan(x)) == TAN_CR
    if sys.platform == "darwin":
        assert _bits(math.tan(x)) == TAN_HOST
    yy, xx = np.mgrid[0:2848, 0:4288]
    layer = np.dstack([((xx + yy) % 2).astype(np.float32), (xx % 3).astype(np.float32) / 3]).astype(np.float32)
    np.save(tmp_path / "layer.npy", layer)
    geom = Geometry(**VERTICAL_15)
    rust = run_npy(
        ["hdrtrack-warp-float", "--in", str(tmp_path / "layer.npy"), "--geometry-json", json.dumps(VERTICAL_15)], tmp_path / "rs.npy"
    )
    with hl.geometry_tan(hl.cr_tan):
        oracle = warp_float(layer, geom)
    assert rust.shape == oracle.shape
    assert not hl._bits_differ(rust, oracle).any()
    if sys.platform == "darwin":
        host = warp_float(layer, geom)
        assert np.argwhere(hl._bits_differ(rust, host)).tolist() == [[2699, 3411, 0], [2699, 3411, 1]]


def test_pinned_hdr_layer_host_tan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned_icc: bytes) -> None:  # noqa: F811
    """The HDR export pair that broke ``test_hdr_real_pairs_match_export_finish`` on e85430f
    (``layer before stamp: 1 of 23567314 differ; first (2680, 3319, 0)``), with that test's
    case 9 (Leica frame, vertical 15). The one value is attributed to macOS ``tan``; the file is
    byte-identical."""
    from support import export_source_cases as xs
    from support.frame_cases import FIXTURE_CJK, cjk_fonts
    from test_core_parity_export_pipeline_hdr import CASES, DAT, run_hdr_case

    if DAT is None:
        samples.missing("没有 g127 DAT")
    dng, jpg = _pair(TAN_PAIR)
    h = xs.make_home(tmp_path / "home", DAT)
    with cjk_fonts((FIXTURE_CJK,)):
        out = run_hdr_case(h, tmp_path, monkeypatch, "PINTAN", dng, jpg, CASES[9], None)
    want: dict[str, Any] = {
        "values": 1,
        "unattributed": 0,
        "first": [2680, 3319, 0],
        "max_ulp": 1,
        "host_replay_vs_rust": 1,
        "tan_calls": 8,
        "tan_off": 2,
        "host_calls": 8,
    }
    assert out["layer_libm"] == want
    assert (out["hdr"], out["how"]) == (True, "equal")


def test_pinned_raw_neutral_base_host_powf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned_icc: bytes) -> None:  # noqa: F811
    """The RAW export pair that broke ``test_raw_branches_real_pairs_match_export_finish`` on
    e85430f (``2 values differ, first at (1411, 3225, 0)``), with that test's ``RAW_CASES[1]``
    (neutral base, crop, saturation 15, long edge 1600). Three base values come from macOS
    ``powf`` at one input; two survive the crop as canvas differences of 2 codes and 1 code; the canvas equals
    the desktop's own steps on Rust's base, and the file equals the desktop's byte for byte."""
    from support import export_source_cases as xs
    from support.frame_cases import FIXTURE_CJK, cjk_fonts
    from test_core_parity_export_pipeline_raw import DAT, RAW_CASES, run_case

    if DAT is None:
        samples.missing("没有 g127 DAT")
    dng, jpg = _pair(POWF_PAIR)
    h = xs.make_home(tmp_path / "home", DAT)
    with cjk_fonts((FIXTURE_CJK,)):
        out = run_case(h, tmp_path, monkeypatch, "PINPOWF", dng, jpg, RAW_CASES[1])
    assert out["libm"] == {"base_values": 3, "powf_inputs": 1, "canvas_values": 2, "canvas_max_code": 2, "file_differs": 0}
    assert (out["render"], out["no_ink"]) == ("neutral-preview", True)


def test_pinned_gain_codes_host_powf(tmp_path: Path) -> None:
    """The HDR pair that broke ``test_real_pair_flow`` on e85430f (``uint8 gain codes from Rust's
    grid differ: 8``). Rust's codes from its own grid equal Python's on that grid (Gate A); the
    8 codes that differ from real Python each move 1 code across a ``.5`` boundary."""
    dng, jpg = _pair(GAIN_PAIR)
    result = hp.flow(tmp_path, jpg.read_bytes(), dng, "PINGAIN")
    assert "sdr_luma_grid:2819" in result.stages
    assert "e2e-gain-codes:8" in result.stages
    points = [s.removeprefix("e2e-gain-point:") for s in result.stages if s.startswith("e2e-gain-point:")]
    assert points == [
        "(10,363) 229->228 228.50001525878906/228.49998474121094",
        "(16,309) 7->6 6.500026226043701/6.499992370605469",
        "(108,854) 90->89 89.50003051757812/89.49999237060547",
        "(152,339) 206->205 205.50001525878906/205.49996948242188",
        "(155,471) 88->87 87.5000228881836/87.49998474121094",
        "(164,470) 134->133 133.50001525878906/133.49998474121094",
        "(371,1014) 131->130 130.50001525878906/130.49998474121094",
        "(406,883) 245->244 244.5/244.49996948242188",
    ]


def test_gain_attribution_rejects_an_unrelated_grid_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative control (Sol r1 P1): one desktop grid cell that the host ``powf`` does not touch,
    (197, 749), moved from ``0x3ef97c70`` to ``0x3ef98070``. That moves a 9th gain code by 1 at a
    ``.5`` boundary, which Gate A and the 1-code cap alone would attribute; the host-libm replay of
    the desktop grid in ``check_sdr`` must reject it instead."""
    from gxr_studio.hdr import gain as g

    dng, jpg = _pair(GAIN_PAIR)
    real = g.sdr_luma_grid

    def mutated(rgb: np.ndarray, factor: int) -> np.ndarray:
        grid = real(rgb, factor)
        assert int(grid[197, 749].view(np.uint32)) == 0x3EF97C70
        grid[197, 749] = np.array(0x3EF98070, np.uint32).view(np.float32)
        return grid

    monkeypatch.setattr(g, "sdr_luma_grid", mutated)
    with pytest.raises(
        pytest.fail.Exception, match=r"^PINMUT sdr_luma_grid: host-libm replay vs desktop: 1 of 763264 differ; first \(197, 749\)"
    ):
        hp.flow(tmp_path, jpg.read_bytes(), dng, "PINMUT")
