"""配方烘焙表和 LUT 表 → 小端 float32 字节 + 描述。布局与 `frontend/src/shaders/common.wgsl` 的读法一一对应。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np

from ..lut import spaces
from ..render.chain import ChromaBake

# common.wgsl 的 enc1/dec1 认得的空间编号
SPACE_IDS = {
    "srgb": 0,
    "rec709": 1,
    "gamma24": 2,
    "llog": 3,
    "llog709": 3,
    "linear": 4,
    "flog": 5,
    "flog2": 6,
    "vlog": 7,
    "slog3": 8,
    "slog3-sgamut3": 8,
    "applelog": 9,
}
OUTPUT_SPACES = ("srgb", "rec709", "gamma24", "linear")  # 着色器里有解码的输出空间
PRE_FLOATS = 4096 * 3


def bake_bytes(bk: ChromaBake) -> tuple[bytes, dict[str, Any]]:
    """pre(4096×3) | matrix(9) | offset(3) | gain(ny×nx×2)。"""
    pre = np.asarray(bk.pre, "<f4").reshape(-1)
    if pre.size != PRE_FLOATS:
        raise ValueError("一维曲线不是 4096×3")
    ny, nx = bk.gain.shape[:2]
    arr = np.concatenate(
        [pre, np.asarray(bk.matrix, "<f4").reshape(-1), np.asarray(bk.offset, "<f4").reshape(-1), np.asarray(bk.gain, "<f4").reshape(-1)]
    )
    meta = {"nx": int(nx), "ny": int(ny), "origin": [float(bk.origin[0]), float(bk.origin[1])], "step": float(bk.step)}
    return arr.astype("<f4").tobytes(), meta


def _gamut(space: str, override: str | None) -> np.ndarray:
    return np.asarray(spaces.matrix(override or spaces.GAMUT[space]), np.float64)


DISPLAY_TABLE_THREADS = 2  # 按要求最多两个并行计算


def display_lut_u8(target: Any) -> bytes:
    """显示类 LUT 的精确表：全部 256³ 个 8 位输入逐个交给生产函数 `lut.photo._apply`，结果按 RGBA 存。

    显示类 LUT 的输入是 8 位底图，所以这张表与 Python 对整张图套 LUT 逐像素相同（取整平局也一样），
    显卡只做查表。布局：x = R（最快）、y = G、z = B。第一次算约 4 秒，结果由调用方缓存。"""
    from ..lut.photo import _apply  # 延迟导入：lut.photo 依赖渲染服务

    ramp = np.arange(256, dtype=np.uint8)

    def block(b0: int) -> np.ndarray:
        grid = np.empty((32, 256, 256, 3), np.uint8)
        grid[..., 0] = ramp[None, None, :]
        grid[..., 1] = ramp[None, :, None]
        grid[..., 2] = np.arange(b0, b0 + 32, dtype=np.uint8)[:, None, None]
        out = np.empty((32, 256, 256, 4), np.uint8)
        out[..., :3] = _apply(target, grid.reshape(-1, 256, 3)).reshape(32, 256, 256, 3)
        out[..., 3] = 255
        return out

    with ThreadPoolExecutor(DISPLAY_TABLE_THREADS, thread_name_prefix="gxr-lut-table") as pool:
        return b"".join(part.tobytes() for part in pool.map(block, range(0, 256, 32)))


def lut_bytes(target: Any) -> tuple[bytes | None, dict[str, Any] | None, str | None]:
    """返回 (字节, 描述, None)，或 (None, None, 原因)。原因只给前端决定退回服务器预览。

    显示类输入（sRGB / Rec.709 / Gamma 2.4）：8 位精确表（`display_lut_u8`），任何一维表、输出空间、色域都支持。
    Log 类输入：码值是浮点，没法穷举，下发 3D 表和空间参数，由着色器计算。"""
    if target.input_space in spaces.DISPLAY:
        return display_lut_u8(target), {"n": 256, "display": True, "table8": True}, None
    cube = target.cube
    if cube.table1 is not None:
        return None, None, "lut_1d_shaper"
    if cube.table3 is None:
        return None, None, "lut_no_3d"
    if target.input_space not in SPACE_IDS:
        return None, None, "lut_input_space"
    if target.output_space not in OUTPUT_SPACES:
        return None, None, "lut_output_space"
    t3 = np.asarray(cube.table3, np.float64)
    n = int(t3.shape[0])
    if t3.shape != (n, n, n, 3) or n < 2:
        return None, None, "lut_shape"
    rgba = np.zeros((n, n, n, 4), "<f4")
    rgba[..., :3] = t3  # 索引 [b][g][r]，R 最快：x=R、y=G、z=B
    m_in = _gamut(target.input_space, target.input_gamut)
    m_out = _gamut(target.output_space, target.output_gamut)
    m709 = np.asarray(spaces.matrix("rec709"), np.float64)
    inv_in = np.linalg.inv(m_in)
    meta = {
        "n": n,
        "display": False,
        "table8": False,
        "input_space_id": SPACE_IDS[target.input_space],
        "output_space_id": SPACE_IDS[target.output_space],
        "input_legal": target.input_range == "legal",
        "output_legal": target.output_range == "legal",
        "domain_min": [float(v) for v in np.asarray(cube.domain_min, np.float64).reshape(-1)[:3]],
        "domain_max": [float(v) for v in np.asarray(cube.domain_max, np.float64).reshape(-1)[:3]],
        "range3": [float(cube.range3[0]), float(cube.range3[1])],
        "xyz_to_input": inv_in.tolist(),
        "out_to_srgb": (np.linalg.inv(m709) @ m_out).tolist(),
    }
    return rgba.tobytes(), meta, None
