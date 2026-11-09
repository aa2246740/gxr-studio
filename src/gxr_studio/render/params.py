"""编辑参数：sidecar 里的 `recipe` / `adjust` / `curve` 三块的读取与校验（ARCHITECTURE §4.4、§5.5）。

- `recipe` 是"会跟着配方进相机"的部分：单元 + 槽位（01..08）+ 旋钮档位（用户看到的档位，不是剂量）；
- `adjust` 和 `curve` 是"只在电脑里生效"的部分：曝光、色温/色调偏移、画面对比/高光/阴影、主曲线和 RGB 曲线。
  数据结构上和 `recipe.levels` 分开存，界面和导出文件里也要分开说。

这里只管数据形状和范围；旋钮档位是否合法（到顶标记、03 没有饱和…）由 `recipes.py` 按 final.json 判断。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

UNITS = ("jlaunch", "qlaunch", "l08firm")
KNOB_NAMES = ("amount", "contrast", "highlight", "shadow", "fade", "saturation", "warmth", "tint", "split", "filter")

EXPOSURE_RANGE = 5.0  # ±EV
SLIDER_RANGE = 100  # 色温/色调/画面对比/高光/阴影：-100..+100
CURVE_MAX_POINTS = 16
CURVE_CHANNELS = ("master", "r", "g", "b")
CurvePoints = tuple[tuple[float, float], ...]


class EditError(ValueError):
    def __init__(self, code: str, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


INVALID_REQUEST = "请求无效。请刷新页面后重试。"


@dataclass(frozen=True)
class RecipeSel:
    unit: str  # jlaunch | qlaunch | l08firm
    slot: int  # 1..8
    levels: tuple[tuple[str, int], ...] = ()  # 用户给出的旋钮档位；没给的取菜单默认值
    kind: str = "builtin"  # builtin | record | look | camera_user
    id: str | None = None  # 文件内条目的内容摘要，换文件后不会误选同一序号

    @property
    def unit_index(self) -> int:
        return UNITS.index(self.unit)

    def levels_dict(self) -> dict[str, int]:
        return dict(self.levels)

    def to_json(self) -> dict[Any, Any]:
        return {"kind": self.kind, "unit": self.unit, "slot": self.slot, "levels": dict(self.levels), **({"id": self.id} if self.id else {})}


@dataclass(frozen=True)
class Adjust:
    exposure_ev: float = 0.0
    warm: int = 0
    tint: int = 0
    contrast: int = 0
    highlight: int = 0
    shadow: int = 0
    black_point: int = 0
    saturation: int = 0
    vibrance: int = 0
    vignette: int = 0

    @property
    def is_identity(self) -> bool:
        return self == Adjust()

    @property
    def tone_is_identity(self) -> bool:
        return self.contrast == 0 and self.highlight == 0 and self.shadow == 0

    def to_json(self) -> dict[Any, Any]:
        out: dict[Any, Any] = {
            "exposure_ev": self.exposure_ev,
            "wb": {"warm": self.warm, "tint": self.tint},
            "contrast": self.contrast,
            "highlight": self.highlight,
            "shadow": self.shadow,
        }
        for key in ("black_point", "saturation", "vibrance", "vignette"):
            value = getattr(self, key)
            if value:
                out[key] = value
        return out


@dataclass(frozen=True)
class Curves:
    master: CurvePoints | None = None
    r: CurvePoints | None = None
    g: CurvePoints | None = None
    b: CurvePoints | None = None

    def to_json(self) -> dict[str, list[list[float]]]:
        # 省略恒等通道，旧的 {"master": [...]} 保持原样。
        return {key: [list(p) for p in points] for key in CURVE_CHANNELS if (points := getattr(self, key)) is not None}


@dataclass(frozen=True)
class EditState:
    recipe: RecipeSel | None = None
    adjust: Adjust = field(default_factory=Adjust)
    curve: Curves | None = None  # 四条曲线的控制点（0..255）；恒等通道为 None
    geometry: Any = None
    frame: Any = None
    stamp: Any = None
    lut: str | None = None

    def to_json(self) -> dict[Any, Any]:
        return {
            "recipe": self.recipe.to_json() if self.recipe else None,
            "adjust": self.adjust.to_json(),
            "curve": self.curve.to_json() if self.curve else None,
            **({"lut": self.lut} if self.lut is not None else {}),
        }

    def canonical(self) -> str:
        """缓存键用的规范化文本（顺序固定、数字格式固定）。"""
        return json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()


def _int_in(value: Any, lo: int, hi: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value != int(value):
        raise EditError("bad_adjust", INVALID_REQUEST, f"无法保存调整。请输入整数。{what}")
    number = int(value)
    if not lo <= number <= hi:
        raise EditError("bad_adjust", INVALID_REQUEST, f"无法保存调整。数值超出范围。{what} {lo} 到 {hi}")
    return number


def parse_adjust(raw: Any) -> Adjust:
    if raw is None:
        return Adjust()
    if not isinstance(raw, dict):
        raise EditError("bad_adjust", "请求无效。请刷新页面后重试。", "adjust 要是一个对象")
    unknown = set(raw) - {"exposure_ev", "wb", "contrast", "highlight", "shadow", "black_point", "saturation", "vibrance", "vignette"}
    if unknown:
        raise EditError("bad_adjust", INVALID_REQUEST, "有无法识别的调整项。")
    ev = raw.get("exposure_ev", 0.0)
    if isinstance(ev, bool) or not isinstance(ev, (int, float)) or not math.isfinite(ev) or abs(ev) > EXPOSURE_RANGE:
        raise EditError("bad_adjust", INVALID_REQUEST, f"曝光要在 ±{EXPOSURE_RANGE:g} 挡以内。")
    wb = raw.get("wb") or {}
    if not isinstance(wb, dict) or set(wb) - {"warm", "tint"}:
        raise EditError("bad_adjust", "请求无效。请刷新页面后重试。", "白平衡只能有色温和色调")
    return Adjust(
        exposure_ev=round(float(ev), 4),
        warm=_int_in(wb.get("warm", 0), -SLIDER_RANGE, SLIDER_RANGE, "色温偏移"),
        tint=_int_in(wb.get("tint", 0), -SLIDER_RANGE, SLIDER_RANGE, "色调偏移"),
        contrast=_int_in(raw.get("contrast", 0), -SLIDER_RANGE, SLIDER_RANGE, "对比度"),
        highlight=_int_in(raw.get("highlight", 0), -SLIDER_RANGE, SLIDER_RANGE, "高光"),
        shadow=_int_in(raw.get("shadow", 0), -SLIDER_RANGE, SLIDER_RANGE, "阴影"),
        black_point=_int_in(raw.get("black_point", 0), -SLIDER_RANGE, SLIDER_RANGE, "黑点"),
        saturation=_int_in(raw.get("saturation", 0), -SLIDER_RANGE, SLIDER_RANGE, "饱和度"),
        vibrance=_int_in(raw.get("vibrance", 0), -SLIDER_RANGE, SLIDER_RANGE, "自然饱和度"),
        vignette=_int_in(raw.get("vignette", 0), -SLIDER_RANGE, SLIDER_RANGE, "暗角"),
    )


def parse_curve(raw: Any) -> Curves | None:
    """四个通道独立校验；缺省、null 和恒等直线不参与渲染。"""
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - set(CURVE_CHANNELS):
        raise EditError("bad_curve", "曲线只能包含主曲线、红、绿、蓝四个通道。")
    curves = Curves(**{key: _parse_points(raw.get(key)) for key in CURVE_CHANNELS})
    return curves if curves.to_json() else None


def _parse_points(pts: Any) -> CurvePoints | None:
    if pts is None:
        return None
    if not isinstance(pts, (list, tuple)) or not 2 <= len(pts) <= CURVE_MAX_POINTS:
        raise EditError("bad_curve", f"每条曲线要有 2 到 {CURVE_MAX_POINTS} 个点。")
    out = []
    for p in pts:
        if (
            not isinstance(p, (list, tuple))
            or len(p) != 2
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 255 for v in p)
        ):
            raise EditError("bad_curve", "曲线上的点须包含两个 0 到 255 之间的数值。")
        out.append((round(float(p[0]), 4), round(float(p[1]), 4)))
    if any(b[0] <= a[0] for a, b in zip(out, out[1:])):
        raise EditError("bad_curve", "曲线上的点须按从左到右排列，横坐标不能相同。")
    if all(x == y for x, y in out):
        return None
    return tuple(out)


def parse_recipe(raw: Any) -> RecipeSel | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise EditError("bad_recipe", "请求无效。请刷新页面后重试。", "recipe 要是一个对象")
    kind = raw.get("kind", "builtin")
    if kind not in ("builtin", "record", "look", "camera_user"):
        raise EditError("recipe_kind_unsupported", INVALID_REQUEST, "配方类型无法识别。")
    identity = raw.get("id") if kind != "builtin" else None
    if kind != "builtin" and (not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity)):
        raise EditError("bad_recipe", INVALID_REQUEST, "配方条目无法识别。")
    unit, slot = raw.get("unit"), raw.get("slot")
    if unit not in UNITS:
        raise EditError("bad_recipe", INVALID_REQUEST, "镜头单元无法识别。")
    if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= 8:
        raise EditError("bad_recipe", INVALID_REQUEST, "槽位要在 1 到 8。")
    levels = raw.get("levels") or {}
    if not isinstance(levels, dict):
        raise EditError("bad_recipe", "请求无效。请刷新页面后重试。", "levels 格式不对")
    pairs = []
    for name in sorted(levels):
        if name not in KNOB_NAMES:
            raise EditError("bad_recipe", INVALID_REQUEST, f"旋钮无法使用。{name}")
        v = levels[name]
        if isinstance(v, bool) or not isinstance(v, int):
            raise EditError("bad_recipe", INVALID_REQUEST, f"档位要是整数。{name}")
        pairs.append((name, v))
    memory = raw.get("memory")
    if memory is not None and not isinstance(memory, dict):
        raise EditError("bad_recipe", "请求无效。请刷新页面后重试。", "memory 要是对象")
    return RecipeSel(unit, slot, tuple(pairs), kind, identity)


def stored_border(side: dict[Any, Any]) -> dict[Any, Any] | None:
    """边框样式在 `border`。旧 sidecar 把样式写进 `frame` 时，带 `style` 的那份也认。尺寸帧没有 style。"""
    for key in ("border", "frame"):
        raw = side.get(key)
        if isinstance(raw, dict) and isinstance(raw.get("style"), str):
            return raw
    return None


def parse_edit(side: dict[Any, Any]) -> EditState:
    """从 sidecar 读出编辑状态；其中任何一块有问题都当作 EditError（让调用方决定是报错还是忽略）。"""
    from ..finish.spec import parse_frame, parse_geometry, parse_stamp

    raw_geo = side.get("geometry")
    geometry = parse_geometry(raw_geo) if raw_geo else None
    angle = (side.get("crop") or {}).get("angle_deg")
    if isinstance(angle, (int, float)) and not isinstance(angle, bool) and abs(float(angle)) > 1e-9:
        from dataclasses import replace

        if geometry is None:
            geometry = parse_geometry({"straighten": float(angle)})
        elif abs(geometry.straighten) < 1e-9:
            geometry = replace(geometry, straighten=float(angle))
    raw_border = stored_border(side)
    return EditState(
        recipe=parse_recipe(side.get("recipe")),
        adjust=parse_adjust(side.get("adjust")),
        curve=parse_curve(side.get("curve")),
        geometry=geometry,
        frame=parse_frame(raw_border) if raw_border is not None else None,
        stamp=parse_stamp(side["stamp"]) if "stamp" in side else None,
        lut=parse_lut(side.get("lut")),
    )


def parse_lut(raw: Any) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9a-f]{64}", raw):
        raise EditError("bad_lut", INVALID_REQUEST)
    return raw
