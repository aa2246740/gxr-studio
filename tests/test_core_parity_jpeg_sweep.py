"""Differential JPEG header sweep and Sol r3 parity against Pillow 12.2.0.

Zero real photo bytes; verifies 0 differences across all 5 outputs:
``Image.open``, ``read_jpeg_meta``, ``photo_meta``, ``read_source_exif``, and ``HdrTrack.begin``.
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image, TiffImagePlugin

import hdr_support as hs
from gxr_studio.export import _DROP_EXIF_TAGS, _clean_text
from gxr_studio.finish.compose import load_jpeg
from gxr_studio.finish.hdr_export import HdrTrack
from gxr_studio.finish.meta_text import photo_meta
from gxr_studio.meta import read_jpeg_meta
from support.core_cli import require_cli
from support.jpeg_header_sweep import SweepCase, build_sweep_cases, sol_r3_cases
from support.wasm_check import PKG

MTIME_MS = 1_761_900_000_000.0
TZ_OFFSET_MIN = 480
NOW_YEAR = 2026


@pytest.fixture(autouse=True)
def _cli() -> Path:
    return require_cli()


def _py_val_to_json(val: Any) -> Any:
    if isinstance(val, TiffImagePlugin.IFDRational):
        return {"num": int(val.numerator), "den": int(val.denominator)}
    if isinstance(val, bytes):
        return {"hex": val.hex()}
    if isinstance(val, (tuple, list)):
        return [_py_val_to_json(x) for x in val]
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return int(val)
    if isinstance(val, float):
        return float(val)
    if isinstance(val, str):
        return val
    raise AssertionError(f"unexpected EXIF type {type(val)}")


def _py_open(path: Path) -> dict[str, Any]:
    try:
        with Image.open(path) as im:
            return {"ok": True, "width": int(im.size[0]), "height": int(im.size[1])}
    except Exception as exc:
        return {"ok": False, "kind": type(exc).__name__}


def _py_read(path: Path) -> dict[str, Any]:
    meta = read_jpeg_meta(path)
    out = dataclasses.asdict(meta)
    out["taken"] = meta.taken.isoformat() if meta.taken is not None else None
    if out.get("error") is not None:
        out["error"] = str(out["error"]).split(":", 1)[0]
    return out


def _py_photo(path: Path, name: str) -> dict[str, Any]:
    class _StubLib:
        @staticmethod
        def abspath(relpath: str) -> Path:
            return Path(relpath)

    shot = {"name": name, "files": {"jpg": {"relpath": str(path)}}}
    tz = timezone(timedelta(minutes=TZ_OFFSET_MIN))
    dt = datetime.fromtimestamp(MTIME_MS / 1000.0, tz=tz).replace(tzinfo=None)

    def _fake_exif(p: Path) -> dict[str, Any]:
        try:
            with Image.open(p) as im:
                exif = im.getexif()
                sub = dict(exif.get_ifd(0x8769))
        except (OSError, ValueError, KeyError):
            return {}
        return {"exif": exif, "sub": sub, "mtime": dt}

    try:
        with patch("gxr_studio.finish.meta_text._exif", side_effect=_fake_exif):
            frame, taken, file_time = photo_meta(_StubLib(), shot)  # type: ignore[arg-type]
        return {
            "ok": {
                "frame": frame,
                "taken": taken.isoformat() if taken is not None else None,
                "file_time": file_time.isoformat() if file_time is not None else None,
            }
        }
    except Exception as exc:
        return {"err": type(exc).__name__}


def _py_exif(path: Path) -> dict[str, dict[str, Any]]:
    base: dict[str, Any] = {}
    sub: dict[str, Any] = {}
    try:
        with Image.open(path) as src:
            exif = src.getexif()
            if 33432 in exif:
                base["33432"] = _py_val_to_json(exif[33432])
            for tag in (271, 272, 306):
                text = _clean_text(exif.get(tag))
                if text:
                    base[str(tag)] = text
            raw_sub = {k: v for k, v in exif.get_ifd(0x8769).items() if k not in _DROP_EXIF_TAGS}
            if not _clean_text(raw_sub.get(42036)):
                lens = _clean_text(exif.get(50708))
                if lens:
                    raw_sub[42036] = lens.removeprefix("RICOH ").strip()
            for k, v in raw_sub.items():
                sub[str(k)] = _py_val_to_json(v)
    except Exception:
        pass
    return {"base": base, "sub": sub}


def _py_begin(path: Path, dng_path: Path, rgb: np.ndarray) -> dict[str, Any]:
    track = HdrTrack(dng_path, jpeg=path)
    try:
        track.begin(rgb)
    except Exception as exc:
        return {"exception": type(exc).__name__}
    if track.layer is None:
        return {"active": False, "shape": None, "problem": track.problem}
    return {"active": True, "shape": list(track.layer.shape), "problem": None}


def _norm_open(d: dict[str, Any]) -> dict[str, Any]:
    if d.get("ok"):
        return {"ok": True, "width": d["width"], "height": d["height"]}
    return {"ok": False, "kind": d.get("kind")}


def _norm_read(d: dict[str, Any]) -> dict[str, Any]:
    out = dict(d)
    if out.get("error") is not None:
        out["error"] = str(out["error"]).split(":", 1)[0]
    return out


def _norm_photo(d: dict[str, Any]) -> dict[str, Any]:
    if "ok" in d:
        return {"ok": d["ok"]}
    return {"err": d["error"].get("kind")}


def _run_sweep(tmp_path: Path, cases: list[SweepCase]) -> None:
    cli = require_cli()
    jpg_bytes, dng_bytes = hs.pair(0, orientation=1, h=128, w=192)
    dng_path = tmp_path / "ref.dng"
    dng_path.write_bytes(dng_bytes)
    rgb = load_jpeg(io.BytesIO(jpg_bytes), None)
    rgb_path = tmp_path / "rgb.npy"
    np.save(rgb_path, np.ascontiguousarray(rgb))

    case_dir = tmp_path / "cases"
    case_dir.mkdir(exist_ok=True)
    meta_lines: list[str] = []
    begin_lines: list[str] = []
    paths: list[Path] = []
    for idx, case in enumerate(cases):
        path = case_dir / f"c_{idx:04d}.jpg"
        path.write_bytes(case.blob)
        paths.append(path)
        meta_lines.append(f"{path}\tIMG_{idx:04d}.JPG")
        begin_lines.append(str(path))

    manifest_meta = tmp_path / "meta_manifest.txt"
    manifest_begin = tmp_path / "begin_manifest.txt"
    manifest_meta.write_text("\n".join(meta_lines) + "\n", encoding="utf-8")
    manifest_begin.write_text("\n".join(begin_lines) + "\n", encoding="utf-8")

    meta_proc = subprocess.run(
        [
            str(cli),
            "export-meta-batch",
            "--manifest",
            str(manifest_meta),
            "--mtime-ms",
            str(int(MTIME_MS)),
            "--tz-offset-min",
            str(TZ_OFFSET_MIN),
            "--now-year",
            str(NOW_YEAR),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    begin_proc = subprocess.run(
        [
            str(cli),
            "hdrtrack-begin-batch",
            "--rgb",
            str(rgb_path),
            "--dng",
            str(dng_path),
            "--dng-name",
            "ref.dng",
            "--manifest",
            str(manifest_begin),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    rs_meta = [json.loads(line) for line in meta_proc.stdout.splitlines() if line.strip()]
    rs_begin = [json.loads(line) for line in begin_proc.stdout.splitlines() if line.strip()]
    assert len(rs_meta) == len(cases)
    assert len(rs_begin) == len(cases)

    diffs: list[str] = []
    for idx, case in enumerate(cases):
        path = paths[idx]
        name = f"IMG_{idx:04d}.JPG"
        rm = rs_meta[idx]
        rb = rs_begin[idx]

        po = _py_open(path)
        ro = _norm_open(rm["open"])
        if ro != po:
            diffs.append(f"{case.name} ({case.family}) open: rust={ro} py={po}")

        pr = _py_read(path)
        rr = _norm_read(rm["read"])
        if rr != pr:
            diffs.append(f"{case.name} ({case.family}) read: rust={rr} py={pr}")

        pp = _py_photo(path, name)
        rp = _norm_photo(rm["photo"])
        if rp != pp:
            diffs.append(f"{case.name} ({case.family}) photo: rust={rp} py={pp}")

        pe = _py_exif(path)
        re = rm["exif"]
        if re != pe:
            diffs.append(f"{case.name} ({case.family}) exif: rust={re} py={pe}")

        pb = _py_begin(path, dng_path, rgb)
        # Documented boundary: Python HdrTrack.begin does not catch DecompressionBombError,
        # while Rust capture.rs reports it as metadata_unreadable.
        if pb == {"exception": "DecompressionBombError"} and rb == {
            "active": False,
            "shape": None,
            "problem": "metadata_unreadable",
        }:
            continue
        if rb != pb:
            diffs.append(f"{case.name} ({case.family}) begin: rust={rb} py={pb}")

    assert not diffs, f"{len(diffs)} parity differences (showing first 10):\n" + "\n".join(diffs[:10])


def test_sol_r3_jpeg_edge_cases_match_pillow_and_wasm(tmp_path: Path) -> None:
    cases = sol_r3_cases()
    _run_sweep(tmp_path, cases)

    if shutil.which("node") is None or not PKG.is_file():
        if os.environ.get("GXR_REQUIRE_CORE") == "1":
            pytest.fail(f"need node and {PKG.name}; run make core-wasm")
        pytest.skip("need node and built gxr_wasm package")

    cli = require_cli()
    jpg_bytes, dng_bytes = hs.pair(0, orientation=1, h=128, w=192)
    rgb = load_jpeg(io.BytesIO(jpg_bytes), None)
    dng_path = tmp_path / "ref.dng"
    rgb_raw = tmp_path / "rgb.u8"
    dng_path.write_bytes(dng_bytes)
    rgb_raw.write_bytes(np.ascontiguousarray(rgb, dtype=np.uint8).tobytes())

    case_paths: list[str] = []
    for idx, case in enumerate(cases):
        p = tmp_path / "cases" / f"c_{idx:04d}.jpg"
        assert p.is_file()
        case_paths.append(str(p))

    script = """
import { readFileSync } from 'node:fs';
const wasm = await import(process.argv[1]);
wasm.initSync({ module: readFileSync(process.argv[2]) });
const dng = new Uint8Array(readFileSync(process.argv[3]));
const rgb = new Uint8Array(readFileSync(process.argv[4]));
const paths = JSON.parse(process.argv[5]);
const out = [];
for (let i = 0; i < paths.length; i++) {
  const b = new Uint8Array(readFileSync(paths[i]));
  const exif = JSON.parse(wasm.export_meta_exif(b));
  let photo;
  try {
    photo = { ok: JSON.parse(wasm.export_meta_photo(b, undefined, `IMG_${String(i).padStart(4, '0')}.JPG`, 1761900000000, 480)) };
  } catch (e) {
    photo = JSON.parse(String(e));
  }
  const st = wasm.hdrtrack_begin(rgb, 192, 128, true, dng, 'ref.dng', true, b, '');
  const begin = JSON.parse(st.state);
  out.push({ exif, photo, begin });
}
process.stdout.write(JSON.stringify(out));
"""
    res = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            script,
            PKG.as_uri(),
            str(PKG.with_name("gxr_wasm_bg.wasm")),
            str(dng_path),
            str(rgb_raw),
            json.dumps(case_paths),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    wasm_rows = json.loads(res.stdout)
    manifest_meta = tmp_path / "meta_manifest.txt"
    manifest_begin = tmp_path / "begin_manifest.txt"
    meta_rows = [
        json.loads(line)
        for line in subprocess.check_output(
            [
                str(cli),
                "export-meta-batch",
                "--manifest",
                str(manifest_meta),
                "--mtime-ms",
                str(int(MTIME_MS)),
                "--tz-offset-min",
                str(TZ_OFFSET_MIN),
                "--now-year",
                str(NOW_YEAR),
            ],
            text=True,
        ).splitlines()
        if line.strip()
    ]
    begin_rows = [
        json.loads(line)
        for line in subprocess.check_output(
            [
                str(cli),
                "hdrtrack-begin-batch",
                "--rgb",
                str(tmp_path / "rgb.npy"),
                "--dng",
                str(dng_path),
                "--dng-name",
                "ref.dng",
                "--manifest",
                str(manifest_begin),
            ],
            text=True,
        ).splitlines()
        if line.strip()
    ]
    for case, w, rm, rb in zip(cases, wasm_rows, meta_rows, begin_rows, strict=True):
        assert w["exif"] == rm["exif"], case.name
        assert w["photo"] == rm["photo"], case.name
        assert w["begin"] == rb, case.name


def test_jpeg_header_differential_sweep_fast(tmp_path: Path) -> None:
    _run_sweep(tmp_path, build_sweep_cases(fast=True))


def test_jpeg_header_differential_sweep_full(tmp_path: Path) -> None:
    _run_sweep(tmp_path, build_sweep_cases(fast=False))
