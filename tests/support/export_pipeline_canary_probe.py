"""Child-pytest probes for the HDR privacy paths (Sol r2 P1): HDR Gate A, HDR Gate B and the HDR
state comparison, each forced to fail on a synthetic EXIF canary through the real helpers of
``test_core_parity_export_pipeline_hdr``. Collected only when a parent test runs this file
explicitly (``-o python_files=*.py``); every test here is meant to fail, without the canary
reaching stdout, stderr or JUnit. Construction after Sol's r2 probe: a 128×128 gradient, a valid
float32 layer and a JPEG payload carrying the canary in Make and LensModel."""

from __future__ import annotations

import io
from typing import Any

import numpy as np
from PIL import Image

from support.export_pipeline_canary import CANARY
from support.export_pipeline_cases import check, redact_failures, same_redacted


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


@redact_failures
def test_hdr_gate_a_mismatch() -> None:
    import test_core_parity_export_pipeline_hdr as target

    expected, fin = _hdr_files()
    target._attribute(expected.replace(CANARY.encode(), b"X" * len(CANARY)), expected, fin, "SYNTHETIC HDR")


@redact_failures
def test_hdr_gate_b_mismatch() -> None:
    import test_core_parity_export_pipeline_hdr as target

    expected, fin = _hdr_files()
    # Gate A holds (Rust equals the sequential oracle); the "desktop" file differs.
    target._attribute(expected, expected.replace(CANARY.encode(), b"X" * len(CANARY)), fin, "SYNTHETIC HDR")


@redact_failures
def test_hdr_state_mismatch() -> None:
    state = {"asked": True, "active": False, "shape": None, "problem": CANARY, "frame": {"items": [{"k": "text", "text": CANARY}]}}
    same_redacted(state, {**state, "problem": "no_raw"}, "SYNTHETIC hdr state")
