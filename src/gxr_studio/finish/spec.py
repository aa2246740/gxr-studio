"""边框、时间戳、纠正的数据形状。缺省表示用出厂默认，不表示用户选了「无」。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..render.params import INVALID_REQUEST, EditError

SLIDER = 100
STAMP_TEXT_MAX = 24


@dataclass(frozen=True)
class Geometry:
    straighten: float = 0.0
    vertical: float = 0.0
    horizontal: float = 0.0
    distortion: float = 0.0
    rotate90: int = 0
    flip: bool = False

    def is_identity(self) -> bool:
        return (
            abs(self.straighten) < 1e-9
            and self.vertical == 0
            and self.horizontal == 0
            and self.distortion == 0
            and self.rotate90 % 4 == 0
            and not self.flip
        )

    def expands(self) -> bool:
        """透视或畸变会改变画布。只有拉直时，坐标仍是转正后的原图。"""
        return self.vertical != 0 or self.horizontal != 0 or self.distortion != 0

    def to_json(self) -> dict[str, Any]:
        return {
            "straighten": self.straighten,
            "vertical": self.vertical,
            "horizontal": self.horizontal,
            "distortion": self.distortion,
            "rotate90": self.rotate90 % 4,
            "flip": self.flip,
        }


@dataclass(frozen=True)
class FrameSpec:
    style: str = "none"
    color: str = "white"
    weight: str = "medium"
    show_logo: bool = True
    show_params: bool = True
    show_date: bool = False
    text: str = ""
    canvas_ratio: str = "4:5"
    keep_ratio: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "style": self.style,
            "color": self.color,
            "weight": self.weight,
            "show_logo": self.show_logo,
            "show_params": self.show_params,
            "show_date": self.show_date,
            "text": self.text,
            "canvas_ratio": self.canvas_ratio,
            "keep_ratio": self.keep_ratio,
        }


@dataclass(frozen=True)
class StampSpec:
    enabled: bool = False
    content: str = "date"
    date_format: str = "'yy mm dd"
    show_time: bool = False
    date_override: str | None = None
    text: str = ""
    font: str = "seg7"
    color: str = "orange"
    size: str = "medium"
    corner: str = "br"
    glow: bool = True
    placement: str = "photo"

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "enabled": self.enabled,
            "content": self.content,
            "date_format": self.date_format,
            "show_time": self.show_time,
            "date_override": self.date_override,
            "text": self.text,
            "font": self.font,
            "color": self.color,
            "size": self.size,
            "corner": self.corner,
            "glow": self.glow,
        }
        if self.placement != "photo":
            out["placement"] = self.placement
        return out


def _num(value: Any, lo: float, hi: float, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lo <= float(value) <= hi:
        raise EditError("bad_geometry", INVALID_REQUEST, f"{what}要在 {lo:g} 到 {hi:g} 之间。")
    return float(value)


def _slider(value: Any, what: str) -> int:
    number = _num(value, -SLIDER, SLIDER, what)
    if not float(number).is_integer():
        raise EditError("bad_geometry", INVALID_REQUEST, f"{what}要是整数。")
    return int(number)


def parse_geometry(raw: Any) -> Geometry:
    if raw is None:
        return Geometry()
    if not isinstance(raw, dict):
        raise EditError("bad_geometry", "纠正参数格式不对。请重新打开这张照片后再试。")
    known = {"straighten", "vertical", "horizontal", "distortion", "rotate90", "flip"}
    unknown = set(raw) - known
    if unknown:
        raise EditError("bad_geometry", INVALID_REQUEST, "有无法识别的纠正参数。")
    turns = raw.get("rotate90", 0)
    if isinstance(turns, bool) or not isinstance(turns, int) or turns not in (0, 1, 2, 3):
        raise EditError("bad_geometry", INVALID_REQUEST, "向左旋转只能是 0 到 3 次。")
    flipped = raw.get("flip", False)
    if not isinstance(flipped, bool):
        raise EditError("bad_geometry", INVALID_REQUEST, "水平翻转只能打开或关闭。")
    return Geometry(
        straighten=round(_num(raw.get("straighten", 0), -45, 45, "拉直"), 4),
        vertical=_slider(raw.get("vertical", 0), "垂直"),
        horizontal=_slider(raw.get("horizontal", 0), "水平"),
        distortion=_slider(raw.get("distortion", 0), "畸变"),
        rotate90=turns,
        flip=flipped,
    )


def parse_frame(raw: Any) -> FrameSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise EditError("bad_frame", "边框设置格式不对。请重新选择样式后再试。")
    style = raw.get("style", "none")
    if style not in {"none", "gallery", "leica", "hasselblad", "film", "instant", "canvas"}:
        raise EditError("bad_frame", INVALID_REQUEST, "没有这个边框样式。")
    color = raw.get("color", "white")
    if color not in {"white", "black", "cream"}:
        raise EditError("bad_frame", INVALID_REQUEST, "边框颜色只能是白、黑或米白。")
    weight = raw.get("weight", "medium")
    if weight not in {"thin", "medium", "thick"}:
        raise EditError("bad_frame", INVALID_REQUEST, "边框宽度只能是细、中或粗。")
    ratio = raw.get("canvas_ratio", "4:5")
    if ratio not in {"1:1", "3:4", "2:3", "4:5", "3:2", "9:16"}:
        raise EditError("bad_frame", INVALID_REQUEST, "画布比例不在可选范围内。")
    text = raw.get("text", "")
    if not isinstance(text, str) or len(text) > STAMP_TEXT_MAX:
        raise EditError("bad_frame", INVALID_REQUEST, f"边框文字最多 {STAMP_TEXT_MAX} 个字。")
    for key in ("show_logo", "show_params", "show_date", "keep_ratio"):
        if key in raw and not isinstance(raw[key], bool):
            raise EditError("bad_frame", INVALID_REQUEST, "边框的显示开关只能打开或关闭。")
    return FrameSpec(
        style=style,
        color=color,
        weight=weight,
        show_logo=bool(raw.get("show_logo", True)),
        show_params=bool(raw.get("show_params", True)),
        show_date=bool(raw.get("show_date", False)),
        text=text,
        canvas_ratio=ratio,
        keep_ratio=raw.get("keep_ratio", True),
    )


def parse_stamp(raw: Any) -> StampSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise EditError("bad_stamp", "时间戳设置格式不对。请重新打开后再试。")
    placement = raw.get("placement", "photo")
    if not isinstance(placement, str) or placement not in {"photo", "border"}:
        raise EditError("bad_stamp", INVALID_REQUEST, "时间戳只能放在照片或拍立得边框上。")
    content = raw.get("content", "date")
    if content not in {"date", "text", "date+text"}:
        raise EditError("bad_stamp", INVALID_REQUEST, "时间戳内容只能是日期、文字，或日期和文字。")
    font = raw.get("font", "seg7")
    if font not in {"seg7", "typewriter", "sans"}:
        raise EditError("bad_stamp", INVALID_REQUEST, "时间戳字体只能是数码管、打字机或无衬线。")
    color = raw.get("color", "orange")
    if color not in {"orange", "red", "yellow", "white"}:
        raise EditError("bad_stamp", INVALID_REQUEST, "时间戳颜色只能是橙、红、黄或白。")
    size = raw.get("size", "medium")
    if size not in {"small", "medium", "large"}:
        raise EditError("bad_stamp", INVALID_REQUEST, "时间戳大小只能是小、中或大。")
    corner = raw.get("corner", "br")
    if corner not in {"tl", "tr", "bl", "br"}:
        raise EditError("bad_stamp", INVALID_REQUEST, "时间戳位置只能是四个角之一。")
    text = raw.get("text", "")
    if not isinstance(text, str) or len(text) > STAMP_TEXT_MAX:
        raise EditError("bad_stamp", INVALID_REQUEST, f"时间戳文字最多 {STAMP_TEXT_MAX} 个字。")
    override = raw.get("date_override")
    if override is not None:
        if not isinstance(override, str):
            raise EditError("bad_stamp", "指定日期要用 YYYY-MM-DD。请重新选择日期后再试。")
        try:
            datetime.strptime(override, "%Y-%m-%d")
        except ValueError as exc:
            raise EditError("bad_stamp", INVALID_REQUEST, "指定日期无效。") from exc
    for key in ("enabled", "show_time", "glow"):
        if key in raw and not isinstance(raw[key], bool):
            raise EditError("bad_stamp", INVALID_REQUEST, "时间戳的开关只能打开或关闭。")
    fmt = raw.get("date_format", "'yy mm dd")
    if not isinstance(fmt, str) or fmt not in {"'yy mm dd", "yy mm dd", "yyyy.mm.dd", "mm dd 'yy"}:
        raise EditError("bad_stamp", INVALID_REQUEST, "没有这个日期格式。")
    return StampSpec(
        enabled=bool(raw.get("enabled", False)),
        content=content,
        date_format=fmt,
        show_time=bool(raw.get("show_time", False)),
        date_override=override,
        text=text,
        font=font,
        color=color,
        size=size,
        corner=corner,
        glow=bool(raw.get("glow", True)),
        placement=placement,
    )


def factory_frame() -> FrameSpec:
    return FrameSpec()


def factory_stamp() -> StampSpec:
    return StampSpec()
