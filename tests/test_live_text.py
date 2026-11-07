"""文字框边输入边生效（web/assets/live_text.js）：画面跟着输入变，保存等停下来、回车或离开输入框。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "web" / "assets"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过前端纯函数测试")

SCRIPT = """
import { liveText } from './live_text.js';
const fake = () => {
  const handlers = {};
  return {
    addEventListener(name, fn) { handlers[name] = fn; },
    fire: (name, ev = {}) => handlers[name]?.({ preventDefault() {}, ...ev }),
  };
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const out = {};
const log = [];
const input = fake();
liveText(input, { apply: () => log.push('apply'), save: () => log.push('save'), wait: 60 });
// 连着输入三下：每下都 apply，停下来以后只 save 一次
input.fire('input'); input.fire('input'); input.fire('input');
out.afterTyping = [...log];
await sleep(25);
out.beforeWait = log.filter((x) => x === 'save').length;
await sleep(100);
out.afterWait = log.filter((x) => x === 'save').length;
// 回车：马上保存；之后计时器不再重复保存
log.length = 0;
input.fire('input');
input.fire('keydown', { key: 'Enter', isComposing: false });
out.enter = log.filter((x) => x === 'save').length;
await sleep(100);
out.enterThenWait = log.filter((x) => x === 'save').length;
// 输入法选字时的回车不算；离开输入框（change）保存；没有改动时什么都不做
log.length = 0;
input.fire('input');
input.fire('keydown', { key: 'Enter', isComposing: true });
out.composing = log.filter((x) => x === 'save').length;
input.fire('change');
out.change = log.filter((x) => x === 'save').length;
input.fire('change');
input.fire('keydown', { key: 'Enter', isComposing: false });
await sleep(100);
out.idle = log.filter((x) => x === 'save').length;
console.log(JSON.stringify(out));
"""


def test_live_text_applies_every_keystroke_and_saves_once(tmp_path):
    (tmp_path / "live_text.js").write_text((ASSETS / "live_text.js").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
    (tmp_path / "check.mjs").write_text(SCRIPT, encoding="utf-8")
    res = subprocess.run(["node", "check.mjs"], capture_output=True, text=True, cwd=tmp_path, encoding="utf-8")
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["afterTyping"] == ["apply", "apply", "apply"]
    assert out["beforeWait"] == 0 and out["afterWait"] == 1
    assert out["enter"] == 1 and out["enterThenWait"] == 1
    assert out["composing"] == 0 and out["change"] == 1 and out["idle"] == 1
