"""裁剪写入的性质测试（种子固定，纯 Python，不起 node）。

真的 `edits.write_crop`（普通写入、`fit` 写入）和真的几何写入（`render/edits.write`），对着真的图库里的一张照片，
随机的画布（互质的、奇数的，加上透视 / 畸变拉开之后 `output_size` 给出的任意尺寸）、每个预设、横竖、翻转、
拉直 / 透视 / 畸变、从极小到整张画面的目标。每个用例断言：

1. 存下来的框通得过严格的 `validate_crop`（用存下来的画布），并且整个落在真正的有效蒙版里；
2. 普通写入收得下的框，`fit` 写入的结果和普通写入的**一模一样**（`fit` 只在普通写入会被拒的时候才改成最近的合格框）；
3. `fit` 的结果不比目标大（目标极小的除外），短边不小于 16 像素；
4. 把存下来的框原样再写一遍（普通写入）会被接受，而且不变；
5. 之后再改几何（拉开、收回、拉直，不含转 90°），服务器顺带收过的框仍然通得过严格校验、落在有效蒙版里。

画布取得小（150–420 像素），为的是几千个用例在半分钟内跑完；用的都是真代码，小画布只是让蒙版算得快。
用法：`GXR_PROP_CASES=数量 GXR_PROP_SEED=种子`。
"""

from __future__ import annotations

import math
import os
import random
from typing import Any

import numpy as np
import pytest

from gxr_studio import crop as cropmod
from gxr_studio.crop import CropError
from gxr_studio.finish import geometry as geometry_mod
from gxr_studio.finish.geometry import max_area_rect, output_size, warp_valid, window_full
from gxr_studio.finish.spec import parse_geometry
from gxr_studio.library import edits
from gxr_studio.library import sidecar as sc
from gxr_studio.render import edits as render_edits
from support.speed import assert_fast

CASES = int(os.environ.get("GXR_PROP_CASES", "250"))
SEED = int(os.environ.get("GXR_PROP_SEED", "1"))
PRESETS = list(cropmod.PRESETS)


def random_geometry(r: random.Random, *, keep_rotation: int | None = None) -> dict[str, Any]:
    def maybe(lo: int, hi: int) -> int:
        return r.randint(lo, hi) if r.random() < 0.45 else 0

    return {
        "straighten": r.choice([0.0, 0.0, 0.5, -2.0, 4.0, -7.5, 12.0, 25.0]),
        "vertical": maybe(-100, 100),
        "horizontal": maybe(-100, 100),
        "distortion": maybe(-100, 100),
        "rotate90": r.randint(0, 3) if keep_rotation is None else keep_rotation,
        "flip": r.random() < 0.3,
    }


def ratio_target(r: random.Random, preset: str, orientation: str, w: int, h: int) -> list[float]:
    """比例对的框（像素上正好是预设的比例），大小、位置随机：普通写入多半收得下。"""
    ratio = cropmod.target_ratio(preset, w, h)
    aspect = float(ratio) if ratio is not None else r.uniform(0.5, 2.0)
    if ratio is not None and (aspect >= 1) != (orientation == "landscape"):
        aspect = 1 / aspect
    bw = r.uniform(0.1, 1.0) * w
    bh = bw / aspect
    if bh > h:
        bh = h * r.uniform(0.1, 1.0)
        bw = bh * aspect
    nw, nh = min(bw / w, 1.0), min(bh / h, 1.0)
    return [r.uniform(0, 1 - nw), r.uniform(0, 1 - nh), nw, nh]


def random_target(r: random.Random) -> list[float]:
    kind = r.choice(["tiny", "small", "medium", "full", "any", "any"])
    if kind == "full":
        return [0.0, 0.0, 1.0, 1.0]
    size = {"tiny": (0.002, 0.03), "small": (0.05, 0.25), "medium": (0.25, 0.7), "any": (0.03, 1.0)}[kind]
    w, h = r.uniform(*size), r.uniform(*size)
    return [r.uniform(0, 1 - w), r.uniform(0, 1 - h), w, h]


def smallest_box(preset: str, w: int, h: int, landscape: bool) -> tuple[int, int]:
    """这个预设在这块画布上能收出的最小合格框（短边不小于 16 像素）：定比例的是比例分子分母的整数倍，原始 / 自由按浮点比例。"""
    ratio = cropmod.target_ratio(preset, w, h)
    if preset == "original" or ratio is None:
        aspect = w / h if preset == "original" else 1.0
        long_side = math.ceil(cropmod.MIN_SIDE_PX * max(aspect, 1 / aspect)) + 1
        short_side = cropmod.MIN_SIDE_PX + 1
    else:
        small, big = sorted((ratio.numerator, ratio.denominator))
        k = -(-cropmod.MIN_SIDE_PX // small)
        long_side, short_side = k * big, k * small
    return (long_side, short_side) if landscape else (short_side, long_side)


def a_legal_box_exists(valid: Any, preset: str, w: int, h: int) -> bool:
    """有效区里放不放得下这个预设的最小框（拿最大内接矩形粗略判断）。放不下，服务器只能尽力而为，性质不再要求。"""
    _x, _y, fw, fh = max_area_rect(valid)
    a, b = smallest_box(preset, w, h, True)
    return (fw >= a and fh >= b) or (fw >= b and fh >= a)


def stored(lib: Any, shot: dict[Any, Any]) -> tuple[dict[Any, Any], Any, int, int, int, int]:
    side, _ = lib.read_sidecar_or_default(shot)
    geom = parse_geometry(side["geometry"]) if isinstance(side.get("geometry"), dict) else parse_geometry({})
    sw, sh = side["frame"]["width"], side["frame"]["height"]
    w, h = output_size(sw, sh, geom)
    return side, geom, sw, sh, w, h


def stored_problems(lib: Any, shot: dict[Any, Any]) -> list[str]:
    side, geom, sw, sh, w, h = stored(lib, shot)
    raw = side.get("crop")
    if not raw:
        return []
    out: list[str] = []
    try:
        cropmod.validate_crop(
            raw["preset"], raw["rect"], float(raw.get("angle_deg", 0.0)), w, h, mirrored=geom.flip, check_region=not geom.expands()
        )
    except CropError as exc:
        out.append(f"存下来的框通不过 validate_crop：{exc.code}")
    x, y, bw, bh = cropmod.crop_box(tuple(raw["rect"]), w, h)  # type: ignore[arg-type]
    valid = warp_valid(sw, sh, geom)
    if not (window_full(valid, x, y, bw, bh) or window_full(valid, x + 1, y + 1, max(1, bw - 2), max(1, bh - 2))):
        out.append("存下来的框不在有效蒙版里")
    return out


@pytest.fixture
def photo(volumes, card, lib):
    from gxr_studio.cards.discovery import find_cards
    from gxr_studio.cards.importer import ImportOptions, run_import

    run_import(lib, find_cards([volumes])[0], ImportOptions(min_free_bytes=0))
    shot = next(s for s in lib.list_shots(limit=100) if s["name"] == "R0000102")
    return shot


def test_random_writes_keep_the_stored_box_valid_and_fit_equals_plain_when_plain_works(lib, photo):
    shot = photo
    path = lib.sidecar_path_for(shot)
    r = random.Random(SEED)
    problems: list[str] = []
    counts = {"plain_ok": 0, "fit_only": 0, "fit_error": 0, "geometry_after": 0}

    def reset(base: dict[Any, Any]) -> None:
        import copy

        sc.write_atomic(path, copy.deepcopy(base), tmp_dir=lib.tmp_dir)

    for case in range(CASES):
        label = f"case {case} (seed {SEED})"
        side = lib.read_sidecar_for_write(shot)
        sw, sh = r.randint(150, 420), r.randint(110, 360)
        side["frame"] = {"width": sw, "height": sh}
        side["crop"] = None
        side.pop("geometry", None)
        sc.write_atomic(path, side, tmp_dir=lib.tmp_dir)
        geometry = random_geometry(r)
        render_edits.write(lib, shot, {"geometry": geometry}, None)  # type: ignore[arg-type]
        base = lib.read_sidecar_for_write(shot)
        preset = r.choice(PRESETS)
        orientation = r.choice(["landscape", "portrait"])
        target = None
        _side, geom, _sw, _sh, w, h = stored(lib, shot)
        angle = float(geometry["straighten"])
        target = ratio_target(r, preset, orientation, w, h) if r.random() < 0.5 else random_target(r)
        info = f"{label}: 画布 {sw}×{sh}→{w}×{h} 几何 {geometry} {preset}/{orientation} 目标 {[round(v, 4) for v in target]}"

        reset(base)
        try:
            plain = edits.write_crop(lib, shot, preset=preset, rect=target, angle_deg=angle, orientation=orientation)
        except CropError as exc:
            plain = None
            plain_error = exc.code
        else:
            plain_error = None
            counts["plain_ok"] += 1
            for p in stored_problems(lib, shot):
                problems.append(f"{info}: 普通写入 {p}")

        reset(base)
        try:
            fitted = edits.write_crop(lib, shot, preset=preset, rect=target, angle_deg=angle, orientation=orientation, fit=True)
        except CropError as exc:
            fitted = None
            counts["fit_error"] += 1
            if exc.code != "too_small":
                problems.append(f"{info}: fit 写入报了 {exc.code}")
        if fitted is not None:
            if plain is not None:
                if fitted["rect"] != plain["rect"]:
                    problems.append(f"{info}: 普通写入收得下，fit 的结果却不一样：普通 {plain['rect']} fit {fitted['rect']}")
            else:
                counts["fit_only"] += 1
            for p in stored_problems(lib, shot):
                problems.append(f"{info}: fit 写入 {p}")
            px = fitted["pixels"]
            if min(px["width"], px["height"]) < cropmod.MIN_SIDE_PX:
                problems.append(f"{info}: fit 的结果短边 {min(px['width'], px['height'])} 像素，小于 {cropmod.MIN_SIDE_PX}")
            tw, th = target[2] * w, target[3] * h
            floor_w, floor_h = smallest_box(preset, w, h, tw >= th)
            if tw >= 2 * floor_w and th >= 2 * floor_h and plain is None and (px["width"] > tw + 2 or px["height"] > th + 2):
                problems.append(f"{info}: fit 的结果比目标大：目标 {tw:.0f}×{th:.0f}，结果 {px['width']}×{px['height']}")
            # 原样再写一遍（普通写入）：接受、不变
            before = lib.read_sidecar_for_write(shot)["crop"]
            try:
                edits.write_crop(lib, shot, preset=fitted["preset"], rect=fitted["rect"], angle_deg=angle, orientation=orientation)
            except CropError as exc:
                problems.append(f"{info}: 存下来的框原样再写一遍被拒：{exc.code}（{before['rect']}）")
            else:
                after = lib.read_sidecar_for_write(shot)["crop"]
                if any(abs(a - b) > 1e-6 for a, b in zip(after["rect"], before["rect"], strict=True)):
                    problems.append(f"{info}: 存下来的框原样再写一遍就变了：{before['rect']} → {after['rect']}")
            # 再改几何（不转 90°）：服务器顺带收过的框仍然合格
            moved = random_geometry(r, keep_rotation=int(geometry["rotate90"]))
            render_edits.write(lib, shot, {"geometry": moved}, None)  # type: ignore[arg-type]
            counts["geometry_after"] += 1
            _s2, geom2, sw2, sh2, w2, h2 = stored(lib, shot)
            if a_legal_box_exists(warp_valid(sw2, sh2, geom2), preset, w2, h2):
                for p in stored_problems(lib, shot):
                    problems.append(f"{info}: 改几何成 {moved} 之后 {p}")
        if plain is None and plain_error is None:
            problems.append(f"{info}: 脚本错误")
        _ = (geom, angle)

    assert counts["plain_ok"] > CASES // 4 and counts["fit_only"] > CASES // 10, counts
    assert counts["fit_error"] < CASES // 20, f"fit 报 too_small 的太多：{counts}"
    assert not problems, f"{len(problems)} 处问题（{counts}）：\n" + "\n".join(problems[:12])


# ---- 收得不比必要的更小：对着穷举的参照 ------------------------------------------------


def all_windows(valid: np.ndarray, tw: int, th: int) -> np.ndarray:
    """穷举：每个左上角位置上 tw×th 的窗口是不是整窗有效。用滑动窗口取 all，和被测代码的积分图是两套算法。"""
    height, width = valid.shape
    if tw > width or th > height:
        return np.zeros((0, 0), bool)
    return np.asarray(np.lib.stride_tricks.sliding_window_view(valid, (th, tw)).all(axis=(2, 3)))


def brute_nearest(valid: np.ndarray, tw: int, th: int, cx: float, cy: float) -> tuple[float, tuple[int, int]] | None:
    ok = all_windows(valid, tw, th)
    ys, xs = np.nonzero(ok)
    if ys.size == 0:
        return None
    dist = (xs + tw / 2.0 - cx) ** 2 + (ys + th / 2.0 - cy) ** 2
    i = int(np.argmin(dist))
    return float(dist[i]), (int(xs[i]), int(ys[i]))


def small_mask(r: random.Random) -> np.ndarray:
    sw, sh = r.randint(60, 120), r.randint(50, 100)
    geom = parse_geometry(random_geometry(r))
    return warp_valid(sw, sh, geom)


def test_nearest_window_is_the_nearest_of_every_position_not_a_sample():
    r = random.Random(SEED)
    checked = 0
    for _ in range(60):
        valid = small_mask(r)
        height, width = valid.shape
        tw, th = r.randint(2, max(2, width // 2)), r.randint(2, max(2, height // 2))
        cx, cy = r.uniform(-5, width + 5), r.uniform(-5, height + 5)
        sheet = geometry_mod._valid_integral(valid)
        got = geometry_mod._nearest_window(sheet, tw, th, cx, cy)
        want = brute_nearest(valid, tw, th, cx, cy)
        if want is None:
            assert got is None
            continue
        assert got is not None
        gx, gy, gw, gh = got
        assert (gw, gh) == (tw, th) and valid[gy : gy + gh, gx : gx + gw].all()
        assert abs(((gx + tw / 2.0 - cx) ** 2 + (gy + th / 2.0 - cy) ** 2) - want[0]) < 1e-6, (got, want)
        checked += 1
    assert checked > 20


RATIOS = [(1, 1), (3, 2), (16, 9), (4, 5), (191, 100), (65, 24)]


def test_a_fixed_ratio_box_is_the_largest_whole_multiple_that_fits_anywhere():
    """fit_ratio_in_valid 给出的是目标大小以内、整窗落在有效像素里的最大整数倍（穷举每个位置核对），不是随便一个更小的。"""
    r = random.Random(SEED + 1)
    checked = 0
    for _ in range(120):
        valid = small_mask(r)
        height, width = valid.shape
        numer, denom = r.choice(RATIOS)
        tw0, th0 = r.uniform(0.1, 1.0) * width, r.uniform(0.1, 1.0) * height
        rect = (r.uniform(0, 1 - tw0 / width), r.uniform(0, 1 - th0 / height), tw0 / width, th0 / height)
        limit_w, limit_h = min(width, max(1, round(rect[2] * width))), min(height, max(1, round(rect[3] * height)))
        max_k = min(limit_w // numer, limit_h // denom)
        best = next((k for k in range(max_k, 0, -1) if all_windows(valid, k * numer, k * denom).any()), 0)
        if best == 0:
            continue
        x, y, bw, bh = geometry_mod.fit_ratio_in_valid(valid, rect, numer, denom)
        assert (bw, bh) == (best * numer, best * denom), f"{valid.shape} {numer}:{denom} 目标 {rect}：最大是 {best} 倍，得到 {bw}×{bh}"
        assert valid[y : y + bh, x : x + bw].all()
        checked += 1
    assert checked > 40


def test_an_original_box_is_not_shrunk_more_than_needed():
    """原始 / 自由的框（clamp_norm_rect）：整窗落在有效区里，高度不比穷举找到的最大的（同样比例、同样取整）小超过 2 像素。"""
    r = random.Random(SEED + 2)
    checked = 0
    for _ in range(80):
        valid = small_mask(r)
        height, width = valid.shape
        aspect = width / height
        ph0 = r.randint(max(2, height // 4), height)
        pw0 = max(1, round(ph0 * aspect))
        pw0, ph0 = min(pw0, width), min(ph0, height)
        rect = (r.randint(0, width - pw0) / width, r.randint(0, height - ph0) / height, pw0 / width, ph0 / height)
        min_h = max(2, math.ceil(16 * max(1.0, 1 / aspect)))
        best = 0
        for th in range(ph0, min_h - 1, -1):
            tw, th2 = geometry_mod._snap_to_aspect(th, aspect, floor=min_h)
            if th2 <= ph0 and tw <= width and all_windows(valid, tw, th2).any():
                best = th2
                break
        if not best:
            continue
        out = geometry_mod.clamp_norm_rect(valid, rect, aspect)
        x, y, bw, bh = (round(out[0] * width), round(out[1] * height), round(out[2] * width), round(out[3] * height))
        assert valid[y : y + bh, x : x + bw].all(), (valid.shape, rect, out)
        assert bh >= best - 2, f"{valid.shape} 目标 {rect}：穷举找到高 {best}，得到 {bw}×{bh}"
        checked += 1
    assert checked > 25


# ---- 大画布（真实照片是 4288×2848）-----------------------------------------------------


@pytest.mark.exclusive  # 大画布上找整窗要在 3 秒内结束
@pytest.mark.parametrize(("width", "height"), [(2200, 1600), (4288, 2848)])
def test_the_whole_lattice_is_searched_on_large_masks(width, height):
    """以前按步长抽样位置，2200×1600 上步长是 2：只在 (1, 1) 有一个整窗有效的 16×16 就找不到。"""
    only = np.zeros((height, width), bool)
    only[1:17, 1:17] = True
    sheet = geometry_mod._valid_integral(only)
    assert geometry_mod._nearest_window(sheet, 16, 16, width / 2, height / 2) == (1, 1, 16, 16)
    out = geometry_mod.clamp_norm_rect(only, (0.4, 0.4, 0.3, 0.3), 1.0)
    assert [round(v * width) for v in (out[0], out[2])] == [1, 16] and [round(v * height) for v in (out[1], out[3])] == [1, 16]
    # 目标中心在有效区外：同样比例的框挪到最近的整窗有效的位置，不放大
    big = np.ones((height, width), bool)
    big[: height // 2, : width // 2] = False
    x, y, bw, bh = geometry_mod.fit_ratio_in_valid(big, (0.0, 0.0, 0.4, 0.4), 3, 2)
    assert big[y : y + bh, x : x + bw].all() and bw * 2 == bh * 3 and bw <= 0.4 * width + 3
    import time

    started = time.perf_counter()
    geometry_mod._nearest_window(geometry_mod._valid_integral(big), width // 2, height // 2, 10.0, 10.0)
    assert_fast(time.perf_counter() - started, 3.0, "大画布找最近整窗（秒）")


def test_large_canvas_writes_store_boxes_the_strict_check_accepts(lib, photo):
    """2200×1600 的真实写入（拉直 + 透视，目标小到整张）：存下来的框通得过严格校验，原样再写不变。"""
    r = random.Random(SEED + 3)
    shot = photo
    path = lib.sidecar_path_for(shot)
    problems: list[str] = []
    for case in range(4):
        side = lib.read_sidecar_for_write(shot)
        side["frame"] = {"width": 2200, "height": 1600}
        side["crop"] = None
        side.pop("geometry", None)
        sc.write_atomic(path, side, tmp_dir=lib.tmp_dir)
        geometry = {**random_geometry(r), "rotate90": 0, "straighten": r.choice([0.5, 4.0, -7.5]), "vertical": r.choice([0, 40, -60])}
        render_edits.write(lib, shot, {"geometry": geometry}, None)  # type: ignore[arg-type]
        _side, _geom, _sw, _sh, w, h = stored(lib, shot)
        preset = r.choice(["original", "free", "3:2", "16:9"])
        target = random_target(r)
        try:
            edits.write_crop(lib, shot, preset=preset, rect=target, angle_deg=float(geometry["straighten"]), fit=True)
        except CropError as exc:
            problems.append(f"case {case} {geometry} {preset} {target}: {exc.code}")
            continue
        problems += [f"case {case} {geometry} {preset} {target} ({w}×{h}): {p}" for p in stored_problems(lib, shot)]
    assert not problems, "\n".join(problems)
