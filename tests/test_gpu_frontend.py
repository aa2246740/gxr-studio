"""显卡预览的前端纯函数：调子表/增益与 Python 一致，路由（显卡还是服务器）与 finish/serve.py 的分支一致。

用 node 跑提交的构建产物 web/assets/gpu/engine.js 和 web/assets/gpu_stage.js 的副本，不在源目录里执行任何东西。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from gxr_studio.render.params import parse_adjust, parse_curve
from gxr_studio.render.tones import channel_gains, display_table

ASSETS = Path(__file__).resolve().parents[1] / "web" / "assets"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过前端纯函数测试")


def run_node(tmp_path: Path, script: str) -> object:
    (tmp_path / "package.json").write_text('{"type":"module"}\n')
    shutil.copyfile(ASSETS / "gpu" / "engine.js", tmp_path / "engine.js")
    (tmp_path / "check.mjs").write_text(script, encoding="utf-8")
    res = subprocess.run(["node", str(tmp_path / "check.mjs")], capture_output=True, text=True, cwd=tmp_path, timeout=60)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


ADJUSTS = [
    {},
    {"contrast": 40},
    {"contrast": -100, "highlight": 100, "shadow": -100},
    {"exposure_ev": 1.3, "wb": {"warm": -55, "tint": 30}, "highlight": -50, "shadow": 40},
]
CURVES = [
    None,
    [[0, 0], [255, 255]],
    [[0, 10], [128, 120], [255, 250]],
    [[0, 0], [40, 10], [64, 48], [128, 128], [192, 210], [230, 250], [255, 255]],
    [[20, 0], [235, 255]],
]


def test_display_table_and_gains_match_python(tmp_path):
    cases = [(a, c) for a in ADJUSTS for c in CURVES]
    out = run_node(
        tmp_path,
        "import { displayTable, channelGains } from './engine.js';\n"
        f"const cases = {json.dumps(cases)};\n"
        "const one = ([a, c]) => { const t = displayTable(a, c); return { t: t ? Array.from(t) : null, g: channelGains(a) }; };\n"
        "console.log(JSON.stringify(cases.map(one)));\n",
    )
    assert isinstance(out, list)
    for (adjust_raw, curve), got in zip(cases, out):
        adjust = parse_adjust(adjust_raw)
        master = parse_curve({"master": curve}) if curve else None
        ref = display_table(adjust, master)
        if ref is None:
            assert got["t"] is None, (adjust_raw, curve)
        else:
            assert got["t"] is not None
            np.testing.assert_allclose(np.asarray(got["t"], np.float32), ref, atol=2e-4, err_msg=json.dumps([adjust_raw, curve]))
        gains = channel_gains(adjust)
        if gains is None:
            assert got["g"] is None
        else:
            np.testing.assert_allclose(got["g"], gains, rtol=1e-12)


def _decide(tmp_path: Path, rows: list[dict[str, object]]) -> list[object]:
    for name in ("gpu_stage.js",):
        shutil.copyfile(ASSETS / name, tmp_path / name)
    (tmp_path / "api.js").write_text("export const api = async () => { throw new Error('不读接口'); };\n")
    (tmp_path / "state.js").write_text("export const state = { photos: [] };\n")
    return run_node(
        tmp_path,
        "import { decide } from './gpu_stage.js';\n"
        f"const rows = {json.dumps(rows)};\n"
        "console.log(JSON.stringify(rows.map((r) => decide(r.ed, r.info, r.stage, r.recipe, r.lutSpace))));\n",
    )  # type: ignore[return-value]


RAW = {"enabled": True, "unit": "jlaunch", "raw": {"url": "/raw"}, "raw_reason": None, "jpg": {"url": "/jpg"}, "scene": {"A": [], "b": []}}
ED = {"v2": True, "lut": None, "geometry": {}, "frame": {"style": "none"}, "stamp": {"enabled": False}}
REC = {"kind": "builtin", "unit": "jlaunch", "slot": 1, "levels": {}}


@pytest.mark.parametrize(
    ("label", "ed", "info", "stage", "recipe", "lut_space", "expected"),
    [
        ("配方 + RAW → 显卡 RAW", ED, RAW, "final", REC, None, {"base": "raw", "recipe": REC, "lut": None}),
        ("没配方、有 JPG → 相机 JPG", ED, RAW, "final", None, None, {"base": "jpg", "lut": None}),
        ("只有 RAW、没配方 → 原厂 04", ED, {**RAW, "jpg": None}, "final", None, None, {"base": "raw", "recipe": None, "lut": None}),
        ("只有 JPG + 配方（近似）→ 服务器", ED, {**RAW, "raw": None, "raw_reason": "no_raw"}, "final", REC, None, None),
        ("RAW 写法不支持 + 配方 → 服务器", ED, {**RAW, "raw": None, "raw_reason": "linearization_table"}, "final", REC, None, None),
        ("开关关着 → 服务器", ED, {**RAW, "enabled": False}, "final", REC, None, None),
        ("原图 → 服务器", ED, RAW, "orig", REC, None, None),
        ("透视 → 服务器", {**ED, "geometry": {"vertical": 10}}, RAW, "uncropped", REC, None, None),
        ("成片里有边框 → 服务器", {**ED, "frame": {"style": "white"}}, RAW, "final", REC, None, None),
        (
            "裁剪页里有边框 → 显卡（未裁切画面不画边框）",
            {**ED, "frame": {"style": "white"}},
            RAW,
            "uncropped",
            REC,
            None,
            {"base": "raw", "recipe": REC, "lut": None},
        ),
        ("时间戳 → 服务器", {**ED, "stamp": {"enabled": True}}, RAW, "final", REC, None, None),
        ("显示类 LUT、没配方、有 JPG → JPG 底图", {**ED, "lut": "x"}, RAW, "final", None, "srgb", {"base": "jpg", "lut": "x"}),
        ("显示类 LUT + 配方 → RAW 底图", {**ED, "lut": "x"}, RAW, "final", REC, "srgb", {"base": "raw", "recipe": REC, "lut": "x"}),
        ("Log LUT + 50mm RAW", {**ED, "lut": "x"}, RAW, "final", None, "log", {"base": "raw", "lut": "x"}),
        ("Log LUT + MOUNT → 服务器（lut_needs_raw）", {**ED, "lut": "x"}, {**RAW, "unit": "l08firm"}, "final", None, "log", None),
        ("LUT 描述还没到 → 服务器", {**ED, "lut": "x"}, RAW, "final", None, None, None),
        ("旧后端（没有 v2）→ 服务器", {**ED, "v2": False}, RAW, "final", REC, None, None),
    ],
)
def test_route_decision_matches_serve_branches(tmp_path, label, ed, info, stage, recipe, lut_space, expected):
    got = _decide(tmp_path, [{"ed": ed, "info": info, "stage": stage, "recipe": recipe, "lutSpace": lut_space}])[0]
    assert got == expected, label


def test_engine_bundle_is_self_contained():
    """构建产物不引用其他模块，也不连外网（工作室页面的 CSP 只允许本机）。"""
    text = (ASSETS / "gpu" / "engine.js").read_text(encoding="utf-8")
    assert re.search(r"^\s*import\s", text, re.M) is None
    assert "http://" not in text and "https://" not in text
