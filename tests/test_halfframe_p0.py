"""halfframe-p0：相机半格双联 JPG 识别，并在桌面与手机单关口按只有 JPG 处理。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import uvicorn
from fastapi.testclient import TestClient
from PIL import Image

from conftest import PORT, REAL_HOME
from gxr_studio import export as expmod
from gxr_studio import halfframe
from gxr_studio.app import create_app
from gxr_studio.camdata import calib, store
from gxr_studio.cards.discovery import find_cards
from gxr_studio.cards.importer import ImportOptions, run_import
from gxr_studio.library.store import Library
from support import samples
from test_g250 import _browser_prereq, _bundle_paths
from test_lut import cube_bytes
from test_mobile_ui_browser import RUNTIME, _drive

REPO = Path(__file__).resolve().parents[1]
WEB = REPO / "web"
MOBILE_UNIT = REPO / "tests" / "support" / "mobile_halfframe_p0_unit.mjs"
BROWSER_DRIVER = REPO / "tests" / "support" / "mobile_halfframe_p0_browser.mjs"

# 3a556bd 基线（导入 g250 相机数据与开发标定后）的非双联默认导出字节长度。
BASELINE_EXPORT_LENGTHS: dict[str, int] = {
    "card250_1": 3288569,
    "card250_11_xpan": 2383615,
    "card249_5_ordinary": 3854098,
}

FIELD_EXPECTATIONS: dict[str, tuple[int, set[int]]] = {
    "card250": (11, {2, 4, 6}),
    "card249": (7, {2, 4}),
    "vertical": (6, {2, 4, 6}),
    "probe251": (9, {2, 4, 6}),
}


def _clear_desktop_cache() -> None:
    with halfframe._CACHE_LOCK:
        halfframe._CACHE.clear()


def _sorted_jpgs(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.upper() == ".JPG")


def _sorted_pairs(folder: Path) -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    for jpg in _sorted_jpgs(folder):
        dng = next((jpg.with_suffix(ext) for ext in (".DNG", ".dng") if jpg.with_suffix(ext).is_file()), None)
        if dng is not None:
            pairs.append((dng, jpg))
    return pairs


def _field_folder(name: str) -> Path:
    folder = samples.captures() / "r6-field-20261118" / name
    if not folder.is_dir():
        samples.missing(f"captures/r6-field-20261118/{name} 不在本机")
    return folder


def _stage_volume_pairs(volumes: Path, selected: list[tuple[Path, Path]]) -> None:
    dcim = volumes / "CARD" / "DCIM" / "100RICOH"
    dcim.mkdir(parents=True, exist_ok=True)
    for dng, jpg in selected:
        shutil.copy2(dng, dcim / dng.name)
        shutil.copy2(jpg, dcim / jpg.name)


def _setup_g250_calibrated_home(settings: Any) -> None:
    dat_path, fx_path = _bundle_paths(250)
    raw = dat_path.read_bytes()
    fx = fx_path.read_bytes()
    store.import_from_dat(raw, settings.home_dir, library=fx)
    sources = calib.dev_sources(samples.sample_parent())
    if not sources:
        samples.missing("研究目录里的标定文件不在本机")
    calib.import_calibration(settings.home_dir, sources)


def _seam_luma_stats(img: Image.Image) -> tuple[int, int, int]:
    """返回横向图居中 5 列与左右各退 50 列窄带的最大灰度（不读文件名或 EXIF）。"""
    gray = np.asarray(img.convert("L"), dtype=np.uint8)
    w = gray.shape[1]
    cx = w // 2
    offset = max(20, w // 40)
    band = max(2, w // 200)
    center_max = int(gray[:, max(0, cx - 2) : min(w, cx + 3)].max())
    left_max = int(gray[:, max(0, cx - offset - band) : max(1, cx - offset + band)].max())
    right_max = int(gray[:, min(w - 1, cx + offset - band) : min(w, cx + offset + band)].max())
    return center_max, left_max, right_max


def _stage_png_seam_stats(png_path: Path) -> tuple[float, int, int, int]:
    """裁剪掉上下黑边与边缘叠加，测量渲染画面中央列带均值/峰值及左右两侧最大灰度。"""
    with Image.open(png_path) as im:
        gray = np.asarray(im.convert("L"), dtype=np.uint8)
    active_rows = gray.max(axis=1) > 25
    assert bool(active_rows.any()), "舞台截图全黑"
    active = gray[active_rows]
    margin = max(1, len(active) // 8)
    core = active[margin:-margin]
    w = core.shape[1]
    cx = w // 2
    offset = max(12, w // 12)
    band = core[:, cx - 1 : cx + 2]
    center_mean = float(band.mean())
    center_max = int(band.max())
    left_max = int(core[:, max(0, cx - offset - 4) : max(1, cx - offset + 4)].max())
    right_max = int(core[:, min(w - 1, cx + offset - 4) : min(w, cx + offset + 4)].max())
    return center_mean, center_max, left_max, right_max


def _make_synthetic_jpeg(
    path: Path,
    width: int = 4288,
    height: int = 2848,
    *,
    seam_range: tuple[int, int] | None = None,
    fill: tuple[int, int, int] = (140, 140, 140),
    exif_orientation: int | None = None,
) -> Path:
    arr = np.empty((height, width, 3), dtype=np.uint8)
    arr[:, :] = fill
    if seam_range is not None:
        x0, x1 = seam_range
        arr[:, x0 : x1 + 1] = 0
    im = Image.fromarray(arr, mode="RGB")
    save_kwargs: dict[str, Any] = {"format": "JPEG", "quality": 92, "subsampling": 0}
    if exif_orientation is not None:
        exif = Image.Exif()
        exif[274] = exif_orientation
        save_kwargs["exif"] = exif.tobytes()
    im.save(path, **save_kwargs)
    return path


def _synthetic_suite(tmp_path: Path) -> list[tuple[str, Path, bool]]:
    return [
        ("g250_seam", _make_synthetic_jpeg(tmp_path / "syn_g250.jpg", seam_range=(2088, 2199)), True),
        ("legacy48_seam", _make_synthetic_jpeg(tmp_path / "syn_legacy48.jpg", seam_range=(2120, 2167)), True),
        ("shifted_seam", _make_synthetic_jpeg(tmp_path / "syn_shifted.jpg", seam_range=(1200, 1311)), False),
        ("wide_band_200px", _make_synthetic_jpeg(tmp_path / "syn_wide.jpg", seam_range=(2050, 2249)), False),
        ("no_seam", _make_synthetic_jpeg(tmp_path / "syn_plain.jpg", seam_range=None), False),
        ("xpan_4288x1584", _make_synthetic_jpeg(tmp_path / "syn_xpan.jpg", 4288, 1584, seam_range=(2088, 2199)), False),
    ]


# --------------------------------------------------------------------------- 1 & 2. 检测（真实实拍 + 合成 + 手机同规则）


def test_synthetic_diptych_detection_desktop(tmp_path: Path) -> None:
    _clear_desktop_cache()
    for label, path, want in _synthetic_suite(tmp_path):
        assert halfframe.is_diptych(path) is want, label
    rot = _make_synthetic_jpeg(tmp_path / "syn_rot6.jpg", seam_range=(2088, 2199), exif_orientation=6)
    rot_plain = _make_synthetic_jpeg(tmp_path / "syn_rot6_plain.jpg", seam_range=None, exif_orientation=6)
    assert halfframe.is_diptych(rot) is True
    assert halfframe.is_diptych(rot_plain) is False


def test_diptych_cache_sha256_behavior(tmp_path: Path) -> None:
    _clear_desktop_cache()
    p = _make_synthetic_jpeg(tmp_path / "cached.jpg", seam_range=(2088, 2199))
    digest_true = hashlib.sha256(p.read_bytes()).hexdigest()
    assert halfframe.is_diptych(p, sha256=digest_true) is True
    # 不同 sha 不串联缓存，同一 missing 路径也不写缓存
    _make_synthetic_jpeg(p, seam_range=None)
    digest_false = hashlib.sha256(p.read_bytes()).hexdigest()
    assert halfframe.is_diptych(p, sha256=digest_false) is False
    missing_path = tmp_path / "missing.jpg"
    assert halfframe.is_diptych(missing_path) is False
    _make_synthetic_jpeg(missing_path, seam_range=(2088, 2199))
    assert halfframe.is_diptych(missing_path) is True


def test_real_field_diptych_detection_desktop() -> None:
    _clear_desktop_cache()
    for folder_key, (expected_count, diptych_indices) in FIELD_EXPECTATIONS.items():
        folder = _field_folder(folder_key)
        jpgs = _sorted_jpgs(folder)
        assert len(jpgs) == expected_count, f"{folder_key}: count={len(jpgs)}"
        for idx, jpg in enumerate(jpgs, start=1):
            want = idx in diptych_indices
            assert halfframe.is_diptych(jpg) is want, f"{folder_key} index={idx} want={want}"

    a12 = samples.a12_50_samples()
    if not a12.is_dir():
        samples.missing("captures/a12-50 样本不在本机")
    ordinary = _sorted_jpgs(a12)
    assert ordinary, "a12-50 sample empty"
    for idx, jpg in enumerate(ordinary, start=1):
        assert halfframe.is_diptych(jpg) is False, f"a12-50 index={idx} must be False"


def test_mobile_is_diptych_rgba_unit() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    result = subprocess.run(
        [node, "--experimental-strip-types", "--test", str(MOBILE_UNIT)],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.browser_serial
def test_mobile_browser_detection_synthetic_and_real_field(tmp_path: Path) -> None:
    _browser_prereq()
    _clear_desktop_cache()
    files: list[str] = []
    labels: list[str] = []
    expected: list[bool] = []

    for label, path, want in _synthetic_suite(tmp_path):
        files.append(str(path))
        labels.append(f"synthetic:{label}")
        expected.append(want)
    rot_true = _make_synthetic_jpeg(tmp_path / "mob_rot6.jpg", seam_range=(2088, 2199), exif_orientation=6)
    rot_false = _make_synthetic_jpeg(tmp_path / "mob_rot6_plain.jpg", seam_range=None, exif_orientation=6)
    files.extend((str(rot_true), str(rot_false)))
    labels.extend(("synthetic:rot6_true", "synthetic:rot6_false"))
    expected.extend((True, False))

    for folder_key, (expected_count, diptych_indices) in FIELD_EXPECTATIONS.items():
        folder = _field_folder(folder_key)
        jpgs = _sorted_jpgs(folder)
        assert len(jpgs) == expected_count
        for idx, jpg in enumerate(jpgs, start=1):
            files.append(str(jpg))
            labels.append(f"{folder_key}:{idx}")
            expected.append(idx in diptych_indices)

    a12 = samples.a12_50_samples()
    if a12.is_dir():
        for idx, jpg in enumerate(_sorted_jpgs(a12), start=1):
            files.append(str(jpg))
            labels.append(f"a12-50:{idx}")
            expected.append(False)

    out_path = tmp_path / "detect-out.json"
    manifest = tmp_path / "detect-manifest.json"
    manifest.write_text(json.dumps({"mode": "detect", "files": files, "out": str(out_path)}), encoding="utf-8")
    proc = _drive(BROWSER_DRIVER, manifest, tmp_path, "")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report.get("pageErrors") == [], report
    assert "error" not in report, report
    got = report["verdicts"]
    assert len(got) == len(expected)
    mismatches = [(lbl, w, g) for lbl, w, g in zip(labels, expected, got, strict=True) if w is not g]
    assert not mismatches, f"mobile detection mismatch: {mismatches}"


# --------------------------------------------------------------------------- 3. 桌面端到端（card250 第 2 张）


@pytest.mark.browser_serial
def test_desktop_e2e_diptych_treated_as_jpg_only_and_recipe_note(settings: Any, lib: Library, volumes: Path, tmp_path: Path) -> None:
    _browser_prereq()
    _clear_desktop_cache()
    card250 = _field_folder("card250")
    pairs = _sorted_pairs(card250)
    # P1a 起第 1+2 张一起导入会配成一对；单独导入第 2 张配第 7 张（非双联）保留未配对双联的 P0 路径校验
    _stage_volume_pairs(volumes, [pairs[1], pairs[6]])
    _setup_g250_calibrated_home(settings)

    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    assert len(lib.list_shots()) == 2
    lib.close()

    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        rows = client.get("/api/photos").json()
        ordered = sorted(rows, key=lambda r: r["name"])
        assert len(ordered) == 2
        shot2, shot1 = ordered[0], ordered[1]

        # 先验证关键用户行为（避免在旧基线上因缺少新字段先报 KeyError）：保留黑缝与拦截 RAW 动作
        render_final = client.get(f"/api/photos/{shot2['id']}/render?stage=final&size=preview")
        assert render_final.status_code == 200
        with Image.open(io.BytesIO(render_final.content)) as im:
            cmax, lmax, rmax = _seam_luma_stats(im)
        assert cmax <= 12 and lmax > 25 and rmax > 25, (cmax, lmax, rmax)

        # 默认 auto 导出保持黑缝；neutral / recipe 被拒（no_raw）
        exp_dir = tmp_path / "exp2"
        exp_dir.mkdir()
        exp_auto = client.post(
            f"/api/photos/{shot2['id']}/export",
            json={"size": "original", "quality_tier": "high", "dest_dir": str(exp_dir)},
        )
        assert exp_auto.status_code == 200, exp_auto.text
        with Image.open(exp_auto.json()["path"]) as im:
            assert im.size == (4288, 2848)
            cmax, lmax, rmax = _seam_luma_stats(im)
        assert cmax <= 12 and lmax > 25 and rmax > 25, (cmax, lmax, rmax)

        exp_neutral = client.post(
            f"/api/photos/{shot2['id']}/export",
            json={"source": "neutral", "dest_dir": str(exp_dir)},
        )
        assert exp_neutral.status_code == 409 and exp_neutral.json()["error"] == "no_raw"

        exp_recipe = client.post(
            f"/api/photos/{shot2['id']}/export",
            json={"source": "recipe", "dest_dir": str(exp_dir)},
        )
        assert exp_recipe.status_code == 409 and exp_recipe.json()["error"] == "no_raw"

        # LUT 路径：Log LUT 拒绝（lut_needs_raw），Display LUT 走相机 JPG 并保留黑缝
        up = client.post(
            "/api/lut/upload",
            content=cube_bytes(lambda x: x, size=2),
            headers={"X-GXR-Filename": "identity.cube"},
        )
        assert up.status_code == 200, up.text
        cube_id = up.json()["cube_id"]

        client.patch(f"/api/luts/{cube_id}", json={"input_space": "llog"})
        log_put = client.put(f"/api/photos/{shot2['id']}/edit", json={"lut": cube_id})
        assert log_put.status_code == 422 and log_put.json()["error"] == "lut_needs_raw"
        log_render = client.post(f"/api/photos/{shot2['id']}/render?stage=final", json={"lut": cube_id})
        assert log_render.status_code == 422 and log_render.json()["error"] == "lut_needs_raw"

        client.patch(f"/api/luts/{cube_id}", json={"input_space": "srgb"})
        disp_put = client.put(f"/api/photos/{shot2['id']}/edit", json={"lut": cube_id})
        assert disp_put.status_code == 200, disp_put.text
        disp_render = client.get(f"/api/photos/{shot2['id']}/render?stage=final&size=preview")
        assert disp_render.status_code == 200
        with Image.open(io.BytesIO(disp_render.content)) as im:
            cmax, lmax, rmax = _seam_luma_stats(im)
        assert cmax <= 12 and lmax > 25 and rmax > 25, (cmax, lmax, rmax)

        lut_prev = client.get(f"/api/lut/{cube_id}/preview", params={"which": "lut", "photo": shot2["id"]})
        assert lut_prev.status_code == 200 and lut_prev.headers.get("X-GXR-Preview-Mode") == "camera-jpg"

        # 改过编辑后的缩略图仍从 JPG 渲染并保留黑缝
        client.put(f"/api/photos/{shot2['id']}/edit", json={"lut": None, "adjust": {"exposure": 0.2}})
        thumb_res = client.get(f"/api/photos/{shot2['id']}/thumb")
        assert thumb_res.status_code == 200
        with Image.open(io.BytesIO(thumb_res.content)) as im:
            cmax, lmax, rmax = _seam_luma_stats(im)
        assert cmax <= 12 and lmax > 20 and rmax > 20, (cmax, lmax, rmax)

        edit2 = client.get(f"/api/photos/{shot2['id']}/edit").json()
        assert edit2["recipe_from_camera"] is False
        assert edit2["recipe"] is None
        assert edit2["render"]["recipe"] == "none"
        assert edit2["render"]["reason"] == "no_raw"

        assert shot1["has_raw"] is True
        assert shot2["diptych"] is True
        assert shot2["has_raw"] is False
        assert shot2["hdr"] == "none"

        p2 = client.get(f"/api/photos/{shot2['id']}").json()
        assert p2["diptych"] is True and p2["has_raw"] is False

        caps2 = client.get(f"/api/photos/{shot2['id']}/capabilities").json()
        assert caps2["raw"] is False and caps2["recipe"] == "none"

        gpu2 = client.get(f"/api/photos/{shot2['id']}/gpu").json()
        assert gpu2["raw"] is None and gpu2["raw_reason"] == "no_raw"

    # 桌面前端「配方」页文字（真实 web/assets/recipes.js + 真实 FastAPI）
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(settings.with_(port=port, web_dir=WEB)), log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        out_path = tmp_path / "desktop-note.json"
        manifest = tmp_path / "desktop-note-manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "mode": "desktop-note",
                    "baseUrl": f"http://127.0.0.1:{port}",
                    "token": settings.token,
                    "photo1Id": shot1["id"],
                    "photo2Id": shot2["id"],
                    "out": str(out_path),
                }
            ),
            encoding="utf-8",
        )
        proc = subprocess.run(
            ["node", str(BROWSER_DRIVER), str(RUNTIME), f"http://127.0.0.1:{port}/", str(manifest)],
            cwd=REPO,
            env={**os.environ, "HOME": str(REAL_HOME)},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        report = json.loads(out_path.read_text(encoding="utf-8"))
        assert "error" not in report, report
        assert report["pageErrors"] == []
        assert "这张双联没配上第 1 张，只能在相机 JPG 上调。" in report["desktopNote2"]
        assert report["desktopHasNote1"] is False
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        sock.close()


# --------------------------------------------------------------------------- 4. 非双联导出与 3a556bd 逐字节不变


def test_non_diptych_exports_match_baseline_hashes(settings: Any, lib: Library, volumes: Path, tmp_path: Path) -> None:
    _clear_desktop_cache()
    card250 = _field_folder("card250")
    card249 = _field_folder("card249")
    pairs250 = _sorted_pairs(card250)
    pairs249 = _sorted_pairs(card249)
    selected = [
        ("card250_1", pairs250[0]),
        ("card250_11_xpan", pairs250[10]),
        ("card249_5_ordinary", pairs249[4]),
    ]
    _stage_volume_pairs(volumes, [p for _, p in selected])
    _setup_g250_calibrated_home(settings)
    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    shots = lib.list_shots()
    assert len(shots) == 3

    by_sha = {shot["files"]["jpg"]["sha256"]: shot for shot in shots}
    lib.close()

    icc_path = tmp_path / "srgb.icc"
    icc_path.write_bytes(expmod.srgb_icc())
    head_exports: dict[str, bytes] = {}
    shot_ids: dict[str, str] = {}

    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        for idx, (label, (_, jpg_path)) in enumerate(selected, start=1):
            jpg_sha = hashlib.sha256(jpg_path.read_bytes()).hexdigest()
            shot = by_sha[jpg_sha]
            assert shot["diptych"] is False and shot["has_raw"] == 1, f"{label} idx={idx}"
            exp_dir = tmp_path / label
            exp_dir.mkdir()
            resp = client.post(
                f"/api/photos/{shot['shot_id']}/export",
                json={"size": "original", "quality_tier": "high", "dest_dir": str(exp_dir)},
            )
            assert resp.status_code == 200, f"{label}: {resp.text}"
            exported_bytes = Path(resp.json()["path"]).read_bytes()
            assert len(exported_bytes) == BASELINE_EXPORT_LENGTHS[label], f"{label} len mismatch"
            head_exports[label] = exported_bytes
            shot_ids[label] = shot["shot_id"]

    has_baseline = subprocess.run(["git", "cat-file", "-e", "3a556bd^{commit}"], cwd=REPO, check=False, capture_output=True)
    assert has_baseline.returncode == 0, "git commit 3a556bd must be reachable to verify byte identity"
    orig_dir = tmp_path / "orig_3a556bd"
    orig_dir.mkdir()
    tar = subprocess.run(["git", "archive", "3a556bd", "src"], cwd=REPO, capture_output=True, check=True)
    subprocess.run(["tar", "-xf", "-", "-C", str(orig_dir)], input=tar.stdout, check=True)

    worker = tmp_path / "export_3a556bd.py"
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

expmod._ICC = icc_bytes
cfg = Settings(
    home_dir=home_dir,
    library_dir=lib_dir,
    export_dir=export_dir,
    volumes_roots=(vol_dir,),
    port=port,
    token=token,
    min_free_bytes=0,
)
paths: dict[str, str] = {}
with TestClient(create_app(cfg), base_url=f"http://127.0.0.1:{port}", headers={"X-GXR-Token": token}) as client:
    for label, sid in shot_ids.items():
        d = out_dir / label
        d.mkdir(parents=True, exist_ok=True)
        resp = client.post(
            f"/api/photos/{sid}/export",
            json={"size": "original", "quality_tier": "high", "dest_dir": str(d)},
        )
        assert resp.status_code == 200, (label, resp.text)
        paths[label] = resp.json()["path"]
print(json.dumps(paths))
""".strip()
        + "\n",
        encoding="utf-8",
    )
    out_orig = tmp_path / "out_3a556bd"
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            str(worker),
            str(orig_dir / "src"),
            str(settings.home_dir),
            str(settings.library_dir),
            str(settings.export_dir),
            str(volumes),
            str(out_orig),
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
    orig_paths = json.loads(proc.stdout.splitlines()[-1])
    for label, _ in selected:
        orig_bytes = Path(orig_paths[label]).read_bytes()
        assert head_exports[label] == orig_bytes, f"{label} exported bytes diverged from 3a556bd"


# --------------------------------------------------------------------------- 5. 手机端浏览器端到端（card250 第 1、2 张）


@pytest.mark.browser_serial
def test_mobile_browser_card250_pair1_and_pair2_e2e(tmp_path: Path) -> None:
    _browser_prereq()
    card250 = _field_folder("card250")
    pairs = _sorted_pairs(card250)
    dat_path, fx_path = _bundle_paths(250)

    # 重命名为匿名序号文件，避免照片名进入浏览器输出与截图报告
    staged = tmp_path / "mobile-staged"
    staged.mkdir()
    p1_dng = staged / "R0000001.DNG"
    p1_jpg = staged / "R0000001.JPG"
    p2_dng = staged / "R0000002.DNG"
    p2_jpg = staged / "R0000002.JPG"
    shutil.copy2(pairs[0][0], p1_dng)
    shutil.copy2(pairs[0][1], p1_jpg)
    shutil.copy2(pairs[1][0], p2_dng)
    shutil.copy2(pairs[1][1], p2_jpg)
    dat_copy = staged / "GXRCE.DAT"
    fx_copy = staged / "GXRFX.BIN"
    shutil.copy2(dat_path, dat_copy)
    shutil.copy2(fx_path, fx_copy)

    out_dir = tmp_path / "exported"
    out_dir.mkdir()
    stage_png = tmp_path / "photo2-stage.png"
    out_path = tmp_path / "e2e-out.json"
    manifest = tmp_path / "e2e-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "mode": "e2e",
                "hasDat": True,
                "count": 2,
                "stems": ["R0000001", "R0000002"],
                "files": [str(p1_dng), str(p1_jpg), str(p2_dng), str(p2_jpg), str(dat_copy), str(fx_copy)],
                "stagePng": str(stage_png),
                "outDir": str(out_dir),
                "out": str(out_path),
            }
        ),
        encoding="utf-8",
    )

    proc = _drive(BROWSER_DRIVER, manifest, tmp_path, "")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert "error" not in report, report
    assert report["pageErrors"] == []

    # 网格徽标：第 1 张有 RAW（无 JPG 角标），第 2 张是半格双联，标「半格」，悬停和读屏说没有 HDR（Import P3-3）
    diptych_note = "半格：按相机拼好的双联图调整，没有 HDR"
    assert report["gridBadges"]["R0000001"] == ""
    assert report["gridBadges"]["R0000002"] == "半格"
    assert report["gridBadgeNote"] == {"title": diptych_note, "aria": diptych_note}

    # 第 1 张按 RAW 打开，无双联提示
    assert report["photo1"]["layer"] == {"w": 2144, "h": 1424}
    assert report["photo1"]["recipeHasDiptychNote"] is False
    assert report["photo1"]["head"]["text"] != "半格" and report["photo1"]["head"]["title"] is None

    # 第 2 张按相机 JPG 打开，舞台截图保留中央黑缝，配方页提示等待配对
    assert report["photo2"]["layer"] == {"w": 4288, "h": 2848}
    assert "这张是半格双联，配方效果要等配对功能。" in report["photo2"]["recipeText"]
    # 编辑页顶部和配方说明也不说「只有 JPG」
    assert report["photo2"]["head"] == {"text": "半格", "title": diptych_note, "aria": diptych_note}
    assert "这张按相机拼好的双联图调整" in report["photo2"]["recipeText"]
    assert "只有相机 JPG" not in report["photo2"]["recipeText"]
    sc_mean, sc_max, sl, sr = _stage_png_seam_stats(stage_png)
    assert sc_mean <= 10.0 and sc_max <= 15 and sl > 50 and sr > 50, f"mobile editor stage seam lost: {(sc_mean, sc_max, sl, sr)}"

    # 导出第 2 张成片保持 4288x2848 且保留中央黑缝；第 1 张也正常导出
    exported_files = sorted(out_dir.iterdir())
    exp1 = next((p for p in exported_files if "R0000001" in p.name), None)
    exp2 = next((p for p in exported_files if "R0000002" in p.name), None)
    assert exp1 is not None and exp1.is_file(), [p.name for p in exported_files]
    assert exp2 is not None and exp2.is_file(), [p.name for p in exported_files]
    with Image.open(exp2) as im:
        assert im.size == (4288, 2848)
        ec, el, er = _seam_luma_stats(im)
    assert ec <= 12 and el > 25 and er > 25, f"mobile export seam lost: {(ec, el, er)}"
