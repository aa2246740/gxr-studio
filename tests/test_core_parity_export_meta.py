"""Parity for gxr-export::meta: source EXIF, photo display text, JPEG/DNG PhotoMeta, and preview offset.

Python + Pillow 12.2.0 is the oracle. Never log or print EXIF values from real photos.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import math
import os
import shutil
import struct
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, TiffImagePlugin

from gxr_studio.export import _DROP_EXIF_TAGS, _clean_text
from gxr_studio.finish.meta_text import photo_meta
from gxr_studio.meta import _load_subifd, dng_preview_image, read_dng_meta, read_jpeg_meta
from support import samples as sample_files
from support.core_cli import CoreCliError, error_of, require_cli, run_json
from support.export_meta_cases import (
    make_jpeg_bytes,
    make_tiff_doc,
    nonfinite_edge_cases,
    sol_mtime_tz_cases,
    synthetic_edge_cases,
    wasm_number_boundary_specs,
)
from support.wasm_check import PKG, run_area

CAPTURES = sample_files.captures()
_CAP = 60


def _real_files(exts: tuple[str, ...], cap: int = _CAP) -> list[Path]:
    root = sample_files.captures()
    if not root.is_dir():
        return []
    found = (p for p in root.rglob("*") if p.is_file() and p.suffix.upper() in exts and "backup" not in p.as_posix().lower())
    return sample_files.stable_sample(found, root, cap)


REAL_DNGS = _real_files((".DNG",))
REAL_JPGS = _real_files((".JPG", ".JPEG"))


def _missing(msg: str) -> None:
    if os.environ.get("GXR_REQUIRE_CORE") == "1":
        pytest.fail(msg)
    sample_files.missing(msg)


# --------------------------------------------------------------------------- Python oracles


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
    raise AssertionError(f"unexpected EXIF value type: {type(val)}")


def _py_source_exif(path: Path) -> dict[str, dict[str, Any]]:
    """Exact source-reading half of `src/gxr_studio/export.py:write_with_exif`."""
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


def _py_photo_meta_json(
    jpg: Path | None,
    dng: Path | None,
    name: str,
    mtime_ms: float | None,
    tz_offset_min: int,
) -> dict[str, Any]:
    """Oracle mirroring `finish/meta_text.py:photo_meta` using explicit `mtime_ms` & `tz_offset_min`."""

    class _StubLib:
        @staticmethod
        def abspath(relpath: str) -> Path:
            return Path(relpath)

    files: dict[str, dict[str, str]] = {}
    if jpg is not None:
        files["jpg"] = {"relpath": str(jpg)}
    if dng is not None:
        files["dng"] = {"relpath": str(dng)}
    shot = {"name": name, "files": files}

    from unittest.mock import patch

    if mtime_ms is not None:
        tz = timezone(timedelta(minutes=tz_offset_min))
        dt = datetime.fromtimestamp(mtime_ms / 1000.0, tz=tz).replace(tzinfo=None)
    else:
        dt = None

    def _fake_exif(path: Path) -> dict[str, Any]:
        try:
            with Image.open(path) as im:
                exif = im.getexif()
                sub = dict(exif.get_ifd(0x8769))
        except (OSError, ValueError, KeyError):
            return {}
        return {"exif": exif, "sub": sub, "mtime": dt}

    with patch("gxr_studio.finish.meta_text._exif", side_effect=_fake_exif):
        frame, taken, file_time = photo_meta(_StubLib(), shot)  # type: ignore[arg-type]

    return {
        "frame": frame,
        "taken": taken.isoformat() if taken is not None else None,
        "file_time": file_time.isoformat() if file_time is not None else None,
    }


def _py_read_meta_json(path: Path, is_dng: bool) -> dict[str, Any]:
    meta = read_dng_meta(path) if is_dng else read_jpeg_meta(path)
    d = dataclasses.asdict(meta)
    d["taken"] = meta.taken.isoformat() if meta.taken is not None else None
    return d


def _digest_value(val: Any) -> str:
    """Summarize any metadata value without exposing its contents."""
    raw = json.dumps(val, sort_keys=True, ensure_ascii=False, allow_nan=True).encode("utf-8")
    return f"{type(val).__name__}:{hashlib.sha256(raw).hexdigest()[:16]}"


def _values_equal(got: Any, want: Any) -> bool:
    if isinstance(got, float) and isinstance(want, float) and math.isnan(got) and math.isnan(want):
        return True
    if isinstance(got, dict) and isinstance(want, dict):
        return got.keys() == want.keys() and all(_values_equal(got[k], want[k]) for k in got)
    if isinstance(got, (list, tuple)) and isinstance(want, (list, tuple)):
        return len(got) == len(want) and all(_values_equal(a, b) for a, b in zip(got, want))
    return type(got) is type(want) and got == want


def _assert_exif_equal(got: dict[str, Any], want: dict[str, Any], label: str) -> None:
    if not isinstance(got, dict) or not isinstance(want, dict) or set(got.keys()) != {"base", "sub"} or set(want.keys()) != {"base", "sub"}:
        got_keys = sorted(got.keys()) if isinstance(got, dict) else [type(got).__name__]
        want_keys = sorted(want.keys()) if isinstance(want, dict) else [type(want).__name__]
        pytest.fail(f"{label}: EXIF structure mismatch: got={got_keys} want={want_keys}", pytrace=False)
    diffs: list[str] = []
    for section in ("base", "sub"):
        g_map = got[section]
        w_map = want[section]
        if not isinstance(g_map, dict) or not isinstance(w_map, dict):
            diffs.append(f"{section}: type got={type(g_map).__name__} want={type(w_map).__name__}")
            continue
        g_keys = set(g_map.keys())
        w_keys = set(w_map.keys())
        if g_keys != w_keys:
            diffs.append(f"{section} keys missing={sorted(w_keys - g_keys)} extra={sorted(g_keys - w_keys)}")
        for k in sorted(g_keys & w_keys):
            gv, wv = g_map[k], w_map[k]
            if not _values_equal(gv, wv):
                diffs.append(f"{section}[{k}]: got={_digest_value(gv)} want={_digest_value(wv)}")
    if diffs:
        pytest.fail(f"{label}: EXIF mismatch: " + "; ".join(diffs), pytrace=False)


def _assert_read_meta_equal(got: dict[str, Any], want: dict[str, Any], label: str) -> None:
    diffs: list[str] = []
    g_err, w_err = got.get("error"), want.get("error")
    if w_err is not None:
        w_kind = str(w_err).split(":", 1)[0]
        if g_err is None:
            diffs.append(f"error: got=None want={w_kind} ({_digest_value(w_err)})")
        else:
            g_kind = str(g_err).split(":", 1)[0]
            if g_kind != w_kind:
                diffs.append(f"error: got={g_kind} ({_digest_value(g_err)}) want={w_kind} ({_digest_value(w_err)})")
    elif g_err is not None:
        g_kind = str(g_err).split(":", 1)[0]
        diffs.append(f"error: got={g_kind} ({_digest_value(g_err)}) want=None")
    for key in ("width", "height", "orientation", "taken", "iso", "lens", "model", "copyright", "recipe_tag"):
        gv, wv = got.get(key), want.get(key)
        if not _values_equal(gv, wv):
            diffs.append(f"field {key}: got={_digest_value(gv)} want={_digest_value(wv)}")
    if diffs:
        pytest.fail(f"{label}: PhotoMeta mismatch: " + "; ".join(diffs), pytrace=False)


def _assert_photo_equal(got: dict[str, Any], want: dict[str, Any], label: str) -> None:
    diffs: list[str] = []
    if set(got.keys()) != set(want.keys()):
        diffs.append(f"keys got={sorted(got.keys())} want={sorted(want.keys())}")
    for key in ("taken", "file_time"):
        if key in got and key in want and not _values_equal(got[key], want[key]):
            diffs.append(f"{key}: got={_digest_value(got[key])} want={_digest_value(want[key])}")
    g_frame, w_frame = got.get("frame"), want.get("frame")
    if not isinstance(g_frame, dict) or not isinstance(w_frame, dict):
        if not _values_equal(g_frame, w_frame):
            diffs.append(f"frame: got={_digest_value(g_frame)} want={_digest_value(w_frame)}")
    else:
        if set(g_frame.keys()) != set(w_frame.keys()):
            diffs.append(f"frame keys got={sorted(g_frame.keys())} want={sorted(w_frame.keys())}")
        for fk in sorted(set(g_frame.keys()) & set(w_frame.keys())):
            gv, wv = g_frame[fk], w_frame[fk]
            if not _values_equal(gv, wv):
                diffs.append(f"frame[{fk}]: got={_digest_value(gv)} want={_digest_value(wv)}")
    if diffs:
        pytest.fail(f"{label}: photo_meta mismatch: " + "; ".join(diffs), pytrace=False)


_make_jpeg_bytes = make_jpeg_bytes
_synthetic_edge_cases = synthetic_edge_cases


# --------------------------------------------------------------------------- Tests


def _assert_no_core_error(code: int, payload: Any, label: str) -> None:
    err = error_of(code, payload)
    if err is not None:
        pytest.fail(f"{label}: unexpected core error kind={err.kind} digest={_digest_value(err.message)}", pytrace=False)


def _py_select_preview_range(path: Path) -> dict[str, int] | None:
    """Exact selection loop of `gxr_studio/meta.py:181 dng_preview_image` without decoding JPEG."""
    try:
        with Image.open(path) as im:
            best: tuple[int, int] | None = None
            best_area = 0
            for off in im.tag_v2.get(330, ()) or ():  # type: ignore[attr-defined]
                try:
                    ifd = _load_subifd(im, int(off))
                except Exception:
                    continue
                if ifd.get(259) != 7 or not ifd.get(273) or not ifd.get(279):
                    continue
                start, count = int(ifd[273][0]), int(ifd[279][0])
                area = int(ifd.get(256, 0)) * int(ifd.get(257, 0))
                if count <= 0 or count > 32 * 1024 * 1024 or area <= best_area:
                    continue
                im.fp.seek(start)  # type: ignore[union-attr]
                data = im.fp.read(count)  # type: ignore[union-attr]
                if len(data) == count:
                    best, best_area = (start, count), area
            return {"offset": best[0], "length": best[1]} if best is not None else None
    except Exception:
        return None


def _assert_preview_equal(
    got_pv: Any,
    py_range: dict[str, int] | None,
    py_pv: Image.Image | None,
    raw_bytes: bytes,
    label: str,
) -> bool:
    if not _values_equal(got_pv, py_range):
        pytest.fail(
            f"{label}: preview selection mismatch got={_digest_value(got_pv)} want={_digest_value(py_range)}",
            pytrace=False,
        )
    if got_pv is None:
        assert py_pv is None
        return False
    off, length = got_pv["offset"], got_pv["length"]
    payload = raw_bytes[off : off + length]
    if py_pv is None:
        # Broken-JPEG candidate selected structurally: caller Pillow decode fails -> end result None.
        with pytest.raises(Exception):
            with Image.open(io.BytesIO(payload)) as rs_pv:
                rs_pv.load()
        return False
    with Image.open(io.BytesIO(payload)) as rs_pv:
        rs_pv.load()
        rs_rgb = rs_pv.convert("RGB")
        if rs_rgb.size != py_pv.size:
            pytest.fail(f"{label}: preview size mismatch got={rs_rgb.size} want={py_pv.size}", pytrace=False)
        rs_hash = hashlib.sha256(rs_rgb.tobytes()).hexdigest()[:16]
        py_hash = hashlib.sha256(py_pv.tobytes()).hexdigest()[:16]
        if rs_hash != py_hash or rs_rgb.tobytes() != py_pv.tobytes():
            pytest.fail(f"{label}: preview pixels mismatch got={rs_hash} want={py_hash}", pytrace=False)
    return True


def test_privacy_assertions_redact_sentinels(tmp_path: Path) -> None:
    sentinel_a = "SYNTHETIC_OWNER_SENTINEL_A"
    sentinel_b = "SYNTHETIC_OWNER_SENTINEL_B"
    with pytest.raises(BaseException) as exc1:
        _assert_exif_equal(
            {"base": {"272": sentinel_a}, "sub": {"42033": sentinel_a}},
            {"base": {"272": sentinel_b}, "sub": {"42033": sentinel_b}},
            "synthetic-exif",
        )
    assert sentinel_a not in str(exc1.value) and sentinel_b not in str(exc1.value)
    assert "42033" in str(exc1.value)

    def read_payload(val: str) -> dict[str, Any]:
        return {
            "width": 8,
            "height": 6,
            "orientation": 1,
            "taken": None,
            "iso": None,
            "lens": val,
            "model": val,
            "error": None,
            "copyright": val,
            "recipe_tag": None,
        }

    with pytest.raises(BaseException) as exc2:
        _assert_read_meta_equal(read_payload(sentinel_a), read_payload(sentinel_b), "synthetic-read")
    assert sentinel_a not in str(exc2.value) and sentinel_b not in str(exc2.value)

    def photo_payload(val: str) -> dict[str, Any]:
        return {
            "frame": {
                "lens": val,
                "lens_short": val,
                "aperture": "",
                "shutter": "",
                "iso": "",
                "date": "",
                "frame_no": "",
            },
            "taken": None,
            "file_time": None,
        }

    with pytest.raises(BaseException) as exc3:
        _assert_photo_equal(photo_payload(sentinel_a), photo_payload(sentinel_b), "synthetic-photo")
    assert sentinel_a not in str(exc3.value) and sentinel_b not in str(exc3.value)

    probe = tmp_path / "test_sentinel_probe.py"
    probe.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
        "from test_core_parity_export_meta import _assert_exif_equal\n"
        "def test_sentinel():\n"
        f"    left = {{'base': {{}}, 'sub': {{'42033': {sentinel_a!r}}}}}\n"
        f"    right = {{'base': {{}}, 'sub': {{'42033': {sentinel_b!r}}}}}\n"
        "    _assert_exif_equal(left, right, 'sentinel')\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider", str(probe)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    combined = proc.stdout + proc.stderr
    assert sentinel_a not in combined and sentinel_b not in combined
    assert "42033" in combined


def test_real_jpgs_and_dngs_exact_parity() -> None:
    if not REAL_JPGS or not REAL_DNGS:
        _missing("real JPG/DNG captures not present under GXR_STUDIO_TEST_PARENT")
    require_cli()
    assert len(REAL_JPGS) >= 50 and len(REAL_DNGS) >= 50

    now_year = datetime.now().year
    preview_found = 0
    preview_none = 0

    for jpg in REAL_JPGS:
        code, got_exif = run_json(["export-meta-exif", "--src", str(jpg)])
        _assert_no_core_error(code, got_exif, f"{jpg.name}::exif")
        _assert_exif_equal(got_exif, _py_source_exif(jpg), jpg.name)

        code, got_read = run_json(["export-meta-read", "--file", str(jpg), "--now-year", str(now_year)])
        _assert_no_core_error(code, got_read, f"{jpg.name}::read")
        _assert_read_meta_equal(got_read, _py_read_meta_json(jpg, is_dng=False), jpg.name)

        code, got_photo = run_json(
            [
                "export-meta-photo",
                "--jpg",
                str(jpg),
                "--name",
                jpg.stem,
                "--mtime-ms",
                "1700000000123",
                "--tz-offset-min",
                "480",
                "--now-year",
                str(now_year),
            ]
        )
        _assert_no_core_error(code, got_photo, f"{jpg.name}::photo")
        _assert_photo_equal(got_photo, _py_photo_meta_json(jpg, None, jpg.stem, 1700000000123.0, 480), jpg.name)

    for dng in REAL_DNGS:
        code, got_exif = run_json(["export-meta-exif", "--src", str(dng)])
        _assert_no_core_error(code, got_exif, f"{dng.name}::exif")
        _assert_exif_equal(got_exif, _py_source_exif(dng), dng.name)

        code, got_read = run_json(["export-meta-read", "--file", str(dng), "--now-year", str(now_year)])
        _assert_no_core_error(code, got_read, f"{dng.name}::read")
        _assert_read_meta_equal(got_read, _py_read_meta_json(dng, is_dng=True), dng.name)

        code, got_photo = run_json(
            [
                "export-meta-photo",
                "--dng",
                str(dng),
                "--name",
                dng.stem,
                "--mtime-ms",
                "1700000000000",
                "--tz-offset-min",
                "-300",
                "--now-year",
                str(now_year),
            ]
        )
        _assert_no_core_error(code, got_photo, f"{dng.name}::photo")
        _assert_photo_equal(got_photo, _py_photo_meta_json(None, dng, dng.stem, 1700000000000.0, -300), dng.name)

        code, got_pv = run_json(["export-meta-preview", "--dng", str(dng)])
        _assert_no_core_error(code, got_pv, f"{dng.name}::preview")
        py_range = _py_select_preview_range(dng)
        py_pv = dng_preview_image(dng, embedded_only=True)
        if _assert_preview_equal(got_pv, py_range, py_pv, dng.read_bytes(), dng.name):
            preview_found += 1
        else:
            preview_none += 1

    print(f"Real parity: {len(REAL_JPGS)} JPGs, {len(REAL_DNGS)} DNGs (preview found={preview_found}, none={preview_none})")


def test_synthetic_pillow_edges_and_damaged_files(tmp_path: Path) -> None:
    require_cli()
    now_year = datetime.now().year
    cases = {**_synthetic_edge_cases(), **nonfinite_edge_cases()}

    for filename, (blob, kind) in cases.items():
        path = tmp_path / filename
        path.write_bytes(blob)
        is_dng = kind == "dng"

        code, got_exif = run_json(["export-meta-exif", "--src", str(path)])
        _assert_no_core_error(code, got_exif, f"{filename}::exif")
        _assert_exif_equal(got_exif, _py_source_exif(path), filename)

        code, got_read = run_json(["export-meta-read", "--file", str(path), "--now-year", str(now_year)])
        _assert_no_core_error(code, got_read, f"{filename}::read")
        _assert_read_meta_equal(got_read, _py_read_meta_json(path, is_dng=is_dng), filename)

        jpg_arg = path if not is_dng else None
        dng_arg = path if is_dng else None
        args = [
            "export-meta-photo",
            "--name",
            f"R001_{path.stem}",
            "--mtime-ms",
            "1700000000123",
            "--tz-offset-min",
            "480",
            "--now-year",
            str(now_year),
        ]
        args += ["--jpg", str(path)] if not is_dng else ["--dng", str(path)]
        code, got_photo = run_json(args)
        if filename.startswith("huge-") and filename.endswith(("-dimensions.jpg", "-dimensions.dng")):
            err = error_of(code, got_photo)
            assert isinstance(err, CoreCliError) and err.kind == "DecompressionBombError", (filename, err)
            with pytest.raises(Image.DecompressionBombError):
                _py_photo_meta_json(jpg_arg, dng_arg, f"R001_{path.stem}", 1700000000123.0, 480)
        else:
            _assert_no_core_error(code, got_photo, f"{filename}::photo")
            _assert_photo_equal(got_photo, _py_photo_meta_json(jpg_arg, dng_arg, f"R001_{path.stem}", 1700000000123.0, 480), filename)
            if filename == "iso-ascii-digits-4300.jpg":
                assert len(str(got_read["iso"])) == 4300 and got_photo["frame"]["iso"] == f"ISO {'7' * 4300}"
            elif filename in {"iso-ascii-digits-4301.jpg", "iso-ascii-digits-5000.jpg"}:
                assert got_read["iso"] is None and got_read["error"] is None and got_photo["frame"]["iso"] == ""

        if is_dng:
            code, got_pv = run_json(["export-meta-preview", "--dng", str(path)])
            _assert_no_core_error(code, got_pv, f"{filename}::preview")
            py_range = _py_select_preview_range(path)
            py_pv = dng_preview_image(path, embedded_only=True)
            picked = _assert_preview_equal(got_pv, py_range, py_pv, blob, filename)
            if filename.startswith("preview-area-") and filename != "preview-area-neg-pos.dng":
                assert picked, filename


def test_photo_prefers_jpg_when_both_present(tmp_path: Path) -> None:
    require_cli()
    jpg = tmp_path / "pair.jpg"
    dng = tmp_path / "pair.dng"
    cases = _synthetic_edge_cases()
    jpg.write_bytes(cases["xmp_orient.jpg"][0])
    dng.write_bytes(cases["le_edges.dng"][0])
    now_year = datetime.now().year
    code, got = run_json(
        [
            "export-meta-photo",
            "--jpg",
            str(jpg),
            "--dng",
            str(dng),
            "--name",
            "IMGP0456",
            "--mtime-ms",
            "1700000000500",
            "--tz-offset-min",
            "0",
            "--now-year",
            str(now_year),
        ]
    )
    assert error_of(code, got) is None
    _assert_photo_equal(got, _py_photo_meta_json(jpg, dng, "IMGP0456", 1700000000500.0, 0), "pair")
    assert got["frame"]["shutter"] == "2.5s"


def test_errors_match_core_error_kinds(tmp_path: Path) -> None:
    require_cli()
    missing = tmp_path / "does_not_exist.dng"
    code, payload = run_json(["export-meta-exif", "--src", str(missing)])
    err = error_of(code, payload)
    assert isinstance(err, CoreCliError) and err.kind == "raw_decode"

    jpg = tmp_path / "ok.jpg"
    jpg.write_bytes(_make_jpeg_bytes())
    for bad_args, expected_kind in [
        (["export-meta-photo", "--name", "a"], "bad_input"),
        (["export-meta-photo", "--jpg", str(jpg), "--mtime-ms", "NaN"], "bad_meta"),
        (["export-meta-photo", "--jpg", str(jpg), "--mtime-ms", "Infinity"], "bad_meta"),
        (["export-meta-photo", "--jpg", str(jpg), "--mtime-ms", "-Infinity"], "bad_meta"),
        (["export-meta-photo", "--jpg", str(jpg), "--mtime-ms", "1000", "--tz-offset-min", "1440"], "bad_meta"),
        (["export-meta-photo", "--jpg", str(jpg), "--mtime-ms", "1000", "--tz-offset-min", "-1440"], "bad_meta"),
        (["export-meta-photo", "--jpg", str(jpg), "--mtime-ms", "1000", "--tz-offset-min", "NaN"], "bad_meta"),
        (["export-meta-photo", "--jpg", str(jpg), "--mtime-ms", "1000", "--tz-offset-min", "Infinity"], "bad_meta"),
        (["export-meta-photo", "--jpg", str(jpg), "--mtime-ms", "1000", "--tz-offset-min", "-Infinity"], "bad_meta"),
        (["export-meta-photo", "--jpg", str(jpg), "--mtime-ms", "1000", "--tz-offset-min", "1.5"], "bad_meta"),
        (["export-meta-photo", "--jpg", str(jpg), "--mtime-ms", "1000", "--tz-offset-min", "4294967297"], "bad_meta"),
        (["export-meta-photo", "--jpg", str(jpg), "--mtime-ms", "1000", "--tz-offset-min", "9007199254740992"], "bad_meta"),
    ]:
        code, payload = run_json(bad_args)
        err = error_of(code, payload)
        assert isinstance(err, CoreCliError) and err.kind == expected_kind, (bad_args, err)

    subnormal_shutter = tmp_path / "shutter_inf.jpg"
    subnormal_shutter.write_bytes(_make_jpeg_bytes(exif_payload=make_tiff_doc([], [(33434, 12, 1, struct.pack("<d", 1e-320))])))
    code, payload = run_json(["export-meta-photo", "--jpg", str(subnormal_shutter), "--name", "ovf"])
    err = error_of(code, payload)
    assert isinstance(err, CoreCliError) and err.kind == "OverflowError", err


def test_mtime_rounding_sol_cases(tmp_path: Path) -> None:
    require_cli()
    jpg = tmp_path / "time.jpg"
    jpg.write_bytes(_make_jpeg_bytes())
    now_year = datetime.now().year
    manifest_entries: list[dict[str, Any]] = []
    for idx, (mtime_ms, tz_min) in enumerate(sol_mtime_tz_cases()):
        code, got_photo = run_json(
            [
                "export-meta-photo",
                "--jpg",
                str(jpg),
                "--name",
                "TEST1234",
                "--mtime-ms",
                repr(mtime_ms),
                "--tz-offset-min",
                str(tz_min),
                "--now-year",
                str(now_year),
            ]
        )
        _assert_no_core_error(code, got_photo, f"mtime[{idx}]")
        _assert_photo_equal(got_photo, _py_photo_meta_json(jpg, None, "TEST1234", mtime_ms, tz_min), f"mtime[{idx}]")
        manifest_entries.append(
            {
                "label": f"mtime_{idx}::photo",
                "kind": "photo",
                "jpg": str(jpg),
                "dng": None,
                "name": "TEST1234",
                "mtime_ms": mtime_ms,
                "tz_offset_min": tz_min,
            }
        )
    manifest = tmp_path / "mtime-wasm.json"
    manifest.write_text(json.dumps(manifest_entries), encoding="utf-8")
    proc = run_area("export", [manifest], tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_wasm_matches_native_export_meta(tmp_path: Path) -> None:
    require_cli()
    cases = _synthetic_edge_cases()
    manifest_entries: list[dict[str, Any]] = []
    null_preview_dngs: list[Path] = []

    for name, (blob, kind) in cases.items():
        p = tmp_path / name
        p.write_bytes(blob)
        manifest_entries.append({"label": f"{name}::exif", "kind": "exif", "src": str(p)})
        manifest_entries.append(
            {
                "label": f"{name}::photo",
                "kind": "photo",
                "jpg": str(p) if kind == "jpg" else None,
                "dng": str(p) if kind == "dng" else None,
                "name": p.stem,
                "mtime_ms": 1700000000123,
                "tz_offset_min": 480,
            }
        )
        if kind == "dng":
            if _py_select_preview_range(p) is not None:
                manifest_entries.append({"label": f"{name}::preview", "kind": "preview", "dng": str(p)})
            else:
                null_preview_dngs.append(p)

    # Include real samples (selected spread) in WASM vs native check
    for jpg in REAL_JPGS[:10]:
        manifest_entries.append({"label": f"{jpg.name}::exif", "kind": "exif", "src": str(jpg)})
        manifest_entries.append(
            {
                "label": f"{jpg.name}::photo",
                "kind": "photo",
                "jpg": str(jpg),
                "dng": None,
                "name": jpg.stem,
                "mtime_ms": 1700000000000,
                "tz_offset_min": 0,
            }
        )
    for dng in REAL_DNGS[:10]:
        manifest_entries.append({"label": f"{dng.name}::exif", "kind": "exif", "src": str(dng)})
        if _py_select_preview_range(dng) is not None:
            manifest_entries.append({"label": f"{dng.name}::preview", "kind": "preview", "dng": str(dng)})
        else:
            null_preview_dngs.append(dng)

    # Error & number-boundary parity cases in WASM vs native (mtime_ms & tz_offset_min as f64)
    ok_jpg = tmp_path / "xmp_orient.jpg"
    manifest_entries.extend(
        [
            {"label": "err::missing_both", "kind": "photo", "jpg": None, "dng": None, "name": "a", "mtime_ms": 1000, "tz_offset_min": 0},
            {"label": "err::bad_tz", "kind": "photo", "jpg": str(ok_jpg), "dng": None, "name": "a", "mtime_ms": 1000, "tz_offset_min": 1440},
            *[
                {"label": lbl, "kind": "photo", "jpg": str(ok_jpg), "dng": None, "name": "a", "mtime_ms": ms, "tz_offset_min": tz}
                for lbl, ms, tz in wasm_number_boundary_specs()
            ],
        ]
    )

    manifest = tmp_path / "export-wasm.json"
    manifest.write_text(json.dumps(manifest_entries), encoding="utf-8")
    proc = run_area("export", [manifest], tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    # Verify null-returning preview DNGs are also byte-identical between native CLI and WASM
    if shutil.which("node") and PKG.is_file() and null_preview_dngs:
        node_script = """
import { readFileSync } from 'node:fs';
const wasm = await import(process.argv[1]);
wasm.initSync({ module: readFileSync(process.argv[2]) });
const results = [];
for (const p of JSON.parse(process.argv[3])) {
  results.push(wasm.export_meta_preview(new Uint8Array(readFileSync(p))));
}
process.stdout.write(JSON.stringify(results));
"""
        res = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                node_script,
                PKG.as_uri(),
                str(PKG.with_name("gxr_wasm_bg.wasm")),
                json.dumps([str(p) for p in null_preview_dngs]),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, res.stderr
        wasm_outputs = json.loads(res.stdout)
        assert len(wasm_outputs) == len(null_preview_dngs)
        for p, got in zip(null_preview_dngs, wasm_outputs, strict=True):
            assert got == "null\n", (p.name, got)

    nonfinite_files: list[tuple[str, str, Path]] = []
    for name, (blob, kind) in nonfinite_edge_cases().items():
        p = tmp_path / name
        p.write_bytes(blob)
        nonfinite_files.append((name, kind, p))
    if shutil.which("node") and PKG.is_file() and nonfinite_files:
        nf_script = """
import { readFileSync } from 'node:fs';
const wasm = await import(process.argv[1]);
wasm.initSync({ module: readFileSync(process.argv[2]) });
const out = [];
for (const item of JSON.parse(process.argv[3])) {
  const b = new Uint8Array(readFileSync(item.path));
  out.push({
    exif: wasm.export_meta_exif(b),
    photo: wasm.export_meta_photo(item.kind === 'jpg' ? b : undefined, item.kind === 'dng' ? b : undefined, item.stem, 1700000000123, 480),
  });
}
process.stdout.write(JSON.stringify(out));
"""
        res = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                nf_script,
                PKG.as_uri(),
                str(PKG.with_name("gxr_wasm_bg.wasm")),
                json.dumps([{"name": n, "kind": k, "path": str(p), "stem": p.stem} for n, k, p in nonfinite_files]),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, res.stderr
        wasm_nf = json.loads(res.stdout)
        cli_bin = require_cli()
        for (name, kind, p), got_w in zip(nonfinite_files, wasm_nf, strict=True):
            c_exif = subprocess.run([str(cli_bin), "export-meta-exif", "--src", str(p)], capture_output=True, text=True, check=True)
            assert got_w["exif"] == c_exif.stdout, name
            flag = "--dng" if kind == "dng" else "--jpg"
            c_photo = subprocess.run(
                [
                    str(cli_bin),
                    "export-meta-photo",
                    flag,
                    str(p),
                    "--name",
                    p.stem,
                    "--mtime-ms",
                    "1700000000123",
                    "--tz-offset-min",
                    "480",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            assert got_w["photo"] == c_photo.stdout, name
