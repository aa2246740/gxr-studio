"""显卡预览的透视（垂直、水平）和畸变，与 `finish.geometry.warp` 逐像素对拍（无头 Chrome + SwiftShader，不需要显卡）。

画布大小在 JS 里按 float64 算（frontend/src/warp.ts 的 expandedBounds），所以必须与 Python 的 `_expanded_bounds` 完全一样；
每个像素的反向映射在着色器里用 f32 算，只允许 f32 舍入的差别。

数据是合成的（纹理丰富的 JPG：渐变、细线、色块、噪点），不用机主的照片。横图、竖图各一组。
门槛（实测见 PR 描述）：
- 画布宽高与 Python 相同（形状对不上，评估前就失败）；
- ≤1 码值的像素 ≥ 99.9%（f32 取样位置的误差约 1e-4 像素，偶尔把取整推过 .5）；
- 最大差 ≤ 4，且超过 2 码值的像素只出现在有效区边缘的内外各一像素里；
- 有效区边界上，两边判断不同的像素数 ≤ 边缘带像素数的 0.5%（f32 与 float64 对「是否落在原图里」的判断可以不同）。

例外（只有一类）：强桶形畸变（滑块 ≳ 80）下，几何.py 里径向反函数的 8 步牛顿迭代在一部分像素上**没有收敛**
（靠近镜头映射的折返点，导数接近 0，残差可到 0.9）。那些像素 Python 自己取的也不是反函数的解，
float64 与 f32 的迭代会各走各的，差多少都没有意义。测试把这些像素（Python 的残差 > 1e-6）单独数出来：
它们只豁免逐像素比较，占照片内容（Python 的有效区）的比例有上限（≤ 3%，实测滑块 100 时 2.5%），其余像素仍用上面的门槛。
"""

from __future__ import annotations

import io
import json

import numpy as np
import pytest
from PIL import Image

from gpu_gate import edge_band, valid_mask
from gpu_support import run_engine
from gxr_studio.finish import geometry as pygeo
from gxr_studio.finish.compose import load_jpeg
from gxr_studio.finish.geometry import FILL, warp
from gxr_studio.finish.spec import parse_geometry
from gxr_studio.gpu.box import final_box

SIZES = {"land": (640, 960), "port": (960, 640)}  # (高, 宽)


def _texture_jpg(h: int, w: int, seed: int) -> bytes:
    """渐变 + 斜线 + 色块 + 噪点。4:4:4、质量 100，几乎无损。"""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    img = np.stack([xx * 255 // w, yy * 255 // h, ((xx + yy) * 3) % 256], axis=-1).astype(np.float64)
    img[(xx + 2 * yy) % 37 < 2] = [250, 250, 250]
    for _ in range(14):
        y0, x0 = rng.integers(0, h - 80), rng.integers(0, w - 100)
        img[y0 : y0 + 80, x0 : x0 + 100] = rng.integers(0, 256, 3)
    img += rng.normal(0, 6, img.shape)
    buf = io.BytesIO()
    Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB").save(buf, "JPEG", quality=100, subsampling=0)
    return buf.getvalue()


# (id, 几何, 裁切框的预设 / None)
GEOMETRIES: list[tuple[str, dict, str | None]] = [
    ("vertical_pos", {"vertical": 40}, None),
    ("vertical_neg", {"vertical": -60}, None),
    ("horizontal_pos", {"horizontal": 35}, None),
    ("horizontal_neg", {"horizontal": -50}, None),
    ("distortion_pos", {"distortion": 50}, None),
    ("distortion_neg", {"distortion": -60}, None),
    ("all_three", {"vertical": 25, "horizontal": 20, "distortion": -30}, None),
    ("max_perspective", {"vertical": 100, "horizontal": -100}, None),
    ("max_distortion_neg", {"distortion": -100}, None),
    ("distortion_70", {"distortion": 70}, None),
    ("distortion_80", {"vertical": 20, "distortion": 80}, None),
    ("distortion_90", {"vertical": 100, "horizontal": -100, "distortion": 90}, None),
    ("distortion_100", {"distortion": 100}, None),
    ("max_values", {"vertical": 100, "horizontal": -100, "distortion": 100}, None),
    ("with_straighten", {"vertical": 30, "distortion": 20, "straighten": 3.5}, None),
    ("with_rotate_flip", {"vertical": -45, "horizontal": 15, "rotate90": 1, "flip": True}, None),
    ("with_rotate3", {"distortion": -40, "horizontal": 30, "rotate90": 3}, None),
    ("everything", {"vertical": 20, "horizontal": -25, "distortion": 35, "straighten": -4.0, "rotate90": 2, "flip": True}, None),
    ("cropped_free", {"vertical": 35, "distortion": -25, "straighten": 2.0}, "free"),
    ("cropped_ratio", {"horizontal": -30, "distortion": 30, "rotate90": 1}, "3:2"),
]


def _unconverged(geom, w: int, h: int) -> np.ndarray:
    """成片坐标里（旋转、翻转之后，未裁切）Python 的牛顿迭代没收敛的像素：径向方程的残差 > 1e-6。"""
    radial, sv, sh, c, s, half_diag = pygeo._lens(geom, w, h)
    minx, miny, out_w, out_h = pygeo._expanded_bounds(w, h, geom)
    ox, oy = np.meshgrid(minx + np.arange(out_w) + 0.5, miny + np.arange(out_h) + 0.5)
    rx, ry = c * ox + s * oy, -s * ox + c * oy
    nx, ny = rx / (w / 2.0), ry / (h / 2.0)
    denom = 1.0 - sh * nx - sv * ny
    cx, cy = nx / denom * (w / 2.0), ny / denom * (h / 2.0)
    r_out = np.hypot(cx, cy) / half_diag
    sx, sy = pygeo._expanded_coords(w, h, geom)
    r_src = np.hypot(sx - (w - 1) / 2.0, sy - (h - 1) / 2.0) / half_diag
    bad = np.abs(radial * r_src**3 + r_src - r_out) > 1e-6
    return pygeo._rot_flip_mask(bad, geom)


@pytest.fixture(scope="module")
def warp_parity(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("warp")
    files, cases, refs, masks, loose, sizes = {}, [], {}, {}, {}, {}
    for orient, (h, w) in SIZES.items():
        data = _texture_jpg(h, w, 7 if orient == "land" else 8)
        (tmp / f"{orient}.jpg").write_bytes(data)
        files[f"/{orient}.jpg"] = data
        rgb = load_jpeg(tmp / f"{orient}.jpg", None)
        for gid, geometry, preset in GEOMETRIES:
            cid = f"{orient}_{gid}"
            geom = parse_geometry(geometry)
            warped, _valid = warp(rgb, geom)
            box = None
            if preset:
                box = list(final_box(w, h, geom, (0.0, 0.0, 1.0, 1.0), preset)[0])
                x, y, bw, bh = box
                warped = warped[y : y + bh, x : x + bw]
            refs[cid] = np.ascontiguousarray(warped)
            masks[cid] = valid_mask(w, h, geom, tuple(box) if box else None)
            bad = _unconverged(geom, w, h)
            loose[cid] = bad[box[1] : box[1] + box[3], box[0] : box[0] + box[2]] if box else bad
            sizes[cid] = (warped.shape[1], warped.shape[0])
            cases.append(
                {
                    "id": cid,
                    "source": f"jpg_{orient}",
                    "jpg": f"/{orient}.jpg",
                    "bake": None,
                    "lut": None,
                    "look": {"recipe": None, "lut": None, "lutStrength": 1, "adjust": {}, "curve": None},
                    "geometry": {
                        "straighten": geom.straighten,
                        "vertical": geom.vertical,
                        "horizontal": geom.horizontal,
                        "distortion": geom.distortion,
                        "rotate90": geom.rotate90,
                        "flip": geom.flip,
                        "box": box,
                    },
                }
            )
    out, summary = run_engine(tmp, files, cases)
    return out, refs, masks, loose, summary


def _stats(gpu: np.ndarray, ref: np.ndarray, mask: np.ndarray, loose: np.ndarray) -> dict:
    diff = np.abs(gpu.astype(np.int16) - ref.astype(np.int16)).max(axis=2)
    diff_all_max = int(diff.max())
    diff = diff * ~loose  # 牛顿迭代没收敛的像素不比（见文件头）
    band = edge_band(mask)
    # 有效区边界上两边判断不同：一边是填色，另一边不是
    fill_gpu = (gpu == np.array(FILL, np.uint8)).all(axis=2) & ~loose
    fill_ref = (ref == np.array(FILL, np.uint8)).all(axis=2) & ~loose
    flips = int((fill_gpu != fill_ref).sum())
    n = diff.size
    return {
        "pixels": n,
        "unconverged_share_of_photo": float((loose & mask).sum() / max(1, mask.sum())),
        "max_incl_unconverged": diff_all_max,
        "max": int(diff.max()),
        "over_1": int((diff > 1).sum()),
        "share_over_1": float((diff > 1).mean()),
        "share_nonzero": float((diff > 0).mean()),
        "band": int(band.sum()),
        "over_2_in_band": int(((diff > 2) & band).sum()),
        "over_2_out_band": int(((diff > 2) & ~band).sum()),
        "max_out_band": int((diff * ~band).max()),
        "coverage_flips": flips,
        "flips_outside_band": int(((fill_gpu != fill_ref) & ~band).sum()),
    }


CASES = [f"{o}_{g[0]}" for o in SIZES for g in GEOMETRIES]


@pytest.mark.parametrize("cid", CASES)
def test_gpu_warp_matches_python(warp_parity, cid):
    out, refs, masks, loose, summary = warp_parity
    assert cid in out, f"页面没有交回 {cid}：{json.dumps(summary)[:300]}"
    gpu, ref = out[cid], refs[cid]
    assert gpu.shape == ref.shape, f"画布宽高与 Python 不同：{gpu.shape} ≠ {ref.shape}"
    s = _stats(gpu, ref, masks[cid], loose[cid])
    print(cid, json.dumps(s))  # pytest -s：PR 描述里的实测数字
    assert s["unconverged_share_of_photo"] <= 0.03, s
    assert s["share_over_1"] <= 1e-3, s
    assert s["max_out_band"] <= 4, s
    assert s["over_2_out_band"] == 0, s
    assert s["flips_outside_band"] == 0, s
    assert s["coverage_flips"] <= max(4, s["band"] * 5e-3), s
