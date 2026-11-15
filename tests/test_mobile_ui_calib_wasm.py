"""UI-M2：WASM 的 `color_calib_check` 和 `gxr-cli color-calib-check --dir` 逐字节相同（成功和每种错误）。

校准包是合成的（tests/support/mobile_ui_calib.py），文件夹在临时目录里。比较驱动是 mobile/scripts/wasm_vs_native.mjs，
用例在 mobile/scripts/wasm_cases/color.mjs 的 `calib`。
"""

from __future__ import annotations

import json
from pathlib import Path

from support.color_fixtures import entry
from support.core_cli import require_cli
from support.mobile_ui_calib import pack_files, write_pack
from support.wasm_check import run_area


def _pack(folder: Path, stock: dict[str, bytes]) -> Path:
    return write_pack(folder, pack_files(stock))


def test_wasm_calib_check_matches_the_cli(tmp_path: Path) -> None:
    require_cli()
    stock = entry(words=0)
    stock_file = tmp_path / "stock.bin"
    stock_file.write_bytes(stock)
    other = tmp_path / "other.bin"
    other.write_bytes(entry(words=1))
    good = _pack(tmp_path / "good", {"jlaunch": stock})
    specs: dict[str, dict[str, object]] = {}
    for iso in (100, 200, 565, 566, 1600, 3200):
        specs[f"ok-{iso}"] = {"kind": "calib", "dir": str(good), "unit": "jlaunch", "iso": iso, "stock": str(stock_file)}
    specs["no-unit"] = {"kind": "calib", "dir": str(good), "unit": "qlaunch", "iso": 200, "stock": str(stock_file)}
    specs["unknown-unit"] = {"kind": "calib", "dir": str(good), "unit": "nounit", "iso": 200, "stock": str(stock_file)}
    specs["stock-mismatch"] = {"kind": "calib", "dir": str(good), "unit": "jlaunch", "iso": 200, "stock": str(other)}
    changed = _pack(tmp_path / "changed", {"jlaunch": stock})
    ring = changed / "ring_jlaunch_1600.json"
    ring.write_bytes(ring.read_bytes() + b" ")
    specs["changed"] = {"kind": "calib", "dir": str(changed), "unit": "jlaunch", "iso": 200, "stock": str(stock_file)}
    missing = _pack(tmp_path / "missing", {"jlaunch": stock})
    (missing / "ctx_jlaunch_200.npz").unlink()
    specs["missing-file"] = {"kind": "calib", "dir": str(missing), "unit": "jlaunch", "iso": 1600, "stock": str(stock_file)}
    old = _pack(tmp_path / "old", {"jlaunch": stock})
    manifest = json.loads((old / "manifest.json").read_text(encoding="utf-8"))
    manifest["schema"] = "gxr-studio/calib/1"
    (old / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    specs["outdated"] = {"kind": "calib", "dir": str(old), "unit": "jlaunch", "iso": 200, "stock": str(stock_file)}
    broken = _pack(tmp_path / "broken", {"jlaunch": stock})
    bad_ring = b'{"schema": "gxr-sector-model/2"}'
    (broken / "ring_jlaunch_200.json").write_bytes(bad_ring)
    manifest = json.loads((broken / "manifest.json").read_text(encoding="utf-8"))
    import hashlib

    manifest["files"]["ring_jlaunch_200.json"] = {"size": len(bad_ring), "sha256": hashlib.sha256(bad_ring).hexdigest()}
    (broken / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    specs["broken-model"] = {"kind": "calib", "dir": str(broken), "unit": "jlaunch", "iso": 200, "stock": str(stock_file)}
    inputs = []
    for name, spec in specs.items():
        path = tmp_path / f"case-calib-{name}.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        inputs.append(path)
    proc = run_area("color", inputs, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
