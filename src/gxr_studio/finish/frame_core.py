"""Integer canvas solver and cap-aligned text from the approved frame reference.

frameLayout in finish_frame_core.js mirrors these equations. Items are ordered:
background, underlays, photo, overlays. New styles can add drawing kinds here.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .layout import Metrics

Item = dict[str, Any]
COLORS = {"white": (255, 255, 255), "black": (14, 14, 16), "cream": (244, 239, 230)}
LOGO_SIZE = (2400, 433)
LOGO_INK_Y = (9, 420)


class Type:
    def __init__(self, metrics: Metrics) -> None:
        self.m = metrics

    def pens(self, font: str, text: str, track: float = 0) -> list[float]:
        upm = self.m.upm(font, False)
        cjk = any(ord(ch) >= 0x2E80 for ch in text)
        extra = 0 if cjk else round(track * upm)
        pens = [0.0]
        for i, ch in enumerate(text):
            is_cjk = ord(ch) >= 0x2E80
            adv = self.m.glyph(font, ch, is_cjk)[0] * upm / self.m.upm(font, is_cjk)
            pens.append(pens[-1] + adv + (extra if i < len(text) - 1 else 0))
        return pens

    def advance(self, font: str, px: int, text: str, track: float = 0) -> float:
        return self.pens(font, text, track)[-1] * px / self.m.upm(font, False)

    def cap_box(self, font: str, px: int) -> tuple[int, int]:
        box = self.m.glyph(font, "H", False)[1]
        if box is None:
            raise ValueError(f"Frame font {font} has no H outline")
        scale = px / self.m.upm(font, False)
        asc = self.m.ascent(font, px, False)
        return math.floor(asc - box[3] * scale), math.ceil(asc - box[1] * scale)

    def cap_h(self, font: str, px: int) -> int:
        top, bottom = self.cap_box(font, px)
        return bottom - top

    def px_for_cap(self, font: str, target: float) -> int:
        px = 8
        while self.cap_h(font, px) < target:
            px += 1
        return px

    def sizes(self, wref: int) -> Sizes:
        return Sizes(self, wref)

    def text(self, x: float, y: float, px: int, font: str, text: str, fill: tuple[int, int, int], track: float = 0) -> Item:
        if any(ord(ch) >= 0x2E80 for ch in text):
            track = 0
        return {
            "k": "text",
            "x": x,
            "y": math.floor(y + 0.5),
            "px": px,
            "font": font,
            "text": text,
            "fill": fill,
            "track": track,
            "cjk": False,
            "xs": [math.floor(x + p * px / self.m.upm(font, False) + 0.5) for p in self.pens(font, text, track)[:-1]],
        }

    def cap_text(
        self, x: float, y_mid: float, px: int, font: str, text: str, fill: tuple[int, int, int], track: float = 0, anchor: str = "l"
    ) -> Item:
        width = self.advance(font, px, text, track)
        x0 = x if anchor == "l" else x - (width / 2 if anchor == "c" else width)
        top, bottom = self.cap_box(font, px)
        return self.text(x0, y_mid - (top + bottom) / 2, px, font, text, fill, track)

    def base_text(
        self, x: float, baseline: float, px: int, font: str, text: str, fill: tuple[int, int, int], track: float = 0, anchor: str = "l"
    ) -> Item:
        width = self.advance(font, px, text, track)
        x0 = x if anchor == "l" else x - (width / 2 if anchor == "c" else width)
        return self.text(x0, baseline - self.m.ascent(font, px, False), px, font, text, fill, track)

    def clip_text(self, text: str, font: str, px: int, max_w: float, track: float = 0) -> str:
        if self.advance(font, px, text, track) <= max_w:
            return text
        while len(text) > 1 and self.advance(font, px, text + "...", track) > max_w:
            text = text[:-1]
        return text.rstrip() + "..."


class Sizes:
    def __init__(self, t: Type, wref: int) -> None:
        self.t, self.wref = t, wref

    def p1(self, font: str) -> int:
        return self.t.px_for_cap(font, 0.015 * self.wref)

    def p2(self, font: str) -> int:
        return self.t.px_for_cap(font, 0.011 * self.wref)

    def cap(self, font: str, fraction: float) -> int:
        return self.t.px_for_cap(font, fraction * self.wref)


def join(parts: list[str], sep: str = "  ") -> str:
    return sep.join(p for p in parts if p)


def frame_index(meta: dict[str, str]) -> int:
    try:
        n = int(meta.get("frame_no") or "0")
    except ValueError:
        n = 0
    return (n - 1) % 36 + 1 if n else 1


def shift_items(items: list[Item], dx: int, dy: int) -> list[Item]:
    out = []
    for orig in items:
        it = dict(orig)
        for key in ("x", "x1", "x2", "cx", "gx"):
            if key in it:
                it[key] += dx
        for key in ("y", "y1", "y2", "cy", "gy"):
            if key in it:
                it[key] += dy
        if "xs" in it:
            it["xs"] = [x + dx for x in it["xs"]]
        for key in ("pts", "hole_pts"):
            if key in it:
                it[key] = [[p[0] + dx, p[1] + dy] for p in it[key]]
        out.append(it)
    return out


def make_unit(
    w: int,
    h: int,
    photo_xy: tuple[int, int],
    pw: int,
    ph: int,
    *,
    under: list[Item] | None = None,
    over: list[Item] | None = None,
    bg: tuple[int, int, int] = (255, 255, 255),
    table: tuple[int, int, int] | None = None,
    bgfx: Item | None = None,
    grain: float = 0.0,
) -> Item:
    return {
        "w": w,
        "h": h,
        "px": photo_xy[0],
        "py": photo_xy[1],
        "pw": pw,
        "ph": ph,
        "under": under or [],
        "over": over or [],
        "bg": bg,
        "table": table,
        "bgfx": bgfx,
        "grain": grain,
    }


def assemble(u: Item, width: int, height: int, x: int, y: int, fixed: bool) -> Item:
    under = [i for i in u["under"] if fixed or not i.get("fx_only")]
    over = [i for i in u["over"] if fixed or not i.get("fx_only")]
    photo = {"k": "photo", "x": u["px"] + x, "y": u["py"] + y, "w": u["pw"], "h": u["ph"]}
    return {
        "width": width,
        "height": height,
        "photo": (photo["x"], photo["y"]),
        "bg": u["table"] if fixed and u.get("table") is not None else u["bg"],
        "bgfx": u.get("bgfx") if fixed or u.get("bgfx_always") else None,
        "items": shift_items(under, x, y) + [photo] + shift_items(over, x, y),
        "unit_box": (x, y, u["w"], u["h"]),
        "fixed": fixed,
        "grain": u.get("grain", 0.0),
    }


def solve(
    unit_fn: Callable[[int], Item],
    ratio: tuple[int, int] | None,
    w_hint: int,
    *,
    natural: tuple[int, int, int],
    f_side: float = 0.06,
    f_top: float = 0.06,
    f_bot: float = 0.04,
    split: float = 0.46,
) -> Item:
    ms, mt, mb = natural
    cap_ref = round(1.9 * w_hint)
    if ratio is None:
        u = unit_fn(w_hint + 2 * ms)
        for _ in range(2):
            u = unit_fn(min(u["w"] + 2 * ms, cap_ref))
        return assemble(u, u["w"] + 2 * ms, u["h"] + mt + mb, ms, mt, False)
    rw, rh = ratio
    u = unit_fn(w_hint)
    k = max(-(-u["w"] * 100 // (round((1 - 2 * f_side) * 100) * rw)), -(-u["h"] * 100 // (round((1 - f_top - f_bot) * 100) * rh)), 1)
    while True:
        width, height = k * rw, k * rh
        u = unit_fn(min(max(width, round(0.62 * height)), cap_ref))
        sm, tm, bm = math.ceil(f_side * width), math.ceil(f_top * height), math.ceil(f_bot * height)
        if width - u["w"] >= 2 * sm and height - u["h"] >= tm + bm:
            break
        k += 1
    free = height - u["h"]
    y = max(tm, min(free - bm, round(split * free)))
    return assemble(u, width, height, (width - u["w"]) // 2, y, True)


def rotate_unit_ccw(unit: Item) -> Item:
    wl = unit["w"]

    def rot(it: Item) -> Item:
        n = dict(it)
        kind = it["k"]
        if kind in ("rect", "hole", "shadow"):
            n["x"], n["y"], n["w"], n["h"] = it["y"], wl - it["x"] - it["w"], it["h"], it["w"]
            if "dy" in it:
                n["dx"], n["dy"] = it["dy"], 0
        elif kind == "poly":
            n["pts"] = [[p[1], wl - p[0]] for p in it["pts"]]
        elif kind == "line":
            n["x1"], n["y1"], n["x2"], n["y2"] = it["y1"], wl - it["x1"], it["y2"], wl - it["x2"]
        elif kind == "text":
            # Rotation uses the very same tracked advances that produced xs.
            width = it["tw"]
            x0 = round(it["x"])
            n.update(rot=True, gx=int(it["y"]), gy=wl - x0 - width, xs0=[x - x0 for x in it["xs"]])
        return n

    out = dict(unit)
    out.update(
        w=unit["h"],
        h=wl,
        px=unit["py"],
        py=wl - unit["px"] - unit["pw"],
        pw=unit["ph"],
        ph=unit["pw"],
        under=[rot(i) for i in unit["under"]],
        over=[rot(i) for i in unit["over"]],
    )
    return out
