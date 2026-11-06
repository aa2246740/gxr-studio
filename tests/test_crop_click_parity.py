"""点比例时界面先画的框（crop.js presetRect → geom.js centeredMaxRect）和后端写下的框（edits.write_crop）是同一框。

PR #39 的复审（Grok）：转过 90°、拉直以后两边不一样，保存回来框会跳，横竖还会反过来
（横向、纵向在后端按旋转之前的原图算，拉直后收框时又按画布的方向重算）。这里每个预设、两个方向，
在不旋转、转 90°、拉直、转 90° 再拉直、转 180° 加翻转再拉直下比较：横竖相同，按全尺寸像素差不超过 4
（4288 宽的 0.1%；拉直以后后端的有效区按像素栅格化，边上会少一两行，实测最多差 3）。
透视、畸变不在这里：那时有效区只有后端知道，界面先画近似框，后端回来再换（见 presetRect 的注释）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from gxr_studio.library import edits
from gxr_studio.library import sidecar as sc

ASSETS = Path(__file__).resolve().parents[1] / "web" / "assets"
FRAME = (4288, 2848)
TOLERANCE_PX = 4.0
FLAT = {"straighten": 0.0, "vertical": 0, "horizontal": 0, "distortion": 0, "rotate90": 0, "flip": False}
# (预设, 界面上能不能横竖切换) —— 和 crop.js 的 ORIENTABLE 一致
PRESETS = [
    ("original", False),
    ("free", False),
    ("3:2", True),
    ("4:3", True),
    ("1:1", False),
    ("4:5", True),
    ("16:9", True),
    ("16:10", False),
    ("1.91:1", True),
    ("2.35:1", False),
    ("xpan-65-24", True),
]
GEOMETRIES = [
    {},
    {"rotate90": 1},
    {"straighten": 4.0},
    {"rotate90": 1, "straighten": -7.5},
    {"rotate90": 2, "flip": True, "straighten": 15.0},
]

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过前端对照")


def cases() -> list[dict]:
    out = []
    for geometry in GEOMETRIES:
        for preset, orientable in PRESETS:
            for orientation in ("landscape", "portrait") if orientable else ("landscape",):
                out.append({"preset": preset, "orientable": orientable, "orientation": orientation, "geometry": geometry})
    return out


def browser_boxes(tmp_path: Path, items: list[dict]) -> list[dict]:
    """照 presetRect 的算法（没有透视、畸变的那一支），在 node 里跑真正的 geom.js。"""
    (tmp_path / "geom.js").write_text((ASSETS / "geom.js").read_text(encoding="utf-8"), encoding="utf-8")
    script = tmp_path / "parity.mjs"
    script.write_text(
        "import * as G from './geom.js';\n"
        f"const frame = {{ w: {FRAME[0]}, h: {FRAME[1]} }};\n"
        f"const items = {json.dumps(items)};\n"
        "console.log(JSON.stringify(items.map((it) => {\n"
        "  const g = it.geometry;\n"
        "  const full = G.canvasAfterRotation(frame, g.rotate90 || 0);\n"
        "  const pair = G.landscapePair(it.preset);\n"
        "  const L = pair ? pair[0] / pair[1] : null;\n"
        "  const ratio = L == null ? null : it.orientable && it.orientation === 'portrait' ? 1 / L : L;\n"
        "  const r = G.centeredMaxRect(full, ratio, g.straighten || 0, pair);\n"
        "  return { canvas: full, rect: [r.x, r.y, r.w, r.h] };\n"
        "})));\n",
        encoding="utf-8",
    )
    res = subprocess.run(["node", str(script)], capture_output=True, text=True, cwd=tmp_path, encoding="utf-8")  # noqa: S603, S607
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)  # type: ignore[no-any-return]


def test_the_box_drawn_on_click_is_the_box_the_server_writes(volumes, card, lib, tmp_path):
    from gxr_studio.cards.discovery import find_cards
    from gxr_studio.cards.importer import ImportOptions, run_import

    run_import(lib, find_cards([volumes])[0], ImportOptions(min_free_bytes=0))
    shot = next(s for s in lib.list_shots(limit=100) if s["name"] == "R0000102")
    path = lib.sidecar_path_for(shot)
    items = cases()
    local = browser_boxes(tmp_path, items)
    bad = []
    for item, mine in zip(items, local, strict=True):
        side = lib.read_sidecar_for_write(shot)
        side["frame"] = {"width": FRAME[0], "height": FRAME[1]}
        side["geometry"] = {**FLAT, **item["geometry"]}
        side["crop"] = None
        sc.write_atomic(path, side, tmp_dir=lib.tmp_dir)
        out = edits.write_crop(
            lib,
            shot,
            preset=item["preset"],
            rect=None,
            angle_deg=float(item["geometry"].get("straighten", 0.0)),
            orientation=item["orientation"],
        )
        cw, ch = mine["canvas"]["w"], mine["canvas"]["h"]
        theirs = [out["rect"][0] * cw, out["rect"][1] * ch, out["rect"][2] * cw, out["rect"][3] * ch]
        drawn = [mine["rect"][0] * cw, mine["rect"][1] * ch, mine["rect"][2] * cw, mine["rect"][3] * ch]
        off = max(abs(a - b) for a, b in zip(drawn, theirs, strict=True))
        if off > TOLERANCE_PX or (drawn[2] >= drawn[3]) != (theirs[2] >= theirs[3]):
            bad.append({**item, "drawn": [round(v, 1) for v in drawn], "written": [round(v, 1) for v in theirs], "off_px": round(off, 1)})
    assert not bad, json.dumps(bad, ensure_ascii=False, indent=1)


DRAG_GEOMETRIES = [
    {"straighten": 8.0},
    {"straighten": 8.0, "flip": True},
    {"rotate90": 1, "straighten": -6.0, "flip": True},
    {"rotate90": 3, "straighten": 12.0},
]
DRAG_PRESETS = [("16:9", "landscape"), ("1:1", "landscape"), ("4:5", "portrait"), ("free", "landscape")]


def dragged_boxes(tmp_path: Path, items: list[dict]) -> list[dict]:
    """界面上的拖动（crop.js 的 fitCrop 那一支）：点比例的框缩到 60%，往四个角拖出界，再收回来。"""
    (tmp_path / "geom.js").write_text((ASSETS / "geom.js").read_text(encoding="utf-8"), encoding="utf-8")
    script = tmp_path / "drag.mjs"
    script.write_text(
        "import * as G from './geom.js';\n"
        f"const frame = {{ w: {FRAME[0]}, h: {FRAME[1]} }};\n"
        f"const items = {json.dumps(items)};\n"
        "console.log(JSON.stringify(items.map((it) => {\n"
        "  const g = it.geometry;\n"
        "  const full = G.canvasAfterRotation(frame, g.rotate90 || 0);\n"
        "  const pair = G.landscapePair(it.preset);\n"
        "  const L = pair ? pair[0] / pair[1] : null;\n"
        "  const ratio = L == null ? null : it.orientation === 'portrait' ? 1 / L : L;\n"
        "  const click = G.centeredMaxRect(full, ratio, g.straighten || 0, pair);\n"
        "  const w = click.w * 0.6, h = click.h * 0.6;\n"
        "  const [cx, cy] = it.corner;\n"
        "  const pushed = { x: cx ? 1.2 - w : -0.2, y: cy ? 1.2 - h : -0.2, w, h };\n"
        "  const r = G.fitStraightened(pushed, full, G.cropTilt(g)).rect;\n"
        "  return { canvas: full, rect: [r.x, r.y, r.w, r.h] };\n"
        "})));\n",
        encoding="utf-8",
    )
    res = subprocess.run(["node", str(script)], capture_output=True, text=True, cwd=tmp_path, encoding="utf-8")  # noqa: S603, S607
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)  # type: ignore[no-any-return]


def test_a_box_dragged_into_a_corner_is_the_box_the_server_writes(volumes, card, lib, tmp_path):
    """PR #39 复审（Grok 第三轮）：翻转再拉直以后拖到角上，后端写下的框比画面上的小（16:9、8°：2400 → 2000 宽）。
    界面和后端现在都按成片真正的有效区（翻转过的照片倾斜方向反过来）。差不超过一个整数比例台阶：
    拉直以后固定比例的框后端取 pair 的整数倍（16:9 一步 16 像素，4:5 一步 5 像素），自由比例只差取整。"""
    from gxr_studio.cards.discovery import find_cards
    from gxr_studio.cards.importer import ImportOptions, run_import

    run_import(lib, find_cards([volumes])[0], ImportOptions(min_free_bytes=0))
    shot = next(s for s in lib.list_shots(limit=100) if s["name"] == "R0000102")
    path = lib.sidecar_path_for(shot)
    steps = {"16:9": 16, "1:1": 1, "4:5": 5, "free": 0}
    items = [
        {"preset": p, "orientation": o, "geometry": g, "corner": [cx, cy]}
        for g in DRAG_GEOMETRIES
        for p, o in DRAG_PRESETS
        for cx in (0, 1)
        for cy in (0, 1)
    ]
    local = dragged_boxes(tmp_path, items)
    bad = []
    for item, mine in zip(items, local, strict=True):
        side = lib.read_sidecar_for_write(shot)
        side["frame"] = {"width": FRAME[0], "height": FRAME[1]}
        side["geometry"] = {**FLAT, **item["geometry"]}
        side["crop"] = None
        sc.write_atomic(path, side, tmp_dir=lib.tmp_dir)
        try:
            out = edits.write_crop(
                lib,
                shot,
                preset=item["preset"],
                rect=mine["rect"],
                angle_deg=float(item["geometry"].get("straighten", 0.0)),
                orientation=item["orientation"],
            )
        except Exception as exc:  # noqa: BLE001  被后端拒收也记下来
            bad.append({**item, "error": repr(exc)})
            continue
        cw, ch = mine["canvas"]["w"], mine["canvas"]["h"]
        theirs = [out["rect"][0] * cw, out["rect"][1] * ch, out["rect"][2] * cw, out["rect"][3] * ch]
        drawn = [mine["rect"][0] * cw, mine["rect"][1] * ch, mine["rect"][2] * cw, mine["rect"][3] * ch]
        off = max(abs(a - b) for a, b in zip(drawn, theirs, strict=True))
        if off > max(TOLERANCE_PX, steps[item["preset"]]):
            bad.append({**item, "drawn": [round(v, 1) for v in drawn], "written": [round(v, 1) for v in theirs], "off_px": round(off, 1)})
    assert not bad, json.dumps(bad, ensure_ascii=False, indent=1)
