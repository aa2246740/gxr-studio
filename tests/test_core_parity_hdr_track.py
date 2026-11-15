"""Parity: ``gxr-cli hdrtrack-*`` and WASM (``gxr-export::hdr_track``, ``gxr-finish::geometry::float_layer``)
against Python ``gxr_studio.finish.hdr_export``, ``geometry.warp_float``, ``geometry.crop_valid``,
and end-to-end ``serve.export_finish(hdr=True)``.
"""

from __future__ import annotations

import io
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

import hdr_support as hs
from gxr_studio.finish.compose import load_jpeg
from gxr_studio.finish.geometry import crop_valid, warp_float
from gxr_studio.finish.hdr_export import HdrTrack
from gxr_studio.finish.spec import FRAME_STYLES, FrameSpec, Geometry, StampSpec
from support import hdr_parity as hp
from support.core_cli import require_cli
from support.frame_cases import FIXTURE_CJK, LOGO_JSON, SAMPLE_LOGOS, cjk_fonts, desktop_mark, font_args

_STUDIO = Path(__file__).resolve().parents[1]
_PARENT = Path(os.environ.get("GXR_STUDIO_TEST_PARENT", str(_STUDIO.parent)))
CAPTURES = _PARENT / "captures"
RNG_SEED = 20261115


@pytest.fixture(autouse=True)
def _cli() -> None:
    require_cli()


def _save(path: Path, arr: np.ndarray) -> str:
    np.save(path, np.ascontiguousarray(arr))
    return str(path)


def _write_json(path: Path, obj: Any) -> str:
    path.write_text(json.dumps(obj), encoding="utf-8")
    return str(path)


def _real_pairs_all() -> list[tuple[Path, Path]]:
    if not CAPTURES.is_dir():
        return []
    dngs = sorted(
        (p for p in CAPTURES.rglob("*") if p.is_file() and p.suffix.upper() == ".DNG" and "backup" not in p.as_posix().lower()),
        key=lambda p: p.as_posix(),
    )
    pairs: list[tuple[Path, Path]] = []
    for d in dngs:
        for ext in (".JPG", ".jpg"):
            j = d.with_suffix(ext)
            if j.is_file():
                pairs.append((j, d))
                break
    return pairs


def _spread_pairs(limit: int) -> list[tuple[Path, Path]]:
    pairs = _real_pairs_all()
    if len(pairs) <= limit:
        return pairs
    return [pairs[(i * len(pairs)) // limit] for i in range(limit)]


def _state_of(track: HdrTrack) -> dict[str, Any]:
    if track.layer is None:
        return {"active": False, "shape": None, "problem": track.problem}
    return {"active": True, "shape": list(track.layer.shape), "problem": None}


def _save_state(tmp: Path, tag: str, track: HdrTrack) -> tuple[str, str]:
    layer = track.layer if track.layer is not None else np.zeros((0, 0, 2), dtype=np.float32)
    l_path = _save(tmp / f"{tag}-layer.npy", layer)
    s_path = _write_json(tmp / f"{tag}-state.json", _state_of(track))
    return l_path, s_path


def _load_track_out(out_npy: Path, meta: dict[str, Any]) -> np.ndarray | None:
    if not meta["active"]:
        arr = np.load(out_npy)
        assert arr.shape == (0, 0, 2)
        return None
    arr = np.load(out_npy)
    assert list(arr.shape) == meta["shape"]
    return np.asarray(arr, dtype=np.float32)


def _same_f32_nan(py: np.ndarray, rs: np.ndarray, what: str) -> None:
    """Exact float32 bits and exact NaN positions."""
    assert py.dtype == np.float32 and rs.dtype == np.float32, f"{what}: dtypes {py.dtype} vs {rs.dtype}"
    assert py.shape == rs.shape, f"{what}: shapes {py.shape} vs {rs.shape}"
    assert np.array_equal(np.isnan(py), np.isnan(rs)), f"{what}: NaN positions differ"
    hp.same_f32(py, rs, what)


def _stamp_changed_mask(shape: tuple[int, ...], spec: StampSpec, taken: datetime | None, file_time: datetime | None) -> np.ndarray:
    from gxr_studio.finish.stamp import apply_stamp

    mask = np.zeros(shape[:2], dtype=bool)
    if not spec.enabled:
        return mask
    for val in (0, 255):
        solid = np.full((shape[0], shape[1], 3), val, dtype=np.uint8)
        mask |= (apply_stamp(solid, spec, taken, file_time) != solid).any(axis=2)
    return mask


def _custom_logo_image(logo_id: str) -> Image.Image:
    return Image.new("RGBA", (300, 120), (20, 40, 80, 200))


# ---------------------------------------------------------------- 1. warp_float & 2-ch float32 crop_valid


GEOMETRIES = [
    Geometry(),
    Geometry(rotate90=1),
    Geometry(rotate90=2, flip=True),
    Geometry(rotate90=3, flip=True),
    Geometry(straighten=4.2),
    Geometry(straighten=-11.5, rotate90=1, flip=True),
    Geometry(vertical=15, horizontal=-10, distortion=8),
    Geometry(straighten=-6.0, vertical=-18, horizontal=12, distortion=-14, rotate90=2, flip=False),
]


@pytest.mark.parametrize("shape", [(29, 37, 2), (24, 31, 1), (19, 23, 3)])
def test_warp_float_parity(tmp_path: Path, shape: tuple[int, int, int]) -> None:
    rng = np.random.default_rng(RNG_SEED)
    layer = (rng.random(shape) * 3.0 - 0.5).astype(np.float32)
    layer[3, 5] = np.nan
    layer[10, 12, 0] = np.nan
    in_path = _save(tmp_path / "in.npy", layer)
    for i, geom in enumerate(GEOMETRIES):
        g_path = _write_json(tmp_path / f"g{i}.json", geom.to_json())
        out_path = tmp_path / f"out{i}.npy"
        report = hp.ok(["hdrtrack-warp-float", "--in", in_path, "--geometry", g_path, "--out", str(out_path)])
        py = warp_float(layer, geom)
        rs = np.load(out_path)
        assert report["dtype"] == "<f4"
        assert report["shape"] == list(py.shape)
        _same_f32_nan(py, rs, f"warp_float {shape} #{i}")


def test_warp_float_2d_and_crop_valid_f32_parity(tmp_path: Path) -> None:
    rng = np.random.default_rng(RNG_SEED + 1)
    layer2d = (rng.random((25, 33)) * 2.0).astype(np.float32)
    layer2d[4, 7] = np.nan
    in2 = _save(tmp_path / "in2.npy", layer2d)
    g_path = _write_json(tmp_path / "g.json", Geometry(straighten=7.5, vertical=10).to_json())
    out2 = tmp_path / "out2.npy"
    hp.ok(["hdrtrack-warp-float", "--in", in2, "--geometry", g_path, "--out", str(out2)])
    py2 = warp_float(layer2d, Geometry(straighten=7.5, vertical=10))
    rs2 = np.load(out2)[:, :, 0]
    _same_f32_nan(py2, rs2, "warp_float 2D")

    layer_2ch = (rng.random((31, 43, 2)) * 4.0).astype(np.float32)
    layer_2ch[5:8, 10:15] = np.nan
    in_c = _save(tmp_path / "inc.npy", layer_2ch)
    for idx, rect in enumerate(((0.1, 0.2, 0.6, 0.5), (0.0, 0.0, 1.0, 1.0), (0.123, 0.234, 0.456, 0.567))):
        r_path = _write_json(tmp_path / f"r{idx}.json", list(rect))
        out_c = tmp_path / f"outc{idx}.npy"
        hp.ok(["hdrtrack-crop-valid", "--in", in_c, "--rect", r_path, "--out", str(out_c)])
        py_c = crop_valid(layer_2ch, rect)
        rs_c = np.load(out_c)
        _same_f32_nan(py_c, rs_c, f"crop_valid 2ch #{idx}")


# ---------------------------------------------------------------- 2. begin: >= 20 real pairs & failure reasons


def test_begin_on_real_pairs_matches_python_bit_for_bit(tmp_path: Path) -> None:
    pairs = _spread_pairs(20)
    if CAPTURES.is_dir():
        assert len(pairs) >= 20, f"expected >= 20 real pairs in {CAPTURES}, got {len(pairs)}"
    else:
        if os.environ.get("GXR_REQUIRE_CORE") == "1":
            pytest.skip(f"no {CAPTURES} directory on this host")
        pytest.skip("no real pairs")

    for i, (jpg, dng) in enumerate(pairs):
        rgb = load_jpeg(jpg, None)
        rgb_path = _save(tmp_path / f"rgb-{i}.npy", rgb)
        out_path = tmp_path / f"out-{i}.npy"
        meta = hp.ok(
            [
                "hdrtrack-begin",
                "--rgb",
                rgb_path,
                "--dng",
                str(dng),
                "--dng-name",
                dng.name,
                "--jpeg",
                str(jpg),
                "--out",
                str(out_path),
            ]
        )
        py_track = HdrTrack(dng, jpeg=jpg)
        py_track.begin(rgb)
        assert meta == _state_of(py_track), f"pair {i} ({dng.name}): python={_state_of(py_track)} rust={meta}"
        rs_layer = _load_track_out(out_path, meta)
        if py_track.layer is not None:
            assert rs_layer is not None
            _same_f32_nan(py_track.layer, rs_layer, f"begin real pair {i} ({dng.name})")


def test_begin_failure_reasons_match_python(tmp_path: Path) -> None:
    jpg_bytes, dng_bytes = hs.pair(0, orientation=1)
    exif_mismatch = Image.Exif()
    exif_mismatch[272] = "OTHER_CAM"
    buf_mismatch = io.BytesIO()
    Image.fromarray(hs.srgb_bytes(hs.scene(seed=7) * hs.EXPOSURE), "RGB").save(buf_mismatch, "JPEG", quality=95, exif=exif_mismatch)
    jpg_b = buf_mismatch.getvalue()
    rgb = load_jpeg(io.BytesIO(jpg_bytes), None)
    rgb_path = _save(tmp_path / "rgb.npy", rgb)
    dng_path = tmp_path / "a.dng"
    dng_path.write_bytes(dng_bytes)
    jpg_path = tmp_path / "a.jpg"
    jpg_path.write_bytes(jpg_bytes)
    jpg_mismatch_path = tmp_path / "b.jpg"
    jpg_mismatch_path.write_bytes(jpg_b)
    bad_dng_path = tmp_path / "bad.dng"
    bad_dng_path.write_bytes(b"NOT_A_DNG_FILE")
    bad_jpg_path = tmp_path / "bad.jpg"
    bad_jpg_path.write_bytes(b"NOT_A_JPEG_FILE")

    # 1. no_raw
    py_no_raw = HdrTrack(None)
    py_no_raw.begin(rgb)
    meta = hp.ok(["hdrtrack-begin", "--rgb", rgb_path, "--out", str(tmp_path / "o1.npy")])
    assert meta == _state_of(py_no_raw) == {"active": False, "shape": None, "problem": "no_raw"}
    assert _load_track_out(tmp_path / "o1.npy", meta) is None

    # 2. raw_unreadable
    py_bad_raw = HdrTrack(bad_dng_path)
    py_bad_raw.begin(rgb)
    meta = hp.ok(["hdrtrack-begin", "--rgb", rgb_path, "--dng", str(bad_dng_path), "--dng-name", "bad.dng", "--out", str(tmp_path / "o2.npy")])
    assert meta == _state_of(py_bad_raw) == {"active": False, "shape": None, "problem": "raw_unreadable"}

    # 3. capture_mismatch (different ISO/tags)
    py_mismatch = HdrTrack(dng_path, jpeg=jpg_mismatch_path)
    py_mismatch.begin(rgb)
    meta = hp.ok(
        [
            "hdrtrack-begin",
            "--rgb",
            rgb_path,
            "--dng",
            str(dng_path),
            "--dng-name",
            "a.dng",
            "--jpeg",
            str(jpg_mismatch_path),
            "--out",
            str(tmp_path / "o3.npy"),
        ]
    )
    assert meta == _state_of(py_mismatch) == {"active": False, "shape": None, "problem": "capture_mismatch"}

    # 4. metadata_unreadable (corrupt JPEG EXIF/container)
    py_bad_meta = HdrTrack(dng_path, jpeg=bad_jpg_path)
    py_bad_meta.begin(rgb)
    meta = hp.ok(
        [
            "hdrtrack-begin",
            "--rgb",
            rgb_path,
            "--dng",
            str(dng_path),
            "--dng-name",
            "a.dng",
            "--jpeg",
            str(bad_jpg_path),
            "--out",
            str(tmp_path / "o4.npy"),
        ]
    )
    assert meta == _state_of(py_bad_meta) == {"active": False, "shape": None, "problem": "metadata_unreadable"}

    # 5. not_aligned (wrong image pixels)
    wrong_rgb = np.flipud(np.fliplr(rgb))
    wrong_rgb_path = _save(tmp_path / "wrong_rgb.npy", wrong_rgb)
    py_not_aligned = HdrTrack(dng_path, jpeg=jpg_path)
    py_not_aligned.begin(wrong_rgb)
    meta = hp.ok(
        [
            "hdrtrack-begin",
            "--rgb",
            wrong_rgb_path,
            "--dng",
            str(dng_path),
            "--dng-name",
            "a.dng",
            "--jpeg",
            str(jpg_path),
            "--out",
            str(tmp_path / "o5.npy"),
        ]
    )
    assert meta == _state_of(py_not_aligned) == {"active": False, "shape": None, "problem": "not_aligned"}

    # 6. valid synthetic pair
    py_ok = HdrTrack(dng_path, jpeg=jpg_path)
    py_ok.begin(rgb)
    meta = hp.ok(
        [
            "hdrtrack-begin",
            "--rgb",
            rgb_path,
            "--dng",
            str(dng_path),
            "--dng-name",
            "a.dng",
            "--jpeg",
            str(jpg_path),
            "--out",
            str(tmp_path / "o6.npy"),
        ]
    )
    assert meta == _state_of(py_ok) and meta["active"] is True
    rs_layer = _load_track_out(tmp_path / "o6.npy", meta)
    assert rs_layer is not None and py_ok.layer is not None
    _same_f32_nan(py_ok.layer, rs_layer, "begin synthetic pair")


# ---------------------------------------------------------------- 3. warp, box, crop, stamp on layer


def test_geometry_and_stamp_on_layer_parity(tmp_path: Path) -> None:
    rng = np.random.default_rng(RNG_SEED + 2)
    base_layer = np.stack(
        [
            (rng.random((180, 240)) * 4.0 + 0.01).astype(np.float32),
            (rng.random((180, 240)) > 0.85).astype(np.float32),
        ],
        axis=-1,
    )
    base_layer[12:15, 20:25] = np.nan

    py = HdrTrack(None)
    py.layer = base_layer.copy()
    l_path, s_path = _save_state(tmp_path, "s0", py)

    # warp
    geom = Geometry(straighten=5.5, vertical=8, distortion=-6, rotate90=1, flip=True)
    py.warp(geom)
    g_path = _write_json(tmp_path / "geom.json", geom.to_json())
    meta = hp.ok(["hdrtrack-warp", "--layer", l_path, "--state", s_path, "--geometry", g_path, "--out", str(tmp_path / "s1.npy")])
    assert meta == _state_of(py)
    rs1 = _load_track_out(tmp_path / "s1.npy", meta)
    assert rs1 is not None and py.layer is not None
    _same_f32_nan(py.layer, rs1, "track.warp")

    # box
    l_path, s_path = _save_state(tmp_path, "s1", py)
    py.box(15, 20, 120, 160)
    meta = hp.ok(
        [
            "hdrtrack-box",
            "--layer",
            l_path,
            "--state",
            s_path,
            "--x",
            "15",
            "--y",
            "20",
            "--w",
            "120",
            "--h",
            "160",
            "--out",
            str(tmp_path / "s2.npy"),
        ]
    )
    assert meta == _state_of(py)
    rs2 = _load_track_out(tmp_path / "s2.npy", meta)
    assert rs2 is not None and py.layer is not None
    _same_f32_nan(py.layer, rs2, "track.box")

    # crop
    l_path, s_path = _save_state(tmp_path, "s2", py)
    rect = (0.05, 0.1, 0.85, 0.8)
    py.crop(rect)
    r_path = _write_json(tmp_path / "rect.json", list(rect))
    meta = hp.ok(["hdrtrack-crop", "--layer", l_path, "--state", s_path, "--rect", r_path, "--out", str(tmp_path / "s3.npy")])
    assert meta == _state_of(py)
    rs3 = _load_track_out(tmp_path / "s3.npy", meta)
    assert rs3 is not None and py.layer is not None
    _same_f32_nan(py.layer, rs3, "track.crop")

    # stamp (caller mask)
    l_path, s_path = _save_state(tmp_path, "s3", py)
    taken = datetime(2026, 10, 31, 14, 25)
    stamp = StampSpec(enabled=True, date_override="2026-10-31", glow=True, corner="br")
    assert py.layer is not None
    raw_mask = _stamp_changed_mask(py.layer.shape, stamp, taken, None)
    assert raw_mask.any()
    py.stamp(stamp, taken, None)
    m_path = _save(tmp_path / "mask.npy", raw_mask)
    meta = hp.ok(["hdrtrack-stamp", "--layer", l_path, "--state", s_path, "--mask", m_path, "--out", str(tmp_path / "s4.npy")])
    assert meta == _state_of(py)
    rs4 = _load_track_out(tmp_path / "s4.npy", meta)
    assert rs4 is not None and py.layer is not None
    _same_f32_nan(py.layer, rs4, "track.stamp")

    # inactive track passthrough
    inactive = HdrTrack(None)
    inactive.problem = "no_raw"
    il_path, is_path = _save_state(tmp_path, "inact", inactive)
    meta_i = hp.ok(["hdrtrack-warp", "--layer", il_path, "--state", is_path, "--geometry", g_path, "--out", str(tmp_path / "iw.npy")])
    assert meta_i == _state_of(inactive)
    empty_mask = _save(tmp_path / "emask.npy", np.zeros((0, 0), dtype=np.uint8))
    meta_is = hp.ok(["hdrtrack-stamp", "--layer", il_path, "--state", is_path, "--mask", empty_mask, "--out", str(tmp_path / "is.npy")])
    assert meta_is == _state_of(inactive)


# ---------------------------------------------------------------- 4. frame across all 13 FRAME_STYLES


def _rust_frame_layout(tmp: Path, w: int, h: int, spec: FrameSpec, meta: dict[str, str], logos: bool) -> str:
    req: dict[str, Any] = {"w": w, "h": h, "meta": meta, "mark": desktop_mark()}
    if logos:
        req["logos"] = LOGO_JSON
    req_path = _write_json(tmp / "f-req.json", req)
    spec_path = _write_json(tmp / "f-spec.json", spec.to_json())
    res = hp.ok(["frame-layout", "--meta", req_path, "--spec", spec_path, *font_args(cjk=True)])
    return json.dumps(res)


@pytest.mark.parametrize("style", sorted(FRAME_STYLES))
def test_frame_matches_python_diff_bbox_for_every_style(tmp_path: Path, style: str) -> None:
    meta_dict = {"camera": "RICOH GXR", "lens": "GR LENS A12 50mm", "params": "50mm  f/2.5  1/250s  ISO 200", "date": "2026-10-31"}
    logo_map = {lg.id: lg for lg in SAMPLE_LOGOS}
    logo_imgs = {lg.id: _custom_logo_image(lg.id) for lg in SAMPLE_LOGOS}
    configs: list[tuple[int, int, str, str, bool]] = [
        (61, 47, "original", "ricoh", False),
        (73, 100, "3:4", "none", False),
        (91, 63, "1:1", "wordmark", True),
    ]
    with cjk_fonts((FIXTURE_CJK,)):
        for idx, (w, h, ratio, logo, use_custom) in enumerate(configs):
            rng = np.random.default_rng(RNG_SEED + idx + len(style))
            layer = np.stack(
                [
                    (rng.random((h, w)) * 3.0 + 0.05).astype(np.float32),
                    (rng.random((h, w)) > 0.9).astype(np.float32),
                ],
                axis=-1,
            )
            layer[0, 0] = np.nan

            spec = FrameSpec(
                style=style,
                output_ratio=ratio,
                logo=logo,
                show_logo=(logo != "none"),
                color="black" if idx == 1 and style in ("gallery", "leica", "thin_keyline") else "",
            )
            py = HdrTrack(None)
            py.layer = layer.copy()
            l_path, s_path = _save_state(tmp_path, f"{style}-{idx}", py)

            py.frame(
                spec,
                meta_dict,
                logos=logo_map if use_custom else None,
                logo_images=logo_imgs if use_custom else None,
            )
            layout_json = _rust_frame_layout(tmp_path, w, h, spec, meta_dict, use_custom)
            layout_path = tmp_path / f"layout-{style}-{idx}.json"
            layout_path.write_text(layout_json, encoding="utf-8")
            out_path = tmp_path / f"fout-{style}-{idx}.npy"
            got_meta = hp.ok(["hdrtrack-frame", "--layer", l_path, "--state", s_path, "--layout", str(layout_path), "--out", str(out_path)])
            assert got_meta == _state_of(py), f"{style} cfg#{idx}: python={_state_of(py)} rust={got_meta}"
            rs_layer = _load_track_out(out_path, got_meta)
            if py.layer is not None:
                assert rs_layer is not None
                _same_f32_nan(py.layer, rs_layer, f"frame {style} cfg#{idx}")
            else:
                assert rs_layer is None
                assert got_meta["problem"] == "frame_layout"
