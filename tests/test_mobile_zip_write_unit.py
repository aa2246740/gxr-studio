"""电脑上一批下载打成的 ZIP（mobile/src/lib/zip_write.ts）：读回、CRC、UTF-8 名字、空批拒绝、拆包、unzip -t、ditto。

Node 直接跑 TypeScript，全是合成数据。这个文件也在导出管线的隐私 lint 范围里，所以不用裸 assert。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UNIT = REPO / "tests" / "support" / "mobile_zip_write_unit.mjs"


def test_mobile_zip_write_units(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    result = subprocess.run(
        [node, "--experimental-strip-types", "--test", str(UNIT)],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "TMPDIR": str(tmp_path)},
    )
    if result.returncode != 0 or "ℹ fail 0" not in result.stdout:
        pytest.fail("node:test zip writer units failed; run tests/support/mobile_zip_write_unit.mjs with node --test", pytrace=False)
