"""浏览器驱动要让 Chrome 正常退出，不能 SIGTERM 后马上 SIGKILL。

macOS 上的 Google Chrome 启动时把应用复制到系统临时目录（code_sign_clone），被强杀时副本留在内置盘上；
2026-11 测试积了 432 份。这里用假的 Chrome（node 子进程）验证 tests/support/debug_port.mjs 的 closeChrome。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SUPPORT = Path(__file__).resolve().parent / "support"


def test_close_chrome_waits_for_a_clean_exit_and_kills_only_as_a_last_resort():
    node = shutil.which("node")
    if node is None:
        pytest.skip("没有 Node.js")
    result = subprocess.run(  # noqa: S603
        [node, str(SUPPORT / "chrome_close_check.mjs")], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])

    # 连着 DevTools 时先发 Browser.close，Chrome 自己退出，不发信号。
    assert out["browserClose"]["how"] == "closed"
    assert out["browserClose"]["sent"] == ["Browser.close"]
    assert out["browserClose"]["signal"] is None
    # 没连上（取 ws 抛错）时 SIGTERM，并且等它 2 秒后自己退出；旧代码 1.2 秒就强杀了。
    assert out["slowTerm"]["how"] == "terminated"
    assert out["slowTerm"]["signal"] is None and out["slowTerm"]["code"] == 0
    assert out["slowTerm"]["ms"] >= 1900
    # 不理 SIGTERM 的才 SIGKILL，而且等够了宽限时间（800 ms）。
    assert out["ignoresTerm"]["how"] == "killed"
    assert out["ignoresTerm"]["signal"] == "SIGKILL"
    assert out["ignoresTerm"]["ms"] >= 750
    assert out["alreadyExited"] == "exited"


def test_no_browser_driver_kills_chrome_directly():
    """关 Chrome 只走 closeChrome。驱动里不再出现 chrome.kill(...)。"""
    offenders = [
        f"{path.name}:{n}"
        for path in sorted(SUPPORT.glob("*.mjs"))
        if path.name != "debug_port.mjs"
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"\bchrome\.kill\(", line)
    ]
    assert offenders == []
