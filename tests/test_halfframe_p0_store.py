"""halfframe-p0：图库判定文件、锁外按需补算、后台回填与 all_files 身份回归测试。"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import PORT
from gxr_studio import app as appmod
from gxr_studio import halfframe
from gxr_studio.app import create_app
from gxr_studio.cards.discovery import find_cards
from gxr_studio.cards.importer import ImportOptions, run_import
from gxr_studio.hdr import derived as hdr_derived
from gxr_studio.library import location as lib_loc
from gxr_studio.library.location import card_roots
from gxr_studio.library.store import Library
from gxr_studio.lut.service import LutService
from gxr_studio.render.service import RenderService
from test_halfframe_p0 import (
    _clear_desktop_cache,
    _field_folder,
    _seam_luma_stats,
    _setup_g250_calibrated_home,
    _sorted_pairs,
    _stage_volume_pairs,
)


def _poll_job(client: TestClient, job_id: str) -> dict[str, Any]:
    for _ in range(200):
        data: dict[str, Any] = client.get(f"/api/jobs/{job_id}").json()
        if data["progress"]["state"] in ("done", "failed", "cancelled"):
            return data
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not settle in time")


def _reset_verdict_state(lib: Library) -> None:
    _clear_desktop_cache()
    vf = lib.abspath(halfframe.HALFFRAME_REL)
    if vf.exists():
        vf.unlink()
    lib._halfframe.load()


def test_diptych_corrupt_sidecar_crop_and_split_import_preserve_dng_identity(settings: Any, lib: Library, volumes: Path) -> None:
    _clear_desktop_cache()
    pairs = _sorted_pairs(_field_folder("card250"))
    dng_src, jpg_src = pairs[1]
    orig_dng_sha = hashlib.sha256(dng_src.read_bytes()).hexdigest()

    _stage_volume_pairs(volumes, [pairs[1]])
    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    shots = lib.list_shots()
    assert len(shots) == 1
    shot = shots[0]
    assert shot["diptych"] is True and "dng" not in shot["files"]
    assert shot["all_files"]["dng"]["sha256"] == orig_dng_sha

    sc_path = lib.sidecar_path_for(shot)
    assert sc_path.name.endswith(".gxrs.json")
    assert sc_path.is_file()
    sc_path.write_text("{corrupt sidecar json", encoding="utf-8")
    lib.close()

    with TestClient(
        create_app(settings),
        base_url=f"http://127.0.0.1:{PORT}",
        headers={"X-GXR-Token": settings.token},
    ) as client:
        resp = client.put(
            f"/api/photos/{shot['shot_id']}/crop",
            json={"preset": "free", "rect": [0.05, 0.05, 0.95, 0.95]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["active"] is True
        assert resp.json()["rect"] == [0.05, 0.05, 0.95, 0.95]

    side = json.loads(sc_path.read_text(encoding="utf-8"))
    assert side["source"]["dng"] is not None
    assert side["source"]["dng_sha256"] == orig_dng_sha
    assert side["import"]["sizes"]["dng"] == dng_src.stat().st_size
    assert side["frame"]["width"] and side["frame"]["height"]
    assert side["crop"]["rect"] == [0.05, 0.05, 0.95, 0.95]

    # 先导入 JPG 后补卡上的 DNG（cards/complete.py）也保留 all_files 里的 dng 与 sha
    split_root = settings.library_dir.parent / "split_lib"
    split_vol = volumes.parent / "split_vol"
    dcim = split_vol / "CARD" / "DCIM" / "100RICOH"
    dcim.mkdir(parents=True, exist_ok=True)
    shutil.copy2(jpg_src, dcim / jpg_src.name)

    lib_split = Library(split_root, card_roots((split_vol,))).open()
    try:
        run_import(lib_split, find_cards((split_vol,))[0], ImportOptions(min_free_bytes=0))
        (s_before,) = lib_split.list_shots()
        assert s_before["diptych"] is True and "dng" not in s_before["all_files"]

        split_vol2 = volumes.parent / "split_vol2"
        dcim2 = split_vol2 / "CARD2" / "DCIM" / "100RICOH"
        dcim2.mkdir(parents=True, exist_ok=True)
        shutil.copy2(jpg_src, dcim2 / jpg_src.name)
        shutil.copy2(dng_src, dcim2 / dng_src.name)
        run_import(lib_split, find_cards((split_vol2,))[0], ImportOptions(min_free_bytes=0))
        (s_after,) = lib_split.list_shots()
        assert s_after["diptych"] is True and s_after["has_raw"] == 0
        assert "dng" not in s_after["files"]
        assert s_after["all_files"]["dng"]["sha256"] == orig_dng_sha
        split_sc = json.loads(lib_split.sidecar_path_for(s_after).read_text(encoding="utf-8"))
        assert split_sc["source"]["dng_sha256"] == orig_dng_sha
    finally:
        lib_split.close()


def test_verdict_store_persistence_unbounded_rebuild_and_raw_count(
    settings: Any, lib: Library, volumes: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_desktop_cache()
    assert halfframe.HALFFRAME_REL in lib_loc.STUDIO_PATHS

    pairs = _sorted_pairs(_field_folder("card250"))
    selected = [pairs[0], pairs[1], pairs[10]]  # 单格(has_raw) + 双联(exclude from raw_count) + XPan(has_raw)
    _stage_volume_pairs(volumes, selected)
    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))

    verdict_path = lib.abspath(halfframe.HALFFRAME_REL)
    assert verdict_path.is_file()
    stored = json.loads(verdict_path.read_text(encoding="utf-8"))
    sha_p0 = hashlib.sha256(pairs[0][1].read_bytes()).hexdigest()
    sha_p1 = hashlib.sha256(pairs[1][1].read_bytes()).hexdigest()
    assert stored["version"] == 1
    assert stored["verdicts"][sha_p0] is False
    assert stored["verdicts"][sha_p1] is True

    dates = lib.dates()
    assert len(dates) == 1
    assert dates[0]["count"] == 3
    assert dates[0]["raw_count"] == 2  # 双联已从 raw_count 扣除
    lib.close()

    # 重开图库时直接读 halfframe-v1.json，list_shots / dates / get_shot 全程零 JPEG 解码
    _clear_desktop_cache()
    monkeypatch.setattr(halfframe, "detect_diptych", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("unexpected decode")))
    lib2 = Library(settings.library_dir, card_roots((volumes,))).open()
    try:
        shots2 = lib2.list_shots()
        by_sha = {s["files"]["jpg"]["sha256"]: s for s in shots2}
        assert by_sha[sha_p0]["diptych"] is False and by_sha[sha_p0]["has_raw"] == 1
        assert by_sha[sha_p1]["diptych"] is True and by_sha[sha_p1]["has_raw"] == 0
        assert lib2.dates()[0]["raw_count"] == 2
        got = lib2.get_shot(by_sha[sha_p1]["shot_id"])
        assert got is not None and got["diptych"] is True
    finally:
        lib2.close()

    # 删除 halfframe-v1.json 后执行 Library.rebuild()：在锁外重建并重新写盘
    monkeypatch.undo()
    real_detect = halfframe.detect_diptych
    lib3 = Library(settings.library_dir, card_roots((volumes,))).open()
    try:
        verdict_path.unlink()
        lib3._halfframe.load()

        def tracked(p: Any) -> bool | None:
            assert not lib3.lock.locked(), "detect_diptych ran while holding lib.lock"
            return real_detect(p)

        monkeypatch.setattr(halfframe, "detect_diptych", tracked)
        lib3.rebuild()
        assert verdict_path.is_file()
        rebuilt = json.loads(verdict_path.read_text(encoding="utf-8"))
        assert rebuilt["verdicts"][sha_p0] is False and rebuilt["verdicts"][sha_p1] is True
        assert lib3.dates()[0]["raw_count"] == 2
    finally:
        lib3.close()

    # 无 4096 上限驱逐：VerdictStore 与全局内存 _CACHE 写入 >4096 项仍保留最初的条目
    big_store = halfframe.VerdictStore(tmp_path / "big-verdicts.json")
    big_items = {f"{i:064x}": (i % 7 == 0) for i in range(4200)}
    big_store.put_many(big_items, replace=True, save=True)
    reloaded = halfframe.VerdictStore(tmp_path / "big-verdicts.json")
    reloaded.load()
    assert reloaded.get(f"{0:064x}") is True and reloaded.get(f"{4199:064x}") is False

    _clear_desktop_cache()
    try:
        for i in range(4200):
            halfframe._cache_put(f"k-{i:04d}", i % 2 == 0)
        with halfframe._CACHE_LOCK:
            assert len(halfframe._CACHE) == 4200
            assert halfframe._CACHE["k-0000"] is True
            assert halfframe._CACHE["k-4199"] is False
    finally:
        _clear_desktop_cache()


def test_pre_backfill_multi_row_no_decode_and_render_export_batch_hdr_lut(
    settings: Any, lib: Library, volumes: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_desktop_cache()
    pairs = _sorted_pairs(_field_folder("card250"))
    _stage_volume_pairs(volumes, [pairs[0], pairs[1]])
    _setup_g250_calibrated_home(settings)
    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))

    real_detect = halfframe.detect_diptych
    calls: list[Path] = []

    def spy_detect(path: Any) -> bool | None:
        assert not lib.lock.locked(), "detect_diptych must execute outside lib.lock"
        calls.append(Path(path))
        return real_detect(path)

    monkeypatch.setattr(halfframe, "detect_diptych", spy_detect)

    # 1. 多行 list_shots 在没有判决文件时绝不解码 JPG
    _reset_verdict_state(lib)
    shots = lib.list_shots(limit=100)
    assert len(calls) == 0, f"multi-row list_shots unexpectedly decoded {calls}"
    ordered = sorted(shots, key=lambda r: r["name"])
    s1, s2 = ordered[0], ordered[1]

    # 2. HDR 衍生 pending / run_pending（回填前）在锁外单张补算并跳过双联
    _reset_verdict_state(lib)
    pending_list = hdr_derived.pending(lib, lib.list_shots(limit=100))
    assert [s["shot_id"] for s in pending_list] == [s1["shot_id"]]
    assert len(calls) == 2

    _reset_verdict_state(lib)
    calls.clear()
    prog = hdr_derived.run_pending(lib, hdr_derived.HdrProgress(), threading.Event())
    assert prog.state == "done" and prog.done == 1
    assert hdr_derived.status_of(lib, s2) in ("pending", "none")
    assert not lib.abspath(f".studio/hdr/{s2['shot_id']}.jpg").exists()
    assert not lib.abspath(f".studio/hdr/{s2['shot_id']}.json").exists()

    # 3. LUT pick_photos(ids=None)（回填前）不会挑选双联做 RAW LUT 拟合
    _reset_verdict_state(lib)
    render_svc = RenderService(settings.home_dir, lib)
    picked = LutService(settings.home_dir, lib, render_svc).pick_photos(limit=10)
    assert [p["shot_id"] for p in picked] == [s1["shot_id"]]

    # 4. 关掉启动回填任务，测回填前 HTTP 列表、/render?stage=final、auto 导出与按日期批量导出
    monkeypatch.setattr(appmod, "start_halfframe_job", lambda _ctx: None)
    lib.close()

    with TestClient(
        create_app(settings),
        base_url=f"http://127.0.0.1:{PORT}",
        headers={"X-GXR-Token": settings.token},
    ) as client:
        calls.clear()
        _clear_desktop_cache()
        vf = settings.library_dir / halfframe.HALFFRAME_REL
        if vf.exists():
            vf.unlink()
        client.app.state.ctx.lib._halfframe.load()
        rows = client.get("/api/photos").json()
        assert len(rows) == 2 and len(calls) == 0

        # 回填前 /render?stage=final 保留黑缝并持久化判决
        r_final = client.get(f"/api/photos/{s2['shot_id']}/render?stage=final&size=preview")
        assert r_final.status_code == 200
        with Image.open(io.BytesIO(r_final.content)) as im:
            cmax, lmax, rmax = _seam_luma_stats(im)
        assert cmax <= 12 and lmax > 25 and rmax > 25

        # 清空判决再测回填前默认 auto 导出
        _clear_desktop_cache()
        if vf.exists():
            vf.unlink()
        client.app.state.ctx.lib._halfframe.load()
        exp1_dir = tmp_path / "pre_single"
        exp1_dir.mkdir()
        r_exp = client.post(
            f"/api/photos/{s2['shot_id']}/export",
            json={"size": "original", "quality_tier": "high", "dest_dir": str(exp1_dir)},
        )
        assert r_exp.status_code == 200
        with Image.open(r_exp.json()["path"]) as im:
            assert im.size == (4288, 2848)
            cmax, lmax, rmax = _seam_luma_stats(im)
        assert cmax <= 12 and lmax > 25 and rmax > 25

        # 清空判决再测回填前按日期批量导出 POST /api/export（多行 list_shots 来源）
        _clear_desktop_cache()
        if vf.exists():
            vf.unlink()
        client.app.state.ctx.lib._halfframe.load()
        exp_batch_dir = tmp_path / "pre_batch"
        exp_batch_dir.mkdir()
        job_resp = client.post(
            "/api/export",
            json={"date": s2["date"], "size": "original", "quality_tier": "high", "dest_dir": str(exp_batch_dir)},
        )
        assert job_resp.status_code == 202
        done_job = _poll_job(client, job_resp.json()["id"])
        results = {item["id"]: item for item in done_job["progress"]["results"]}
        with Image.open(results[s2["shot_id"]]["path"]) as im:
            assert im.size == (4288, 2848)
            cmax, lmax, rmax = _seam_luma_stats(im)
        assert cmax <= 12 and lmax > 25 and rmax > 25


def test_startup_halfframe_backfill_populates_verdicts_and_dates(
    settings: Any, lib: Library, volumes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_desktop_cache()
    pairs = _sorted_pairs(_field_folder("card250"))
    _stage_volume_pairs(volumes, [pairs[0], pairs[1]])
    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    sha1 = hashlib.sha256(pairs[0][1].read_bytes()).hexdigest()
    sha2 = hashlib.sha256(pairs[1][1].read_bytes()).hexdigest()

    verdict_path = lib.abspath(halfframe.HALFFRAME_REL)
    verdict_path.unlink()
    lib.close()
    _clear_desktop_cache()

    real_detect = halfframe.detect_diptych
    saw_unlocked: list[bool] = []

    with TestClient(
        create_app(settings),
        base_url=f"http://127.0.0.1:{PORT}",
        headers={"X-GXR-Token": settings.token},
    ) as client:
        for _ in range(200):
            if verdict_path.is_file():
                data = json.loads(verdict_path.read_text(encoding="utf-8"))
                if sha1 in data.get("verdicts", {}) and sha2 in data.get("verdicts", {}):
                    break
            time.sleep(0.05)
        else:
            raise AssertionError("halfframe startup backfill did not populate verdict file")

        dates = client.get("/api/dates").json()
        assert len(dates) == 1
        assert dates[0]["count"] == 2
        assert dates[0]["raw_count"] == 1

    # 额外直接验证 run_backfill 过程中不持有 lib.lock
    lib_check = Library(settings.library_dir, card_roots((volumes,))).open()
    try:
        todo = [(s["files"]["jpg"]["relpath"], s["files"]["jpg"]["sha256"]) for s in lib_check.list_shots() if "jpg" in s["files"]]
        verdict_path.unlink()
        lib_check._halfframe.load()

        def spy_backfill(p: Any) -> bool | None:
            saw_unlocked.append(not lib_check.lock.locked())
            return real_detect(p)

        monkeypatch.setattr(halfframe, "detect_diptych", spy_backfill)
        prog = halfframe.run_backfill(lib_check, todo, halfframe.HalfframeProgress(total=len(todo)), threading.Event())
        assert prog.state == "done" and prog.diptychs == 1
        assert saw_unlocked and all(saw_unlocked)
    finally:
        lib_check.close()
