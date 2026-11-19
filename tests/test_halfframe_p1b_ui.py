"""halfframe-p1b-ui 单元与 API 验收：冲突后缀配对、单格只读渲染 view=first|second、前端纯逻辑。"""

from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from conftest import PORT
from gxr_studio import halfframe_pairs as hfp
from gxr_studio.app import create_app
from gxr_studio.cards.discovery import find_cards
from gxr_studio.cards.importer import ImportOptions, run_import
from gxr_studio.library.store import Library
from test_halfframe_p0 import (
    WEB,
    _clear_desktop_cache,
    _field_folder,
    _setup_g250_calibrated_home,
    _sorted_pairs,
    _stage_volume_pairs,
)
from test_halfframe_p1a_field import _index_shots_by_jpg_sha, _seam_core

UNIT_JS = Path(__file__).resolve().parent / "support" / "desktop_halfframe_p1b_unit.mjs"


def test_extract_number_and_neighbors_strip_conflict_suffix() -> None:
    """P1a r2 N4：导入改名产生的 _{6+ hex} 冲突后缀在解析编号和相邻文件名时会被剥离。"""
    assert hfp.extract_number("R0012345_a1b2c3d4") == 12345
    assert hfp.extract_number("R0012345_a1b2c3d4.JPG") == 12345
    assert hfp.extract_number("R0012345_0123456789ab") == 12345
    assert hfp.extract_number("R0012345_deadbeef") == 12345
    # 原本就是前缀_数字 的普通文件名（如 DSC_123456）剥离后不再以数字结尾，不能误剥离
    assert hfp.extract_number("DSC_123456") == 123456
    assert hfp.extract_number("IMG_0001") == 1

    neighbors = hfp.neighbor_names("R0012345_a1b2c3d4", +1)
    assert "R0012346" in neighbors
    prev_neighbors = hfp.neighbor_names("R0012346_deadbeef.JPG", -1)
    assert "R0012345" in prev_neighbors
    assert "DSC_123457" in hfp.neighbor_names("DSC_123456", +1)


def test_render_pair_single_views_on_real_card250(
    settings: Any,
    lib: Library,
    volumes: Path,
) -> None:
    """GET/POST /api/photos/{p2}/render?view=first|second 返回对应半格整幅画面（带整对色调）、永不写 sidecar。"""
    _clear_desktop_cache()
    pairs = _sorted_pairs(_field_folder("card250"))
    _stage_volume_pairs(volumes, [*pairs[:2], pairs[10]])
    _setup_g250_calibrated_home(settings)
    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    by_idx = _index_shots_by_jpg_sha(lib, [*pairs[:2], pairs[10]])
    p2 = by_idx[2]["shot_id"]
    xpan = by_idx[3]["shot_id"]
    lib.close()

    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        before_edit = client.get(f"/api/photos/{p2}/edit").json()

        r_dip = client.get(f"/api/photos/{p2}/render?stage=final&size=preview")
        assert r_dip.status_code == 200, r_dip.text
        r1 = client.get(f"/api/photos/{p2}/render?stage=final&size=preview&view=first")
        assert r1.status_code == 200, r1.text
        r2 = client.get(f"/api/photos/{p2}/render?stage=final&size=preview&view=second")
        assert r2.status_code == 200, r2.text

        with Image.open(io.BytesIO(r_dip.content)) as im_d:
            dip = np.asarray(im_d.convert("RGB"))
        with Image.open(io.BytesIO(r1.content)) as im_1:
            img1 = np.asarray(im_1.convert("RGB"))
        with Image.open(io.BytesIO(r2.content)) as im_2:
            img2 = np.asarray(im_2.convert("RGB"))

        # 双联中心有黑缝；第 1 张和第 2 张是各自的整幅（没有双联黑缝，且两张画面不同）
        _, dip_core, _ = _seam_core(dip)
        assert float(dip_core.mean()) <= 3.0
        _, c1_core, _ = _seam_core(img1)
        _, c2_core, _ = _seam_core(img2)
        assert float(c1_core.mean()) > 5.0, "view=first should be photo 1 full frame, not diptych"
        assert float(c2_core.mean()) > 5.0, "view=second should be photo 2 full frame, not diptych"
        assert img1.shape == img2.shape
        diff12 = float(np.abs(img1.astype(np.float32) - img2.astype(np.float32)).mean())
        assert diff12 > 5.0, f"view=first and view=second should differ, got mean diff {diff12:.2f}"

        # POST /render?view=first 应用整对的草稿配方（03 高对比黑白），且绝不写入 sidecar
        r1_bw = client.post(
            f"/api/photos/{p2}/render?stage=final&size=preview&view=first",
            json={"recipe": {"kind": "builtin", "unit": "jlaunch", "slot": 3, "levels": {}}},
        )
        assert r1_bw.status_code == 200, r1_bw.text
        with Image.open(io.BytesIO(r1_bw.content)) as im_bw:
            bw = np.asarray(im_bw.convert("RGB"))
        chroma = float(np.abs(bw[:, :, 0].astype(np.float32) - bw[:, :, 2].astype(np.float32)).mean())
        assert chroma < 4.0, f"expected B&W look on view=first, got chroma {chroma:.2f}"

        after_edit = client.get(f"/api/photos/{p2}/edit").json()
        assert after_edit["rev"] == before_edit["rev"]
        assert after_edit["recipe"] == before_edit["recipe"]

        # 非法 view 与非成对照片返回 422
        assert client.get(f"/api/photos/{p2}/render?stage=final&view=third").status_code == 422
        assert client.get(f"/api/photos/{xpan}/render?stage=final&view=first").status_code == 422


def test_halfframe_js_helpers() -> None:
    """Node 单元测试：折叠、跨页补齐、选区展开与去重、角标、取半格裁切、直方图排除黑缝。"""
    assert shutil.which("node") is not None, "node is required"
    proc = subprocess.run(
        ["node", str(UNIT_JS), str(WEB / "assets")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"JS unit test failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
