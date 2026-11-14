"""EXIF 和分组的纯函数。Node 直接跑 TypeScript，不另装测试框架。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UNIT = REPO / "tests" / "support" / "mobile_ui_unit.mjs"


def test_mobile_ui_exif_and_grouping() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    result = subprocess.run(
        [node, "--experimental-strip-types", "--test", str(UNIT)],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


RECIPES = REPO / "tests" / "support" / "mobile_ui_recipes_unit.mjs"


def test_mobile_ui_zip_pack_unit_and_iso(tmp_path: Path) -> None:
    """UI-M2：校准数据 zip、校准包核对、镜头单元和 ISO 模型、配方列表。桌面写的 zip 用合成的包现做。"""
    import os
    import zipfile

    from gxr_studio.camdata import calib_export
    from support.color_fixtures import entry
    from support.mobile_ui_calib import pack_files

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    files = pack_files({"jlaunch": entry(words=0), "qlaunch": entry(words=1), "l08firm": entry(words=2)})
    stored = tmp_path / "GXR 校准数据 2026-11-15.zip"
    calib_export.write_zip(files, os.open(stored, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), (2026, 11, 15, 9, 0, 0))
    deflated = tmp_path / "deflated.zip"
    with zipfile.ZipFile(deflated, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, blob in files.items():
            archive.writestr(name, blob)
    result = subprocess.run(
        [node, "--experimental-strip-types", "--test", str(RECIPES)],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "GXR_M2_STORED_ZIP": str(stored), "GXR_M2_DEFLATED_ZIP": str(deflated)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "# pass 8" in result.stdout or "ℹ pass 8" in result.stdout, result.stdout
