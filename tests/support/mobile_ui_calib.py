"""合成的校准包：给「导出给 iPhone」和手机导入的测试用。

全部是生成的：数组来自 `color_fixtures.arrays`，色相环来自 `color_fixtures.ring`，
只有 `camera_stock_sha256` 跟着调用方给的原厂 04 条目走（它就是那 96 字节的 SHA-256）。
不含任何设备上做出来的校准数据。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from support.color_fixtures import arrays, ctx_npz, ring

UNIT_ISOS = {"jlaunch": [200, 1600], "qlaunch": [200], "l08firm": [200]}


def pack_files(stock04: dict[str, bytes], *, seed: int = 3) -> dict[str, bytes]:
    """{文件名: 字节}，含 `manifest.json`，布局和 `calib.import_calibration` 写出来的一样。"""
    blobs: dict[str, bytes] = {}
    units: dict[str, object] = {}
    for offset, (unit, stock) in enumerate(sorted(stock04.items())):
        models: dict[str, dict[str, str]] = {}
        for iso in (200, 1600):
            d0, d1, coefficients = arrays(4, seed + offset * 10 + (iso == 1600))
            ctx_name, ring_name = f"ctx_{unit}_{iso}.npz", f"ring_{unit}_{iso}.json"
            blobs[ctx_name] = ctx_npz(d0, d1, coefficients)
            model = ring(per_sector=iso == 200)
            model.pop("bootstrap", None)
            blobs[ring_name] = (json.dumps(model, indent=2) + "\n").encode()
            models[str(iso)] = {"ctx": ctx_name, "ring": ring_name}
        units[unit] = {
            "camera_stock_sha256": hashlib.sha256(stock[115:211]).hexdigest(),
            "models": models,
            "isos": UNIT_ISOS[unit],
            "exposure_unit": "jlaunch",
        }
    manifest = {
        "schema": "gxr-studio/calib/2",
        "units": units,
        "sources": {},
        "imported_at": "2026-11-15T09:00:00+0900",
        "files": {name: {"size": len(b), "sha256": hashlib.sha256(b).hexdigest()} for name, b in sorted(blobs.items())},
    }
    blobs["manifest.json"] = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode()
    return blobs


def write_pack(folder: Path, files: dict[str, bytes]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name, blob in files.items():
        (folder / name).write_bytes(blob)
    return folder
