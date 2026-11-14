"""Quantization attribution helper for gxr-jpeg Gate B parity checks.

Mirrors ``jpeg-encoder 0.6.0`` (`rgb_to_ycbcr`, `fdct`, and `QuantizationTable::quantize`)
plus ``gxr-jpeg``'s ``rgb_to_ycbcr_h2v2`` so that whenever a real image exceeds the legacy
single-step difference limit (``max > 4`` for ``q >= 85`` or ``max > 16`` for ``q == 70``),
we verify that every differing quantized coefficient in the worst pixel's MCU/block sits
at an exact half-integer (``.5``) before quantization.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Sequence

import numpy as np
from PIL import Image

_CONST_BITS = 13
_PASS1_BITS = 2
_SHIFT = 15

_FIX_0_298631336 = 2446
_FIX_0_390180644 = 3196
_FIX_0_541196100 = 4433
_FIX_0_765366865 = 6270
_FIX_0_899976223 = 7373
_FIX_1_175875602 = 9633
_FIX_1_501321110 = 12299
_FIX_1_847759065 = 15137
_FIX_1_961570560 = 16069
_FIX_2_053119869 = 16819
_FIX_2_562915447 = 20995
_FIX_3_072711026 = 25172


@dataclass(frozen=True)
class QuantDiff:
    component: str
    u: int
    v: int
    fdct_val: int
    q_val: int
    divisor: int
    rust_q: int
    libjpeg_q: int
    is_exact_half: bool


def _descale(x: int, n: int) -> int:
    return (x + (1 << (n - 1))) >> n


def _into_i16(v: int) -> int:
    return ((v + 32768) & 0xFFFF) - 32768


def _fdct_8x8(block64: list[int]) -> list[int]:
    data = list(block64)
    for y in range(8):
        o = y * 8
        tmp0 = data[o + 0] + data[o + 7]
        tmp7 = data[o + 0] - data[o + 7]
        tmp1 = data[o + 1] + data[o + 6]
        tmp6 = data[o + 1] - data[o + 6]
        tmp2 = data[o + 2] + data[o + 5]
        tmp5 = data[o + 2] - data[o + 5]
        tmp3 = data[o + 3] + data[o + 4]
        tmp4 = data[o + 3] - data[o + 4]

        tmp10 = tmp0 + tmp3
        tmp13 = tmp0 - tmp3
        tmp11 = tmp1 + tmp2
        tmp12 = tmp1 - tmp2
        data[o + 0] = _into_i16((tmp10 + tmp11) << _PASS1_BITS)
        data[o + 4] = _into_i16((tmp10 - tmp11) << _PASS1_BITS)
        z1 = (tmp12 + tmp13) * _FIX_0_541196100
        data[o + 2] = _into_i16(_descale(z1 + tmp13 * _FIX_0_765366865, _CONST_BITS - _PASS1_BITS))
        data[o + 6] = _into_i16(_descale(z1 + tmp12 * -_FIX_1_847759065, _CONST_BITS - _PASS1_BITS))

        z1 = tmp4 + tmp7
        z2 = tmp5 + tmp6
        z3 = tmp4 + tmp6
        z4 = tmp5 + tmp7
        z5 = (z3 + z4) * _FIX_1_175875602
        t4 = tmp4 * _FIX_0_298631336
        t5 = tmp5 * _FIX_2_053119869
        t6 = tmp6 * _FIX_3_072711026
        t7 = tmp7 * _FIX_1_501321110
        z1 *= -_FIX_0_899976223
        z2 *= -_FIX_2_562915447
        z3 = z3 * -_FIX_1_961570560 + z5
        z4 = z4 * -_FIX_0_390180644 + z5
        data[o + 7] = _into_i16(_descale(t4 + z1 + z3, _CONST_BITS - _PASS1_BITS))
        data[o + 5] = _into_i16(_descale(t5 + z2 + z4, _CONST_BITS - _PASS1_BITS))
        data[o + 3] = _into_i16(_descale(t6 + z2 + z3, _CONST_BITS - _PASS1_BITS))
        data[o + 1] = _into_i16(_descale(t7 + z1 + z4, _CONST_BITS - _PASS1_BITS))

    for x in range(8):
        tmp0 = data[0 * 8 + x] + data[7 * 8 + x]
        tmp7 = data[0 * 8 + x] - data[7 * 8 + x]
        tmp1 = data[1 * 8 + x] + data[6 * 8 + x]
        tmp6 = data[1 * 8 + x] - data[6 * 8 + x]
        tmp2 = data[2 * 8 + x] + data[5 * 8 + x]
        tmp5 = data[2 * 8 + x] - data[5 * 8 + x]
        tmp3 = data[3 * 8 + x] + data[4 * 8 + x]
        tmp4 = data[3 * 8 + x] - data[4 * 8 + x]

        tmp10 = tmp0 + tmp3
        tmp13 = tmp0 - tmp3
        tmp11 = tmp1 + tmp2
        tmp12 = tmp1 - tmp2
        data[0 * 8 + x] = _into_i16(_descale(tmp10 + tmp11, _PASS1_BITS))
        data[4 * 8 + x] = _into_i16(_descale(tmp10 - tmp11, _PASS1_BITS))
        z1 = (tmp12 + tmp13) * _FIX_0_541196100
        data[2 * 8 + x] = _into_i16(_descale(z1 + tmp13 * _FIX_0_765366865, _CONST_BITS + _PASS1_BITS))
        data[6 * 8 + x] = _into_i16(_descale(z1 + tmp12 * -_FIX_1_847759065, _CONST_BITS + _PASS1_BITS))

        z1 = tmp4 + tmp7
        z2 = tmp5 + tmp6
        z3 = tmp4 + tmp6
        z4 = tmp5 + tmp7
        z5 = (z3 + z4) * _FIX_1_175875602
        t4 = tmp4 * _FIX_0_298631336
        t5 = tmp5 * _FIX_2_053119869
        t6 = tmp6 * _FIX_3_072711026
        t7 = tmp7 * _FIX_1_501321110
        z1 *= -_FIX_0_899976223
        z2 *= -_FIX_2_562915447
        z3 = z3 * -_FIX_1_961570560 + z5
        z4 = z4 * -_FIX_0_390180644 + z5
        data[7 * 8 + x] = _into_i16(_descale(t4 + z1 + z3, _CONST_BITS + _PASS1_BITS))
        data[5 * 8 + x] = _into_i16(_descale(t5 + z2 + z4, _CONST_BITS + _PASS1_BITS))
        data[3 * 8 + x] = _into_i16(_descale(t6 + z2 + z3, _CONST_BITS + _PASS1_BITS))
        data[1 * 8 + x] = _into_i16(_descale(t7 + z1 + z4, _CONST_BITS + _PASS1_BITS))
    return data


def _compute_reciprocal(divisor: int) -> tuple[int, int]:
    if divisor <= 1:
        return 1, 0
    recip = (1 << _SHIFT) // divisor
    frac = (1 << _SHIFT) % divisor
    corr = divisor // 2
    if frac != 0:
        if frac <= corr:
            corr += 1
        else:
            recip += 1
    return recip, corr


def _quant_rust(val: int, q_val: int) -> int:
    divisor = q_val * 8
    recip, corr = _compute_reciprocal(divisor)
    prod = ((abs(val) + corr) * recip) >> _SHIFT
    return -prod if val < 0 else prod


def _quant_libjpeg(val: int, q_val: int) -> int:
    divisor = q_val * 8
    prod = (abs(val) + divisor // 2) // divisor
    return -prod if val < 0 else prod


def _rgb_to_ycbcr_px(r: int, g: int, b: int) -> tuple[int, int, int]:
    y = (19595 * r + 38470 * g + 7471 * b + 0x7FFF) >> 16
    cb = (-11059 * r - 21709 * g + 32768 * b + (128 << 16) + 0x7FFF) >> 16
    cr = (32768 * r - 27439 * g - 5329 * b + (128 << 16) + 0x7FFF) >> 16
    return y, cb, cr


def _collect_block_diffs(component: str, block64: list[int], dqt: Sequence[int]) -> list[QuantDiff]:
    fdct_vals = _fdct_8x8(block64)
    diffs: list[QuantDiff] = []
    for u in range(8):
        for v in range(8):
            idx = u * 8 + v
            fv = fdct_vals[idx]
            qv = int(dqt[idx])
            divisor = qv * 8
            qr = _quant_rust(fv, qv)
            ql = _quant_libjpeg(fv, qv)
            if qr != ql:
                diffs.append(
                    QuantDiff(
                        component=component,
                        u=u,
                        v=v,
                        fdct_val=fv,
                        q_val=qv,
                        divisor=divisor,
                        rust_q=qr,
                        libjpeg_q=ql,
                        is_exact_half=(abs(fv) % divisor == divisor // 2),
                    )
                )
    return diffs


def _extract_y_block(rgb: np.ndarray, by: int, bx: int) -> list[int]:
    h, w, _ = rgb.shape
    block: list[int] = []
    for dy in range(8):
        ry = min(by + dy, h - 1)
        for dx in range(8):
            rx = min(bx + dx, w - 1)
            px = rgb[ry, rx]
            y_val, _, _ = _rgb_to_ycbcr_px(int(px[0]), int(px[1]), int(px[2]))
            block.append(y_val - 128)
    return block


def _extract_h2v2_chroma_blocks(rgb: np.ndarray, my: int, mx: int) -> tuple[list[int], list[int]]:
    h, w, _ = rgb.shape
    cb_block: list[int] = []
    cr_block: list[int] = []
    for cy in range(8):
        y0 = min(my + 2 * cy, h - 1)
        y1 = min(y0 + 1, h - 1)
        for cx in range(8):
            col = mx + 2 * cx
            x0 = min(col, w - 1)
            x1 = min(col + 1, w - 1)
            bias = 1 if (col // 2) % 2 == 0 else 2
            cb_sum = 0
            cr_sum = 0
            for ry in (y0, y1):
                for rx in (x0, x1):
                    px = rgb[ry, rx]
                    _, cb, cr = _rgb_to_ycbcr_px(int(px[0]), int(px[1]), int(px[2]))
                    cb_sum += cb
                    cr_sum += cr
            cb_block.append(((cb_sum + bias) >> 2) - 128)
            cr_block.append(((cr_sum + bias) >> 2) - 128)
    return cb_block, cr_block


def assert_halfway_attribution(
    rgb: np.ndarray,
    quality: int,
    rust_jpg: bytes,
    py_jpg: bytes,
    label: str,
) -> list[QuantDiff]:
    """Prove every differing quantized coefficient in the worst pixel's block is exact .5."""
    im_r = Image.open(io.BytesIO(rust_jpg))
    im_p = Image.open(io.BytesIO(py_jpg))
    arr_r = np.asarray(im_r.convert("RGB"), dtype=np.int16)
    arr_p = np.asarray(im_p.convert("RGB"), dtype=np.int16)
    diff = np.abs(arr_r - arr_p)
    max_diff = int(diff.max())
    ys, xs, cs = np.where(diff == max_diff)
    wy, wx, wc = int(ys[0]), int(xs[0]), int(cs[0])

    dqt = im_p.quantization
    by, bx = (wy // 8) * 8, (wx // 8) * 8
    diffs = _collect_block_diffs("Y", _extract_y_block(rgb, by, bx), dqt[0])
    if quality < 90:
        my, mx = (wy // 16) * 16, (wx // 16) * 16
        cb_blk, cr_blk = _extract_h2v2_chroma_blocks(rgb, my, mx)
        diffs.extend(_collect_block_diffs("Cb", cb_blk, dqt[1]))
        diffs.extend(_collect_block_diffs("Cr", cr_blk, dqt[1]))

    if not diffs:
        raise AssertionError(
            f"{label} q={quality}: worst pixel (y={wy}, x={wx}, c={wc}, diff={max_diff}, "
            f"rust={arr_r[wy, wx].tolist()}, py={arr_p[wy, wx].tolist()}) had no differing quantized DCT coefficients"
        )
    non_half = [d for d in diffs if not d.is_exact_half]
    if non_half:
        raise AssertionError(
            f"{label} q={quality}: worst pixel (y={wy}, x={wx}, c={wc}, diff={max_diff}) has non-.5 quantized diffs: {non_half}"
        )
    return diffs
