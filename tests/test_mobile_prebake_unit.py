"""网格空闲预烘焙（`mobile/src/app/prebake.ts`）单元测试：Node --experimental-strip-types，真实 Book 和 Pool。

覆盖：网格静下来以后第一次换到默认卡和第一屏的卡不再发烘焙请求、表的参数和编辑器自己烘焙的一样；预烘焙在跑时点卡不被耽误
（3 个和 4 个 Worker）；换校准、换相机配方文件以后按新版本重做、旧版本晚到的表不留；清空照片作废；上限（张数、并发、两个
Worker 不做、池子忙和导出时不开始）。`GXR_TEST_CHECKOUT` 指向旧版本的检出目录时做负对照。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UNIT = REPO / "tests" / "support" / "mobile_prebake_unit.mjs"


def test_mobile_prebake_units() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    result = subprocess.run(
        [node, "--experimental-strip-types", "--test", str(UNIT)],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
