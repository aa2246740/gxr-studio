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
import struct
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image, TiffImagePlugin, TiffTags

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
# Documented mobile deviation: for JPEGs exceeding Pillow's pixel limit, desktop HdrTrack.begin
# leaves DecompressionBombError uncaught, whereas mobile capture.rs reports metadata_unreadable.
_ACCEPTED_BOMB_BEGIN_CASES = frozenset(
    {
        "ord_mp_big_bomb_sof",
        "ord_mp_no_b002_bomb_sof",
        "ord_bomb_solo",
        "ord_zero_then_bomb",
        "ord_ok_then_bomb",
    }
)


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
    bomb_cases_seen: set[str] = set()
    for idx, case in enumerate(cases):
        path = paths[idx]
        name = f"IMG_{idx:04d}.JPG"
        rm = rs_meta[idx]
        rb = rs_begin[idx]
        assert rm["exit"] == 0, f"{case.name}: unexpected meta exit {rm}"
        assert rb["exit"] == 0, f"{case.name}: unexpected begin exit {rb}"
        rb_state = rb["state"]

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
        # Accepted deviation: Python HdrTrack.begin raises DecompressionBombError uncaught,
        # while Rust capture.rs reports it as metadata_unreadable on these 5 named cases.
        if (
            case.name in _ACCEPTED_BOMB_BEGIN_CASES
            and pb == {"exception": "DecompressionBombError"}
            and rb_state == {"active": False, "shape": None, "problem": "metadata_unreadable"}
        ):
            bomb_cases_seen.add(case.name)
            continue
        if rb_state != pb:
            diffs.append(f"{case.name} ({case.family}) begin: rust={rb_state} py={pb}")

    assert not diffs, f"{len(diffs)} parity differences (showing first 10):\n" + "\n".join(diffs[:10])
    if any(c.name in _ACCEPTED_BOMB_BEGIN_CASES for c in cases):
        assert bomb_cases_seen == _ACCEPTED_BOMB_BEGIN_CASES


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
        assert rm["exit"] == 0, case.name
        assert rb["exit"] == 0, case.name
        assert w["exif"] == rm["exif"], case.name
        assert w["photo"] == rm["photo"], case.name
        assert w["begin"] == rb["state"], case.name


def _minimal_le_tiff(tag: int, typ: int, count: int, raw: bytes) -> bytes:
    hdr = b"II*\x00\x08\x00\x00\x00"
    n = struct.pack("<H", 1)
    if len(raw) <= 4:
        val_field = raw.ljust(4, b"\x00")
        extra = b""
    else:
        val_field = struct.pack("<I", 26)
        extra = raw
    entry = struct.pack("<HHI", tag, typ, count) + val_field
    return hdr + n + entry + b"\x00\x00\x00\x00" + extra


def _py_ifd(tiff_bytes: bytes, group: int | None) -> dict[str, Any]:
    bio = io.BytesIO(tiff_bytes)
    hdr = bio.read(8)
    ifd = TiffImagePlugin.ImageFileDirectory_v2(ifh=hdr, group=group)
    ifd.load(bio)
    return {str(k): _py_val_to_json(v) for k, v in ifd.items()}


def test_tiff_tag_length_matches_pillow_groups(tmp_path: Path) -> None:
    cli = require_cli()
    groups: list[int | None] = [None, *sorted(TiffTags.TAGS_V2_GROUPS.keys()), 60000]
    for group in groups:
        cmd = [str(cli), "export-meta-tiff-tags"]
        if group is not None:
            cmd.extend(["--group", str(group)])
        rust_data = json.loads(subprocess.check_output(cmd, text=True))
        assert rust_data["group"] == group
        py_lengths = {str(tag): length for tag in range(65536) if (length := TiffTags.lookup(tag, group).length) is not None}
        assert rust_data["lengths"] == py_lengths, f"group={group}"

    # Sol r4 four count=2 counterexamples + Exif/root/unknown-group controls + all 22 GPS non-1 tags
    gps_non_scalar = [0, 1, 2, 3, 4, 7, 8, 9, 10, 12, 14, 16, 18, 19, 20, 21, 22, 23, 25, 27, 28, 29]
    cases: list[tuple[str, int | None, bytes]] = [
        ("gps_30_cnt2", 34853, _minimal_le_tiff(30, 3, 2, struct.pack("<HH", 0, 1))),
        ("unknown_grp_30_cnt2", 60000, _minimal_le_tiff(30, 3, 2, struct.pack("<HH", 0, 1))),
        ("interop_1_cnt2", 40965, _minimal_le_tiff(1, 3, 2, struct.pack("<HH", 1, 2))),
        ("root_1_cnt2", None, _minimal_le_tiff(1, 3, 2, struct.pack("<HH", 1, 2))),
        ("root_326_cnt2", None, _minimal_le_tiff(326, 3, 2, struct.pack("<HH", 2, 3))),
        ("exif_326_cnt2", 34665, _minimal_le_tiff(326, 3, 2, struct.pack("<HH", 2, 3))),
        ("root_327_cnt2", None, _minimal_le_tiff(327, 3, 2, struct.pack("<HH", 2, 3))),
        ("exif_327_cnt2", 34665, _minimal_le_tiff(327, 3, 2, struct.pack("<HH", 2, 3))),
    ]
    for tag in gps_non_scalar:
        cases.append((f"gps_{tag}_cnt1", 34853, _minimal_le_tiff(tag, 3, 1, struct.pack("<H", 7))))

    for idx, (label, group, blob) in enumerate(cases):
        tif_path = tmp_path / f"ifd_{idx:02d}.tif"
        tif_path.write_bytes(blob)
        cmd = [str(cli), "export-meta-ifd", "--file", str(tif_path)]
        if group is not None:
            cmd.extend(["--group", str(group)])
        rust_tags = json.loads(subprocess.check_output(cmd, text=True))["tags"]
        py_tags = _py_ifd(blob, group)
        assert rust_tags == py_tags, f"{label}: rust={rust_tags} py={py_tags}"


def test_batch_row_isolation(tmp_path: Path) -> None:
    cli = require_cli()
    jpg_bytes, dng_bytes = hs.pair(0, orientation=1, h=128, w=192)
    dng_path = tmp_path / "ref.dng"
    dng_path.write_bytes(dng_bytes)
    rgb = load_jpeg(io.BytesIO(jpg_bytes), None)
    rgb_path = tmp_path / "rgb.npy"
    np.save(rgb_path, np.ascontiguousarray(rgb))

    valid_jpg = tmp_path / "valid.jpg"
    after_jpg = tmp_path / "after.jpg"
    malformed_jpg = tmp_path / "malformed.jpg"
    missing_jpg = tmp_path / "missing.jpg"
    valid_jpg.write_bytes(jpg_bytes)
    after_jpg.write_bytes(jpg_bytes)
    malformed_jpg.write_bytes(b"\xff\xd8\xff\xc0\x00\x03\x08\xff\xd9")

    for seq_name, items in [
        ("missing", [("valid.jpg", valid_jpg), ("missing.jpg", missing_jpg), ("after.jpg", after_jpg)]),
        ("malformed", [("valid.jpg", valid_jpg), ("malformed.jpg", malformed_jpg), ("after.jpg", after_jpg)]),
    ]:
        meta_manifest = tmp_path / f"meta_{seq_name}.tsv"
        begin_manifest = tmp_path / f"begin_{seq_name}.txt"
        meta_manifest.write_text("\n".join(f"{p}\t{name}" for name, p in items) + "\n", encoding="utf-8")
        begin_manifest.write_text("\n".join(str(p) for _, p in items) + "\n", encoding="utf-8")

        meta_batch = subprocess.run(
            [
                str(cli),
                "export-meta-batch",
                "--manifest",
                str(meta_manifest),
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
        begin_batch = subprocess.run(
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
                str(begin_manifest),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        meta_rows = [json.loads(line) for line in meta_batch.stdout.splitlines() if line.strip()]
        begin_rows = [json.loads(line) for line in begin_batch.stdout.splitlines() if line.strip()]
        assert len(meta_rows) == 3, seq_name
        assert len(begin_rows) == 3, seq_name

        for row_idx, ((name, path), rm, rb) in enumerate(zip(items, meta_rows, begin_rows, strict=True)):
            read_p = subprocess.run(
                [str(cli), "export-meta-read", "--file", str(path), "--now-year", str(NOW_YEAR)],
                capture_output=True,
                text=True,
            )
            photo_p = subprocess.run(
                [
                    str(cli),
                    "export-meta-photo",
                    "--jpg",
                    str(path),
                    "--name",
                    name,
                    "--mtime-ms",
                    str(int(MTIME_MS)),
                    "--tz-offset-min",
                    str(TZ_OFFSET_MIN),
                    "--now-year",
                    str(NOW_YEAR),
                ],
                capture_output=True,
                text=True,
            )
            exif_p = subprocess.run(
                [str(cli), "export-meta-exif", "--src", str(path)],
                capture_output=True,
                text=True,
            )
            if rm["exit"] == 0:
                assert read_p.returncode == 0 and photo_p.returncode == 0 and exif_p.returncode == 0
                assert rm["read"] == json.loads(read_p.stdout)
                assert rm["photo"] == {"ok": json.loads(photo_p.stdout)}
                assert rm["exif"] == json.loads(exif_p.stdout)
                assert _norm_open(rm["open"]) == _py_open(path)
            else:
                assert rm["exit"] == read_p.returncode == photo_p.returncode == exif_p.returncode == 2
                assert rm == {"exit": 2, "error": json.loads(read_p.stdout)["error"]}
                assert rm["error"] == json.loads(photo_p.stdout)["error"] == json.loads(exif_p.stdout)["error"]

            begin_p = subprocess.run(
                [
                    str(cli),
                    "hdrtrack-begin",
                    "--rgb",
                    str(rgb_path),
                    "--dng",
                    str(dng_path),
                    "--dng-name",
                    "ref.dng",
                    "--jpeg",
                    str(path),
                    "--out",
                    str(tmp_path / f"single_{seq_name}_{row_idx}.npy"),
                ],
                capture_output=True,
                text=True,
            )
            if rb["exit"] == 0:
                assert begin_p.returncode == 0
                assert rb["state"] == json.loads(begin_p.stdout)
            else:
                assert rb["exit"] == begin_p.returncode == 1
                assert rb == {"exit": 1, "stderr": begin_p.stderr}

        assert meta_rows[0]["exit"] == 0 and meta_rows[2]["exit"] == 0
        assert begin_rows[0]["exit"] == 0 and begin_rows[2]["exit"] == 0

    # Global manifest/option/shared-input failure still aborts the whole batch
    bad_meta = subprocess.run(
        [str(cli), "export-meta-batch", "--manifest", str(tmp_path / "no_such_manifest.txt")],
        capture_output=True,
        text=True,
    )
    bad_begin = subprocess.run(
        [
            str(cli),
            "hdrtrack-begin-batch",
            "--rgb",
            str(rgb_path),
            "--dng",
            str(tmp_path / "no_such.dng"),
            "--manifest",
            str(tmp_path / "begin_missing.txt"),
        ],
        capture_output=True,
        text=True,
    )
    assert bad_meta.returncode != 0
    assert bad_begin.returncode != 0


def test_jpeg_header_differential_sweep_fast(tmp_path: Path) -> None:
    _run_sweep(tmp_path, build_sweep_cases(fast=True))


def test_jpeg_header_differential_sweep_full(tmp_path: Path) -> None:
    _run_sweep(tmp_path, build_sweep_cases(fast=False))
