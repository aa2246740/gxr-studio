"""g256 复审 r1 回归：旧缓存升级重查/配对（F1）与按需全量误报/Chromium 解码一致性扫描（F3/F4）。"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import PORT
from gxr_studio import halfframe as hf
from gxr_studio import halfframe_pairs as hfp
from gxr_studio.app import create_app
from gxr_studio.cards.discovery import find_cards
from gxr_studio.cards.importer import ImportOptions, run_import
from gxr_studio.config import Settings
from gxr_studio.library.store import Library
from support import samples
from test_g250 import _browser_prereq
from test_g256 import _hf256_field_folder, _node_is_diptych_batch
from test_halfframe_p0 import BROWSER_DRIVER, _clear_desktop_cache, _sorted_jpgs, _sorted_pairs, _stage_volume_pairs
from test_halfframe_p1a_field import _index_shots_by_jpg_sha
from test_mobile_ui_browser import _drive

# 仅在显式开启 GXR_HALFFRAME_SWEEP=1 时用于核对本机 captures/ 全量实拍的已知双联位置（按目录内 JPG 排序的 1-based 序号）。
_SWEEP_KNOWN_DIPTYCHS: dict[str, set[int]] = {
    "hf256-field-20261120/card": {2, 7, 10},
    "r6-field-20261117/card242": {3, 5},
    "r6-field-20261117/card245": {4},
    "r6-field-20261118/card249": {2, 4},
    "r6-field-20261118/card250": {2, 4, 6},
    "r6-field-20261118/kubik_r6": {5},
    "r6-field-20261118/probe251": {2, 4, 6},
    "r6-field-20261118/vertical": {2, 4, 6},
}

_SWEEP_SKIP = pytest.mark.skipif(
    os.environ.get("GXR_HALFFRAME_SWEEP") != "1",
    reason="captures/ 全量扫描默认跳过（设置 GXR_HALFFRAME_SWEEP=1 运行）",
)


def _seed_legacy_hf256_library(
    tmp_path: Path, name: str, field_pairs: list[tuple[Path, Path]]
) -> tuple[Settings, dict[int, str], dict[int, str]]:
    """按旧接缝规则（无 rule 字段、MOUNT #7 判为 False、无 MOUNT 配对且含旧 rejected）构造待升级图库。"""
    base = tmp_path / name
    home_dir = base / "home"
    lib_dir = base / "library"
    exp_dir = base / "export"
    vol_dir = base / "volumes"
    home_dir.mkdir(parents=True)
    lib_dir.mkdir(parents=True)
    exp_dir.mkdir(parents=True)
    vol_dir.mkdir(parents=True)
    settings = Settings(home_dir=home_dir, library_dir=lib_dir, export_dir=exp_dir, volumes_roots=(vol_dir,), token="t-g256-upgrade")
    _stage_volume_pairs(vol_dir, field_pairs)
    lib = Library(lib_dir).open()
    try:
        run_import(lib, find_cards((vol_dir,))[0], ImportOptions(min_free_bytes=0))
        by_idx = _index_shots_by_jpg_sha(lib, field_pairs)
        assert len(by_idx) == 10
        shas = {i: hashlib.sha256(field_pairs[i - 1][1].read_bytes()).hexdigest() for i in range(1, 11)}
        shot_ids = {i: by_idx[i]["shot_id"] for i in range(1, 11)}

        # 模拟旧版规则写出的 .studio/halfframe-v1.json（version=1，无 rule 字段，MOUNT #7 为 False）
        legacy_verdicts = {shas[i]: (i in {2, 10}) for i in range(1, 11)}
        lib.abspath(hf.HALFFRAME_REL).write_text(
            json.dumps({"version": 1, "verdicts": legacy_verdicts}, sort_keys=True),
            encoding="utf-8",
        )
        # 模拟旧版规则写出的 .studio/halfframe-pairs-v1.json（保留 28mm/50mm，去掉 MOUNT 对，并模拟旧 rejected）
        existing_pairs = lib._pairs.all_pairs()
        existing_pairs.pop(shas[7], None)
        assert shas[2] in existing_pairs and shas[10] in existing_pairs
        legacy_fl = {shas[i]: bool(lib._pairs.get_first_like(shas[i])) for i in range(1, 11)}
        assert legacy_fl[shas[6]] is True
        lib.abspath(hfp.PAIRS_REL).write_text(
            json.dumps(
                {
                    "version": 1,
                    "first_like": legacy_fl,
                    "pairs": existing_pairs,
                    "rejected": {shas[7]: [shas[6]]},
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    finally:
        lib.close()
    return settings, shas, shot_ids


def _assert_mount_paired(shots: list[dict[str, Any]], shot_ids: dict[int, str]) -> None:
    by_id = {s.get("shot_id") or s["id"]: s for s in shots}
    s6, s7 = by_id[shot_ids[6]], by_id[shot_ids[7]]
    assert s7["diptych"] is True and bool(s7["has_raw"]) is True
    assert s6.get("pair") is not None and s6["pair"]["role"] == "first" and s6["pair"]["partner_id"] == shot_ids[7]
    assert s7.get("pair") is not None and s7["pair"]["role"] == "second" and s7["pair"]["partner_id"] == shot_ids[6]


def _wait_halfframe_job(client: TestClient, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        res = client.get("/api/jobs")
        assert res.status_code == 200
        jobs = res.json()
        hf_jobs = [j for j in jobs if j.get("kind") == "halfframe"]
        if hf_jobs:
            last = hf_jobs[-1]
            if not last.get("running"):
                return last
        time.sleep(0.05)
    return last


def test_f1_upgrade_old_verdicts_and_neighbor_memo_pairs_mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F1：旧 halfframe-v1.json（MOUNT #7=False）与旧配对/邻居缓存升级后，启动回填、重建索引、单张读取均可配上 MOUNT。"""
    field_pairs = _sorted_pairs(_hf256_field_folder())
    assert len(field_pairs) == 10

    # 1) 启动后台回填路径（start_halfframe_job / run_backfill）：旧正判定不重解，旧负判定重查一次后自动补配对
    s_backfill, shas, shot_ids = _seed_legacy_hf256_library(tmp_path, "upgrade_backfill", field_pairs)
    real_detect = hf.detect_diptych
    decoded_shas: list[str] = []

    def spy_detect(path: Any) -> bool | None:
        decoded_shas.append(hashlib.sha256(Path(path).read_bytes()).hexdigest())
        return real_detect(path)

    monkeypatch.setattr(hf, "detect_diptych", spy_detect)
    with TestClient(create_app(s_backfill), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": s_backfill.token}) as client:
        job = _wait_halfframe_job(client)
        assert job and not job.get("running") and (job.get("progress") or {}).get("state") == "done", job
        rows = client.get("/api/photos?limit=100").json()
        _assert_mount_paired(rows, shot_ids)

    # 旧正判定（28mm #2、50mm #10）无需重解码；MOUNT #7 被重查一次
    assert shas[2] not in decoded_shas and shas[10] not in decoded_shas
    assert shas[7] in decoded_shas
    vdoc = json.loads((s_backfill.library_dir / hf.HALFFRAME_REL).read_text(encoding="utf-8"))
    assert vdoc["version"] == hf.HALFFRAME_VERSION and vdoc["rule"] == hf.VERDICT_RULE_VERSION
    assert vdoc["verdicts"][shas[7]] is True

    # 第二次重启不再重复解码，pending 为空
    lib_again = Library(s_backfill.library_dir).open()
    try:
        assert lib_again.halfframe_pending() == []
        assert lib_again.halfframe_pair_pending() == []
        _assert_mount_paired(lib_again.list_shots(), shot_ids)
    finally:
        lib_again.close()
    monkeypatch.undo()

    # 2) 重建索引路径（Library.rebuild）：含已有 neighbors_checked 缓存，重建后 MOUNT 必须配上
    s_rebuild, r_shas, r_ids = _seed_legacy_hf256_library(tmp_path, "upgrade_rebuild", field_pairs)
    lib_r = Library(s_rebuild.library_dir).open()
    try:
        lib_r._pairs.mark_neighbors_checked(r_shas[6])
        lib_r._pairs.mark_neighbors_checked(r_shas[7])
        lib_r.rebuild()
        assert not lib_r._pairs.neighbors_checked(r_shas[6])
        assert not lib_r._pairs.neighbors_checked(r_shas[7])
        _assert_mount_paired(lib_r.list_shots(), r_ids)
    finally:
        lib_r.close()

    # 3) 单张读取路径（get_shot）：即使 neighbors_checked 已预填充，读 #7 仍会失效旧备忘并配上 #6
    s_single, g_shas, g_ids = _seed_legacy_hf256_library(tmp_path, "upgrade_get_shot", field_pairs)
    lib_g = Library(s_single.library_dir).open()
    try:
        lib_g._pairs.mark_neighbors_checked(g_shas[6])
        lib_g._pairs.mark_neighbors_checked(g_shas[7])
        got7 = lib_g.get_shot(g_ids[7])
        assert got7 is not None and got7["diptych"] is True and got7["has_raw"] == 1
        assert got7.get("pair") is not None and got7["pair"]["role"] == "second" and got7["pair"]["partner_id"] == g_ids[6]
        _assert_mount_paired(lib_g.list_shots(), g_ids)
    finally:
        lib_g.close()

    # 4) 反向单张读取：未回填前先点开 #6，也能顺带配上 #7 并同步写入 #7 的双联判定
    s_rev, _rev_shas, rev_ids = _seed_legacy_hf256_library(tmp_path, "upgrade_get_first", field_pairs)
    lib_rev = Library(s_rev.library_dir).open()
    try:
        got6 = lib_rev.get_shot(rev_ids[6])
        assert got6 is not None and got6.get("pair") is not None
        assert got6["pair"]["role"] == "first" and got6["pair"]["partner_id"] == rev_ids[7]
        _assert_mount_paired(lib_rev.list_shots(), rev_ids)
    finally:
        lib_rev.close()


def _collect_captures_4288() -> tuple[list[tuple[Path, str, int, np.ndarray, bool]], list[Path]]:
    captures = samples.captures()
    if not captures.is_dir():
        samples.missing("captures/ 不在本机")

    known_paths: set[Path] = set()
    for rel_folder, indices in _SWEEP_KNOWN_DIPTYCHS.items():
        folder = captures / rel_folder
        if not folder.is_dir():
            continue
        jpgs = _sorted_jpgs(folder)
        for idx in indices:
            if 1 <= idx <= len(jpgs):
                known_paths.add(jpgs[idx - 1].resolve())

    rows: list[tuple[Path, str, int, np.ndarray, bool]] = []
    folder_counts: dict[str, int] = {}
    diptych_paths: list[Path] = []
    for path in sorted(captures.rglob("*")):
        if not path.is_file() or path.suffix.upper() != ".JPG":
            continue
        ok, luma = hf.decode_draft_luma(path)
        if not ok or luma is None:
            continue
        parts = path.relative_to(captures).parts
        fkey = "/".join(parts[:2]) if len(parts) > 2 else parts[0]
        folder_counts[fkey] = folder_counts.get(fkey, 0) + 1
        expected = path.resolve() in known_paths
        if expected:
            diptych_paths.append(path)
        rows.append((path, fkey, folder_counts[fkey], luma, expected))
    return rows, diptych_paths


@_SWEEP_SKIP
def test_opt_in_captures_sweep_desktop_and_phone_parity() -> None:
    """按需全量扫描 captures/ 全部 4288×2848 JPEG：0 误报，18/18 已知双联命中，桌面与手机规则 100% 一致。"""
    rows, diptych_paths = _collect_captures_4288()
    assert len(rows) > 0 and len(diptych_paths) == 18

    phone_verdicts = _node_is_diptych_batch([(luma, "col") for _p, _fk, _ord, luma, _exp in rows])
    for (_path, fkey, ord_idx, luma, expected), phone_v in zip(rows, phone_verdicts, strict=True):
        desk_v = hf.is_diptych_luma(luma)
        assert desk_v is phone_v, f"desktop/phone mismatch on {fkey} #{ord_idx}"
        assert desk_v is expected, f"verdict mismatch on {fkey} #{ord_idx}: got={desk_v} want={expected}"


@_SWEEP_SKIP
@pytest.mark.browser_serial
def test_opt_in_browser_decoder_parity_sweep(tmp_path: Path) -> None:
    """按需 Chromium 解码核对（F3）：全部 18 张实拍双联 + 30 张普通照（含暗景近邻）100% 与桌面一致。"""
    _browser_prereq()
    _clear_desktop_cache()
    rows, diptych_paths = _collect_captures_4288()
    assert len(diptych_paths) == 18

    g250_c0, g250_c1 = hf._SEAM_SPEC_COLS[0][1], hf._SEAM_SPEC_COLS[0][2]
    ordinary_rows = [(p, fk, oi, int(luma[:, g250_c0 : g250_c1 + 1].max())) for p, fk, oi, luma, exp in rows if not exp]
    # 优先选暗中缝普通照（seam_max <= 30），再用等间距普通照补足到 30 张
    dark_ord = [item for item in ordinary_rows if item[3] <= 30]
    dark_set = {item[0].resolve() for item in dark_ord}
    rest_ord = [item for item in ordinary_rows if item[0].resolve() not in dark_set]
    want_ord = 30
    picked_ord = list(dark_ord[:want_ord])
    need = want_ord - len(picked_ord)
    if need > 0 and rest_ord:
        step = max(1, len(rest_ord) // need)
        picked_ord.extend(rest_ord[::step][:need])
    assert len(picked_ord) == want_ord

    files: list[str] = []
    labels: list[str] = []
    expected: list[bool] = []
    for idx, p in enumerate(diptych_paths, start=1):
        files.append(str(p))
        labels.append(f"dip:{idx}")
        expected.append(True)
    for idx, (p, fk, oi, sm) in enumerate(picked_ord, start=1):
        files.append(str(p))
        labels.append(f"ord:{idx}:{fk}#{oi}:sm{sm}")
        expected.append(False)

    desk_verdicts = [hf.is_diptych(Path(f)) for f in files]
    assert desk_verdicts == expected, [(lbl, w, d) for lbl, w, d in zip(labels, expected, desk_verdicts, strict=True) if w is not d]

    out_path = tmp_path / "sweep-browser-out.json"
    manifest = tmp_path / "sweep-browser-manifest.json"
    manifest.write_text(json.dumps({"mode": "detect", "files": files, "out": str(out_path)}), encoding="utf-8")
    proc = _drive(BROWSER_DRIVER, manifest, tmp_path, "")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report.get("pageErrors") == [], report
    assert "error" not in report, report
    got = report["verdicts"]
    assert got == expected, [(lbl, w, g) for lbl, w, g in zip(labels, expected, got, strict=True) if w is not g]
    assert got == desk_verdicts
