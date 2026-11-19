"""g250 半格一对的 RAW 合成器：全分辨率条带合成、半分辨率代理合成与单格切分。

几何约定（传感器坐标系，4288×2848）：
- 理光相机写出的 JPEG 比 DNG DefaultCropOrigin [8, 7] 多偏移 1 列，因此相机 JPEG 列 1100 对应 DNG 列 1099。
- 每半取 DNG 列 1099 .. 1099+2087（宽 2088），接缝在 2088..2199（宽 112，RGB 0）。
- 第 1 张放在 `first_half`（`left` → 0..2087，`right` → 2200..4287），第 2 张在另一侧，拼完再按共享 EXIF Orientation 转正。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import numpy as np
from PIL import Image

from . import crop as cropmod
from . import halfframe as hf
from . import halfframe_pairs as hfp
from .raw import NEUTRAL, Dng, RawDecodeError, Renderer, half_res_codes, open_dng, orient_array
from .raw.codes import crop_shape
from .raw.demosaic import demosaic_strips
from .raw.dng import require_finite
from .render import chain
from .render.camera_recipe import recorded_recipe
from .render.params import EditError, EditState
from .render.tones import channel_gains, display_table

FULL_WIDTH = hf.FULL_WIDTH  # 4288
FULL_HEIGHT = hf.FULL_HEIGHT  # 2848
FULL_HALF_W = hfp.G250_HALF_W  # 2088
FULL_SEAM_X0 = hfp.G250_SEAM_X0  # 2088
FULL_SEAM_X1 = hfp.G250_SEAM_X1  # 2199
FULL_SEAM_W = FULL_SEAM_X1 - FULL_SEAM_X0 + 1  # 112
FULL_RIGHT_X0 = FULL_SEAM_X1 + 1  # 2200
FULL_STRIP_X0_DNG = hfp.G250_STRIP_X0_DNG  # 1099
FULL_STRIP_X0_JPG = hfp.G250_STRIP_X0_JPG  # 1100


@dataclass(frozen=True)
class PairGeometry:
    width: int
    half_w: int
    strip_x0: int
    seam_x0: int
    seam_w: int
    right_x0: int


def validate_orientation(orientation: int) -> int:
    """只允许 EXIF Orientation 1/3/6/8（旋转，不含镜像 2/4/5/7）。"""
    ori = int(orientation)
    if ori not in hfp.VALID_ORIENTATIONS:
        raise ValueError(f"unsupported orientation: {orientation}")
    return ori


def full_geometry() -> PairGeometry:
    return PairGeometry(
        width=FULL_WIDTH,
        half_w=FULL_HALF_W,
        strip_x0=FULL_STRIP_X0_DNG,
        seam_x0=FULL_SEAM_X0,
        seam_w=FULL_SEAM_W,
        right_x0=FULL_RIGHT_X0,
    )


def proxy_geometry(width: int) -> PairGeometry:
    """代理宽度 `width` 下的几何取整：half_w = round(2088*W/4288)，strip_x0 = floor(1099*W/4288)，seam = W - 2*half_w。"""
    if isinstance(width, bool) or width <= 0:
        raise ValueError(f"invalid proxy width: {width}")
    half_w = round(FULL_HALF_W * width / FULL_WIDTH)
    strip_x0 = (FULL_STRIP_X0_DNG * width) // FULL_WIDTH
    seam_w = width - 2 * half_w
    if half_w <= 0 or seam_w < 0 or strip_x0 + half_w > width:
        raise ValueError(f"proxy width too small for half-frame geometry: {width}")
    return PairGeometry(
        width=width,
        half_w=half_w,
        strip_x0=strip_x0,
        seam_x0=half_w,
        seam_w=seam_w,
        right_x0=half_w + seam_w,
    )


def compose_strips(
    strip1: np.ndarray,
    strip2: np.ndarray,
    *,
    first_half: str,
    seam_w: int,
    orientation: int,
) -> np.ndarray:
    """在传感器坐标系下拼接两张半格条带（接缝为 0），再按共享 EXIF 方向转正。"""
    ori = validate_orientation(orientation)
    if first_half not in ("left", "right"):
        raise ValueError(f"invalid first_half: {first_half}")
    if strip1.ndim != 3 or strip2.ndim != 3 or strip1.shape != strip2.shape:
        raise ValueError(f"strip shapes must match (H, half_w, C): {strip1.shape} vs {strip2.shape}")
    if seam_w < 0:
        raise ValueError(f"invalid seam_w: {seam_w}")
    h, half_w, ch = strip1.shape
    canvas = np.zeros((h, 2 * half_w + seam_w, ch), dtype=strip1.dtype)
    if first_half == "left":
        canvas[:, :half_w] = strip1
        canvas[:, half_w + seam_w :] = strip2
    else:
        canvas[:, :half_w] = strip2
        canvas[:, half_w + seam_w :] = strip1
    return np.ascontiguousarray(orient_array(canvas, ori))


def orient_half(strip: np.ndarray, orientation: int) -> np.ndarray:
    """将传感器坐标系下的单格条带按共享 EXIF 方向转正。"""
    ori = validate_orientation(orientation)
    return np.ascontiguousarray(orient_array(strip, ori))


def camera_jpg_singles(
    first_jpg: Path,
    second_jpg: Path,
    *,
    first_half: str,
    orientation: int,
) -> tuple[np.ndarray, np.ndarray]:
    """原图（相机 JPG）来源的两张单格：第 1 张取主图列 1100..3187，第 2 张取相机双联里的另一半。"""
    ori = validate_orientation(orientation)
    if first_half not in ("left", "right"):
        raise ValueError(f"invalid first_half: {first_half}")
    with Image.open(first_jpg) as im1:
        arr1 = np.asarray(im1.convert("RGB"), dtype=np.uint8)
    with Image.open(second_jpg) as im2:
        arr2 = np.asarray(im2.convert("RGB"), dtype=np.uint8)
    if arr1.shape[:2] != (FULL_HEIGHT, FULL_WIDTH) or arr2.shape[:2] != (FULL_HEIGHT, FULL_WIDTH):
        raise ValueError(f"unexpected camera JPEG size for half-frame singles: {arr1.shape[:2]} / {arr2.shape[:2]}")
    strip1 = arr1[:, FULL_STRIP_X0_JPG : FULL_STRIP_X0_JPG + FULL_HALF_W]
    strip2 = arr2[:, FULL_RIGHT_X0 : FULL_RIGHT_X0 + FULL_HALF_W] if first_half == "left" else arr2[:, :FULL_HALF_W]
    return orient_half(strip1, ori), orient_half(strip2, ori)


def _half_shot_from_source(src: dict[str, Any]) -> dict[str, Any]:
    raw_files = src.get("all_files")
    all_files: dict[str, Any] = raw_files if isinstance(raw_files, dict) else {}
    return {
        "shot_id": src["shot_id"],
        "name": src["name"],
        "orientation": int(src.get("orientation") or 1),
        "unit": src.get("unit"),
        "lens": src.get("lens"),
        "iso": src.get("iso"),
        "width": src.get("width"),
        "height": src.get("height"),
        "date": src.get("date"),
        "captured_at": src.get("captured_at"),
        "copyright": src.get("copyright"),
        "recipe_tag": src.get("recipe_tag"),
        "files": {k: dict(v) for k, v in all_files.items()},
    }


def pair_half_shots(shot: dict[Any, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """取出半格一对两张原片的合成用 shot 字典（各自带自己的 DNG/JPG、镜头、ISO、拍摄标签）。"""
    sources = shot.get("pair_sources")
    if not isinstance(sources, dict) or not isinstance(sources.get("first"), dict) or not isinstance(sources.get("second"), dict):
        raise ValueError("shot has no pair_sources")
    return _half_shot_from_source(sources["first"]), _half_shot_from_source(sources["second"])


def pair_orientation_and_half(shot: dict[Any, Any]) -> tuple[int, str]:
    pair = shot.get("pair")
    if not isinstance(pair, dict):
        raise ValueError("shot has no pair info")
    first_half = str(pair.get("first_half") or "")
    if first_half not in ("left", "right"):
        raise ValueError(f"invalid pair first_half: {first_half}")
    return validate_orientation(int(shot.get("orientation") or 1)), first_half


def half_edit_for(render: Any, half_shot: dict[str, Any], edit: EditState) -> EditState:
    """显式选中配方时两半共用该配方；未显式选择时各自按自己的拍摄标签选配方，缺标签则退回默认。"""
    if edit.recipe is not None:
        return edit
    try:
        sel = recorded_recipe(half_shot.get("recipe_tag"), render.recipe_menu())
    except EditError:
        sel = None
    return replace(edit, recipe=sel) if sel is not None else edit


def pair_plans(
    render: Any,
    shot: dict[Any, Any],
    edit: EditState,
) -> tuple[Any, Any, dict[str, Any], dict[str, Any], EditState, EditState]:
    s1, s2 = pair_half_shots(shot)
    e1 = half_edit_for(render, s1, edit)
    e2 = half_edit_for(render, s2, edit)
    return render.plan(s1, e1), render.plan(s2, e2), s1, s2, e1, e2


def combined_plan(plan1: Any, plan2: Any) -> Any:
    from .render.service import CALIBRATED_LABEL, UNCALIBRATED_LABEL

    calibrated = bool(plan1.calibrated and plan2.calibrated)
    reasons = tuple(dict.fromkeys((*plan1.reasons, *plan2.reasons)))
    return replace(
        plan2,
        calibrated=calibrated,
        reasons=reasons,
        label=CALIBRATED_LABEL if calibrated else UNCALIBRATED_LABEL,
    )


def render_dng_strip_full(dng: Dng, renderer: Renderer, geo: PairGeometry | None = None) -> np.ndarray:
    """全分辨率条带式去马赛克：只对半格列区间 [strip_x0, strip_x0 + half_w) 执行色彩渲染。"""
    h, w = crop_shape(dng)
    g = geo if geo is not None else (full_geometry() if w == FULL_WIDTH else proxy_geometry(w))
    out = np.empty((h, g.half_w, 3), np.uint8)
    x0, x1 = g.strip_x0, g.strip_x0 + g.half_w
    for y0, strip in demosaic_strips(dng):
        sliced = strip[:, x0:x1]
        require_finite(sliced, "去马赛克结果")
        out[y0 : y0 + strip.shape[0]] = renderer.display(sliced)
    return out


def render_dng_strip_half(dng: Dng, renderer: Renderer) -> tuple[np.ndarray, PairGeometry]:
    """半分辨率快速路径（传感器坐标系，不转正），只对半格列区间渲染。"""
    codes = half_res_codes(dng)
    geo = proxy_geometry(codes.shape[1])
    sliced = codes[:, geo.strip_x0 : geo.strip_x0 + geo.half_w]
    require_finite(sliced, "半分辨率码值")
    return renderer.display(sliced), geo


def _render_one_dng_strip(
    path: Path,
    renderer: Renderer,
    *,
    full: bool,
) -> tuple[np.ndarray, PairGeometry]:
    dng = open_dng(path)
    try:
        if full:
            _h, w = crop_shape(dng)
            geo = full_geometry() if w == FULL_WIDTH else proxy_geometry(w)
            return render_dng_strip_full(dng, renderer, geo), geo
        return render_dng_strip_half(dng, renderer)
    finally:
        dng.release()


def _labels_from_renderers(r1: Renderer, r2: Renderer, *, neutral: bool) -> dict[str, str]:
    if neutral:
        note = NEUTRAL.description or f"GXR Studio {NEUTRAL.id}: white-balanced linear sensor data to sRGB, no recipe applied"
        return {
            "kind": "original",
            "label": "original",
            "text": "中性转换",
            "id": "neutral-preview",
            "tag": "_neutral",
            "description": note,
            "detail": "",
            "key": "",
            "cache": "miss",
            "calibrated": "0",
        }
    calibrated = bool(r1.calibrated and r2.calibrated)
    ident = "recipe-prediction" if calibrated else "recipe-prediction-uncalibrated"
    label = r2.label if calibrated else (r1.label if not r1.calibrated else r2.label)
    note = r2.description or label
    return {
        "kind": "prediction",
        "label": "prediction",
        "text": label,
        "id": ident,
        "tag": r2.filename_tag,
        "description": note,
        "detail": "",
        "key": "",
        "cache": "miss",
        "calibrated": "1" if calibrated else "0",
        **(r2.recipe_binding or {}),
    }


def render_pair_strips(
    render: Any,
    lib: Any,
    shot: dict[Any, Any],
    edit: EditState,
    *,
    full: bool,
    neutral: bool = False,
    precise: bool = False,
) -> tuple[np.ndarray, np.ndarray, int, str, int, dict[str, str]]:
    """渲染半格一对的两张半格条带（传感器坐标系），同时返回接缝宽度、方向和标注。"""
    ori, first_half = pair_orientation_and_half(shot)
    s1, s2 = pair_half_shots(shot)
    if neutral:
        r1 = r2 = NEUTRAL
    else:
        e1 = half_edit_for(render, s1, edit)
        e2 = half_edit_for(render, s2, edit)
        r1 = render.renderer(s1, e1, precise=precise)
        r2 = render.renderer(s2, e2, precise=precise)
    strip1, geo1 = _render_one_dng_strip(lib.abspath(s1["files"]["dng"]["relpath"]), r1, full=full)
    strip2, geo2 = _render_one_dng_strip(lib.abspath(s2["files"]["dng"]["relpath"]), r2, full=full)
    if strip1.shape != strip2.shape or geo1.seam_w != geo2.seam_w:
        raise RawDecodeError("半格一对的两张 RAW 尺寸不一致")
    labels = _labels_from_renderers(r1, r2, neutral=neutral)
    return strip1, strip2, geo1.seam_w, first_half, ori, labels


def render_pair_rgb(
    render: Any,
    lib: Any,
    shot: dict[Any, Any],
    edit: EditState,
    *,
    full: bool,
    neutral: bool = False,
    precise: bool = False,
) -> tuple[np.ndarray, dict[str, str]]:
    """合成半格一对双联 RGB（已按共享方向转正）。"""
    strip1, strip2, seam_w, first_half, ori, labels = render_pair_strips(render, lib, shot, edit, full=full, neutral=neutral, precise=precise)
    rgb = compose_strips(strip1, strip2, first_half=first_half, seam_w=seam_w, orientation=ori)
    return rgb, labels


def render_pair_lut_scene_strips(
    lib: Any,
    shot: dict[Any, Any],
    target: Any,
    apply_fn: Callable[[Any, np.ndarray, np.ndarray | None], np.ndarray],
    *,
    full: bool,
) -> tuple[np.ndarray, np.ndarray, int, str, int]:
    """场景域 / Log LUT：每张 DNG 各自做 scene_colour 转换，返回传感器坐标系下的两张半格条带。"""
    from .lut.scene import scene_colour

    ori, first_half = pair_orientation_and_half(shot)
    s1, s2 = pair_half_shots(shot)
    strips: list[tuple[np.ndarray, PairGeometry]] = []
    for half_shot in (s1, s2):
        dng = open_dng(lib.abspath(half_shot["files"]["dng"]["relpath"]))
        try:
            colour = scene_colour(dng)

            def _display(codes: np.ndarray, col: Any = colour) -> np.ndarray:
                return apply_fn(target, NEUTRAL.display(codes), col.xyz(codes))

            renderer = Renderer("lut-prediction", "电脑预测", "", False, _display)
            if full:
                _h, w = crop_shape(dng)
                geo = full_geometry() if w == FULL_WIDTH else proxy_geometry(w)
                strips.append((render_dng_strip_full(dng, renderer, geo), geo))
            else:
                strips.append(render_dng_strip_half(dng, renderer))
        finally:
            dng.release()
    (strip1, geo1), (strip2, geo2) = strips
    if strip1.shape != strip2.shape or geo1.seam_w != geo2.seam_w:
        raise RawDecodeError("半格一对的两张 RAW 尺寸不一致")
    return strip1, strip2, geo1.seam_w, first_half, ori


def render_pair_lut_scene(
    lib: Any,
    shot: dict[Any, Any],
    target: Any,
    apply_fn: Callable[[Any, np.ndarray, np.ndarray | None], np.ndarray],
    *,
    full: bool,
) -> np.ndarray:
    """场景域 / Log LUT：每张 DNG 各自做 scene_colour 转换后再拼成双联。"""
    strip1, strip2, seam_w, first_half, ori = render_pair_lut_scene_strips(lib, shot, target, apply_fn, full=full)
    return compose_strips(strip1, strip2, first_half=first_half, seam_w=seam_w, orientation=ori)


def _resize_codes(codes: np.ndarray, target_h: int, target_w: int) -> np.ndarray:
    if codes.shape[:2] == (target_h, target_w):
        return codes
    out = np.empty((target_h, target_w, 3), np.float32)
    for ch in range(3):
        im = Image.fromarray(np.ascontiguousarray(codes[..., ch], np.float32), mode="F")
        out[..., ch] = np.asarray(im.resize((target_w, target_h), Image.Resampling.BILINEAR))
    return out


def pair_sensor_strips(
    render: Any,
    shot: dict[Any, Any],
    max_edge: int | None,
) -> tuple[np.ndarray, np.ndarray, int, int, str, float]:
    """从两个单 DNG 代理缓存中取半格条带；按 max_edge 缩放到代理几何并缓存条带。"""
    ori, first_half = pair_orientation_and_half(shot)
    s1, s2 = pair_half_shots(shot)
    d1, d2 = s1["files"]["dng"], s2["files"]["dng"]

    t0 = time.perf_counter()
    decodes_before = render.stats["decodes"]

    def _get_strip(dng_rec: dict[str, Any]) -> tuple[np.ndarray, int]:
        sha = dng_rec["sha256"]
        cache_key = (sha, "hf_strip", max_edge)
        cached = render._proxies.get(cache_key)
        if cached is not None:
            arr = cast(np.ndarray, cached)
            full_w = max_edge if (max_edge is not None and max_edge < FULL_WIDTH // 2) else (FULL_WIDTH // 2)
            # 按缓存条带所在目标宽度重算 seam_w
            base = render._proxies.get((sha, "sensor"))
            if base is not None:
                bh, bw = base.shape[:2]
                if max_edge and max(bh, bw) > max_edge:
                    scale = max_edge / max(bh, bw)
                    full_w = max(1, round(bw * scale))
                else:
                    full_w = bw
            return arr, proxy_geometry(full_w).seam_w
        base = render.proxy_sensor_codes(dng_rec)
        bh, bw = base.shape[:2]
        src_geo = proxy_geometry(bw)
        strip = np.ascontiguousarray(base[:, src_geo.strip_x0 : src_geo.strip_x0 + src_geo.half_w], np.float32)
        seam_w = src_geo.seam_w
        if max_edge and max(bh, bw) > max_edge:
            scale = max_edge / max(bh, bw)
            nw, nh = max(1, round(bw * scale)), max(1, round(bh * scale))
            dst_geo = proxy_geometry(nw)
            strip = _resize_codes(strip, nh, dst_geo.half_w)
            seam_w = dst_geo.seam_w
        render._proxies.put(cache_key, strip, strip.nbytes)
        return strip, seam_w

    strip1, seam1 = _get_strip(d1)
    strip2, seam2 = _get_strip(d2)
    decode_ms = (time.perf_counter() - t0) * 1000 if render.stats["decodes"] != decodes_before else 0.0
    if strip1.shape != strip2.shape or seam1 != seam2:
        raise RawDecodeError("半格一对的两张 RAW 尺寸不一致")
    return strip1, strip2, seam1, ori, first_half, decode_ms


def _apply_half_codes(
    render: Any,
    plan: Any,
    codes: np.ndarray,
    gains: Any,
    table: Any,
    *,
    exact: bool,
) -> tuple[np.ndarray, float, float]:
    from .render.service import PREVIEW_STEP

    t = time.perf_counter()
    if exact:
        rgb = chain.exact_u8(plan.model or render.model(plan.model_iso, plan.unit), plan.entry, codes, gains, table)
        return rgb, 0.0, (time.perf_counter() - t) * 1000
    bakes = render.stats["bakes"]
    bk = render._bake(plan, PREVIEW_STEP)
    bake_ms = (time.perf_counter() - t) * 1000 if render.stats["bakes"] != bakes else 0.0
    t_apply = time.perf_counter()
    rgb = chain.chroma_u8(codes, bk, gains, table)
    return rgb, bake_ms, (time.perf_counter() - t_apply) * 1000


def pair_render_preview(
    render: Any,
    shot: dict[Any, Any],
    edit: EditState,
    *,
    max_edge: int | None = None,
    exact: bool = False,
    crop: cropmod.Crop | None = None,
    quality: int = 88,
) -> Any:
    """半格一对的代理渲染路径（对应 RenderService.render_preview）。"""
    from .export import crop_image
    from .render.service import ENGINE, PREVIEW_STEP, RenderOut, _encode_jpeg

    ori, first_half = pair_orientation_and_half(shot)
    plan1, plan2, s1, s2, _e1, _e2 = pair_plans(render, shot, edit)
    plan = combined_plan(plan1, plan2)
    parts = {
        "pair": [s1["files"]["dng"]["sha256"], s2["files"]["dng"]["sha256"]],
        "first_half": first_half,
        "orientation": ori,
        "adjust": edit.adjust.to_json(),
        "recipe_entries": [plan1.entry_sha256, plan2.entry_sha256],
        "model_isos": [plan1.model_iso, plan2.model_iso],
        "model_units": [plan1.unit, plan2.unit],
        "bindings": [getattr(plan1, "binding_sha256", ""), getattr(plan2, "binding_sha256", "")],
        "curve": edit.curve.to_json() if edit.curve else None,
        "engine": ENGINE,
        "width": max_edge,
        "exact": exact,
        "step": PREVIEW_STEP,
        "crop": crop.to_json() if crop else None,
        "quality": quality,
    }
    key = hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    hit = render._results.get((key, quality))
    if hit is not None:
        render.stats["result_hits"] += 1
        jpeg, w, h = hit
        return RenderOut(jpeg, w, h, key, True, plan, {"total": 0.0})

    t0 = time.perf_counter()
    with render._gate:
        strip1, strip2, seam_w, ori, first_half, decode_ms = pair_sensor_strips(render, shot, max_edge)
        gains = channel_gains(edit.adjust)
        table = display_table(edit.adjust, edit.curve)
        rgb1, bake1, apply1 = _apply_half_codes(render, plan1, strip1, gains, table, exact=exact)
        rgb2, bake2, apply2 = _apply_half_codes(render, plan2, strip2, gains, table, exact=exact)
        t_comp = time.perf_counter()
        rgb = compose_strips(rgb1, rgb2, first_half=first_half, seam_w=seam_w, orientation=ori)
        if crop is not None:
            rgb = np.asarray(crop_image(Image.fromarray(rgb, "RGB"), crop))
        jpeg = _encode_jpeg(rgb, quality)
        encode_ms = (time.perf_counter() - t_comp) * 1000
    ms = {
        "decode": decode_ms,
        "bake": bake1 + bake2,
        "apply": apply1 + apply2,
        "encode": encode_ms,
        "total": (time.perf_counter() - t0) * 1000,
    }
    render.stats["renders"] += 1
    h, w = rgb.shape[:2]
    render._results.put((key, quality), (jpeg, w, h), len(jpeg))
    return RenderOut(jpeg, w, h, key, False, plan, ms)


def pair_lut_base(
    render: Any,
    lib: Any,
    shot: dict[Any, Any],
    max_edge: int,
    colour_of: Callable[[dict[str, Any]], Any],
) -> tuple[np.ndarray, np.ndarray | None, str]:
    """LUT 预览/试图的半格一对基准：(sRGB float, 场景 XYZ 或 None, 模式说明)。"""
    from .lut.errors import LutError
    from .render.edits import read as read_edit
    from .render.service import RenderUnavailable

    try:
        strip1, strip2, seam_w, ori, first_half, _decode_ms = pair_sensor_strips(render, shot, max_edge)
    except Exception as exc:  # noqa: BLE001
        raise LutError(422, "raw_unreadable", "无法读取这张照片。文件可能已损坏。", {"detail": str(exc)}) from exc
    s1, s2 = pair_half_shots(shot)
    c1, c2 = colour_of(s1["files"]["dng"]), colour_of(s2["files"]["dng"])
    xyz: np.ndarray | None = None
    if c1 is not None and c2 is not None:
        xyz1 = c1.xyz(strip1.reshape(-1, 3)).reshape(strip1.shape)
        xyz2 = c2.xyz(strip2.reshape(-1, 3)).reshape(strip2.shape)
        xyz = compose_strips(xyz1, xyz2, first_half=first_half, seam_w=seam_w, orientation=ori)
    edit, _side = read_edit(lib, shot, None)
    rgb_halves: list[np.ndarray] = []
    mode = "neutral"
    try:
        for half_shot, strip in ((s1, strip1), (s2, strip2)):
            half_edit = half_edit_for(render, half_shot, edit)
            if half_edit.recipe is not None:
                rgb_halves.append(render.renderer(half_shot, EditState(recipe=half_edit.recipe)).display(strip))
                mode = "recipe-prediction"
            else:
                base = render.default_look(half_shot, strip)
                if base is not None:
                    rgb_halves.append(base)
                    if mode == "neutral":
                        mode = "04-simulation"
                else:
                    rgb_halves.append(NEUTRAL.display(strip))
    except RenderUnavailable:
        if edit.recipe is not None:
            raise LutError(409, "camera_prediction_unavailable", "无法生成拍摄配方的电脑预测。请检查相机配方文件和校准数据。") from None
        rgb_halves = [NEUTRAL.display(strip1), NEUTRAL.display(strip2)]
        mode = "neutral"
    except EditError as exc:
        raise LutError(422, exc.code, exc.message) from exc
    rgb = compose_strips(rgb_halves[0], rgb_halves[1], first_half=first_half, seam_w=seam_w, orientation=ori)
    return rgb.astype(float), xyz, mode


def _camera_jpg_parts(lib: Any, shot: dict[Any, Any], detail: str = "") -> tuple[np.ndarray, np.ndarray, np.ndarray, bool, dict[str, str]]:
    from .finish.compose import load_jpeg

    ori, first_half = pair_orientation_and_half(shot)
    s1, s2 = pair_half_shots(shot)
    single1, single2 = camera_jpg_singles(
        lib.abspath(s1["files"]["jpg"]["relpath"]),
        lib.abspath(s2["files"]["jpg"]["relpath"]),
        first_half=first_half,
        orientation=ori,
    )
    diptych = load_jpeg(lib.abspath(shot["files"]["jpg"]["relpath"]), None)
    labels = {
        "kind": "original",
        "label": "original",
        "text": "原图",
        "id": "camera-jpg",
        "tag": "",
        "description": "原图",
        "detail": detail,
        "calibrated": "0",
        "cache": "miss",
        "key": "",
    }
    return diptych, single1, single2, False, labels


def render_pair_export_parts(
    render: Any,
    lib: Any,
    shot: dict[Any, Any],
    edit: EditState,
    *,
    base: str = "auto",
) -> tuple[np.ndarray, np.ndarray, np.ndarray, bool, dict[str, str]]:
    """一次去马赛克同时产出双联与两张单格底图（均已转正）：(diptych, single1, single2, from_raw, labels)。"""
    from .lut.errors import LutError
    from .lut.photo import _apply as lut_apply
    from .lut.scene import SceneError
    from .lut.spaces import DISPLAY
    from .render.params import Adjust
    from .render.service import RenderUnavailable

    if edit.lut is not None:
        try:
            target, item = render.luts.target(edit.lut)
            if target.input_space in DISPLAY:
                stripped = replace(edit, lut=None, adjust=Adjust(), curve=None)
                try:
                    dip, s1_rgb, s2_rgb, _fr, _lbl = render_pair_export_parts(render, lib, shot, stripped, base=base)
                except RenderUnavailable:
                    if edit.recipe is not None:
                        raise
                    dip, s1_rgb, s2_rgb, _fr, _lbl = render_pair_export_parts(render, lib, shot, EditState(), base="neutral")
                dip = lut_apply(target, dip)
                s1_rgb = lut_apply(target, s1_rgb)
                s2_rgb = lut_apply(target, s2_rgb)
            else:
                strip1, strip2, seam_w, first_half, ori = render_pair_lut_scene_strips(lib, shot, target, lut_apply, full=True)
                dip = compose_strips(strip1, strip2, first_half=first_half, seam_w=seam_w, orientation=ori)
                s1_rgb = orient_half(strip1, ori)
                s2_rgb = orient_half(strip2, ori)
        except LutError as exc:
            raise EditError(exc.code, exc.message) from exc
        except SceneError as exc:
            raise EditError(exc.code, "无法换算输入色彩空间。请检查 RAW 文件。") from exc
        return (
            dip,
            s1_rgb,
            s2_rgb,
            False,
            {
                "kind": "prediction",
                "label": "prediction",
                "text": "电脑预测",
                "id": "lut-prediction",
                "tag": "",
                "description": f"GXR Studio computer prediction: LUT {edit.lut}, {item['input_space']} to {item['output_space']}",
                "detail": "",
                "key": "",
                "cache": "miss",
                "calibrated": "0",
            },
        )
    if base == "camera-jpg":
        return _camera_jpg_parts(lib, shot)
    if base == "neutral":
        strip1, strip2, seam_w, first_half, ori, labels = render_pair_strips(render, lib, shot, EditState(), full=True, neutral=True)
        return (
            compose_strips(strip1, strip2, first_half=first_half, seam_w=seam_w, orientation=ori),
            orient_half(strip1, ori),
            orient_half(strip2, ori),
            False,
            labels,
        )
    try:
        strip1, strip2, seam_w, first_half, ori, labels = render_pair_strips(render, lib, shot, edit, full=True, neutral=False)
    except (RenderUnavailable, RawDecodeError) as exc:
        if edit.recipe is not None and edit.recipe.kind == "local":
            raise
        if "jpg" not in shot.get("files", {}):
            raise
        return _camera_jpg_parts(lib, shot, detail=f"没有全尺寸传感器数据。已改用相机 JPG。{exc}")
    return (
        compose_strips(strip1, strip2, first_half=first_half, seam_w=seam_w, orientation=ori),
        orient_half(strip1, ori),
        orient_half(strip2, ori),
        True,
        labels,
    )
