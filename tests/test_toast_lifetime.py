"""提示条的寿命由可控时钟检查，鼠标和键盘焦点各自都能暂停计时。"""

import json
import shutil
import subprocess

import pytest

from test_edit_route import SUPPORT, prepare

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node")


def test_toasts_expire_and_keep_actions_reachable_until_they_do(tmp_path):
    folder = prepare(tmp_path)
    shutil.copy(SUPPORT / "toast_lifetime.mjs", tmp_path / "toast_lifetime.mjs")
    res = subprocess.run(["node", str(tmp_path / "toast_lifetime.mjs"), str(folder)], capture_output=True, text=True, timeout=20)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip())
    assert out["infoBefore"] is True and out["infoAfter"] is False
    assert out["actionBefore"] is True and out["actionAfter"] is False
    assert out["longAfter"] is False
    assert out["pending"] is True
    assert out["errorBefore"] is True and out["errorAfter"] is False and out["ran"] == 1
    assert out["focusPauses"] is True and out["internalFocusPauses"] is True
    assert out["resumeBefore"] is True and out["resumeAfter"] is False
