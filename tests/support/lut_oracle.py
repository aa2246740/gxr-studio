"""BLAS-free sequential Python oracle and Accelerate FMA attribution helpers for LUT parity tests."""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import numpy as np

from gxr_studio.lut import spaces
from gxr_studio.lut.cube import Cube

THRESHOLD_RED_DELTA = 0.00011095497837924685
GRADE_MUL = np.array([1.05, 0.96, 0.91], dtype=np.float64)
GRADE_OFF = np.array([0.02, -0.01, 0.03], dtype=np.float64)
THRESHOLD_OFF = np.array([0.02 - THRESHOLD_RED_DELTA, -0.01, 0.03], dtype=np.float64)


def matmul_t_seq(x: np.ndarray, m: np.ndarray) -> np.ndarray:
    """Evaluate `x @ m.T` in plain sequential IEEE index order (no BLAS, no FMA)."""
    a = np.asarray(x, np.float64)
    mat = np.asarray(m, np.float64)
    out = np.empty_like(a)
    for i in range(3):
        out[..., i] = (a[..., 0] * mat[i, 0] + a[..., 1] * mat[i, 1]) + a[..., 2] * mat[i, 2]
    return out


def dot_fma_left(v: np.ndarray, row: np.ndarray) -> float:
    """Left-to-right `math.fma` reproducing Accelerate's FMA contraction order on a 3-vector dot."""
    return math.fma(float(v[2]), float(row[2]), math.fma(float(v[1]), float(row[1]), float(v[0]) * float(row[0])))


def matmul_t_fma(px3: np.ndarray, m: np.ndarray) -> np.ndarray:
    """Single-pixel `px3 @ m.T` using `dot_fma_left`."""
    return np.array([dot_fma_left(px3, m[i]) for i in range(3)], dtype=np.float64)


class SeqTarget:
    """Drop-in BLAS-free sequential mirror of `gxr_studio.lut.spaces.Target` for test parity."""

    def __init__(
        self,
        cube: Cube,
        input_space: str,
        output_space: str = "srgb",
        input_gamut: str | None = None,
        output_gamut: str | None = None,
        input_range: str = "full",
        output_range: str = "full",
    ) -> None:
        self.cube = cube
        self.input_space = input_space
        self.output_space = output_space
        self.input_gamut = input_gamut
        self.output_gamut = output_gamut
        self.input_range = input_range
        self.output_range = output_range

    def render(self, stock_rgb: np.ndarray, xyz: np.ndarray | None = None, info: bool = False) -> Any:
        if self.input_space in spaces.DISPLAY:
            lin = spaces.decode(np.asarray(stock_rgb) / 255, "srgb")
            xyz_display = matmul_t_seq(lin, spaces.matrix("rec709"))
            inv_in = np.linalg.inv(spaces.matrix(self.input_gamut or spaces.GAMUT[self.input_space]))
            x = spaces.encode(matmul_t_seq(xyz_display, inv_in), self.input_space)
        else:
            if xyz is None:
                raise ValueError("Log LUT 需要 DNG 的场景 XYZ，不能从 JPEG 猜测")
            inv_in = np.linalg.inv(spaces.matrix(self.input_gamut or spaces.GAMUT[self.input_space]))
            x = spaces.encode(matmul_t_seq(xyz, inv_in), self.input_space)
        if self.input_range == "legal":
            x = (64 + 876 * x) / 1023
        out, stats = self.cube.apply(x, return_info=True)
        if self.output_range == "legal":
            out = (1023 * out - 64) / 876
        lin_out = spaces.decode(out, self.output_space)
        xyz_out = matmul_t_seq(lin_out, spaces.matrix(self.output_gamut or spaces.GAMUT[self.output_space]))
        srgb = spaces.encode(matmul_t_seq(xyz_out, np.linalg.inv(spaces.matrix("rec709"))), "srgb")
        stats["display_gamut_clipped_share"] = float(np.any((srgb < 0) | (srgb > 1), axis=-1).mean())
        rgb = np.clip(srgb, 0, 1) * 255
        return (rgb, stats) if info else rgb


def seq_apply(target: SeqTarget, rgb: np.ndarray, xyz: np.ndarray | None = None) -> np.ndarray:
    """Mirror of `gxr_studio.lut.photo._apply` evaluating through `SeqTarget`."""
    shape = rgb.shape
    flat = rgb.reshape(-1, 3)
    scene = None if xyz is None else xyz.reshape(-1, 3)
    result = np.empty(flat.shape, np.uint8)
    for start in range(0, len(flat), 65536):
        end = start + 65536
        out = target.render(flat[start:end], None if scene is None else scene[start:end])
        result[start:end] = np.floor(np.clip(out, 0, 255) + 0.5).astype(np.uint8)
    return cast(np.ndarray, result.reshape(shape))


def seq_display_lut_u8(target: SeqTarget) -> bytes:
    """Mirror of `gxr_studio.gpu.tables.display_lut_u8` evaluating through `seq_apply`."""
    ramp = np.arange(256, dtype=np.uint8)

    def block(b0: int) -> np.ndarray:
        grid = np.empty((32, 256, 256, 3), np.uint8)
        grid[..., 0] = ramp[None, None, :]
        grid[..., 1] = ramp[None, :, None]
        grid[..., 2] = np.arange(b0, b0 + 32, dtype=np.uint8)[:, None, None]
        out = np.empty((32, 256, 256, 4), np.uint8)
        out[..., :3] = seq_apply(target, grid.reshape(-1, 256, 3)).reshape(32, 256, 256, 3)
        out[..., 3] = 255
        return out

    with ThreadPoolExecutor(2, thread_name_prefix="gxr-lut-oracle") as pool:
        return b"".join(part.tobytes() for part in pool.map(block, range(0, 256, 32)))


def fma_render_display_px(
    cube: Cube,
    px_u8: np.ndarray,
    *,
    input_space: str,
    output_space: str,
    input_range: str = "full",
    output_range: str = "full",
    input_gamut: str | None = None,
    output_gamut: str | None = None,
) -> np.ndarray:
    """Reproduce Accelerate's FMA-fused batch result on one display pixel using `math.fma`."""
    lin = spaces.decode(np.asarray(px_u8, np.float64) / 255.0, "srgb")
    xyz_d = matmul_t_fma(lin, spaces.matrix("rec709"))
    inv_in = np.linalg.inv(spaces.matrix(input_gamut or spaces.GAMUT[input_space]))
    x = spaces.encode(matmul_t_fma(xyz_d, inv_in), input_space)
    if input_range == "legal":
        x = (64 + 876 * x) / 1023
    out = cube.apply(x[None, :])[0]
    if output_range == "legal":
        out = (1023 * out - 64) / 876
    lin_o = spaces.decode(out, output_space)
    xyz_o = matmul_t_fma(lin_o, spaces.matrix(output_gamut or spaces.GAMUT[output_space]))
    srgb = spaces.encode(matmul_t_fma(xyz_o, np.linalg.inv(spaces.matrix("rec709"))), "srgb")
    return np.clip(srgb, 0, 1) * 255.0
