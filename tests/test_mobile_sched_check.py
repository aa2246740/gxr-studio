"""手机版 Worker 池的调度检查（G1–G5，见 docs/mobile.md）。

tests/support/mobile_sched_check.mjs 用真实的 Pool、Book 和 CardBakeController，假 Worker 和假时钟，跑每个以前的
发现的固定日程和 1–4 个 Worker 上各 375 个随机日程。违反时打印种子和事件记录。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CHECKER = REPO / "tests" / "support" / "mobile_sched_check.mjs"


def test_mobile_pool_scheduling_g1_to_g5() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    result = subprocess.run(
        [node, "--experimental-strip-types", "--test", str(CHECKER)],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert result.stdout.count("SCHED ") == 2, result.stdout
