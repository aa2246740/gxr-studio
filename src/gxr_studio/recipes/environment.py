"""模型、映射、原厂04及控制表的可移植快照。只执行当前支持的契约。"""

from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from ..camdata import calib, store
from ..camdata.unit_model import UnitModel
from ..engine import knobs, menu
from ..privacy import PrivateDir
from ..render.params import UNITS, RecipeSel
from ..render.recipes import CameraData
from .types import RecipeError, canonical, digest

CONTRACT = "gxr-studio/full-render/1"
MODEL = "native-C040-v2.0"


@dataclass(frozen=True)
class RenderEnvironment:
    data: bytes

    @property
    def identity(self) -> str:
        return digest(self.data)

    def document(self) -> dict[str, Any]:
        doc: dict[str, Any] = json.loads(self.data)
        if doc.get("contract") != CONTRACT or doc.get("model") != MODEL or doc.get("unit") not in UNITS:
            raise RecipeError("unsupported_binding", "这份配方的渲染契约当前不可用，请重新生成。")
        return doc

    @property
    def unit(self) -> str:
        return str(self.document()["unit"])

    @property
    def stock(self) -> bytes:
        return base64.b64decode(self.document()["stock04"], validate=True)

    def model_for(self, iso: int) -> tuple[UnitModel, int, bool]:
        doc = self.document()
        branch = 1600 if iso >= 566 else 200
        pair = doc["models"][str(branch)]
        context = base64.b64decode(pair["context"], validate=True)
        with np.load(io.BytesIO(context), allow_pickle=False) as arrays:
            d0, d1, coefficients = (arrays[k].copy() for k in ("d0", "d1", "input_coefficients"))
        if d0.shape != (24,) or d1.shape != (24,) or not all(np.isfinite(a).all() for a in (d0, d1, coefficients)):
            raise RecipeError("bad_binding", "完整配方的冻结模型参数无效。")
        model = UnitModel(calib.stock_words_of_entry(self.stock), d0, d1, pair["ring"], self.stock, coefficients)
        return model, branch, iso in calib.UNIT_ISOS[doc["unit"]]

    def controls(self, slot: int) -> list[dict[str, Any]]:
        doc = self.document()
        data, unit = doc["menu"], UNITS.index(doc["unit"])
        recipe = next(r for r in data["recipes"] if (r["unit"], r["slot"]) == (unit, slot))
        return [
            {
                "name": data["controls"][i["control"]]["name"],
                "label": data["controls"][i["control"]]["label"],
                "default": i["default_level"],
                "min": i["min_level"],
                "max": i["max_level"],
                "levels": [p["level"] for p in i["points"]],
                "cap_lower": bool(i["cap_lower"]),
                "cap_upper": bool(i["cap_upper"]),
                "amount_step": i["amount_step"] or None,
            }
            for i in data["items"][recipe["first_item"] : recipe["first_item"] + recipe["item_count"]]
        ]

    def apply(self, entry: bytes, sel: RecipeSel) -> tuple[bytes, dict[str, int]]:
        doc = self.document()
        if sel.unit != doc["unit"]:
            raise RecipeError("unit_mismatch", "这份完整配方只适用于它绑定的镜头单元。")
        controls = self.controls(sel.slot)
        given = sel.levels_dict()
        names = {c["name"] for c in controls}
        if set(given) - names:
            raise RecipeError("knob_not_available", "这份完整配方没有该旋钮。")
        levels = {c["name"]: given.get(c["name"], c["default"]) for c in controls}
        try:
            request = menu.request(doc["menu"], UNITS.index(sel.unit), sel.slot, list(levels.values()))
        except ValueError as exc:
            raise RecipeError("level_not_allowed", "这份完整配方不支持该档位。") from exc
        # 默认控件是被评记录本身，避免旋钮核心的整数量化改变canonical entry。
        if all(levels[c["name"]] == c["default"] for c in controls):
            return entry, levels
        return knobs.apply(self.stock, entry, request)[0], levels

    def validate(self) -> None:
        self.document()
        if len(self.stock) != 552:
            raise RecipeError("bad_binding", "完整配方缺少原厂04快照。")
        from .record import check_record

        check_record(self.stock, full=False)
        for iso in (200, 1600):
            self.model_for(iso)

    @classmethod
    def capture(cls, home: Path, camera: CameraData, unit: str) -> RenderEnvironment:
        if unit not in UNITS:
            raise RecipeError("bad_unit", "镜头单元无法识别。")
        # model_for先核两分支成员；随后重新核快照成员和manifest，捕获期间变化就拒绝。
        calib.model_for(home, 200, unit=unit)
        with PrivateDir(home, "完整配方依赖") as directory:
            raw_manifest = directory.read("calib/manifest.json")
            manifest = json.loads(raw_manifest)
            meta = manifest["units"][unit]
            models = {}
            for iso in (200, 1600):
                pair = meta["models"][str(iso)]
                blobs = {key: directory.read(f"calib/{name}") for key, name in pair.items()}
                if any(digest(blobs[k]) != manifest["files"][pair[k]]["sha256"] for k in pair):
                    raise RecipeError("binding_changed", "校准数据在冻结期间发生变化，请重新生成。")
                models[str(iso)] = {"context": base64.b64encode(blobs["ctx"]).decode(), "ring": json.loads(blobs["ring"])}
            camera_manifest = directory.read(f"{store.camera_data_dir(home).name}/manifest.json")
            if digest(camera_manifest) != camera.manifest_sha256 or raw_manifest != directory.read("calib/manifest.json"):
                raise RecipeError("binding_changed", "相机数据在冻结期间发生变化，请重新生成。")
        result = cls(
            canonical(
                {
                    "contract": CONTRACT,
                    "model": MODEL,
                    "unit": unit,
                    "stock04": base64.b64encode(camera.stock[UNITS.index(unit)]).decode(),
                    "menu": camera.menu,
                    "models": models,
                    "sources": {"camera": camera.manifest_sha256, "calibration": digest(raw_manifest), "controls": camera.table_sha256},
                }
            )
        )
        result.validate()
        return result
