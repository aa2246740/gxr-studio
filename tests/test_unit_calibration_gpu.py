"""公开合成数据在真实 WebGPU 上核对输入多项式和独立查表矩阵。"""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from gpu_gate import evaluate
from gpu_support import run_engine
from gxr_studio.gpu.source import raw_layout
from gxr_studio.gpu.tables import bake_bytes
from gxr_studio.raw import half_res_codes, open_dng, orient_array
from gxr_studio.render.chain import chroma_u8
from gxr_studio.render.params import parse_adjust
from gxr_studio.render.tones import channel_gains, display_table
from test_clone_coverage import _tiny_dng
from test_gpu_parity import _bake


def test_gpu_applies_input_polynomial_before_curves_and_uses_factory_lookup(tmp_path):
    dng_data = _tiny_dng(w=128, h=96)
    dng = open_dng(dng_data)
    try:
        layout, reason = raw_layout(dng)
        assert layout is not None, reason
        codes = np.ascontiguousarray(orient_array(half_res_codes(dng), dng.orientation), np.float32)
    finally:
        dng.release()
    files = {"/raw.dng": dng_data}
    cases, references = [], {}
    for degree in (4, 10):
        coefficients = np.zeros((degree, 3))
        coefficients[0] = [0.005, -0.003, 0.002]
        coefficients[1:] = np.arange((degree - 1) * 3).reshape(-1, 3) / 2000
        base = _bake()
        # The stock lookup and recipe chroma deliberately rotate differently.
        lookup = base.matrix.copy()
        lookup[[1, 2]] = lookup[[2, 1]]
        bake = replace(base, input_coefficients=coefficients, lookup_matrix=lookup, lookup_offset=base.offset)
        data, meta = bake_bytes(bake)
        url = f"/bake{degree}.bin"
        files[url] = data
        for exposure in (0, 0.6):
            identity = f"{degree}-{exposure}"
            adjust = {"exposure_ev": exposure}
            state = parse_adjust(adjust)
            references[identity] = chroma_u8(codes, bake, channel_gains(state), display_table(state, None), workers=1)
            cases.append(
                dict(
                    id=identity,
                    source="raw",
                    raw={"url": "/raw.dng", "layout": layout},
                    bake={"url": url, "key": url, "meta": meta},
                    look={"recipe": url, "lut": None, "lutStrength": 1, "adjust": adjust, "curve": None},
                    geometry={"straighten": 0, "rotate90": 0, "flip": False, "box": None},
                )
            )
    results, summary = run_engine(tmp_path, files, cases)
    assert summary.get("info", {}).get("vendor"), summary
    for identity, expected in references.items():
        verdict = evaluate(results[identity], expected)
        assert verdict["pass"], (identity, verdict)
