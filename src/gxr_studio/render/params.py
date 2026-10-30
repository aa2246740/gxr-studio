"""编辑参数：sidecar 里的 `recipe` / `adjust` / `curve` 三块的读取与校验（ARCHITECTURE §4.4、§5.5）。

- `recipe` 是"会跟着配方进相机"的部分：单元 + 槽位（01..08）+ 旋钮档位（用户看到的档位，不是剂量）；
- `adjust` 和 `curve` 是"只在电脑里生效"的部分：曝光、色温/色调偏移、画面对比/高光/阴影、主曲线。
  数据结构上和 `recipe.levels` 分开存，界面和导出文件里也要分开说。

这里只管数据形状和范围；旋钮档位是否合法（到顶标记、03 没有饱和…）由 `recipes.py` 按 final.json 判断。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field

UNITS = ("jlaunch", "qlaunch", "l08firm")
KNOB_NAMES = ("amount", "contrast", "highlight", "shadow", "fade", "saturation", "warmth", "tint", "split", "filter")

EXPOSURE_RANGE = 5.0  # ±EV
SLIDER_RANGE = 100  # 色温/色调/画面对比/高光/阴影：-100..+100
CURVE_MAX_POINTS = 16


class EditError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RecipeSel:
    unit: str  # jlaunch | qlaunch | l08firm
    slot: int  # 1..8
    levels: tuple[tuple[str, int], ...] = ()  # 用户给出的旋钮档位；没给的取菜单默认值

    @property
    def unit_index(self) -> int:
        return UNITS.index(self.unit)

    def levels_dict(self) -> dict[str, int]:
        return dict(self.levels)

    def to_json(self) -> dict:
        return {"kind": "builtin", "unit": self.unit, "slot": self.slot, "levels": dict(self.levels)}


@dataclass(frozen=True)
class Adjust:
    exposure_ev: float = 0.0
    warm: int = 0
    tint: int = 0
    contrast: int = 0
    highlight: int = 0
    shadow: int = 0

    @property
    def is_identity(self) -> bool:
        return self == Adjust()

    @property
    def tone_is_identity(self) -> bool:
        return self.contrast == 0 and self.highlight == 0 and self.shadow == 0

    def to_json(self) -> dict:
        return {
            "exposure_ev": self.exposure_ev,
            "wb": {"warm": self.warm, "tint": self.tint},
            "contrast": self.contrast,
            "highlight": self.highlight,
            "shadow": self.shadow,
        }


@dataclass(frozen=True)
class EditState:
    recipe: RecipeSel | None = None
    adjust: Adjust = field(default_factory=Adjust)
    master: tuple[tuple[float, float], ...] | None = None  # 主曲线控制点（0..255）

    def to_json(self) -> dict:
        return {
            "recipe": self.recipe.to_json() if self.recipe else None,
            "adjust": self.adjust.to_json(),
            "curve": {"master": [list(p) for p in self.master]} if self.master else None,
        }

    def canonical(self) -> str:
        """缓存键用的规范化文本（顺序固定、数字格式固定）。"""
        return json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()


def _int_in(value, lo: int, hi: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value != int(value):
        raise EditError("bad_adjust", f"{what}要是整数")
    value = int(value)
    if not lo <= value <= hi:
        raise EditError("bad_adjust", f"{what}要在 {lo} 到 {hi} 之间")
    return value


def parse_adjust(raw) -> Adjust:
    if raw is None:
        return Adjust()
    if not isinstance(raw, dict):
        raise EditError("bad_adjust", "adjust 要是一个对象")
    unknown = set(raw) - {"exposure_ev", "wb", "contrast", "highlight", "shadow"}
    if unknown:
        raise EditError("bad_adjust", f"不认识的调整项：{sorted(unknown)[0]}")
    ev = raw.get("exposure_ev", 0.0)
    if isinstance(ev, bool) or not isinstance(ev, (int, float)) or not math.isfinite(ev) or abs(ev) > EXPOSURE_RANGE:
        raise EditError("bad_adjust", f"曝光要在 ±{EXPOSURE_RANGE:g} EV 之内")
    wb = raw.get("wb") or {}
    if not isinstance(wb, dict) or set(wb) - {"warm", "tint"}:
        raise EditError("bad_adjust", "wb 只能有 warm 和 tint")
    return Adjust(
        exposure_ev=round(float(ev), 4),
        warm=_int_in(wb.get("warm", 0), -SLIDER_RANGE, SLIDER_RANGE, "色温偏移"),
        tint=_int_in(wb.get("tint", 0), -SLIDER_RANGE, SLIDER_RANGE, "色调偏移"),
        contrast=_int_in(raw.get("contrast", 0), -SLIDER_RANGE, SLIDER_RANGE, "画面对比"),
        highlight=_int_in(raw.get("highlight", 0), -SLIDER_RANGE, SLIDER_RANGE, "高光"),
        shadow=_int_in(raw.get("shadow", 0), -SLIDER_RANGE, SLIDER_RANGE, "阴影"),
    )


def parse_curve(raw) -> tuple[tuple[float, float], ...] | None:
    """返回主曲线控制点；缺省、空、或恰好是恒等直线 → None。"""
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {"master"}:
        raise EditError("bad_curve", "curve 只支持 master（主曲线）")
    pts = raw.get("master")
    if pts is None:
        return None
    if not isinstance(pts, (list, tuple)) or not 2 <= len(pts) <= CURVE_MAX_POINTS:
        raise EditError("bad_curve", f"主曲线要有 2 到 {CURVE_MAX_POINTS} 个点")
    out = []
    for p in pts:
        if (
            not isinstance(p, (list, tuple))
            or len(p) != 2
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 255 for v in p)
        ):
            raise EditError("bad_curve", "曲线的点要是 [输入, 输出]，都在 0 到 255 之间")
        out.append((round(float(p[0]), 4), round(float(p[1]), 4)))
    if any(b[0] <= a[0] for a, b in zip(out, out[1:])):
        raise EditError("bad_curve", "曲线的输入要从小到大，不能重复")
    if all(x == y for x, y in out):
        return None
    return tuple(out)


def parse_recipe(raw) -> RecipeSel | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise EditError("bad_recipe", "recipe 要是一个对象")
    if raw.get("kind", "builtin") != "builtin":
        raise EditError("recipe_kind_unsupported", "这一版只支持相机内置配方（底子 + 旋钮）")
    unit, slot = raw.get("unit"), raw.get("slot")
    if unit not in UNITS:
        raise EditError("bad_recipe", "单元要是 jlaunch / qlaunch / l08firm 之一")
    if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= 8:
        raise EditError("bad_recipe", "槽位要是 1 到 8")
    levels = raw.get("levels") or {}
    if not isinstance(levels, dict):
        raise EditError("bad_recipe", "levels 要是 {旋钮名: 档位}")
    pairs = []
    for name in sorted(levels):
        if name not in KNOB_NAMES:
            raise EditError("bad_recipe", f"不认识的旋钮：{name}")
        v = levels[name]
        if isinstance(v, bool) or not isinstance(v, int):
            raise EditError("bad_recipe", f"旋钮 {name} 的档位要是整数")
        pairs.append((name, v))
    memory = raw.get("memory")
    if memory is not None and not isinstance(memory, dict):
        raise EditError("bad_recipe", "memory 要是对象")
    return RecipeSel(unit, slot, tuple(pairs))


def parse_edit(side: dict) -> EditState:
    """从 sidecar 读出编辑状态；其中任何一块有问题都当作 EditError（让调用方决定是报错还是忽略）。"""
    return EditState(
        recipe=parse_recipe(side.get("recipe")),
        adjust=parse_adjust(side.get("adjust")),
        master=parse_curve(side.get("curve")),
    )
