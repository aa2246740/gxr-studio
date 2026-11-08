"""换照片后的边框残留（web/assets/stage_finish.js 的 clearDecor）。

显卡画布是整页唯一的一块，换照片时被搬进一个新的 .pv。上一张的边框把 position / left / top 写在这块画布上；
新 .pv 是干净的（没有边框的记号），旧版本的 clearDecor 因此什么都不做，画布留着绝对定位，.pv 缩成 0 × 0，画面一片空白，
要刷新页面才好。这里用最小的假元素在 node 里直接测，不需要浏览器和显卡：真浏览器里的整套走法在 tests/test_frame_navigation_browser.py。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "web" / "assets"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过前端纯函数测试")

SCRIPT = """
import { clearDecor } from './stage_finish.js';

// 够 clearDecor 用的假元素：classList、dataset、style（setProperty / removeProperty）、children
function el(classes = []) {
  const set = new Set(classes);
  const props = new Map();
  const node = {
    children: [],
    dataset: {},
    classList: { contains: (c) => set.has(c), add: (c) => set.add(c), remove: (c) => set.delete(c) },
    style: { setProperty: (k, v) => props.set(k, v), removeProperty: (k) => props.delete(k) },
    props: () => Object.fromEntries(props),
    classes: () => [...set],
  };
  return node;
}
const put = (node, props) => { for (const [k, v] of Object.entries(props)) node.style.setProperty(k, v); };
const STALE_CANVAS = { width: '1035px', height: '688px', position: 'absolute', left: '37px', top: '37px' };
const FRAMED_PV = {
  width: '1100px', height: '760px', background: 'rgb(255, 255, 255)',
  '--fx': '37px', '--fy': '37px', '--fw': '1035px', '--fh': '688px',
};
const deco = (parent) => {
  const node = { classList: { contains: (c) => c === 'pv-deco' }, remove: () => { parent.children.splice(parent.children.indexOf(node), 1); } };
  parent.children.push(node);
  return node;
};
const out = {};

// 1) 这次的 bug：新 .pv 是干净的，画布（复用的）身上留着上一张边框写的定位
{
  const pv = el(['pv']);
  const canvas = el(['pv-gpu']);
  put(canvas, STALE_CANVAS);
  clearDecor(pv, canvas);
  out.freshPvStaleCanvas = canvas.props();
}

// 2) 同一个 .pv 带着整套边框的样子（边框拿掉、换成没有边框的样式）：.pv、画布、叠加层都清干净
{
  const pv = el(['pv', 'framed']);
  pv.dataset.decor = '1';
  put(pv, FRAMED_PV);
  deco(pv);
  const canvas = el(['pv-gpu']);
  put(canvas, STALE_CANVAS);
  clearDecor(pv, canvas);
  out.framedPv = { pv: pv.props(), classes: pv.classes(), decor: pv.dataset.decor ?? null, deco: pv.children.length, canvas: canvas.props() };
}

// 3) 只有日期戳（没有边框）：.pv 只有 data-decor 和叠加层
{
  const pv = el(['pv']);
  pv.dataset.decor = '1';
  deco(pv);
  const canvas = el(['pv-gpu']);
  clearDecor(pv, canvas);
  out.stampOnlyPv = { decor: pv.dataset.decor ?? null, deco: pv.children.length };
}

// 4) 什么都没有的时候什么都不加（每次重画都会走到这里，不能凭空写出样式）
{
  const pv = el(['pv']);
  const canvas = el(['pv-gpu']);
  canvas.style.setProperty('width', '885px');
  clearDecor(pv, canvas);
  clearDecor(pv, canvas);
  out.clean = { pv: pv.props(), pvClasses: pv.classes(), canvas: canvas.props() };
}

// 5) 不认识的行内样式不动（只去掉边框版面写的那几项）
{
  const pv = el(['pv', 'framed']);
  pv.style.setProperty('width', '1100px');
  pv.style.setProperty('opacity', '0.5');
  clearDecor(pv, null);
  out.otherStyles = pv.props();
}
console.log(JSON.stringify(out));
"""


def test_clear_decor_removes_the_frame_layout_from_the_reused_canvas(tmp_path: Path) -> None:
    for name in ("stage_finish.js", "finish_layout.js", "finish_fonts.js", "finish_draw.js"):
        (tmp_path / name).write_text((WEB / name).read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
    (tmp_path / "check.mjs").write_text(SCRIPT, encoding="utf-8")
    res = subprocess.run(["node", "check.mjs"], capture_output=True, text=True, cwd=tmp_path, encoding="utf-8")
    assert res.returncode == 0, res.stderr[-2000:]
    out = json.loads(res.stdout)
    # 新 .pv 是干净的也要把画布上上一张的定位去掉：留着它 .pv 就是 0 × 0
    assert out["freshPvStaleCanvas"] == {"width": "1035px", "height": "688px"}
    assert out["framedPv"] == {"pv": {}, "classes": ["pv"], "decor": None, "deco": 0, "canvas": {"width": "1035px", "height": "688px"}}
    assert out["stampOnlyPv"] == {"decor": None, "deco": 0}
    assert out["clean"] == {"pv": {}, "pvClasses": ["pv"], "canvas": {"width": "885px"}}
    assert out["otherStyles"] == {"opacity": "0.5"}
