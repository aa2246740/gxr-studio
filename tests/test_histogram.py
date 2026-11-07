"""直方图的算法：前端的数点规则（web/assets/histogram_math.js）和 numpy 参考实现逐档、逐个计数核对。

用 node 跑提交的源文件的副本，不在源目录里执行任何东西。显卡着色器的同样核对在 test_histogram_gpu.py，
这里还检查引擎里那几份拷贝的常量（溢出判定、取样间隔、矩形换算）和 JS 这边是同一套。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

import histogram_support as ref

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "web" / "assets"
FRONTEND = ROOT / "frontend" / "src"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过前端纯函数测试")


def run_node(tmp_path: Path, script: str) -> object:
    (tmp_path / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
    shutil.copyfile(ASSETS / "histogram_math.js", tmp_path / "histogram_math.js")
    shutil.copyfile(ASSETS / "gpu" / "engine.js", tmp_path / "engine.js")
    (tmp_path / "check.mjs").write_text(script, encoding="utf-8")
    res = subprocess.run(["node", str(tmp_path / "check.mjs")], capture_output=True, text=True, cwd=tmp_path, timeout=120, encoding="utf-8")  # noqa: S603, S607
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def rgba(rgb: np.ndarray) -> np.ndarray:
    return np.concatenate([rgb, np.full(rgb.shape[:2] + (1,), 255, np.uint8)], axis=-1)


def images() -> dict[str, np.ndarray]:
    big = ref.noise(800, 1000, seed=3)
    big[:200, :250] = 0  # 左上一块纯黑，取样间隔 2 时也要数到
    big[600:, 700:] = 255
    odd = ref.noise(777, 1001, seed=11)
    return {
        "ramp": ref.ramp(),
        "noise": ref.noise(),
        "blocks": ref.clipping_blocks(),
        "big_stride2": big,  # 80 万像素 > 262144：取样间隔 2
        "odd_size": odd,
        "one_pixel": np.array([[[10, 250, 255]]], np.uint8),
    }


# (图, 归一化矩形或 None, 最多取样点数)
CASES = [
    ("ramp", None, None),
    ("noise", None, None),
    ("blocks", None, None),
    ("big_stride2", None, None),
    ("odd_size", None, None),
    ("one_pixel", None, None),
    ("noise", (0.1, 0.2, 0.5, 0.4), None),
    ("big_stride2", (0.25, 0.25, 0.5, 0.5), None),
    ("blocks", (0.0, 0.0, 0.25, 0.3), None),
    ("big_stride2", None, 20000),  # 取样间隔 7
    ("noise", (0.9, 0.9, 0.5, 0.5), None),  # 越界的框：收回到画面里
]


def test_counts_match_numpy(tmp_path):
    imgs = images()
    for name, img in imgs.items():
        (tmp_path / f"{name}.rgba").write_bytes(rgba(img).tobytes())
    cases = [{"name": n, "w": imgs[n].shape[1], "h": imgs[n].shape[0], "region": r, "max": m} for n, r, m in CASES]
    out = run_node(
        tmp_path,
        "import { readFileSync } from 'node:fs';\n"
        "import { countRGBA, pixelRegion, sampleStride, MAX_SAMPLES } from './histogram_math.js';\n"
        f"const cases = {json.dumps(cases)};\n"
        "const res = cases.map((c) => {\n"
        "  const data = readFileSync(`${c.name}.rgba`);\n"
        "  const region = c.region ? { x: c.region[0], y: c.region[1], w: c.region[2], h: c.region[3] } : null;\n"
        "  const rect = pixelRegion(c.w, c.h, region);\n"
        "  const stride = sampleStride(rect[2] - rect[0], rect[3] - rect[1], c.max || MAX_SAMPLES);\n"
        "  const r = countRGBA(data, c.w, c.h, { rect, stride });\n"
        "  return { counts: Array.from(r.counts), samples: r.samples, stride: r.stride, rect: r.rect };\n"
        "});\n"
        "console.log(JSON.stringify(res));\n",
    )
    assert isinstance(out, list) and len(out) == len(CASES)
    for (name, region, max_samples), got in zip(CASES, out, strict=True):
        img = imgs[name]
        rect = ref.pixel_rect(img.shape[1], img.shape[0], region)
        want = ref.reference(img, rect, None, max_samples or ref.MAX_SAMPLES)
        label = f"{name} {region} {max_samples}"
        assert tuple(got["rect"]) == want["rect"] == rect, label
        assert got["stride"] == want["stride"], label
        assert got["samples"] == want["samples"], label
        np.testing.assert_array_equal(np.asarray(got["counts"]), want["counts"], err_msg=label)


def test_known_blocks_have_the_expected_clip_counts(tmp_path):
    """不靠参考实现：clipping_blocks 里各块的个数是手算的。"""
    img = ref.clipping_blocks()
    want = ref.reference(img)
    counts = want["counts"]
    assert counts[ref.AT["crushed"]] == 2 * 40 * 40  # (0,0,0) 和 (1,2,2)
    assert counts[ref.AT["blown"]] == 3 * 40 * 40  # (255,255,255)、(253,254,255)、(255,255,253)
    assert want["samples"] == 240 * 320 and want["stride"] == 1


def test_summary_thresholds(tmp_path):
    """三角在占比 ≥ 0.5% 时亮：999 个点里 4 个压死（0.4%）不亮，5 个（0.5%）亮；高光同理。"""
    out = run_node(
        tmp_path,
        "import { summarize, COUNT_SIZE, AT, WARN_SHARE, percentText } from './histogram_math.js';\n"
        "const make = (crushed, blown, samples) => {\n"
        "  const c = new Uint32Array(COUNT_SIZE);\n"
        "  c[AT.crushed] = crushed;\n"
        "  c[AT.blown] = blown;\n"
        "  return summarize({ counts: c, samples });\n"
        "};\n"
        "const rows = [make(4, 0, 1000), make(5, 0, 1000), make(0, 4, 1000), make(0, 5, 1000), make(0, 0, 0), make(1000, 1000, 1000)];\n"
        "console.log(JSON.stringify({ warn: WARN_SHARE, rows: rows.map((s) => [s.shadowWarn, s.highlightWarn]),\n"
        "  text: [0, 0.0004, 0.0006, 0.034, 0.1, 0.5].map(percentText) }));\n",
    )
    assert out["warn"] == ref.WARN_SHARE == 0.005  # type: ignore[index]
    assert out["rows"] == [[False, False], [True, False], [False, False], [False, True], [False, False], [True, True]]  # type: ignore[index]
    assert out["text"] == ["0%", "0%", "0.1%", "3.4%", "10%", "50%"]  # type: ignore[index]


def test_mean_moves_with_brightness(tmp_path):
    """平均亮度档：整体加亮 40 级，平均值也加 40（不到顶的灰阶图）。"""
    base = np.clip(ref.noise(100, 100, seed=2).astype(int) // 2, 0, 120).astype(np.uint8)
    bright = np.clip(base.astype(int) + 40, 0, 255).astype(np.uint8)
    for name, img in (("base", base), ("bright", bright)):
        (tmp_path / f"{name}.rgba").write_bytes(rgba(img).tobytes())
    out = run_node(
        tmp_path,
        "import { readFileSync } from 'node:fs';\n"
        "import { countRGBA, summarize } from './histogram_math.js';\n"
        "const mean = (n) => summarize(countRGBA(readFileSync(`${n}.rgba`), 100, 100)).mean;\n"
        "console.log(JSON.stringify([mean('base'), mean('bright')]));\n",
    )

    def lum(img: np.ndarray) -> float:
        px = img.astype(np.int64)
        return float(np.mean((2126 * px[..., 0] + 7152 * px[..., 1] + 722 * px[..., 2] + 5000) // 10000))

    assert out[0] == pytest.approx(lum(base)) and out[1] == pytest.approx(lum(bright))  # type: ignore[index]
    assert out[1] - out[0] > 39  # type: ignore[index]


def test_mark_clipping_matches_the_reference_overlay(tmp_path):
    img = ref.clipping_blocks()
    (tmp_path / "blocks.rgba").write_bytes(rgba(img).tobytes())
    out = run_node(
        tmp_path,
        "import { readFileSync } from 'node:fs';\n"
        "import { markClipping } from './histogram_math.js';\n"
        "const data = readFileSync('blocks.rgba');\n"
        "const res = {};\n"
        "const modes = { both: [true, true], shadow: [true, false], highlight: [false, true], none: [false, false] };\n"
        "for (const [k, o] of Object.entries(modes)) {\n"
        "  const out = new Uint8ClampedArray(data.length);\n"
        "  const n = markClipping(data, out, { shadow: o[0], highlight: o[1] });\n"
        "  res[k] = { n, out: Array.from(out) };\n"
        "}\n"
        "console.log(JSON.stringify(res));\n",
    )
    for key, (shadow, highlight) in {"both": (True, True), "shadow": (True, False), "highlight": (False, True), "none": (False, False)}.items():
        marked = np.asarray(out[key]["out"], np.uint8).reshape(240, 320, 4)  # type: ignore[index]
        want = ref.overlay(img, shadow=shadow, highlight=highlight)
        hit = (want != img).any(axis=2)
        # 画了颜色的点：颜色对、不透明；其余点全透明
        assert (marked[..., 3][hit] == 255).all() and (marked[..., 3][~hit] == 0).all(), key
        np.testing.assert_array_equal(marked[..., :3][hit], want[hit], err_msg=key)
        assert out[key]["n"] == {"crushed": 3200 if shadow else 0, "blown": 4800 if highlight else 0}, key  # type: ignore[index]


def test_engine_copies_agree_with_the_js_rules(tmp_path):
    """引擎（TypeScript，构建进 engine.js）里的判定、取样间隔、矩形换算是 histogram_math.js 的拷贝，两边必须一样。"""
    sizes = [(1, 1), (256, 64), (640, 480), (1000, 800), (2144, 1424), (4288, 2848), (4288, 1583), (777, 1001), (100000, 3)]
    regions = [None, {"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.4}, {"x": 0.9, "y": 0.9, "w": 0.5, "h": 0.5}, {"x": 0, "y": 0, "w": 1e-9, "h": 1e-9}]
    out = run_node(
        tmp_path,
        "import * as js from './histogram_math.js';\n"
        "import * as gpu from './engine.js';\n"
        f"const sizes = {json.dumps(sizes)};\n"
        f"const regions = {json.dumps(regions)};\n"
        "const same = [];\n"
        "for (const [w, h] of sizes) {\n"
        "  same.push(js.sampleStride(w, h) === gpu.sampleStride(w, h), js.sampleStride(w, h, 20000) === gpu.sampleStride(w, h, 20000));\n"
        "  for (const r of regions) {\n"
        "    const a = js.pixelRegion(w, h, r);\n"
        "    const b = gpu.pixelRegion(w, h, r && [r.x, r.y, r.w, r.h]);\n"
        "    same.push(JSON.stringify(a) === JSON.stringify(b));\n"
        "  }\n"
        "}\n"
        "const strides = sizes.map(([w, h]) => js.sampleStride(w, h));\n"
        "const max = [gpu.HIST_MAX_SAMPLES, js.MAX_SAMPLES];\n"
        "console.log(JSON.stringify({ same, strides, clip: gpu.CLIP, max, js: [js.SHADOW_MAX, js.HIGHLIGHT_MIN] }));\n",
    )
    assert all(out["same"]), out  # type: ignore[index]
    assert out["clip"] == {"shadowMax": ref.SHADOW_MAX, "highlightMin": ref.HIGHLIGHT_MIN}  # type: ignore[index]
    assert out["js"] == [ref.SHADOW_MAX, ref.HIGHLIGHT_MIN]  # type: ignore[index]
    assert out["max"] == [ref.MAX_SAMPLES, ref.MAX_SAMPLES]  # type: ignore[index]
    # 参考实现自己的取样间隔，和 JS 一致
    assert out["strides"] == [ref.stride_for(w, h) for w, h in sizes]  # type: ignore[index]


def test_shader_constants_match_the_js_rules():
    """着色器里写死的数字（hist.wgsl 的 step/规则来自 uniform，present.wgsl 的颜色是字面量）要和 histogram_math.js 一致。"""
    math_js = (ASSETS / "histogram_math.js").read_text(encoding="utf-8")
    shadow = tuple(int(v) for v in re.search(r"SHADOW_RGB = \[(\d+), (\d+), (\d+)\]", math_js).groups())  # type: ignore[union-attr]
    highlight = tuple(int(v) for v in re.search(r"HIGHLIGHT_RGB = \[(\d+), (\d+), (\d+)\]", math_js).groups())  # type: ignore[union-attr]
    assert shadow == ref.SHADOW_RGB and highlight == ref.HIGHLIGHT_RGB
    present = (FRONTEND / "shaders" / "present.wgsl").read_text(encoding="utf-8")
    blue = re.search(r"return vec4f\((\d+)\.0 / 255\.0, (\d+)\.0 / 255\.0, 1\.0, 1\.0\)", present)
    red = re.search(r"return vec4f\(1\.0, (\d+)\.0 / 255\.0, (\d+)\.0 / 255\.0, 1\.0\)", present)
    assert blue and red
    assert (int(blue.group(1)), int(blue.group(2)), 255) == shadow
    assert (255, int(red.group(1)), int(red.group(2))) == highlight
    hist = (FRONTEND / "shaders" / "hist.wgsl").read_text(encoding="utf-8")
    assert "2126u * c.x + 7152u * c.y + 722u * c.z + 5000u) / 10000u" in hist
