"""预览网格和导出用的同一条 Rust 路径，在同一张预览 RGB 上逐格相同。

Worker 侧是 fitPreviewGrid（hdrtrack_begin → hdrtrack_to_grid → hdr_luma → hdr_gain）。
对照侧是同样的 gxr-cli 命令。不解码增益 JPEG。输出里没有文件名、拍摄时间或 EXIF。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageOps

from support import samples
from support.core_cli import error_of, run_json
from support.hdr_parity import real_pairs
from support.wasm_check import CLI, PKG

REPO = Path(__file__).resolve().parents[1]
FIT = REPO / "tests" / "support" / "hdr_preview_fit.mjs"
PREVIEW = 1600
MIB = 1024 * 1024


def _need() -> None:
    if shutil.which("node") is None or not PKG.is_file() or not CLI.is_file():
        if os.environ.get("GXR_REQUIRE_CORE") == "1":
            pytest.fail("need node, gxr-cli and the wasm package")
        pytest.skip("need node, gxr-cli and the wasm package")


def _preview_rgb(jpg: Path, edit: str) -> np.ndarray:
    image = ImageOps.exif_transpose(Image.open(jpg)).convert("RGB")
    image.thumbnail((PREVIEW, PREVIEW))
    rgb = np.asarray(image, dtype=np.uint8)
    if edit == "flat":
        return np.full_like(rgb, 128)
    if edit == "edited":
        # 曝光大约 +1 再压一点绿：仍是这张照片，对齐还能过，网格会跟着变。
        lifted = np.clip(rgb.astype(np.float32) * 1.4, 0, 255)
        lifted[..., 1] *= 0.94
        return np.clip(lifted, 0, 255).astype(np.uint8)
    return rgb


def _worker(tmp: Path, rgb: np.ndarray, dng: Path, jpg: Path | None) -> dict:
    rgb_path = tmp / "rgb.u8"
    rgb_path.write_bytes(np.ascontiguousarray(rgb).tobytes())
    args = ["node", "--experimental-strip-types", str(FIT), str(rgb_path), str(rgb.shape[1]), str(rgb.shape[0]), str(dng)]
    if jpg is not None:
        args.append(str(jpg))
    proc = subprocess.run(args, cwd=REPO, check=False, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        pytest.fail(f"preview grid worker failed\n{proc.stderr[-2000:]}")
    return json.loads(proc.stdout)


def _cli_grid(tmp: Path, rgb: np.ndarray, dng: Path, jpg: Path | None) -> dict:
    from support.hdr_parity import save

    rgb_npy = save(tmp / "rgb.npy", rgb)
    begin = ["hdrtrack-begin", "--rgb", rgb_npy, "--dng", str(dng), "--dng-name", "preview.dng", "--out", str(tmp / "layer.npy")]
    if jpg is not None:
        begin.extend(["--jpeg", str(jpg)])
    code, payload = run_json(begin)
    err = error_of(code, payload)
    if err is not None:
        return {"ok": False, "reason": err.kind}
    if not payload.get("active"):
        return {"ok": False, "reason": payload.get("problem") or "hdr_unsupported"}
    h, w = rgb.shape[:2]
    code, _desc = run_json(
        [
            "hdrtrack-to-grid",
            "--layer",
            str(tmp / "layer.npy"),
            "--out-w",
            str(w),
            "--out-h",
            str(h),
            "--factor",
            "4",
            "--out",
            str(tmp / "raw.npy"),
            "--clip-out",
            str(tmp / "clip.npy"),
        ]
    )
    if error_of(code, _desc):
        return {"ok": False, "reason": error_of(code, _desc).kind}
    code, _luma = run_json(["hdr-luma", "--rgb", rgb_npy, "--factor", "4", "--out", str(tmp / "sdr.npy")])
    if error_of(code, _luma):
        return {"ok": False, "reason": error_of(code, _luma).kind}
    code, report = run_json(
        [
            "hdr-gain",
            "--sdr",
            str(tmp / "sdr.npy"),
            "--raw",
            str(tmp / "raw.npy"),
            "--clip",
            str(tmp / "clip.npy"),
            "--out",
            str(tmp / "codes.npy"),
        ]
    )
    err = error_of(code, report)
    if err is not None:
        return {"ok": False, "reason": err.kind}
    codes = np.load(tmp / "codes.npy")
    return {"ok": True, "codes": codes, "meta": report["meta"], "wasm_note": None}


def _same(index: int, worker: dict, native: dict) -> None:
    codes = np.frombuffer(base64.b64decode(worker["codes"]), np.uint8).reshape(worker["height"], worker["width"])
    assert codes.shape == native["codes"].shape, index
    assert np.array_equal(codes, native["codes"]), index
    for key, value in worker["meta"].items():
        assert value == pytest.approx(native["meta"][key]), index
    assert worker["gridBytes"] < MIB


def test_preview_grid_matches_the_export_path(tmp_path: Path) -> None:
    _need()
    pairs = real_pairs(12)
    if len(pairs) < 5:
        samples.missing("fewer than 5 RAW+JPG pairs")
    rows = []
    peak = 0
    matched = 0
    edited_done = False
    for index, (jpg, dng) in enumerate(pairs):
        if matched >= 5 and edited_done:
            break
        rgb = _preview_rgb(jpg, "plain")
        work = tmp_path / f"p{index}"
        work.mkdir()
        worker = _worker(work, rgb, dng, jpg)
        native = _cli_grid(work, rgb, dng, jpg)
        assert worker["ok"] is native["ok"], index
        if not worker["ok"]:
            assert worker["reason"] == native["reason"], index
            rows.append({"index": index, "kind": "plain", "ok": False, "reason": worker["reason"]})
        else:
            _same(index, worker, native)
            matched += 1
            rows.append({"index": index, "kind": "plain", "ok": True, "grid": int(worker["gridBytes"])})
            if not edited_done:
                erw = tmp_path / f"e{index}"
                erw.mkdir()
                ergb = _preview_rgb(jpg, "edited")
                ew = _worker(erw, ergb, dng, jpg)
                en = _cli_grid(erw, ergb, dng, jpg)
                assert ew["ok"] is en["ok"]
                if ew["ok"]:
                    _same(index, ew, en)
                    edited_done = True
                    rows.append({"index": index, "kind": "edited", "ok": True, "grid": int(ew["gridBytes"])})
                else:
                    assert ew["reason"] == en["reason"]
                    rows.append({"index": index, "kind": "edited", "ok": False, "reason": ew["reason"]})
        peak = max(peak, int(worker.get("rss") or 0))
    assert matched >= 5, rows
    assert edited_done, rows
    flat = tmp_path / "flat"
    flat.mkdir()
    _jpg, dng = pairs[0]
    other, _other_dng = pairs[1]
    rgb = _preview_rgb(other, "flat")
    worker = _worker(flat, rgb, dng, other)
    native = _cli_grid(flat, rgb, dng, other)
    assert worker["ok"] is False and native["ok"] is False
    assert worker["reason"] == native["reason"]
    rows.append({"index": "unsupported", "ok": False, "reason": worker["reason"]})
    assert peak <= 640 * MIB, peak
    print(json.dumps({"matched": matched, "rss_mib": round(peak / MIB, 1), "rows": rows}, ensure_ascii=False))
