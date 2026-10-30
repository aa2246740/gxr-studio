"""标定上下文（电脑预测要用）：放进应用数据目录 `calib/`，运行时和相机数据合成渲染模型。

研究里的 `data_<iso>.npz` 含理光的原厂扇区字 `stock`；这里**拆开**：

- 只存 `d0`、`d1`（五色修饰量，来自固件参数，视为理光派生，本地数据）和 `ring_model_v2*.json`（我们的实测拟合）；
- `stock` 在运行时取自用户卡上原厂 04（jlaunch）的色表行，不另存；`S_eff` 由两者算出（与 npz 里的逐位相同）。

导入时用 npz 里的 `stock` 核对卡上的 04（取代研究里 `Engine` 对 DEEPTBL0.DAT 的核对），不一致就拒绝。
只有 A12-50 的 ISO 200 / 1600 两套实拍标定；其它 ISO 用最近的一套并标注"未标定"。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

import numpy as np

from . import store

SCHEMA = "gxr-studio/calib/1"
CALIBRATED_ISOS = (200, 1600)


class CalibError(ValueError):
    pass


class StockMismatch(CalibError):
    """标定包里的 stock 与已导入相机数据里 jlaunch 的原厂 04 色表行对不上。"""


def calib_dir(home: Path) -> Path:
    return Path(home) / "calib"


def dev_sources(repo_root: Path) -> dict[Any, Any]:
    """研究目录里的标定文件位置（只在开发机上用于第一次导入）。"""
    r = Path(repo_root) / "research"
    return {
        200: (
            r / "color_fit_v2/06_classic_neg/round4/local/cache/data_200.npz",
            r / "hue_ring/local/ring_model_v2.json",
        ),
        1600: (
            r / "color_fit_v2/06_classic_neg/round4/local/cache/data_1600.npz",
            r / "hue_ring/local/ring_model_v2_iso1600.json",
        ),
    }


def _sha_file(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def stock_words_of_entry(entry: bytes) -> np.ndarray:
    """原厂 04 条目的 24 个扇区字 → (24, 2) 整数（`colour.decode_word`）。"""
    from ..engine import colour

    row = entry[115:211]
    return np.array([colour.decode_word(row[4 * s : 4 * s + 4]) for s in range(24)], dtype=np.int64)


def stock_fingerprint(entry: bytes) -> str:
    """原厂 04 的色表行（标定要核对的那 96 B）的 SHA-256；标定清单里记一份，相机数据换了能发现。"""
    return hashlib.sha256(entry[115:211]).hexdigest()


def import_calibration(home: Path, sources: dict[Any, Any], *, origin: dict[Any, Any] | None = None) -> dict[Any, Any]:
    """sources: {iso: (npz_path, ring_json_path)}。需要先导入相机数据（用来核对 stock）。"""
    stock04 = store.load_stock04(home, "jlaunch")
    stock = stock_words_of_entry(stock04)
    out = calib_dir(home)
    tmp = out.with_name(out.name + ".new")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    manifest: dict[Any, Any] = {
        "schema": SCHEMA,
        "files": {},
        "sources": {},
        "note": "d0/d1 为固件参数派生（理光派生，仅本地）；ring_model 为我们的拟合；stock 不存，运行时取自 04",
        "camera_stock_sha256": stock_fingerprint(stock04),
        "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if origin:
        manifest["origin"] = origin
    for iso, (npz_path, ring_path) in sorted(sources.items()):
        with np.load(npz_path, allow_pickle=False) as z:
            if not np.array_equal(z["stock"], stock):
                raise StockMismatch(f"ISO {iso} 的校准数据和存储卡上原厂 04 的色表行不一致")
            d0, d1 = z["d0"].astype(float), z["d1"].astype(float)
            s_eff = ((z["stock"][:, 0] + d0) - 1j * (z["stock"][:, 1] + d1)) / 32
            if not np.array_equal(s_eff, z["S_eff"]):
                raise CalibError(f"ISO {iso}：S_eff 不能由 stock/d0/d1 逐位还原")
        ring = json.loads(Path(ring_path).read_text())
        if ring.get("schema") != "gxr-sector-model/2":
            raise CalibError("需要 gxr-sector-model/2")
        np.savez(tmp / f"ctx_{iso}.npz", d0=d0, d1=d1)
        shutil.copyfile(ring_path, tmp / f"ring_model_{iso}.json")
        manifest["sources"][str(iso)] = {
            "ctx": Path(npz_path).name,
            "ctx_source_sha256": _sha_file(npz_path),
            "ring": Path(ring_path).name,
            "ring_source_sha256": _sha_file(ring_path),
        }
    for p in sorted(tmp.iterdir()):
        manifest["files"][p.name] = {"size": p.stat().st_size, "sha256": _sha_file(p)}
    (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    store.swap_in(tmp, out)
    return manifest


def model_for(home: Path, iso: int = 200) -> tuple[Any, ...]:
    """返回 (Model, info)。info['calibrated'] = 该 ISO 是否有实拍标定。"""
    from ..engine.model import Model

    d = calib_dir(home)
    m = json.loads((d / "manifest.json").read_text())
    for name, meta in m["files"].items():
        if _sha_file(d / name) != meta["sha256"]:
            raise CalibError("校准数据被改动")
    model_iso = 1600 if int(iso) == 1600 else 200
    with np.load(d / f"ctx_{model_iso}.npz", allow_pickle=False) as z:
        d0, d1 = z["d0"].copy(), z["d1"].copy()
    ring = json.loads((d / f"ring_model_{model_iso}.json").read_text())
    stock = stock_words_of_entry(store.load_stock04(home, "jlaunch"))
    model = Model(stock, d0, d1, ring)
    return model, {"iso": int(iso), "calibrated": int(iso) in CALIBRATED_ISOS, "model_iso": model_iso}
