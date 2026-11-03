"""确认组件的键盘行为；仅用 Node 标准库，可在 Linux CI 运行。"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node")
def test_confirmation_defaults_to_cancel_and_enter_never_confirms(tmp_path):
    root = Path(__file__).resolve().parents[1]
    module = tmp_path / "dom.mjs"
    module.write_bytes((root / "web/assets/dom.js").read_bytes())
    script = root / "tests/support/confirmation_dom.mjs"
    result = subprocess.run(["node", str(script), module.as_uri()], capture_output=True, text=True, timeout=10, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "default_focus": "取消",
        "enter_cancelled": True,
        "enter_on_confirm_blocked": True,
        "confirmed_by_click": 1,
        "overwrite_label": "替换",
        "overwrite_by_enter": 0,
    }
