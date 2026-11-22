"""关机图（GXROFF.JPG）格式、校验器、双端对拍与实拍参考测试。"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from support import samples
from support.core_cli import require_cli, run_json
from support.wasm_check import PKG

_PARENT = samples.sample_parent()
_REF_SCRIPT = _PARENT / "scripts" / "make_off_image.py"
_FIELD_DIR = _PARENT / "firmware" / "package_v310b_off_image_r2" / "local" / "test260-REVIEW_PENDING"
_FIELD_REF = _FIELD_DIR / "GXROFF.JPG"


def _load_ref_tool() -> Any:
    if not _REF_SCRIPT.is_file():
        pytest.skip("reference make_off_image.py not present")
    spec = importlib.util.spec_from_file_location("ref_make_off_image", _REF_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _with_exif_orientation(jpeg: bytes, orientation: int) -> bytes:
    """在 SOI 之后插入仅含 Orientation 标签的 APP1 EXIF 段。"""
    tiff = b"MM\x00\x2a" + struct.pack(">I", 8) + struct.pack(">H", 1)
    tiff += struct.pack(">HHIHH", 0x0112, 3, 1, orientation, 0) + struct.pack(">I", 0)
    body = b"Exif\x00\x00" + tiff
    return jpeg[:2] + b"\xff\xe1" + struct.pack(">H", len(body) + 2) + body + jpeg[2:]


def _make_jpeg_bytes(rgb: np.ndarray, *, quality: int = 95, orientation: int | None = None) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, "JPEG", quality=quality)
    raw = buf.getvalue()
    return _with_exif_orientation(raw, orientation) if orientation is not None else raw


def _make_png_bytes(rgba: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG")
    return buf.getvalue()


def _varied_inputs() -> list[tuple[str, bytes]]:
    # 1. 横图（1200x700 渐变与几何块）
    yy, xx = np.mgrid[0:700, 0:1200]
    land = np.stack([(xx * 255 // 1199), (yy * 255 // 699), ((xx + yy) * 127 // 1898)], axis=-1).astype(np.uint8)
    land[180:520, 320:880] = (210, 140, 75)

    # 2 & 3. 竖图（存储为 900x600 横图，左半白右半黑，分别加 Orientation 6 和 8）
    split = np.zeros((600, 900, 3), dtype=np.uint8)
    split[:, :450] = 245
    split[:, 450:] = 15

    # 4. 带透明通道的 PNG（800x800）
    py, px = np.mgrid[0:800, 0:800]
    png_rgba = np.zeros((800, 800, 4), dtype=np.uint8)
    png_rgba[..., 0] = (px * 255 // 799).astype(np.uint8)
    png_rgba[..., 1] = (py * 255 // 799).astype(np.uint8)
    png_rgba[..., 2] = 160
    png_rgba[..., 3] = np.where((px - 400) ** 2 + (py - 400) ** 2 < 320**2, 255, 96).astype(np.uint8)

    # 5. 高频细节图（q92 会超过 32,768 B，必须自动降品质）
    rng = np.random.default_rng(20261121)
    detailed = rng.integers(0, 256, size=(768, 1024, 3), dtype=np.uint8)

    # 6. 极小图（24x16，需要放大铺满 640x480）
    ty, tx = np.mgrid[0:16, 0:24]
    tiny = np.stack([(tx * 10) & 255, (ty * 16) & 255, ((tx + ty) * 7) & 255], axis=-1).astype(np.uint8)

    # 7. 刚好 640x480
    ey, ex = np.mgrid[0:480, 0:640]
    exact = np.stack([(ex * 255 // 639), (ey * 255 // 479), np.full_like(ex, 128)], axis=-1).astype(np.uint8)

    return [
        ("landscape", _make_jpeg_bytes(land)),
        ("portrait_o6", _make_jpeg_bytes(split, orientation=6)),
        ("portrait_o8", _make_jpeg_bytes(split, orientation=8)),
        ("png_rgba", _make_png_bytes(png_rgba)),
        ("detailed", _make_jpeg_bytes(detailed, quality=98)),
        ("tiny", _make_jpeg_bytes(tiny, quality=95)),
        ("exact_640x480", _make_jpeg_bytes(exact, quality=95)),
    ]


def _segments_of(data: bytes) -> list[tuple[int, bytes]]:
    assert data[:2] == b"\xff\xd8"
    pos = 2
    segs: list[tuple[int, bytes]] = []
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker = data[pos + 1]
        length = struct.unpack(">H", data[pos + 2 : pos + 4])[0]
        body = data[pos + 4 : pos + 2 + length]
        segs.append((marker, body))
        pos += 2 + length
        if marker == 0xDA:
            break
    return segs


def _run_ref_check_cli(path: Path) -> str:
    if not _REF_SCRIPT.is_file():
        pytest.skip("reference make_off_image.py not present")
    proc = subprocess.run(
        ["/usr/bin/python3", "-I", str(_REF_SCRIPT), "--check", str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"reference --check failed: {proc.stdout}{proc.stderr}"
    return proc.stdout.strip()


def _wasm_off_image(rgb: np.ndarray, tmp_path: Path) -> tuple[bytes, dict[str, Any]]:
    if shutil.which("node") is None or not PKG.is_file():
        if os.environ.get("GXR_REQUIRE_CORE") == "1":
            pytest.fail("node and gxr_wasm package are required when GXR_REQUIRE_CORE=1")
        pytest.skip("node or gxr_wasm package missing")
    raw_path = tmp_path / "wasm-rgb.bin"
    out_path = tmp_path / "wasm-out.jpg"
    raw_path.write_bytes(np.ascontiguousarray(rgb, dtype=np.uint8).tobytes())
    h, w = rgb.shape[:2]
    script = f"""
import {{ readFileSync, writeFileSync }} from 'node:fs';
import * as wasm from {json.dumps(str(PKG))};
wasm.initSync({{ module: readFileSync({json.dumps(str(PKG.with_name("gxr_wasm_bg.wasm")))}) }});
const rgb = new Uint8Array(readFileSync({json.dumps(str(raw_path))}));
const made = wasm.off_image_make(rgb, {w}, {h});
try {{
  const jpg = made.file();
  writeFileSync({json.dumps(str(out_path))}, jpg);
  const chk = JSON.parse(wasm.off_image_check(jpg));
  console.log(JSON.stringify({{ meta: JSON.parse(made.json()), check: chk }}));
}} finally {{
  made.free();
}}
"""
    proc = subprocess.run(["node", "--input-type=module", "-e", script], check=False, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"wasm off_image_make failed: {proc.stdout}{proc.stderr}"
    return out_path.read_bytes(), json.loads(proc.stdout)


def _wasm_check(data: bytes, tmp_path: Path) -> dict[str, Any]:
    if shutil.which("node") is None or not PKG.is_file():
        if os.environ.get("GXR_REQUIRE_CORE") == "1":
            pytest.fail("node and gxr_wasm package are required when GXR_REQUIRE_CORE=1")
        pytest.skip("node or gxr_wasm package missing")
    in_path = tmp_path / "wasm-check.jpg"
    in_path.write_bytes(data)
    script = f"""
import {{ readFileSync }} from 'node:fs';
import * as wasm from {json.dumps(str(PKG))};
wasm.initSync({{ module: readFileSync({json.dumps(str(PKG.with_name("gxr_wasm_bg.wasm")))}) }});
const jpg = new Uint8Array(readFileSync({json.dumps(str(in_path))}));
console.log(wasm.off_image_check(jpg));
"""
    proc = subprocess.run(["node", "--input-type=module", "-e", script], check=False, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"wasm off_image_check failed: {proc.stdout}{proc.stderr}"
    return dict(json.loads(proc.stdout))


@pytest.mark.parametrize("name,raw", _varied_inputs(), ids=lambda x: x if isinstance(x, str) else "")
def test_format_and_parity_varied_inputs(name: str, raw: bytes, tmp_path: Path) -> None:
    from gxr_studio import off_image

    rgb = off_image.prepare_image_bytes(raw)
    py_res = off_image.encode_off_image_rgb(rgb)

    assert py_res.width == 640 and py_res.height == 480
    assert len(py_res.data) <= off_image.MAX_BYTES
    assert py_res.bytes == len(py_res.data)
    if name == "detailed":
        assert py_res.quality < 92

    # 校验段布局与自身校验器
    segs = _segments_of(py_res.data)
    assert [m for m, _ in segs] == [0xDB, 0xC0, 0xC4, 0xDD, 0xDA]
    assert len(segs[0][1]) == 130
    assert py_res.data[-2:] == b"\xff\xd9"
    py_chk = off_image.check_off_image(py_res.data)
    assert py_chk.ok and py_chk.dat_state == 7 and py_chk.errors == ()

    # 参考脚本 --check 必须输出 J7
    py_file = tmp_path / f"{name}_py.jpg"
    py_file.write_bytes(py_res.data)
    if _REF_SCRIPT.is_file():
        line = _run_ref_check_cli(py_file)
        assert ": J7 ok" in line

    # EXIF 方向验证：o6 白在上，o8 白在下
    if name in ("portrait_o6", "portrait_o8"):
        dec = np.asarray(Image.open(io.BytesIO(py_res.data)).convert("RGB"))
        top = float(dec[:120].mean())
        bottom = float(dec[-120:].mean())
        if name == "portrait_o6":
            assert top > 200 and bottom < 50
        else:
            assert bottom > 200 and top < 50

    # Rust CLI 对拍（需编好 gxr-cli）
    require_cli()
    npy_path = tmp_path / f"{name}.npy"
    rs_file = tmp_path / f"{name}_rs.jpg"
    np.save(npy_path, np.ascontiguousarray(rgb, dtype=np.uint8))
    code, rs_meta = run_json(["off-image-make", "--in", str(npy_path), "--out", str(rs_file)])
    assert code == 0
    rs_bytes = rs_file.read_bytes()
    assert len(rs_bytes) <= off_image.MAX_BYTES
    assert rs_meta["quality"] == py_res.quality
    assert rs_meta["bytes"] == len(rs_bytes)
    chk_code, rs_chk = run_json(["off-image-check", "--in", str(rs_file)])
    assert chk_code == 0
    assert rs_chk["ok"] is True and rs_chk["dat_state"] == 7 and rs_chk["errors"] == []
    if _REF_SCRIPT.is_file():
        line_rs = _run_ref_check_cli(rs_file)
        assert ": J7 ok" in line_rs

    # 桌面与手机核心逐字节一致
    assert rs_bytes == py_res.data

    # WASM 对拍
    if shutil.which("node") is not None and (PKG.is_file() or os.environ.get("GXR_REQUIRE_CORE") == "1"):
        wasm_bytes, wasm_out = _wasm_off_image(rgb, tmp_path)
        assert wasm_bytes == py_res.data
        assert wasm_out["meta"]["quality"] == py_res.quality
        assert wasm_out["check"]["ok"] is True and wasm_out["check"]["dat_state"] == 7


def _broken_variants(valid: bytes) -> list[tuple[str, bytes, set[str]]]:
    """构造 9 类验收违规文件 + 熵数据翻位。"""
    # 拆出原始段
    segs = _segments_of(valid)
    dqt_body = segs[0][1]
    sof0_body = segs[1][1]
    dht_body = segs[2][1]
    dri_body = segs[3][1]
    sos_body = segs[4][1]
    scan = valid[595:]

    def seg(marker: int, body: bytes) -> bytes:
        return bytes([0xFF, marker]) + struct.pack(">H", len(body) + 2) + body

    # 1. 尺寸不对（320x240）
    bad_size = valid[:141] + struct.pack(">HH", 240, 320) + valid[145:]

    # 2. 带 APP0 段
    app0 = seg(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    with_app = valid[:2] + app0 + valid[2:]

    # 3. 两个 DQT 段
    two_dqt = b"\xff\xd8" + seg(0xDB, dqt_body[:65]) + seg(0xDB, dqt_body[65:]) + valid[136:]

    # 4. DHT 顺序不对（DC0, AC0, DC1, AC1 而非 DC0, DC1, AC0, AC1）
    tables: dict[int, bytes] = {}
    rem = dht_body
    while rem:
        sz = 17 + sum(rem[1:17])
        tables[rem[0]] = rem[:sz]
        rem = rem[sz:]
    wrong_dht_body = tables[0x00] + tables[0x10] + tables[0x01] + tables[0x11]
    wrong_dht = (
        b"\xff\xd8" + seg(0xDB, dqt_body) + seg(0xC0, sof0_body) + seg(0xC4, wrong_dht_body) + seg(0xDD, dri_body) + seg(0xDA, sos_body) + scan
    )

    # 5. 没有 DRI
    no_dri = b"\xff\xd8" + seg(0xDB, dqt_body) + seg(0xC0, sof0_body) + seg(0xC4, dht_body) + seg(0xDA, sos_body) + scan

    # 6. 缺少一个 RST 标记
    idx_rst = scan.rfind(b"\xff\xd2")
    assert idx_rst >= 0
    missing_rst = valid[:595] + scan[:idx_rst] + scan[idx_rst + 2 :]

    # 7. EOI 后面有多余字节
    extra_after_eoi = valid + b"\x00\x01"

    # 8. 超过 32 KB
    over_32k = valid[:-2] + b"\x00" * (32768 - len(valid) + 1) + b"\xff\xd9"

    # 9. 4:2:0 色度采样（Y=2x2 0x22）
    sub_420 = valid[:147] + b"\x22" + valid[148:]

    # 10. 熵流翻位（J8）
    k = 700
    while valid[k] in (0xFF, 0x00) or valid[k - 1] == 0xFF or valid[k + 1] == 0xFF or (valid[k] ^ 0x5A) in (0, 0xFF):
        k += 1
    flipped = valid[:k] + bytes([valid[k] ^ 0x5A]) + valid[k + 1 :]

    return [
        ("wrong_size", bad_size, {"bad_dimensions"}),
        ("app_segment", with_app, {"forbidden_segment"}),
        ("two_dqt", two_dqt, {"multiple_dqt"}),
        ("wrong_dht_order", wrong_dht, {"bad_dht_order"}),
        ("no_dri", no_dri, {"missing_dri"}),
        ("missing_rst", missing_rst, {"missing_rst", "bad_rst_order"}),
        ("extra_after_eoi", extra_after_eoi, {"trailing_bytes"}),
        ("over_32kb", over_32k, {"over_max_bytes"}),
        ("subsampling_420", sub_420, {"bad_subsampling"}),
        ("entropy_flip", flipped, {"bad_scan_data"}),
    ]


def test_checker_rejects_every_broken_rule_python_and_rust(tmp_path: Path) -> None:
    from gxr_studio import off_image

    ey, ex = np.mgrid[0:480, 0:640]
    base_rgb = np.stack([(ex * 3) & 255, (ey * 5) & 255, ((ex ^ ey) * 2) & 255], axis=-1).astype(np.uint8)
    valid = off_image.encode_off_image_rgb(base_rgb).data
    assert off_image.check_off_image(valid).ok

    require_cli()
    ref_mod = _load_ref_tool() if _REF_SCRIPT.is_file() else None
    has_wasm = shutil.which("node") is not None and (PKG.is_file() or os.environ.get("GXR_REQUIRE_CORE") == "1")

    for label, broken, expected_codes in _broken_variants(valid):
        py_chk = off_image.check_off_image(broken)
        assert not py_chk.ok, f"Python checker unexpectedly accepted {label}"
        py_codes = {issue.code for issue in py_chk.errors}
        assert py_codes & expected_codes, (
            f"{label}: Python codes {py_codes} missing {expected_codes}; msgs={[i.message for i in py_chk.errors]}"
        )
        assert all(issue.message.strip() for issue in py_chk.errors)

        p = tmp_path / f"broken_{label}.jpg"
        p.write_bytes(broken)
        rs_code, rs_chk = run_json(["off-image-check", "--in", str(p)])
        assert rs_code == 0
        assert rs_chk["ok"] is False, f"Rust checker unexpectedly accepted {label}"
        rs_codes = {e["code"] for e in rs_chk["errors"]}
        assert rs_codes & expected_codes, f"{label}: Rust codes {rs_codes} missing {expected_codes}"
        assert rs_chk["dat_state"] == py_chk.dat_state

        if has_wasm:
            w_chk = _wasm_check(broken, tmp_path)
            assert w_chk["ok"] is False and w_chk["dat_state"] == py_chk.dat_state
            assert {e["code"] for e in w_chk["errors"]} == rs_codes

        if ref_mod is not None:
            assert ref_mod.check_bytes(broken), f"reference tool accepted {label}"
            assert ref_mod.dat_state(broken) == py_chk.dat_state


def test_field_reference_comparison(tmp_path: Path) -> None:
    if not _FIELD_REF.is_file():
        pytest.skip("private field reference GXROFF.JPG not present")
    from gxr_studio import off_image

    ref_bytes = _FIELD_REF.read_bytes()
    py_chk = off_image.check_off_image(ref_bytes)
    assert py_chk.ok and py_chk.dat_state == 7

    require_cli()
    chk_code, rs_chk = run_json(["off-image-check", "--in", str(_FIELD_REF)])
    assert chk_code == 0
    assert rs_chk["ok"] is True and rs_chk["dat_state"] == 7

    not_for_card = _FIELD_DIR / "not-for-card"
    srcs = sorted(not_for_card.glob("*.jpg")) + sorted(not_for_card.glob("*.JPG"))
    if srcs:
        raw = srcs[0].read_bytes()
        rgb = off_image.prepare_image_bytes(raw)
        py_res = off_image.encode_off_image_rgb(rgb)
        assert py_res.data == ref_bytes
        assert py_res.quality == 92 and py_res.bytes == len(ref_bytes)

        npy_path = tmp_path / "field.npy"
        rs_out = tmp_path / "field_rs.jpg"
        np.save(npy_path, np.ascontiguousarray(rgb, dtype=np.uint8))
        mk_code, _ = run_json(["off-image-make", "--in", str(npy_path), "--out", str(rs_out)])
        assert mk_code == 0
        assert rs_out.read_bytes() == ref_bytes


def test_save_off_image_refuses_mounted_card_and_failed_check(tmp_path: Path) -> None:
    from gxr_studio import off_image
    from gxr_studio.export import ExportError

    ey, ex = np.mgrid[0:480, 0:640]
    rgb = np.stack([(ex * 2) & 255, (ey * 2) & 255, np.full_like(ex, 80)], axis=-1).astype(np.uint8)
    valid = off_image.encode_off_image_rgb(rgb).data

    fake_card = tmp_path / "volumes" / "RICOHDCX"
    fake_card.mkdir(parents=True)
    with pytest.raises(ExportError) as exc_info:
        off_image.save_off_image(valid, fake_card, [tmp_path / "volumes", fake_card])
    assert exc_info.value.code == "bad_destination"
    assert not (fake_card / "GXROFF.JPG").exists()

    good_dir = tmp_path / "exports"
    with pytest.raises(ExportError) as exc_bad:
        off_image.save_off_image(valid + b"\x00", good_dir, [tmp_path / "volumes"])
    assert exc_bad.value.code == "off_image_check_failed"
    assert not (good_dir / "GXROFF.JPG").exists()

    saved = off_image.save_off_image(valid, good_dir, [tmp_path / "volumes"])
    assert saved.name == "GXROFF.JPG" and saved.read_bytes() == valid
