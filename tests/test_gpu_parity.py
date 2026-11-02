"""显卡预览与 Python 生产函数逐像素对拍（无头 Chrome + SwiftShader 软件适配器，CI 不需要显卡）。

数据全部是合成的：合成 DNG（12/16 位、奇数裁切相位、2×2 黑电平、方向 1/6/8）、合成配方烘焙表、合成 .cube、
合成相机色彩矩阵、带 EXIF 方向的合成 JPG。不用机主的照片，也不用理光数据。
参考值来自生产代码：`chain.chroma_u8`、`finish.compose.finish_array`、`lut.photo._apply`、`raw.half_res_codes`、`load_jpeg`。
门槛见 `gpu_gate.py`（复审 r1 第 1–4 条）：≤1 码值 ≥ 99.99%、平均 ΔE2000 ≤ 0.01，有效区边缘带外最大差 ≤ 4，
>2 码值的像素只能在冻结位置上并由前向量化复算解释（这里的合成用例冻结列表都是空的），带内 ≤ ceil(N×10⁻⁶)。
复审的两个全合成反例（`clip(rgb × [0.86, 0.97, 1.05])` 的 17³ LUT 配 RAW 和 4:4:4 渐变 JPG）也在这里。
"""

from __future__ import annotations

import io
import json

import numpy as np
import pytest
from PIL import Image

from gpu_gate import evaluate, valid_mask
from gpu_support import run_engine
from gxr_studio.engine.colour import delta_e00, lab_from_codes
from gxr_studio.finish.compose import finish_array, load_jpeg
from gxr_studio.finish.spec import factory_frame, factory_stamp, parse_geometry
from gxr_studio.gpu.box import final_box
from gxr_studio.gpu.source import affine_of, raw_layout
from gxr_studio.gpu.tables import bake_bytes, lut_bytes
from gxr_studio.lut.cube import Cube
from gxr_studio.lut.photo import _apply
from gxr_studio.lut.scene import SceneColour
from gxr_studio.lut.spaces import Target
from gxr_studio.raw import NEUTRAL, half_res_codes, open_dng, orient_array
from gxr_studio.render.chain import ChromaBake, chroma_u8
from gxr_studio.render.params import EditState, parse_adjust, parse_curve
from gxr_studio.render.tones import channel_gains, display_table
from raw_support import bayer_scene, build_dng

H, W = 480, 640  # 马赛克尺寸；颜色层 240×320


def _scene(h: int, w: int, seed: int) -> np.ndarray:
    """平滑渐变 + 色块 + 细纹理 + 少量过曝，线性 0..1.2。"""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w] / np.array([h, w])[:, None, None]
    img = np.stack([xx, yy, 1 - 0.5 * (xx + yy)], axis=-1) * 0.8
    for _ in range(12):
        y0, x0 = rng.integers(0, h - 60), rng.integers(0, w - 80)
        img[y0 : y0 + 60, x0 : x0 + 80] = rng.uniform(0.02, 1.0, 3)
    img += rng.normal(0, 0.01, img.shape)
    img[: h // 10, : w // 10] = 1.2
    return np.clip(img, 0, 1.2)


def _dngs() -> dict[str, bytes]:
    a = build_dng(
        bayer_scene(H, W, np.clip(_scene(H, W, 1), 0, 1), white=4095, black=64),
        bps=12,
        black=(64,),
        white=4095,
        crop_origin=(1, 1),
        crop_size=(W - 2, H - 2),
        neutral=(0.55, 1.0, 0.62),
    )
    cfa_b = ((1, 0), (2, 1))
    mos_b = (
        bayer_scene(H, W, np.clip(_scene(H, W, 2), 0, 1), cfa=cfa_b, white=16000, black=0)
        + np.array([[60, 64], [62, 66]], np.uint16)[np.indices((H, W))[0] % 2, np.indices((H, W))[1] % 2]
    )
    b = build_dng(mos_b, bps=16, cfa=cfa_b, black=(60, 64, 62, 66), black_repeat=(2, 2), white=16066, orientation=6, neutral=(0.6, 1.0, 0.5))
    c = build_dng(bayer_scene(H, W, np.clip(_scene(H, W, 3), 0, 1), white=4095), bps=12, orientation=8, neutral=(0.5, 1.0, 0.7))
    return {"a": a, "b": b, "c": c}


def _bake() -> ChromaBake:
    """合成的配方表：每通道略有不同的一维曲线、BT.601 矩阵、在 (Cb, Cr) 上平滑变化的复数增益。"""
    x = np.arange(4096, dtype=np.float64)[:, None] / 4095.0
    pre = 255.0 * np.clip(x ** (1 / np.array([2.1, 2.2, 2.35])) * np.array([0.98, 1.0, 1.03]), 0, 1)
    matrix = np.array([[0.299, 0.587, 0.114], [-0.168736, -0.331264, 0.5], [0.5, -0.418688, -0.081312]])
    step, n = 2.0, 131
    origin = (-130.0, -130.0)
    cb = origin[0] + step * np.arange(n)
    cr = origin[1] + step * np.arange(n)
    c = cb[None, :] + 1j * cr[:, None]
    theta = np.angle(c)
    gain = (1 + 0.25 * np.sin(2 * theta)) * (1 + 0.1 * np.abs(c) / 128) * np.exp(1j * 0.15 * np.cos(theta))
    g = np.stack([gain.real, gain.imag], axis=-1).astype(np.float32)
    return ChromaBake(pre.astype(np.float32), matrix.astype(np.float32), np.array([2.0, 0.0, 0.0], np.float32), g, origin, step)


def _cube(size: int = 17) -> Cube:
    v = np.linspace(0, 1, size)
    nodes = np.array([[r, g, b] for b in v for g in v for r in v])
    mix = np.array([[0.9, 0.1, 0.0], [0.05, 0.85, 0.1], [0.0, 0.15, 0.95]])
    rows = np.clip(nodes @ mix.T, 0, 1) ** 0.9
    text = f'TITLE "synthetic"\nLUT_3D_SIZE {size}\n' + "\n".join(" ".join(f"{c:.9f}" for c in row) for row in rows) + "\n"
    return Cube.parse(text)


def _jpg(orientation: int) -> bytes:
    h, w = 360, 480
    yy, xx = np.mgrid[0:h, 0:w]
    img = np.stack([(xx * 255 // w), (yy * 255 // h), ((xx + yy) * 7) % 256], axis=-1).astype(np.uint8)
    img[100:200, 150:300] = [200, 60, 40]
    exif = Image.Exif()
    exif[0x0112] = orientation
    buf = io.BytesIO()
    Image.fromarray(img, "RGB").save(buf, "JPEG", quality=90, exif=exif.tobytes())
    return buf.getvalue()


def _counter_cube() -> Cube:
    """复审 r1 的合成反例：节点 = clip([r, g, b] × [0.86, 0.97, 1.05], 0, 1)，17³。"""
    v = np.linspace(0, 1, 17)
    nodes = np.clip(np.array([[r, g, b] for b in v for g in v for r in v]) * [0.86, 0.97, 1.05], 0, 1)
    text = 'TITLE "synthetic quantization challenge"\nLUT_3D_SIZE 17\n' + "\n".join(" ".join(f"{c:.17g}" for c in row) for row in nodes)
    return Cube.parse(text + "\n")


def _gradient_jpg() -> bytes:
    """复审 r1 的合成反例：256×256 渐变，质量 100、4:4:4，没有方向标记。"""
    yy, xx = np.mgrid[0:256, 0:256]
    buf = io.BytesIO()
    Image.fromarray(np.stack([xx, yy, (xx + yy) // 2], axis=-1).astype(np.uint8)).save(buf, "JPEG", quality=100, subsampling=0)
    return buf.getvalue()


def _stats(gpu: np.ndarray, ref: np.ndarray) -> dict[str, float]:
    assert gpu.shape == ref.shape, (gpu.shape, ref.shape)
    a, b = gpu.reshape(-1, 3), ref.reshape(-1, 3)
    diff = np.abs(a.astype(int) - b.astype(int)).max(axis=1)
    de = delta_e00(lab_from_codes(a), lab_from_codes(b))
    return {
        "mean": float(de.mean()),
        "within_1": float((diff <= 1).mean()),
        "identical": float((diff == 0).mean()),
        "max_code": int(diff.max()),
    }


def _finish(rgb, edit, *, stage="final", crop=None, from_raw):
    rect = tuple(crop["rect"]) if crop else None
    out, _valid = finish_array(
        rgb,
        edit,
        stage=stage,
        frame=factory_frame(),
        stamp=factory_stamp(),
        crop_rect=rect,
        meta={},
        taken=None,
        file_time=None,
        from_raw=from_raw,
        crop_preset=crop.get("preset") if crop else None,
    )
    return np.ascontiguousarray(out)


ADJ_TONE = {"exposure_ev": 0.6, "wb": {"warm": 25, "tint": -15}, "contrast": 35, "highlight": -40, "shadow": 30}
ADJ_EXTRA = {"black_point": 15, "saturation": 25, "vibrance": 20, "vignette": -35}
CURVE = [[0, 0], [64, 48], [128, 128], [192, 210], [255, 255]]


@pytest.fixture(scope="module")
def parity(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gpu")
    dngs = _dngs()
    bake = _bake()
    bake_data, bake_meta = bake_bytes(bake)
    cube = _cube()
    targets = {
        "display": Target(cube, "srgb", "srgb"),
        "rec709": Target(cube, "rec709", "gamma24"),
        "log": Target(cube, "llog", "gamma24"),
        "counter": Target(_counter_cube(), "srgb", "srgb"),
    }
    lut_files, lut_meta = {}, {}
    for k, t in targets.items():
        data, meta, reason = lut_bytes(t)
        assert data is not None and meta is not None, reason
        lut_files[f"/lut_{k}.bin"] = data
        lut_meta[k] = meta
    colour = SceneColour(np.array([0.55, 1.0, 0.62]), np.array([[0.6, 0.3, 0.1], [0.25, 0.7, 0.05], [0.02, 0.1, 0.9]]))
    scene = affine_of(colour)
    jpgs = {6: _jpg(6), 1: _jpg(1), 0: _gradient_jpg()}
    files = {f"/{k}.dng": v for k, v in dngs.items()} | {"/bake.bin": bake_data} | lut_files | {f"/o{k}.jpg": v for k, v in jpgs.items()}
    codes, layouts = {}, {}
    for k, data in dngs.items():
        dng = open_dng(data, f"{k}.dng")
        layout, reason = raw_layout(dng)
        assert layout is not None, reason
        layouts[k] = layout
        codes[k] = np.ascontiguousarray(orient_array(half_res_codes(dng), dng.orientation), np.float32)
    jpg_rgb = {}
    for k, data in jpgs.items():
        (tmp / f"o{k}.jpg").write_bytes(data)
        jpg_rgb[k] = load_jpeg(tmp / f"o{k}.jpg", None)

    cases, refs, masks, display = [], {}, {}, {}

    def add(cid, *, src, look_adjust, curve=None, lut=None, geometry=None, crop=None, stage="final"):
        adjust = parse_adjust(look_adjust)
        master = parse_curve({"master": curve}) if curve else None
        geom = parse_geometry(geometry or {})
        edit = EditState(adjust=adjust, master=master, geometry=geom)
        if src.startswith("jpg"):
            k = int(src[3:])
            base = jpg_rgb[k]
            source = {"source": src, "jpg": f"/o{k}.jpg"}
            layer = base.shape[1], base.shape[0]
            rgb = _apply(targets[lut], base) if lut else base
            ref = _finish(rgb, edit, stage=stage, crop=crop, from_raw=False)
        else:
            source = {"source": src, "raw": {"url": f"/{src}.dng", "layout": layouts[src], "scene": scene}}
            layer = codes[src].shape[1], codes[src].shape[0]
            if lut == "log":
                rgb = _apply(targets["log"], NEUTRAL.display(codes[src]), colour.xyz(codes[src]))
                ref = _finish(rgb, edit, stage=stage, crop=crop, from_raw=False)
            elif lut:
                rgb = _apply(targets[lut], chroma_u8(codes[src], bake, None, None))
                ref = _finish(rgb, edit, stage=stage, crop=crop, from_raw=False)
            else:
                rgb = chroma_u8(codes[src], bake, channel_gains(adjust), display_table(adjust, master))
                ref = _finish(rgb, edit, stage=stage, crop=crop, from_raw=True)
        box = None
        if stage == "final" and (crop or not geom.is_identity()):
            rect = tuple(crop["rect"]) if crop else None
            box = list(final_box(layer[0], layer[1], geom, rect, crop.get("preset") if crop else None)[0])
        g = {"straighten": geom.straighten, "rotate90": geom.rotate90, "flip": geom.flip, "box": box}
        masks[cid] = valid_mask(layer[0], layer[1], geom, tuple(box) if box else None)
        display[cid] = lut is not None and lut != "log"
        cases.append(
            {
                "id": cid,
                **source,
                "bake": {"url": "/bake.bin", "key": "bake", "meta": bake_meta} if not src.startswith("jpg") and lut != "log" else None,
                "lut": {"url": f"/lut_{lut}.bin", "key": lut, "meta": lut_meta[lut]} if lut else None,
                "look": {"recipe": "bake", "lut": lut, "lutStrength": 1, "adjust": look_adjust, "curve": curve},
                "geometry": g,
            }
        )
        refs[cid] = ref

    add("raw12_tone", src="a", look_adjust=ADJ_TONE)
    add("raw16_o6_curve_extra", src="b", look_adjust=ADJ_EXTRA, curve=CURVE)
    add("raw_o8_display_lut", src="c", look_adjust={"exposure_ev": 0.3, "contrast": 20, "saturation": -20}, lut="display")
    add("raw_log_lut", src="a", look_adjust={"exposure_ev": -0.3, "vignette": 25}, lut="log")
    add("jpg_o6_adjust", src="jpg6", look_adjust={**ADJ_TONE, **ADJ_EXTRA})
    add("jpg_rec709_lut_curve", src="jpg1", look_adjust={"shadow": 30}, curve=[[0, 10], [128, 120], [255, 250]], lut="rec709")
    add("jpg_untouched", src="jpg6", look_adjust={})
    add(
        "raw_geometry_ratio",
        src="a",
        look_adjust={},
        geometry={"straighten": 4.2, "rotate90": 1, "flip": True},
        crop={"preset": "3:2", "rect": [0.05, 0.05, 0.9, 0.9]},
    )
    add("raw_free_crop", src="b", look_adjust={"exposure_ev": 0.2}, crop={"preset": "free", "rect": [0.1, 0.08, 0.75, 0.8]})
    add("raw_uncropped_straighten", src="c", look_adjust={"highlight": -20}, geometry={"straighten": -6.0}, stage="uncropped")
    # 复审 r1 的两个全合成反例（原来按旧门槛失败：99.961% / 99.959%）
    add("counter_raw_lut_adjust", src="a", look_adjust={"exposure_ev": 0.3, "contrast": 20, "saturation": -20}, lut="counter")
    add("counter_jpg_lut_highlight", src="jpg0", look_adjust={"highlight": -30}, lut="counter")
    # 显示类 LUT + 拉直 + 旋转翻转 + 裁切：边缘带随几何变换
    add(
        "raw_display_lut_geometry",
        src="b",
        look_adjust={"contrast": 15},
        lut="display",
        geometry={"straighten": -3.5, "rotate90": 3, "flip": True},
        crop={"preset": "free", "rect": [0.0, 0.0, 1.0, 1.0]},
    )
    out, summary = run_engine(tmp, files, cases)
    return out, refs, summary, masks, display


CASES = [
    "raw12_tone",
    "raw16_o6_curve_extra",
    "raw_o8_display_lut",
    "raw_log_lut",
    "jpg_o6_adjust",
    "jpg_rec709_lut_curve",
    "jpg_untouched",
    "raw_geometry_ratio",
    "raw_free_crop",
    "raw_uncropped_straighten",
    "counter_raw_lut_adjust",
    "counter_jpg_lut_highlight",
    "raw_display_lut_geometry",
]
FROZEN: dict[str, dict[tuple[int, int], int]] = {}  # 合成用例没有需要冻结的 >2 码值像素


@pytest.mark.parametrize("cid", CASES)
def test_gpu_matches_python(parity, cid):
    out, refs, summary, masks, display = parity
    assert cid in out, f"页面没有交回 {cid}：{json.dumps(summary)[:300]}"
    r = evaluate(out[cid], refs[cid], mask=masks[cid], frozen=FROZEN.get(cid), display_lut=display[cid])
    print(cid, json.dumps({k: v for k, v in r.items() if k != "over_2_out_band"}))  # pytest -s 时打印，报告里引用
    assert r["pass"], r


def test_gpu_parity_ran_on_a_real_adapter(parity):
    _out, _refs, summary, _masks, _display = parity
    assert summary.get("info", {}).get("vendor"), summary
