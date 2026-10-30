"""相机配方：把"单元 + 槽位 + 旋钮档位"变成 552 B 深层行（ARCHITECTURE §6）。

- 底子的 552 B 条目、原厂 04 来自 `camdata`（用户自己卡上的 DAT，在应用数据目录里）；
- 旋钮档位 → 剂量 → `knobs.apply`，全部用 E0 冻结的整数核心（`engine.menu` / `engine.knobs`），
  菜单项、档位范围、到顶标记由 final.json 驱动，不在这里硬编码：03 黑白只有 Cont/High/Shad/Fade/Filt，04 没有 Amount；
- 每次都从不可变的底子字节重算，不累加；默认档位（Amount 100、旋钮全 0）得到的就是底子本身。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ..camdata import store
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
    levels: dict  # 这个配方菜单里每个旋钮的实际档位（没给的取默认）
    stats: dict


class Menu:
    """旋钮菜单表（final.json）。不含任何理光字节，所以没导入相机数据时也能列出配方和旋钮。"""

    def __init__(self):
        self.menu = menu.load()
        self.table_sha256 = hashlib.sha256(menu.TABLE_PATH.read_bytes()).hexdigest()
        self._recipes = {(r["unit"], r["slot"]): r for r in self.menu["recipes"]}

    def _items(self, unit: int, slot: int) -> list[dict]:
        r = self._recipes[(unit, slot)]
        return self.menu["items"][r["first_item"] : r["first_item"] + r["item_count"]]

    def controls(self, unit: int, slot: int) -> list[dict]:
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

    def listing(self) -> list[dict]:
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
                    "calibration_note": None if info["calibrated"] else "未单独标定：沿用 A12-50 的模型迁移，颜色只是电脑预测",
                }
            )
        return units

    def request_for(self, sel: RecipeSel):
        """档位 → (菜单旋钮名, 实际档位列表, knobs.Request)。档位不合法就抛 EditError，不钳位。"""
        u, slot = sel.unit_index, sel.slot
        items = self._items(u, slot)
        names = [self.menu["controls"][it["control"]]["name"] for it in items]
        given = sel.levels_dict()
        for name, value in given.items():
            if name not in names and value != NEUTRAL.get(name, 0):
                raise EditError("knob_not_available", f"配方 {slot:02d} 没有「{name}」这个旋钮")
        levels = [given.get(n, it["default_level"]) for n, it in zip(names, items)]
        try:
            return names, levels, menu.request(self.menu, u, slot, levels)
        except ValueError as exc:
            bad = next((n for n, lv, it in zip(names, levels, items) if all(p["level"] != lv for p in it["points"])), None)
            raise EditError("level_not_allowed", f"旋钮「{bad}」的这个档位在配方 {slot:02d} 里不能用（超出范围或超过到顶标记）") from exc


class CameraData(Menu):
    """已导入的相机数据 + 旋钮菜单表。构造时逐文件核对 SHA-256 和条目 CRC。"""

    def __init__(self, home: Path):
        super().__init__()
        self.home = Path(home)
        self.manifest = store.verify(self.home)  # 缺失/被改动 → CamdataError
        self.manifest_sha256 = hashlib.sha256((store.camera_data_dir(self.home) / "manifest.json").read_bytes()).hexdigest()
        self.stock = [store.load_stock04(self.home, u) for u in range(3)]
        self.entries = {(u, s): store.load_entry(self.home, u, s) for u in range(3) for s in store.SLOTS}

    def entry_sha256(self, unit: int, slot: int) -> str | None:
        return hashlib.sha256(self.entries[(unit, slot)]).hexdigest()

    # ---- 解析 -------------------------------------------------------------
    def resolve(self, sel: RecipeSel) -> Resolved:
        names, levels, req = self.request_for(sel)
        u, slot = sel.unit_index, sel.slot
        entry, stats = knobs.apply(self.stock[u], self.entries[(u, slot)], req)
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
