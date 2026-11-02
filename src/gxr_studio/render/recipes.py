"""相机配方：把"单元 + 槽位 + 旋钮档位"变成 552 B 深层行（ARCHITECTURE §6）。

- 底子的 552 B 条目、原厂 04 来自 `camdata`（用户自己卡上的 DAT，在应用数据目录里）；
- 旋钮档位 → 剂量 → `knobs.apply`，全部用 E0 冻结的整数核心（`engine.menu` / `engine.knobs`），
  菜单项、档位范围、到顶标记由 final.json 驱动，不在这里硬编码：03 黑白只有 Cont/High/Shad/Fade/Filt，04 没有 Amount；
- 每次都从不可变的底子字节重算，不累加；默认档位（Amount 100、旋钮全 0）得到的就是底子本身。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from ..camdata import datcodec, knob_table, store
from ..engine import knobs, menu
from .params import EditError, RecipeSel

# 单元顺序推断对应 50mm / 28mm / MOUNT（来自研究代码的用法；只有 50mm 有真机样本）
UNIT_INFO = (
    {"id": "jlaunch", "label": "50mm（A12）", "lens": "50mm", "calibrated": True},
    {"id": "qlaunch", "label": "28mm", "lens": "28mm", "calibrated": False},
    {"id": "l08firm", "label": "MOUNT", "lens": "MOUNT", "calibrated": False},
)
# GXUI 条目名里每个槽位的组名（datcodec._extension）
SLOT_SHORT = ("GRP", "GRN", "HCB", "STD", "FCC", "FCN", "LCH", "LVI")
NEUTRAL = {"amount": 100}  # 不在菜单里的旋钮，只有取这个"中性值"才算没动


@dataclass(frozen=True)
class Resolved:
    entry: bytes
    entry_sha256: str
    levels: dict[Any, Any]  # 这个配方菜单里每个旋钮的实际档位（没给的取默认）
    stats: dict[Any, Any]


class Menu:
    """旋钮菜单表（final.json）。不含任何理光字节，所以没导入相机数据时也能列出配方和旋钮。"""

    def __init__(self) -> None:
        self.menu = menu.load()
        self.table_sha256 = hashlib.sha256(menu.TABLE_PATH.read_bytes()).hexdigest()
        self._recipes = {(r["unit"], r["slot"]): r for r in self.menu["recipes"]}

    def _items(self, unit: int, slot: int) -> list[dict[Any, Any]]:
        r = self._recipes[(unit, slot)]
        # menu.load() 是 JSON，条目列表的元素类型存根里是 Any。
        return cast(list[dict[str, Any]], self.menu["items"][r["first_item"] : r["first_item"] + r["item_count"]])

    def controls(self, unit: int, slot: int) -> list[dict[Any, Any]]:
        out = []
        for it in self._items(unit, slot):
            c = self.menu["controls"][it["control"]]
            out.append(
                {
                    "name": c["name"],
                    "label": c["label"],
                    "min": it["min_level"],
                    "max": it["max_level"],
                    "default": it["default_level"],
                    "cap_lower": bool(it["cap_lower"]),
                    "cap_upper": bool(it["cap_upper"]),
                    "amount_step": it["amount_step"] or None,
                    "levels": [p["level"] for p in it["points"]],
                }
            )
        return out

    def entry_sha256(self, unit: int, slot: int) -> str | None:
        return None

    def listing(self) -> list[dict[Any, Any]]:
        units = []
        for u, info in enumerate(UNIT_INFO):
            recipes = []
            for slot in store.SLOTS:
                controls = self.controls(u, slot)
                recipes.append(
                    {
                        "slot": slot,
                        "number": f"{slot:02d}",
                        "short": SLOT_SHORT[slot - 1],
                        "is_stock": slot == 4,
                        "has_amount": any(c["name"] == "amount" for c in controls),
                        "entry_sha256": self.entry_sha256(u, slot),
                        "controls": controls,
                    }
                )
            units.append(
                {
                    **info,
                    "recipes": recipes,
                    "calibration_note": None if info["calibrated"] else "未校准",
                }
            )
        return units

    def request_for(self, sel: RecipeSel) -> tuple[list[str], list[int], knobs.Request]:
        """档位 → (菜单旋钮名, 实际档位列表, knobs.Request)。档位不合法就抛 EditError，不钳位。"""
        if sel.kind != "builtin":
            raise EditError("recipe_not_found", "找不到这个配方。请重新选择。")
        u, slot = sel.unit_index, sel.slot
        items = self._items(u, slot)
        names = [self.menu["controls"][it["control"]]["name"] for it in items]
        given = sel.levels_dict()
        for name, value in given.items():
            if name not in names and value != NEUTRAL.get(name, 0):
                raise EditError("knob_not_available", "无法保存配方。有一个旋钮无法使用。", f"{slot:02d} {name}")
        levels = [given.get(n, it["default_level"]) for n, it in zip(names, items)]
        try:
            return names, levels, menu.request(self.menu, u, slot, levels)
        except ValueError as exc:
            bad = next((n for n, lv, it in zip(names, levels, items) if all(p["level"] != lv for p in it["points"])), None)
            raise EditError("level_not_allowed", "无法保存配方。这个档位不能用。", f"{bad} {slot:02d}") from exc


class CameraData(Menu):
    """已导入的相机数据 + 旋钮菜单表。构造时逐文件核对 SHA-256 和条目 CRC。"""

    def __init__(self, home: Path) -> None:
        super().__init__()
        self.home = Path(home)
        self.manifest = store.verify(self.home)  # 缺失/被改动 → CamdataError
        self.manifest_sha256 = hashlib.sha256((store.camera_data_dir(self.home) / "manifest.json").read_bytes()).hexdigest()
        self.stock = [store.load_stock04(self.home, u) for u in range(3)]
        self.entries = {(u, s): store.load_entry(self.home, u, s) for u in range(3) for s in store.SLOTS}
        self._extras: dict[tuple[str, str, int], dict[str, Any]] = {}
        self._groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
        # 旧代数的规范化数据可独立使用。新代数须从原 DAT 读取控制表和文件内配方。
        if self.manifest["generation"] in (127, 129, 131, 135, 137, 138) and "dat-template/GXRCE.DAT" not in self.manifest["files"]:
            return
        dat = datcodec.parse(store.load_template(self.home))
        if dat.kv2_block is not None:
            self.menu = knob_table.read(dat.kv2_block, self.menu, mount_knobs_from_50mm=datcodec.PROFILES[dat.generation].mount_knobs_from_50mm)
            self._recipes = {(r["unit"], r["slot"]): r for r in self.menu["recipes"]}
            self.table_sha256 = hashlib.sha256(dat.kv2_block).hexdigest()
        for unit in range(3):
            for i, look in enumerate(dat.looks):
                if look.unit_mask >> unit & 1:
                    self._add_extra(unit, "look", i, look.name, look.base, look.entry, {})
            for i, user in enumerate(dat.users):
                defaults = dict(zip(("contrast", "highlight", "shadow", "saturation"), user["levels"][:4]))
                self._add_extra(unit, "camera_user", i, dat.user_names[i], user["base"], self.entries[(unit, user["base"])], defaults)

    def _add_extra(self, unit: int, kind: str, index: int, name: str, base: int, entry: bytes, defaults: dict[str, int]) -> None:
        identity = hashlib.sha256(f"{kind}/{index}/{name}/".encode() + entry + json.dumps(defaults, sort_keys=True).encode()).hexdigest()
        controls = [{**c, "default": defaults.get(c["name"], c["default"])} for c in self.controls(unit, base)]
        info = {
            "kind": kind,
            "id": identity,
            "name": name,
            "slot": base,
            "controls": controls,
            "entry_sha256": hashlib.sha256(entry).hexdigest(),
            "has_amount": any(c["name"] == "amount" for c in controls),
        }
        self._extras[(kind, identity, unit)] = {"info": info, "entry": entry, "defaults": defaults}
        self._groups.setdefault((unit, kind), []).append(info)

    def listing(self) -> list[dict[Any, Any]]:
        units = super().listing()
        for unit, info in enumerate(units):
            info["looks"] = self._groups.get((unit, "look"), [])
            info["users"] = self._groups.get((unit, "camera_user"), [])
        return units

    def _extra_for(self, sel: RecipeSel) -> dict[str, Any]:
        item = self._extras.get((sel.kind, sel.id or "", sel.unit_index))
        if item is None or item["info"]["slot"] != sel.slot:
            raise EditError("recipe_not_found", "找不到这个配方。请重新选择。")
        return item

    def request_for(self, sel: RecipeSel) -> tuple[list[str], list[int], knobs.Request]:
        if sel.kind == "builtin":
            return super().request_for(sel)
        extra = self._extra_for(sel)
        given = {**extra["defaults"], **sel.levels_dict()}
        return super().request_for(replace(sel, levels=tuple(given.items()), kind="builtin", id=None))

    def entry_sha256(self, unit: int, slot: int) -> str | None:
        return hashlib.sha256(self.entries[(unit, slot)]).hexdigest()

    # ---- 解析 -------------------------------------------------------------
    def resolve(self, sel: RecipeSel) -> Resolved:
        names, levels, req = self.request_for(sel)
        u, slot = sel.unit_index, sel.slot
        base = self.entries[(u, slot)] if sel.kind == "builtin" else self._extra_for(sel)["entry"]
        entry, stats = knobs.apply(self.stock[u], base, req)
        return Resolved(entry, hashlib.sha256(entry).hexdigest(), dict(zip(names, levels)), stats)

    def default_selection(self, unit: str) -> RecipeSel:
        """没选配方时的默认：该单元的原厂 04（电脑模拟）。"""
        return RecipeSel(unit, 4)


def unit_of_lens(lens: str | None, model: str | None = None) -> str | None:
    """EXIF 镜头名 → 单元 id；认不出就返回 None（调用方标"未标定"）。

    只认 A12-50 / A12-28 / MOUNT 这三种写法，与 DECISIONS 一致；28mm、MOUNT 没有真机文件核实过。
    """
    text = f"{lens or ''} {model or ''}".lower()
    if "mount" in text:
        return "l08firm"
    if "50mm" in text:
        return "jlaunch"
    if "28mm" in text:
        return "qlaunch"
    return None
