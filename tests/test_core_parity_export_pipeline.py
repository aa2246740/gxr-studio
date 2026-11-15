"""Parity: the phone export pipeline (``gxr-export`` ``pipeline/``) against desktop ``export_finish``.

Gates (UNIT-export-pipeline.md):
- ``export-run-huffopt``: re-coding Pillow's ``optimize=False`` file with optimal tables gives
  Pillow's ``optimize=True`` file byte for byte (what ``export.py:_finish`` writes).
- ``export-run-complete`` (no ink): ``_fit`` + one encode equals ``write_rgb_with_exif`` byte for
  byte, qualities 95/85/70, long edge on and off; the result JSON equals ``export_finish``'s fields.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from gxr_studio.export import srgb_icc, write_rgb_with_exif
from gxr_studio.finish.compose import load_jpeg
from gxr_studio.finish.serve import _fit
from support.core_cli import error_of, require_cli, run_json
from support.export_pipeline_cases import first_difference, real_pairs, source_exif_json, synthetic_images, synthetic_source

QUALITIES = (95, 85, 70)


@pytest.fixture(autouse=True)
def _cli() -> None:
    require_cli()


@pytest.fixture
def pinned_icc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> bytes:
    """Python's ``srgb_icc()`` is made by LittleCMS at run time, with the creation time in header
    bytes 24..36, so no two desktop runs write the same file. gxr-jpeg fixes those bytes. Assert
    that this is the only difference, then give Python the fixed profile."""
    from gxr_studio import export as export_mod

    rgb = np.zeros((8, 8, 3), dtype=np.uint8)
    np.save(tmp_path / "icc.npy", rgb)
    code, payload = run_json(["jpeg-encode", "--in", str(tmp_path / "icc.npy"), "--quality", "95", "--out", str(tmp_path / "icc.jpg")])
    assert code == 0, payload
    with Image.open(tmp_path / "icc.jpg") as im:
        rs = bytes(im.info["icc_profile"])
    py = srgb_icc()
    assert len(rs) == len(py) == 588
    assert rs[:24] == py[:24] and rs[36:] == py[36:]
    monkeypatch.setattr(export_mod, "_ICC", rs)
    return rs


def _pillow(rgb: np.ndarray, quality: int, optimize: bool) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(rgb), "RGB").save(
        buf, "JPEG", quality=quality, subsampling=0 if quality >= 90 else 2, optimize=optimize, icc_profile=srgb_icc()
    )
    return buf.getvalue()


def _huffopt(data: bytes, tmp: Path, tag: str) -> bytes:
    src, out = tmp / f"{tag}.jpg", tmp / f"{tag}.opt.jpg"
    src.write_bytes(data)
    code, payload = run_json(["export-run-huffopt", "--in", str(src), "--out", str(out)])
    err = error_of(code, payload)
    if err is not None:
        raise err
    return out.read_bytes()


@pytest.mark.parametrize("quality", QUALITIES + (100, 90, 89, 1))
def test_huffopt_equals_pillow_optimize(quality: int, tmp_path: Path) -> None:
    for name, rgb in synthetic_images():
        plain = _pillow(rgb, quality, False)
        want = _pillow(rgb, quality, True)
        got = _huffopt(plain, tmp_path, f"{name}-{quality}")
        assert got == want, f"{name} q{quality}: {first_difference(got, want)}"


def test_huffopt_real_arrays(tmp_path: Path) -> None:
    pairs = real_pairs(6)
    if not pairs:
        pytest.skip("no real captures under GXR_STUDIO_TEST_PARENT")
    for i, (jpg, _dng) in enumerate(pairs):
        rgb = load_jpeg(jpg, None)
        for quality in QUALITIES:
            got = _huffopt(_pillow(rgb, quality, False), tmp_path, f"real{i}-{quality}")
            want = _pillow(rgb, quality, True)
            assert got == want, f"real {i} q{quality}: {first_difference(got, want)}"


def test_huffopt_rejects_damaged_input(tmp_path: Path) -> None:
    good = _pillow(np.zeros((16, 16, 3), dtype=np.uint8), 95, False)
    for tag, data in [("empty", b""), ("text", b"not a jpeg"), ("truncated", good[: len(good) - 40]), ("no-eoi", good[:-2])]:
        src, out = tmp_path / f"{tag}.jpg", tmp_path / f"{tag}.out.jpg"
        src.write_bytes(data)
        code, payload = run_json(["export-run-huffopt", "--in", str(src), "--out", str(out)])
        err = error_of(code, payload)
        assert err is not None and err.kind == "encode_failed", tag


def _state(name: str, quality: Any, long_edge: Any, labels: dict[str, Any], hdr: Any = None) -> dict[str, Any]:
    return {"name": name, "quality": quality, "long_edge": long_edge, "labels": labels, "hdr": hdr}


def _complete(tmp: Path, tag: str, canvas: np.ndarray, state: dict[str, Any], exif_json: str) -> tuple[bytes | None, Any]:
    c, s, e, o = (tmp / f"{tag}.{x}" for x in ("npy", "state.json", "exif.json", "jpg"))
    np.save(c, np.ascontiguousarray(canvas, dtype=np.uint8))
    s.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    e.write_text(exif_json, encoding="utf-8")
    code, payload = run_json(["export-run-complete", "--canvas", str(c), "--state", str(s), "--exif", str(e), "--out", str(o)])
    if code != 0:
        return None, payload
    return o.read_bytes(), payload


LABEL_SETS: list[dict[str, str]] = [
    {
        "kind": "original",
        "label": "original",
        "text": "原图",
        "id": "camera-jpg",
        "tag": "",
        "description": "原图",
        "detail": "",
        "calibrated": "0",
    },
    {
        "kind": "prediction",
        "label": "prediction",
        "text": "电脑预测 · 未校准",
        "id": "recipe-prediction-uncalibrated",
        "tag": "",
        "description": "GXR Studio computer prediction (not camera-measured): jlaunch recipe 03, ISO model 200, NOT calibrated.",
        "detail": "",
        "calibrated": "0",
    },
    {
        "kind": "original",
        "label": "original",
        "text": "中性转换",
        "id": "neutral-preview",
        "tag": "_neutral",
        "description": "",
        "detail": "说明",
        "calibrated": "",
    },
    {},
]


def _expected_result(name: str, rgb: np.ndarray, data: bytes, labels: dict[str, str], hdr: Any = None) -> dict[str, Any]:
    """``export_finish``'s field rules (serve.py:507-512 and the ExportResult it builds)."""
    kind = labels.get("label") or labels.get("kind") or "original"
    ident = labels.get("id") or ("camera-jpg" if kind == "original" else "recipe-prediction")
    cal = labels.get("calibrated")
    tag = labels.get("tag") or ""
    if hdr is not None and hdr.get("hdr") is True:
        tag += "_hdr"
    return {
        "width": rgb.shape[1],
        "height": rgb.shape[0],
        "bytes": len(data),
        "render": ident,
        "render_label": labels.get("text") or "原图",
        "format": "jpeg",
        "render_kind": kind,
        "calibrated": True if cal == "1" else False if cal == "0" else None,
        "detail": labels.get("detail") or "",
        "hdr": None if hdr is None else hdr.get("hdr"),
        "hdr_reason": None if hdr is None else hdr.get("reason"),
        "recipe_binding": None,
        "file_name": f"{name}_gxr{tag}.jpg",
        "tag": tag,
    }


@pytest.mark.parametrize("quality", QUALITIES)
@pytest.mark.parametrize("long_edge", [None, 64])
def test_complete_synthetic_equals_write_rgb_with_exif(quality: int, long_edge: int | None, tmp_path: Path, pinned_icc: bytes) -> None:
    src = synthetic_source(tmp_path / "src.jpg")
    exif_json = source_exif_json(src)
    for i, (name, rgb) in enumerate(synthetic_images()):
        labels = LABEL_SETS[i % len(LABEL_SETS)]
        note = labels.get("description") or labels.get("text") or "原图"
        fitted = _fit(rgb, long_edge)
        want = write_rgb_with_exif(fitted, src, note, quality=quality, fmt="jpeg")
        got, payload = _complete(tmp_path, f"{name}", rgb, _state("R0001", quality, long_edge, labels), exif_json)
        assert got is not None, payload
        assert got == want, f"{name}: {first_difference(got, want)}"
        assert payload["result"] == _expected_result("R0001", fitted, want, labels)


def test_complete_real_arrays(tmp_path: Path, pinned_icc: bytes) -> None:
    pairs = real_pairs(8)
    if not pairs:
        pytest.skip("no real captures under GXR_STUDIO_TEST_PARENT")
    labels = LABEL_SETS[0]
    for i, (jpg, dng) in enumerate(pairs):
        rgb = load_jpeg(jpg, None)
        source = jpg if i % 2 == 0 else dng
        exif_json = source_exif_json(source)
        quality = QUALITIES[i % 3]
        long_edge = None if i % 2 else 1600
        fitted = _fit(rgb, long_edge)
        want = write_rgb_with_exif(fitted, source, "原图", quality=quality, fmt="jpeg")
        got, payload = _complete(tmp_path, f"real{i}", rgb, _state(f"R{i:04d}", quality, long_edge, labels), exif_json)
        assert got is not None, payload.get("error", {}).get("kind")
        assert got == want, f"real {i}: {first_difference(got, want)}"
        assert payload["result"]["width"] == fitted.shape[1] and payload["result"]["height"] == fitted.shape[0]


@pytest.mark.parametrize(
    ("state", "kind"),
    [
        (_state("R", 0, None, {}), "bad_quality"),
        (_state("R", 101, None, {}), "bad_quality"),
        (_state("R", 95.0, None, {}), "bad_quality"),
        (_state("R", 95, 63, {}), "bad_size"),
        (_state("R", 95, 30001, {}), "bad_size"),
        (_state("R", 95, 1600.0, {}), "bad_size"),
        (_state("R", True, None, {}), "bad_state"),
        ({"quality": 95, "labels": {}}, "bad_state"),
        (_state("R", 95, None, {"text": 1}), "bad_request"),
    ],
)
def test_complete_option_errors(state: dict[str, Any], kind: str, tmp_path: Path) -> None:
    got, payload = _complete(tmp_path, "err", np.zeros((4, 4, 3), dtype=np.uint8), state, "")
    assert got is None and payload["error"]["kind"] == kind
