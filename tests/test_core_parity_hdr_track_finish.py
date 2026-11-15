"""Parity (part 2): ``hdrtrack-to-grid``, ``hdrtrack-finish``,
WASM vs native parity, and end-to-end ``export_finish(hdr=True)``.
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
from gxr_studio.finish import hdr_export
from gxr_studio.finish.compose import encode_jpeg, load_jpeg
from gxr_studio.finish.hdr_export import HdrTrack, _to_grid
from gxr_studio.finish.serve import export_finish, load_defaults
from gxr_studio.finish.spec import FrameSpec, Geometry, StampSpec
from gxr_studio.render.params import Adjust, EditState
from gxr_studio.render.service import RenderService
from support import hdr_parity as hp
from support.core_cli import require_cli
from support.frame_cases import desktop_mark, font_args
from support.wasm_check import run_area

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


def _spread_pairs(limit: int) -> list[tuple[Path, Path]]:
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


def _encode_gain_jpeg(codes: np.ndarray, quality: int) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(codes, dtype=np.uint8), "L").save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _extract_gain_jpeg(hdr_bytes: bytes) -> bytes:
    """Reconstruct the embedded secondary gainmap JPEG bytes (excluding XMP/ISO APP segments)."""
    from gxr_studio.hdr import container

    _segs, _sos, eoi = container.jpeg_segments(hdr_bytes)
    sec = hdr_bytes[eoi:]
    sec_segs, sec_sos, sec_eoi = container.jpeg_segments(sec)
    plain = b"".join(sec[s.start : s.end] for s in sec_segs if s.marker not in (0xE1, 0xE2))
    return b"\xff\xd8" + plain + sec[sec_sos:sec_eoi]


def _oracle_to_grid(layer: np.ndarray, size: tuple[int, int], factor: int, *, use_fma: bool) -> tuple[np.ndarray, np.ndarray]:
    from gxr_studio.hdr.gain import block_mean
    from support.resample_oracle import oracle_resample_f32

    w_out, h_out = size
    weight = np.isfinite(layer[..., 0]).astype(np.float32)
    planes = [np.where(weight > 0, layer[..., k], np.float32(0.0)).astype(np.float32) for k in range(2)] + [weight]
    if (layer.shape[1], layer.shape[0]) != (w_out, h_out):
        planes = [oracle_resample_f32(p, w_out, h_out, "box", use_fma=use_fma) for p in planes]
    y, clip, w = (block_mean(p, factor) for p in planes)
    photo = w > np.float32(0.999)
    raw_y = np.where(photo, y / np.maximum(w, np.float32(1e-6)), np.float32(np.nan)).astype(np.float32)
    return raw_y, (clip / np.maximum(w, np.float32(1e-6))) > np.float32(0.01)


def _rust_frame_layout(tmp: Path, w: int, h: int, spec: FrameSpec, meta: dict[str, str]) -> str:
    req: dict[str, Any] = {"w": w, "h": h, "meta": meta, "mark": desktop_mark()}
    req_path = _write_json(tmp / "f-req.json", req)
    spec_path = _write_json(tmp / "f-spec.json", spec.to_json())
    res = hp.ok(["frame-layout", "--meta", req_path, "--spec", spec_path, *font_args(cjk=True)])
    return json.dumps(res)


# ---------------------------------------------------------------- 5. finish: _to_grid (incl. BOX resize), gain JPEG, unified finish


def test_finish_reasons_and_exact_bytes(tmp_path: Path) -> None:
    jpg_bytes, dng_bytes = hs.pair(0, orientation=1)
    rgb = load_jpeg(io.BytesIO(jpg_bytes), None)
    dng_path = tmp_path / "p.dng"
    dng_path.write_bytes(dng_bytes)
    jpg_path = tmp_path / "p.jpg"
    jpg_path.write_bytes(jpg_bytes)

    py_track = HdrTrack(dng_path, jpeg=jpg_path)
    py_track.begin(rgb)
    assert py_track.layer is not None
    # Inject a partial-NaN boundary block to exercise _to_grid's w > 0.999 and clip > 0.01 rules.
    py_track.layer[0:2, 0:2] = np.nan
    py_track.layer[4, 4, 1] = 1.0

    l_path, s_path = _save_state(tmp_path, "fin", py_track)
    rgb_path = _save(tmp_path / "rgb.npy", rgb)
    payload = encode_jpeg(rgb, 92)
    payload_path = tmp_path / "payload.jpg"
    payload_path.write_bytes(payload)

    # 1. not_jpeg
    py_out, py_info = hdr_export.finish(payload, py_track, "png", rgb)
    assert py_out == payload and py_info == {"hdr": False, "reason": "not_jpeg"}
    fin_png = hp.ok(
        [
            "hdrtrack-finish",
            "--payload",
            str(payload_path),
            "--layer",
            l_path,
            "--state",
            s_path,
            "--fmt",
            "png",
            "--rgb",
            rgb_path,
            "--out",
            str(tmp_path / "o-png.npy"),
        ]
    )
    assert fin_png == py_info and np.load(tmp_path / "o-png.npy").tobytes() == payload

    # 2. inactive track problem
    inact = HdrTrack(None)
    inact.problem = "not_aligned"
    il_path, is_path = _save_state(tmp_path, "inact", inact)
    py_out_i, py_info_i = hdr_export.finish(payload, inact, "jpeg", rgb)
    assert py_out_i == payload and py_info_i == {"hdr": False, "reason": "not_aligned"}
    fin_i = hp.ok(
        [
            "hdrtrack-finish",
            "--payload",
            str(payload_path),
            "--layer",
            il_path,
            "--state",
            is_path,
            "--fmt",
            "jpeg",
            "--rgb",
            rgb_path,
            "--out",
            str(tmp_path / "o-inact.npy"),
        ]
    )
    assert fin_i == py_info_i and np.load(tmp_path / "o-inact.npy").tobytes() == payload

    # 3. _to_grid exact on same size AND Gate A/B on long-edge downscale
    h, w = rgb.shape[:2]
    hp.ok(
        [
            "hdrtrack-to-grid",
            "--layer",
            l_path,
            "--out-w",
            str(w),
            "--out-h",
            str(h),
            "--factor",
            "4",
            "--out",
            str(tmp_path / "tg-y.npy"),
            "--clip-out",
            str(tmp_path / "tg-c.npy"),
        ]
    )
    py_ry, py_rc = _to_grid(py_track.layer, (w, h), 4)
    _same_f32_nan(py_ry, np.load(tmp_path / "tg-y.npy"), "_to_grid same-size raw_y")
    hp.same_exact(py_rc, np.load(tmp_path / "tg-c.npy"), "_to_grid same-size clip")

    # Resized (long_edge) _to_grid: non-8-aligned odd dimensions + partial NaNs
    for rw, rh in ((100, 67), (73, 49)):
        hp.ok(
            [
                "hdrtrack-to-grid",
                "--layer",
                l_path,
                "--out-w",
                str(rw),
                "--out-h",
                str(rh),
                "--factor",
                "4",
                "--out",
                str(tmp_path / f"tg-y-{rw}.npy"),
                "--clip-out",
                str(tmp_path / f"tg-c-{rw}.npy"),
            ]
        )
        rs_ry = np.load(tmp_path / f"tg-y-{rw}.npy")
        rs_rc = np.load(tmp_path / f"tg-c-{rw}.npy")
        # Gate A: exact float32 bits against sequential IEEE 754 oracle
        or_ry, or_rc = _oracle_to_grid(py_track.layer, (rw, rh), 4, use_fma=False)
        _same_f32_nan(or_ry, rs_ry, f"_to_grid Gate A ({rw}x{rh}) raw_y")
        hp.same_exact(or_rc, rs_rc, f"_to_grid Gate A ({rw}x{rh}) clip")
        # Gate B: any diff vs host Pillow F-mode BOX resize matches FMA contraction oracle
        py_ry_r, py_rc_r = _to_grid(py_track.layer, (rw, rh), 4)
        if not np.array_equal(py_rc_r, rs_rc) or not (
            np.array_equal(np.isnan(py_ry_r), np.isnan(rs_ry))
            and np.array_equal(py_ry_r.view(np.uint32)[~np.isnan(py_ry_r)], rs_ry.view(np.uint32)[~np.isnan(rs_ry)])
        ):
            fma_ry, fma_rc = _oracle_to_grid(py_track.layer, (rw, rh), 4, use_fma=True)
            _same_f32_nan(fma_ry, py_ry_r, f"_to_grid Gate B FMA ({rw}x{rh}) raw_y")
            hp.same_exact(fma_rc, py_rc_r, f"_to_grid Gate B FMA ({rw}x{rh}) clip")

    # 4. Unified finish: exact HDR JPEG bytes AND exact gainmap JPEG bytes against Pillow
    py_hdr, py_hdr_info = hdr_export.finish(payload, py_track, "jpeg", rgb)
    assert py_hdr_info["hdr"] is True
    fin_meta = hp.ok(
        [
            "hdrtrack-finish",
            "--payload",
            str(payload_path),
            "--layer",
            l_path,
            "--state",
            s_path,
            "--fmt",
            "jpeg",
            "--rgb",
            rgb_path,
            "--out",
            str(tmp_path / "hdr-out.npy"),
        ]
    )
    assert fin_meta == py_hdr_info
    rs_hdr = np.load(tmp_path / "hdr-out.npy").tobytes()
    assert _extract_gain_jpeg(rs_hdr) == _extract_gain_jpeg(py_hdr), "gainmap JPEG bytes differ from Pillow"
    assert rs_hdr == py_hdr, f"HDR JPEG bytes differ: len py={len(py_hdr)} rs={len(rs_hdr)}"

    # Also check quality < 90 and non-8-aligned output dimensions in unified finish
    rgb_sm = np.asarray(Image.fromarray(rgb).resize((73, 49), Image.Resampling.LANCZOS))
    sm_rgb_path = _save(tmp_path / "rgb-sm.npy", rgb_sm)
    sm_payload = encode_jpeg(rgb_sm, 80)
    sm_pay_path = tmp_path / "payload-sm.jpg"
    sm_pay_path.write_bytes(sm_payload)
    sm_params = _write_json(tmp_path / "params-q75.json", {"quality": 75, "map_factor": 1})
    py_track_q = HdrTrack(dng_path, jpeg=jpg_path)
    py_track_q.layer = py_track.layer.copy()
    from gxr_studio.hdr.gain import Params as PyParams

    py_track_q.params = PyParams(quality=75, map_factor=1)
    py_hdr_sm, py_info_sm = hdr_export.finish(sm_payload, py_track_q, "jpeg", rgb_sm)
    assert py_info_sm["hdr"] is True
    rs_info_sm = hp.ok(
        [
            "hdrtrack-finish",
            "--payload",
            str(sm_pay_path),
            "--layer",
            l_path,
            "--state",
            s_path,
            "--fmt",
            "jpeg",
            "--rgb",
            sm_rgb_path,
            "--params",
            sm_params,
            "--out",
            str(tmp_path / "hdr-sm.npy"),
        ]
    )
    rs_hdr_sm = np.load(tmp_path / "hdr-sm.npy").tobytes()
    if rs_hdr_sm != py_hdr_sm:
        from gxr_studio.hdr import container as hc
        from gxr_studio.hdr import gain as hg

        or_ry, or_rc = _oracle_to_grid(py_track.layer, (73, 49), 1, use_fma=False)
        or_res = hg.compute_gain(hg.block_mean(hp._ORACLE[0][rgb_sm] @ hg.LUMA, 1), or_ry, or_rc, hg.Params(quality=75, map_factor=1))
        or_codes = hp.codes_of(or_res)
        assert _extract_gain_jpeg(rs_hdr_sm) == _encode_gain_jpeg(or_codes, 75)
        or_hdr_sm = hc.assemble(sm_payload, _encode_gain_jpeg(or_codes, 75), or_res.meta)
        assert rs_hdr_sm == or_hdr_sm
        assert rs_info_sm == {"hdr": True, "gain_max_stops": or_res.stats["gain_max_stops"]}
    else:
        assert _extract_gain_jpeg(rs_hdr_sm) == _extract_gain_jpeg(py_hdr_sm)
        assert rs_info_sm == py_info_sm

    # 5. container reason on bad payload
    py_bad_c, py_bad_info = hdr_export.finish(b"not-a-jpeg", py_track, "jpeg", rgb)
    assert py_bad_c == b"not-a-jpeg" and py_bad_info == {"hdr": False, "reason": "container"}
    bad_payload = tmp_path / "bad-payload.jpg"
    bad_payload.write_bytes(b"not-a-jpeg")
    fin_bad = hp.ok(
        [
            "hdrtrack-finish",
            "--payload",
            str(bad_payload),
            "--layer",
            l_path,
            "--state",
            s_path,
            "--fmt",
            "jpeg",
            "--rgb",
            rgb_path,
            "--out",
            str(tmp_path / "bad-fin.npy"),
        ]
    )
    assert fin_bad == {"hdr": False, "reason": "container"}
    assert np.load(tmp_path / "bad-fin.npy").tobytes() == b"not-a-jpeg"

    # 6. no_curve reason on saturated/flat RGB (sdr_y >= 0.97 -> 0 valid pixels)
    flat_rgb = np.full_like(rgb, 255)
    flat_path = _save(tmp_path / "flat.npy", flat_rgb)
    py_nc, py_nc_info = hdr_export.finish(payload, py_track, "jpeg", flat_rgb)
    assert py_nc == payload and py_nc_info == {"hdr": False, "reason": "no_curve"}
    fin_nc = hp.ok(
        [
            "hdrtrack-finish",
            "--payload",
            str(payload_path),
            "--layer",
            l_path,
            "--state",
            s_path,
            "--fmt",
            "jpeg",
            "--rgb",
            flat_path,
            "--out",
            str(tmp_path / "o-nc.npy"),
        ]
    )
    assert fin_nc == py_nc_info and np.load(tmp_path / "o-nc.npy").tobytes() == payload


# ---------------------------------------------------------------- 6. WASM vs Native parity


def test_wasm_matches_native_for_hdr_track(tmp_path: Path) -> None:
    jpg_bytes, dng_bytes = hs.pair(0, orientation=1)
    rgb = load_jpeg(io.BytesIO(jpg_bytes), None)
    py = HdrTrack(tmp_path / "pair.dng", jpeg=tmp_path / "pair.jpg")
    (tmp_path / "pair.dng").write_bytes(dng_bytes)
    (tmp_path / "pair.jpg").write_bytes(jpg_bytes)
    py.begin(rgb)
    assert py.layer is not None
    # Stamp edge NaNs to verify byte-identical NaN preservation in WASM.
    py.layer[1:3, 2:4] = np.nan

    f1 = tmp_path / "active"
    f1.mkdir()
    rng = np.random.default_rng(RNG_SEED + 3)
    fl = (rng.random((20, 24, 2)) * 2.5).astype(np.float32)
    fl[2, 3] = np.nan
    _save(f1 / "float_layer.npy", fl)
    _write_json(f1 / "geom.json", Geometry(straighten=3.5, vertical=8, rotate90=1, flip=True).to_json())
    _write_json(f1 / "rect.json", [0.1, 0.1, 0.8, 0.75])
    _save(f1 / "rgb.npy", rgb)
    (f1 / "input.dng").write_bytes(dng_bytes)
    (f1 / "input.jpg").write_bytes(jpg_bytes)
    _save(f1 / "layer.npy", py.layer)
    _write_json(f1 / "state.json", _state_of(py))
    _write_json(f1 / "box.json", {"x": 4, "y": 5, "w": 64, "h": 48})
    mask = np.zeros(py.layer.shape[:2], dtype=np.uint8)
    mask[8:12, 8:14] = 1
    _save(f1 / "mask.npy", mask)
    layout_json = _rust_frame_layout(
        tmp_path,
        py.layer.shape[1],
        py.layer.shape[0],
        FrameSpec(style="gallery", output_ratio="original"),
        {"camera": "RICOH GXR"},
    )
    (f1 / "layout.json").write_text(layout_json, encoding="utf-8")
    _save(f1 / "finish_rgb.npy", rgb)
    _write_json(f1 / "to_grid.json", {"out_w": rgb.shape[1] // 2 + 3, "out_h": rgb.shape[0] // 2 + 1, "factor": 4})
    (f1 / "payload.jpg").write_bytes(encode_jpeg(rgb, 90))
    (f1 / "bad_nums").write_text("1", encoding="utf-8")
    (f1 / "json_depth").write_text("1", encoding="utf-8")

    # Inactive + png/container/bad-dng cases
    f2 = tmp_path / "inactive"
    f2.mkdir()
    _save(f2 / "rgb.npy", rgb)
    (f2 / "input.dng").write_bytes(b"CORRUPT")
    _save(f2 / "layer.npy", np.zeros((0, 0, 2), dtype=np.float32))
    _write_json(f2 / "state.json", {"active": False, "shape": None, "problem": "no_raw"})
    _write_json(f2 / "geom.json", {})
    _write_json(f2 / "box.json", {"x": 0, "y": 0, "w": 1, "h": 1})
    _write_json(f2 / "rect.json", [0.0, 0.0, 1.0, 1.0])
    _save(f2 / "mask.npy", np.zeros((0, 0), dtype=np.uint8))
    (f2 / "layout.json").write_text("null", encoding="utf-8")
    _save(f2 / "finish_rgb.npy", rgb)
    (f2 / "fmt.txt").write_text("png", encoding="utf-8")
    (f2 / "payload.jpg").write_bytes(b"not-a-jpeg")

    proc = run_area("hdrtrack", [f1, f2], tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---------------------------------------------------------------- 7. End-to-end >= 10 real pairs: export_finish(hdr=True) vs Rust chain


def _run_rust_chain(
    tmp: Path,
    tag: str,
    rgb_base: np.ndarray,
    dng: Path,
    jpg: Path,
    edit: EditState,
    crop_rect: tuple[float, float, float, float] | None,
    fitted_box: tuple[int, int, int, int] | None,
    stamp: StampSpec,
    taken: datetime | None,
    file_time: datetime | None,
    frame: FrameSpec,
    meta_dict: dict[str, str],
    pre_encode_rgb: np.ndarray,
    payload: bytes,
    stamp_after_box_shape: tuple[int, int],
) -> tuple[bytes, dict[str, Any], np.ndarray | None]:
    rgb0_path = _save(tmp / f"{tag}-rgb0.npy", rgb_base)
    l_path = tmp / f"{tag}-l0.npy"
    state = hp.ok(["hdrtrack-begin", "--rgb", rgb0_path, "--dng", str(dng), "--dng-name", dng.name, "--jpeg", str(jpg), "--out", str(l_path)])
    s_path = Path(_write_json(tmp / f"{tag}-s0.json", state))

    geom = edit.geometry if isinstance(edit.geometry, Geometry) else Geometry()
    if not geom.is_identity():
        g_path = _write_json(tmp / f"{tag}-g.json", geom.to_json())
        l_next = tmp / f"{tag}-l1.npy"
        state = hp.ok(["hdrtrack-warp", "--layer", str(l_path), "--state", str(s_path), "--geometry", g_path, "--out", str(l_next)])
        l_path = l_next
        s_path = Path(_write_json(tmp / f"{tag}-s1.json", state))

    if fitted_box is not None:
        bx, by, bw, bh = fitted_box
        l_next = tmp / f"{tag}-l2.npy"
        state = hp.ok(
            [
                "hdrtrack-box",
                "--layer",
                str(l_path),
                "--state",
                str(s_path),
                "--x",
                str(bx),
                "--y",
                str(by),
                "--w",
                str(bw),
                "--h",
                str(bh),
                "--out",
                str(l_next),
            ]
        )
        l_path = l_next
        s_path = Path(_write_json(tmp / f"{tag}-s2.json", state))
    elif crop_rect is not None and crop_rect != (0.0, 0.0, 1.0, 1.0):
        r_path = _write_json(tmp / f"{tag}-rect.json", list(crop_rect))
        l_next = tmp / f"{tag}-l2.npy"
        state = hp.ok(["hdrtrack-crop", "--layer", str(l_path), "--state", str(s_path), "--rect", r_path, "--out", str(l_next)])
        l_path = l_next
        s_path = Path(_write_json(tmp / f"{tag}-s2.json", state))

    border_stamp = stamp.enabled and stamp.placement == "border" and frame.style == "instant"
    eff_stamp = StampSpec(enabled=False) if border_stamp else stamp
    mask = _stamp_changed_mask(stamp_after_box_shape, eff_stamp, taken, file_time)
    m_path = _save(tmp / f"{tag}-mask.npy", mask)
    l_next = tmp / f"{tag}-l3.npy"
    state = hp.ok(["hdrtrack-stamp", "--layer", str(l_path), "--state", str(s_path), "--mask", m_path, "--out", str(l_next)])
    l_path = l_next
    s_path = Path(_write_json(tmp / f"{tag}-s3.json", state))

    if frame.style != "none":
        fh, fw = stamp_after_box_shape
        layout_json = _rust_frame_layout(tmp, fw, fh, frame, meta_dict)
        lay_path = tmp / f"{tag}-lay.json"
        lay_path.write_text(layout_json, encoding="utf-8")
        l_next = tmp / f"{tag}-l4.npy"
        state = hp.ok(["hdrtrack-frame", "--layer", str(l_path), "--state", str(s_path), "--layout", str(lay_path), "--out", str(l_next)])
        l_path = l_next
        s_path = Path(_write_json(tmp / f"{tag}-s4.json", state))

    frgb_path = _save(tmp / f"{tag}-frgb.npy", pre_encode_rgb)
    p_path = tmp / f"{tag}-payload.jpg"
    p_path.write_bytes(payload)
    out_npy = tmp / f"{tag}-final.npy"
    fin_meta = hp.ok(
        [
            "hdrtrack-finish",
            "--payload",
            str(p_path),
            "--layer",
            str(l_path),
            "--state",
            str(s_path),
            "--fmt",
            "jpeg",
            "--rgb",
            frgb_path,
            "--out",
            str(out_npy),
        ]
    )
    rs_layer = np.load(l_path) if state["active"] else None
    return np.load(out_npy).tobytes(), fin_meta, rs_layer


@pytest.mark.parametrize("long_edge", [None, 1600])
def test_e2e_real_pairs_match_export_finish(
    long_edge: int | None, tmp_path: Path, lib: Any, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    pairs = _spread_pairs(16)
    if not pairs:
        if os.environ.get("GXR_REQUIRE_CORE") == "1":
            pytest.skip(f"no {CAPTURES} directory on this host")
        pytest.skip("no real pairs")

    from gxr_studio.finish import compose as compose_mod
    from gxr_studio.finish import serve as serve_mod

    folder = lib.root / "2026" / f"real_{long_edge or 'full'}"
    folder.mkdir(parents=True)
    export_dir = settings.export_dir
    render = RenderService(settings.home_dir, lib)

    edits = [
        (
            EditState(
                adjust=Adjust(exposure_ev=0.25, contrast=10),
                geometry=Geometry(straighten=2.0),
                frame=FrameSpec(style="gallery", output_ratio="original"),
                stamp=StampSpec(enabled=True, date_override="2026-10-31"),
            ),
            {"crop": {"rect": [0.05, 0.05, 0.9, 0.9], "preset": "free"}},
        ),
        (EditState(adjust=Adjust(contrast=6), frame=FrameSpec(style="leica", show_logo=True), stamp=StampSpec(enabled=False)), {}),
        (
            EditState(
                geometry=Geometry(rotate90=1, flip=True),
                frame=FrameSpec(style="thin_keyline"),
                stamp=StampSpec(enabled=True, date_override="2026-11-15"),
            ),
            {},
        ),
        (
            EditState(
                adjust=Adjust(exposure_ev=-0.2),
                frame=FrameSpec(style="instant", fmt="instax_mini"),
                stamp=StampSpec(enabled=True, placement="border", date_override="2026-11-15"),
            ),
            {},
        ),
        (EditState(frame=FrameSpec(style="blur_fill"), stamp=StampSpec(enabled=False)), {}),
        (EditState(), {}),
    ]

    compared = 0
    for idx, (jpg, dng) in enumerate(pairs):
        stem = f"REAL{long_edge or 'F'}_{idx:03d}"
        j_copy = folder / f"{stem}.JPG"
        d_copy = folder / f"{stem}.DNG"
        j_copy.symlink_to(jpg)
        d_copy.symlink_to(dng)
        im_rgb = load_jpeg(jpg, None)
        h0, w0 = im_rgb.shape[:2]
        shot = {
            "shot_id": stem,
            "name": stem,
            "width": w0,
            "height": h0,
            "files": {
                "jpg": {"relpath": f"2026/real_{long_edge or 'full'}/{stem}.JPG"},
                "dng": {"relpath": f"2026/real_{long_edge or 'full'}/{stem}.DNG"},
            },
        }
        edit, side = edits[idx % len(edits)]

        # Capture the pre-encode rgb, payload, and geometry/box decisions Python computes
        captured: dict[str, Any] = {}
        orig_finish_hdr = hdr_export.finish

        def spy_finish_hdr(payload_bytes: bytes, track_obj: HdrTrack, fmt: str, pre_rgb: np.ndarray) -> tuple[bytes, dict[str, Any]]:
            captured["payload"] = payload_bytes
            captured["rgb"] = pre_rgb
            captured["layer"] = None if track_obj.layer is None else track_obj.layer.copy()
            captured["problem"] = track_obj.problem
            out_bytes, info = orig_finish_hdr(payload_bytes, track_obj, fmt, pre_rgb)
            captured["info"] = info
            return out_bytes, info

        with monkeypatch.context() as mp:
            mp.setattr(serve_mod, "finish_hdr", spy_finish_hdr)
            res = export_finish(
                render, lib, shot, edit, side, export_dir, [], quality=92, long_edge=long_edge, fmt="jpeg", base="auto", hdr=True
            )

        py_bytes = res.path.read_bytes()
        frame_def, stamp_def = load_defaults(render.home)
        frame = compose_mod.resolved_frame(edit.frame, frame_def)
        stamp = compose_mod.resolved_stamp(edit.stamp, stamp_def)
        meta_dict, taken, file_time = serve_mod._meta(shot, lib)

        geom = edit.geometry if isinstance(edit.geometry, Geometry) else Geometry()
        from gxr_studio.finish.geometry import clamp_norm_rect, fit_preset_in_valid, needs_valid_crop, normalized_valid_rect, warp_valid

        valid = np.ones((h0, w0), dtype=bool) if geom.is_identity() else warp_valid(w0, h0, geom)
        raw_crop_obj: Any = side.get("crop")
        crop_raw: dict[str, Any] = raw_crop_obj if isinstance(raw_crop_obj, dict) else {}
        raw_rect = crop_raw.get("rect")
        crop_rect = (
            (float(raw_rect[0]), float(raw_rect[1]), float(raw_rect[2]), float(raw_rect[3]))
            if isinstance(raw_rect, list) and len(raw_rect) == 4
            else None
        )
        raw_preset = crop_raw.get("preset")
        preset = raw_preset if isinstance(raw_preset, str) else None
        fitted = fit_preset_in_valid(valid, preset, crop_rect or (0.0, 0.0, 1.0, 1.0)) if needs_valid_crop(geom) else None
        eff_crop = crop_rect
        shape_after_crop = valid.shape
        if fitted is not None:
            shape_after_crop = (fitted[3], fitted[2])
        elif needs_valid_crop(geom):
            eff_crop = (
                normalized_valid_rect(valid) if (crop_rect is None or crop_rect == (0.0, 0.0, 1.0, 1.0)) else clamp_norm_rect(valid, crop_rect)
            )
            from gxr_studio.finish.geometry import crop_box

            _, _, cw, ch = crop_box(valid.shape[1], valid.shape[0], eff_crop)
            shape_after_crop = (ch, cw)
        elif crop_rect is not None and crop_rect != (0.0, 0.0, 1.0, 1.0):
            from gxr_studio.finish.geometry import crop_box

            _, _, cw, ch = crop_box(valid.shape[1], valid.shape[0], crop_rect)
            shape_after_crop = (ch, cw)

        untouched = not compose_mod.has_visible_edit(edit, side, frame, stamp)
        if untouched:
            rs_bytes, rs_info, rs_layer = _run_rust_chain(
                tmp_path,
                f"e2e-{long_edge or 'full'}-{idx}",
                im_rgb,
                dng,
                jpg,
                EditState(),
                None,
                None,
                StampSpec(enabled=False),
                None,
                None,
                FrameSpec(style="none"),
                meta_dict,
                captured["rgb"],
                captured["payload"],
                im_rgb.shape[:2],
            )
        else:
            rs_bytes, rs_info, rs_layer = _run_rust_chain(
                tmp_path,
                f"e2e-{long_edge or 'full'}-{idx}",
                im_rgb,
                dng,
                jpg,
                edit,
                eff_crop,
                fitted,
                stamp,
                taken,
                file_time,
                frame,
                meta_dict,
                captured["rgb"],
                captured["payload"],
                shape_after_crop,
            )

        if captured["layer"] is None:
            assert rs_layer is None, f"pair {idx} ({dng.name}): expected inactive layer"
        else:
            assert rs_layer is not None
            _same_f32_nan(captured["layer"], rs_layer, f"e2e {idx} ({dng.name}) final track layer")

        if rs_bytes != py_bytes:
            # Gate A + Gate B: attribute any diff to POW_CODES=(71, 180) and/or Pillow F-mode BOX FMA.
            # Gate A: sequential IEEE oracle (BOX without FMA + correctly-rounded sRGB table) matches Rust bit-for-bit.
            # Gate B: FMA BOX oracle + host sRGB table reproduces host Python bytes bit-for-bit.
            from gxr_studio.hdr import container as hc
            from gxr_studio.hdr import gain as hg

            assert captured["layer"] is not None
            pre_rgb = captured["rgb"]
            out_size = (pre_rgb.shape[1], pre_rgb.shape[0])
            or_ry, or_rc = _oracle_to_grid(captured["layer"], out_size, 4, use_fma=False)
            or_sdr = hg.block_mean(hp._ORACLE[0][pre_rgb] @ hg.LUMA, 4)
            or_res = hg.compute_gain(or_sdr, or_ry, or_rc, hg.Params())
            or_codes = hp.codes_of(or_res)
            or_gain_jpg = _encode_gain_jpeg(or_codes, 90)
            assert _extract_gain_jpeg(rs_bytes) == or_gain_jpg, f"pair {idx} ({dng.name}): gainmap JPEG differs from Pillow"
            or_hdr = hc.assemble(captured["payload"], or_gain_jpg, or_res.meta)
            assert rs_bytes == or_hdr, f"pair {idx} ({dng.name}): e2e HDR bytes differ even from sequential oracle"
            assert rs_info == {"hdr": True, "gain_max_stops": or_res.stats["gain_max_stops"]}

            fma_ry, fma_rc = _oracle_to_grid(captured["layer"], out_size, 4, use_fma=True)
            fma_res = hg.compute_gain(hg.sdr_luma_grid(pre_rgb, 4), fma_ry, fma_rc, hg.Params())
            fma_hdr = hc.assemble(captured["payload"], _encode_gain_jpeg(hp.codes_of(fma_res), 90), fma_res.meta)
            assert py_bytes == fma_hdr, f"pair {idx} ({dng.name}): host Python differs from FMA oracle"
        else:
            if captured["info"].get("hdr"):
                assert _extract_gain_jpeg(rs_bytes) == _extract_gain_jpeg(py_bytes)
            assert rs_info == captured["info"], f"pair {idx} ({dng.name}): python={captured['info']} rust={rs_info}"
        compared += 1

    if CAPTURES.is_dir():
        assert compared >= 10, f"expected >= 10 real pairs compared, got {compared}"
