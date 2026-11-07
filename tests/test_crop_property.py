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

import pytest

from gxr_studio import crop as cropmod
from gxr_studio.crop import CropError
from gxr_studio.finish.geometry import max_area_rect, output_size, warp_valid, window_full
from gxr_studio.finish.spec import parse_geometry
from gxr_studio.library import edits
from gxr_studio.library import sidecar as sc
from gxr_studio.render import edits as render_edits

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
