"""逐单元标定包。理光派生量只保存在本机；stock 运行时取各单元自己的原厂 04。"""

from __future__ import annotations

import hashlib
import io
import json
import time
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from . import store
from .unit_model import UnitModel

SCHEMA = "gxr-studio/calib/2"
CALIBRATED_ISOS = (200, 1600)
UNIT_ISOS = {"jlaunch": CALIBRATED_ISOS, "qlaunch": (200,), "l08firm": (200,)}
MODEL_ISOS = (200, 1600)
Source = Path | tuple[str, bytes]
Sources = dict[str, dict[int, tuple[Source, Source]]]


class CalibError(ValueError):
    pass


class StockMismatch(CalibError):
    """标定包与对应单元的原厂 04 色表行不同。"""


def calib_dir(home: Path) -> Path:
    return Path(home) / "calib"


def dev_sources(repo_root: Path) -> Sources:
    from .dev_calib import sources

    return sources(repo_root)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _load(src: Source) -> tuple[str, bytes]:
    return src if isinstance(src, tuple) else (src.name, src.read_bytes())


def stock_words_of_entry(entry: bytes) -> np.ndarray:
    from ..engine import colour

    row = entry[115:211]
    return np.array([colour.decode_word(row[4 * s : 4 * s + 4]) for s in range(24)], dtype=np.int64)


def stock_fingerprint(entry: bytes) -> str:
    return _sha(entry[115:211])


def _context(data: bytes, stock: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    with np.load(io.BytesIO(data), allow_pickle=False) as z:
        if not np.array_equal(z["stock"], stock):
            raise StockMismatch("校准数据和对应单元原厂 04 的色表行不一致")
        d0, d1, coefficients = (z[k].astype(float) for k in ("d0", "d1", "input_coefficients"))
        if d0.shape != (24,) or d1.shape != (24,) or coefficients.shape not in ((4, 3), (10, 3)):
            raise CalibError("校准数据的数组尺寸不正确")
        if not all(np.all(np.isfinite(a)) for a in (d0, d1, coefficients)):
            raise CalibError("校准数据含非有限数")
        s_eff = ((stock[:, 0] + d0) - 1j * (stock[:, 1] + d1)) / 32
        if not np.array_equal(s_eff, z["S_eff"]):
            raise CalibError("S_eff 不能由 stock/d0/d1 逐位还原")
    return d0, d1, coefficients


def import_calibration(home: Path, sources: Sources, *, origin: dict[Any, Any] | None = None, forbidden: Iterable[Path] = ()) -> dict[str, Any]:
    """先核对所有来源，再原子替换整个包。可只导入部分单元，不迁移旧格式。"""
    if not sources or not set(sources) <= set(UNIT_ISOS):
        raise CalibError("校准数据的单元不正确")
    manifest: dict[str, Any] = {"schema": SCHEMA, "units": {}, "sources": {}, "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    if origin:
        manifest["origin"] = origin
    files: dict[str, bytes] = {}
    for unit, isos in sorted(sources.items()):
        if tuple(sorted(isos)) != MODEL_ISOS:
            raise CalibError("每个单元需要 ISO 200 / 1600 两个模型分支；实拍校准范围另行标注")
        stock04 = store.load_stock04(home, unit)
        meta: dict[str, Any] = {
            "camera_stock_sha256": stock_fingerprint(stock04),
            "models": {},
            "isos": list(UNIT_ISOS[unit]),
            "exposure_unit": "jlaunch",
        }
        for iso, (npz_src, ring_src) in sorted(isos.items()):
            npz_name, npz_bytes = _load(npz_src)
            ring_name, ring_bytes = _load(ring_src)
            d0, d1, coefficients = _context(npz_bytes, stock_words_of_entry(stock04))
            ring = json.loads(ring_bytes)
            if not isinstance(ring, dict) or ring.get("schema") != "gxr-sector-model/2":
                raise CalibError("需要 gxr-sector-model/2")
            UnitModel(stock_words_of_entry(stock04), d0, d1, ring, stock04, coefficients)
            ctx = io.BytesIO()
            np.savez(ctx, d0=d0, d1=d1, input_coefficients=coefficients)
            ctx_name, ring_dest = f"ctx_{unit}_{iso}.npz", f"ring_{unit}_{iso}.json"
            files[ctx_name], files[ring_dest] = ctx.getvalue(), ring_bytes
            meta["models"][str(iso)] = {"ctx": ctx_name, "ring": ring_dest}
            manifest["sources"][f"{unit}/{iso}"] = {
                "ctx": npz_name,
                "ctx_source_sha256": _sha(npz_bytes),
                "ring": ring_name,
                "ring_source_sha256": _sha(ring_bytes),
            }
        manifest["units"][unit] = meta
    manifest["files"] = {name: {"size": len(b), "sha256": _sha(b)} for name, b in sorted(files.items())}
    files["manifest.json"] = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode()
    store.write_tree(home, calib_dir(home).name, files, manifest["files"], "calibration", forbidden)
    return manifest


def model_for(home: Path, iso: int = 200, *, unit: str = "jlaunch") -> tuple[UnitModel, dict[str, Any]]:
    d = calib_dir(home)
    m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(m, dict) or m.get("schema") != SCHEMA:
        raise CalibError("校准数据版本已更新，请重新导入")
    if unit not in UNIT_ISOS or unit not in m["units"]:
        raise CalibError("缺少这个单元的校准数据")
    meta = m["units"][unit]
    stock04 = store.load_stock04(home, unit)
    if meta["camera_stock_sha256"] != stock_fingerprint(stock04):
        raise StockMismatch("校准数据与这个单元的原厂 04 不匹配，请重新导入")
    blobs = {}
    for branch in MODEL_ISOS:
        for name in meta["models"][str(branch)].values():
            if not isinstance(name, str) or Path(name).name != name or name not in m["files"]:
                raise CalibError("校准数据的文件清单不正确")
            blob = (d / name).read_bytes()
            if _sha(blob) != m["files"][name]["sha256"]:
                raise CalibError("校准数据被改动，请重新导入")
            blobs[name] = blob
    model_iso = 1600 if int(iso) >= 566 else 200
    pair = meta["models"][str(model_iso)]
    with np.load(io.BytesIO(blobs[pair["ctx"]]), allow_pickle=False) as z:
        d0, d1, coefficients = (z[k].copy() for k in ("d0", "d1", "input_coefficients"))
    model = UnitModel(stock_words_of_entry(stock04), d0, d1, json.loads(blobs[pair["ring"]]), stock04, coefficients)
    return model, {"iso": int(iso), "calibrated": int(iso) in UNIT_ISOS[unit], "model_iso": model_iso}


def unit_status(home: Path, unit: str) -> dict[str, Any]:
    state: str = "ready"
    message: str | None = None
    try:
        model_for(home, 200, unit=unit)
    except FileNotFoundError:
        state = "missing" if not (calib_dir(home) / "manifest.json").exists() else "invalid"
        message = "缺少校准数据" if state == "missing" else "校准数据文件缺失，请重新导入"
    except CalibError as exc:
        state, message = ("missing" if "缺少" in str(exc) else "invalid"), str(exc)
    except Exception:  # noqa: BLE001  文件格式、解压或模型参数损坏都归为校准失效
        state, message = "invalid", "校准数据读不出来，请重新导入"
    return {
        "state": state,
        "message": message,
        "isos": list(UNIT_ISOS[unit]) if state == "ready" else [],
        "exposure_note": "曝光沿用 50mm" if unit != "jlaunch" else None,
    }
