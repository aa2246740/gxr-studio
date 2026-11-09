"""只读研究数据的开发导入器；不导入研究代码，也不写回研究工作区。"""

from __future__ import annotations

import copy
import io
import json
from pathlib import Path
from typing import Any

import numpy as np

from . import datcodec
from .calib import MODEL_ISOS, Sources, stock_words_of_entry

UNITS = ("a12-50", "a12-28", "mount-a12")
FIRMWARE = ("jlaunch3", "qlaunch3", "l08firm3")


def sources(root: Path) -> Sources:
    """以 fit-v4-r2 的冻结输入映射和 native-C040-v2.0 为准；缺研究数据时返回空。"""
    try:
        return _sources(Path(root))
    except FileNotFoundError:
        return {}


def _sources(root: Path) -> Sources:
    research = root / "research"
    workspace = research / "fit_v4_r2/local/adapter/local_batch"

    def source(rel: str) -> Path:
        frozen = research / "recipe_redo_r1/local/source" / rel
        return frozen if frozen.is_file() else root / rel

    def read(path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    if read(workspace / "workspace.json").get("model_version") != "native-C040-v2.0":
        raise ValueError("研究工作区不是已支持的 native-C040-v2.0")
    official = read(source("research/module_adapt/local/official_parameters.json"))["units"]
    display = read(source("research/stage2e_verification/local/measurements.json"))["frames"]["R8051160"]["meta"]["display"]
    dat = datcodec.parse(source("firmware/package_v39_g154_looks18_test/local/SD_ROOT/GXRCE.DAT").read_bytes())
    native = []
    for index, key in enumerate(("50mm", "28mm", "mount")):
        data = official[key]
        firmware = (root / "artifacts/firmware/gxr-1.51" / FIRMWARE[index]).read_bytes()
        colour = data["rows"]["colour1"]
        row = bytes.fromhex(colour["hex"])
        offset = colour["file_offset"]
        if firmware[offset : offset + len(row)] != row:
            raise ValueError("C080 来源核对失败")
        c040 = firmware[offset - 26 * 48 : offset - 25 * 48]
        if len(c040) != 48 or c040[1::2] != bytes([64]) * 24:
            raise ValueError("C040 来源核对失败")
        codes = np.asarray(list(c040[::2]), float)
        if not np.all((np.diff(np.r_[codes, codes[0] + 256]) >= 2) & (np.diff(np.r_[codes, codes[0] + 256]) < 80)):
            raise ValueError("C040 节点步长不正确")
        native.append(codes)
    out: Sources = {}
    for index, (unit, key) in enumerate(zip(UNITS, ("50mm", "28mm", "mount"))):
        data = official[key]
        d0, d1 = np.zeros(24), np.zeros(24)
        for i, color in enumerate(("orange", "green", "blue", "red", "magenta")):
            selected = np.array([bool(int(data["mask"][i]) & (1 << j)) for j in range(24)])
            level = int(display[color + "_hue"]) - 1
            d0[selected] = np.asarray(data["d0_levels"])[level, selected]
            d1[selected] = np.asarray(data["d1_levels"])[level, selected]
            saturation = np.asarray(data["sat_levels"])[int(display[color + "_saturation"]) - 1]
            if not np.all(saturation == 100) and (saturation.ndim == 0 or not np.all(saturation[selected] == 100)):
                raise ValueError("校准来源不是中性饱和度")
            if data["vividness_levels"][int(display["vividness"]) - 1] != 100:
                raise ValueError("校准来源不是中性鲜艳度")
        stock = stock_words_of_entry(dat.entry_of(index, 4))
        effective = ((stock[:, 0] + d0) - 1j * (stock[:, 1] + d1)) / 32
        mapping = workspace / f"calibration/input-map-{unit}.json"
        coefficients = np.asarray(read(mapping)["coefficients"])
        out[datcodec.NAMES[index]] = {}
        for iso in MODEL_ISOS:
            ring_path = source("research/hue_ring/local/" + ("ring_model_v2.json" if iso == 200 else "ring_model_v2_iso1600.json"))
            ring = copy.deepcopy(read(ring_path))
            if ring.get("module_c040_geometry"):
                raise ValueError("应从未迁移的实测 50mm 色相环导入，避免重复叠加 C040")
            if index:
                change = (native[index] - native[0]) * 360 / 256
                for name in ("nodes_deg", "nodes_unwrapped_deg", "sector_centres_deg"):
                    ring[name] = (np.asarray(ring[name]) + change).tolist()
                ring["module_c040_geometry"] = {"unit": unit, "mode": "native"}
            ctx = io.BytesIO()
            np.savez(ctx, stock=stock, d0=d0, d1=d1, S_eff=effective, input_coefficients=coefficients)
            out[datcodec.NAMES[index]][iso] = (
                (f"{unit}_{iso}.npz", ctx.getvalue()),
                (f"{unit}_{iso}_ring.json", json.dumps(ring).encode()),
            )
    return out
