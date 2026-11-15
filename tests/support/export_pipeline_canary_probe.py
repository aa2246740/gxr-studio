"""Child-pytest probes for the HDR privacy paths (Sol r2 P1): HDR Gate A, HDR Gate B and the HDR
state comparison, each forced to fail on a synthetic EXIF canary through the real helpers of
``test_core_parity_export_pipeline_hdr``; plus pixel arrays with distinctive values (Sol r4) and
the helpers' fixed vocabularies (an unknown field name, error kind and structured dtype field);
plus Sol r5's slips: every helper given a label made from the photo's LensModel. Collected only
when a parent test runs this file explicitly (``-o python_files=*.py``); every test here is meant to fail, without the canary
reaching stdout, stderr or JUnit. Construction after Sol's r2 probe: a 128×128 gradient, a valid
float32 layer and a JPEG payload carrying the canary in Make and LensModel."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from support.export_pipeline_canary import CANARY, _synthetic
from support.export_pipeline_cases import check, cli_ok, produced, same_file_redacted, same_kind, same_pixels, same_redacted

# Values that must not reach the output (besides CANARY): float32 pixel digits.
SECRETS = ("7071067", "3183098")


def _hdr_files() -> tuple[bytes, dict[str, Any]]:
    import test_core_parity_export_pipeline_hdr as target
    from gxr_studio.hdr import container as hc
    from gxr_studio.hdr import gain as hg
    from support import hdr_parity as hp

    rgb = np.repeat(np.tile(np.linspace(30, 230, 128, dtype=np.uint8), (128, 1))[:, :, None], 3, axis=2)
    layer = np.zeros((128, 128, 2), np.float32)
    layer[:, :, 0] = np.linspace(0.01, 4.0, 128, dtype=np.float32)[None, :]
    exif = Image.Exif()
    exif[271] = CANARY
    exif.get_ifd(0x8769)[42036] = CANARY
    stream = io.BytesIO()
    Image.fromarray(rgb).save(stream, "JPEG", exif=exif)
    payload = stream.getvalue()
    ry, rc = target._oracle_to_grid(layer, (128, 128), 4, use_fma=False)
    result = hg.compute_gain(hg.block_mean(hp._ORACLE[0][rgb] @ hg.LUMA, 4), ry, rc, hg.Params())
    gain = target._encode_gain_jpeg(hp.codes_of(result), 90)
    expected = hc.assemble(payload, gain, result.meta)
    check(CANARY.encode() in expected, "probe setup: the canary is not in the HDR file")
    return expected, {"rgb": rgb, "layer": layer, "payload": payload}


def test_hdr_gate_a_mismatch() -> None:
    import test_core_parity_export_pipeline_hdr as target

    expected, fin = _hdr_files()
    target._attribute(expected.replace(CANARY.encode(), b"X" * len(CANARY)), expected, fin, "SYNTHETIC HDR")


def test_hdr_gate_b_mismatch() -> None:
    import test_core_parity_export_pipeline_hdr as target

    expected, fin = _hdr_files()
    # Gate A holds (Rust equals the sequential oracle); the "desktop" file differs.
    target._attribute(expected, expected.replace(CANARY.encode(), b"X" * len(CANARY)), fin, "SYNTHETIC HDR")


def test_hdr_state_mismatch() -> None:
    state = {"asked": True, "active": False, "shape": None, "problem": CANARY, "frame": {"items": [{"k": "text", "text": CANARY}]}}
    same_redacted(state, {**state, "problem": "no_raw"}, "SYNTHETIC hdr state")


def test_pixels_mismatch() -> None:
    want = np.full((6, 4, 3), 0.31830987, np.float32)
    got = want.copy()
    got[2, 1, 0] = 0.70710677
    same_pixels(got, want, "SYNTHETIC pixels")


def test_unknown_field_name_is_hashed() -> None:
    same_redacted({"meta": {CANARY: "a"}}, {"meta": {CANARY: "b"}}, "SYNTHETIC field")


def test_unknown_error_kind_is_hashed() -> None:
    same_kind({"error": {"kind": CANARY, "message": CANARY}}, "json", "SYNTHETIC kind")


def test_structured_dtype_field_is_hashed() -> None:
    dtype = np.dtype([(CANARY, np.uint8)])
    same_pixels(np.zeros(2, dtype), np.zeros(3, dtype), "SYNTHETIC dtype")


# Sol r5 P1-01 / P2-01: ordinary slips that build a helper's label from photo data (here the
# LensModel of a synthetic JPEG, which is the canary). The label is printed only as a hash.
@pytest.fixture
def photo(tmp_path: Path) -> str:
    with Image.open(_synthetic(tmp_path)) as image:
        return str(image.getexif().get_ifd(0x8769)[42036])


def test_slip_metadata_label(photo: str) -> None:
    same_redacted({"lens": photo}, {"lens": "expected"}, f"{photo} metadata")


def test_slip_file_label(photo: str) -> None:
    same_file_redacted(photo.encode(), b"expected", f"{photo} export")


def test_slip_pixels_label(photo: str) -> None:
    same_pixels(np.zeros(1, np.uint8), np.ones(1, np.uint8), f"{photo} canvas")


def test_slip_cli_label(photo: str) -> None:
    cli_ok(2, {"error": {"kind": "bad_input", "message": photo}}, f"{photo} prepare")


def test_slip_missing_output_label(photo: str) -> None:
    produced(None, {"error": {"kind": "bad_input"}}, f"{photo} complete")


def test_slip_error_kind_label(photo: str) -> None:
    same_kind({"error": {"kind": "bad_input"}}, "json", f"{photo} kind")


def test_slip_check_keyword_label(photo: str) -> None:
    # The lint flags the next line (E_LABEL, allowed here by its marker); the hash still holds.
    check(ok=False, label=f"{photo} metadata")  # canary slip
