"""halfframe-p1c 实拍验收（本地 `GXR_STUDIO_TEST_PARENT`，缺失时自动跳过；不输出照片名/时间/EXIF）。

覆盖：
- 验收 1：card250 全部 3 对衍生 HDR 为 ready，主图与相机双联逐字节一致，每半对齐位移 (0, 0)，
  转正后接缝增益严格 0 档，并检查每对高光增益（含提亮导出验证 ≥ 0.5 档）；
- 验收 2：diptych / singles / both 导出 HDR 均通过独立容器解析、主图与 hdr=False 逐字节一致、
  单格增益图等于双联对应半格切片；
- 验收 4：card250 第 7 张、第 11 张（XPan）、card249 普通 3:2（第 5 张）与 card250 第 3 张的
  衍生 HDR 与导出 HDR 结果与 base `ee33a95` 逐字节一致。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
from fastapi.testclient import TestClient

import hdr_support as hs
from conftest import PORT
from gxr_studio import export as expmod
from gxr_studio import halfframe_compose as hfc
from gxr_studio.app import create_app
from gxr_studio.cards.discovery import find_cards
from gxr_studio.cards.importer import ImportOptions, run_import
from gxr_studio.hdr import derived as hdr_derived
from gxr_studio.hdr import gain as g
from gxr_studio.library.store import Library
from gxr_studio.raw import open_dng
from test_halfframe_p0 import (
    REPO,
    _clear_desktop_cache,
    _field_folder,
    _setup_g250_calibrated_home,
    _sorted_pairs,
    _stage_volume_pairs,
)
from test_halfframe_p1a_field import _index_shots_by_jpg_sha

BASE_COMMIT = "ee33a95"


def _seam_slice(arr: np.ndarray, orientation: int) -> np.ndarray:
    inv = 8 if orientation == 6 else (6 if orientation == 8 else orientation)
    sensor = hfc.orient_array(arr, inv)
    geo = hfc.proxy_geometry(sensor.shape[1])
    return sensor[:, geo.seam_x0 : geo.right_x0]


# --------------------------------------------------------------------------- 验收 1：card250 3 对衍生 HDR


def test_field_card250_all_three_pairs_derived_hdr(
    settings: Any,
    lib: Library,
    volumes: Path,
) -> None:
    _clear_desktop_cache()
    card250_pairs = _sorted_pairs(_field_folder("card250"))
    _stage_volume_pairs(volumes, card250_pairs[:6])
    _setup_g250_calibrated_home(settings)
    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    by_idx = _index_shots_by_jpg_sha(lib, card250_pairs[:6])
    assert len(by_idx) == 6

    pending_ids = {s["shot_id"] for s in hdr_derived.pending(lib, lib.list_shots(limit=100))}
    for i1, i2, want_half in ((1, 2, "left"), (3, 4, "right"), (5, 6, "left")):
        shot2 = lib.get_shot(by_idx[i2]["shot_id"])
        assert shot2 is not None
        assert shot2["pair"]["first_half"] == want_half
        assert shot2["shot_id"] in pending_ids, f"pair ({i1},{i2}) not in derived HDR pending"
        assert hdr_derived.eligible(shot2) is True

        status = hdr_derived.generate(lib, shot2)
        assert status == hdr_derived.READY, f"pair ({i1},{i2}) derived HDR status={status}"
        assert hdr_derived.status_of(lib, shot2) == hdr_derived.READY

        cam_dip = card250_pairs[i2 - 1][1].read_bytes()
        hdr_bytes = hdr_derived.hdr_path(lib, shot2["shot_id"]).read_bytes()
        checked = hs.check_file(cam_dip, hdr_bytes)
        assert checked["ok"], f"pair ({i1},{i2}) container failed: {checked['fail']}"

        # 独立核对：pair_raw_luma 整幅与逐半对齐位移均为 (0, 0)，转正后接缝增益严格为 0 档
        ori, first_half = hfc.pair_orientation_and_half(shot2)
        s1, s2 = hfc.pair_half_shots(shot2)
        raw = g.pair_raw_luma(
            open_dng(lib.abspath(s1["files"]["dng"]["relpath"])),
            open_dng(lib.abspath(s2["files"]["dng"]["relpath"])),
            first_half=first_half,
            orientation=ori,
            first_jpeg=lib.abspath(s1["files"]["jpg"]["relpath"]).read_bytes(),
        )
        out2, stats = g.hdr_from_camera_jpeg(cam_dip, raw)
        assert out2 == hdr_bytes
        assert stats["alignment"]["shift"] == [0, 0], f"pair ({i1},{i2}) whole shift={stats['alignment']}"
        assert stats["alignment"]["tiles_moved"] == 0 and stats["alignment"]["tiles_unverified"] == 0

        rgb = g.decode_rgb(cam_dip)
        sdr_y = g.sdr_luma_grid(rgb, g.Params().map_factor)
        stored_y = g.to_storage(raw.y, ori)
        stored_c = g.to_storage(raw.clip, ori)
        s_geo = hfc.proxy_geometry(sdr_y.shape[1])
        r_geo = hfc.proxy_geometry(stored_y.shape[1])
        al_left = g.align(sdr_y[:, : s_geo.half_w], stored_y[:, : r_geo.half_w])
        al_right = g.align(sdr_y[:, s_geo.right_x0 :], stored_y[:, r_geo.right_x0 :])
        assert (al_left.dy, al_left.dx) == (0, 0), f"pair ({i1},{i2}) left half shift={(al_left.dy, al_left.dx)}"
        assert (al_right.dy, al_right.dx) == (0, 0), f"pair ({i1},{i2}) right half shift={(al_right.dy, al_right.dx)}"

        # 接缝增益（编码前与解码后，按方向转正后）严格为 0 档
        al = g.align(sdr_y, stored_y)
        ry, rc = g.register(g.RawLuma(stored_y, stored_c), sdr_y.shape, al, sdr_y.shape)
        gain_res = g.compute_gain(sdr_y, ry, rc, g.Params())
        oriented_delta = hfc.orient_array(gain_res.delta, ori)
        assert float(_seam_slice(oriented_delta, ori).max()) == 0.0
        oriented_dec = hfc.orient_array(checked["log_gain"], ori)
        assert float(_seam_slice(oriented_dec, ori).max()) == 0.0


# --------------------------------------------------------------------------- 验收 2：card250 3 对导出 HDR（diptych / singles / both）


def test_field_card250_export_hdr_diptych_singles_both_and_gain_sanity(
    settings: Any,
    lib: Library,
    volumes: Path,
    tmp_path: Path,
) -> None:
    _clear_desktop_cache()
    card250_pairs = _sorted_pairs(_field_folder("card250"))
    _stage_volume_pairs(volumes, card250_pairs[:6])
    _setup_g250_calibrated_home(settings)
    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    by_idx = _index_shots_by_jpg_sha(lib, card250_pairs[:6])
    assert len(by_idx) == 6
    lib.close()

    max_gains_boosted: list[float] = []
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        for i1, i2, want_half in ((1, 2, "left"), (3, 4, "right"), (5, 6, "left")):
            sid2 = by_idx[i2]["shot_id"]
            ori = int(by_idx[i2].get("orientation") or 1)

            # 1) 默认拍摄配方：diptych / singles / both 全部通过容器校验、主图与 hdr=False 逐字节一致、单格等于双联切片
            r_dip_sdr = client.post(
                f"/api/photos/{sid2}/export",
                json={
                    "halfframe": "diptych",
                    "size": "original",
                    "quality_tier": "high",
                    "hdr": False,
                    "dest_dir": str(tmp_path / f"p{i2}_dip_sdr"),
                },
            )
            r_dip_hdr = client.post(
                f"/api/photos/{sid2}/export",
                json={
                    "halfframe": "diptych",
                    "size": "original",
                    "quality_tier": "high",
                    "hdr": True,
                    "dest_dir": str(tmp_path / f"p{i2}_dip_hdr"),
                },
            )
            assert r_dip_sdr.status_code == 200 and r_dip_hdr.status_code == 200
            dip_body = r_dip_hdr.json()
            assert dip_body["hdr"] is True and dip_body.get("hdr_note") is None
            assert Path(dip_body["path"]).name.endswith("_hdr.jpg")
            dip_chk = hs.check_file(Path(r_dip_sdr.json()["path"]).read_bytes(), Path(dip_body["path"]).read_bytes())
            assert dip_chk["ok"], f"pair ({i1},{i2}) diptych: {dip_chk['fail']}"
            assert float(_seam_slice(dip_chk["log_gain"], ori).max()) == 0.0

            r_s_sdr = client.post(
                f"/api/photos/{sid2}/export",
                json={
                    "halfframe": "singles",
                    "size": "original",
                    "quality_tier": "high",
                    "hdr": False,
                    "dest_dir": str(tmp_path / f"p{i2}_s_sdr"),
                },
            )
            r_s_hdr = client.post(
                f"/api/photos/{sid2}/export",
                json={
                    "halfframe": "singles",
                    "size": "original",
                    "quality_tier": "high",
                    "hdr": True,
                    "dest_dir": str(tmp_path / f"p{i2}_s_hdr"),
                },
            )
            assert r_s_sdr.status_code == 200 and r_s_hdr.status_code == 200
            s_sdr_files = r_s_sdr.json()["files"]
            s_hdr_files = r_s_hdr.json()["files"]
            assert len(s_hdr_files) == 2
            s_gains: dict[str, np.ndarray] = {}
            for fs, fh in zip(s_sdr_files, s_hdr_files, strict=True):
                assert fh["hdr"] is True and fh.get("hdr_note") is None
                assert Path(fh["path"]).name.endswith("_half_hdr.jpg")
                chk = hs.check_file(Path(fs["path"]).read_bytes(), Path(fh["path"]).read_bytes())
                assert chk["ok"], f"pair ({i1},{i2}) single {fh['role']}: {chk['fail']}"
                s_gains[fh["role"]] = chk["log_gain"]

            dip_h1, dip_h2 = hfc.split_diptych_halves(dip_chk["log_gain"], first_half=want_half, orientation=ori)
            for role, dip_slice in (("first", dip_h1), ("second", dip_h2)):
                assert s_gains[role].shape == dip_slice.shape
                diff = np.abs(s_gains[role][2:-2, 2:-2] - dip_slice[2:-2, 2:-2])
                assert float(np.percentile(diff, 95)) < 0.15, (i1, i2, role, float(np.percentile(diff, 95)))

            r_b_sdr = client.post(
                f"/api/photos/{sid2}/export",
                json={"halfframe": "both", "size": "small", "quality_tier": "high", "hdr": False, "dest_dir": str(tmp_path / f"p{i2}_b_sdr")},
            )
            r_b_hdr = client.post(
                f"/api/photos/{sid2}/export",
                json={"halfframe": "both", "size": "small", "quality_tier": "high", "hdr": True, "dest_dir": str(tmp_path / f"p{i2}_b_hdr")},
            )
            assert r_b_sdr.status_code == 200 and r_b_hdr.status_code == 200
            b_sdr_files = r_b_sdr.json()["files"]
            b_hdr_files = r_b_hdr.json()["files"]
            assert len(b_hdr_files) == 3
            for fs, fh in zip(b_sdr_files, b_hdr_files, strict=True):
                assert fh["hdr"] is True and fh.get("hdr_note") is None
                assert Path(fh["path"]).name.endswith("_hdr.jpg")
                chk = hs.check_file(Path(fs["path"]).read_bytes(), Path(fh["path"]).read_bytes())
                assert chk["ok"], f"pair ({i1},{i2}) both {fh['role']}: {chk['fail']}"

            # 2) +2.0 EV 导出：card250 三对机内双联都是偏暗室内场景（全图线性亮度 < knee=0.45），
            # 拉亮 +2.0 EV 可验证真实半格双 DNG 的高光余量能产生 ≥ 0.5 档的非平凡提亮，同时接缝为 0 且单格匹配双联切片
            assert client.put(f"/api/photos/{sid2}/edit", json={"adjust": {"exposure_ev": 2.0}}).status_code == 200
            ev_bases = ("camera-jpg", "auto") if (i1, i2) == (1, 2) else ("camera-jpg",)
            for ev_base in ev_bases:
                r_ev_sdr = client.post(
                    f"/api/photos/{sid2}/export",
                    json={
                        "halfframe": "both",
                        "source": ev_base,
                        "size": "original",
                        "quality_tier": "high",
                        "hdr": False,
                        "dest_dir": str(tmp_path / f"p{i2}_ev_{ev_base}_sdr"),
                    },
                )
                r_ev_hdr = client.post(
                    f"/api/photos/{sid2}/export",
                    json={
                        "halfframe": "both",
                        "source": ev_base,
                        "size": "original",
                        "quality_tier": "high",
                        "hdr": True,
                        "dest_dir": str(tmp_path / f"p{i2}_ev_{ev_base}_hdr"),
                    },
                )
                assert r_ev_sdr.status_code == 200 and r_ev_hdr.status_code == 200
                ev_sdr = {f["role"]: Path(f["path"]).read_bytes() for f in r_ev_sdr.json()["files"]}
                ev_hdr_items = r_ev_hdr.json()["files"]
                for f in ev_hdr_items:
                    assert f["hdr"] is True, (
                        f"pair ({i1},{i2}) +2.0EV {ev_base} {f['role']} failed HDR: hdr={f.get('hdr')} note={f.get('hdr_note')}"
                    )
                ev_hdr = {f["role"]: Path(f["path"]).read_bytes() for f in ev_hdr_items}
                ev_dip_chk = hs.check_file(ev_sdr["diptych"], ev_hdr["diptych"])
                assert ev_dip_chk["ok"], ev_dip_chk["fail"]
                ev_seam = _seam_slice(ev_dip_chk["log_gain"], ori)
                assert float(ev_seam.max()) < 0.1 and float(ev_seam.mean()) < 0.01
                # 接缝在 1/4 网格上位于 [522, 550)（28 列）：首尾各 6 列与照片共享 8×8 JPEG DCT 块，中间 16 列为纯接缝块，解码后严格为 0
                ev_seam_inner = ev_seam[:, 6:-6] if ori in (1, 3) else ev_seam[6:-6, :]
                assert float(ev_seam_inner.max()) == 0.0
                max_gains_boosted.append(float(ev_dip_chk["meta"]["GainMapMax"]))
                ev_h1, ev_h2 = hfc.split_diptych_halves(ev_dip_chk["log_gain"], first_half=want_half, orientation=ori)
                for role, dip_slice in (("first", ev_h1), ("second", ev_h2)):
                    s_chk = hs.check_file(ev_sdr[role], ev_hdr[role])
                    assert s_chk["ok"], s_chk["fail"]
                    diff = np.abs(s_chk["log_gain"][2:-2, 2:-2] - dip_slice[2:-2, 2:-2])
                    assert float(np.median(diff)) < 0.08 and float(np.percentile(diff, 95)) < 0.25, (
                        i1,
                        i2,
                        ev_base,
                        role,
                        float(np.median(diff)),
                        float(np.percentile(diff, 95)),
                    )
            assert client.put(f"/api/photos/{sid2}/edit", json={"adjust": {"exposure_ev": 0.0}}).status_code == 200

    assert max(max_gains_boosted) >= 0.5, f"expected >= 0.5 stop highlight gain, got {max_gains_boosted}"


# --------------------------------------------------------------------------- 验收 4：非半格 HDR 输出与 ee33a95 逐字节一致


def test_field_non_pair_hdr_outputs_byte_identical_to_base_ee33a95(
    settings: Any,
    lib: Library,
    volumes: Path,
    tmp_path: Path,
) -> None:
    _clear_desktop_cache()
    pairs250 = _sorted_pairs(_field_folder("card250"))
    pairs249 = _sorted_pairs(_field_folder("card249"))
    selected = [
        ("card250_3_highgain", pairs250[2]),
        ("card250_7_unpaired", pairs250[6]),
        ("card250_11_xpan", pairs250[10]),
        ("card249_5_ordinary", pairs249[4]),
    ]
    _stage_volume_pairs(volumes, [p for _, p in selected])
    _setup_g250_calibrated_home(settings)
    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    by_sha = {s["files"]["jpg"]["sha256"]: s for s in lib.list_shots(limit=100)}
    assert len(by_sha) == len(selected)

    shot_ids: dict[str, str] = {}
    head_derived: dict[str, tuple[str, bytes]] = {}
    for label, (_dng, jpg_path) in selected:
        sha = hashlib.sha256(jpg_path.read_bytes()).hexdigest()
        sid = by_sha[sha]["shot_id"]
        shot_ids[label] = sid
        shot = lib.get_shot(sid)
        assert shot is not None
        st = hdr_derived.generate(lib, shot)
        path = hdr_derived.hdr_path(lib, sid) if st == hdr_derived.READY else hdr_derived.status_path(lib, sid)
        head_derived[label] = (st, path.read_bytes())
        # 清掉衍生文件让 base 子进程重新生成，避免直接命中缓存
        path.unlink(missing_ok=True)
    lib.close()

    icc_bytes = expmod.srgb_icc()
    icc_path = tmp_path / "srgb.icc"
    icc_path.write_bytes(icc_bytes)
    head_exports: dict[str, tuple[bool | None, bytes]] = {}
    orig_icc = expmod._ICC
    expmod._ICC = icc_bytes
    try:
        with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
            for label, sid in shot_ids.items():
                exp_dir = tmp_path / f"head_{label}"
                exp_dir.mkdir()
                resp = client.post(
                    f"/api/photos/{sid}/export",
                    json={"size": "original", "quality_tier": "high", "hdr": True, "dest_dir": str(exp_dir)},
                )
                assert resp.status_code == 200, f"{label}: {resp.text}"
                body = resp.json()
                head_exports[label] = (body.get("hdr"), Path(body["path"]).read_bytes())
    finally:
        expmod._ICC = orig_icc

    has_base = subprocess.run(["git", "cat-file", "-e", f"{BASE_COMMIT}^{{commit}}"], cwd=REPO, check=False, capture_output=True)
    assert has_base.returncode == 0, f"git commit {BASE_COMMIT} must be reachable"
    base_dir = tmp_path / f"base_{BASE_COMMIT}"
    base_dir.mkdir()
    tar = subprocess.run(["git", "archive", BASE_COMMIT, "src"], cwd=REPO, capture_output=True, check=True)
    subprocess.run(["tar", "-xf", "-", "-C", str(base_dir)], input=tar.stdout, check=True)

    worker = tmp_path / "hdr_base.py"
    worker.write_text(
        """
import json
import sys
from pathlib import Path

src_root = Path(sys.argv[1])
home_dir = Path(sys.argv[2])
lib_dir = Path(sys.argv[3])
export_dir = Path(sys.argv[4])
vol_dir = Path(sys.argv[5])
out_dir = Path(sys.argv[6])
icc_bytes = Path(sys.argv[7]).read_bytes()
port = int(sys.argv[8])
token = sys.argv[9]
shot_ids = json.loads(sys.argv[10])

sys.meta_path = [f for f in sys.meta_path if "__editable" not in repr(f) and "gxr_studio" not in repr(f)]
sys.path.insert(0, str(src_root))

import gxr_studio
assert Path(gxr_studio.__file__).resolve().is_relative_to(src_root.resolve()), gxr_studio.__file__

from fastapi.testclient import TestClient
from gxr_studio import export as expmod
from gxr_studio.app import create_app
from gxr_studio.config import Settings
from gxr_studio.hdr import derived as hdr_derived
from gxr_studio.library.store import Library

expmod._ICC = icc_bytes
lib = Library(lib_dir).open()
derived_out = {}
try:
    for label, sid in shot_ids.items():
        shot = lib.get_shot(sid)
        assert shot is not None
        st = hdr_derived.generate(lib, shot)
        p = hdr_derived.hdr_path(lib, sid) if st == hdr_derived.READY else hdr_derived.status_path(lib, sid)
        derived_out[label] = [st, str(p)]
finally:
    lib.close()

cfg = Settings(
    home_dir=home_dir,
    library_dir=lib_dir,
    export_dir=export_dir,
    volumes_roots=(vol_dir,),
    port=port,
    token=token,
    min_free_bytes=0,
)
export_out = {}
with TestClient(create_app(cfg), base_url=f"http://127.0.0.1:{port}", headers={"X-GXR-Token": token}) as client:
    for label, sid in shot_ids.items():
        d = out_dir / label
        d.mkdir(parents=True, exist_ok=True)
        resp = client.post(
            f"/api/photos/{sid}/export",
            json={"size": "original", "quality_tier": "high", "hdr": True, "dest_dir": str(d)},
        )
        assert resp.status_code == 200, (label, resp.text)
        body = resp.json()
        export_out[label] = [body.get("hdr"), body["path"]]
print(json.dumps({"derived": derived_out, "export": export_out}))
""".strip()
        + "\n",
        encoding="utf-8",
    )
    out_base = tmp_path / "out_base"
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            str(worker),
            str(base_dir / "src"),
            str(settings.home_dir),
            str(settings.library_dir),
            str(settings.export_dir),
            str(volumes),
            str(out_base),
            str(icc_path),
            str(PORT),
            settings.token,
            json.dumps(shot_ids),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    base_res = json.loads(proc.stdout.splitlines()[-1])
    for label, _ in selected:
        b_st, b_dp = base_res["derived"][label]
        assert head_derived[label][0] == b_st, f"{label} derived status diverged"
        assert head_derived[label][1] == Path(b_dp).read_bytes(), f"{label} derived HDR bytes diverged from {BASE_COMMIT}"
        b_hdr, b_ep = base_res["export"][label]
        assert head_exports[label][0] == b_hdr, f"{label} export hdr flag diverged"
        assert head_exports[label][1] == Path(b_ep).read_bytes(), f"{label} export HDR bytes diverged from {BASE_COMMIT}"
